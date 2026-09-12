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

A **docker** binding needs its own half (:func:`revive_docker_agent` and the
offer keyboard around it): there is no tmux window to rebuild and no folder to
pick — the agent is a tmux session *inside* the container, which a
``docker restart`` wipes for every sibling (the entrypoint recreates only the
main one). The topic keeps its binding and its files, so recovery is just
"start that session again", either continuing a conversation or clean.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..agent_session import AgentSession
from ..i18n import tr
from ..runtimes import AgentRuntime, default_runtime, get_runtime
from ..session import session_manager
from ..tmux_manager import tmux_manager
from ..utils import is_valid_session_id
from .callback_data import CB_DVR_BACK, CB_DVR_FRESH, CB_DVR_LIST, CB_DVR_RESUME
from .message_sender import safe_send

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


# --- Docker bindings: their agent is a tmux session inside the container ----

# How many earlier conversations the «other sessions» screen offers. One
# container's claude-home holds every sibling's history (33 files on the
# operator's box), so the list is a shortlist by recency, not an archive.
REVIVE_SESSION_ROWS = 6


async def revive_docker_agent(window_id: str, *, session_id: str | None) -> None:
    """Start this docker binding's agent again — resuming, or clean.

    ``session_id`` continues that conversation (``claude --resume``);
    ``None`` starts a fresh one with a ccbot-pinned id, the same way a
    sibling is provisioned. The binding and the topic are untouched either
    way — that is the whole point of reviving instead of re-creating: the
    topic keeps its history and its files.
    """
    from ..docker_driver import docker_driver

    target = session_manager.resolve_docker_target(window_id)
    if target is None:
        raise ReviveError("revive.no_agent")
    if not await docker_driver.is_container_alive(target.agent.container):
        raise ReviveError("revive.container_down", container=target.agent.container)
    if session_id and not is_valid_session_id(session_id):
        # The id rides in a callback payload and ends up on a command line.
        raise ReviveError("revive.bad_session")

    cwd = session_manager.get_window_state(window_id).cwd or "/workspace"
    async with session_manager.send_lock(window_id):
        # A dead session is the normal case here; kill_session tolerates its
        # absence and clears a half-alive one that would refuse the new name.
        await docker_driver.kill_session(
            target.agent.container, session=target.tmux_session
        )
        await asyncio.sleep(1)
        ok = await session_manager.start_docker_agent(
            window_id,
            resume_session_id=session_id,
            new_session_id=None if session_id else str(uuid.uuid4()),
            cwd=cwd,
        )
    if not ok:
        raise ReviveError("revive.start_failed")

    if session_id:
        # `--resume` makes the container's hook report a NEW session id while
        # messages keep landing in the resumed JSONL — point the monitor at
        # the transcript the user actually chose (same override as the tmux
        # revive and the picker's resume path).
        state = session_manager.get_window_state(window_id)
        if state.session_id != session_id:
            state.session_id = session_id
            state.cwd = cwd
            session_manager._save_state()

    logger.info(
        "Revived docker agent %s (container=%s, session=%s, resume=%s)",
        window_id,
        target.agent.container,
        target.tmux_session,
        session_id or "none",
    )


async def docker_revive_options(
    window_id: str,
) -> tuple[AgentSession | None, list[AgentSession]]:
    """(the topic's own last conversation, other resumable ones).

    Sessions another binding is tracking are dropped: one container's agents
    share a claude-home, and resuming a conversation the parent (or another
    sibling) currently holds would have two agents writing one transcript.
    """
    state = session_manager.window_states.get(window_id)
    last_id = state.session_id if state else ""
    busy = session_manager.session_ids_of_other_bindings(window_id)
    sessions = [
        s
        for s in await session_manager.list_agent_sessions(window_id)
        if s.session_id not in busy
    ]
    last = next((s for s in sessions if s.session_id == last_id), None)
    return last, [s for s in sessions if s is not last]


def _session_label(session: AgentSession, key: str) -> str:
    """Button label for one resumable conversation (title + age)."""
    from .directory_browser import _relative_time_short

    title = session.summary[:24] + "…" if len(session.summary) > 24 else session.summary
    age = _relative_time_short(session.file_path)
    return tr(key, title=title, age=age) if age else tr(key, title=title, age="")


def build_revive_keyboard(
    last: AgentSession | None, others: list[AgentSession]
) -> InlineKeyboardMarkup:
    """The recovery offer: continue where it stopped, start clean, or choose."""
    rows: list[list[InlineKeyboardButton]] = []
    if last is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    _session_label(last, "revive.btn_continue"),
                    callback_data=f"{CB_DVR_RESUME}{last.session_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("revive.btn_fresh"), callback_data=CB_DVR_FRESH)]
    )
    if others:
        rows.append(
            [InlineKeyboardButton(tr("revive.btn_earlier"), callback_data=CB_DVR_LIST)]
        )
    return InlineKeyboardMarkup(rows)


def build_revive_session_list(others: list[AgentSession]) -> InlineKeyboardMarkup:
    """«Earlier conversations» — a shortlist by recency, newest first."""
    rows = [
        [
            InlineKeyboardButton(
                _session_label(s, "revive.btn_session"),
                callback_data=f"{CB_DVR_RESUME}{s.session_id}",
            )
        ]
        for s in others[:REVIVE_SESSION_ROWS]
    ]
    rows.append(
        [InlineKeyboardButton(tr("revive.btn_back"), callback_data=CB_DVR_BACK)]
    )
    return InlineKeyboardMarkup(rows)


async def offer_docker_revive(
    bot: Bot, chat_id: int, thread_id: int | None, window_id: str
) -> bool:
    """Post the "your agent isn't running — bring it back" offer. True if sent.

    The one place that message is built, because every way the user meets a
    dead container agent leads here: the 👾 panel (whose pane capture fails),
    a message that couldn't be delivered, and the status poll's own notice.
    Without it the panel was a dead end — the 🔄 button it advertised lives
    *inside* the panel it refused to open (operator report 2026-09-13).
    """
    from ..docker_driver import docker_driver

    name = session_manager.get_display_name(window_id)
    target = session_manager.resolve_docker_target(window_id)
    if target is None:
        return False
    if not await docker_driver.is_container_alive(target.agent.container):
        await safe_send(
            bot,
            chat_id,
            tr("revive.container_down", container=target.agent.container),
            message_thread_id=thread_id,
        )
        return True
    last, others = await docker_revive_options(window_id)
    logger.info(
        "Offering revive for %s (thread=%s, resume candidates=%d%s)",
        window_id,
        thread_id,
        len(others),
        ", last session known" if last else "",
    )
    await safe_send(
        bot,
        chat_id,
        tr("revive.offer", name=name),
        message_thread_id=thread_id,
        reply_markup=build_revive_keyboard(last, others),
    )
    return True
