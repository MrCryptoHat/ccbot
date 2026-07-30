"""🗑 Delete agent + topic, for any bound topic (the non-worktree half).

Worktree topics keep their own 🗑 (`handlers/worktrees`) because deleting one
can destroy unmerged git work and needs the dirty/unmerged guard. Everything
else — a plain tmux topic, a sibling agent, a container's own agent — is torn
down here: one red confirm, then the same teardown a hard-deleted topic gets
(`cleanup.purge_deleted_topic`), followed by deleting the topic itself.

What "delete" means per kind is spelled out in the confirm text, because it
differs where it matters:
  - tmux topic  → its window (and the agent in it) is killed;
  - sibling     → its in-container session is killed;
  - docker agent → the container's own agent keeps running (its lifecycle is
    the container's, not the topic's) — only the topic and the binding go.
"""

from __future__ import annotations

import logging

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.constants import KeyboardButtonStyle
from telegram.ext import ContextTypes

from ..i18n import tr
from ..session import session_manager
from . import get_thread_id
from .callback_data import CB_AGENT_DEL, CB_AGENT_DELNO, CB_AGENT_DELOK
from .cleanup import purge_deleted_topic

logger = logging.getLogger(__name__)

# user_data slot holding what the confirm caption actually described:
# (thread_id, binding). The ✅ payload can only carry the thread — this is what
# makes the second tap verify it is still killing the agent the user was shown.
_TARGET_KEY = "_agentdel_target"


def _confirm_caption(wid: str, display: str) -> str:
    """Confirm copy for this binding's kind (what exactly gets killed)."""
    if session_manager.is_docker_sub_agent(wid):
        return tr("agentdel.confirm_sibling", name=display)
    if session_manager._is_docker_binding(wid):
        return tr("agentdel.confirm_docker", name=display)
    return tr("agentdel.confirm_tmux", name=display)


async def _handle_agent_del(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """🗑 Удалить агента — show the red confirm on the panel."""
    thread_id = get_thread_id(update)
    if thread_id is None:
        await query.answer(tr("wt.not_in_topic"), show_alert=True)
        return
    # The topic's CURRENT binding, not the payload: callback data carries only
    # its first CALLBACK_WID_MAX chars, and the ✅ step compares this value
    # against the full binding — a truncated one would refuse forever.
    wid = (
        session_manager.get_window_for_thread(user.id, thread_id)
        or data[len(CB_AGENT_DEL) :]
    )
    display = session_manager.get_display_name(wid)
    if context.user_data is not None:
        context.user_data[_TARGET_KEY] = (thread_id, wid)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("agentdel.ok"),
                    callback_data=f"{CB_AGENT_DELOK}{thread_id}"[:64],
                    style=KeyboardButtonStyle.DANGER,
                )
            ],
            [
                InlineKeyboardButton(
                    tr("wt.cancel"), callback_data=f"{CB_AGENT_DELNO}{wid}"[:64]
                )
            ],
        ]
    )
    try:
        await query.edit_message_caption(
            caption=_confirm_caption(wid, display), reply_markup=keyboard
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("agent delete confirm edit failed: %s", e)
    await query.answer()


async def _handle_agent_delok(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Confirmed 🗑 — tear the agent down and delete the topic."""
    thread_id = int(data[len(CB_AGENT_DELOK) :])
    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await query.answer(tr("wt.agent_gone"), show_alert=True)
        return
    # The confirm caption named a specific agent; this payload only carries the
    # topic. A topic can rebind between the two taps (tmux-server restart
    # re-maps @N, an unbind/rebind, or simply an old panel scrolled back to
    # weeks later), and destroying whatever is bound NOW instead of what the
    # caption described is exactly the kind of surprise the guard exists for.
    intended = (context.user_data or {}).pop(_TARGET_KEY, None)
    if intended != (thread_id, wid):
        await query.answer(tr("cb.stale_panel"), show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:  # noqa: BLE001
            logger.debug("agent delete stale clear failed: %s", e)
        return
    display = session_manager.get_display_name(wid)
    await query.answer(tr("wt.deleting"))
    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    # Same teardown a hard-deleted topic gets (kills the window / sibling
    # session, unbinds, clears per-topic state), then the topic itself.
    await purge_deleted_topic(context.bot, user.id, thread_id, wid)
    try:
        await context.bot.delete_forum_topic(
            chat_id=chat_id, message_thread_id=thread_id
        )
    except Exception as e:  # noqa: BLE001
        # The agent is already down but the topic stayed (typically the bot
        # lacks «Delete messages»). Say so in the topic that is still there —
        # otherwise the next message in it silently opens the bind flow.
        logger.warning("delete_forum_topic on agent delete failed: %s", e)
        try:
            await query.edit_message_caption(
                caption=tr("agentdel.err_topic", name=display)
            )
        except Exception:  # noqa: BLE001
            pass
        return
    logger.info(
        "Agent %s deleted with its topic (user=%d, thread=%d)", wid, user.id, thread_id
    )


async def _handle_agent_delno(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """↩ Отмена — restore the agent panel caption + keyboard."""
    from telegram.helpers import escape_markdown

    from .commands import _build_commands_keyboard

    wid = data[len(CB_AGENT_DELNO) :]
    display = session_manager.get_display_name(wid)
    try:
        await query.edit_message_caption(
            caption=tr(
                "wt.panel_agent_header", name=escape_markdown(display, version=2)
            ),
            parse_mode="MarkdownV2",
            reply_markup=_build_commands_keyboard(wid, tab="ses"),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("agent delete cancel restore failed: %s", e)
    await query.answer(tr("wt.cancelled"))
