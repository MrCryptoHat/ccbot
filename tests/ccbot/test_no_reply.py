"""An agent turn that ends with only the no-reply marker is not relayed."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot import bot
from ccbot.session_monitor import NewMessage


def _msg(text: str, **kw) -> NewMessage:
    return NewMessage(session_id="s", text=text, is_complete=True, **kw)


def test_only_the_exact_marker_counts() -> None:
    assert bot.is_no_reply(_msg("(no reply)"))
    assert bot.is_no_reply(_msg("  (no reply)\n"))
    # Anything else is a real reply — never swallowed.
    assert not bot.is_no_reply(_msg("Учла. (no reply)"))
    assert not bot.is_no_reply(_msg("(no reply)", role="user"))
    assert not bot.is_no_reply(_msg("(no reply)", content_type="thinking"))


@pytest.mark.asyncio
async def test_no_reply_turn_reaches_no_chat() -> None:
    sm = MagicMock()
    sm.find_users_for_session = AsyncMock(return_value=[(1, "@3", 42)])
    enqueue = AsyncMock()
    with (
        patch.object(bot, "session_manager", sm),
        patch.object(bot, "enqueue_content_message", enqueue),
        patch.object(bot, "_bump_read_offset_to_eof", AsyncMock()) as bump,
    ):
        await bot.handle_new_message(_msg("(no reply)"), MagicMock())
    enqueue.assert_not_awaited()
    bump.assert_awaited_once_with(1, "@3")
