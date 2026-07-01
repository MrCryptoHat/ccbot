"""Tests for askquestion_parser — pulling AskUserQuestion + prose from a pane.

Claude Code holds the whole AskUserQuestion turn (prose, question, options) out
of JSONL until the user answers, so to show it before the answer ccbot parses
the rendered tmux pane. These tests pin the current Claude Code v2.1.x layout —
a TUI redesign should break them rather than silently regress the feature. The
``COFFEE_PANE`` fixture is a real capture (trimmed).
"""

from ccbot.handlers.askquestion_parser import parse_ask_question

# A real capture: assistant wrote 3 paragraphs of prose, then AskUserQuestion
# with 3 real options (+ Claude Code's always-present "Type something." /
# "Chat about this"). The pane carries earlier transcript above the prose.
COFFEE_PANE = """\
● Ran 1 stop hook (ctrl+o to expand)
  ⎿  Stop hook error: Failed with non-blocking status code: /bin/sh: 1:
  /home/user/agents/demo/.claude/hooks/check.sh: Permission denied

✻ Cogitated for 29s

❯ Тест рендера для ccbot: напиши 2-3 абзаца текста про что угодно — например про
  кофе — а в конце задай мне вопрос через инструмент AskUserQuestion.

● Окей, рендер-тест. Текст ради текста — про кофе.

  Кофе — это, по сути, самый массовый легальный психостимулятор на планете, и при
  этом его готовят кустарно почти все. Просто кофе прощает безразличие: даже плохо
  сваренный он даёт кофеин, а значит, «работает».

  Если копнуть, главная переменная — не сорт и даже не обжарка, а свежесть помола
  и температура воды. Третье — соотношение: грубо 60 г зерна на литр воды.

  И ещё момент: кофе — это во многом про паузу, а не про бодрость. Убери ритуал —
  и останется просто горькая вода с эффектом, которую можно заменить таблеткой.
────────────────────────────────────────────────────────────────────────────────
 ☐ Кофе

А ты сам как пьёшь кофе?

❯ 1. На автомате
     Растворимый или капсула, главное — чтобы быстро и работало. Вкус вторичен.
  2. С заморочкой
     Свежее зерно, весы, контроль температуры, любимый метод заваривания. Это ритуал.
  3. Не пью вообще
     Чай, вода или ничего. Кофеин не нужен или не заходит.
  4. Type something.
────────────────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""


class TestParseAskQuestion:
    def test_coffee_pane_full_parse(self):
        parsed = parse_ask_question(COFFEE_PANE)
        assert parsed is not None
        assert parsed.question == "А ты сам как пьёшь кофе?"
        # Three real options; the meta ones are dropped.
        labels = [label for label, _ in parsed.options]
        assert labels == ["На автомате", "С заморочкой", "Не пью вообще"]
        # Descriptions came through.
        assert parsed.options[0][1].startswith("Растворимый или капсула")
        assert (
            parsed.options[2][1]
            == "Чай, вода или ничего. Кофеин не нужен или не заходит."
        )
        # The cursor "❯" was on option 1.
        assert parsed.cursor_label == "На автомате"

    def test_coffee_pane_prose(self):
        parsed = parse_ask_question(COFFEE_PANE)
        assert parsed is not None
        # First line of the assistant block, bullet stripped.
        assert parsed.prose.startswith(
            "Окей, рендер-тест. Текст ради текста — про кофе."
        )
        # Body paragraphs survived, continuation indents stripped.
        assert (
            "Кофе — это, по сути, самый массовый легальный психостимулятор"
            in parsed.prose
        )
        assert "которую можно заменить таблеткой." in parsed.prose
        # The transcript above the prose (the user prompt, the stop-hook noise)
        # must NOT leak in.
        assert "Тест рендера для ccbot" not in parsed.prose
        assert "stop hook" not in parsed.prose.lower()
        # Paragraph blanks preserved → blank line between paragraphs.
        assert "\n\n" in parsed.prose

    def test_no_prose_when_user_prompt_above_widget(self):
        # Agent called AskUserQuestion with no preceding text — the thing above
        # the widget border is the user's own prompt.
        pane = """\
❯ Спроси меня что-нибудь короткое.
────────────────────────────────────────────────────────────────────────────────
 ☐ Q
Любишь чай?
❯ 1. Да
  2. Нет
  3. Type something.
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
        parsed = parse_ask_question(pane)
        assert parsed is not None
        assert parsed.prose == ""
        assert parsed.question == "Любишь чай?"
        assert [label for label, _ in parsed.options] == ["Да", "Нет"]

    def test_no_prose_when_tool_call_above_widget(self):
        pane = """\
● Bash(echo hi)
  ⎿  hi
────────────────────────────────────────────────────────────────────────────────
 ☐ Q
Дальше?
❯ 1. Да
  2. Нет
Enter to select · Esc to cancel
"""
        parsed = parse_ask_question(pane)
        assert parsed is not None
        assert parsed.prose == ""
        assert parsed.question == "Дальше?"

    def test_multiline_option_description(self):
        pane = """\
────────────────────────────────────────────────────────────────────────────────
 ☐ Q
Какой вариант?
❯ 1. Первый
     Длинное описание которое не влезло в одну строку терминала и было перенесено
     на следующую строку рендерером.
  2. Второй
     Коротко.
Enter to select · Esc to cancel
"""
        parsed = parse_ask_question(pane)
        assert parsed is not None
        assert parsed.options[0][0] == "Первый"
        assert parsed.options[0][1] == (
            "Длинное описание которое не влезло в одну строку терминала и было перенесено "
            "на следующую строку рендерером."
        )

    def test_no_widget_returns_none(self):
        assert parse_ask_question("$ echo hello\nhello\n$\n") is None
        assert parse_ask_question("") is None
        # An ExitPlanMode-ish prompt is not an AskUserQuestion → None.
        assert (
            parse_ask_question("Would you like to proceed?\n  ctrl-g to edit\n") is None
        )

    def test_question_without_header_line(self):
        # Some renders may not show the "☐" tab line; if there's no tab anchor
        # we currently can't locate the widget → None (fails open, photo shows it).
        pane = """\
Какой вариант?
❯ 1. А
  2. Б
Enter to select · Esc to cancel
"""
        assert parse_ask_question(pane) is None
