"""Rate limiter that distinguishes three traffic classes.

The bot's outbound traffic has three shapes with different needs:

* **Stream** — assistant output assembled by the per-topic message-queue
  worker (text, status-message sends *and* status edits). It must stay under
  Telegram's documented ~20 msg/min-per-group limit. It goes through PTB's
  parent limiter **unmodified** — keyed on the real ``chat_id`` (the
  supergroup), at the ``group_max_rate``/``group_time_period`` the Application
  configured (~12/60 s, leaving headroom for the user-driven classes). We deliberately do **not** split the
  bucket per forum topic: there's no evidence Telegram budgets topics
  independently (its limiter is ``chat_id``-keyed), so N topics × 12/min would
  blow past the real per-chat ceiling — that's what tripped 429s when several
  agents were busy at once. The cost is that a flooding topic can delay a
  sibling topic's sends by a few seconds; that's the real Telegram limit
  asserting itself, not a bug.
* **Interactive** — single responses to a user action (slash commands, menu
  taps, inline-keyboard callbacks, interactive-UI photos, status polling).
  The user is *waiting*; throttling these is visible latency ("Query is too
  old" callback errors, multi-second /screenshot delays). Skips every bucket.
  This is the default class.
* **Background** — not user-waiting and prone to bursts: the topic-existence
  probe, mail notifications, dashboard refreshes. A burst of these used to
  blow past a Telegram limit, and a 429 on *any* request makes
  ``AIORateLimiter`` set ``_retry_after_event`` — which pauses **every**
  in-flight send (stream + interactive too) for the ban window. So background
  traffic is funnelled through a slow shared lane (``_BG_MIN_INTERVAL`` apart)
  and then issued *directly*, bypassing PTB's retry loop: on a 429 the caller's
  own ``except`` logs it and moves on — we never enter the retry path, so a
  stray background 429 can no longer freeze the whole bot.

  Two per-chat governors sit on top of the shared lane, both scoped to
  *content* endpoints (message sends/edits — the calls Telegram counts against
  its ~20 msg/min-per-chat flood budget; chat-management probes and
  ``sendChatAction`` are exempt so e.g. the worktree deletion probe keeps its
  cadence):

  - **Spacing** — background content sends into one chat sleep until they are
    ``_BG_CHAT_MIN_INTERVAL`` apart. Stream's 12/min + background's ≤3/min
    stays well under the ~20/min ceiling, so a tick-rate dashboard can no longer
    earn the whole chat a flood ban (which froze interactive sends too — the
    2026-07-18 browser_live incident).
  - **429 cooldown** — a ``RetryAfter`` from a background call arms a per-chat
    cooldown for its duration (+slack); until it expires, further background
    content sends to that chat raise ``RetryAfter`` locally without touching
    the API, instead of hammering a banned chat every tick.

``stream_context()`` and ``background_context()`` (set by the queue worker and
by background tasks respectively) flag the class via ``ContextVar``s, which
asyncio propagates into every awaited send. No call-site annotations needed
outside those two boundaries.

Safety net: a 429 *after retries* on a bypassed (interactive) request is logged
loudly so the assumption — that interactive traffic stays under Telegram's
limits without throttling — can be revisited.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dtm
import logging
import math
import time
from collections.abc import Callable, Coroutine, Iterator
from contextvars import ContextVar
from typing import Any

from telegram._utils.types import JSONDict
from telegram.error import RetryAfter
from telegram.ext import AIORateLimiter

logger = logging.getLogger(__name__)

# True only within the per-topic message-queue worker. asyncio propagates
# ContextVar values into awaited tasks, so any send reached from inside
# ``stream_context()`` — directly or through helpers — sees this as True.
_IS_STREAM: ContextVar[bool] = ContextVar("ccbot_is_stream", default=False)

# True within ``background_context()`` — topic probes, mail notices, dashboard
# refreshes. Mutually exclusive with _IS_STREAM in practice; if both were set,
# background wins (checked first in process_request).
_IS_BACKGROUND: ContextVar[bool] = ContextVar("ccbot_is_background", default=False)

# Minimum spacing between background API calls, shared across *all* background
# traffic. ≈3 calls/s — far under every Telegram limit, yet enough for a probe
# per few minutes plus a handful of mail notices and dashboard edits.
_BG_MIN_INTERVAL = 0.34  # seconds

# Minimum spacing between background *content* sends (messages / edits / media)
# into ONE chat. The per-chat flood budget (~20 msg/min) is shared with stream
# traffic (12/min bucket) AND the user-driven classes (interactive taps,
# reaction-acks, pins), so background content must stay a small fraction:
# 20 s ⇒ ≤3/min, keeping sustained bot traffic ≈15/min with headroom.
# Callers sleep until their slot — a one-shot notice is merely delayed, a
# tick-rate dashboard is naturally paced down.
_BG_CHAT_MIN_INTERVAL = 20.0  # seconds

# Extra seconds added on top of Telegram's retry_after when arming a per-chat
# cooldown — retrying at the exact expiry re-earns the ban half the time.
_BG_BAN_SLACK = 2.0  # seconds


def _is_content_endpoint(endpoint: str) -> bool:
    """True for endpoints that create or edit a chat message — the calls
    Telegram counts against its per-chat flood budget. Chat-management calls
    (``reopenForumTopic`` probes, pins) and ``sendChatAction`` (typing) have
    their own, much laxer buckets and must not be paced down with content."""
    if endpoint == "sendChatAction":
        return False
    return endpoint.startswith(("send", "edit", "copy", "forward"))


def _retry_after_seconds(exc: RetryAfter) -> float:
    """Normalize PTB's ``RetryAfter.retry_after`` (int pre-22.2, timedelta after)."""
    value = exc.retry_after
    if isinstance(value, dtm.timedelta):
        return value.total_seconds()
    return float(value)


@contextlib.contextmanager
def stream_context() -> Iterator[None]:
    """Mark enclosed sends as stream traffic (counts against the chat's budget)."""
    token = _IS_STREAM.set(True)
    try:
        yield
    finally:
        _IS_STREAM.reset(token)


@contextlib.contextmanager
def background_context() -> Iterator[None]:
    """Mark enclosed sends as background traffic (not user-waiting, bursty).

    Sends inside are spaced through a slow shared lane so a burst can't push the
    bot over a Telegram limit, then issued directly (no PTB retry loop) so a
    stray 429 here can't trigger the global RetryAfter pause — the caller's own
    ``except`` is expected to log it and carry on.
    """
    token = _IS_BACKGROUND.set(True)
    try:
        yield
    finally:
        _IS_BACKGROUND.reset(token)


class CcbotRateLimiter(AIORateLimiter):
    """AIORateLimiter that routes by traffic class (see module docstring).

    * **Background** → per-chat content governor (spacing + flood-ban
      cooldown), then a slot in the shared slow lane, then call the API
      directly (no limiter buckets, no retry loop).
    * **Stream** → delegate to the parent unchanged: keyed on the real
      ``chat_id`` (the supergroup), so the parent's 20/60 s-style group bucket
      *is* the per-chat governor.
    * **Interactive** → ``chat_id`` stripped so the parent skips both group and
      overall buckets.

    ``args``/``kwargs`` are never mutated, so the real chat_id and thread_id
    always reach the Telegram API.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Shared background-lane gate. asyncio.Lock() doesn't bind to a loop at
        # construction (3.10+), so building this before the loop exists is fine.
        self._bg_lock = asyncio.Lock()
        self._bg_next_slot = 0.0
        # Per-chat governors for background *content* sends (see module
        # docstring): next allowed slot, and flood-ban cooldown expiry.
        self._bg_chat_next_slot: dict[Any, float] = {}
        self._bg_chat_banned_until: dict[Any, float] = {}

    async def _await_background_slot(self) -> None:
        """Block until this background call may go out (≥ _BG_MIN_INTERVAL since
        the previous one), then reserve the next slot."""
        async with self._bg_lock:
            now = time.monotonic()
            if now < self._bg_next_slot:
                await asyncio.sleep(self._bg_next_slot - now)
                now = time.monotonic()
            self._bg_next_slot = now + _BG_MIN_INTERVAL

    def _check_background_cooldown(self, chat_id: Any) -> None:
        """Raise ``RetryAfter`` locally while ``chat_id`` is under a flood-ban
        cooldown — never hit the API with a send Telegram is known to refuse."""
        remaining = self._bg_chat_banned_until.get(chat_id, 0.0) - time.monotonic()
        if remaining > 0:
            raise RetryAfter(math.ceil(remaining))

    async def _await_background_chat_slot(self, chat_id: Any) -> None:
        """Sleep until this chat's next background-content slot (spacing
        ``_BG_CHAT_MIN_INTERVAL``), reserving it first so concurrent callers
        queue FIFO instead of stampeding when the slot frees."""
        self._check_background_cooldown(chat_id)
        now = time.monotonic()
        slot = max(now, self._bg_chat_next_slot.get(chat_id, 0.0))
        self._bg_chat_next_slot[chat_id] = slot + _BG_CHAT_MIN_INTERVAL
        if slot > now:
            await asyncio.sleep(slot - now)
            # A ban may have been armed while we slept — bail before the call.
            self._check_background_cooldown(chat_id)

    def _arm_background_cooldown(self, chat_id: Any, exc: RetryAfter) -> None:
        retry_in = _retry_after_seconds(exc) + _BG_BAN_SLACK
        until = time.monotonic() + retry_in
        if until > self._bg_chat_banned_until.get(chat_id, 0.0):
            self._bg_chat_banned_until[chat_id] = until
            logger.warning(
                "Background send hit a flood ban (chat=%s) — cooling that chat's "
                "background content down for %.0fs",
                chat_id,
                retry_in,
            )

    async def process_request(
        self,
        callback: Callable[..., Coroutine[Any, Any, bool | JSONDict | list[JSONDict]]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: int | None,
    ) -> bool | JSONDict | list[JSONDict]:
        if _IS_BACKGROUND.get():
            # Per-chat content governor first (spacing + ban cooldown), then the
            # slow shared lane, then issue directly: no buckets, no retry loop —
            # a 429 here surfaces to the caller's except instead of arming
            # _retry_after_event and freezing every other send.
            chat_id = data.get("chat_id")
            content = chat_id is not None and _is_content_endpoint(endpoint)
            if content:
                await self._await_background_chat_slot(chat_id)
            await self._await_background_slot()
            try:
                return await callback(*args, **kwargs)
            except RetryAfter as e:
                if content:
                    self._arm_background_cooldown(chat_id, e)
                raise

        if _IS_STREAM.get():
            # Stream traffic (text + status sends + status edits) all counts
            # against the supergroup's rate budget — pass through unchanged so
            # the parent's group bucket keys on the real chat_id.
            return await super().process_request(
                callback, args, kwargs, endpoint, data, rate_limit_args
            )

        # Interactive (user waiting) — skip both limiters.
        return await self._call_bypassing_limiters(
            callback, args, kwargs, endpoint, data, rate_limit_args
        )

    async def _call_bypassing_limiters(
        self,
        callback: Callable[..., Coroutine[Any, Any, bool | JSONDict | list[JSONDict]]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: int | None,
    ) -> bool | JSONDict | list[JSONDict]:
        """Call super with ``chat_id`` stripped so neither limiter fires."""
        data_no_chat = {k: v for k, v in data.items() if k != "chat_id"}
        try:
            return await super().process_request(
                callback, args, kwargs, endpoint, data_no_chat, rate_limit_args
            )
        except RetryAfter:
            logger.error(
                "429 after retries on bypassed (interactive) traffic "
                "(endpoint=%s) — Telegram counted this against its rate bucket "
                "after all; revisit stream_context placement / whether this "
                "endpoint should be classed as stream in rate_limiter.py",
                endpoint,
            )
            raise
