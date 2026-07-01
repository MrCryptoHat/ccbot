"""Shared helpers for screenshot / interactive-UI message handlers.

Three small optimizations that piggyback on each other:

1. **Per-message pane-hash cache** — maps Telegram `message_id` to the
   hash of the pane text it currently displays. Lets render+edit paths
   short-circuit on unchanged content: hash the new capture, compare to
   the stored one, and if identical skip both `text_to_image` (~220 ms
   CPU) and `edit_message_media` (~300 ms upload). Telegram would
   eventually return "not modified", but only after we paid both costs.

2. **Pane-hash → file_id cache** — when we render the same pane content
   we've shown before (e.g. the user navigates AskUserQuestion ↓↓↑↑ and
   revisits a previous cursor position), we already uploaded the bytes
   in this bot's lifetime. Telegram's `file_id` for that upload remains
   valid indefinitely for the same bot. So instead of re-rendering and
   re-uploading, we hand `InputMediaPhoto(media=<cached_file_id>)` to
   `editMessageMedia`, which serves the same bytes without our client
   ever uploading them — kills the entire upload leg (~250 ms RTT on
   a high-latency link). Bounded LRU at FILE_ID_CACHE_MAX so a long-running
   bot doesn't grow this dict without limit.

3. **Adaptive wait for pane change** — replaces a fixed
   `asyncio.sleep(0.5)` between a key press and the subsequent capture.
   Given the last-rendered hash (from cache #1), we poll the pane at
   short intervals and return as soon as the hash changes, or the
   max_wait deadline elapses. Fast redraws (common case) get ~150 ms
   instead of 500 ms; stuck redraws still bound at max_wait.
"""

import asyncio
import hashlib
import logging
from collections import OrderedDict

from ..session import session_manager

logger = logging.getLogger(__name__)

# message_id -> sha1 hex (truncated) of the pane text rendered into it
_PANE_HASH_BY_MSG: dict[int, str] = {}

# pane_hash -> Telegram file_id for the rendered photo. OrderedDict for LRU.
FILE_ID_CACHE_MAX = 256
_FILE_ID_BY_HASH: OrderedDict[str, str] = OrderedDict()


def pane_hash(text: str) -> str:
    """Short stable hash used to detect "pane unchanged since last render"."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def get_hash(message_id: int) -> str | None:
    return _PANE_HASH_BY_MSG.get(message_id)


def set_hash(message_id: int, h: str) -> None:
    _PANE_HASH_BY_MSG[message_id] = h


def forget(message_id: int) -> None:
    _PANE_HASH_BY_MSG.pop(message_id, None)


def get_file_id(pane_hash_value: str) -> str | None:
    """Look up a previously uploaded photo by its pane-content hash.

    On hit, also moves the entry to the LRU tail so frequently-used
    photos survive cache eviction.
    """
    fid = _FILE_ID_BY_HASH.get(pane_hash_value)
    if fid is not None:
        _FILE_ID_BY_HASH.move_to_end(pane_hash_value)
    return fid


def set_file_id(pane_hash_value: str, file_id: str) -> None:
    """Record the file_id Telegram returned for an upload of this pane."""
    if pane_hash_value in _FILE_ID_BY_HASH:
        _FILE_ID_BY_HASH.move_to_end(pane_hash_value)
        _FILE_ID_BY_HASH[pane_hash_value] = file_id
        return
    if len(_FILE_ID_BY_HASH) >= FILE_ID_CACHE_MAX:
        _FILE_ID_BY_HASH.popitem(last=False)
    _FILE_ID_BY_HASH[pane_hash_value] = file_id


def forget_file_id(pane_hash_value: str) -> None:
    """Drop a file_id from cache (e.g. Telegram rejected it as stale)."""
    _FILE_ID_BY_HASH.pop(pane_hash_value, None)


async def wait_pane_change(
    window_id: str,
    prior_hash: str | None,
    *,
    min_settle: float = 0.15,
    max_wait: float = 0.6,
    poll_interval: float = 0.12,
) -> tuple[str | None, str | None]:
    """Poll pane until its hash differs from `prior_hash` or timeout.

    Returns (text, hash) of the latest capture — even on timeout, so the
    caller still has a usable value. If `prior_hash` is None, just
    settles for `min_settle` and captures once (caller has no baseline).
    """
    await asyncio.sleep(min_settle)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait
    text = await session_manager.capture_pane(window_id, with_ansi=True)
    if not text:
        return None, None
    cur = pane_hash(text)
    while prior_hash is not None and cur == prior_hash and loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        text = await session_manager.capture_pane(window_id, with_ansi=True)
        if not text:
            return None, None
        cur = pane_hash(text)
    return text, cur
