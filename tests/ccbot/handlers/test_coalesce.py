"""Tests for coalesce_text: reassembling Telegram-split long pastes.

Covers the three behaviours that matter: an ordinary short message passes
through untouched and instantly; near-ceiling fragments followed by a short
tail are joined (in message_id order, no separator) into one send; an all-full-
width paste flushes on the safety timeout.
"""

from __future__ import annotations

import asyncio

import pytest

from ccbot.handlers import coalesce


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test starts with empty buffers; cancel any leftover timers after."""
    coalesce._buffers.clear()
    coalesce._locks.clear()
    yield
    for buf in coalesce._buffers.values():
        if buf.timer is not None:
            buf.timer.cancel()
    coalesce._buffers.clear()
    coalesce._locks.clear()


async def test_short_message_passes_through_immediately():
    sent: list[str] = []

    async def cb(text: str) -> None:
        sent.append(text)

    await coalesce.coalesce_text(1, 42, 100, "hello", cb)

    assert sent == ["hello"]
    assert (1, 42) not in coalesce._buffers  # nothing buffered


async def test_split_paste_reassembled_on_short_tail():
    sent: list[str] = []

    async def cb(text: str) -> None:
        sent.append(text)

    part1 = "a" * coalesce.NEAR_CEILING
    part2 = "b" * coalesce.NEAR_CEILING
    tail = "ccc"  # below ceiling → completes the paste

    await coalesce.coalesce_text(1, 42, 100, part1, cb)
    assert sent == []  # buffered, not delivered yet
    await coalesce.coalesce_text(1, 42, 101, part2, cb)
    assert sent == []
    await coalesce.coalesce_text(1, 42, 102, tail, cb)

    assert sent == [part1 + part2 + tail]
    assert (1, 42) not in coalesce._buffers


async def test_fragments_ordered_by_message_id():
    """Concurrent handlers can reach the buffer out of order; the join must
    still follow message_id, not arrival order."""
    sent: list[str] = []

    async def cb(text: str) -> None:
        sent.append(text)

    part_a = "a" * coalesce.NEAR_CEILING
    part_b = "b" * coalesce.NEAR_CEILING
    tail = "z"

    # Arrive 101 before 100 (out of order), then the tail at 102.
    await coalesce.coalesce_text(1, 42, 101, part_b, cb)
    await coalesce.coalesce_text(1, 42, 100, part_a, cb)
    await coalesce.coalesce_text(1, 42, 102, tail, cb)

    assert sent == [part_a + part_b + tail]


async def test_all_full_width_flushes_on_timeout(monkeypatch):
    """A paste whose length is an exact multiple of the split size never sends
    a short tail — the safety timeout must flush what's buffered."""
    monkeypatch.setattr(coalesce, "FLUSH_TIMEOUT", 0.05)
    sent: list[str] = []

    async def cb(text: str) -> None:
        sent.append(text)

    part1 = "a" * coalesce.NEAR_CEILING
    part2 = "b" * coalesce.NEAR_CEILING

    await coalesce.coalesce_text(1, 42, 100, part1, cb)
    await coalesce.coalesce_text(1, 42, 101, part2, cb)
    assert sent == []  # still waiting for more / timeout

    await asyncio.sleep(0.12)  # let the safety timer fire

    assert sent == [part1 + part2]
    assert (1, 42) not in coalesce._buffers


async def test_separate_topics_do_not_mix():
    sent_a: list[str] = []
    sent_b: list[str] = []

    async def cb_a(text: str) -> None:
        sent_a.append(text)

    async def cb_b(text: str) -> None:
        sent_b.append(text)

    big = "a" * coalesce.NEAR_CEILING
    # Topic 42 opens a buffer; topic 43 gets a normal short message meanwhile.
    await coalesce.coalesce_text(1, 42, 100, big, cb_a)
    await coalesce.coalesce_text(1, 43, 200, "hi", cb_b)

    assert sent_b == ["hi"]  # other topic unaffected, delivered at once
    assert sent_a == []  # topic 42 still buffering
