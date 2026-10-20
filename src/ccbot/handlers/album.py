"""Gather a Telegram album (several photos/files sent at once) into one prompt.

An album reaches the bot as one message per item, sharing a
``media_group_id``, each its own Update and — under ``concurrent_updates`` —
its own handler. Forwarded one by one, the first item starts a turn and the
rest queue up as separate prompts, with the caption riding on whichever
item carried it: the agent sees "here's a photo" several times instead of
one message with the user's text and all the attachments.

Each item's handler opens a slot (``open_item``) BEFORE downloading and
fills it (``close_item``) after, so a slow download holds the album back
instead of being cut off. Once every slot is filled and :data:`QUIET_SEC`
passed without a new item, the album is delivered once: every caption (in
message order), then every attachment line. Order is by ``message_id`` —
concurrent handlers arrive scrambled.

Only albums wait: a single photo/file has no ``media_group_id`` and never
comes here. :data:`MAX_WAIT_SEC` bounds a download that never finishes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Telegram delivers an album's items within a few hundred ms of each other.
QUIET_SEC = 1.5
MAX_WAIT_SEC = 120.0

# deliver(text, first_update, attachment_count)
DeliverCallback = Callable[[str, Any, int], Awaitable[None]]
AlbumKey = tuple[int, int, str]  # (user_id, thread_id, media_group_id)


@dataclass
class _Item:
    update: Any = None
    caption: str = ""
    attach: str | None = None
    done: bool = False


@dataclass
class _Album:
    items: dict[int, _Item] = field(default_factory=dict)  # message_id → item
    deliver: DeliverCallback | None = None
    opened: float = field(default_factory=time.monotonic)
    timer: asyncio.Task[None] | None = None


_albums: dict[AlbumKey, _Album] = {}


def open_item(key: AlbumKey, message_id: int) -> None:
    """Reserve this item's place: the album won't go out until it's closed."""
    album = _albums.setdefault(key, _Album())
    album.items.setdefault(message_id, _Item())
    if album.timer is not None:
        album.timer.cancel()
        album.timer = None


def close_item(
    key: AlbumKey,
    message_id: int,
    *,
    update: Any,
    caption: str,
    attach: str | None,
    deliver: DeliverCallback,
) -> None:
    """Fill the item (``attach`` None = it failed, only its caption counts)
    and send the album once it is complete and quiet."""
    album = _albums.get(key)
    if album is None:
        return
    item = album.items.setdefault(message_id, _Item())
    item.update, item.caption, item.attach, item.done = update, caption, attach, True
    album.deliver = deliver
    if album.timer is not None:
        album.timer.cancel()
    album.timer = asyncio.create_task(_flush_later(key, album))


async def _flush_later(key: AlbumKey, album: _Album) -> None:
    try:
        await asyncio.sleep(QUIET_SEC)
        # An item still downloading: its close_item re-arms this timer; the
        # loop only matters if it never closes.
        while not all(i.done for i in album.items.values()):
            if time.monotonic() - album.opened >= MAX_WAIT_SEC:
                logger.warning("Album %s: sending without unfinished items", key)
                break
            await asyncio.sleep(QUIET_SEC)
    except asyncio.CancelledError:
        return
    if _albums.get(key) is not album:
        return
    _albums.pop(key, None)
    await _send(key, album)


async def _send(key: AlbumKey, album: _Album) -> None:
    items = [album.items[m] for m in sorted(album.items) if album.items[m].done]
    captions = [i.caption.strip() for i in items if i.caption.strip()]
    attaches = [i.attach for i in items if i.attach]
    first = next((i.update for i in items if i.update is not None), None)
    if album.deliver is None or first is None or (not attaches and not captions):
        return
    text = "\n\n".join([*captions, "\n".join(attaches)] if attaches else captions)
    logger.info("Album %s: %d attachment(s) in one message", key, len(attaches))
    try:
        await album.deliver(text, first, len(attaches))
    except Exception:
        logger.exception("Album %s delivery failed", key)
