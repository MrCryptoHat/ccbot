"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.status_polling import update_status_message


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test.

    Also resets the 3-second "already sent recently" dedup timestamp,
    otherwise a test that triggers handle_interactive_ui can make the
    next test's call return True without actually rendering anything.
    """
    from ccbot.handlers.interactive_ui import (
        _interactive_last_sent,
        _interactive_mode,
        _interactive_msgs,
        _interactive_widget_name,
    )

    for d in (
        _interactive_mode,
        _interactive_msgs,
        _interactive_last_sent,
        _interactive_widget_name,
    ):
        d.clear()
    yield
    for d in (
        _interactive_mode,
        _interactive_msgs,
        _interactive_last_sent,
        _interactive_widget_name,
    ):
        d.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_keyboard_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures Settings pane → handle_interactive_ui sends keyboard."""
        window_id = "@5"

        with (
            patch(
                "ccbot.handlers.status_polling.session_manager.capture_pane",
                new=AsyncMock(return_value=sample_pane_settings),
            ),
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch(
                "ccbot.handlers.status_polling.session_manager.capture_pane",
                new=AsyncMock(return_value=normal_pane),
            ),
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_login_success_screen_keeps_keyboard_and_repaints(
        self, mock_bot: AsyncMock
    ):
        """After the sign-in code is accepted, Claude Code swaps the URL screen
        for "Login successful. Press Enter to continue…" with no keypress from
        us. The poll must keep the interactive photo (that Enter is the last
        step of the login) and repaint it, not tear it down."""
        from ccbot.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_widget_name,
        )

        window_id = "@5"
        _interactive_mode[(1, 42)] = window_id
        _interactive_widget_name[(1, 42)] = "LoginPrompt"
        pane = (
            "  Login\n"
            "\n"
            "  Logged in as user@example.com\n"
            "  Login successful. Press Enter to continue…\n"
        )

        with (
            patch(
                "ccbot.handlers.status_polling.session_manager.capture_pane",
                new=AsyncMock(return_value=pane),
            ),
            patch(
                "ccbot.handlers.status_polling.clear_interactive_msg",
                new_callable=AsyncMock,
            ) as mock_clear,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_clear.assert_not_called()
            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_same_widget_is_not_repainted_every_tick(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """A widget that merely redraws (cursor moves, spinner) must be left
        alone — repainting per tick would be a photo upload per second."""
        from ccbot.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_widget_name,
        )

        window_id = "@5"
        _interactive_mode[(1, 42)] = window_id
        _interactive_widget_name[(1, 42)] = "Settings"

        with (
            patch(
                "ccbot.handlers.status_polling.session_manager.capture_pane",
                new=AsyncMock(return_value=sample_pane_settings),
            ),
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_telegram_photo(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → is_interactive_ui → handle_interactive_ui
        → bot.send_photo with the compact nav keyboard attached.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        The text-based send_message path was replaced by a photo + nav-key
        keyboard — the pane screenshot carries all context so Claude Code's
        UI changes don't break us at the prompt-parsing layer.
        """
        window_id = "@5"
        # Both update_status_message and handle_interactive_ui capture the
        # pane via the shared session_manager singleton (it branches by
        # binding type), so patch the methods on that one instance — that's
        # what both modules' `session_manager` name resolves to.
        from ccbot.session import session_manager

        with (
            patch.object(
                session_manager,
                "capture_pane",
                AsyncMock(return_value=sample_pane_settings),
            ),
            patch.object(session_manager, "is_generating", return_value=False),
            patch.object(session_manager, "mark_idle"),
            patch.object(session_manager, "resolve_chat_id", return_value=100),
            patch(
                "ccbot.handlers.interactive_ui.text_to_image",
                return_value=b"fake-png",
            ),
        ):
            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # Photo + keyboard, never plain text.
            mock_bot.send_photo.assert_called_once()
            mock_bot.send_message.assert_not_called()
            call_kwargs = mock_bot.send_photo.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            assert call_kwargs["reply_markup"] is not None
