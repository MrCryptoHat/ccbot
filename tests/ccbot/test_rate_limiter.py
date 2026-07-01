"""Tests for CcbotRateLimiter: traffic-class routing (stream / interactive / background).

* stream  → through the parent's group bucket keyed on the *real* chat_id (the
  supergroup) — the per-chat governor; all stream traffic, incl. status edits.
* interactive (default) → skips every bucket (user is waiting).
* background → one shared slow lane, then a direct API call (no buckets, no
  PTB retry loop, so a 429 here doesn't arm the global RetryAfter pause).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from ccbot.rate_limiter import CcbotRateLimiter, background_context, stream_context


def _limiter() -> CcbotRateLimiter:
    """Per-test limiter — 1 token/60 s for the group bucket so saturation is
    instantaneous; no overall cap."""
    return CcbotRateLimiter(
        overall_max_rate=0,
        group_max_rate=1,
        group_time_period=60,
    )


@pytest.mark.asyncio
async def test_interactive_send_skips_every_bucket():
    """Default path (no context): a send registers no limiter bucket at all."""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})

    await limiter.process_request(
        callback=callback,
        args=(),
        kwargs={},
        endpoint="sendMessage",
        data={"chat_id": -100111, "message_thread_id": 7},
        rate_limit_args=None,
    )

    assert not limiter._group_limiters
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_send_uses_the_real_chat_id_bucket():
    """Inside stream_context, the group bucket keys on the supergroup's chat_id
    — NOT a per-topic key. Forum topics share the per-chat budget."""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})
    chat_id = -100222

    with stream_context():
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="sendMessage",
            data={"chat_id": chat_id, "message_thread_id": 3},
            rate_limit_args=None,
        )

    assert chat_id in limiter._group_limiters
    assert f"{chat_id}:3" not in limiter._group_limiters


@pytest.mark.asyncio
async def test_stream_edit_also_counts_against_the_chat_bucket():
    """Status edits in stream context go through the bucket too — Telegram
    counts edits against the per-chat rate, so they must self-throttle."""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})
    chat_id = -100222

    with stream_context():
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="editMessageText",
            data={"chat_id": chat_id, "message_thread_id": 3},
            rate_limit_args=None,
        )

    assert chat_id in limiter._group_limiters
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_topics_share_one_chat_bucket():
    """Streams to different topics in the same supergroup draw from one bucket
    — once it's drained, the next topic's send waits. (The cost of respecting
    Telegram's real per-chat ceiling; a flooding topic *can* delay a sibling.)"""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})
    chat_id = -100333

    with stream_context():
        # Topic 1 drains the chat's single token.
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="sendMessage",
            data={"chat_id": chat_id, "message_thread_id": 1},
            rate_limit_args=None,
        )

        async def _send_topic_2() -> None:
            await limiter.process_request(
                callback=callback,
                args=(),
                kwargs={},
                endpoint="sendMessage",
                data={"chat_id": chat_id, "message_thread_id": 2},
                rate_limit_args=None,
            )

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_send_topic_2(), timeout=0.3)

    assert callback.await_count == 1


@pytest.mark.asyncio
async def test_command_response_not_blocked_by_a_saturated_chat_bucket():
    """A command (outside stream_context) bypasses the chat bucket — so it
    responds at once even when a stream has drained that chat's budget."""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})
    chat_id = -100444

    with stream_context():
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="sendMessage",
            data={"chat_id": chat_id, "message_thread_id": 9},
            rate_limit_args=None,
        )

    async def _command_response() -> None:
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="sendPhoto",
            data={"chat_id": chat_id, "message_thread_id": 9},
            rate_limit_args=None,
        )

    await asyncio.wait_for(_command_response(), timeout=1.0)
    assert callback.await_count == 2


@pytest.mark.asyncio
async def test_args_and_kwargs_passthrough_unchanged():
    """``args``/``kwargs`` reach the API call verbatim — the limiter never
    mutates them."""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})
    kwargs = {"chat_id": -100666, "message_thread_id": 11, "text": "hi"}

    with stream_context():
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs=kwargs,
            endpoint="sendMessage",
            data={"chat_id": -100666, "message_thread_id": 11, "text": "hi"},
            rate_limit_args=None,
        )

    callback.assert_awaited_once_with(chat_id=-100666, message_thread_id=11, text="hi")


@pytest.mark.asyncio
async def test_stream_context_is_scoped_to_with_block():
    """``stream_context()`` doesn't leak: a send after the with-block bypasses."""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})
    chat_id = -100777

    with stream_context():
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="sendMessage",
            data={"chat_id": chat_id, "message_thread_id": 1},
            rate_limit_args=None,
        )
    await limiter.process_request(
        callback=callback,
        args=(),
        kwargs={},
        endpoint="sendMessage",
        data={"chat_id": chat_id, "message_thread_id": 1},
        rate_limit_args=None,
    )

    assert list(limiter._group_limiters.keys()) == [chat_id]
    assert callback.await_count == 2


@pytest.mark.asyncio
async def test_background_send_skips_buckets_and_calls_raw():
    """Background traffic registers no bucket — it goes through the slow shared
    lane, then straight to the API call."""
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})

    with background_context():
        await limiter.process_request(
            callback=callback,
            args=(),
            kwargs={},
            endpoint="unpinAllForumTopicMessages",
            data={"chat_id": -100888, "message_thread_id": 5},
            rate_limit_args=None,
        )

    assert not limiter._group_limiters
    callback.assert_awaited_once()
    assert limiter._bg_next_slot > 0


@pytest.mark.asyncio
async def test_background_sends_are_spaced(monkeypatch: pytest.MonkeyPatch):
    """Two consecutive background sends are ≥ _BG_MIN_INTERVAL apart — a burst
    can't push the bot over a Telegram limit."""
    monkeypatch.setattr("ccbot.rate_limiter._BG_MIN_INTERVAL", 0.08)
    limiter = _limiter()
    callback = AsyncMock(return_value={"ok": True})

    async def _bg_call() -> None:
        with background_context():
            await limiter.process_request(
                callback=callback,
                args=(),
                kwargs={},
                endpoint="sendMessage",
                data={"chat_id": -100999},
                rate_limit_args=None,
            )

    await _bg_call()
    t0 = time.monotonic()
    await _bg_call()
    assert time.monotonic() - t0 >= 0.06
    assert callback.await_count == 2


@pytest.mark.asyncio
async def test_background_429_propagates_without_bypass_log(
    caplog: pytest.LogCaptureFixture,
):
    """A 429 in the background path surfaces raw — no retry loop, no
    'after retries on bypassed' log (which would mean RetryAfter pause armed)."""
    from telegram.error import RetryAfter

    limiter = _limiter()
    callback = AsyncMock(side_effect=RetryAfter(3))

    with caplog.at_level("ERROR"), background_context():
        with pytest.raises(RetryAfter):
            await limiter.process_request(
                callback=callback,
                args=(),
                kwargs={},
                endpoint="unpinAllForumTopicMessages",
                data={"chat_id": -1001234},
                rate_limit_args=None,
            )
    assert "after retries on bypassed" not in caplog.text
