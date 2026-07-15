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

Everything downstream of ``parse_entries`` (NewMessage → queue → voice / tables
/ pins) is runtime-agnostic. Adding a third model = one ``AgentRuntime``
subclass implementing these; no monitor rewrite.

Key API: ``get_runtime(name)`` → AgentRuntime; module singletons ``CLAUDE`` /
``CODEX``; ``RUNTIMES`` registry; ``monitored_runtimes()``; ``is_valid_runtime``.
"""

from __future__ import annotations

import abc
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any

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

    def panel_slash(self, action: str) -> str | None:
        """Slash command this runtime types for a panel action (None if none)."""
        return self.panel_slash_commands.get(action)

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

    def __init__(self) -> None:
        # rollout path -> its session_meta.cwd (immutable per file); avoids
        # re-reading a rollout header every tick. Instance state on the CODEX
        # singleton.
        self._meta_cwd: dict[str, str | None] = {}

    def launch_command(
        self, window_name: str, resume_session_id: str | None = None
    ) -> str:
        codex = config.codex_command
        if resume_session_id and is_valid_session_id(resume_session_id):
            return f"{codex} resume {resume_session_id}"
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
        return codex

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

    def _resolve_rollout(self, cwd: str) -> Path | None:
        """Newest codex rollout whose session_meta.cwd matches ``cwd``.

        The live session is the most-recently-written file, so a restart or
        /clear that begins a fresh rollout is picked up automatically next tick.
        """
        root = config.codex_sessions_path
        if not root.exists():
            return None
        try:
            files = sorted(
                root.rglob("rollout-*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        for f in files[:60]:
            if self._rollout_cwd(f) == cwd:
                return f
        return None

    async def iter_transcripts(
        self, session_manager: Any, monitor: Any, active_session_ids: set[str]
    ) -> list[TranscriptRef]:
        refs: list[TranscriptRef] = []
        for binding, ws in list(session_manager.window_states.items()):
            if getattr(ws, "runtime", CLAUDE_RUNTIME) != self.name:
                continue
            cwd = ws.cwd
            # cwd is normally set at bind time; recover it from the live tmux
            # window for codex windows created before that (or if it was lost).
            if not cwd:
                from .tmux_manager import tmux_manager

                win = await tmux_manager.find_window_by_id(binding)
                if win and win.cwd:
                    cwd = win.cwd
                    ws.cwd = cwd
                    session_manager._save_state()
                else:
                    continue
            rollout = self._resolve_rollout(cwd)
            if rollout is None:
                continue
            match = _CODEX_ROLLOUT_SID_RE.search(rollout.name)
            sid = match.group(1) if match else None
            if not sid:
                continue
            # Mirror the rollout's session id onto the window so routing works.
            if ws.session_id != sid:
                ws.session_id = sid
                session_manager._save_state()
            refs.append((sid, rollout))
        return refs


CLAUDE = ClaudeRuntime()
CODEX = CodexRuntime()

RUNTIMES: dict[str, AgentRuntime] = {CLAUDE.name: CLAUDE, CODEX.name: CODEX}


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
