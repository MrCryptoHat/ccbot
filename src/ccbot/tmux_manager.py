"""Tmux session/window management via libtmux.

Wraps libtmux to provide async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name: discover Claude Code windows.
  - agent_running_ids / is_agent_running: does a window's agent still hold its
    terminal — the dead-window check; the rule is pane_agent_running.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys: forward user input or control keys to a window.
  - create_window / kill_window: lifecycle management.

All blocking libtmux calls are wrapped in asyncio.to_thread().

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import libtmux

from .config import config
from .procinfo import foreground_process_groups
from .runtimes import get_runtime
from .utils import CCBOT_DIR_ENV

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PaneState:
    """What tmux reports about one pane — enough to judge its agent's liveness."""

    pid: int | None  # pane_pid: the pane's root process, normally its shell
    dead: bool  # pane_dead: the root exited and remain-on-exit kept the pane
    start_command: str  # pane_start_command as tmux renders it; "" = default shell


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory of the active pane
    panes: list[PaneState] = field(default_factory=list)


# tmux renders a pane's start command in its own quoting (args_escape): wrapped
# in "…" or '…' when it holds shell-special characters, with backslash escapes
# inside — \\ \" \$ \` and \~ for the character itself, C-style (\t, \n) or
# octal (\033) for control bytes. Not shell quoting: shlex keeps "\$" as-is.
_TMUX_ESCAPE_RE = re.compile(r"\\([0-7]{3}|.)", re.DOTALL)
_TMUX_CSTYLE = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _tmux_unescape(rendered: str) -> str:
    """Undo tmux's quoting of a single-argument start command."""
    body = rendered
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1]

    def _char(match: re.Match[str]) -> str:
        code = match.group(1)
        if len(code) == 3:
            return chr(int(code, 8))
        return _TMUX_CSTYLE.get(code, code)

    return _TMUX_ESCAPE_RE.sub(_char, body)


def _starts_default_shell(start_command: str, default_command: str) -> bool:
    """Did tmux start this pane with its default shell rather than a program?

    Empty means tmux ran ``default-shell``. A set ``default-command`` (``set -g
    default-command "$SHELL"`` is a common dotfile line) is copied into EVERY
    command-less pane's start command in tmux's quoting (``zsh -l`` →
    ``"zsh -l"``) — compare it with that quoting undone, or every pane on such
    a host reads as "started with a program" and is never reaped. A rendering
    this doesn't decode just fails to match, i.e. reads as running.
    """
    if not start_command:
        return True
    if not default_command:
        return False
    return _tmux_unescape(start_command) == default_command


def pane_agent_running(
    pane: PaneState, foreground: Mapping[int, int], default_command: str
) -> bool:
    """Does something other than the pane's own shell hold its terminal?

    ccbot types the launch command into a shell pane, so "the agent exited" is
    exactly "the shell has the terminal back": the tty's foreground process
    group is the shell's own again (``foreground[pid] == pid``). That is a
    kernel fact, the same for every CLI, version and OS. The process NAME is
    not: codex runs as ``codex``, a native-install Claude Code as its version
    (``2.1.267`` on macOS), and after a CLI self-update the installed binary
    resolves to the NEW version while every window launched earlier still runs
    the old one. Each name-based check reaped live windows until the next
    variant — codex at launch, native claude at launch, then every window
    minutes after a background update.

    Every unknown answers "running": a false "dead" kills a live session, a
    false "running" only delays cleanup. A Ctrl-Z'd agent does hand the
    terminal back and reads as exited — deliberately, since text sent to that
    pane would now run as shell commands.
    """
    if pane.dead:
        # Before anything else: a remain-on-exit pane keeps its exited root's
        # stale pid, which the unknown-means-running rule would keep forever.
        return False
    if not _starts_default_shell(pane.start_command, default_command):
        # Started with a program (e.g. a window adopted from outside ccbot):
        # the root IS the program and holds the terminal itself, so the shell
        # rule would read it as idle. It lives exactly as long as the pane.
        return True
    if pane.pid is None:
        return True
    tpgid = foreground.get(pane.pid)
    if tpgid is None or tpgid <= 0:
        return True
    return tpgid != pane.pid


def _pane_state(pane: libtmux.Pane) -> PaneState:
    """PaneState from a libtmux pane row — every field arrives as a string."""
    try:
        pid = int(pane.pane_pid) if pane.pane_pid else None
    except ValueError:
        pid = None
    return PaneState(
        pid=pid,
        dead=pane.pane_dead == "1",  # "0" is a truthy string
        start_command=pane.pane_start_command or "",
    )


class TmuxManager:
    """Manages tmux windows for Claude Code sessions."""

    def __init__(self, session_name: str | None = None):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
        """
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            self._export_ccbot_dir(session)
            return session

        # Create new session with main window named specifically
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        # Rename the default window to the main window name
        if session.windows:
            session.windows[0].rename_window(config.tmux_main_window_name)
        self._scrub_session_env(session)
        self._export_ccbot_dir(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN.
        """
        for var in config.sensitive_env_vars:
            try:
                session.unset_environment(var)
            except Exception:
                pass  # var not set in session env — nothing to remove

    @staticmethod
    def _export_ccbot_dir(session: libtmux.Session) -> None:
        """Publish the bot's resolved CCBOT_DIR to the tmux session env.

        The SessionStart hook runs inside agent panes and inherits the tmux
        *server* environment, not the bot's — so a CCBOT_DIR that reached the
        bot only via `.env` (which the hook deliberately never reads) would
        make the hook write session_map.json to ~/.ccbot while the bot reads
        it from $CCBOT_DIR: replies silently stop arriving. Exporting the
        resolved path into the session env makes every window created after
        this point see the same directory the bot uses.
        """
        try:
            session.set_environment(CCBOT_DIR_ENV, str(config.config_dir))
        except Exception:
            logger.warning("Failed to export CCBOT_DIR into tmux session env")

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        One ``list-panes -s`` for the whole session — every row already names
        its window. (``window.active_pane`` cost a tmux call per window, on a
        poll loop that lists twice a second.)

        Returns:
            List of TmuxWindow with window info, active-pane cwd and pane states
        """

        def _sync_list_windows() -> list[TmuxWindow]:
            session = self.get_session()
            if not session:
                return []

            windows: dict[str, TmuxWindow] = {}
            for pane in session.panes:
                window_id = pane.window_id or ""
                name = pane.window_name or ""
                # Skip the main window (placeholder window)
                if not window_id or name == config.tmux_main_window_name:
                    continue
                window = windows.get(window_id)
                if window is None:
                    window = windows[window_id] = TmuxWindow(
                        window_id=window_id,
                        window_name=name,
                        cwd=pane.pane_current_path or "",
                    )
                elif pane.pane_active == "1":
                    window.cwd = pane.pane_current_path or ""
                window.panes.append(_pane_state(pane))

            return list(windows.values())

        return await asyncio.to_thread(_sync_list_windows)

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name.

        Args:
            window_name: The window name to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        Args:
            window_id: The tmux window ID to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_id == window_id:
                return window
        logger.debug("Window not found by id: %s", window_id)
        return None

    async def agent_running_ids(self, windows: Iterable[TmuxWindow]) -> set[str]:
        """IDs of the windows whose agent still holds its terminal.

        A window counts while ANY of its panes does: a user who splits an agent
        window and focuses the shell half must not get the agent reaped. A
        window with no pane data counts too — nothing to judge by. The rule
        itself is pane_agent_running.
        """
        windows = list(windows)

        def _sync() -> set[str]:
            panes = [p for w in windows for p in w.panes]
            # Dead panes are never looked up: their pid is stale, maybe reused.
            foreground = foreground_process_groups(
                p.pid for p in panes if p.pid is not None and not p.dead
            )
            # Only a pane started with a command needs default-command to be
            # judged — skip the tmux call when no pane has one.
            default_command = (
                self._default_command() if any(p.start_command for p in panes) else ""
            )
            return {
                w.window_id
                for w in windows
                if not w.panes
                or any(
                    pane_agent_running(p, foreground, default_command) for p in w.panes
                )
            }

        return await asyncio.to_thread(_sync)

    async def is_agent_running(self, window: TmuxWindow) -> bool:
        """Whether this window's agent still holds its terminal."""
        return window.window_id in await self.agent_running_ids([window])

    def _default_command(self) -> str:
        """The session's effective ``default-command`` ("" when unset or unknown).

        Unknown degrades safely: a start command that then fails to match is
        judged "started with a program", i.e. running.
        """
        session = self.get_session()
        if not session:
            return ""
        try:
            lines = session.cmd("show-options", "-Av", "default-command").stdout
        except Exception:
            return ""
        return lines[0] if lines else ""

    async def ensure_session_pane_size(self, cols: int, rows: int) -> None:
        """Pin the session's pane size for screenshots.

        tmux sizes panes to the smallest attached client; with no client
        attached the session falls back to ``default-size`` (80x24 by
        default). Without this override capture-pane returns 80-column
        text and Claude Code's footer wraps off-screen in /screenshot.

        Sets ``default-size`` so newly created windows inherit, then
        resizes every existing window. If a real client (SSH, mosh) is
        attached at a smaller size, the smaller size wins for the
        duration of the attach — this is a best-effort floor.
        """
        # default-size for new windows.
        await asyncio.create_subprocess_exec(
            "tmux",
            "set-option",
            "-t",
            self.session_name,
            "default-size",
            f"{cols}x{rows}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Resize live windows.
        session = self.get_session()
        if not session:
            return
        for window in list(session.windows):
            wid = window.window_id
            if not wid:
                continue
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "resize-window",
                "-t",
                f"{self.session_name}:{wid}",
                "-x",
                str(cols),
                "-y",
                str(rows),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.debug(
                    "resize-window failed for %s: %s",
                    wid,
                    stderr.decode(errors="replace").strip(),
                )

    async def capture_pane(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes
            scrollback_lines: If >0, include this many rows of scrollback
                above the visible area (passes `-S -<N>` to tmux). Use when
                the caller needs to see content that may have scrolled off
                — /context's output, for example, regularly exceeds the
                50-row viewport.

        Returns:
            The captured text, or None on failure.
        """
        if with_ansi or scrollback_lines > 0:
            # CLI path covers both ANSI capture and scrollback. libtmux's
            # pane.capture_pane() defaults to visible-only and we don't
            # need the extra knob in two places.
            argv = ["tmux", "capture-pane", "-p", "-t", window_id]
            if with_ansi:
                argv.insert(2, "-e")
            if scrollback_lines > 0:
                argv.extend(["-S", f"-{scrollback_lines}"])
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return stdout.decode("utf-8")
                logger.error(
                    f"Failed to capture pane {window_id}: {stderr.decode('utf-8')}"
                )
                return None
            except Exception as e:
                logger.error(f"Unexpected error capturing pane {window_id}: {e}")
                return None

        # Original implementation for plain text - wrap in thread
        def _sync_capture() -> str | None:
            session = self.get_session()
            if not session:
                return None
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return None
                pane = window.active_pane
                if not pane:
                    return None
                lines = pane.capture_pane()
                return "\n".join(lines) if isinstance(lines, list) else str(lines)
            except Exception as e:
                logger.error(f"Failed to capture pane {window_id}: {e}")
                return None

        return await asyncio.to_thread(_sync_capture)

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Args:
            window_id: The window ID to send to
            text: Text to send
            enter: Whether to press enter after the text
            literal: If True, send text literally. If False, interpret special keys
                     like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns:
            True if successful, False otherwise
        """
        if literal and enter:
            # Split into text + delay + Enter via libtmux.
            # Claude Code's TUI sometimes interprets a rapid-fire Enter
            # (arriving in the same input batch as the text) as a newline
            # rather than submit.  A 500ms gap lets the TUI process the
            # text before receiving Enter.
            def _send_literal(chars: str) -> bool:
                session = self.get_session()
                if not session:
                    logger.error("No tmux session found")
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        logger.error(f"Window {window_id} not found")
                        return False
                    pane = window.active_pane
                    if not pane:
                        logger.error(f"No active pane in window {window_id}")
                        return False
                    # Not pane.send_keys(literal=True): libtmux builds
                    # `send-keys -l <text>` without `--`, so text starting
                    # with "-" is eaten as tmux flags and the send fails.
                    res = pane.cmd("send-keys", "-l", "--", chars)
                    if res.stderr:
                        logger.error(
                            f"send-keys failed for window {window_id}: {res.stderr}"
                        )
                        return False
                    return True
                except Exception as e:
                    logger.error(f"Failed to send keys to window {window_id}: {e}")
                    return False

            def _send_enter() -> bool:
                session = self.get_session()
                if not session:
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        return False
                    pane = window.active_pane
                    if not pane:
                        return False
                    pane.send_keys("", enter=True, literal=False)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send Enter to window {window_id}: {e}")
                    return False

            # Claude Code's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if text.startswith("!"):
                if not await asyncio.to_thread(_send_literal, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    if not await asyncio.to_thread(_send_literal, rest):
                        return False
            else:
                # Split long text into chunks to avoid tmux buffer limits
                chunk_size = 200
                for i in range(0, len(text), chunk_size):
                    chunk = text[i : i + chunk_size]
                    if not await asyncio.to_thread(_send_literal, chunk):
                        return False
                    if i + chunk_size < len(text):
                        await asyncio.sleep(0.1)
            # Longer delay for multi-line/long text to let TUI process paste
            delay = 1.5 if len(text) > 200 else 0.5
            await asyncio.sleep(delay)
            return await asyncio.to_thread(_send_enter)

        # Other cases: special keys (literal=False) or no-enter
        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error(f"Window {window_id} not found")
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error(f"No active pane in window {window_id}")
                    return False

                if literal:
                    # `--` guards text starting with "-" (see _send_literal).
                    res = pane.cmd("send-keys", "-l", "--", text)
                    if res.stderr:
                        logger.error(
                            f"send-keys failed for window {window_id}: {res.stderr}"
                        )
                        return False
                    if enter:
                        pane.send_keys("", enter=True, literal=False)
                else:
                    pane.send_keys(text, enter=enter, literal=literal)
                return True

            except Exception as e:
                logger.error(f"Failed to send keys to window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_send_keys)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except Exception as e:
                logger.error(f"Failed to rename window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_rename)

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID."""

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                return True
            except Exception as e:
                logger.error(f"Failed to kill window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_kill)

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
        runtime: str = "claude",
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start the agent CLI.

        Args:
            work_dir: Working directory for the new window
            window_name: Optional window name (defaults to directory name)
            start_claude: Whether to launch the agent CLI (kept name for
                back-compat; applies to whichever runtime)
            resume_session_id: If set (and well-formed), resume that session
            runtime: Agent runtime to launch — "claude" (default) or "codex".
                The launch/resume command is built by runtimes.get_runtime.

        Returns:
            Tuple of (success, message, window_name, window_id)
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # Create window name, adding suffix if name already exists
        final_window_name = window_name if window_name else path.name

        # Check for existing window name
        base_name = final_window_name
        counter = 2
        while await self.find_window_by_name(final_window_name):
            final_window_name = f"{base_name}-{counter}"
            counter += 1

        # Create window in thread
        def _create_and_start() -> tuple[bool, str, str, str]:
            session = self.get_or_create_session()
            try:
                # Create new window
                window = session.new_window(
                    window_name=final_window_name,
                    start_directory=str(path),
                )

                wid = window.window_id or ""

                # Prevent Claude Code from overriding window name
                window.set_window_option("allow-rename", "off")

                # Start the agent CLI if requested. The launch/resume command
                # is runtime-specific (claude: `claude --name X [--resume Y]`;
                # codex: `codex [resume Y]`) and built by the runtime — which
                # also shlex-quotes the window name and validates the resume id
                # (an unvalidated id typed into the shell is a command-injection
                # vector). (audit HIGH#1 / MEDIUM)
                if start_claude:
                    pane = window.active_pane
                    if pane:
                        cmd = get_runtime(runtime).launch_command(
                            final_window_name, resume_session_id
                        )
                        pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created window '{final_window_name}' at {path}",
                    final_window_name,
                    wid,
                )

            except Exception as e:
                logger.error(f"Failed to create window: {e}")
                return False, f"Failed to create window: {e}", "", ""

        return await asyncio.to_thread(_create_and_start)


# Global instance with default session name
tmux_manager = TmuxManager()
