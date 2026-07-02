"""Shared pre-send pipeline for user-originated text (typed or transcribed).

Every path that delivers the user's words to an agent must make the same
checks before send_to_window: route a free-form answer into an open
AskUserQuestion, refuse to type into any other interactive widget (the
TUI eats the characters and Enter activates the highlighted option —
e.g. silently grants a permission), and prepend the once-per-session
voice directive. text_handler and voice_handler both call this; keeping
the sequence in one place is what stops the voice path from drifting
behind the text path again.
"""

from __future__ import annotations

import logging

from ..session import session_manager
from ..voice import build_on_directive, off_directive
from .ask_question_router import try_route_to_text_option
from .reaction_emit import arm as arm_reaction_ack

logger = logging.getLogger(__name__)


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
