"""Tests for message_sender — the topic-gone error classifier.

``send_with_fallback`` / ``safe_send`` / ``send_photo`` re-raise instead of
swallowing when the destination forum topic was deleted, so the queue worker
can purge the binding (handlers.cleanup.purge_deleted_topic). This pins which
Telegram error strings count as "topic gone".
"""

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
