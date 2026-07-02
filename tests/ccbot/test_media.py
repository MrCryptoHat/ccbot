"""Tests for media inbound-filename sanitization.

Telegram's ``document.file_name`` is client-supplied — a malicious
client could set it to ``"../../.credentials.json"``. For docker
bindings the saved path lives under the agent's bind-mounted
workspace, so a traversal would land on host paths the container
could otherwise never touch.
"""

from __future__ import annotations

from ccbot.handlers.media import _safe_filename_component


class TestSafeFilenameComponent:
    def test_plain_name_passes(self) -> None:
        assert _safe_filename_component("report.pdf") == "report.pdf"

    def test_strips_unix_traversal(self) -> None:
        # Basename collapse should leave nothing usable.
        assert _safe_filename_component("../../.credentials.json") == ""

    def test_strips_windows_traversal(self) -> None:
        # Basename "secrets.txt" itself isn't malicious — the leading
        # ``..\\..\\`` just gets collapsed away. What matters is that
        # nothing escapes the inbox dir.
        assert _safe_filename_component("..\\..\\secrets.txt") == "secrets.txt"

    def test_strips_absolute_path(self) -> None:
        # Basename of "/etc/passwd" is "passwd" — no traversal, usable.
        assert _safe_filename_component("/etc/passwd") == "passwd"

    def test_rejects_dot_only(self) -> None:
        assert _safe_filename_component(".") == ""
        assert _safe_filename_component("..") == ""

    def test_rejects_leading_dot(self) -> None:
        # Keep the inbox dir free of hidden files that would slip past
        # casual listings.
        assert _safe_filename_component(".env") == ""
        assert _safe_filename_component(".credentials.json") == ""

    def test_rejects_empty(self) -> None:
        assert _safe_filename_component("") == ""

    def test_keeps_spaces_and_unicode(self) -> None:
        # These are legitimate user file names; no reason to reject.
        assert _safe_filename_component("мой отчёт.pdf") == "мой отчёт.pdf"

    def test_mixed_separator_then_basename(self) -> None:
        assert _safe_filename_component("a/b\\c.txt") == "c.txt"


class TestMediaWidgetGuard:
    """Photo/document markers must go through deliver_user_text: a raw
    send_to_window with an interactive widget open types the marker into
    the widget and presses Enter — on a permission prompt that GRANTS it."""

    async def test_blocked_widget_stops_delivery_and_tells_user(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from ccbot.handlers.media import _deliver_media_text

        update = MagicMock()
        update.message.chat.id = -100123
        update.message.message_id = 7

        with (
            patch(
                "ccbot.handlers.media.deliver_user_text",
                new=AsyncMock(return_value=("blocked_widget", "PermissionPrompt")),
            ),
            patch("ccbot.handlers.media.safe_reply", new=AsyncMock()) as reply,
        ):
            status, _ = await _deliver_media_text(
                update, 1, 42, "@5", "(image attached: /x.jpg)"
            )
        assert status == "blocked"
        reply.assert_awaited_once()

    async def test_sent_and_routed_count_as_ok(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from ccbot.handlers.media import _deliver_media_text

        for outcome in ("sent", "routed"):
            update = MagicMock()
            with patch(
                "ccbot.handlers.media.deliver_user_text",
                new=AsyncMock(return_value=(outcome, "")),
            ):
                status, _ = await _deliver_media_text(update, 1, 42, "@5", "x")
            assert status == "ok"
