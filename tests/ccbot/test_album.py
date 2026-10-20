"""Tests for handlers.album: an album's items go out as one prompt."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import album

KEY = (1, 42, "grp")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(album, "QUIET_SEC", 0.05)
    monkeypatch.setattr(album, "_albums", {})


def _close(mid: int, deliver, caption: str = "", attach: str | None = None) -> None:
    album.close_item(
        KEY,
        mid,
        update=f"upd{mid}",
        caption=caption,
        attach=attach if attach is not None else f"(image attached: /p/{mid}.jpg)",
        deliver=deliver,
    )


async def test_items_go_out_once_with_caption_first() -> None:
    deliver = AsyncMock()
    for mid in (12, 10, 11):  # handlers arrive scrambled
        album.open_item(KEY, mid)
    _close(12, deliver)
    _close(10, deliver)
    _close(11, deliver, caption="here are all of them")
    await asyncio.sleep(0.15)
    deliver.assert_awaited_once()
    text, first, count = deliver.await_args.args
    assert text == (
        "here are all of them\n\n"
        "(image attached: /p/10.jpg)\n"
        "(image attached: /p/11.jpg)\n"
        "(image attached: /p/12.jpg)"
    )
    assert (first, count) == ("upd10", 3)


async def test_waits_for_a_slow_download() -> None:
    deliver = AsyncMock()
    album.open_item(KEY, 1)
    album.open_item(KEY, 2)
    _close(1, deliver)
    await asyncio.sleep(0.15)  # item 2 still downloading
    deliver.assert_not_awaited()
    _close(2, deliver)
    await asyncio.sleep(0.15)
    assert deliver.await_args.args[2] == 2


async def test_failed_item_keeps_its_caption() -> None:
    deliver = AsyncMock()
    album.open_item(KEY, 1)
    album.open_item(KEY, 2)
    _close(1, deliver, caption="текст")
    album.close_item(KEY, 2, update="upd2", caption="", attach=None, deliver=deliver)
    await asyncio.sleep(0.15)
    text, _, count = deliver.await_args.args
    assert text == "текст\n\n(image attached: /p/1.jpg)" and count == 1


async def test_unfinished_item_gives_up_after_max_wait(monkeypatch) -> None:
    monkeypatch.setattr(album, "MAX_WAIT_SEC", 0.1)
    deliver = AsyncMock()
    album.open_item(KEY, 1)
    album.open_item(KEY, 2)  # never closes
    _close(1, deliver)
    await asyncio.sleep(0.3)
    assert deliver.await_args.args[2] == 1


async def test_separate_albums_stay_separate() -> None:
    deliver = AsyncMock()
    other = (1, 42, "grp2")
    album.open_item(KEY, 1)
    album.open_item(other, 5)
    _close(1, deliver)
    album.close_item(other, 5, update="u5", caption="", attach="(f)", deliver=deliver)
    await asyncio.sleep(0.15)
    assert deliver.await_count == 2
