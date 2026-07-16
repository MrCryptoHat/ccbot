"""Tests for message_sender — the topic-gone error classifier.

``send_with_fallback`` / ``safe_send`` / ``send_photo`` re-raise instead of
swallowing when the destination forum topic was deleted, so the queue worker
can purge the binding (handlers.cleanup.purge_deleted_topic). This pins which
Telegram error strings count as "topic gone".
"""

import pytest
from telegram.error import BadRequest, RetryAfter

from ccbot.handlers.message_sender import is_topic_gone_error


class TestIsTopicGoneError:
    def test_topic_id_invalid(self):
        assert is_topic_gone_error(BadRequest("Topic_id_invalid")) is True

    def test_message_thread_not_found(self):
        assert is_topic_gone_error(BadRequest("Message thread not found")) is True

    def test_case_insensitive(self):
        assert is_topic_gone_error(BadRequest("TOPIC_ID_INVALID")) is True

    def test_other_bad_request_is_not_topic_gone(self):
        assert is_topic_gone_error(BadRequest("message to edit not found")) is False
        assert is_topic_gone_error(BadRequest("can't parse entities")) is False

    def test_non_bad_request_is_not_topic_gone(self):
        # RetryAfter (flood control) and generic exceptions are handled elsewhere.
        assert is_topic_gone_error(RetryAfter(5)) is False
        assert is_topic_gone_error(RuntimeError("Topic_id_invalid")) is False


class TestNetworkErrorNoDuplicate:
    """TimedOut/NetworkError must propagate, NOT trigger the plain-text
    fallback: after a client-side timeout the first send may already be
    committed server-side, so an immediate resend produced duplicates.
    The queue worker retries these with a bounded backoff instead."""

    async def test_send_with_fallback_reraises_network_error(self):
        from unittest.mock import AsyncMock, MagicMock

        from telegram.error import TimedOut

        from ccbot.handlers.message_sender import send_with_fallback

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=TimedOut("boom"))
        with pytest.raises(TimedOut):
            await send_with_fallback(bot, 123, "hello")
        # Exactly one attempt — no plain-text second send.
        assert bot.send_message.await_count == 1

    async def test_bad_request_still_falls_back_to_plain(self):
        from unittest.mock import AsyncMock, MagicMock

        from telegram.error import BadRequest

        from ccbot.handlers.message_sender import send_with_fallback

        bot = MagicMock()
        bot.send_message = AsyncMock(
            side_effect=[BadRequest("can't parse entities"), MagicMock()]
        )
        result = await send_with_fallback(bot, 123, "hello *broken")
        assert result is not None
        assert bot.send_message.await_count == 2


class TestSendRichMessage:
    """Bot API 10.2 rich-first sends: raw _post, message_id on success, None
    (→ legacy fallback) on rejection, RetryAfter / topic-gone re-raised so the
    worker's requeue and purge logic stay uniform."""

    def _bot(self, post):
        from unittest.mock import MagicMock

        bot = MagicMock()
        bot._post = post
        return bot

    @pytest.mark.asyncio
    async def test_success_returns_message_id(self):
        from unittest.mock import AsyncMock

        from ccbot.handlers.message_sender import send_rich_message

        post = AsyncMock(return_value={"message_id": 77})
        mid = await send_rich_message(self._bot(post), -100, "## hi", thread_id=5)
        assert mid == 77
        endpoint, data = post.await_args.args
        assert endpoint == "sendRichMessage"
        assert data["rich_message"] == {"markdown": "## hi"}
        assert data["message_thread_id"] == 5
        assert "disable_notification" not in data  # text replies ring

    @pytest.mark.asyncio
    async def test_bad_request_falls_back_with_none(self):
        from unittest.mock import AsyncMock

        from ccbot.handlers.message_sender import send_rich_message

        post = AsyncMock(side_effect=BadRequest("can't parse blocks"))
        assert await send_rich_message(self._bot(post), -100, "x") is None

    @pytest.mark.asyncio
    async def test_retry_after_propagates(self):
        from unittest.mock import AsyncMock

        from ccbot.handlers.message_sender import send_rich_message

        post = AsyncMock(side_effect=RetryAfter(3))
        with pytest.raises(RetryAfter):
            await send_rich_message(self._bot(post), -100, "x")

    @pytest.mark.asyncio
    async def test_topic_gone_propagates(self):
        from unittest.mock import AsyncMock

        from ccbot.handlers.message_sender import send_rich_message

        post = AsyncMock(side_effect=BadRequest("Topic_id_invalid"))
        with pytest.raises(BadRequest):
            await send_rich_message(self._bot(post), -100, "x")

    @pytest.mark.asyncio
    async def test_oversize_skipped_without_api_call(self):
        from unittest.mock import AsyncMock

        from ccbot.handlers.message_sender import (
            RICH_MESSAGE_MAX_CHARS,
            send_rich_message,
        )

        post = AsyncMock()
        big = "x" * (RICH_MESSAGE_MAX_CHARS + 1)
        assert await send_rich_message(self._bot(post), -100, big) is None
        post.assert_not_awaited()
