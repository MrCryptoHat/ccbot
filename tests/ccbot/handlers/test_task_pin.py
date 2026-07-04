"""Tests for task_pin: auto-pin of new-task user messages (/pin).

Pins the decision logic — the length gate, the pre-send idle check, the
default-on/override semantics — and the two Telegram sides: the pin call
itself never raises, and the «pinned a message» service line is deleted
only when the bot made the pin.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.config import config
from ccbot.handlers import task_pin
from ccbot.session import session_manager

USER = 12345
THREAD = 7
KEY = f"{USER}:{THREAD}"
LONG = "x" * config.pin_tasks_min_chars
SHORT = "делай"


@pytest.fixture(autouse=True)
def _pin_overrides_reset():
    session_manager.pin_topic_overrides.clear()
    yield
    session_manager.pin_topic_overrides.clear()


@pytest.fixture()
def _idle_pane(monkeypatch):
    monkeypatch.setattr(task_pin, "is_claude_working", lambda pane: False)
    monkeypatch.setattr(task_pin, "has_queued_messages", lambda pane: False)


# --- is_task_text -----------------------------------------------------------


def test_short_text_is_not_a_task():
    assert not task_pin.is_task_text(SHORT)


def test_threshold_length_is_a_task():
    assert task_pin.is_task_text(LONG)


def test_bash_command_is_not_a_task_however_long():
    assert not task_pin.is_task_text("!" + LONG)


def test_whitespace_padding_does_not_count():
    padded = SHORT + " " * config.pin_tasks_min_chars
    assert not task_pin.is_task_text(padded)


# --- default-on / override semantics ----------------------------------------


def test_pin_mode_is_on_by_default():
    assert config.pin_tasks_default is True
    assert session_manager.is_pin_mode(USER, THREAD)


def test_toggle_from_default_turns_off_then_back_on():
    assert session_manager.toggle_pin_mode(USER, THREAD) is False
    assert not session_manager.is_pin_mode(USER, THREAD)
    assert session_manager.toggle_pin_mode(USER, THREAD) is True
    assert session_manager.is_pin_mode(USER, THREAD)


def test_no_thread_means_no_pin_mode():
    assert not session_manager.is_pin_mode(USER, None)


# --- should_pin_task --------------------------------------------------------


@pytest.mark.asyncio
async def test_no_thread_no_pin(_idle_pane):
    assert not await task_pin.should_pin_task(USER, None, "@1", LONG, pane_text="p")


@pytest.mark.asyncio
async def test_opted_out_topic_no_pin(_idle_pane):
    session_manager.pin_topic_overrides[KEY] = False
    assert not await task_pin.should_pin_task(USER, THREAD, "@1", LONG, pane_text="p")


@pytest.mark.asyncio
async def test_short_text_no_pin_and_no_capture(monkeypatch, _idle_pane):
    capture = AsyncMock()
    monkeypatch.setattr(session_manager, "capture_pane", capture)
    assert not await task_pin.should_pin_task(USER, THREAD, "@1", SHORT)
    capture.assert_not_called()


@pytest.mark.asyncio
async def test_long_text_idle_agent_pins_by_default(_idle_pane):
    assert await task_pin.should_pin_task(USER, THREAD, "@1", LONG, pane_text="p")


@pytest.mark.asyncio
async def test_captures_pane_when_not_pre_captured(monkeypatch, _idle_pane):
    capture = AsyncMock(return_value="pane")
    monkeypatch.setattr(session_manager, "capture_pane", capture)
    assert await task_pin.should_pin_task(USER, THREAD, "@1", LONG)
    capture.assert_awaited_once_with("@1")


@pytest.mark.asyncio
async def test_working_agent_no_pin(monkeypatch):
    monkeypatch.setattr(task_pin, "is_claude_working", lambda pane: True)
    monkeypatch.setattr(task_pin, "has_queued_messages", lambda pane: False)
    assert not await task_pin.should_pin_task(USER, THREAD, "@1", LONG, pane_text="p")


@pytest.mark.asyncio
async def test_queued_input_no_pin(monkeypatch):
    monkeypatch.setattr(task_pin, "is_claude_working", lambda pane: False)
    monkeypatch.setattr(task_pin, "has_queued_messages", lambda pane: True)
    assert not await task_pin.should_pin_task(USER, THREAD, "@1", LONG, pane_text="p")


@pytest.mark.asyncio
async def test_unreadable_pane_no_pin(monkeypatch, _idle_pane):
    monkeypatch.setattr(session_manager, "capture_pane", AsyncMock(return_value=None))
    assert not await task_pin.should_pin_task(USER, THREAD, "@1", LONG)


# --- pin_task_message -------------------------------------------------------


@pytest.mark.asyncio
async def test_pin_is_silent():
    bot = AsyncMock()
    await task_pin.pin_task_message(bot, -100, 42)
    bot.pin_chat_message.assert_awaited_once_with(
        chat_id=-100, message_id=42, disable_notification=True
    )


@pytest.mark.asyncio
async def test_pin_failure_swallowed():
    bot = AsyncMock()
    bot.pin_chat_message.side_effect = Exception("no admin right")
    await task_pin.pin_task_message(bot, -100, 42)  # must not raise


# --- pinned_service_message_handler ------------------------------------------


def _service_update(from_id: int, *, pinned: bool = True) -> MagicMock:
    update = MagicMock()
    msg = MagicMock()
    msg.pinned_message = MagicMock() if pinned else None
    msg.from_user.id = from_id
    msg.delete = AsyncMock()
    update.effective_message = msg
    return update


def _ctx(bot_id: int) -> MagicMock:
    context = MagicMock()
    context.bot.id = bot_id
    return context


@pytest.mark.asyncio
async def test_bot_pin_service_line_deleted():
    update = _service_update(from_id=999)
    await task_pin.pinned_service_message_handler(update, _ctx(bot_id=999))
    update.effective_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_manual_pin_service_line_kept():
    update = _service_update(from_id=USER)
    await task_pin.pinned_service_message_handler(update, _ctx(bot_id=999))
    update.effective_message.delete.assert_not_called()


@pytest.mark.asyncio
async def test_non_pin_service_message_ignored():
    update = _service_update(from_id=999, pinned=False)
    await task_pin.pinned_service_message_handler(update, _ctx(bot_id=999))
    update.effective_message.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_failure_swallowed():
    update = _service_update(from_id=999)
    update.effective_message.delete.side_effect = Exception("no delete right")
    await task_pin.pinned_service_message_handler(update, _ctx(bot_id=999))
