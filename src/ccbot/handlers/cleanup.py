"""Unified cleanup API for topic state.

Provides centralized cleanup functions that coordinate state cleanup across
all modules, preventing memory leaks when topics are deleted.

Functions:
  - clear_topic_state: Clean up all memory state for a specific topic
  - purge_deleted_topic: Full teardown when Telegram reports a topic gone
    (kill tmux window + unbind thread + clear_topic_state)
"""

import logging
from typing import Any

from telegram import Bot

from ..session import session_manager
from ..tmux_manager import tmux_manager
from .interactive_ui import clear_interactive_msg
from .message_queue import clear_tool_msg_ids_for_topic

logger = logging.getLogger(__name__)


async def clear_topic_state(
    user_id: int,
    thread_id: int,
    bot: Bot | None = None,
    user_data: dict[str, Any] | None = None,
) -> None:
    """Clear all memory state associated with a topic.

    This should be called when:
      - A topic is closed or deleted
      - A thread binding becomes stale (window deleted externally)

    Cleans up:
      - _tool_msg_ids (tool_use → message_id mapping)
      - _interactive_msgs and _interactive_mode (interactive UI state)
      - user_data pending state (_pending_thread_id, _pending_thread_text)
    """
    # Clear status message tracking

    # Clear tool message ID tracking
    clear_tool_msg_ids_for_topic(user_id, thread_id)

    # Clear interactive UI state (also deletes message from chat)
    await clear_interactive_msg(user_id, bot, thread_id)

    # Clear voice mode and menu-keyboard flag for this topic
    key = f"{user_id}:{thread_id}"
    session_manager.voice_mode_topics.discard(key)
    session_manager.menu_shown_topics.discard(key)

    # Clear pending thread state from user_data
    if user_data is not None:
        if user_data.get("_pending_thread_id") == thread_id:
            user_data.pop("_pending_thread_id", None)
            user_data.pop("_pending_thread_text", None)


async def purge_deleted_topic(bot: Bot, user_id: int, thread_id: int, wid: str) -> None:
    """Tear down everything tied to a topic Telegram reports gone.

    Triggered two ways: the periodic topic-existence probe (backstop, for idle
    topics) and — the primary path — a ``Topic_id_invalid`` from an actual send
    in the message-queue worker. Kills the bound tmux window (docker bindings
    have no window — skipped), unbinds the thread, and clears per-topic memory.
    Idempotent: safe to call again if a few already-queued sends keep bouncing
    before the worker's queue drains.

    Worktree topics: this is the HEADLESS path (a hard-deleted topic — no close
    event, no UI to show the delete guard). A clean+merged worktree is torn down
    in full here so "delete the topic" cleans up like "close" does; a dirty or
    unmerged worktree is preserved (flagged orphaned, left on disk for the
    phase-3 GC) and falls through to the standard window/binding teardown.
    """
    meta = session_manager.get_worktree_meta(user_id, thread_id)
    if meta is not None:
        from .worktrees import handle_deleted_worktree_topic

        if await handle_deleted_worktree_topic(bot, user_id, thread_id, meta):
            return  # clean worktree → already fully torn down
    if not session_manager._is_docker_binding(wid):
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
    session_manager.unbind_thread(user_id, thread_id)
    await clear_topic_state(user_id, thread_id, bot)
    logger.info(
        "Topic gone — unbound thread %d, cleaned up (window_id=%s) for user %d",
        thread_id,
        wid,
        user_id,
    )
