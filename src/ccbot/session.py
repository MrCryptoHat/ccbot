"""Claude Code session management — the core state hub.

Manages the key mappings:
  Window→Session (window_states): which Claude session_id a window holds (keyed by window_id).
  User→Thread→Window (thread_bindings): topic-to-window bindings (1 topic = 1 window_id).

Responsibilities:
  - Persist/load state to ~/.ccbot/state.json.
  - Sync window↔session bindings from session_map.json (written by hook).
  - Resolve window IDs to ClaudeSession objects (JSONL file reading).
  - Track per-user read offsets for unread-message detection.
  - Manage thread↔window bindings for Telegram topic routing.
  - Send keystrokes to tmux windows and retrieve message history.
  - Maintain window_id→display name mapping for UI display.
  - Re-resolve stale window IDs on startup (tmux server restart recovery).

Key class: SessionManager (singleton instantiated as `session_manager`).
Key methods for thread binding access:
  - resolve_window_for_thread: Get window_id for a user's thread
  - iter_thread_bindings: Generator for iterating all (user_id, thread_id, window_id)
  - find_users_for_session: Find all users bound to a session_id
"""

import asyncio
import fcntl
import json
import os
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any, Literal, TypeAlias

import aiofiles

from . import i18n
from .agent_session import AgentSession
from .config import config
from .docker_driver import docker_driver
from .runtimes import get_runtime
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import (
    atomic_write_json,
    is_valid_session_id,
    schedule_async_json_write,
)
from .voice.safety import BudgetEvent, VoiceBudget
from .worktrees import WorktreeMeta

logger = logging.getLogger(__name__)


@dataclass
class WindowState:
    """Persistent state for a tmux window.

    Attributes:
        session_id: Associated agent session ID (empty if not yet detected)
        cwd: Working directory for direct file path construction
        window_name: Display name of the window
        runtime: Agent runtime this window runs — "claude" (default) or
            "codex". Orthogonal to transport; drives launch/resume, transcript
            parsing and terminal heuristics (see runtimes.py). Legacy state has
            no value → defaults to "claude", so nothing migrates.
    """

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    runtime: str = "claude"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        if self.window_name:
            d["window_name"] = self.window_name
        # Persist runtime only when non-default — keeps existing state.json
        # rows byte-identical (zero migration) and the file uncluttered.
        if self.runtime and self.runtime != "claude":
            d["runtime"] = self.runtime
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            runtime=data.get("runtime", "claude") or "claude",
        )


# Backward-compatible alias: the descriptor is runtime-agnostic (also produced
# by CodexRuntime.list_sessions), so it lives in a neutral module. Old imports
# `from ..session import ClaudeSession` keep working.
ClaudeSession: TypeAlias = AgentSession


@dataclass
class SessionManager:
    """Manages session state for Claude Code.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> WindowState (session_id, cwd, window_name)
    user_window_offsets: user_id -> {window_id -> byte_offset}
    thread_bindings: user_id -> {thread_id -> window_id}
    window_display_names: window_id -> window_name (for display)
    group_chat_ids: "user_id:thread_id" -> group chat_id (for supergroup routing)
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # "user_id:thread_id" -> group chat_id (for supergroup forum topic routing)
    # IMPORTANT: This mapping is essential for supergroup/forum topic support.
    # Telegram Bot API requires group chat_id (negative number like -100xxx)
    # as the chat_id parameter when sending messages to forum topics.
    # Using user_id as chat_id will fail with "Message thread not found".
    # See: https://core.telegram.org/bots/api#sendmessage
    # History: originally added in 5afc111, erroneously removed in 26cb81f,
    # restored in PR #23.
    group_chat_ids: dict[str, int] = field(default_factory=dict)
    # Per-topic voice mode: "user_id:thread_id" keys
    voice_mode_topics: set[str] = field(default_factory=set)
    # Per-topic diff mode (/diff): "user_id:thread_id" keys. When on, the
    # bot renders the agent's edit-tool diffs (Edit/MultiEdit/Write) as a
    # red/green screenshot, one image per agent turn. Off by default — the
    # default chat stays free of tool plumbing. See handlers/diff_view.py.
    diff_mode_topics: set[str] = field(default_factory=set)
    # Task-pin mode (/pin): a user message that looks like a NEW TASK
    # (≥ config.pin_tasks_min_chars chars, sent to an idle agent) is pinned
    # in its topic, so the pinned list reads as the topic's task history.
    # ON by default everywhere (config.pin_tasks_default); this dict holds
    # only per-topic /pin overrides, keyed "user_id:thread_id" → bool.
    # See handlers/task_pin.py.
    pin_topic_overrides: dict[str, bool] = field(default_factory=dict)
    # Topics that already received the persistent menu ReplyKeyboard
    # ("user_id:thread_id" keys). Telegram scopes reply keyboards per forum
    # topic, so each topic needs one message carrying the markup; this set
    # makes that exactly-once — bind confirmations and the first delivered
    # reply (message_queue backstop) attach the keyboard only when the
    # topic isn't marked yet. /menu re-attaches unconditionally.
    menu_shown_topics: set[str] = field(default_factory=set)
    # Global toggle (/react): bot puts a 👀 reaction on a user message the
    # moment the agent takes it into context. Default from CCBOT_REACTION_ACK
    # (on); /react overrides at runtime (persisted in state.json).
    reaction_ack_enabled: bool = field(
        default_factory=lambda: config.reaction_ack_default
    )
    # Global UI language (/lang): "ru" or "en". Single-user bot → one global
    # setting, not per-topic. Synced into i18n._current_lang on load and on
    # set_ui_language so call sites just call i18n.tr(). Defaults from
    # config.default_lang (CCBOT_DEFAULT_LANG).
    ui_language: str = "ru"
    # Sessions (session_id) that have already received the voice-mode ON
    # directive in their context. Persisted so that after a bot restart
    # we still know a given Claude session has the voice-style tags in
    # its context — otherwise toggling voice OFF post-restart wouldn't
    # send Claude the OFF directive and Claude would keep emitting
    # [warmly] / [pause] tags from its stale context.
    # A new session_id (e.g. after /clear) naturally won't be in the set,
    # so the directive is re-sent automatically.
    voice_announced_sessions: set[str] = field(default_factory=set)
    # Daily TTS character budget — global, persisted, defensive backstop
    # against any future replay/retry leak that bypasses Layer 1+2. When
    # exhausted, voice auto-disables in every topic until the local midnight
    # rollover. See voice/safety.py for the rationale.
    voice_budget: VoiceBudget = field(default_factory=VoiceBudget)
    # In-memory generation state per binding (binding → monotonic timestamp of
    # last "active" signal). Drives the Telegram typing indicator: True means
    # Claude is mid-turn for that binding. Set on user input (send_to_window),
    # refreshed on each inbound JSONL event, cleared on final assistant text
    # or interactive-UI detection. Not persisted — a bot restart just delays
    # typing until the next event.
    _generating: dict[str, float] = field(default_factory=dict)
    # Per-binding asyncio locks serializing multi-step pane writes (chunked
    # typing + pacing delays + Enter). Concurrent writers — parallel update
    # handlers, media, reaction confirm, restart
    # sequences — would otherwise interleave inside one prompt. Runtime-only.
    _send_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Live-dashboard message_ids per docker-agent (agent_name → telegram
    # message_id in the shared LIVE_DASHBOARD_THREAD_ID topic). Persisted
    # so a bot restart edits the existing message instead of leaving a
    # duplicate. Owned entirely by ccbot — the daemon's browser-live.json
    # contract is unchanged.
    live_dashboard_message_ids: dict[str, int] = field(default_factory=dict)
    # Learned topic→directory memory: user_id -> {thread_id -> directory}.
    # A topic's thread_id is permanent (the root message id), independent of
    # the topic's display name. Whenever a topic is bound to a tmux directory
    # we record it here, so a later message in the same topic — after its
    # window died, tmux restarted, or the topic was renamed to something that
    # no longer name-matches a folder — auto-rebinds to the same directory
    # instead of dropping back to the directory browser. This is the reliable,
    # name-independent half of auto-bind; the name match is just a convenience
    # for never-before-bound topics. Docker bindings are not recorded (their
    # lifecycle is the container's, not a tmux window's).
    thread_directory_memory: dict[int, dict[int, str]] = field(default_factory=dict)
    # Companion to thread_directory_memory: user_id -> {thread_id -> runtime}.
    # Remembers which agent CLI the topic ran, so the memory rebind relaunches
    # the SAME runtime (a codex topic must not silently come back as Claude
    # after its window dies). Absent entry = "claude" (legacy rows predate the
    # runtime axis). Kept as a parallel map so old state.json files load
    # unchanged.
    thread_runtime_memory: dict[int, dict[int, str]] = field(default_factory=dict)
    # Last seen CLI version per runtime name (the self-update canary's
    # persistence): runtime → `--version` first line. Compared periodically by
    # status_polling's version watcher; a change means every TUI anchor is now
    # unverified against the new build, so it's surfaced instead of drifting.
    runtime_versions: dict[str, str] = field(default_factory=dict)
    # Worktree-agent metadata: user_id -> {thread_id -> WorktreeMeta}. Keyed by
    # the permanent thread_id (same as every other per-topic structure), so the
    # teardown guard — which receives (user, thread) — looks it up directly,
    # with no dependence on the unstable @id or a derived cwd. Holds only what
    # git can't recover (human task name, base branch, worktree path). A normal
    # tmux topic has no entry here; deleting the whole map degrades worktree
    # topics to plain tmux topics without breaking routing.
    worktree_meta: dict[int, dict[int, WorktreeMeta]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Seed the UI language from config before state loads, so a fresh
        # install (no state.json) still honors CCBOT_DEFAULT_LANG. _load_state
        # overrides from the persisted value when present.
        self.ui_language = config.default_lang
        i18n.set_language(self.ui_language)
        self._load_state()

    # --- Generation state (drives Telegram typing indicator) ---

    def mark_generating(self, binding: str) -> None:
        """Mark/refresh a binding as actively generating."""
        self._generating[binding] = time.monotonic()

    def mark_idle(self, binding: str) -> None:
        """Mark a binding as idle (final text, interactive UI, session end)."""
        self._generating.pop(binding, None)

    # --- Live-dashboard message_id tracking (per docker agent) ---

    def get_dashboard_message_id(self, agent_name: str) -> int | None:
        """Return the persisted live-dashboard message_id for an agent, or None."""
        return self.live_dashboard_message_ids.get(agent_name)

    def set_dashboard_message_id(self, agent_name: str, message_id: int) -> None:
        """Record the live-dashboard message_id for an agent. Persisted."""
        if self.live_dashboard_message_ids.get(agent_name) == message_id:
            return
        self.live_dashboard_message_ids[agent_name] = message_id
        self._save_state()

    def clear_dashboard_message_id(self, agent_name: str) -> None:
        """Drop the persisted live-dashboard message_id for an agent (e.g.
        after Telegram returned 'message to edit not found'). Persisted."""
        if self.live_dashboard_message_ids.pop(agent_name, None) is not None:
            self._save_state()

    def is_generating(self, binding: str, *, stale_after: float = 60.0) -> bool:
        """Return True if the binding is actively generating.

        Auto-clears the flag if no refresh arrived within ``stale_after``
        seconds — safety net for missed end-of-turn events so a stuck True
        doesn't keep typing alive forever.
        """
        ts = self._generating.get(binding)
        if ts is None:
            return False
        if time.monotonic() - ts > stale_after:
            self._generating.pop(binding, None)
            return False
        return True

    def _save_state(self) -> None:
        """Persist in-memory state to disk without blocking the event loop.

        The snapshot itself is built synchronously (just dict comprehensions
        over in-memory state — cheap and coherent relative to the caller's
        view). The actual disk write + fsync is handed to a background
        single-thread writer via ``schedule_async_json_write``, so callers
        like ``set_status_msg`` or ``update_user_window_offset`` that fire
        many times per second do not stall asyncio with fsync latency.

        Ordering is preserved by the writer's single-thread executor: a
        snapshot submitted later always lands on disk after an earlier one.
        Drain on shutdown via ``shutdown_async_writer``.
        """
        state: dict[str, Any] = {
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "thread_bindings": {
                str(uid): {str(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in self.thread_bindings.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
            "voice_mode_topics": sorted(self.voice_mode_topics),
            "diff_mode_topics": sorted(self.diff_mode_topics),
            "pin_topic_overrides": dict(sorted(self.pin_topic_overrides.items())),
            "menu_shown_topics": sorted(self.menu_shown_topics),
            "reaction_ack_enabled": self.reaction_ack_enabled,
            "ui_language": self.ui_language,
            "voice_announced_sessions": sorted(self.voice_announced_sessions),
            "voice_budget": self.voice_budget.to_dict(),
            "live_dashboard_message_ids": dict(self.live_dashboard_message_ids),
            "thread_directory_memory": {
                str(uid): {str(tid): d for tid, d in dirs.items()}
                for uid, dirs in self.thread_directory_memory.items()
            },
            "thread_runtime_memory": {
                str(uid): {str(tid): r for tid, r in rts.items()}
                for uid, rts in self.thread_runtime_memory.items()
            },
            "runtime_versions": dict(self.runtime_versions),
            "worktree_meta": {
                str(uid): {str(tid): m.to_dict() for tid, m in metas.items()}
                for uid, metas in self.worktree_meta.items()
            },
        }
        schedule_async_json_write(config.state_file, state)
        logger.debug("State save scheduled for %s", config.state_file)

    def _is_window_id(self, key: str) -> bool:
        """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    @staticmethod
    def _is_docker_binding(value: str) -> bool:
        """Check if a binding value points to a docker-agent (prefix form)."""
        return value.startswith("docker:") and len(value) > len("docker:")

    def resolve_binding(
        self, user_id: int, thread_id: int | None
    ) -> tuple[Literal["tmux", "docker"], str] | None:
        """Resolve a thread binding to its transport type and target.

        Returns:
          ("tmux", "@12")       — bound to tmux window @12
          ("docker", "assistant") — bound to docker agent "assistant"
          None                  — no binding for this thread

        Callers can branch on the type to pick the right transport
        (tmux send-keys vs docker exec) without inspecting string shapes
        themselves. Existing `@<id>` binding values continue to resolve
        as ("tmux", ...) unchanged — adding this method does not alter
        stored state or behavior for legacy bindings.
        """
        if thread_id is None:
            return None
        value = self.get_window_for_thread(user_id, thread_id)
        if not value:
            return None
        if self._is_docker_binding(value):
            return "docker", value[len("docker:") :]
        return "tmux", value

    async def resolve_agent_binding(self, name: str) -> str | None:
        """Resolve a bare agent name to a binding value for name-addressed sends.

        Used by the ``/inject`` endpoint, which addresses agents by
        name rather than by topic/thread. Resolution order:

          - a configured docker agent → ``docker:<name>`` (always
            resolvable; the container's liveness is the send path's
            concern, surfaced later as an availability error);
          - else a live host tmux window named ``<name>`` → its ``@<id>``;
          - neither → ``None``.

        The asymmetry is deliberate: a docker agent is a permanent fixture
        (so "down" is an availability failure at send time), whereas a host
        tmux agent exists only while running — no window means the agent
        simply isn't up, which the caller reports distinctly from "not in
        allowlist". Routing onwards is transport-agnostic via
        ``send_to_window``; this method only picks the binding shape.
        """
        if config.get_docker_agent(name) is not None:
            return f"docker:{name}"
        window = await tmux_manager.find_window_by_name(name)
        if window is not None:
            return window.window_id
        return None

    def _load_state(self) -> None:
        """Load state synchronously during initialization.

        Detects old-format state (window_name keys without '@' prefix) and
        marks for migration on next startup re-resolution.
        """
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                self.window_states = {
                    k: WindowState.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
                self.user_window_offsets = {
                    int(uid): offsets
                    for uid, offsets in state.get("user_window_offsets", {}).items()
                }
                self.thread_bindings = {
                    int(uid): {int(tid): wid for tid, wid in bindings.items()}
                    for uid, bindings in state.get("thread_bindings", {}).items()
                }
                self.window_display_names = state.get("window_display_names", {})
                self.thread_directory_memory = {
                    int(uid): {int(tid): d for tid, d in dirs.items()}
                    for uid, dirs in state.get("thread_directory_memory", {}).items()
                }
                self.thread_runtime_memory = {
                    int(uid): {int(tid): r for tid, r in rts.items()}
                    for uid, rts in state.get("thread_runtime_memory", {}).items()
                }
                self.runtime_versions = {
                    str(k): str(v) for k, v in state.get("runtime_versions", {}).items()
                }
                self.group_chat_ids = {
                    k: int(v) for k, v in state.get("group_chat_ids", {}).items()
                }
                self.voice_mode_topics = set(state.get("voice_mode_topics", []))
                self.diff_mode_topics = set(state.get("diff_mode_topics", []))
                self.pin_topic_overrides = {
                    str(k): bool(v)
                    for k, v in state.get("pin_topic_overrides", {}).items()
                }
                # Legacy shape (pre default-on): a list of opted-in topics.
                for key in state.get("pin_mode_topics", []):
                    self.pin_topic_overrides.setdefault(str(key), True)
                self.menu_shown_topics = set(state.get("menu_shown_topics", []))
                self.reaction_ack_enabled = bool(
                    state.get("reaction_ack_enabled", config.reaction_ack_default)
                )
                self.ui_language = state.get("ui_language") or config.default_lang
                i18n.set_language(self.ui_language)
                self.voice_announced_sessions = set(
                    state.get("voice_announced_sessions", [])
                )
                self.voice_budget = VoiceBudget.from_dict(state.get("voice_budget"))
                self.live_dashboard_message_ids = {
                    str(k): int(v)
                    for k, v in state.get("live_dashboard_message_ids", {}).items()
                }
                self.worktree_meta = {
                    int(uid): {
                        int(tid): WorktreeMeta.from_dict(m) for tid, m in metas.items()
                    }
                    for uid, metas in state.get("worktree_meta", {}).items()
                }

                # Detect old format: keys that don't look like window IDs.
                # docker:<agent> bindings are a valid non-window-id form and
                # must not be mistaken for an old window-name key.
                needs_migration = False
                for k in self.window_states:
                    if not self._is_window_id(k) and not self._is_docker_binding(k):
                        needs_migration = True
                        break
                if not needs_migration:
                    for bindings in self.thread_bindings.values():
                        for wid in bindings.values():
                            if not self._is_window_id(
                                wid
                            ) and not self._is_docker_binding(wid):
                                needs_migration = True
                                break
                        if needs_migration:
                            break

                if needs_migration:
                    logger.info(
                        "Detected old-format state (window_name keys), "
                        "will re-resolve on startup"
                    )
                    pass

            # TypeError/AttributeError/KeyError cover STRUCTURALLY wrong
            # state (valid JSON, wrong shapes — e.g. a hand-edited file where
            # a dict became a list): those used to escape this handler and
            # crash the bot at boot with a bare traceback.
            except (
                json.JSONDecodeError,
                ValueError,
                TypeError,
                AttributeError,
                KeyError,
            ) as e:
                logger.warning("Failed to load state: %s", e)
                self.window_states = {}
                self.user_window_offsets = {}
                self.thread_bindings = {}
                self.window_display_names = {}
                self.thread_directory_memory = {}
                self.thread_runtime_memory = {}
                self.runtime_versions = {}
                self.group_chat_ids = {}
                self.voice_mode_topics = set()
                self.diff_mode_topics = set()
                self.pin_topic_overrides = {}
                self.menu_shown_topics = set()
                self.reaction_ack_enabled = config.reaction_ack_default
                self.ui_language = config.default_lang
                i18n.set_language(self.ui_language)
                self.voice_announced_sessions = set()
                self.voice_budget = VoiceBudget()
                self.live_dashboard_message_ids = {}
                self.worktree_meta = {}

    async def resolve_stale_ids(self) -> None:
        """Re-resolve persisted window IDs against live tmux windows.

        Called on startup. Handles two cases:
        1. Old-format migration: window_name keys → window_id keys
        2. Stale IDs: window_id no longer exists but display name matches a live window

        Builds {window_name: window_id} from live windows, then remaps or drops entries.
        """
        windows = await tmux_manager.list_windows()
        live_by_name: dict[str, str] = {}  # window_name -> window_id
        live_ids: set[str] = set()
        for w in windows:
            live_by_name[w.window_name] = w.window_id
            live_ids.add(w.window_id)

        changed = False

        # --- Migrate window_states ---
        new_window_states: dict[str, WindowState] = {}
        for key, ws in self.window_states.items():
            if self._is_docker_binding(key):
                # Docker-agent state lives outside tmux; keep verbatim.
                new_window_states[key] = ws
                continue
            if self._is_window_id(key):
                if key in live_ids:
                    new_window_states[key] = ws
                else:
                    # Stale ID — try re-resolve by display name
                    display = self.window_display_names.get(key, ws.window_name or key)
                    new_id = live_by_name.get(display)
                    if new_id:
                        logger.info(
                            "Re-resolved stale window_id %s -> %s (name=%s)",
                            key,
                            new_id,
                            display,
                        )
                        new_window_states[new_id] = ws
                        ws.window_name = display
                        self.window_display_names[new_id] = display
                        self.window_display_names.pop(key, None)
                        changed = True
                    else:
                        logger.info(
                            "Dropping stale window_state: %s (name=%s)", key, display
                        )
                        changed = True
            else:
                # Old format: key is window_name
                new_id = live_by_name.get(key)
                if new_id:
                    logger.info("Migrating window_state key %s -> %s", key, new_id)
                    ws.window_name = key
                    new_window_states[new_id] = ws
                    self.window_display_names[new_id] = key
                    changed = True
                else:
                    logger.info(
                        "Dropping old-format window_state: %s (no live window)", key
                    )
                    changed = True
        self.window_states = new_window_states

        # --- Migrate thread_bindings ---
        for uid, bindings in self.thread_bindings.items():
            new_bindings: dict[int, str] = {}
            for tid, val in bindings.items():
                if self._is_docker_binding(val):
                    # Docker bindings have no tmux window to re-resolve.
                    new_bindings[tid] = val
                    continue
                if self._is_window_id(val):
                    if val in live_ids:
                        new_bindings[tid] = val
                    else:
                        display = self.window_display_names.get(val, val)
                        new_id = live_by_name.get(display)
                        if new_id:
                            logger.info(
                                "Re-resolved thread binding %s -> %s (name=%s)",
                                val,
                                new_id,
                                display,
                            )
                            new_bindings[tid] = new_id
                            self.window_display_names[new_id] = display
                            changed = True
                        else:
                            logger.info(
                                "Dropping stale thread binding: user=%d, thread=%d, wid=%s",
                                uid,
                                tid,
                                val,
                            )
                            changed = True
                else:
                    # Old format: val is window_name
                    new_id = live_by_name.get(val)
                    if new_id:
                        logger.info("Migrating thread binding %s -> %s", val, new_id)
                        new_bindings[tid] = new_id
                        self.window_display_names[new_id] = val
                        changed = True
                    else:
                        logger.info(
                            "Dropping old-format thread binding: user=%d, thread=%d, name=%s",
                            uid,
                            tid,
                            val,
                        )
                        changed = True
            self.thread_bindings[uid] = new_bindings

        # Remove empty user entries
        empty_users = [uid for uid, b in self.thread_bindings.items() if not b]
        for uid in empty_users:
            del self.thread_bindings[uid]

        # --- Migrate user_window_offsets ---
        for uid, offsets in self.user_window_offsets.items():
            new_offsets: dict[str, int] = {}
            for key, offset in offsets.items():
                if self._is_docker_binding(key):
                    new_offsets[key] = offset
                    continue
                if self._is_window_id(key):
                    if key in live_ids:
                        new_offsets[key] = offset
                    else:
                        display = self.window_display_names.get(key, key)
                        new_id = live_by_name.get(display)
                        if new_id:
                            new_offsets[new_id] = offset
                            changed = True
                        else:
                            changed = True
                else:
                    new_id = live_by_name.get(key)
                    if new_id:
                        new_offsets[new_id] = offset
                        changed = True
                    else:
                        changed = True
            self.user_window_offsets[uid] = new_offsets

        if changed:
            self._save_state()
            logger.info("Startup re-resolution complete")

        # Clean up session_map.json: stale window IDs and old-format keys
        await self._cleanup_stale_session_map_entries(live_ids)
        await self._cleanup_old_format_session_map_keys()

    @staticmethod
    def _locked_session_map_rmw(mutate: Callable[[dict], bool]) -> None:
        """Read-modify-write session_map.json under the hook's flock.

        The SessionStart hook does its own flock'd RMW on this file; the
        bot's cleanup passes used to read/write it lock-free, so a hook
        firing between the bot's read and write had its fresh entry
        silently erased — that agent's replies never delivered until the
        next SessionStart. Sync by design (flock is not awaitable); called
        via asyncio.to_thread from the async cleanups. ``mutate`` returns
        True when the map changed and must be written back.
        """
        map_file = config.session_map_file
        if not map_file.exists():
            return
        lock_path = map_file.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        return
                    if not isinstance(session_map, dict):
                        return
                    if mutate(session_map):
                        atomic_write_json(map_file, session_map)
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except OSError as e:
            logger.warning("session_map cleanup skipped (lock/IO): %s", e)

    async def _cleanup_old_format_session_map_keys(self) -> None:
        """Remove old-format keys (window_name instead of @window_id) from session_map.json."""

        def _mutate(session_map: dict) -> bool:
            prefix = f"{config.tmux_session_name}:"
            old_keys = [
                key
                for key in session_map
                if key.startswith(prefix) and not self._is_window_id(key[len(prefix) :])
            ]
            for key in old_keys:
                del session_map[key]
            if old_keys:
                logger.info(
                    "Cleaned up %d old-format session_map keys: %s",
                    len(old_keys),
                    old_keys,
                )
            return bool(old_keys)

        await asyncio.to_thread(self._locked_session_map_rmw, _mutate)

    async def _cleanup_stale_session_map_entries(self, live_ids: set[str]) -> None:
        """Remove entries for tmux windows that no longer exist.

        When windows are closed externally (outside ccbot), session_map.json
        retains orphan references. This cleanup removes entries whose window_id
        is not in the current set of live tmux windows.
        """

        def _mutate(session_map: dict) -> bool:
            prefix = f"{config.tmux_session_name}:"
            stale_keys = [
                key
                for key in session_map
                if key.startswith(prefix)
                and self._is_window_id(key[len(prefix) :])
                and key[len(prefix) :] not in live_ids
            ]
            for key in stale_keys:
                del session_map[key]
                logger.info("Removed stale session_map entry: %s", key)
            if stale_keys:
                logger.info(
                    "Cleaned up %d stale session_map entries "
                    "(windows no longer in tmux)",
                    len(stale_keys),
                )
            return bool(stale_keys)

        await asyncio.to_thread(self._locked_session_map_rmw, _mutate)

    # --- Display name management ---

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def update_display_name(self, window_id: str, new_name: str) -> None:
        """Update the display name for a window and persist state."""
        self.window_display_names[window_id] = new_name
        # Also update WindowState.window_name if it exists
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self._save_state()
        logger.info("Updated display name: window_id %s -> '%s'", window_id, new_name)

    # --- Group chat ID management (supergroup forum topic routing) ---

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        """Store the group chat_id for a user+thread combination.

        In supergroups with forum topics, messages must be sent to the group's
        chat_id (negative number like -100xxx) rather than the user's personal ID.
        Telegram's Bot API rejects message_thread_id when chat_id is a private
        user ID — the thread only exists within the group context.

        DO NOT REMOVE this method or the group_chat_ids mapping.
        Without it, all outbound messages in forum topics fail with
        "Message thread not found". See commit history: 5afc111 → 26cb81f → PR #23.
        """
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            self._save_state()
            logger.debug(
                "Stored group chat_id: user=%d, thread=%s, chat_id=%d",
                user_id,
                thread_id,
                chat_id,
            )

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        """Resolve the correct chat_id for sending messages.

        Returns the stored group chat_id when a thread_id is present and a
        mapping exists, otherwise falls back to user_id (for private chats).

        Every outbound Telegram API call (send_message, edit_message_text,
        delete_message, send_chat_action, edit_forum_topic, etc.) MUST use
        this method instead of raw user_id. Using user_id directly breaks
        supergroup forum topic routing.
        """
        if thread_id is not None:
            key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(key)
            if group_id is not None:
                return group_id
        return user_id

    def is_voice_mode(self, user_id: int, thread_id: int | None) -> bool:
        """Check if voice mode is enabled for a topic."""
        if thread_id is None:
            return False
        return f"{user_id}:{thread_id}" in self.voice_mode_topics

    def toggle_voice_mode(self, user_id: int, thread_id: int) -> bool:
        """Toggle voice mode for a topic. Returns new state (True=on)."""
        key = f"{user_id}:{thread_id}"
        if key in self.voice_mode_topics:
            self.voice_mode_topics.discard(key)
            self._save_state()
            return False
        self.voice_mode_topics.add(key)
        self._save_state()
        return True

    def is_diff_mode(self, user_id: int, thread_id: int | None) -> bool:
        """Check if diff-screenshot mode (/diff) is enabled for a topic."""
        if thread_id is None:
            return False
        return f"{user_id}:{thread_id}" in self.diff_mode_topics

    def toggle_diff_mode(self, user_id: int, thread_id: int) -> bool:
        """Toggle diff-screenshot mode for a topic. Returns new state (True=on)."""
        key = f"{user_id}:{thread_id}"
        if key in self.diff_mode_topics:
            self.diff_mode_topics.discard(key)
            self._save_state()
            return False
        self.diff_mode_topics.add(key)
        self._save_state()
        return True

    def is_pin_mode(self, user_id: int, thread_id: int | None) -> bool:
        """Task-pin mode for a topic: /pin override, else the global default."""
        if thread_id is None:
            return False
        return self.pin_topic_overrides.get(
            f"{user_id}:{thread_id}", config.pin_tasks_default
        )

    def toggle_pin_mode(self, user_id: int, thread_id: int) -> bool:
        """Toggle task-pin mode for a topic. Returns new state (True=on)."""
        new_state = not self.is_pin_mode(user_id, thread_id)
        self.pin_topic_overrides[f"{user_id}:{thread_id}"] = new_state
        self._save_state()
        return new_state

    def is_menu_shown(self, user_id: int, thread_id: int | None) -> bool:
        """Has this topic already received the persistent menu keyboard?"""
        if thread_id is None:
            return False
        return f"{user_id}:{thread_id}" in self.menu_shown_topics

    def mark_menu_shown(self, user_id: int, thread_id: int) -> None:
        """Record that the menu keyboard was delivered to this topic."""
        key = f"{user_id}:{thread_id}"
        if key not in self.menu_shown_topics:
            self.menu_shown_topics.add(key)
            self._save_state()

    def clear_menu_shown(self, user_id: int, thread_id: int) -> None:
        """Forget the menu-keyboard flag (topic closed/deleted)."""
        key = f"{user_id}:{thread_id}"
        if key in self.menu_shown_topics:
            self.menu_shown_topics.discard(key)
            self._save_state()

    def is_reaction_ack_enabled(self) -> bool:
        """Global: does the bot mark ingested user messages with 👀? (/react)."""
        return self.reaction_ack_enabled

    def toggle_reaction_ack(self) -> bool:
        """Flip the reaction-ack toggle. Returns the new state (True=on)."""
        self.reaction_ack_enabled = not self.reaction_ack_enabled
        self._save_state()
        return self.reaction_ack_enabled

    def set_ui_language(self, lang: str) -> str:
        """Set the global UI language ("ru"/"en"); persist and sync i18n.

        Unknown codes fall back to the default. Returns the effective
        language so the caller can confirm it.
        """
        self.ui_language = lang if lang in i18n.LANGUAGES else i18n.DEFAULT_LANGUAGE
        i18n.set_language(self.ui_language)
        self._save_state()
        return self.ui_language

    def toggle_ui_language(self) -> str:
        """Flip ru↔en (single-user, two languages). Returns the new code."""
        return self.set_ui_language("en" if self.ui_language == "ru" else "ru")

    def is_session_voice_aware(self, user_id: int, thread_id: int | None) -> bool:
        """True if voice is currently enabled for the topic OR this
        session was ever told about voice mode (Claude may still emit
        TTS audio tags by inertia after toggle-off or after an explicit
        off directive has been queued).

        Used to gate defensive tag stripping in the text send path —
        stripping runs only in topics where voice is, or was, relevant.
        """
        if self.is_voice_mode(user_id, thread_id):
            return True
        if thread_id is None:
            return False
        wid = self.get_window_for_thread(user_id, thread_id)
        if not wid:
            return False
        ws = self.window_states.get(wid)
        session_id = ws.session_id if ws else ""
        return bool(session_id) and session_id in self.voice_announced_sessions

    # --- Voice budget (global daily TTS char ceiling) ---

    def voice_budget_can_spend(self, chars: int) -> bool:
        """True if recording ``chars`` would stay within today's TTS budget.

        Reads-and-rolls: triggers a date-rollover reset if needed but does
        not record the spend. Caller pairs this with ``voice_budget_record``
        after a successful synth (never before — failed synth must not bill
        the budget).
        """
        return self.voice_budget.can_spend(chars)

    def voice_budget_record(self, chars: int) -> BudgetEvent:
        """Record TTS spend; persist; return what crossed.

        State is persisted on every call (cheap — one schedule_async_json_write
        per assistant text segment, well under polling/status churn rates).
        Returns a BudgetEvent so the caller can post a one-shot 80%-warning
        or exhaustion notice.
        """
        event = self.voice_budget.record(chars)
        self._save_state()
        return event

    def voice_budget_disable_all(self) -> list[tuple[int, int]]:
        """Clear voice mode in every topic; return the disabled topics.

        Used when the daily budget is exhausted: stop further TTS calls
        immediately rather than wait for the user to /voice-off each
        topic. Caller iterates the returned list to post a per-topic
        "voice auto-disabled" notice. State persisted.
        """
        if not self.voice_mode_topics:
            return []
        disabled: list[tuple[int, int]] = []
        for key in list(self.voice_mode_topics):
            try:
                uid_str, tid_str = key.split(":", 1)
                disabled.append((int(uid_str), int(tid_str)))
            except (ValueError, IndexError):
                logger.warning("Skipping malformed voice_mode_topics key: %r", key)
        self.voice_mode_topics.clear()
        self._save_state()
        return disabled

    def consume_voice_directive(
        self, user_id: int, thread_id: int | None
    ) -> str | None:
        """Decide whether Claude needs a voice-mode state-change directive
        before the next user message, and mark the session as notified.

        Returns:
            "on"  — voice is enabled and this session hasn't been told yet
            "off" — voice is disabled but this session was previously told
                    it's on; Claude needs to know the mode changed
            None  — nothing to announce

        The session is anchored by session_id (not thread_id), so /clear
        — which produces a new session_id — automatically re-announces.
        """
        if thread_id is None:
            return None
        wid = self.get_window_for_thread(user_id, thread_id)
        if not wid:
            return None
        ws = self.window_states.get(wid)
        session_id = ws.session_id if ws else ""
        if not session_id:
            return None
        voice_on = self.is_voice_mode(user_id, thread_id)
        announced = session_id in self.voice_announced_sessions
        if voice_on and not announced:
            self.voice_announced_sessions.add(session_id)
            return "on"
        if not voice_on and announced:
            self.voice_announced_sessions.discard(session_id)
            return "off"
        return None

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Poll the relevant session_map until an entry for ``window_id`` appears.

        For tmux bindings (``@12``) polls ``~/.ccbot/session_map.json`` using
        key ``<tmux_session>:<window_id>``. For docker bindings
        (``docker:<agent>``) polls the agent's ``session_map_path`` using the
        binding value as the key (that's the format the container's hook
        writes).

        Returns True if an entry appeared within ``timeout``, else False.
        """
        logger.debug(
            "Waiting for session_map entry: window_id=%s, timeout=%.1f",
            window_id,
            timeout,
        )

        if self._is_docker_binding(window_id):
            agent = config.get_docker_agent(window_id[len("docker:") :])
            if not agent:
                return False
            map_path = agent.session_map_path
            key = window_id
        else:
            map_path = config.session_map_file
            key = f"{config.tmux_session_name}:{window_id}"

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                if map_path.exists():
                    async with aiofiles.open(map_path, "r") as f:
                        content = await f.read()
                    session_map = json.loads(content)
                    info = session_map.get(key, {})
                    if info.get("session_id"):
                        # Found — load into window_states immediately
                        logger.debug(
                            "session_map entry found for window_id %s", window_id
                        )
                        await self.load_session_map()
                        return True
            except (json.JSONDecodeError, OSError):
                pass
            await asyncio.sleep(interval)
        logger.warning(
            "Timed out waiting for session_map entry: window_id=%s", window_id
        )
        return False

    async def load_session_map(self) -> None:
        """Merge every session_map source into ``window_states``.

        Sources:
          - Main ``~/.ccbot/session_map.json`` keyed ``"<tmux_session>:<window_id>"``.
            Prefix is stripped to the tmux window id (``@12``).
          - Each active docker agent's ``session_map_path`` keyed
            ``"docker:<agent>"`` directly.

        Both kinds of keys land in ``window_states`` using the binding
        value (``@12`` or ``docker:assistant``) so downstream lookups are
        uniform. Also cleans up ``window_states`` entries that no longer
        appear in any session_map source, and refreshes display names.
        """
        valid_wids: set[str] = set()
        changed = False
        any_source_readable = False
        any_source_corrupt = False
        prefix = f"{config.tmux_session_name}:"

        def _apply(binding_key: str, info: dict[str, Any]) -> bool:
            """Update window_states for one entry; return True if state changed."""
            new_sid = info.get("session_id", "")
            new_cwd = self._normalize_cwd(info.get("cwd", ""))
            new_wname = info.get("window_name", "")
            if not new_sid:
                return False
            state = self.get_window_state(binding_key)
            mutated = False
            if state.session_id != new_sid or state.cwd != new_cwd:
                logger.info(
                    "Session map: %s updated sid=%s, cwd=%s",
                    binding_key,
                    new_sid,
                    new_cwd,
                )
                state.session_id = new_sid
                state.cwd = new_cwd
                mutated = True
            if new_wname:
                state.window_name = new_wname
                if self.window_display_names.get(binding_key) != new_wname:
                    self.window_display_names[binding_key] = new_wname
                    mutated = True
            return mutated

        # Main (tmux) session_map
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                session_map = json.loads(content)
                any_source_readable = True
            except (json.JSONDecodeError, OSError):
                # Existing but unreadable (mid-write, corrupt) — flag it so
                # the cleanup below is skipped this round.
                session_map = {}
                any_source_corrupt = True
            for key, info in session_map.items():
                if not key.startswith(prefix):
                    continue
                window_id = key[len(prefix) :]
                if not self._is_window_id(window_id):
                    continue
                valid_wids.add(window_id)
                if _apply(window_id, info):
                    changed = True

        # Per-agent (docker) session_maps
        for agent in config.active_docker_agents():
            path = agent.session_map_path
            if not path.exists():
                continue
            try:
                async with aiofiles.open(path, "r") as f:
                    content = await f.read()
                agent_map = json.loads(content)
                any_source_readable = True
            except (json.JSONDecodeError, OSError):
                any_source_corrupt = True
                continue
            for key, info in agent_map.items():
                # Only the agent's OWN binding key: the file is written
                # inside the container (untrusted), so accepting any
                # docker:* key would let a compromised agent overwrite
                # another agent's window_state.
                if key != f"docker:{agent.name}":
                    continue
                valid_wids.add(key)
                if _apply(key, info):
                    changed = True

        # Skip cleanup when no session_map source was readable (e.g. right
        # after ccbot boot but before the hook has fired) OR any existing
        # source failed to read/parse — a corrupt or mid-write file must
        # not wipe legitimate window_states; the next poll retries.
        if not any_source_readable or any_source_corrupt:
            if any_source_corrupt:
                logger.warning(
                    "session_map source unreadable — skipping window_states "
                    "cleanup this round"
                )
            return

        # Clean up window_states entries not in any session_map source.
        # Only session_map runtimes (Claude) are covered by this map; a window
        # of a hookless runtime (Codex — tracked by cwd→rollout matching, no
        # session_map at all) must not be reaped for being absent from a map
        # it never writes to — that would drop its runtime tag mid-session.
        # Its staleness is the tmux window's lifecycle (dead-window check).
        stale_wids = [
            w
            for w in self.window_states
            if w
            and w not in valid_wids
            and get_runtime(self.window_states[w].runtime).uses_session_map
        ]
        for wid in stale_wids:
            logger.info("Removing stale window_state: %s", wid)
            del self.window_states[wid]
            changed = True

        if changed:
            self._save_state()

    # --- Window state management ---

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or create window state."""
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState()
        return self.window_states[window_id]

    def window_runtime(self, binding_value: str) -> str:
        """Runtime name (``"claude"`` / ``"codex"``) of a bound window.

        Reads ``WindowState.runtime`` (default ``"claude"`` for legacy rows and
        docker bindings that never set it). The single point every transport-
        agnostic runtime dispatch keys on.
        """
        ws = self.window_states.get(binding_value)
        return getattr(ws, "runtime", None) or "claude"

    def save_state(self) -> None:
        """Public state-save entry point for collaborators outside this class
        (e.g. a runtime mutating WindowState it was handed). Same scheduled
        atomic write as the internal ``_save_state``."""
        self._save_state()

    def note_runtime_version(self, runtime: str, version: str) -> str | None:
        """Record a runtime CLI's current version; return the PREVIOUS one iff
        it changed (the self-update canary's trigger). First sighting and
        no-change both return None — only a real bump warrants the alert.
        """
        prev = self.runtime_versions.get(runtime)
        if prev == version:
            return None
        self.runtime_versions[runtime] = version
        self._save_state()
        return prev

    def tag_window_runtime(self, window_id: str, runtime: str, cwd: str) -> None:
        """Stamp a freshly created window with its runtime (and cwd if needed).

        The shared bootstrap step of every window-creation path (picker "new
        session"/resume, worktree provision, memory rebind). The runtime tag
        keeps load_session_map's stale-window sweep from reaping a non-
        session_map window; for runtimes WITHOUT a session_map hook the cwd is
        persisted too — it's how the monitor resolves their transcript
        (rollout ``session_meta.cwd`` matching).
        """
        rt = get_runtime(runtime)
        ws = self.get_window_state(window_id)
        changed = False
        if ws.runtime != rt.name:
            ws.runtime = rt.name
            changed = True
        if not rt.uses_session_map and cwd and ws.cwd != cwd:
            ws.cwd = cwd
            changed = True
        if changed:
            self._save_state()

    async def has_live_agent_on_cwd(self, runtime: str, cwd: str) -> bool:
        """True iff a LIVE tmux window of ``runtime`` already runs in ``cwd``.

        Guard for runtimes without a session_map (Codex): their transcript is
        resolved by cwd with "newest wins", so two live windows on one
        directory would cross-talk — both topics would mirror the same newest
        rollout. Creation paths refuse the second window instead. Dead
        window_states rows (window gone from tmux) don't count.
        """
        from .tmux_manager import tmux_manager

        for wid, ws in list(self.window_states.items()):
            if (getattr(ws, "runtime", None) or "claude") != runtime:
                continue
            if not ws.cwd or ws.cwd != cwd:
                continue
            if self._is_docker_binding(wid):
                continue
            if await tmux_manager.find_window_by_id(wid) is not None:
                return True
        return False

    def is_agent_working(self, binding_value: str, pane_text: str | None) -> bool:
        """True iff the bound agent is mid-turn — the runtime-aware busy check.

        Dispatches to the window's runtime (Claude's status line vs Codex's
        "(Ns • esc to interrupt)" counter), so every "don't barge" call site
        works for both without an ``if codex:`` branch. Empty pane → False.
        """
        if not pane_text:
            return False
        return get_runtime(self.window_runtime(binding_value)).is_working(pane_text)

    def agent_has_queued_input(self, binding_value: str, pane_text: str | None) -> bool:
        """True iff the bound agent shows buffered, not-yet-ingested input.

        Runtime-aware analog of ``terminal_parser.has_queued_messages`` — drives
        reaction-ack timing (fire the 👀 when this flips False).
        """
        if not pane_text:
            return False
        return get_runtime(self.window_runtime(binding_value)).has_queued_input(
            pane_text
        )

    def agent_supports(self, binding_value: str, action: str) -> bool:
        """True iff the bound agent's runtime offers the given panel button.

        The single seam the agent-panel keyboard consults to gate a button by
        capability (see runtimes.AgentRuntime.panel_actions) — the panel shows
        only what the agent can actually do, no ``if codex:`` at the builder.
        """
        return get_runtime(self.window_runtime(binding_value)).supports_panel_action(
            action
        )

    def can_offer_worktree(self, binding_value: str) -> bool:
        """True iff the 🌳 (new worktree agent) button should show for a window.

        Requires BOTH: the runtime supports worktrees AND the topic can fork a
        repo — it's already a worktree topic (base repo recorded) OR its cwd is
        a git repo. A plain non-repo folder (e.g. a data-agent dir) has nothing
        to fork, so the button is hidden rather than erroring on tap.
        """
        if not self.agent_supports(binding_value, "worktree"):
            return False
        if self.is_worktree_window(binding_value):
            return True
        from .worktrees import is_git_repo

        ws = self.window_states.get(binding_value)
        return is_git_repo(Path(ws.cwd)) if ws and ws.cwd else False

    def clear_window_session(self, window_id: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        self._save_state()
        logger.info("Cleared session for window_id %s", window_id)

    @staticmethod
    def _normalize_cwd(cwd: str) -> str:
        """Resolve symlinks so the same directory always has one canonical path.

        Without this, ~/agents/foo (symlink) and ~/mnt/remote/foo (realpath)
        would encode to two different project directories under
        ~/.claude/projects/, splitting session history.
        """
        if not cwd:
            return cwd
        try:
            return os.path.realpath(cwd)
        except (OSError, ValueError):
            return cwd

    @staticmethod
    def _encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Claude Code's project directory naming.

        Replaces all non-alphanumeric characters (except dash) with dashes.
        E.g. /home/user_name/Code/project -> -home-user-name-Code-project

        Resolves symlinks first so the encoding is stable across
        symlink/realpath variants of the same directory.
        """
        resolved = SessionManager._normalize_cwd(cwd)
        return re.sub(r"[^a-zA-Z0-9-]", "-", resolved)

    def resolve_agent_file_path(self, binding_value: str, raw_path: str) -> Path | None:
        """Resolve a ``(send file: <raw_path>)`` marker for the given binding.

        Rules:
          - Tmux binding (``@12``): pass through verbatim. Tmux agents live
            on the host, so the marker is already a host path.
          - Docker binding (``docker:<agent>``): strict whitelist — only
            container paths under ``/workspace/`` are accepted, and they
            map to ``<agent.workspace_host_path>/...`` on host. Any other
            container prefix (``/auth/``, ``/root/``, ``/etc/``, …) is
            rejected so a compromised or confused agent cannot exfiltrate
            secrets that happen to be bind-mounted in for internal use.
          - Docker binding with unknown agent: rejected.

        Returns None on rejection; the caller should log and skip.
        Returning a Path does not imply the file exists — the caller
        still does ``.is_file()``.
        """
        if self._is_docker_binding(binding_value):
            agent = config.get_docker_agent(binding_value[len("docker:") :])
            if not agent:
                return None
            prefix = "/workspace/"
            if raw_path == "/workspace":
                return agent.workspace_host_path
            if not raw_path.startswith(prefix):
                return None
            # Strip the /workspace/ prefix and defend against `..` traversal:
            # the relative component must not escape workspace_host_path.
            rel = raw_path[len(prefix) :]
            resolved = (agent.workspace_host_path / rel).resolve()
            try:
                resolved.relative_to(agent.workspace_host_path.resolve())
            except ValueError:
                return None
            return resolved
        return Path(raw_path)

    def _projects_root_for_binding(self, binding_value: str) -> Path:
        """Which ``.../projects`` dir holds JSONL for this binding.

        Tmux bindings use the host's Claude projects path. Docker
        bindings use the agent's bind-mounted claude-home (so ccbot
        reads JSONL that Claude wrote *inside* the container). Misconfigured
        docker bindings (flag off, unknown agent) silently fall back to
        the main path — reads will just miss, not blow up.
        """
        if self._is_docker_binding(binding_value):
            agent = config.get_docker_agent(binding_value[len("docker:") :])
            if agent:
                return agent.claude_home_host_path / "projects"
        return config.claude_projects_path

    def _build_session_file_path(
        self,
        session_id: str,
        cwd: str,
        projects_root: Path | None = None,
    ) -> Path | None:
        """Build the direct file path for a session from session_id and cwd.

        ``projects_root`` overrides the default ``config.claude_projects_path``
        — needed so docker-agent sessions (whose JSONL lives under the
        container's bind-mounted claude-home) resolve to the right file.
        """
        if not session_id or not cwd:
            return None
        encoded_cwd = self._encode_cwd(cwd)
        root = (
            projects_root if projects_root is not None else config.claude_projects_path
        )
        return root / encoded_cwd / f"{session_id}.jsonl"

    def _locate_session_file(
        self,
        session_id: str,
        cwd: str,
        projects_root: Path | None = None,
    ) -> Path | None:
        """Resolve a session's JSONL path (direct build + glob fallback).

        Path-only: no content read. Callers that need just the file (offset
        bumps, size checks) use this instead of the full parse in
        ``_get_session_direct``.
        """
        file_path = self._build_session_file_path(session_id, cwd, projects_root)
        if file_path and file_path.exists():
            return file_path
        glob_root = (
            projects_root if projects_root is not None else config.claude_projects_path
        )
        pattern = f"*/{session_id}.jsonl"
        matches = list(glob_root.glob(pattern))
        if matches:
            logger.debug("Found session via glob: %s", matches[0])
            return matches[0]
        return None

    def resolve_session_file_for_window(self, window_id: str) -> Path | None:
        """Path-only variant of resolve_session_for_window (no JSONL parse)."""
        state = self.get_window_state(window_id)
        if not state.session_id or not state.cwd:
            return None
        projects_root = self._projects_root_for_binding(window_id)
        return self._locate_session_file(state.session_id, state.cwd, projects_root)

    async def _get_session_direct(
        self,
        session_id: str,
        cwd: str,
        projects_root: Path | None = None,
    ) -> ClaudeSession | None:
        """Get a ClaudeSession directly from session_id and cwd (no scanning)."""
        file_path = self._locate_session_file(session_id, cwd, projects_root)
        if file_path is None:
            return None

        # Single pass: read file once, extract summary + count messages
        summary = ""
        last_user_msg = ""
        message_count = 0
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                        # Check for summary
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        # Track last user message as fallback
                        elif TranscriptParser.is_user_message(data):
                            parsed = TranscriptParser.parse_message(data)
                            if parsed and parsed.text.strip():
                                last_user_msg = parsed.text.strip()
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return ClaudeSession(
            session_id=session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    # --- Directory session listing ---

    async def list_sessions_for_directory(self, cwd: str) -> list[ClaudeSession]:
        """List existing Claude sessions for a directory.

        Encodes the cwd path to find the project directory under
        ~/.claude/projects/{encoded_cwd}/, globs *.jsonl files, and
        extracts summary info from each.

        Returns a list sorted by mtime (most recent first), capped at 10.
        """
        encoded_cwd = self._encode_cwd(cwd)
        project_dir = config.claude_projects_path / encoded_cwd
        if not project_dir.is_dir():
            return []

        # Collect JSONL files sorted by mtime (newest first)
        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        # Skip sessions-index and cap at 10
        sessions: list[ClaudeSession] = []
        for f in jsonl_files:
            if f.stem == "sessions-index":
                continue
            # A JSONL filename becomes a `--resume <id>` typed into the shell;
            # skip anything not UUID-shaped so a crafted `<meta;chars>.jsonl`
            # can never reach the resume command. (audit HIGH#1)
            if not is_valid_session_id(f.stem):
                continue
            if len(sessions) >= 10:
                break
            session_id = f.stem
            session = await self._get_session_direct(session_id, cwd)
            if session and session.message_count > 0:
                sessions.append(session)
        return sessions

    # --- Window → Session resolution ---

    async def resolve_session_for_window(self, window_id: str) -> ClaudeSession | None:
        """Resolve a binding (tmux window or docker agent) to its Claude session.

        Uses persisted session_id + cwd to construct the JSONL path directly.
        For docker bindings the lookup runs against the agent's bind-mounted
        claude-home so we read exactly what Claude wrote inside the container.
        Returns None if no session is associated.
        """
        state = self.get_window_state(window_id)

        if not state.session_id or not state.cwd:
            return None

        projects_root = self._projects_root_for_binding(window_id)
        session = await self._get_session_direct(
            state.session_id, state.cwd, projects_root
        )
        if session:
            return session

        # No JSONL yet. This is NOT necessarily stale: a freshly-launched
        # session (after «Новая»/restart) has no transcript file until its
        # first turn, yet the SessionStart hook already reported the id into
        # session_map. So this read path must stay pure — it returns None
        # (nothing to deliver yet) WITHOUT clearing session_id. Clearing here
        # used to ping-pong against load_session_map (which re-applies the id
        # every poll) and spam false "no longer exists" warnings, leaving the
        # bot unable to track the new session. Genuine stale cleanup belongs
        # to load_session_map's window_states sweep + the monitor's session
        # change detection, not to a resolver.
        logger.debug(
            "No transcript yet for window_id %s (sid=%s, cwd=%s) — "
            "fresh session or not-yet-written",
            window_id,
            state.session_id,
            state.cwd,
        )
        return None

    # --- User window offset management ---

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        """Update the user's last read offset for a window."""
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self._save_state()

    def reset_all_user_offsets_for_window(self, window_id: str) -> None:
        """Reset all users' read offsets for a window (e.g. when session changes)."""
        changed = False
        for uid, offsets in self.user_window_offsets.items():
            if window_id in offsets:
                offsets[window_id] = 0
                changed = True
                logger.info(
                    "Reset user %d offset for window %s (session changed)",
                    uid,
                    window_id,
                )
        if changed:
            self._save_state()

    # --- Thread binding management ---

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        """Bind a Telegram topic thread to a tmux window.

        Args:
            user_id: Telegram user ID
            thread_id: Telegram topic thread ID
            window_id: Tmux window ID (e.g. '@0')
            window_name: Display name for the window (optional)
        """
        if user_id not in self.thread_bindings:
            self.thread_bindings[user_id] = {}
        self.thread_bindings[user_id][thread_id] = window_id
        if window_name:
            self.window_display_names[window_id] = window_name
        self._save_state()
        display = window_name or self.get_display_name(window_id)
        logger.info(
            "Bound thread %d -> window_id %s (%s) for user %d",
            thread_id,
            window_id,
            display,
            user_id,
        )

    def record_thread_directory(
        self, user_id: int, thread_id: int, directory: str, runtime: str | None = None
    ) -> None:
        """Remember that ``thread_id`` resolved to ``directory`` (tmux dir).

        Keyed by the permanent thread_id, so a future message in the same
        topic can auto-rebind to the same folder regardless of the topic's
        current name. ``runtime`` (when given) is remembered alongside, so the
        rebind relaunches the same agent CLI. See ``thread_directory_memory``
        / ``thread_runtime_memory``.
        """
        if not directory:
            return
        changed = False
        if (
            self.thread_directory_memory.setdefault(user_id, {}).get(thread_id)
            != directory
        ):
            self.thread_directory_memory[user_id][thread_id] = directory
            changed = True
        if (
            runtime
            and self.thread_runtime_memory.setdefault(user_id, {}).get(thread_id)
            != runtime
        ):
            self.thread_runtime_memory[user_id][thread_id] = runtime
            changed = True
        if not changed:
            return  # no change — skip the state write
        self._save_state()
        logger.info(
            "Remembered topic directory: thread %d -> %s (runtime=%s, user %d)",
            thread_id,
            directory,
            runtime or "unchanged",
            user_id,
        )

    def get_remembered_directory(self, user_id: int, thread_id: int) -> str | None:
        """Return the last directory this topic was bound to, or None."""
        return self.thread_directory_memory.get(user_id, {}).get(thread_id)

    def get_remembered_runtime(self, user_id: int, thread_id: int) -> str | None:
        """Runtime the topic last ran, or None if never recorded (pre-runtime
        state rows and never-bound topics). Callers decide the fallback
        (usually ``runtimes.default_runtime()``)."""
        return self.thread_runtime_memory.get(user_id, {}).get(thread_id)

    # --- Worktree-agent metadata (keyed by thread_id) ---

    def set_worktree_meta(
        self, user_id: int, thread_id: int, meta: WorktreeMeta
    ) -> None:
        """Record/replace the worktree metadata for a topic and persist."""
        self.worktree_meta.setdefault(user_id, {})[thread_id] = meta
        self._save_state()

    def get_worktree_meta(self, user_id: int, thread_id: int) -> WorktreeMeta | None:
        """Return the worktree metadata for a topic, or None for a plain topic."""
        return self.worktree_meta.get(user_id, {}).get(thread_id)

    def clear_worktree_meta(self, user_id: int, thread_id: int) -> None:
        """Drop the worktree metadata for a topic (final teardown step)."""
        metas = self.worktree_meta.get(user_id)
        if not metas or thread_id not in metas:
            return
        del metas[thread_id]
        if not metas:
            del self.worktree_meta[user_id]
        self._save_state()

    def mark_worktree_orphaned(self, user_id: int, thread_id: int) -> bool:
        """Flag a worktree as orphaned (topic gone, worktree survives on disk).

        Used by the headless purge path and the orphan-window janitor, which
        must never run destructive git. Returns True if an entry was flipped.
        """
        meta = self.get_worktree_meta(user_id, thread_id)
        if meta is None or meta.status == "orphaned":
            return False
        meta.status = "orphaned"
        self._save_state()
        return True

    def iter_worktree_meta(self) -> Iterator[tuple[int, int, WorktreeMeta]]:
        """Iterate all worktree metadata as (user_id, thread_id, meta)."""
        for user_id, metas in self.worktree_meta.items():
            for thread_id, meta in metas.items():
                yield user_id, thread_id, meta

    def is_worktree_window(self, window_id: str) -> bool:
        """True if the topic bound to ``window_id`` is a worktree agent.

        Reverse lookup (window → thread → meta) so the panel keyboard, which
        only has the window_id, can decide whether to show the 🗑 button.
        """
        for user_id, thread_id, wid in self.iter_thread_bindings():
            if wid == window_id and self.get_worktree_meta(user_id, thread_id):
                return True
        return False

    def reconcile_worktree_meta(self) -> int:
        """Drop worktree_meta rows whose worktree is gone AND thread is unbound.

        Run at startup (after stale-id resolution drops dead bindings) to sweep
        leftovers from manual teardown / lost state. A live agent (bound) or an
        on-disk worktree is never dropped. Returns the count removed.
        """
        dropped = 0
        for user_id in list(self.worktree_meta.keys()):
            for thread_id in list(self.worktree_meta[user_id].keys()):
                meta = self.worktree_meta[user_id][thread_id]
                bound = self.get_window_for_thread(user_id, thread_id) is not None
                if not bound and not Path(meta.path).exists():
                    del self.worktree_meta[user_id][thread_id]
                    dropped += 1
            if not self.worktree_meta[user_id]:
                del self.worktree_meta[user_id]
        if dropped:
            self._save_state()
            logger.info("Reconciled worktree_meta: dropped %d stale row(s)", dropped)
        return dropped

    def mark_worktree_orphaned_by_path(self, path: str) -> bool:
        """Flag the worktree at ``path`` orphaned (used by the window janitor,
        which only knows a window's cwd, not its thread). Returns True if flipped.
        """
        for metas in self.worktree_meta.values():
            for meta in metas.values():
                if meta.path == path and meta.status != "orphaned":
                    meta.status = "orphaned"
                    self._save_state()
                    return True
        return False

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        """Remove a thread binding. Returns the previously bound window_id, or None."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings or thread_id not in bindings:
            return None
        window_id = bindings.pop(thread_id)
        if not bindings:
            del self.thread_bindings[user_id]
        self._save_state()
        logger.info(
            "Unbound thread %d (was %s) for user %d",
            thread_id,
            window_id,
            user_id,
        )
        return window_id

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        """Look up the window_id bound to a thread."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings:
            return None
        return bindings.get(thread_id)

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
    ) -> str | None:
        """Resolve the tmux window_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id)

    def iter_thread_bindings(self) -> Iterator[tuple[int, int, str]]:
        """Iterate all thread bindings as (user_id, thread_id, window_id).

        Provides encapsulated access to thread_bindings without exposing
        the internal data structure directly.
        """
        for user_id, bindings in self.thread_bindings.items():
            for thread_id, window_id in bindings.items():
                yield user_id, thread_id, window_id

    async def find_users_for_session(
        self,
        session_id: str,
    ) -> list[tuple[int, str, int]]:
        """Find all users whose thread-bound window maps to the given session_id.

        Returns list of (user_id, window_id, thread_id) tuples.

        Pure in-memory compare against window_state.session_id — this runs
        for EVERY monitor event (dozens per tick on a tool-heavy turn), and
        the previous implementation re-parsed the whole JSONL per binding per
        event via resolve_session_for_window (O(file size), json.loads per
        line, in the event loop) only to compare a session_id it already had
        in memory. The file's existence needs no re-check either: the event
        being routed exists because the monitor just read that very JSONL.
        """
        result: list[tuple[int, str, int]] = []
        for user_id, thread_id, window_id in self.iter_thread_bindings():
            state = self.window_states.get(window_id)
            if state and state.session_id == session_id:
                result.append((user_id, window_id, thread_id))
        return result

    # --- Tmux helpers ---

    def send_lock(self, binding_value: str) -> asyncio.Lock:
        """Per-binding lock serializing multi-step pane writes.

        Sending text is chunked typing with awaits between chunks plus a
        separate Enter; without the lock, concurrent writers interleave
        inside one prompt and an Enter from one message submits half of
        another. Held by send_to_window/send_keys for the whole sequence;
        the restart handlers take it explicitly around /exit → relaunch
        so nothing types into the pane between the two.
        """
        lock = self._send_locks.get(binding_value)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[binding_value] = lock
        return lock

    async def send_to_window(self, window_id: str, text: str) -> tuple[bool, str]:
        """Send text to a bound agent.

        ``window_id`` accepts either form stored in thread_bindings:
          - ``@<id>`` — legacy tmux window on the host.
          - ``docker:<agent>`` — Claude Code inside a docker container,
            reached via ``docker exec`` to the agent's tmux pane.

        Docker routing is gated by ``config.docker_agents_enabled`` — a
        stale ``docker:*`` binding with the flag off yields an explicit
        error rather than falling through to tmux (which would report
        a confusing "Window not found").
        """
        display = self.get_display_name(window_id)
        logger.debug(
            "send_to_window: window_id=%s (%s), text_len=%d",
            window_id,
            display,
            len(text),
        )

        async with self.send_lock(window_id):
            if self._is_docker_binding(window_id):
                if not config.docker_agents_enabled:
                    return False, "Docker agents disabled in config"
                agent_name = window_id[len("docker:") :]
                agent = config.get_docker_agent(agent_name)
                if not agent:
                    return False, f"Docker agent '{agent_name}' not configured"
                if not await docker_driver.is_container_alive(agent.container):
                    return False, f"Container '{agent.container}' is not running"
                success = await docker_driver.send_keys(agent.container, text)
                if success:
                    self.mark_generating(window_id)
                    return True, f"Sent to {display}"
                return False, "Failed to send keys (docker)"

            window = await tmux_manager.find_window_by_id(window_id)
            if not window:
                return False, "Window not found (may have been closed)"
            success = await tmux_manager.send_keys(window.window_id, text)
            if success:
                self.mark_generating(window_id)
                return True, f"Sent to {display}"
            return False, "Failed to send keys"

    async def send_keys(
        self,
        binding_value: str,
        keys: str,
        *,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        """Raw key send to a bound agent. No side effects.

        Same routing as send_to_window but without mark_generating or
        user-facing status — meant for inline-keyboard key presses
        (arrows, Escape, Shift+Tab, etc.) that must not advance the
        generating state machine.

        Takes the same per-binding send lock as send_to_window: a nav
        key or typed answer landing mid-chunked-send is exactly the
        interleaving the lock exists to prevent. Single key presses
        hold it for one tmux round-trip — uncontended cost is nil.
        """
        async with self.send_lock(binding_value):
            if self._is_docker_binding(binding_value):
                if not config.docker_agents_enabled:
                    return False
                agent = config.get_docker_agent(binding_value[len("docker:") :])
                if not agent:
                    return False
                if not await docker_driver.is_container_alive(agent.container):
                    return False
                return await docker_driver.send_keys(
                    agent.container, keys, enter=enter, literal=literal
                )
            window = await tmux_manager.find_window_by_id(binding_value)
            if not window:
                return False
            return await tmux_manager.send_keys(
                window.window_id, keys, enter=enter, literal=literal
            )

    async def send_composer_image(
        self, binding_value: str, image_path: str, caption: str = ""
    ) -> bool:
        """Deliver an image to a runtime with NATIVE composer input (Codex).

        Types the image PATH into the composer — codex auto-converts a recognized
        image path to ``[Image #N]`` after a brief IDLE (no keypress), a CLIENT-
        side read that works even when the path is outside the sandbox's workspace
        (the failure mode of the plain ``(image attached: <path>)`` text marker).
        Then appends the caption and submits with a SINGLE Enter, so image +
        caption land as ONE turn.

        Enter must NOT be pressed to "attach" — in codex an Enter SUBMITS the
        composer, so a premature Enter sends ``[Image #N]`` alone and the caption
        becomes a second, separate turn (verified live: the bug this fixes). The
        attach is idle-triggered; we WAIT for the ``[Image #N]`` token to appear,
        then type the caption, then Enter once.

        Multi-step pane write held under ONE ``send_lock`` (calling the transport
        primitives directly; the wrapper ``send_keys`` would re-lock). Codex is
        host-tmux only; returns False for a docker binding or a missing window.
        """
        # Codex's idle auto-convert is ~2-3s; poll up to ~6s, break on the token.
        _POLL_TRIES, _POLL_INTERVAL = 30, 0.2
        token = (
            get_runtime(self.window_runtime(binding_value)).composer_image_token
            or "[Image #"
        )
        async with self.send_lock(binding_value):
            window = await tmux_manager.find_window_by_id(binding_value)
            if not window:
                return False
            wid = window.window_id
            # 1. Type the path (NO Enter — an Enter here would submit the image
            #    alone before the caption is typed).
            if not await tmux_manager.send_keys(
                wid, image_path, enter=False, literal=True
            ):
                return False
            # 2. Wait for codex's idle auto-convert: the raw path in the composer
            #    turns into the [Image #N] attach token on its own.
            for _ in range(_POLL_TRIES):
                await asyncio.sleep(_POLL_INTERVAL)
                pane = await tmux_manager.capture_pane(wid)
                if pane and token in "\n".join(pane.splitlines()[-8:]):
                    break
            else:
                logger.warning(
                    "send_composer_image: %s never showed %r (idle auto-convert "
                    "didn't fire) — submitting anyway",
                    binding_value,
                    token,
                )
            # 3. Append the caption AFTER the attach token, then submit with ONE
            #    Enter so [Image #N] + caption go as a single turn.
            if caption:
                await tmux_manager.send_keys(
                    wid, " " + caption, enter=False, literal=True
                )
            ok = await tmux_manager.send_keys(wid, "Enter", enter=False, literal=False)
            if ok:
                self.mark_generating(binding_value)
            return ok

    async def capture_pane(
        self,
        binding_value: str,
        *,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str | None:
        """Capture the bound agent's pane (tmux window or docker container)."""
        if self._is_docker_binding(binding_value):
            if not config.docker_agents_enabled:
                return None
            agent = config.get_docker_agent(binding_value[len("docker:") :])
            if not agent:
                return None
            if not await docker_driver.is_container_alive(agent.container):
                return None
            return await docker_driver.capture_pane(
                agent.container,
                with_ansi=with_ansi,
                scrollback_lines=scrollback_lines,
            )
        window = await tmux_manager.find_window_by_id(binding_value)
        if not window:
            return None
        return await tmux_manager.capture_pane(
            window.window_id,
            with_ansi=with_ansi,
            scrollback_lines=scrollback_lines,
        )

    async def kill_agent(self, binding_value: str) -> bool:
        """Kill the bound agent.

        Tmux binding → kill the tmux window (the Claude process dies with it).
        Docker binding → kill the in-container tmux session named `claude`;
        the container itself stays up so /restart can re-spawn the session.
        Returns True on success or if the target is already dead.
        """
        if self._is_docker_binding(binding_value):
            if not config.docker_agents_enabled:
                return False
            agent = config.get_docker_agent(binding_value[len("docker:") :])
            if not agent:
                return False
            if not await docker_driver.is_container_alive(agent.container):
                return True
            return await docker_driver.kill_session(agent.container)
        window = await tmux_manager.find_window_by_id(binding_value)
        if not window:
            return True
        return await tmux_manager.kill_window(window.window_id)

    # --- Message history ---

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get user/assistant messages for a window's session.

        Resolves window → transcript through the window's runtime (Claude:
        session_id+cwd JSONL under the projects tree; Codex: the cwd-matched
        rollout), then parses with that runtime's parser. Supports byte range
        filtering via start_byte/end_byte. Returns (messages, total_count).
        """
        runtime = get_runtime(self.window_runtime(window_id))
        file_path = await runtime.history_transcript(self, window_id)
        if not file_path or not file_path.exists():
            return [], 0

        # Read JSONL entries (optionally filtered by byte range)
        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    # Check byte limit before reading
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = TranscriptParser.parse_line(line)  # generic JSON line
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        parsed_entries, _ = runtime.parse_entries(entries, {})
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in parsed_entries
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()
