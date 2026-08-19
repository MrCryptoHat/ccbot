"""Tests for the shared pre-send pipeline (deliver_user_text).

Pins the guard added for non-AskUserQuestion interactive widgets: plain
text typed into PermissionPrompt/ExitPlanMode is discarded by the TUI
while the trailing Enter activates the highlighted option — i.e. a
permission granted without the user seeing it. The pipeline must block
the send and report which widget is up.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.delivery import (
    _deliver_when_pane_free,
    deliver_user_text,
    forward_pending_text,
)


class TestDeliverUserText:
    @pytest.mark.asyncio
    async def test_blocked_widget_does_not_send(self):
        with (
            patch(
                "ccbot.handlers.delivery.try_route_to_text_option",
                new=AsyncMock(return_value=(False, "blocking_widget:PermissionPrompt")),
            ),
            patch("ccbot.handlers.delivery.session_manager") as sm,
        ):
            sm.send_to_window = AsyncMock()
            status, detail = await deliver_user_text(1, 42, "@5", "да, давай")
            assert (status, detail) == ("blocked_widget", "PermissionPrompt")
            sm.send_to_window.assert_not_called()
            sm.consume_voice_directive.assert_not_called()

    @pytest.mark.asyncio
    async def test_routed_into_ask_question(self):
        with (
            patch(
                "ccbot.handlers.delivery.try_route_to_text_option",
                new=AsyncMock(return_value=(True, None)),
            ),
            patch("ccbot.handlers.delivery.session_manager") as sm,
        ):
            sm.send_to_window = AsyncMock()
            status, _ = await deliver_user_text(1, 42, "@5", "свой вариант")
            assert status == "routed"
            sm.send_to_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_plain_send_with_voice_directive(self):
        with (
            patch(
                "ccbot.handlers.delivery.try_route_to_text_option",
                new=AsyncMock(return_value=(False, None)),
            ),
            patch("ccbot.handlers.delivery.session_manager") as sm,
            patch(
                "ccbot.handlers.delivery.build_on_directive",
                return_value="[VOICE ON]",
            ),
        ):
            sm.consume_voice_directive = MagicMock(return_value="on")
            sm.send_to_window = AsyncMock(return_value=(True, "Sent"))
            status, _ = await deliver_user_text(1, 42, "@5", "привет")
            assert status == "sent"
            sent_text = sm.send_to_window.await_args.args[1]
            assert sent_text.startswith("[VOICE ON]")
            assert sent_text.endswith("привет")

    @pytest.mark.asyncio
    async def test_send_failure_reported(self):
        with (
            patch(
                "ccbot.handlers.delivery.try_route_to_text_option",
                new=AsyncMock(return_value=(False, None)),
            ),
            patch("ccbot.handlers.delivery.session_manager") as sm,
        ):
            sm.consume_voice_directive = MagicMock(return_value=None)
            sm.send_to_window = AsyncMock(return_value=(False, "Window not found"))
            status, detail = await deliver_user_text(1, 42, "@5", "привет")
            assert (status, detail) == ("error", "Window not found")


class TestForwardPendingText:
    """The topic's stashed first message goes through the same guard as every
    other message. It didn't use to: the bare send_to_window typed it — plus
    Enter — into whatever the freshly launched agent had on screen, and on a
    resumed session that screen is Claude Code's resume-cost dialog, whose
    preselected option compacts the conversation away."""

    @pytest.mark.asyncio
    async def test_clean_pane_delivers_immediately(self):
        bot = MagicMock()
        with (
            patch(
                "ccbot.handlers.delivery.deliver_user_text",
                new=AsyncMock(return_value=("sent", "")),
            ) as deliver,
            patch(
                "ccbot.handlers.delivery.handle_interactive_ui", new=AsyncMock()
            ) as surface,
            patch("ccbot.handlers.delivery.asyncio.create_task") as spawn,
        ):
            status = await forward_pending_text(bot, 1, 42, "@5", "привет")
            assert status == "sent"
            deliver.assert_awaited_once()
            surface.assert_not_awaited()
            spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_widget_on_screen_holds_the_message_back(self):
        bot = MagicMock()
        with (
            patch(
                "ccbot.handlers.delivery.deliver_user_text",
                new=AsyncMock(return_value=("blocked_widget", "ResumePrompt")),
            ),
            patch(
                "ccbot.handlers.delivery.handle_interactive_ui", new=AsyncMock()
            ) as surface,
            patch("ccbot.handlers.delivery.safe_send", new=AsyncMock()) as notify,
            patch("ccbot.handlers.delivery.session_manager") as sm,
            patch("ccbot.handlers.delivery.asyncio.create_task") as spawn,
        ):
            sm.resolve_chat_id = MagicMock(return_value=-100)
            status = await forward_pending_text(bot, 1, 42, "@5", "привет")
            assert status == "blocked_widget"
            # Nothing typed, dialog surfaced, user told, delivery deferred.
            sm.send_to_window.assert_not_called()
            surface.assert_awaited_once()
            notify.assert_awaited_once()
            spawn.assert_called_once()
            # The coroutine handed to create_task is never awaited here.
            spawn.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_deferred_waiter_sends_once_the_dialog_clears(self):
        panes = ["  ❯ 1. Resume from summary\n", "  ❯ \n"]
        with (
            patch("ccbot.handlers.delivery.session_manager") as sm,
            patch(
                "ccbot.handlers.delivery.is_interactive_ui",
                side_effect=[True, False],
            ),
            patch(
                "ccbot.handlers.delivery.deliver_user_text",
                new=AsyncMock(return_value=("sent", "")),
            ) as deliver,
            patch("ccbot.handlers.delivery.PENDING_WIDGET_POLL_SEC", 0),
        ):
            sm.resolve_binding = MagicMock(return_value=("tmux", "@5"))
            sm.capture_pane = AsyncMock(side_effect=panes)
            await _deliver_when_pane_free(MagicMock(), 1, 42, "@5", "привет")
            assert sm.capture_pane.await_count == 2
            deliver.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deferred_waiter_gives_up_and_says_so(self):
        with (
            patch("ccbot.handlers.delivery.session_manager") as sm,
            patch("ccbot.handlers.delivery.is_interactive_ui", return_value=True),
            patch(
                "ccbot.handlers.delivery.deliver_user_text", new=AsyncMock()
            ) as deliver,
            patch("ccbot.handlers.delivery.safe_send", new=AsyncMock()) as notify,
            patch("ccbot.handlers.delivery.PENDING_WIDGET_POLL_SEC", 0),
            patch("ccbot.handlers.delivery.PENDING_WIDGET_WAIT_SEC", 0.05),
        ):
            sm.resolve_binding = MagicMock(return_value=("tmux", "@5"))
            sm.capture_pane = AsyncMock(return_value="  ❯ 1. Resume from summary\n")
            sm.resolve_chat_id = MagicMock(return_value=-100)
            await _deliver_when_pane_free(MagicMock(), 1, 42, "@5", "привет")
            deliver.assert_not_awaited()
            notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deferred_waiter_drops_when_the_window_is_gone(self):
        with (
            patch("ccbot.handlers.delivery.session_manager") as sm,
            patch(
                "ccbot.handlers.delivery.deliver_user_text", new=AsyncMock()
            ) as deliver,
            patch("ccbot.handlers.delivery.PENDING_WIDGET_POLL_SEC", 0),
        ):
            sm.resolve_binding = MagicMock(return_value=("tmux", "@5"))
            sm.capture_pane = AsyncMock(return_value=None)
            await _deliver_when_pane_free(MagicMock(), 1, 42, "@5", "привет")
            deliver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deferred_waiter_drops_when_the_topic_was_rebound(self):
        """Five minutes is long enough for the topic to point somewhere else.
        Delivering to the remembered window would hand the user's message to a
        different agent."""
        with (
            patch("ccbot.handlers.delivery.session_manager") as sm,
            patch("ccbot.handlers.delivery.is_interactive_ui", return_value=False),
            patch(
                "ccbot.handlers.delivery.deliver_user_text", new=AsyncMock()
            ) as deliver,
            patch("ccbot.handlers.delivery.PENDING_WIDGET_POLL_SEC", 0),
        ):
            sm.resolve_binding = MagicMock(return_value=("docker", "assistant"))
            sm.capture_pane = AsyncMock(return_value="  ❯ \n")
            await _deliver_when_pane_free(MagicMock(), 1, 42, "@5", "привет")
            deliver.assert_not_awaited()
            sm.capture_pane.assert_not_awaited()
