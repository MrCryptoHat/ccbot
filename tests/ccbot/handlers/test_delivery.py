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

from ccbot.handlers.delivery import deliver_user_text


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
