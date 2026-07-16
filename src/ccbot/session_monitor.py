"""Session monitoring service — watches JSONL files for new messages.

Runs an async polling loop that:
  1. Loads the current session_map to know which sessions to watch.
  2. Detects session_map changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each session file using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects to a callback.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Key classes: SessionMonitor, NewMessage, SessionInfo.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiofiles

from . import i18n
from .config import config
from .monitor_state import MonitorState, TrackedSession
from .runtimes import monitored_runtimes
from .transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)

# Context-fill notifications. When an assistant turn's input-side context
# (input + cache-read + cache-creation tokens) crosses one of these absolute
# token counts, the monitor emits one "context_alert" NewMessage to the bound
# topic. Thresholds are absolute token counts (not percentages) against a fixed
# 1M window — so no per-model window detection is needed (the JSONL `model`
# field doesn't carry the `[1m]` suffix anyway, so 200k-vs-1M can't be inferred
# from it).
CONTEXT_TOKEN_THRESHOLDS: tuple[int, ...] = (300_000, 500_000, 700_000)
CONTEXT_WINDOW_TOKENS = 1_000_000


def _format_context_alert(tokens: int) -> str:
    """One-liner for a crossed context threshold (actual fill shown)."""
    pct = round(tokens / CONTEXT_WINDOW_TOKENS * 100)
    return i18n.tr("ctx.alert", k=tokens // 1000, pct=pct)


@dataclass
class SessionInfo:
    """Information about a Claude Code session."""

    session_id: str
    file_path: Path


@dataclass
class NewMessage:
    """A new message detected by the monitor."""

    session_id: str
    text: str
    is_complete: bool  # True when stop_reason is set (final message)
    content_type: str = "text"  # "text" or "thinking"
    tool_use_id: str | None = None
    role: str = "assistant"  # "user" or "assistant"
    tool_name: str | None = None  # For tool_use messages, the tool name
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    # ISO timestamp from JSONL entry. Used by handle_new_message to filter
    # stale replays away from TTS — anything older than VOICE_FRESH_WINDOW
    # is forced to text fallback even in voice-mode topics.
    entry_ts_iso: str | None = None
    # True for the assistant text block that precedes an AskUserQuestion call —
    # see ParsedEntry.precedes_interactive_prompt and consume_pending_prose_upgrade.
    precedes_interactive_prompt: bool = False


class SessionMonitor:
    """Monitors Claude Code sessions for new assistant messages.

    Uses simple async polling with aiofiles for non-blocking I/O.
    Emits both intermediate and complete assistant messages.
    """

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.claude_projects_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        # Per-session pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, Any]] = {}  # session_id -> pending
        # Track last known session_map for detecting changes
        # Keys may be window_id (@12) or window_name (old format) during transition
        self._last_session_map: dict[str, str] = {}  # window_key -> session_id
        # In-memory mtime cache for quick file change detection (not persisted)
        self._file_mtimes: dict[str, float] = {}  # session_id -> last_seen_mtime
        # session_id -> set of context-token thresholds already announced this
        # session. In-memory (like _pending_tools): a restart re-evaluates
        # against the current fill and announces at most the current band once.
        self._context_fired: dict[str, set[int]] = {}

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def _project_roots(self) -> list[Path]:
        """All project-log roots to scan this tick.

        Always includes the main ``claude_projects_path`` (host-side Claude
        Code for tmux agents). Adds each docker agent's
        ``<claude_home_host_path>/projects`` so JSONL written inside a
        container (visible on host via bind-mount) is picked up the same
        way. Returns only existing directories — missing ones are silently
        skipped so a not-yet-started container doesn't spam errors.
        """
        roots: list[Path] = [self.projects_path]
        for agent in config.active_docker_agents():
            roots.append(agent.claude_home_host_path / "projects")
        return [r for r in roots if r.exists()]

    async def scan_projects(self) -> list[SessionInfo]:
        """Return every (session_id, jsonl) pair under each project root.

        Walks every root returned by ``_project_roots`` — the host
        ``~/.claude/projects`` plus any docker-agent claude-home bind
        mounted in. check_for_updates() filters the result by
        ``active_session_ids`` (the sessions in session_map.json) so we
        don't actually process sessions the hook hasn't registered. We
        used to also pre-filter here by matching each project's cwd
        against the live tmux windows' cwds, but that broke whenever an
        agent's pane lost its cwd (e.g. after an ``fusermount -u`` of a
        mount the pane was sitting in — the kernel's cwd pointer falls
        back to a phantom path and the session looks "not active" even
        though it's clearly running and listed in session_map).
        """
        # The same session_id CAN legitimately appear in several files:
        # `claude --resume <id>` from a DIFFERENT cwd keeps the id but writes a
        # new JSONL under the new cwd's project dir (e.g. a session started in
        # ~/agents/notes continued in ~/projects/notes-writer). Newest mtime
        # wins — that's the live continuation; picking the stale copy silences
        # the topic entirely (the bug this replaced "first wins" caused).
        best: dict[str, SessionInfo] = {}
        for projects_root in self._project_roots():
            await self._scan_one_root(projects_root, best)
        return list(best.values())

    @staticmethod
    def _offer_session(best: dict[str, SessionInfo], candidate: SessionInfo) -> None:
        """Keep the newest-mtime file per session_id (collisions are rare, so
        the extra stat runs only when a duplicate id is actually seen)."""
        current = best.get(candidate.session_id)
        if current is None:
            best[candidate.session_id] = candidate
            return
        try:
            if candidate.file_path.stat().st_mtime > current.file_path.stat().st_mtime:
                best[candidate.session_id] = candidate
        except OSError:
            pass  # unstatable candidate loses

    async def _scan_one_root(
        self,
        projects_root: Path,
        best: dict[str, SessionInfo],
    ) -> None:
        for project_dir in projects_root.iterdir():
            if not project_dir.is_dir():
                continue

            index_file = project_dir / "sessions-index.json"
            indexed_ids: set[str] = set()

            if index_file.exists():
                try:
                    async with aiofiles.open(index_file, "r") as f:
                        content = await f.read()
                    index_data = json.loads(content)
                    entries = index_data.get("entries", [])

                    for entry in entries:
                        session_id = entry.get("sessionId", "")
                        full_path = entry.get("fullPath", "")
                        if not session_id or not full_path:
                            continue
                        indexed_ids.add(session_id)
                        file_path = Path(full_path)
                        if file_path.exists():
                            self._offer_session(
                                best,
                                SessionInfo(
                                    session_id=session_id,
                                    file_path=file_path,
                                ),
                            )
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(f"Error reading index {index_file}: {e}")

            # Pick up un-indexed .jsonl files (new sessions not yet in index)
            try:
                for jsonl_file in project_dir.glob("*.jsonl"):
                    session_id = jsonl_file.stem
                    if session_id in indexed_ids:
                        continue
                    self._offer_session(
                        best,
                        SessionInfo(
                            session_id=session_id,
                            file_path=jsonl_file,
                        ),
                    )
            except OSError as e:
                logger.debug(f"Error scanning jsonl files in {project_dir}: {e}")

    async def _read_new_lines(
        self, session: TrackedSession, file_path: Path
    ) -> list[dict]:
        """Read new lines from a session file using byte offset for efficiency.

        Detects file truncation (e.g. after /clear) and resets offset.
        Recovers from corrupted offsets (mid-line) by scanning to next line.
        """
        new_entries = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                # Get file size to detect truncation
                await f.seek(0, 2)  # Seek to end
                file_size = await f.tell()

                # Detect file truncation: if offset is beyond file size, reset
                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s "
                        "(offset %d > size %d). Resetting.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0

                # Seek to last read position for incremental reading
                await f.seek(session.last_byte_offset)

                # Detect corrupted offset: if we're mid-line (not at '{'),
                # scan forward to the next line start. This can happen if
                # the state file was manually edited or corrupted.
                if session.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s (mid-line), "
                            "scanning to next line",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await f.readline()  # Skip rest of partial line
                        session.last_byte_offset = await f.tell()
                        return []
                    await f.seek(session.last_byte_offset)  # Reset for normal read

                # Read only new lines from the offset.
                # Track safe_offset: only advance past lines that parsed
                # successfully. A non-empty line that fails JSON parsing is
                # likely a partial write; stop and retry next cycle.
                safe_offset = session.last_byte_offset
                try:
                    async for line in f:
                        data = TranscriptParser.parse_line(line)
                        if data:
                            new_entries.append(data)
                            safe_offset = await f.tell()
                        elif line.strip():
                            if line.endswith("\n"):
                                # Complete line the parser rejected (corrupt
                                # record, or an entry type it skips). Advance
                                # past it — otherwise it stalls ALL downstream
                                # output for this session forever, silently.
                                # The trailing newline is the completeness
                                # signal: a partial write hasn't got one yet.
                                # (audit MEDIUM)
                                logger.warning(
                                    "Skipping unparseable JSONL line in session %s",
                                    session.session_id,
                                )
                                safe_offset = await f.tell()
                            else:
                                # No trailing newline → a partial write still in
                                # flight; stop and retry the same offset.
                                logger.warning(
                                    "Partial JSONL line in session %s, "
                                    "will retry next cycle",
                                    session.session_id,
                                )
                                break
                        else:
                            # Empty line — safe to skip
                            safe_offset = await f.tell()
                except UnicodeDecodeError:
                    # Claude appends UTF-8 (mostly multibyte cyrillic here)
                    # while we read — a char split at EOF raises mid-
                    # iteration. Entries parsed so far are delivered; the
                    # offset commit below stops at the last good line, so
                    # the next cycle resumes there without duplicates.
                    logger.debug(
                        "Truncated multibyte char in session %s, will retry next cycle",
                        session.session_id,
                    )

                session.last_byte_offset = safe_offset

        except (OSError, UnicodeDecodeError) as e:
            # UnicodeDecodeError here = the seek/first-char probe hit a
            # mid-multibyte offset; offset untouched, next cycle retries.
            logger.warning("Error reading session file %s: %s", file_path, e)
        return new_entries

    async def check_for_updates(self, active_session_ids: set[str]) -> list[NewMessage]:
        """Check every runtime's live transcripts for new messages.

        Generic over runtime: each registered ``AgentRuntime`` resolves its own
        transcripts (``iter_transcripts``) and parses them (``parse_entries``);
        the per-transcript read → parse → emit tail is shared
        (``_process_transcript``). ``active_session_ids`` (from the Claude
        session_map) is passed through — runtimes without a session_map ignore
        it. A failure in one runtime's resolution never blocks the others.
        """
        from .session import session_manager

        new_messages: list[NewMessage] = []
        for runtime in monitored_runtimes():
            try:
                refs = await runtime.iter_transcripts(
                    session_manager, self, active_session_ids
                )
            except Exception as e:
                logger.exception(
                    "iter_transcripts failed for runtime %s: %s", runtime.name, e
                )
                continue
            seen: set[str] = set()  # first ref wins if a session_id repeats
            for session_id, file_path in refs:
                if session_id in seen:
                    continue
                seen.add(session_id)
                new_messages.extend(
                    await self._process_transcript(runtime, session_id, file_path)
                )
        self.state.save_if_dirty()
        return new_messages

    def _parsed_to_messages(
        self, session_id: str, parsed_entries: list
    ) -> list[NewMessage]:
        """Convert a ParsedEntry list into NewMessages (shared by both runtimes).

        Drops empty entries and user messages when show_user_messages is off.
        The runtime-agnostic tail of both the Claude and Codex paths.
        """
        msgs: list[NewMessage] = []
        for entry in parsed_entries:
            if not entry.text and not entry.image_data:
                continue
            if entry.role == "user" and not config.show_user_messages:
                continue
            msgs.append(
                NewMessage(
                    session_id=session_id,
                    text=entry.text,
                    is_complete=True,
                    content_type=entry.content_type,
                    tool_use_id=entry.tool_use_id,
                    role=entry.role,
                    tool_name=entry.tool_name,
                    image_data=entry.image_data,
                    entry_ts_iso=entry.timestamp,
                    precedes_interactive_prompt=entry.precedes_interactive_prompt,
                )
            )
        return msgs

    async def _process_transcript(
        self, runtime: Any, session_id: str, file_path: Path
    ) -> list[NewMessage]:
        """Read new lines from one transcript, parse via ``runtime``, emit messages.

        The runtime-agnostic tail shared by every runtime: byte-offset
        incremental read, EOF-start on first track (so a resumed session's
        history isn't re-flooded), mtime skip, parse, and context alert. Only
        ``runtime.parse_entries`` / ``latest_context_tokens`` differ per runtime.
        """
        new_messages: list[NewMessage] = []
        try:
            tracked = self.state.get_session(session_id)
            if tracked is None:
                # Newly tracked → start at current EOF. Fresh session (post-/clear
                # or brand new) is tiny so nothing's missed; a resumed one already
                # holds history the user saw or doesn't want re-flooded.
                try:
                    st = file_path.stat()
                    current_mtime = st.st_mtime
                    file_size = st.st_size
                except OSError:
                    current_mtime = 0.0
                    file_size = 0
                tracked = TrackedSession(
                    session_id=session_id,
                    file_path=str(file_path),
                    last_byte_offset=file_size,
                )
                self.state.update_session(tracked)
                self._file_mtimes[session_id] = current_mtime
                logger.info(
                    "Started tracking session %s at EOF (%d bytes)",
                    session_id,
                    file_size,
                )
                return new_messages

            if tracked.file_path != str(file_path):
                # The session moved to a different transcript file — a
                # cross-directory `--resume` keeps the session id but writes
                # under the new cwd's project dir. The old byte offset is
                # meaningless against the new file (reading with it would
                # either reflood the copied history or start mid-line), so
                # re-track at the new file's EOF; appends flow from next tick.
                try:
                    st = file_path.stat()
                    new_size, new_mtime = st.st_size, st.st_mtime
                except OSError:
                    return new_messages
                logger.info(
                    "Session %s switched transcript %s -> %s — re-tracking at "
                    "EOF (%d bytes)",
                    session_id,
                    tracked.file_path,
                    file_path,
                    new_size,
                )
                tracked.file_path = str(file_path)
                tracked.last_byte_offset = new_size
                self.state.update_session(tracked)
                self._file_mtimes[session_id] = new_mtime
                return new_messages

            try:
                st = file_path.stat()
                current_mtime = st.st_mtime
                current_size = st.st_size
            except OSError:
                return new_messages

            last_mtime = self._file_mtimes.get(session_id, 0.0)
            if current_mtime <= last_mtime and current_size <= tracked.last_byte_offset:
                return new_messages  # unchanged

            new_entries = await self._read_new_lines(tracked, file_path)
            self._file_mtimes[session_id] = current_mtime

            carry = self._pending_tools.get(session_id, {})
            parsed_entries, remaining = runtime.parse_entries(new_entries, carry)
            if remaining:
                self._pending_tools[session_id] = remaining
            else:
                self._pending_tools.pop(session_id, None)

            new_messages.extend(self._parsed_to_messages(session_id, parsed_entries))

            # Context-fill threshold alerts — the token extraction is
            # runtime-specific (usage shape differs); the threshold bookkeeping
            # is generic. None → runtime opts out (e.g. Codex, for now).
            latest_tokens = runtime.latest_context_tokens(new_entries)
            if latest_tokens is not None:
                alert = self._check_context_thresholds(session_id, latest_tokens)
                if alert is not None:
                    new_messages.append(
                        NewMessage(
                            session_id=session_id,
                            text=alert,
                            is_complete=True,
                            content_type="context_alert",
                        )
                    )

            self.state.update_session(tracked)
        except (OSError, UnicodeDecodeError) as e:
            # Per-transcript catch: one unreadable file must not abort the tick.
            logger.debug("Error processing session %s: %s", session_id, e)
        return new_messages

    def _check_context_thresholds(self, session_id: str, tokens: int) -> str | None:
        """Return an alert string iff `tokens` newly crossed a threshold.

        Tracks per-session which thresholds have fired; a threshold re-arms
        once the fill drops back below it (e.g. after /compact or /clear), so
        a genuine re-cross alerts again. When several thresholds are crossed in
        one go (e.g. the first measurement after a restart is already at 600k),
        a single alert showing the actual fill is emitted — not one per band.
        """
        crossed = {t for t in CONTEXT_TOKEN_THRESHOLDS if tokens >= t}
        newly = crossed - self._context_fired.get(session_id, set())
        self._context_fired[session_id] = crossed
        if not newly:
            return None
        return _format_context_alert(tokens)

    async def _load_current_session_map(self) -> dict[str, str]:
        """Merge every session_map source into one ``binding_value → session_id``.

        Sources:
          - Main ``~/.ccbot/session_map.json`` keyed ``"<tmux_session>:<window_id>"``
            (e.g. ``ccbot:@12``). The tmux-session prefix is stripped so the
            resulting key is the tmux binding value (``@12``).
          - Each active docker agent's ``session_map_path`` on host (written
            by the container's hook via bind mount) keyed ``"docker:<agent>"``.
            No prefix stripping — the key *is* the binding value.

        The merged dict is keyed by the binding value that thread_bindings
        stores, so ``_detect_and_cleanup_changes`` can compare tmux and
        docker entries uniformly. First non-empty session_id wins per key.
        """
        window_to_session: dict[str, str] = {}
        prefix = f"{config.tmux_session_name}:"

        def _ingest(
            session_map: dict[str, Any],
            tmux_prefix: str | None,
            only_key: str | None = None,
        ) -> None:
            for key, info in session_map.items():
                session_id = info.get("session_id", "")
                if not session_id:
                    continue
                if only_key is not None and key != only_key:
                    # Per-agent maps are written inside the container
                    # (untrusted) — accept only the agent's own binding
                    # key, otherwise a compromised agent could spoof
                    # another agent's session.
                    continue
                if tmux_prefix is not None:
                    if not key.startswith(tmux_prefix):
                        continue
                    binding_key = key[len(tmux_prefix) :]
                else:
                    binding_key = key
                if binding_key and binding_key not in window_to_session:
                    window_to_session[binding_key] = session_id

        # Main (tmux-host) session_map.
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                _ingest(json.loads(content), prefix)
            except (json.JSONDecodeError, OSError):
                pass

        # Per-agent session_maps (docker).
        for agent in config.active_docker_agents():
            path = agent.session_map_path
            if not path.exists():
                continue
            try:
                async with aiofiles.open(path, "r") as f:
                    content = await f.read()
                _ingest(json.loads(content), None, only_key=f"docker:{agent.name}")
            except (json.JSONDecodeError, OSError):
                pass

        return window_to_session

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up all tracked sessions not in current session_map (used on startup)."""
        current_map = await self._load_current_session_map()
        active_session_ids = set(current_map.values())

        stale_sessions = []
        for session_id in self.state.tracked_sessions.keys():
            if session_id not in active_session_ids:
                stale_sessions.append(session_id)

        if stale_sessions:
            logger.info(
                f"[Startup cleanup] Removing {len(stale_sessions)} stale sessions"
            )
            for session_id in stale_sessions:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                self._context_fired.pop(session_id, None)
            self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, str]:
        """Detect session_map changes and cleanup replaced/removed sessions.

        Returns current session_map for further processing.
        """
        current_map = await self._load_current_session_map()

        sessions_to_remove: set[str] = set()

        # Check for window session changes (window exists in both, but session_id changed)
        for window_id, old_session_id in self._last_session_map.items():
            new_session_id = current_map.get(window_id)
            if new_session_id and new_session_id != old_session_id:
                logger.info(
                    "Window '%s' session changed: %s -> %s",
                    window_id,
                    old_session_id,
                    new_session_id,
                )
                sessions_to_remove.add(old_session_id)

        # Check for deleted windows (window in old map but not in current)
        old_windows = set(self._last_session_map.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows

        for window_id in deleted_windows:
            old_session_id = self._last_session_map[window_id]
            logger.info(
                "Window '%s' deleted, removing session %s",
                window_id,
                old_session_id,
            )
            sessions_to_remove.add(old_session_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                self._context_fired.pop(session_id, None)
            self.state.save_if_dirty()

        # Reset user_window_offsets for windows with changed sessions
        # so new session messages are delivered from the start.
        # The merged session_map keys ARE binding values (either '@12' or
        # 'docker:assistant') — pass them through unchanged. The old code
        # did a split(':')[-1] here as defensive legacy handling, but that
        # would have mangled docker bindings ('docker:assistant' → 'assistant')
        # and was already dead for tmux entries since _load_current_session_map
        # strips the tmux-session prefix before returning.
        from .session import session_manager as _sm

        for binding_value, old_session_id in self._last_session_map.items():
            new_session_id = current_map.get(binding_value)
            if new_session_id and new_session_id != old_session_id:
                _sm.reset_all_user_offsets_for_window(binding_value)

        # Update last known map
        self._last_session_map = current_map

        return current_map

    async def _monitor_loop(self) -> None:
        """Background loop for checking session updates.

        Uses simple async polling with aiofiles for non-blocking I/O.
        """
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Deferred import to avoid circular dependency (cached once)
        from .session import session_manager

        # Clean up all stale sessions on startup
        await self._cleanup_all_stale_sessions()
        # Initialize last known session_map
        self._last_session_map = await self._load_current_session_map()

        while self._running:
            try:
                # Load hook-based session map updates
                await session_manager.load_session_map()

                # Detect session_map changes and cleanup replaced/removed sessions
                current_map = await self._detect_and_cleanup_changes()
                active_session_ids = set(current_map.values())

                # Check every runtime's transcripts in one generic pass (Claude
                # via session_map + scan_projects, Codex via cwd-resolved
                # rollouts, …) — each runtime's iter_transcripts, one shared tail.
                new_messages = await self.check_for_updates(active_session_ids)

                for msg in new_messages:
                    status = "complete" if msg.is_complete else "streaming"
                    preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
                    logger.info("[%s] session=%s: %s", status, msg.session_id, preview)
                    if self._message_callback:
                        try:
                            await self._message_callback(msg)
                        except Exception as e:
                            logger.exception(f"Message callback error: {e}")

            except Exception as e:
                logger.exception(f"Monitor loop error: {e}")

            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state.save()
        logger.info("Session monitor stopped and state saved")
