"""Tests for interactive_ui — handle_interactive_ui and keyboard layout.

Claude Code shows several kinds of TUI prompts (Settings/model picker,
AskUserQuestion, ExitPlanMode, permission prompts, …). ccbot renders
them all through a single path: a PNG screenshot of the pane plus a
compact ↑ / ↓ / ⏎ Enter / ⎋ Esc / 🔄 Обновить keyboard. The user reads
the screenshot for context and drives the cursor via those five keys.
These tests pin that contract down: send_photo (not send_message) is
the delivery mechanism, the keyboard always has exactly those five
callback types, and no-UI panes return False without sending anything.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_UP,
)
from ccbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    _surface_ask_question_text,
    consume_pending_ask_tool_use,
    consume_pending_prose_upgrade,
    handle_interactive_ui,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_photo.return_value = sent_msg
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test.

    Includes ``_interactive_last_sent`` (dedup timestamp) so the 3-second
    "already sent recently" guard doesn't make a follow-up test return
    True without actually rendering anything.
    """
    from ccbot.handlers.interactive_ui import (
        _interactive_last_sent,
        _interactive_mode,
        _interactive_msgs,
    )

    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_last_sent.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    _interactive_last_sent.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_photo_with_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """A Settings/model-picker pane is rendered as a photo + compact nav keyboard."""
        window_id = "@5"

        with (
            # interactive_ui captures the pane via session_manager (it
            # branches by binding type for tmux/docker), not tmux_manager.
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.interactive_ui.text_to_image",
                return_value=b"fake-png",
            ),
        ):
            mock_sm.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_photo.assert_called_once()
        mock_bot.send_message.assert_not_called()
        call_kwargs = mock_bot.send_photo.call_args.kwargs
        assert call_kwargs["chat_id"] == 100
        assert call_kwargs["message_thread_id"] == 42
        assert call_kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """No interactive UI detected → returns False, nothing is sent."""
        window_id = "@5"

        with patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm:
            mock_sm.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_photo.assert_not_called()
        mock_bot.send_message.assert_not_called()


class TestInteractiveKeyboardLayout:
    """The unified keyboard attached to every interactive-UI screenshot.

    Eight keys: ← ↑ ↓ → / ⏎ Enter / ⎋ Esc / ␣ / 🔄 Обновить. The old per-UI
    variation (each prompt kind a bespoke button set) was removed in favour
    of one layout — the user reads the screenshot for context and walks the
    cursor with the directional cluster. ←/→ switch between an
    AskUserQuestion's question tabs, which ↑/↓ alone can't reach; ␣ toggles
    multi-select checkboxes (whitespace can't be sent as a text message, so
    without the button a phone user can't answer those at all).
    """

    def test_keyboard_has_expected_callback_kinds(self):
        keyboard = _build_interactive_keyboard("@5")
        all_cb_data: list[str] = [
            btn.callback_data
            for row in keyboard.inline_keyboard
            for btn in row
            if isinstance(btn.callback_data, str)
        ]
        assert any(CB_ASK_LEFT in d for d in all_cb_data)
        assert any(CB_ASK_UP in d for d in all_cb_data)
        assert any(CB_ASK_DOWN in d for d in all_cb_data)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data)
        assert any(CB_ASK_ENTER in d for d in all_cb_data)
        assert any(CB_ASK_ESC in d for d in all_cb_data)
        assert any(CB_ASK_SPACE in d for d in all_cb_data)
        assert any(CB_ASK_REFRESH in d for d in all_cb_data)
        # And nothing else — the count pins it down (← ↑ ↓ → ⏎ ⎋ ␣ 🔄).
        assert len(all_cb_data) == 8

    def test_callback_data_carries_window_id(self):
        keyboard = _build_interactive_keyboard("@42")
        all_cb_data: list[str] = [
            btn.callback_data
            for row in keyboard.inline_keyboard
            for btn in row
            if isinstance(btn.callback_data, str)
        ]
        for data in all_cb_data:
            assert data.endswith("@42"), data


# A real AskUserQuestion capture (trimmed): the agent wrote two prose paragraphs,
# then asked a question with three real options. The transcript above the prose
# (the user's prompt) must not leak into what we surface.
_AUQ_PANE_WITH_PROSE = """\
❯ Напиши два абзаца про чай, а потом задай вопрос через AskUserQuestion.

● Чай — один из самых древних напитков на планете: его пьют тысячи лет, от горных
  деревень Юньнани до лондонских гостиных.

  Помимо вкуса, чай ценят за мягкую бодрость: кофеин работает в связке с L-теанином.
────────────────────────────────────────────────────────────────────────────────
 ☐ Любимый чай

Какой чай тебе ближе всего?

❯ 1. Зелёный
     Свежий, травянистый.
  2. Чёрный
     Насыщенный и крепкий.
  3. Не пью
     Чай не для меня.
  4. Type something.
────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

# An AskUserQuestion with no preceding prose (the agent put everything in the
# question field — common for the docker "assistant" shopping agent). The thing
# directly above the widget border is a tool result (⎿), so _extract_prose
# correctly finds no prose.
_AUQ_PANE_NO_PROSE = """\
❯ Закажи набор наклеек.

● Bash(ls /workspace/.playwright-mcp)
  ⎿  page-2026-05-12.yml

────────────────────────────────────────────────────────────────────────────────
 ☐ Как заказываем

У варианта 2 минимальный заказ — 10 шт (5 нельзя). 10 шт ≈ 10 EUR + доставка. Как делаем?

❯ 1. Берём 10 шт
     Оформляю вариант 2 на 10 шт.
  2. Отмена
     Не заказываем.
  3. Type something.
────────────────────────────────────────────────────────────────────────────────
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""


@pytest.fixture
def _clear_auq_state():
    """Wipe the AskUserQuestion surface bookkeeping before/after each test."""
    from ccbot.handlers.interactive_ui import _auq_text_sent, _pending_auq

    _auq_text_sent.clear()
    _pending_auq.clear()
    yield
    _auq_text_sent.clear()
    _pending_auq.clear()


@pytest.mark.usefixtures("_clear_auq_state")
class TestSurfaceAskQuestionText:
    """Before the user answers, AskUserQuestion's prose + question are surfaced
    as one text message from the pane (JSONL holds the turn until the answer);
    the options stay on the screenshot only. After the answer, the JSONL copies
    are de-duplicated against that message."""

    async def _surface(self, pane: str, window_id: str = "docker:assistant") -> str:
        """Run _surface_ask_question_text against ``pane`` and return the text it
        posted ("" if it posted nothing)."""
        from ccbot.handlers import interactive_ui as iui

        bot = AsyncMock()
        sent = MagicMock()
        sent.message_id = 777
        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(
                iui, "send_with_fallback", AsyncMock(return_value=sent)
            ) as mock_send,
            patch.object(iui, "note_topic_message"),
        ):
            mock_sm.capture_pane = AsyncMock(return_value=pane)
            sess = MagicMock()
            sess.session_id = "sess-abc"
            mock_sm.resolve_session_for_window = AsyncMock(return_value=sess)
            await _surface_ask_question_text(
                bot, chat_id=100, window_id=window_id, ikey=(1, 42), thread_kwargs={}
            )
            if not mock_send.called:
                return ""
            return mock_send.call_args.args[2]

    @pytest.mark.asyncio
    async def test_prose_and_question_surfaced_options_not(self):
        from ccbot.handlers import interactive_ui as iui

        sent_text = await self._surface(_AUQ_PANE_WITH_PROSE)
        assert "Чай — один из самых древних" in sent_text  # prose
        assert "Помимо вкуса" in sent_text  # prose, second paragraph
        assert "Какой чай тебе ближе всего?" in sent_text  # the question
        # Prose then the question (in that order).
        assert sent_text.index("Помимо вкуса") < sent_text.index("Какой чай тебе ближе")
        # The options and the user's prompt above the prose must NOT be there.
        assert "Зелёный" not in sent_text
        assert "Type something" not in sent_text
        assert "Напиши два абзаца" not in sent_text
        # Bookkeeping recorded so the post-answer copies can be de-duped.
        assert iui._pending_auq["sess-abc"] == ((777,), "Какой чай тебе ближе всего?")
        assert iui._auq_text_sent[(1, 42)] == 777

    @pytest.mark.asyncio
    async def test_question_surfaced_when_no_prose(self):
        from ccbot.handlers import interactive_ui as iui

        sent_text = await self._surface(_AUQ_PANE_NO_PROSE)
        assert "минимальный заказ — 10 шт" in sent_text  # the question text
        assert "Берём 10 шт" not in sent_text  # option label, not surfaced
        assert iui._pending_auq["sess-abc"][1].startswith("У варианта 2")

    @pytest.mark.asyncio
    async def test_long_prose_splits_instead_of_bailing(self):
        """A >4096-char preamble used to surface NOTHING (the old length bail) —
        exactly the long-plan/long-explanation case where pre-answer reading
        matters most. Now it splits; all message ids are recorded so the
        post-answer upgrade can clean up the extras."""
        from ccbot.handlers import interactive_ui as iui

        prose_lines = "\n".join(
            "  строка длинной преамбулы номер %03d — много подробностей тут" % i
            for i in range(100)
        )
        pane = (
            "● Начало длинного объяснения перед вопросом.\n"
            f"{prose_lines}\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " ☐ Выбор\n"
            "\n"
            "Какой вариант берём?\n"
            "\n"
            "❯ 1. Первый\n"
            "  2. Второй\n"
            "  3. Type something.\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        bot = AsyncMock()
        ids = iter(range(700, 720))
        sends: list[str] = []

        async def fake_send(bot_, chat_id_, text, **kw):
            sends.append(text)
            m = MagicMock()
            m.message_id = next(ids)
            return m

        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(iui, "send_with_fallback", AsyncMock(side_effect=fake_send)),
            patch.object(iui, "note_topic_message"),
        ):
            mock_sm.capture_pane = AsyncMock(return_value=pane)
            sess = MagicMock()
            sess.session_id = "sess-long"
            mock_sm.resolve_session_for_window = AsyncMock(return_value=sess)
            await _surface_ask_question_text(
                bot, chat_id=100, window_id="@5", ikey=(1, 42), thread_kwargs={}
            )
        assert len(sends) > 1  # split, not dropped
        assert "Какой вариант берём?" in sends[-1]
        msg_ids, question = iui._pending_auq["sess-long"]
        assert len(msg_ids) == len(sends)
        assert msg_ids[0] == 700
        assert question == "Какой вариант берём?"

    @pytest.mark.asyncio
    async def test_home_paths_relativized(self):
        """Absolute home paths lifted off the pane read as clutter (and leak the
        operator's username in screenshots) — rewritten to ``~``."""
        from pathlib import Path

        home = str(Path.home())
        pane = (
            f"● Файл {home}/projects/demo/app.py готов к правке.\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " ☐ Выбор\n"
            "Правим?\n"
            "❯ 1. Да\n"
            "  2. Нет\n"
            "Enter to select · Esc to cancel\n"
        )
        sent_text = await self._surface(pane)
        assert "~/projects/demo/app.py" in sent_text
        assert home not in sent_text

    @pytest.mark.asyncio
    async def test_parse_miss_surfaces_nothing(self):
        from ccbot.handlers import interactive_ui as iui

        bot = AsyncMock()
        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(iui, "send_with_fallback", AsyncMock()) as mock_send,
            patch.object(iui, "note_topic_message"),
        ):
            mock_sm.capture_pane = AsyncMock(return_value="$ just a shell prompt\n$\n")
            await _surface_ask_question_text(
                bot, chat_id=100, window_id="@5", ikey=(1, 0), thread_kwargs={}
            )
            mock_send.assert_not_called()
        # Recorded as "tried, nothing" so the 1 s poll doesn't retry every tick.
        assert iui._auq_text_sent[(1, 0)] == 0
        assert "sess-abc" not in iui._pending_auq


@pytest.mark.usefixtures("_clear_auq_state")
class TestConsumePostAnswerCopies:
    @pytest.mark.asyncio
    async def test_prose_upgrade_keeps_question_and_does_not_evict(self):
        from ccbot.handlers import interactive_ui as iui

        iui._pending_auq["s1"] = ((501,), "Какой вариант берём?")
        bot = AsyncMock()
        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(iui, "safe_edit", AsyncMock()) as mock_edit,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            ok = await consume_pending_prose_upgrade(
                bot,
                "s1",
                user_id=1,
                thread_id=None,
                jsonl_text="Чистый **markdown** prose.",
            )
        assert ok is True
        edited_text = mock_edit.call_args.args[1]
        assert edited_text == "Чистый **markdown** prose.\n\nКакой вариант берём?"
        # NOT evicted — the tool_use that follows the answer does that.
        assert "s1" in iui._pending_auq

    @pytest.mark.asyncio
    async def test_prose_upgrade_deletes_extra_surfaced_messages(self):
        """A multi-message surface (long preamble) upgrades the FIRST message in
        place and drops the extras before the clean re-delivery sends its own
        follow-ups — otherwise the old chunks would sit as duplicates."""
        from ccbot.handlers import interactive_ui as iui

        iui._pending_auq["s1"] = ((501, 502, 503), "Вопрос?")
        bot = AsyncMock()
        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(iui, "safe_edit", AsyncMock()) as mock_edit,
            patch.object(iui, "_safe_delete_message", AsyncMock()) as mock_del,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            ok = await consume_pending_prose_upgrade(
                bot, "s1", user_id=1, thread_id=None, jsonl_text="Чистая проза."
            )
        assert ok is True
        deleted = {c.args[2] for c in mock_del.call_args_list}
        assert deleted == {502, 503}
        # The first message got the in-place upgrade.
        assert mock_edit.call_args.kwargs.get("message_id") == 501

    @pytest.mark.asyncio
    async def test_prose_upgrade_no_pending_returns_false(self):
        bot = AsyncMock()
        ok = await consume_pending_prose_upgrade(
            bot, "unknown", user_id=1, thread_id=None, jsonl_text="x"
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_prose_upgrade_extracts_wide_table_as_image(self):
        """A wide markdown table in the prose above the question is sent as its own
        photo (not inlined): the intro text edits the surfaced message in place,
        the table → image, the trailing prose + question → a new message. This is
        the AskUserQuestion analogue of the normal send path's table rendering."""
        from ccbot.handlers import interactive_ui as iui
        from ccbot.handlers import message_queue as mq

        iui._pending_auq["s1"] = ((501,), "Какой объём выполнить?")
        jsonl_text = (
            "Вот сравнение по весу бандлов:\n\n"
            "| Снять из layout | gzip |\n"
            "|---|---|\n"
            "| fullcalendar.bundle.js + css | ~135 КБ |\n"
            "| prismjs.bundle.js + css | ~11 КБ |\n\n"
            "Итого ~153 КБ с каждой страницы."
        )
        sent = MagicMock()
        sent.message_id = 888
        bot = AsyncMock()
        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(iui, "safe_edit", AsyncMock()) as mock_edit,
            patch.object(
                iui, "send_with_fallback", AsyncMock(return_value=sent)
            ) as mock_send,
            patch.object(iui, "note_topic_message"),
            patch.object(mq, "_send_table_image", AsyncMock()) as mock_img,
            patch.object(mq, "_send_code_file", AsyncMock()) as mock_file,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            ok = await consume_pending_prose_upgrade(
                bot, "s1", user_id=1, thread_id=42, jsonl_text=jsonl_text
            )
        assert ok is True
        # The table left the text and became one photo (no document).
        mock_img.assert_called_once()
        mock_file.assert_not_called()
        grid_text = mock_img.call_args.args[2]
        assert "fullcalendar.bundle.js" in grid_text  # the table data is in the image
        # The intro edits the surfaced message in place; the table is NOT inlined.
        edited_text = mock_edit.call_args.args[1]
        assert "Вот сравнение" in edited_text
        assert "fullcalendar.bundle.js" not in edited_text
        # The trailing prose + the question arrive as a follow-up message.
        trailing = " ".join(c.args[2] for c in mock_send.call_args_list)
        assert "Какой объём выполнить?" in trailing

    @pytest.mark.asyncio
    async def test_prose_upgrade_long_text_splits_instead_of_bailing(self):
        """A >4096-char prose used to fail the single-edit guard and leave the
        box-art pane capture. Now it splits: first chunk edits in place, the rest
        (with the question) is sent as follow-up messages."""
        from ccbot.handlers import interactive_ui as iui

        iui._pending_auq["s1"] = ((501,), "Финальный вопрос?")
        jsonl_text = "А" * 5000  # no table; just longer than 4096
        sent = MagicMock()
        sent.message_id = 888
        bot = AsyncMock()
        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(iui, "safe_edit", AsyncMock()) as mock_edit,
            patch.object(
                iui, "send_with_fallback", AsyncMock(return_value=sent)
            ) as mock_send,
            patch.object(iui, "note_topic_message"),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            ok = await consume_pending_prose_upgrade(
                bot, "s1", user_id=1, thread_id=None, jsonl_text=jsonl_text
            )
        assert ok is True
        mock_edit.assert_called_once()  # first chunk, in place
        assert mock_send.call_count >= 1  # the overflow no longer dropped
        all_text = mock_edit.call_args.args[1] + " ".join(
            c.args[2] for c in mock_send.call_args_list
        )
        assert "Финальный вопрос?" in all_text  # the question survived the split

    def test_tool_use_consume_evicts_and_returns_true_then_false(self):
        from ccbot.handlers import interactive_ui as iui

        iui._pending_auq["s1"] = ((501,), "Q?")
        assert consume_pending_ask_tool_use("s1") is True
        assert "s1" not in iui._pending_auq
        # Second time (or parse-miss case): nothing to suppress.
        assert consume_pending_ask_tool_use("s1") is False

    def test_record_pending_auq_is_capped(self):
        from ccbot.handlers import interactive_ui as iui
        from ccbot.handlers.interactive_ui import _PENDING_AUQ_CAP, _record_pending_auq

        for i in range(_PENDING_AUQ_CAP + 10):
            _record_pending_auq(f"s{i}", (i,), f"q{i}")
        assert len(iui._pending_auq) == _PENDING_AUQ_CAP
        # Oldest evicted, newest kept.
        assert "s0" not in iui._pending_auq
        assert f"s{_PENDING_AUQ_CAP + 9}" in iui._pending_auq


# The ExitPlanMode widget footer as v2.1.3x renders it (real capture, trimmed).
_PLAN_WIDGET_PANE = """\
   Claude has written up a plan and is ready to execute. Would you like to
   proceed?

   ❯ 1. Yes, and use auto mode
     2. Yes, manually approve edits
     4. Tell Claude what to change

   ctrl+g to edit in Vim  ·
                         ~/.claude/plans/test-plan-quiet-otter.md
"""


@pytest.fixture
def _clear_plan_state():
    """Wipe the ExitPlanMode surface bookkeeping before/after each test."""
    from ccbot.handlers.interactive_ui import _pending_plan_sessions, _plan_text_sent

    _plan_text_sent.clear()
    _pending_plan_sessions.clear()
    yield
    _plan_text_sent.clear()
    _pending_plan_sessions.clear()


@pytest.mark.usefixtures("_clear_plan_state")
class TestSurfacePlanText:
    """Before approval, the plan is surfaced from the plan FILE (whose basename
    the widget footer shows); the JSONL copy is held until the user answers and
    is then de-duplicated via consume_pending_plan_text."""

    async def _surface(self, pane: str, plans_dir, window_id: str = "@5"):
        """Run _surface_plan_text against ``pane``; return list of sent texts."""
        from ccbot.handlers import interactive_ui as iui
        from ccbot.handlers.interactive_ui import _surface_plan_text

        bot = AsyncMock()
        sent = MagicMock()
        sent.message_id = 555
        with (
            patch.object(iui, "session_manager") as mock_sm,
            patch.object(
                iui, "send_with_fallback", AsyncMock(return_value=sent)
            ) as mock_send,
            patch.object(iui, "note_topic_message"),
        ):
            mock_sm.plans_dir_for_binding.return_value = plans_dir
            sess = MagicMock()
            sess.session_id = "plan-sess"
            mock_sm.resolve_session_for_window = AsyncMock(return_value=sess)
            await _surface_plan_text(
                bot,
                chat_id=100,
                window_id=window_id,
                ikey=(1, 42),
                thread_kwargs={},
                pane_plain=pane,
            )
            return [c.args[2] for c in mock_send.call_args_list]

    @pytest.mark.asyncio
    async def test_plan_file_content_surfaced(self, tmp_path):
        from ccbot.handlers import interactive_ui as iui

        (tmp_path / "test-plan-quiet-otter.md").write_text(
            "# Plan: split auth.py\n\n## Context\n\nOne module per concern.",
            encoding="utf-8",
        )
        texts = await self._surface(_PLAN_WIDGET_PANE, tmp_path)
        assert len(texts) == 1
        assert "split auth.py" in texts[0]
        assert "One module per concern." in texts[0]
        # Bookkeeping: post-answer JSONL copy will be skipped, once.
        assert iui._plan_text_sent[(1, 42)] == 555
        from ccbot.handlers.interactive_ui import consume_pending_plan_text

        assert consume_pending_plan_text("plan-sess") is True
        assert consume_pending_plan_text("plan-sess") is False

    @pytest.mark.asyncio
    async def test_no_footer_path_fails_open(self, tmp_path):
        from ccbot.handlers import interactive_ui as iui

        texts = await self._surface("Would you like to proceed?\n", tmp_path)
        assert texts == []
        # Recorded as "tried, nothing" so the 1 s poll doesn't retry every tick.
        assert iui._plan_text_sent[(1, 42)] == 0
        assert iui._pending_plan_sessions == {}

    @pytest.mark.asyncio
    async def test_missing_plan_file_fails_open(self, tmp_path):
        from ccbot.handlers import interactive_ui as iui

        # Footer names a file that doesn't exist (yet) → photo-only fallback.
        texts = await self._surface(_PLAN_WIDGET_PANE, tmp_path)
        assert texts == []
        assert iui._plan_text_sent[(1, 42)] == 0

    @pytest.mark.asyncio
    async def test_long_plan_splits_across_messages(self, tmp_path):
        (tmp_path / "test-plan-quiet-otter.md").write_text(
            "# Plan\n\n" + ("шаг плана — подробное описание строки\n" * 300),
            encoding="utf-8",
        )
        texts = await self._surface(_PLAN_WIDGET_PANE, tmp_path)
        assert len(texts) > 1  # not dropped, not truncated
        assert sum("шаг плана" in t for t in texts) >= 2
