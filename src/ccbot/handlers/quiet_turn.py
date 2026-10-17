"""Quiet turns — a turn the chat's user didn't start shows them only its outcome.

A prompt that opens with :data:`QUIET_TURN_MARKER` (typed into the pane by
something other than the chat's user — e.g. a plugin waking the agent about
mail) starts a quiet turn on that binding. Until it ends, nothing of it is
relayed: the assistant's texts are held, only the LAST one is kept. When the
agent is idle again and the transcript has been still for
:data:`SETTLE_SEC`, that last text goes through the normal delivery path —
unless it is the no-reply marker, in which case the chat gets nothing at all.
The prompt itself is never shown, ``show_user_messages`` or not.

Without this, every intermediate line the agent wrote while handling the
wake ("reading the letter…") reached the user mid-turn; ``(no reply)`` alone
could only silence the final message.

The user writing to the agent mid-turn releases it: what was held so far is
delivered on the next poll tick and the rest of the turn is relayed as usual
— a reply to them must never wait behind a turn they can't see.

Turn end is inferred (idle pane + still transcript): the transcript carries
no reliable end-of-turn mark per entry. A quiet turn older than
:data:`MAX_QUIET_SEC` is flushed regardless, so a stuck pane can't hold a
reply forever.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..session import session_manager
from ..terminal_parser import is_interactive_ui

if TYPE_CHECKING:
    from telegram import Bot

    from ..session_monitor import NewMessage

logger = logging.getLogger(__name__)

QUIET_TURN_MARKER = "(quiet turn)"
SETTLE_SEC = 5.0
MAX_QUIET_SEC = 30 * 60.0


@dataclass
class _Turn:
    started: float
    last_event: float
    last_text: "NewMessage | None" = None
    held: int = field(default=0)


_turns: dict[str, _Turn] = {}
# Released (by the user writing) or finished turns whose last text is due.
_due: list["NewMessage"] = []
_deliver: Callable[["NewMessage", "Bot"], Awaitable[None]] | None = None


def set_deliver(fn: Callable[["NewMessage", "Bot"], Awaitable[None]]) -> None:
    """The normal delivery path (``bot.handle_new_message``), wired by bot.py —
    importing it here would be a cycle."""
    global _deliver
    _deliver = fn


def opens_quiet_turn(msg: "NewMessage") -> bool:
    return msg.role == "user" and msg.text.lstrip().startswith(QUIET_TURN_MARKER)


def is_quiet(wid: str) -> bool:
    return wid in _turns


def start(wid: str) -> None:
    now = time.monotonic()
    if wid not in _turns:
        _turns[wid] = _Turn(started=now, last_event=now)
        logger.info("Quiet turn started on %s", wid)


def hold(wid: str, msg: "NewMessage") -> None:
    """Swallow one event of a quiet turn, keeping the latest assistant text."""
    turn = _turns[wid]
    turn.last_event = time.monotonic()
    turn.held += 1
    if msg.role == "assistant" and msg.content_type == "text":
        turn.last_text = msg


def release(wid: str) -> None:
    """The user wrote to this agent: end its quiet turn now."""
    turn = _turns.pop(wid, None)
    if turn is not None:
        logger.info("Quiet turn on %s released by user input", wid)
        if turn.last_text is not None:
            _due.append(turn.last_text)


async def poll(bot: "Bot") -> None:
    """Status-poll tick: finish settled quiet turns, deliver what's due."""
    now = time.monotonic()
    for wid, turn in list(_turns.items()):
        if now - turn.started < MAX_QUIET_SEC:
            if now - turn.last_event < SETTLE_SEC:
                continue
            pane = await session_manager.capture_pane(wid)
            if session_manager.is_agent_working(wid, pane) or (
                pane and is_interactive_ui(pane)
            ):
                continue
        _turns.pop(wid, None)
        logger.info("Quiet turn on %s ended (%d event(s) held)", wid, turn.held)
        if turn.last_text is not None:
            _due.append(turn.last_text)
    while _due and _deliver is not None:
        await _deliver(_due.pop(0), bot)
