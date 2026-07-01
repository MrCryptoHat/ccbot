"""Bot-emitted 👀 reaction acking that a user message entered the agent's context.

On by default (``CCBOT_REACTION_ACK``); global, toggled at runtime via ``/react``
(``session_manager.reaction_ack_enabled``, persisted).
Two steps so the mark means "the agent took your message into context", not
merely "Telegram/ccbot delivered it to the pane":

  1. ``arm`` — called from the shared ``deliver_user_text`` seam the instant a
     user message is typed into the agent's pane (covers typed + dictated voice).
  2. ``maybe_fire`` — called every status-poll tick with the live "are there
     queued (buffered) messages?" signal for that window. The 👀 fires the moment
     the window's input queue is empty — i.e. the message was taken up by the
     agent, not left waiting behind a running turn.

If sent to a busy agent the message sits in Claude Code's input queue ("Press up
to edit queued messages"); the ack waits until that drains. If sent to an idle
agent there is no queue, so it fires on the next poll (~1s).

The reaction is silent for the user only if Telegram reaction-notifications are
muted client-side (the bot cannot suppress the push itself). All failures are
swallowed — a missed ack must never disturb delivery or the poll loop.
"""

from __future__ import annotations

import logging
import time

from telegram import Bot, ReactionTypeEmoji

logger = logging.getLogger(__name__)

ACK_EMOJI = "👀"
# Safety net: drop a pending ack that never cleared (e.g. window rebound). A
# message can legitimately sit queued behind a very long turn, so keep it large.
_TTL_SEC = 3600.0


class _Pending:
    __slots__ = ("chat_id", "message_id", "ts")

    def __init__(self, chat_id: int, message_id: int) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.ts = time.monotonic()


# window_id -> the one pending ack for that window (latest message wins).
_pending: dict[str, _Pending] = {}


def arm(window_id: str, chat_id: int, message_id: int) -> None:
    """Record a message to 👀 once its window's input queue drains."""
    _pending[window_id] = _Pending(chat_id, message_id)


def forget(window_id: str) -> None:
    """Drop any pending ack for a window (e.g. on unbind / kill)."""
    _pending.pop(window_id, None)


async def maybe_fire(bot: Bot, window_id: str, *, has_queue: bool) -> None:
    """Fire the pending 👀 once the window has no queued (buffered) input."""
    pending = _pending.get(window_id)
    if pending is None:
        return
    if time.monotonic() - pending.ts > _TTL_SEC:
        _pending.pop(window_id, None)
        return
    if has_queue:
        # Message still buffered behind a running turn — not in context yet.
        return
    # Queue drained → the agent has taken our message into context.
    _pending.pop(window_id, None)
    try:
        await bot.set_message_reaction(
            chat_id=pending.chat_id,
            message_id=pending.message_id,
            reaction=[ReactionTypeEmoji(emoji=ACK_EMOJI)],
        )
    except Exception as e:  # noqa: BLE001 — best-effort; never disturb the poll
        logger.debug("reaction-ack set failed (wid=%s): %s", window_id, e)
