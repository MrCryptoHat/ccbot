"""Task-pin mode (/pin): auto-pin user messages that start a new task.

ON by default in every topic (``config.pin_tasks_default``; /pin opts a
topic out): a user message is pinned in its topic iff it reads as a NEW
TASK — at least ``config.pin_tasks_min_chars`` characters AND delivered to
an *idle* agent (no running turn, no queued input). Short "да, делай"
nudges and mid-turn clarifications never pin, so the topic's pinned list
stays a scannable task history instead of a second transcript.

The idle check MUST run against the pane captured BEFORE the send — right
after ``send_to_window`` the agent starts chewing on this very message and
``is_claude_working`` flips True. Callers therefore ask ``should_pin_task``
first and pin only after the delivery reports "sent" ("routed" = an answer
into a widget, not a task).

Every ``pin_chat_message`` spawns a service line («closed the pin bar» has
no API; the line is real chat noise at one-per-task rates) —
``pinned_service_message_handler`` deletes the ones this bot created.
Needs the *Pin messages* + *Delete messages* admin rights; both failure
modes degrade to a WARNING, never a crash.
"""

from __future__ import annotations

import logging

from telegram import Bot, Update
from telegram.ext import ContextTypes

from ..config import config
from ..session import session_manager
from ..terminal_parser import has_queued_messages, is_claude_working

logger = logging.getLogger(__name__)


def is_task_text(text: str) -> bool:
    """Length gate: does this text qualify as a pin-worthy task?

    ``!``-prefixed input is a shell command typed into the TUI, not a task,
    however long it is.
    """
    stripped = text.strip()
    if stripped.startswith("!"):
        return False
    return len(stripped) >= config.pin_tasks_min_chars


async def should_pin_task(
    user_id: int,
    thread_id: int | None,
    wid: str,
    text: str,
    *,
    pane_text: str | None = None,
) -> bool:
    """Decide BEFORE the send whether this message should pin after it.

    Cheap gates first (mode toggle, length); the pane capture — the
    expensive part — only runs when those pass and no pre-send capture was
    handed in. A missing/empty pane reads as "can't prove idle" → no pin.
    """
    if thread_id is None:
        return False
    if not session_manager.is_pin_mode(user_id, thread_id):
        return False
    if not is_task_text(text):
        return False
    if pane_text is None:
        pane_text = await session_manager.capture_pane(wid)
    if not pane_text:
        return False
    return not (is_claude_working(pane_text) or has_queued_messages(pane_text))


async def pin_task_message(bot: Bot, chat_id: int, message_id: int) -> None:
    """Pin one message, silently; failure is logged, never raised."""
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )
    except Exception as e:
        logger.warning(
            "Failed to pin task message (chat=%s, msg=%s): %s — "
            "does the bot have the 'Pin messages' admin right?",
            chat_id,
            message_id,
            e,
        )


async def pinned_service_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the «pinned a message» service line for pins this bot made.

    Only the bot's own pins are cleaned up — a pin the user made by hand
    keeps its service message untouched.
    """
    msg = update.effective_message
    if msg is None or msg.pinned_message is None:
        return
    if msg.from_user is None or msg.from_user.id != context.bot.id:
        return
    try:
        await msg.delete()
    except Exception as e:
        logger.warning(
            "Failed to delete pin service message (chat=%s, msg=%s): %s — "
            "does the bot have the 'Delete messages' admin right?",
            msg.chat_id,
            msg.message_id,
            e,
        )
