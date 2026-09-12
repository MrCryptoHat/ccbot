"""Shared pre-send pipeline for user-originated text (typed or transcribed).

Every path that delivers the user's words to an agent must make the same
checks before send_to_window: route a free-form answer into an open
AskUserQuestion, refuse to type into any other interactive widget (the
TUI eats the characters and Enter activates the highlighted option —
e.g. silently grants a permission), and prepend the once-per-session
voice directive. text_handler and voice_handler both call this; keeping
the sequence in one place is what stops the voice path from drifting
behind the text path again.

``forward_pending_text`` is the same pipeline for the topic's *stashed
first* message — the one typed before the topic had a binding, delivered
right after the bind. It exists because a just-created window is the one
moment the pane is most likely to hold a widget nobody has seen yet.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telegram import Bot

from ..i18n import tr
from ..session import session_manager
from ..terminal_parser import is_interactive_ui
from ..voice import build_on_directive, off_directive
from .ask_question_router import try_route_to_text_option
from .interactive_ui import handle_interactive_ui
from .message_sender import safe_reply, safe_send
from .reaction_emit import arm as arm_reaction_ack

logger = logging.getLogger(__name__)

# How long a deferred first message waits for the startup widget to clear,
# and how often the pane is re-checked. Generous: the user has to read the
# dialog photo, understand it, and tap — and the alternative to waiting is
# dropping a message they already sent.
PENDING_WIDGET_WAIT_SEC = 300.0
PENDING_WIDGET_POLL_SEC = 2.0

# Strong refs to in-flight deferred-forward tasks (asyncio only holds weak
# ones — an unreferenced task can be garbage-collected mid-wait).
_deferred_tasks: set[asyncio.Task[None]] = set()


async def deliver_user_text(
    user_id: int,
    thread_id: int | None,
    wid: str,
    text: str,
    *,
    ack_chat_id: int | None = None,
    ack_message_id: int | None = None,
) -> tuple[str, str]:
    """Route, guard, and send one user message to the bound agent.

    Returns ``(status, detail)``:
      - ``("routed", "")``          — typed into an AskUserQuestion text
                                      option and submitted
      - ``("blocked_no_text_option", "")``
                                    — AskUserQuestion without a free-form
                                      field; caller warns the user
      - ``("blocked_widget", name)`` — another interactive widget is on
                                      screen; caller warns the user, the
                                      message was NOT delivered
      - ``("sent", "")``            — delivered via send_to_window
      - ``("error", message)``      — send failed; detail is user-facing
    """
    routed, route_reason = await try_route_to_text_option(wid, text)
    if routed:
        return "routed", ""
    if route_reason == "no_text_option":
        return "blocked_no_text_option", ""
    if route_reason and route_reason.startswith("blocking_widget:"):
        return "blocked_widget", route_reason.split(":", 1)[1]

    # Voice mode: announce state change to Claude's session once, not per
    # message. Prepend as a system-style directive so it reads as context
    # for the user's actual text that follows.
    directive = session_manager.consume_voice_directive(user_id, thread_id)
    if directive == "on":
        text = f"{build_on_directive()}\n\n---\n{text}"
    elif directive == "off":
        text = f"{off_directive()}\n\n---\n{text}"

    ok, msg = await session_manager.send_to_window(wid, text)
    if ok:
        # Reaction-ack (opt-in via /react): the message reached the agent's pane.
        # Arm a pending 👀 here in the shared seam (so typed and voice behave
        # identically); the status poll fires it once this window's input queue
        # drains — i.e. when the agent actually takes the message into context,
        # not while it's still buffered behind a running turn.
        if (
            ack_chat_id is not None
            and ack_message_id is not None
            and session_manager.is_reaction_ack_enabled()
        ):
            arm_reaction_ack(wid, ack_chat_id, ack_message_id)
        return "sent", ""
    return "error", msg


async def report_delivery_failure(
    bot: Bot,
    message: Any,
    user_id: int,
    thread_id: int | None,
    wid: str,
    detail: str,
) -> None:
    """Answer a send that failed — with the way back when one exists.

    A container agent whose in-container tmux session is gone (a docker
    restart takes every sibling with it) is recoverable without touching the
    topic, so the user gets the revive offer instead of a bare
    «Failed to send keys (docker)» they can do nothing with. Everything else
    reports the error as before.
    """
    from .agent_restart import offer_docker_revive

    if session_manager._is_docker_binding(
        wid
    ) and not await session_manager.docker_agent_running(wid):
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        if await offer_docker_revive(bot, chat_id, thread_id, wid):
            return
    if message is not None:
        await safe_reply(message, f"❌ {detail}")


async def forward_pending_text(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    wid: str,
    text: str,
) -> str:
    """Deliver the topic's stashed first message to a just-bound window.

    Same guard as every other user message (``deliver_user_text``), which
    is the whole point: a freshly launched agent may open on a widget
    instead of the prompt box — Claude Code's resume-cost dialog on
    ``--resume``, the folder-trust prompt, a sign-in screen — and typing
    into one discards the characters while the trailing Enter activates
    the *preselected* option. On the resume dialog that option is «Resume
    from summary», so the blind send used to compact away the very
    context the user had just asked to resume (operator report,
    2026-08-19).

    Blocked ⇒ the widget is surfaced (photo + ↑↓⏎ keyboard) and the text
    is delivered by a background waiter as soon as the user answers it —
    dropping a message the user already sent would just move the surprise.

    Returns the ``deliver_user_text`` status of the FIRST attempt.
    """
    status, detail = await deliver_user_text(user_id, thread_id, wid, text)
    if status in ("sent", "routed"):
        return status

    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    if status == "error":
        logger.warning("Pending forward failed (wid=%s): %s", wid, detail)
        await safe_send(
            bot,
            chat_id,
            tr("bot.pending_send_failed", err=detail),
            message_thread_id=thread_id,
        )
        return status

    logger.info(
        "Pending forward held back — %s on screen (wid=%s, thread=%s)",
        detail or "widget",
        wid,
        thread_id,
    )
    await handle_interactive_ui(bot, user_id, wid, thread_id)
    await safe_send(
        bot,
        chat_id,
        tr("bot.pending_deferred"),
        message_thread_id=thread_id,
    )
    task = asyncio.create_task(
        _deliver_when_pane_free(bot, user_id, thread_id, wid, text)
    )
    _deferred_tasks.add(task)
    task.add_done_callback(_deferred_tasks.discard)
    return status


async def _deliver_when_pane_free(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    wid: str,
    text: str,
) -> None:
    """Wait out the startup widget, then deliver ``text`` through the pipeline.

    Polls the pane itself rather than riding the 1 s status loop: this is a
    one-off per bind, and keeping it here means the whole "first message vs.
    startup dialog" story lives in one file.
    """
    deadline = time.monotonic() + PENDING_WIDGET_WAIT_SEC
    while time.monotonic() < deadline:
        await asyncio.sleep(PENDING_WIDGET_POLL_SEC)
        # The topic can be rebound while we wait (unbind, /bind <agent>, a
        # re-created window). Delivering to the remembered wid would then put
        # the user's message in front of a DIFFERENT agent — drop it instead.
        binding = session_manager.resolve_binding(user_id, thread_id)
        if binding is None or binding[1] != wid:
            logger.info("Deferred forward dropped — topic rebound (wid=%s)", wid)
            return
        pane = await session_manager.capture_pane(wid)
        if pane is None:
            logger.info("Deferred forward dropped — no pane (wid=%s)", wid)
            return
        if is_interactive_ui(pane):
            continue
        status, detail = await deliver_user_text(user_id, thread_id, wid, text)
        if status in ("sent", "routed"):
            logger.info("Deferred forward delivered after dialog (wid=%s)", wid)
            return
        if status == "error":
            logger.warning("Deferred forward failed (wid=%s): %s", wid, detail)
            await safe_send(
                bot,
                session_manager.resolve_chat_id(user_id, thread_id),
                tr("bot.pending_send_failed", err=detail),
                message_thread_id=thread_id,
            )
            return
        # A new widget appeared between the capture and the send — keep waiting.

    logger.info("Deferred forward timed out behind a widget (wid=%s)", wid)
    await safe_send(
        bot,
        session_manager.resolve_chat_id(user_id, thread_id),
        tr("bot.pending_undelivered"),
        message_thread_id=thread_id,
    )
