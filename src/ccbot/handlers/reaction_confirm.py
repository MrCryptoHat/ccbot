"""React with 👍 on an agent's message to confirm — no typing needed.

When the owner adds the configured reaction (default 👍) to a message ccbot
posted into a forum topic — a streamed assistant message or an interactive-prompt
screenshot — ccbot treats it as "yes, go ahead":

  - interactive prompt visible in the pane → press Enter (accept the highlighted
    option / "Yes" / "proceed") — same effect as the ⏎ inline button;
  - agent idle, waiting for input → type «да» into the pane (a real submitted
    prompt, same path as a typed message);
  - agent busy → do nothing (a stray reaction must not inject text into a
    running prompt — only a short notice in the topic).

A debounce (``config.reaction_confirm_debounce_sec``, default 2.5 s) makes an
accidental tap recoverable: remove the reaction within the window and nothing
happens.

Telegram only delivers ``message_reaction`` updates when the bot is an
administrator in the chat *and* the type is listed in ``allowed_updates`` (done
in ``main.py``, gated on ``config.reaction_confirm_enabled``).

Resolving *which topic* a reaction belongs to needs a ``(chat_id, message_id) →
(user, thread)`` index, because Telegram's ``MessageReactionUpdated`` carries
only ``chat`` + ``message_id`` — no ``message_thread_id``. The index is a
bounded LRU, populated by the two places that post agent-originated messages
into topics: the queue worker (``message_queue._process_content_task``) and the
interactive-UI handler (``interactive_ui``). A 👍 on anything not in the index
(status messages, command replies, messages older than the LRU window, anything
after a ccbot restart) is silently ignored.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Sequence

from telegram import ReactionType, ReactionTypeEmoji, Update
from telegram.ext import ContextTypes

from ..config import config
from ..i18n import tr
from ..session import session_manager
from ..terminal_parser import is_claude_working, is_interactive_ui
from . import is_user_allowed
from .message_sender import safe_send

logger = logging.getLogger(__name__)

# (chat_id, message_id) -> (user_id, thread_id_or_0). Bounded LRU — a 👍 on a
# message older than this many topic posts simply does nothing.
_MSG_INDEX_MAX = 1000
_msg_index: OrderedDict[tuple[int, int], tuple[int, int]] = OrderedDict()

# (chat_id, message_id) -> the pending confirm task, so a reaction-removal
# within the debounce window can cancel it.
_pending: dict[tuple[int, int], asyncio.Task[None]] = {}


def note_topic_message(
    chat_id: int, message_id: int, user_id: int, thread_id: int | None
) -> None:
    """Record an agent-originated topic message so a 👍 on it can be resolved."""
    if not config.reaction_confirm_enabled:
        return
    key = (chat_id, message_id)
    _msg_index[key] = (user_id, thread_id or 0)
    _msg_index.move_to_end(key)
    while len(_msg_index) > _MSG_INDEX_MAX:
        _msg_index.popitem(last=False)


def _has_confirm_emoji(reactions: Sequence[ReactionType]) -> bool:
    emoji = config.reaction_confirm_emoji
    return any(isinstance(r, ReactionTypeEmoji) and r.emoji == emoji for r in reactions)


def decide_confirm_action(pane_text: str | None) -> str:
    """Pure: given the agent's pane, what does a 👍 mean here?

    Returns ``"enter"`` (press Enter on the interactive prompt), ``"type_yes"``
    (type «да» — agent is idle waiting for input), or ``"skip"`` (agent busy or
    pane unavailable — don't touch it).
    """
    if not pane_text or not pane_text.strip():
        return "skip"
    if is_interactive_ui(pane_text):
        return "enter"
    if not is_claude_working(pane_text):
        return "type_yes"
    return "skip"


async def handle_message_reaction(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """MessageReactionHandler callback: 👍 on a tracked topic message → confirm."""
    if not config.reaction_confirm_enabled:
        return
    mr = update.message_reaction
    if mr is None or mr.user is None or not is_user_allowed(mr.user.id):
        return

    key = (mr.chat.id, mr.message_id)
    target = _msg_index.get(key)
    if target is None:
        return  # not an agent-originated topic message we know about

    had = _has_confirm_emoji(mr.old_reaction)
    has = _has_confirm_emoji(mr.new_reaction)

    if had and not has:
        # Reaction taken back within the debounce window → cancel the confirm.
        task = _pending.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Reaction confirm cancelled (msg %s)", mr.message_id)
        return

    if has and not had:
        user_id, thread_id = target
        old = _pending.pop(key, None)
        if old is not None and not old.done():
            old.cancel()
        _pending[key] = context.application.create_task(
            _confirm_after_delay(context, key, user_id, thread_id),
            update=update,
        )


async def _confirm_after_delay(
    context: ContextTypes.DEFAULT_TYPE,
    key: tuple[int, int],
    user_id: int,
    thread_id: int,
) -> None:
    try:
        await asyncio.sleep(config.reaction_confirm_debounce_sec)
    except asyncio.CancelledError:
        return
    finally:
        if _pending.get(key) is asyncio.current_task():
            _pending.pop(key, None)
    await _do_confirm(context, user_id, thread_id, message_id=key[1])


async def _do_confirm(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    *,
    message_id: int,
) -> None:
    bot = context.bot
    tid = thread_id or None
    binding = session_manager.get_window_for_thread(user_id, thread_id)
    if not binding:
        logger.info("Reaction confirm: thread %s has no binding, skipping", thread_id)
        return
    chat_id = session_manager.resolve_chat_id(user_id, tid)
    pane = await session_manager.capture_pane(binding)
    action = decide_confirm_action(pane)
    logger.info(
        "Reaction confirm: msg=%s binding=%s action=%s", message_id, binding, action
    )

    if action == "enter":
        if await session_manager.send_keys(
            binding, "Enter", enter=False, literal=False
        ):
            await safe_send(bot, chat_id, tr("rconf.confirmed"), message_thread_id=tid)
        return
    if action == "type_yes":
        ok, _ = await session_manager.send_to_window(binding, "да")
        if ok:
            await safe_send(bot, chat_id, tr("rconf.sent_yes"), message_thread_id=tid)
        return
    # skip — agent busy or unavailable; tell the user the tap was seen.
    await safe_send(
        bot,
        chat_id,
        tr("rconf.agent_busy"),
        message_thread_id=tid,
    )
