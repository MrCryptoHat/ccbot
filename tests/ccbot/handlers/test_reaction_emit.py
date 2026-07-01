"""Tests for reaction_emit: the 👀 "agent took your message into context" ack.

Pins the arm()→maybe_fire() machine: the ack is withheld while the message is
buffered in the window's input queue (busy agent) and fires only once the queue
drains (message taken into context). Plus TTL drop and swallowed failures.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import reaction_emit


@pytest.fixture(autouse=True)
def _clear_pending():
    reaction_emit._pending.clear()
    yield
    reaction_emit._pending.clear()


@pytest.mark.asyncio
async def test_waits_while_queued_then_fires_when_drained():
    bot = AsyncMock()
    reaction_emit.arm("@1", -100, 5)

    await reaction_emit.maybe_fire(bot, "@1", has_queue=True)  # still buffered
    bot.set_message_reaction.assert_not_called()

    await reaction_emit.maybe_fire(bot, "@1", has_queue=False)  # drained → fire
    bot.set_message_reaction.assert_awaited_once()
    _, kwargs = bot.set_message_reaction.call_args
    assert kwargs["chat_id"] == -100
    assert kwargs["message_id"] == 5
    assert kwargs["reaction"][0].emoji == reaction_emit.ACK_EMOJI == "👀"


@pytest.mark.asyncio
async def test_idle_agent_fires_on_first_poll():
    bot = AsyncMock()
    reaction_emit.arm("@1", -100, 5)
    # No queue (idle agent took it straight into context) → fire next poll.
    await reaction_emit.maybe_fire(bot, "@1", has_queue=False)
    bot.set_message_reaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_double_fire():
    bot = AsyncMock()
    reaction_emit.arm("@1", -100, 5)
    await reaction_emit.maybe_fire(bot, "@1", has_queue=False)
    await reaction_emit.maybe_fire(bot, "@1", has_queue=False)
    bot.set_message_reaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_pending_is_noop():
    bot = AsyncMock()
    await reaction_emit.maybe_fire(bot, "@nope", has_queue=False)
    bot.set_message_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_ttl_drops_stale_pending():
    bot = AsyncMock()
    reaction_emit.arm("@1", -100, 5)
    reaction_emit._pending["@1"].ts -= reaction_emit._TTL_SEC + 1
    await reaction_emit.maybe_fire(bot, "@1", has_queue=False)
    bot.set_message_reaction.assert_not_called()
    assert "@1" not in reaction_emit._pending


@pytest.mark.asyncio
async def test_forget_drops_pending():
    bot = AsyncMock()
    reaction_emit.arm("@1", -100, 5)
    reaction_emit.forget("@1")
    await reaction_emit.maybe_fire(bot, "@1", has_queue=False)
    bot.set_message_reaction.assert_not_called()


@pytest.mark.asyncio
async def test_failure_is_swallowed():
    bot = AsyncMock()
    bot.set_message_reaction.side_effect = RuntimeError("telegram boom")
    reaction_emit.arm("@1", -100, 5)
    await reaction_emit.maybe_fire(bot, "@1", has_queue=False)  # must not raise
    assert "@1" not in reaction_emit._pending
