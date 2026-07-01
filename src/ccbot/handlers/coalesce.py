"""Reassemble Telegram-split long pastes into one prompt before sending.

A message over Telegram's 4096-char limit is split by the *client* into several
messages on send; each reaches the bot as its own Update (and, under
``concurrent_updates(True)``, its own handler coroutine). Delivered one by one,
the agent takes the first fragment as a turn and the rest pile into Claude
Code's input queue as separate prompts — so it reads only a slice of the paste.

This buffers the fragments per ``(user_id, thread_id)`` and forwards the
reassembled text as a single send. The trigger is *length*, not a blanket
debounce: only a fragment at/near the ceiling (``NEAR_CEILING``) opens a buffer,
so ordinary short messages pass straight through with zero added latency. An
open buffer flushes when a short (tail) fragment arrives, or — for a paste whose
length is an exact multiple of the split size, where every fragment is
full-width — after ``FLUSH_TIMEOUT`` of quiet.

Fragments are joined with no separator (the client splits on a raw character
boundary, so concatenation restores the original text) and ordered by
``message_id`` (concurrent handlers can reach the buffer out of arrival order;
message_id is monotonic per chat).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# A fragment this long or longer is treated as "more may follow". Telegram's
# hard limit is 4096; clients break a touch earlier when a whitespace is near
# the boundary, so the threshold sits below 4096 with margin. Matches the
# outbound MERGE_MAX_LENGTH sensibility. A single genuine paste of this size
# that *wasn't* split costs one FLUSH_TIMEOUT of delay — rare and harmless.
NEAR_CEILING = 3800

# Safety flush for a buffer that never sees a short tail (paste length is an
# exact multiple of the split size). Seconds of quiet before sending what we
# have.
FLUSH_TIMEOUT = 1.2

FlushCallback = Callable[[str], Awaitable[None]]

QueueKey = tuple[int, int]


@dataclass
class _Buffer:
    parts: list[tuple[int, str]] = field(default_factory=list)  # (message_id, text)
    flush_cb: FlushCallback | None = None
    timer: asyncio.Task[None] | None = None


_buffers: dict[QueueKey, _Buffer] = {}
_locks: dict[QueueKey, asyncio.Lock] = {}


def _lock_for(key: QueueKey) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def coalesce_text(
    user_id: int,
    thread_id: int,
    message_id: int,
    text: str,
    flush_cb: FlushCallback,
) -> None:
    """Buffer a split fragment, or pass a normal message straight through.

    ``flush_cb(assembled_text)`` is awaited exactly once per logical message:
    immediately for an ordinary (non-fragment) message, or after the split
    fragments are reassembled. The per-topic lock held across the callback
    serialises sends within a topic (preserving order); different topics never
    contend.
    """
    key = (user_id, thread_id)
    async with _lock_for(key):
        buf = _buffers.get(key)
        is_fragment = len(text) >= NEAR_CEILING

        # Common case: a short message with nothing buffered — deliver now.
        if buf is None and not is_fragment:
            await flush_cb(text)
            return

        if buf is None:
            buf = _Buffer()
            _buffers[key] = buf
            logger.info(
                "Coalescing split paste (user=%d, thread=%d): first fragment %d chars",
                user_id,
                thread_id,
                len(text),
            )
        buf.parts.append((message_id, text))
        buf.flush_cb = flush_cb

        # A fragment shorter than the ceiling is the tail — the paste is
        # complete, send it now. A full-width fragment means more may follow;
        # (re)arm the safety timer instead.
        if not is_fragment:
            await _flush_locked(key)
        else:
            _arm_timer(key)


def _arm_timer(key: QueueKey) -> None:
    """(Re)start the safety-flush timer for an open buffer. Caller holds lock."""
    buf = _buffers.get(key)
    if buf is None:
        return
    if buf.timer is not None:
        buf.timer.cancel()
    buf.timer = asyncio.create_task(_timeout_flush(key))


async def _timeout_flush(key: QueueKey) -> None:
    try:
        await asyncio.sleep(FLUSH_TIMEOUT)
        async with _lock_for(key):
            # A tail fragment may have already flushed and popped the buffer.
            if key in _buffers:
                logger.info("Coalesce buffer for %s flushed on timeout", key)
                await _flush_locked(key)
    except asyncio.CancelledError:
        pass


async def _flush_locked(key: QueueKey) -> None:
    """Assemble buffered parts and forward them as one. Caller holds lock."""
    buf = _buffers.pop(key, None)
    if buf is None:
        return
    if buf.timer is not None:
        buf.timer.cancel()
        buf.timer = None
    if buf.flush_cb is None:
        return
    buf.parts.sort(key=lambda p: p[0])
    assembled = "".join(text for _, text in buf.parts)
    if len(buf.parts) > 1:
        logger.info(
            "Coalesced %d fragments → %d chars for %s",
            len(buf.parts),
            len(assembled),
            key,
        )
    await buf.flush_cb(assembled)
