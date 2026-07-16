"""Agent runtime abstraction — Claude Code, Codex, and future CLIs.

An agent *runtime* is the CLI a topic's window runs (Claude Code, OpenAI Codex,
…). It is a second axis, independent of the *transport* (tmux vs docker — see
session.py / resolve_binding). This module isolates EVERY runtime-specific
decision behind ``AgentRuntime`` so the rest of the codebase — the monitor
especially — stays generic: it iterates runtimes and calls the interface, never
branching on ``if codex:`` at a call site (mirrors how transport lives behind
SessionManager wrappers).

Runtime is a property of the *window* (``WindowState.runtime``), NEVER encoded
in the binding string — ``@<id>`` / ``docker:<agent>`` stay transport-only, so
``resolve_binding`` is untouched. It defaults to ``"claude"`` for legacy state
(zero migration).

The interface a runtime implements:
  - ``launch_command``          — how to start/resume the CLI in a fresh pane.
  - ``iter_transcripts``        — locate the live transcript file(s) of bound
                                   windows (Claude: session_map + scan_projects;
                                   Codex: rollout resolved by cwd).
  - ``parse_entries``           — turn raw transcript lines into the shared
                                   ``ParsedEntry`` (Claude & Codex schemas differ;
                                   the output type does not).
  - ``latest_context_tokens``   — context-window fill for the context alert
                                   (runtime-specific usage shape; None to skip).
  - ``list_sessions``           — resumable sessions for a directory, for the
                                   picker's per-runtime tab (Claude: project
                                   glob; Codex: rollout-by-cwd; → ``AgentSession``).

Everything downstream of ``parse_entries`` (NewMessage → queue → voice / tables
/ pins) is runtime-agnostic. Adding a third model = one ``AgentRuntime``
subclass implementing these; no monitor rewrite, and it becomes a picker tab
automatically (``pickable_runtimes()`` walks the registry).

Window-bootstrap divergence is expressed as *capabilities*, not name checks:
``uses_session_map`` (hook wait / cwd persistence / stale-window sweep),
``auto_forward_first_message``, ``ready_message_key``, ``interrupt_keys``
(/esc), ``is_available`` (picker-tab gating on an installed CLI),
``history_transcript`` (the /commands history source). Call sites must never
compare ``runtime.name``.

Key API: ``get_runtime(name)`` → AgentRuntime; module singletons ``CLAUDE`` /
``CODEX``; ``RUNTIMES`` registry; ``monitored_runtimes()`` /
``pickable_runtimes()`` (installed CLIs only) / ``default_runtime()``
(CCBOT_DEFAULT_RUNTIME, validated + availability-checked); ``is_valid_runtime``.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from .agent_session import AgentSession
from .codex_transcript_parser import CodexTranscriptParser
from .config import config
from .terminal_parser import (
    has_codex_queued_messages,
    has_queued_messages,
    is_claude_working,
    is_codex_working,
)
from .transcript_parser import ParsedEntry, TranscriptParser
from .utils import is_valid_session_id

logger = logging.getLogger(__name__)

CLAUDE_RUNTIME = "claude"
CODEX_RUNTIME = "codex"

# A transcript reference the monitor tracks: (session_id, file_path).
TranscriptRef = tuple[str, Path]

# Session id embedded in a Codex rollout filename
# (rollout-<ISO-ts>-<uuid>.jsonl). Codex uses UUIDv7 (8-4-4-4-12 hex).
_CODEX_ROLLOUT_SID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# Newest rollouts scanned when listing a cwd's codex sessions for the picker.
# Only the cheap cached first-line read runs per file; the full read (summary +
# count) runs for the ≤10 that match the cwd. Bounds a large ~/.codex tree.
_CODEX_LIST_SCAN_CAP = 150
_CODEX_LIST_MAX = 10
# Newest rollouts scanned when resolving live windows' transcripts each monitor
# tick. A window whose rollout falls outside this prefix is covered by the
# sticky ``_last_rollout`` fallback (see CodexRuntime.__init__).
_CODEX_SCAN_CAP = 60


def _entry_context_tokens(entry: dict) -> int | None:
    """Context-window fill (input-side tokens) for a Claude assistant entry.

    Returns input + cache_creation + cache_read tokens — the prompt size Claude
    Code reports as "X/1m" in /context. Output tokens are the *next* turn's
    input, so excluded to match. None for non-assistant entries or ones with no
    usage block. (Lives here, not in the monitor, so the extraction is behind
    the runtime; the threshold bookkeeping stays generic in the monitor.)
    """
    if entry.get("type") != "assistant":
        return None
    usage = (entry.get("message") or {}).get("usage") or {}
    if not usage:
        return None
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )


class AgentRuntime(abc.ABC):
    """One agent CLI's runtime-specific behavior.

    The base class is abstract; use the concrete singletons below (``CLAUDE`` /
    ``CODEX``) and resolve via :func:`get_runtime`. ``session_manager`` /
    ``monitor`` are passed to ``iter_transcripts`` as objects (typed ``Any`` to
    avoid an import cycle — the monitor imports this module).
    """

    #: Stable identifier persisted in ``WindowState.runtime`` and env config.
    name: str = ""
    #: Short label for UI (the runtime picker, status). Bilingual-neutral —
    #: these are product names, not translated strings.
    display_name: str = ""
    #: Whether this runtime's CLI registers sessions via a SessionStart hook
    #: into a session_map (Claude Code). Gates the window-bootstrap behavior at
    #: EVERY creation path (bot._create_and_bind_window, worktree provision,
    #: auto-rebind): a session_map runtime waits for the hook entry and raises
    #: the hook-missing alarm; a runtime WITHOUT one (Codex) skips the wait and
    #: instead needs ``WindowState.cwd`` persisted so the monitor can resolve
    #: its transcript by cwd. Also gates load_session_map's stale-window sweep
    #: (only session_map windows may be reaped for being absent from the map).
    #: Never branch on the runtime *name* for any of this — a third runtime
    #: with its own hook must not silently inherit Codex's treatment.
    uses_session_map: bool = True
    #: Whether the first (pending) topic message may be auto-typed into a
    #: freshly created window. False for CLIs that can open on an interactive
    #: startup screen (Codex's sign-in menu) where blind typing + Enter would
    #: take a step the user didn't choose.
    auto_forward_first_message: bool = True
    #: Keys sent by /esc to interrupt a running turn, in order (tmux key
    #: names). Claude Code takes Escape (agent) + Ctrl-C (bash command);
    #: Codex must NOT get the trailing Ctrl-C — on its TUI an idle Ctrl-C arms
    #: the quit sequence instead of being a no-op.
    interrupt_keys: tuple[str, ...] = ("Escape", "C-c")
    #: Emoji shown next to ``display_name`` on the picker's runtime tab (inactive
    #: tab: ``{icon} {display_name}``; active tab swaps it for a ``▸`` pointer,
    #: matching the agent-panel tab convention). A new runtime just sets its own.
    picker_icon: str = ""

    #: Which of the agent panel's *divergent* buttons this runtime offers, by
    #: logical action id. The panel shows a gated button only when its id is
    #: here (``supports_panel_action``), so the keyboard reflects the agent's
    #: real capabilities instead of assuming Claude Code's command set — no
    #: ``if codex:`` at the call site. Universal keys (esc/arrows/slash/enter/
    #: wipe) and lifecycle (resume/new/restart/end/refresh) are always shown and
    #: NOT listed here. Ids: "mode" (Shift+Tab plan/auto-accept cycle), "effort",
    #: "compact", "clear", "model", "context", "mcp", "background" (Ctrl+B →
    #: background task), "worktree" (fork a sibling agent). A third agent = a new
    #: subclass declaring its own set; the panel builder never changes.
    panel_actions: frozenset[str] = frozenset()

    #: Logical panel-action id → the slash command typed into the pane for that
    #: button. The base map is Claude Code's; a runtime whose command differs
    #: overrides just that entry (e.g. Codex "context" → ``/status``), so the
    #: callback handler resolves the real command through the runtime instead of
    #: a global Claude-shaped table. Actions driven by a KEY (mode = Shift+Tab)
    #: or a lifecycle handler (resume) aren't listed here.
    panel_slash_commands: dict[str, str] = {
        "clear": "/clear",
        "compact": "/compact",
        "model": "/model",
        "mcp": "/mcp",
        "context": "/context",
        "effort": "/effort",
    }

    def supports_panel_action(self, action: str) -> bool:
        """True iff this runtime offers the given gated panel button."""
        return action in self.panel_actions

    def ready_message_key(self, resumed: bool) -> str:
        """i18n key of the "window is ready" message posted after creation.

        ``resumed`` distinguishes a fresh window from a ``--resume`` one. A
        runtime with a startup screen the user must know about (Codex sign-in)
        overrides this to its own key for both cases.
        """
        return "bot.window_resumed" if resumed else "bot.window_ready"

    @abc.abstractmethod
    def cli_command(self) -> str:
        """The configured shell command that launches this runtime's CLI.

        Only the first token is used for availability probing; the full string
        is what ``launch_command`` builds on.
        """
        ...

    def is_available(self) -> bool:
        """True iff this runtime's CLI binary is installed (on PATH).

        Gates the picker tabs (``pickable_runtimes``): a runtime whose binary
        is absent must not be offered — typing a missing command into a fresh
        pane leaves a dead window the health check reaps 30 s later, with no
        explanation the user can act on. Probed per call (cheap PATH stat) so
        installing the CLI needs no bot restart.
        """
        parts = self.cli_command().split()
        if not parts:
            return False
        return shutil.which(os.path.expanduser(parts[0])) is not None

    async def cli_version(self) -> str | None:
        """First line of ``<cli> --version`` output (None if the probe fails).

        Feeds the self-update canary (status_polling's version watcher): agent
        CLIs update themselves in place — codex does so silently — and every
        TUI anchor (busy counter, /status box, diff crop, menu chrome) is
        pinned to the version its patterns were captured on. A version bump is
        the moment those anchors need re-verification, so it must not pass
        unnoticed.
        """
        parts = self.cli_command().split()
        if not parts:
            return None
        binary = os.path.expanduser(parts[0])
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            return None
        lines = out.decode(errors="replace").strip().splitlines()
        return lines[0].strip() if lines else None

    def panel_slash(self, action: str) -> str | None:
        """Slash command this runtime types for a panel action (None if none)."""
        return self.panel_slash_commands.get(action)

    #: /diff (opt-in edit-diff screenshots). ``edit_tool_names`` are the tool_use
    #: names whose activity triggers a pane scan; ``diff_header_re`` matches the
    #: line that STARTS the runtime's native diff block and ``diff_boundary_re``
    #: a line that ENDS it (besides a blank line). Empty / None → the runtime
    #: renders no croppable diff and /diff no-ops for it. diff_view owns the
    #: shared crop engine and reads these (kept as data here to avoid a runtimes
    #: → handlers import cycle).
    edit_tool_names: frozenset[str] = frozenset()
    diff_header_re: re.Pattern[str] | None = None
    diff_boundary_re: re.Pattern[str] | None = None

    def is_edit_tool(self, name: str | None) -> bool:
        """True iff a tool_use with this name should trigger a /diff scan."""
        return bool(name) and name in self.edit_tool_names

    #: How the runtime ingests an inbound image. False (default): a text marker
    #: ``(image attached: <path>)`` is typed and the CLI reads the file itself
    #: (Claude Code). True: the CLI has NATIVE multimodal input — the path is
    #: typed into its composer, which attaches the image CLIENT-side (Codex →
    #: ``[Image #N]``). The client-side read is what makes it work under codex's
    #: sandbox, which blocks reading a path outside the workspace (the bug this
    #: fixes). ``composer_image_token`` is the pane text that confirms the
    #: attach landed, polled by ``session_manager.send_composer_image``.
    native_image_input: bool = False
    composer_image_token: str = ""

    def image_marker(self, path: str) -> str:
        """Text-marker form for a text-marker runtime (native_image_input=False)."""
        return f"(image attached: {path})"

    #: tmux ``pane_current_command`` values that mean "the agent is still running
    #: in this window" — the dead-window health check (status_polling) treats any
    #: OTHER foreground command (a bare ``bash`` the CLI exited back to) as a
    #: crash and reaps the window after a grace period. Claude Code's foreground
    #: is ``claude`` / ``node``; a runtime with a different process name (Codex →
    #: ``codex``) MUST override this or its windows get killed 30 s after launch,
    #: sign-in menu and all. Default is Claude's set (matches get_runtime's
    #: fallback-to-claude).
    pane_alive_commands: frozenset[str] = frozenset({"claude", "node"})

    async def list_sessions(self, session_manager: Any, cwd: str) -> list[AgentSession]:
        """Resumable sessions this runtime has for ``cwd`` (newest first, capped).

        Powers the picker's per-runtime session tab: the user picks Claude Code
        or Codex on top, and this returns *that* runtime's resume candidates for
        the folder. Default: none (a runtime with no resumable transcript store).
        ``session_manager`` is passed as ``Any`` to avoid an import cycle.
        """
        return []

    async def history_transcript(
        self, session_manager: Any, window_id: str
    ) -> Path | None:
        """Path of the window's current transcript for the history view.

        Powers ``get_recent_messages`` (the /commands history pages and unread
        catch-up) — runtime-dispatched because each CLI stores transcripts
        differently. Default: the Claude session_id+cwd JSONL resolution (also
        correct for docker bindings via their projects root).
        """
        session = await session_manager.resolve_session_for_window(window_id)
        if session and session.file_path:
            return Path(session.file_path)
        return None

    @abc.abstractmethod
    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        """Shell command to type into a fresh pane to start (or resume) the agent.

        ``window_name`` is the tmux window/display name; ``resume_session_id``
        resumes a prior session when set and well-formed (a malformed id is
        ignored and a fresh session starts — an unvalidated id is a shell-
        injection vector, so callers must never bypass this).
        """
        ...

    @abc.abstractmethod
    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        """Return ``(session_id, file_path)`` for each live transcript to read.

        Called once per monitor tick. ``active_session_ids`` is the set from the
        (Claude) session_map; runtimes without a session_map (Codex) ignore it
        and resolve by their own means.
        """
        ...

    @abc.abstractmethod
    def parse_entries(
        self, raw_entries: list[dict], pending_tools: dict
    ) -> tuple[list[ParsedEntry], dict]:
        """Parse raw transcript lines into ParsedEntry + carried pending tools."""
        ...

    def latest_context_tokens(self, raw_entries: list[dict]) -> int | None:
        """Latest context-window fill across ``raw_entries`` (None → no alert).

        Default: no context tracking. Runtimes with a usage block override.
        """
        return None

    def is_working(self, pane_text: str) -> bool:
        """True iff the pane shows a turn actively running (interruptible).

        Gates every "don't barge the agent" decision (restart guard, /inject
        busy-gate, task-pin idle check, reaction-confirm). Default is Claude
        Code's status-line detection; a runtime with different TUI chrome
        (Codex) overrides. Behind the runtime, NOT an ``if codex:`` at each call
        site — call sites go through ``session_manager.is_agent_working``.
        """
        return is_claude_working(pane_text)

    def has_queued_input(self, pane_text: str) -> bool:
        """True iff the CLI shows buffered, not-yet-ingested input.

        Drives reaction-ack timing: the 👀 fires when this flips False (the
        queued message entered the agent's context). Default is Claude Code's
        queued-messages hint; Codex overrides with its own.
        """
        return has_queued_messages(pane_text)

    def exit_command(self) -> str:
        """Text typed (+ Enter) to quit the CLI's TUI back to the shell.

        Used by the restart handlers (exit → relaunch in the same pane).
        Default is Claude Code's ``/exit``; Codex overrides.
        """
        return "/exit"


class ClaudeRuntime(AgentRuntime):
    """Claude Code — the legacy/default runtime."""

    name = CLAUDE_RUNTIME
    display_name = "Claude Code"
    picker_icon = "🟠"
    # /diff: Claude Code renders "● Update/Write(path)" blocks with numbered
    # ±gutter lines; a block ends at the next tool bullet (● / ⏺).
    edit_tool_names = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
    diff_header_re = re.compile(
        r"^[●⏺]\s+(Update|Write|Create|Edit|MultiEdit|NotebookEdit)\("
    )
    diff_boundary_re = re.compile(r"^[●⏺]")
    # Claude Code offers the full panel — this is the historical baseline.
    panel_actions = frozenset(
        {
            "mode",
            "effort",
            "compact",
            "clear",
            "model",
            "context",
            "mcp",
            "background",
            "worktree",
        }
    )

    def cli_command(self) -> str:
        return config.claude_command

    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        # shlex.quote (not repr): this string is typed into the pane's shell,
        # and repr() is not shell-safe for a name holding both quote kinds.
        cmd = f"{config.claude_command} --name {shlex.quote(window_name)}"
        if resume_session_id and is_valid_session_id(resume_session_id):
            cmd = f"{cmd} --resume {resume_session_id}"
        elif resume_session_id:
            logger.warning(
                "Ignoring malformed resume session id for window %s; starting fresh",
                window_name,
            )
        return cmd

    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        # scan_projects walks every project root (host + docker bind-mounts);
        # the session_map (active_session_ids) filters to sessions the hook
        # actually registered — exactly the pre-unification behavior.
        sessions = await monitor.scan_projects()
        return [
            (s.session_id, s.file_path)
            for s in sessions
            if s.session_id in active_session_ids
        ]

    def parse_entries(
        self, raw_entries: list[dict], pending_tools: dict
    ) -> tuple[list[ParsedEntry], dict]:
        return TranscriptParser.parse_entries(raw_entries, pending_tools=pending_tools)

    async def list_sessions(self, session_manager: Any, cwd: str) -> list[AgentSession]:
        # The Claude enumeration already lives on SessionManager (globs
        # ~/.claude/projects/<encoded cwd>/*.jsonl); reuse it verbatim.
        return await session_manager.list_sessions_for_directory(cwd)

    def latest_context_tokens(self, raw_entries: list[dict]) -> int | None:
        latest: int | None = None
        for raw in raw_entries:
            t = _entry_context_tokens(raw)
            if t is not None:
                latest = t
        return latest


class CodexRuntime(AgentRuntime):
    """OpenAI Codex CLI.

    Launch: no ``--name`` flag (the tmux window name is set at ``new_window``
    time), resume is ``codex resume <uuid>``. A bare unauthenticated ``codex``
    opens an in-TUI sign-in menu, driven by the shared interactive-UI photo +
    keys (terminal_parser CodexSignIn/CodexLogin patterns).

    Transcript: Codex has no SessionStart-hook session_map, so a window's
    rollout (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``) is located by
    matching its cwd to a rollout's ``session_meta.cwd`` (newest wins). The
    rollout's session id is mirrored into ``WindowState.session_id`` so the
    normal find_users_for_session routing delivers replies to the topic.
    """

    name = CODEX_RUNTIME
    display_name = "Codex"
    picker_icon = "🔵"
    # No SessionStart hook in codex 0.144.x interactive launch — windows are
    # tracked by cwd→rollout matching, so bootstrap must persist cwd and skip
    # the claude session_map wait/alarm (see the base-class capability doc).
    uses_session_map = False
    # A fresh codex can open on its sign-in menu; blind-typing the pending
    # topic message (with Enter) would pick a menu option the user never chose.
    auto_forward_first_message = False
    # Idle Ctrl-C arms codex's quit sequence — /esc sends Escape only.
    interrupt_keys = ("Escape",)
    # Codex's TUI foreground process is `codex` (a native binary, not node) — the
    # dead-window health check must accept it or every codex window is reaped 30 s
    # after launch. codex owns the pane the whole time (subcommands run in its own
    # PTY, not the tmux pane), so a single value suffices.
    pane_alive_commands = frozenset({"codex"})
    # Codex panel set (probed live). Same wire strings as Claude for
    # compact/clear/model/mcp; "mode" reuses Shift+Tab (codex cycles "Plan
    # mode" on back-tab, same key handler); "context" maps to /status (codex has
    # no /context — /status shows model/permissions/account/limits, parsed by
    # codex_status_parser). EXCLUDED: "effort" (folded into /model), "background"
    # (no Ctrl+B background-task), "worktree" (codex worktree not built).
    panel_actions = frozenset({"compact", "clear", "model", "mcp", "mode", "context"})
    panel_slash_commands = {
        **AgentRuntime.panel_slash_commands,
        "context": "/status",  # codex's session-status command (no /context)
    }
    # /diff: codex edits via the apply_patch tool (logged as a custom_tool_call,
    # which codex_transcript_parser now emits as a tool_use). Its native pane
    # block is "• Edited <file> (+N -M)" + numbered ±gutter lines, ended by the
    # next • bullet or the long ─ separator codex draws after a tool block.
    edit_tool_names = frozenset({"apply_patch"})
    diff_header_re = re.compile(r"^[•●]\s+\w+.*\(\+\d+")
    diff_boundary_re = re.compile(r"^[•●]|^─{5,}")
    # Codex has native multimodal input: typing an image path into the composer
    # auto-attaches it as "[Image #N]" (a client-side read that bypasses the
    # sandbox). See session_manager.send_composer_image + media.photo_handler.
    native_image_input = True
    composer_image_token = "[Image #"

    def __init__(self) -> None:
        # rollout path -> its session_meta.cwd (immutable per file); avoids
        # re-reading a rollout header every tick. Instance state on the CODEX
        # singleton.
        self._meta_cwd: dict[str, str | None] = {}
        # cwd -> last successfully resolved rollout path. Sticky fallback: the
        # mtime scan is capped (_CODEX_SCAN_CAP), so on a host with many codex
        # sessions across other projects a quiet window's rollout can fall out
        # of the scanned prefix — without this, its topic silently stops
        # receiving replies. The cached path is re-checked (exists + cwd still
        # matches) before use.
        self._last_rollout: dict[str, Path] = {}

    def cli_command(self) -> str:
        return config.codex_command

    def ready_message_key(self, resumed: bool) -> str:
        # Fresh AND resumed codex windows may open on the sign-in menu — the
        # ready message explains the nav-keys flow either way.
        return "bot.window_codex_ready"

    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        codex = config.codex_command
        # Opt-in (config.codex_bypass_sandbox): run codex unsandboxed. Needed
        # where codex's bundled bubblewrap can't initialize (nested-userns UID
        # mapping blocked) — otherwise codex can't run shell / edit files / read
        # documents. Accepted as an option by both `codex` and `codex resume`.
        flag = (
            " --dangerously-bypass-approvals-and-sandbox"
            if config.codex_bypass_sandbox
            else ""
        )
        if resume_session_id and is_valid_session_id(resume_session_id):
            return f"{codex} resume {resume_session_id}{flag}"
        elif resume_session_id:
            logger.warning(
                "Ignoring malformed resume session id for codex window %s; "
                "starting fresh",
                window_name,
            )
        # Bare `codex`: an unauthenticated launch opens codex's own in-TUI
        # sign-in MENU (ChatGPT / Device Code / API key). We keep the menu
        # (rather than forcing a flow) so the user picks the method — it's
        # detected as an interactive UI (terminal_parser CodexSignIn/CodexLogin
        # patterns) and driven by the SAME photo + ↑↓⏎ nav keyboard + auth-URL
        # surfacing Claude Code's /login uses. Nothing is auto-typed into it
        # (bot.py skips forwarding the first message to codex), so no keypress
        # mis-selects an option.
        return f"{codex}{flag}"

    def parse_entries(
        self, raw_entries: list[dict], pending_tools: dict
    ) -> tuple[list[ParsedEntry], dict]:
        return CodexTranscriptParser.parse_entries(raw_entries, pending_tools)

    def exit_command(self) -> str:
        return "/quit"  # codex's TUI quit (verified: "/quit exit Codex")

    def is_working(self, pane_text: str) -> bool:
        # Codex has no ─ chrome separator for is_claude_working to anchor on;
        # its status line carries a distinctive "(Ns • esc to interrupt)" counter.
        return is_codex_working(pane_text)

    def has_queued_input(self, pane_text: str) -> bool:
        # Codex's "Messages to be submitted after next tool call" hint (its
        # analog of Claude's "Press up to edit queued messages").
        return has_codex_queued_messages(pane_text)

    # latest_context_tokens: Codex's usage block (event_msg/token_count) uses a
    # per-model window (e.g. 258k for gpt-5.5), not the fixed 1M the alert
    # thresholds assume — so context alerts are skipped for now (base returns
    # None). Wire per-model windows before enabling.

    def _rollout_cwd(self, rollout: Path) -> str | None:
        """session_meta.cwd of a codex rollout (cached; the header is immutable)."""
        key = str(rollout)
        if key in self._meta_cwd:
            return self._meta_cwd[key]
        cwd: str | None = None
        try:
            with open(rollout, "r", encoding="utf-8") as fh:
                first = fh.readline()
            data = json.loads(first)
            payload = data.get("payload")
            src = payload if isinstance(payload, dict) else data
            cw = src.get("cwd")
            cwd = cw if isinstance(cw, str) else None
        except (OSError, json.JSONDecodeError, ValueError):
            cwd = None
        self._meta_cwd[key] = cwd
        return cwd

    def _rollout_files_newest_first(self) -> list[Path]:
        """All rollout files under the sessions root, newest first ([] on error)."""
        root = config.codex_sessions_path
        if not root.exists():
            return []
        try:
            return sorted(
                root.rglob("rollout-*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []

    def _sticky_rollout(self, cwd: str) -> Path | None:
        """Last rollout resolved for ``cwd``, if it still exists and matches."""
        cached = self._last_rollout.get(cwd)
        if cached and cached.exists() and self._rollout_cwd(cached) == cwd:
            return cached
        return None

    def _resolve_rollout(
        self, cwd: str, files: list[Path] | None = None
    ) -> Path | None:
        """Newest codex rollout whose session_meta.cwd matches ``cwd``.

        The live session is the most-recently-written file, so a restart or
        /clear that begins a fresh rollout is picked up automatically next
        tick. ``files`` lets iter_transcripts share ONE directory scan across
        all codex windows. When the capped scan misses (busy hosts push a
        quiet window's rollout past the prefix), the sticky last-resolved path
        keeps the topic tracking instead of silently going dark.
        """
        if files is None:
            files = self._rollout_files_newest_first()
        for f in files[:_CODEX_SCAN_CAP]:
            if self._rollout_cwd(f) == cwd:
                self._last_rollout[cwd] = f
                return f
        return self._sticky_rollout(cwd)

    async def list_sessions(self, session_manager: Any, cwd: str) -> list[AgentSession]:
        """Codex rollouts whose session_meta.cwd matches ``cwd`` (newest first).

        Scans the newest ``_CODEX_LIST_SCAN_CAP`` rollouts (cheap cached
        first-line cwd read), then fully reads only the ≤10 that match to build
        summary + count — the same AgentSession the Claude picker shows.
        """
        files = self._rollout_files_newest_first()
        sessions: list[AgentSession] = []
        for f in files[:_CODEX_LIST_SCAN_CAP]:
            if len(sessions) >= _CODEX_LIST_MAX:
                break
            if self._rollout_cwd(f) != cwd:
                continue
            match = _CODEX_ROLLOUT_SID_RE.search(f.name)
            if not match or not is_valid_session_id(match.group(1)):
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    entries = [json.loads(ln) for ln in fh if ln.strip()]
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            summary, count = CodexTranscriptParser.summarize(entries)
            if count == 0:
                continue  # header-only rollout (no real turn yet) — skip
            sessions.append(
                AgentSession(
                    session_id=match.group(1),
                    summary=summary,
                    message_count=count,
                    file_path=str(f),
                )
            )
        return sessions

    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        codex_windows = [
            (binding, ws)
            for binding, ws in list(session_manager.window_states.items())
            if getattr(ws, "runtime", CLAUDE_RUNTIME) == self.name
        ]
        if not codex_windows:
            return []
        # ONE directory scan shared by every codex window this tick — the
        # rglob+stat sort is the expensive part and is identical per window.
        files = self._rollout_files_newest_first()
        refs: list[TranscriptRef] = []
        for binding, ws in codex_windows:
            cwd = ws.cwd
            # cwd is normally set at bind time; recover it from the live tmux
            # window for codex windows created before that (or if it was lost).
            if not cwd:
                from .tmux_manager import tmux_manager

                win = await tmux_manager.find_window_by_id(binding)
                if win and win.cwd:
                    cwd = win.cwd
                    ws.cwd = cwd
                    session_manager.save_state()
                else:
                    continue
            rollout = self._resolve_rollout(cwd, files)
            if rollout is None:
                continue
            match = _CODEX_ROLLOUT_SID_RE.search(rollout.name)
            sid = match.group(1) if match else None
            if not sid:
                continue
            # Mirror the rollout's session id onto the window so routing works.
            if ws.session_id != sid:
                ws.session_id = sid
                session_manager.save_state()
            refs.append((sid, rollout))
        return refs

    async def history_transcript(
        self, session_manager: Any, window_id: str
    ) -> Path | None:
        # History reads the same rollout the monitor tracks — resolved by cwd,
        # not via the Claude projects tree (which never holds codex sessions).
        ws = session_manager.get_window_state(window_id)
        if not ws.cwd:
            return None
        return self._resolve_rollout(ws.cwd)


CLAUDE = ClaudeRuntime()
CODEX = CodexRuntime()

RUNTIMES: dict[str, AgentRuntime] = {CLAUDE.name: CLAUDE, CODEX.name: CODEX}


def pickable_runtimes() -> list[AgentRuntime]:
    """Runtimes offered as tabs in the session picker, in display order.

    Registered runtimes whose CLI is actually installed (``is_available``) —
    a fresh install without codex must never see a Codex tab whose "new
    session" types a missing command into the pane (dead window, no
    explanation). Registry insertion order (Claude, Codex, …); a new runtime
    added to ``RUNTIMES`` becomes a picker tab automatically. Falls back to
    Claude if nothing probes available (main.py already warns at boot), so the
    picker is never empty.
    """
    available = [rt for rt in RUNTIMES.values() if rt.is_available()]
    return available or [CLAUDE]


def default_runtime() -> AgentRuntime:
    """The runtime new windows default to (``CCBOT_DEFAULT_RUNTIME``).

    Resolves the configured name through the registry (unknown → Claude) and
    falls back to Claude when the configured runtime's CLI isn't installed —
    the knob must never produce windows that can't launch.
    """
    rt = get_runtime(config.default_runtime)
    return rt if rt.is_available() else CLAUDE


def monitored_runtimes() -> list[AgentRuntime]:
    """Runtimes the monitor iterates each tick.

    All registered runtimes: each ``iter_transcripts`` is cheap when that
    runtime has no bound windows (Claude's scan is already done regardless;
    Codex returns early when no codex windows exist / no sessions dir).
    """
    return list(RUNTIMES.values())


def is_valid_runtime(name: str | None) -> bool:
    """True if ``name`` is a known runtime identifier."""
    return bool(name) and name in RUNTIMES


def get_runtime(name: str | None) -> AgentRuntime:
    """Resolve a runtime name to its ``AgentRuntime``.

    Unknown/empty names fall back to Claude — legacy ``WindowState`` rows carry
    no runtime, and an unrecognised value must degrade to the default rather
    than crash a launch/monitor path.
    """
    if name and name in RUNTIMES:
        return RUNTIMES[name]
    return CLAUDE
