"""Revive a topic's agent from scratch — same folder, same CLI, same session.

🔄 Restart means "bring back what was here", and that has to hold after the
window is already gone: an agent that crashed (or was reaped as dead 30 s
later) leaves the topic unbound, and the panel/`/restart` used to answer
«window gone» — the only way back was a message, which now opens the session
picker. So the restart paths call :func:`revive_topic_agent`, which rebuilds
the window from what the topic remembers:

  - folder   — ``thread_directory_memory`` (survives the window),
  - runtime  — ``thread_runtime_memory`` (a codex topic comes back as codex),
  - session  — the window's own ``session_id`` while its state still exists,
    else the newest session of that runtime in the folder. Both resolve to a
    `--resume`, so the conversation continues instead of starting over.

This is the ONE path that creates a window without a picker tap, and that is
the point: the user asked for this exact agent back (see binding-flows.md —
auto-bind itself never starts a session).
"""

from __future__ import annotations

import logging

from ..runtimes import AgentRuntime, default_runtime, get_runtime
from ..session import session_manager
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


class ReviveError(Exception):
    """Revive failed. ``key`` is an i18n key, ``fmt`` its format arguments."""

    def __init__(self, key: str, **fmt: object) -> None:
        super().__init__(key)
        self.key = key
        self.fmt = fmt


def _runtime_for_topic(user_id: int, thread_id: int) -> AgentRuntime:
    """The CLI this topic last ran, degraded to the default if it's gone."""
    remembered = session_manager.get_remembered_runtime(user_id, thread_id)
    rt = get_runtime(remembered) if remembered else default_runtime()
    if not rt.is_available():
        logger.warning(
            "Revive: runtime %s unavailable for thread %d — using default",
            rt.name,
            thread_id,
        )
        rt = default_runtime()
    return rt


async def _session_to_resume(rt: AgentRuntime, cwd: str, old_wid: str | None) -> str:
    """The session id 🔄 should continue, or "" when there is nothing to resume.

    The dead window's own state is the exact answer while it lasts; the stale
    sweep in ``load_session_map`` drops it, so the newest session recorded for
    the folder is the fallback (same conversation for every everyday case —
    one topic works one folder).
    """
    if old_wid:
        ws = session_manager.window_states.get(old_wid)
        if ws and ws.session_id:
            return ws.session_id
    sessions = await rt.list_sessions(session_manager, cwd)
    return sessions[0].session_id if sessions else ""


async def revive_topic_agent(
    user_id: int, thread_id: int, *, fresh: bool = False
) -> tuple[str, str]:
    """Recreate this topic's agent window and bind it. Returns (wid, display).

    ``fresh=True`` (🆕 New session on a dead window) skips the resume and
    starts the same CLI in the same folder with an empty context.

    Raises :class:`ReviveError` with a user-facing i18n key when the topic has
    no remembered folder, the folder is gone, a hookless runtime already runs
    there, or tmux refuses the window.
    """
    from pathlib import Path

    cwd = session_manager.get_remembered_directory(user_id, thread_id)
    if not cwd or not Path(cwd).is_dir():
        raise ReviveError("restart.nothing_to_revive")

    rt = _runtime_for_topic(user_id, thread_id)
    old_wid = session_manager.get_window_for_thread(user_id, thread_id)
    resume_id = "" if fresh else await _session_to_resume(rt, cwd, old_wid)

    # Same-cwd guard for hookless runtimes: their transcript resolves by cwd
    # ("newest wins"), so a second live window here would make two topics
    # mirror one session.
    if not rt.uses_session_map and await session_manager.has_live_agent_on_cwd(
        rt.name, cwd
    ):
        raise ReviveError("bot.same_dir_conflict", agent=rt.display_name, dir=cwd)

    ok, message, wname, wid = await tmux_manager.create_window(
        cwd, resume_session_id=resume_id or None, runtime=rt.name
    )
    if not ok:
        logger.warning("Revive: create_window failed for %s: %s", cwd, message)
        raise ReviveError("restart.window_failed", err=message[:120])

    session_manager.tag_window_runtime(wid, rt.name, cwd)
    session_manager.record_thread_directory(user_id, thread_id, cwd, runtime=rt.name)
    hook_ok = False
    if rt.uses_session_map:
        hook_ok = await session_manager.wait_for_session_map_entry(
            wid, timeout=15.0 if resume_id else 5.0
        )
    session_manager.bind_thread(user_id, thread_id, wid, window_name=wname)

    if resume_id and rt.uses_session_map:
        # `--resume` makes the SessionStart hook report a NEW session_id while
        # messages keep writing to the ORIGINAL JSONL — pin window_state to the
        # resumed id so the monitor tracks the right transcript (mirrors the
        # resume override in bot._create_and_bind_window).
        ws = session_manager.get_window_state(wid)
        if not hook_ok:
            logger.warning(
                "Revive: hook timed out for %s — pinning session_id=%s cwd=%s",
                wid,
                resume_id,
                cwd,
            )
            ws.session_id = resume_id
            ws.cwd = cwd
            ws.window_name = wname
            session_manager._save_state()
        elif ws.session_id != resume_id:
            ws.session_id = resume_id
            session_manager._save_state()

    logger.info(
        "Revived agent for thread %d: window %s at %s (runtime=%s, resume=%s)",
        thread_id,
        wid,
        cwd,
        rt.name,
        resume_id or "none",
    )
    return wid, wname
