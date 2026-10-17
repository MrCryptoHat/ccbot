"""A quiet turn shows the chat only its outcome — never its steps."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot import bot
from ccbot.handlers import quiet_turn
from ccbot.session_monitor import NewMessage

IDLE = "out\n" + "─" * 40 + "\n  ❯ \n" + "─" * 40 + "\n"


def _m(text: str, **kw) -> NewMessage:
    return NewMessage(session_id="s", text=text, is_complete=True, **kw)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(quiet_turn, "_turns", {})
    monkeypatch.setattr(quiet_turn, "_due", [])
    monkeypatch.setattr(quiet_turn, "SETTLE_SEC", 0.0)


async def _feed(msgs, sm) -> AsyncMock:
    enqueue = AsyncMock()
    with (
        patch.object(bot, "session_manager", sm),
        patch.object(bot, "enqueue_content_message", enqueue),
        patch.object(bot, "_bump_read_offset_to_eof", AsyncMock()),
    ):
        for m in msgs:
            await bot.handle_new_message(m, MagicMock())
    return enqueue


def _sm() -> MagicMock:
    sm = MagicMock()
    sm.find_users_for_session = AsyncMock(return_value=[(1, "@3", 42)])
    return sm


async def test_steps_are_held_and_only_the_last_text_is_delivered() -> None:
    sm = _sm()
    enqueue = await _feed(
        [
            _m("(quiet turn) wake up", role="user"),
            _m("reading the letter…"),
            _m("Bash(mail check)", content_type="tool_use", tool_name="Bash"),
            _m("letter body", content_type="tool_result"),
            _m("Tell her: the appointment moved."),
        ],
        sm,
    )
    enqueue.assert_not_awaited()  # nothing mid-turn, not even the prompt
    sm.mark_generating.assert_not_called()  # no typing indicator either
    deliver = AsyncMock()
    qsm = MagicMock()
    qsm.capture_pane = AsyncMock(return_value=IDLE)
    qsm.is_agent_working.return_value = False
    with (
        patch.object(quiet_turn, "_deliver", deliver),
        patch.object(quiet_turn, "session_manager", qsm),
    ):
        await quiet_turn.poll(MagicMock())
    deliver.assert_awaited_once()
    assert deliver.await_args.args[0].text == "Tell her: the appointment moved."
    assert not quiet_turn.is_quiet("@3")


async def test_busy_agent_keeps_the_turn_open() -> None:
    await _feed([_m("(quiet turn) x", role="user"), _m("step")], _sm())
    deliver = AsyncMock()
    qsm = MagicMock()
    qsm.capture_pane = AsyncMock(return_value=IDLE)
    qsm.is_agent_working.return_value = True
    with (
        patch.object(quiet_turn, "_deliver", deliver),
        patch.object(quiet_turn, "session_manager", qsm),
    ):
        await quiet_turn.poll(MagicMock())
    deliver.assert_not_awaited()
    assert quiet_turn.is_quiet("@3")


async def test_no_reply_outcome_reaches_nobody() -> None:
    """End to end through the real delivery: the final (no reply) is dropped."""
    sm = _sm()
    await _feed([_m("(quiet turn) x", role="user"), _m("(no reply)")], sm)
    qsm = MagicMock()
    qsm.capture_pane = AsyncMock(return_value=IDLE)
    qsm.is_agent_working.return_value = False
    enqueue = AsyncMock()
    with (
        patch.object(quiet_turn, "session_manager", qsm),
        patch.object(bot, "session_manager", sm),
        patch.object(bot, "enqueue_content_message", enqueue),
        patch.object(bot, "_bump_read_offset_to_eof", AsyncMock()),
    ):
        await quiet_turn.poll(MagicMock())
    enqueue.assert_not_awaited()


async def test_user_writing_releases_the_turn() -> None:
    await _feed([_m("(quiet turn) x", role="user"), _m("held so far")], _sm())
    quiet_turn.release("@3")
    assert not quiet_turn.is_quiet("@3")
    deliver = AsyncMock()
    with patch.object(quiet_turn, "_deliver", deliver):
        await quiet_turn.poll(MagicMock())
    assert deliver.await_args.args[0].text == "held so far"


def test_monitor_lets_the_quiet_prompt_through() -> None:
    from ccbot.session_monitor import _opens_quiet_turn

    assert _opens_quiet_turn("  (quiet turn) [service] mail")
    assert not _opens_quiet_turn("hello (quiet turn)")
