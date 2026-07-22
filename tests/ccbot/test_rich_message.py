"""Tests for rich_message.flatten_rich_message (Bot API 10.1 inbound blocks).

Pins the markdown the agent receives for each block kind, and the PTB seam:
an unknown Message field (rich_message) must surface in api_kwargs so the
rich_message_filter can catch what filters.TEXT never will (text is None).
Plus the outbound is_rich_safe gate (what may go through sendRichMessage).
"""

from ccbot.rich_message import flatten_rich_message, is_rich_safe, normalize_tables


def _msg(*blocks) -> dict:
    return {"blocks": list(blocks)}


class TestRichText:
    def test_plain_and_nested_marks(self):
        rich = _msg(
            {
                "type": "paragraph",
                "text": [
                    "plain ",
                    {"type": "bold", "text": "fat"},
                    " and ",
                    {"type": "italic", "text": {"type": "code", "text": "x"}},
                ],
            }
        )
        assert flatten_rich_message(rich) == "plain **fat** and *`x`*"

    def test_url_with_distinct_text(self):
        rich = _msg(
            {
                "type": "paragraph",
                "text": {"type": "url", "text": "site", "url": "https://e.com"},
            }
        )
        assert flatten_rich_message(rich) == "[site](https://e.com)"

    def test_unknown_inline_type_keeps_text(self):
        rich = _msg(
            {
                "type": "paragraph",
                "text": {"type": "hologram", "text": "still here"},
            }
        )
        assert flatten_rich_message(rich) == "still here"


class TestBlocks:
    def test_heading_size(self):
        rich = _msg({"type": "heading", "text": "Title", "size": 3})
        assert flatten_rich_message(rich) == "### Title"

    def test_pre_with_language(self):
        rich = _msg({"type": "pre", "text": "print(1)", "language": "python"})
        assert flatten_rich_message(rich) == "```python\nprint(1)\n```"

    def test_table_to_pipe_markdown(self):
        rich = _msg(
            {
                "type": "table",
                "cells": [
                    [
                        {"text": "поле", "is_header": True},
                        {"text": "поле 2", "is_header": True},
                    ],
                    [{"text": "ответ"}, {"text": "ответ 2"}],
                ],
            }
        )
        assert flatten_rich_message(rich) == (
            "| поле | поле 2 |\n|---|---|\n| ответ | ответ 2 |"
        )

    def test_table_escapes_pipes_and_newlines(self):
        rich = _msg(
            {"type": "table", "cells": [[{"text": "a|b"}, {"text": "two\nlines"}]]}
        )
        assert flatten_rich_message(rich) == "| a\\|b | two lines |\n|---|---|"

    def test_list_checkboxes_and_labels(self):
        rich = _msg(
            {
                "type": "list",
                "items": [
                    {
                        "label": "1.",
                        "blocks": [{"type": "paragraph", "text": "first"}],
                    },
                    {
                        "has_checkbox": True,
                        "is_checked": True,
                        "blocks": [{"type": "paragraph", "text": "done"}],
                    },
                    {
                        "has_checkbox": True,
                        "blocks": [{"type": "paragraph", "text": "todo"}],
                    },
                ],
            }
        )
        assert flatten_rich_message(rich) == "1. first\n- [x] done\n- [ ] todo"

    def test_blockquote_with_credit(self):
        rich = _msg(
            {
                "type": "blockquote",
                "blocks": [{"type": "paragraph", "text": "wisdom"}],
                "credit": "sage",
            }
        )
        assert flatten_rich_message(rich) == "> wisdom\n> — sage"

    def test_media_block_becomes_stub(self):
        rich = _msg({"type": "photo", "caption": {"text": "sunset"}})
        assert flatten_rich_message(rich) == "(photo: sunset)"

    def test_unknown_block_salvages_children(self):
        rich = _msg(
            {
                "type": "future_widget",
                "blocks": [{"type": "paragraph", "text": "inner"}],
            }
        )
        assert flatten_rich_message(rich) == "inner"

    def test_blocks_joined_with_blank_lines(self):
        rich = _msg(
            {"type": "heading", "text": "H", "size": 1},
            {"type": "paragraph", "text": "p1"},
            {"type": "divider"},
        )
        assert flatten_rich_message(rich) == "# H\n\np1\n\n---"


class TestRobustness:
    def test_non_dict_input(self):
        assert flatten_rich_message(None) == ""
        assert flatten_rich_message("junk") == ""

    def test_empty_blocks(self):
        assert flatten_rich_message({"blocks": []}) == ""

    def test_recursion_bounded(self):
        node: dict = {"type": "bold", "text": "deep"}
        for _ in range(100):
            node = {"type": "bold", "text": node}
        rich = _msg({"type": "paragraph", "text": node})
        # Must not raise; content beyond the depth cap may be dropped.
        flatten_rich_message(rich)


class TestIsRichSafe:
    """Outbound gate: Telegram's rich parser desyncs on emphasis markers
    inside inline code spans (accepted-but-mangled, so the send-failure
    fallback never fires — the gate is the only protection)."""

    def test_plain_prose_is_safe(self):
        assert is_rich_safe("Just **bold** and *italic* prose with `code`.")

    def test_star_inside_inline_code_is_unsafe(self):
        # The live-regression SHAPE (text rebuilt synthetically): * inside the
        # span pairs as italic across the backtick, shifting every later code
        # span by one.
        text = (
            "**example-app: 443 файла `data/cache/*.json` + "
            "изменённая `state.db` не в git.** Надо решить: коммитить "
            "`data/` в репо, завести бэкап-поток в `_backups/`."
        )
        assert not is_rich_safe(text)

    def test_underscore_inside_inline_code_is_unsafe(self):
        assert not is_rich_safe("см. каталог `_backups/` на диске")

    def test_emphasis_only_in_fenced_block_is_safe(self):
        text = "Пример:\n```python\nx = a * b\nname_with_underscore = 1\n```\nГотово."
        assert is_rich_safe(text)

    def test_inline_code_without_emphasis_chars_is_safe(self):
        assert is_rich_safe("запусти `uv run ruff check` и `pytest -q`")

    def test_risky_span_outside_fence_still_caught(self):
        text = "```\nsafe * here\n```\nа вот `glob *.json` снаружи"
        assert not is_rich_safe(text)


class TestPtbApiKwargsSeam:
    """PTB parses unknown Message fields into api_kwargs — the seam both the
    filter and the handler rely on. If a PTB upgrade starts consuming
    rich_message natively, this pins the moment the seam moves."""

    def _ptb_message(self):
        from telegram import Message

        return Message.de_json(
            {
                "message_id": 1,
                "date": 1752655500,
                "chat": {"id": -100123, "type": "supergroup", "title": "g"},
                "from": {"id": 42, "is_bot": False, "first_name": "U"},
                "rich_message": {"blocks": [{"type": "paragraph", "text": "hello"}]},
            },
            bot=None,
        )

    def test_rich_message_lands_in_api_kwargs(self):
        msg = self._ptb_message()
        assert msg.text is None
        rich = msg.api_kwargs.get("rich_message")
        assert flatten_rich_message(rich) == "hello"

    def test_filter_matches(self):
        from ccbot.bot import rich_message_filter

        assert rich_message_filter.filter(self._ptb_message()) is True


class TestNormalizeTables:
    """Telegram's rich parser follows GFM: a table cannot interrupt a
    paragraph. Agents write the label-then-table shape all the time, and
    without a blank line the rows arrive as one line of pipe soup (hit live).
    """

    _TABLE = "| Item | kcal |\n|---|---|\n| Soup | 280 |"

    def test_blank_line_inserted_after_paragraph(self):
        out = normalize_tables("**Totals:**\n" + self._TABLE)
        assert out == "**Totals:**\n\n" + self._TABLE

    def test_already_separated_is_untouched(self):
        md = "**Totals:**\n\n" + self._TABLE
        assert normalize_tables(md) == md

    def test_table_at_start_is_untouched(self):
        assert normalize_tables(self._TABLE) == self._TABLE

    def test_every_table_in_the_message(self):
        md = "**Totals:**\n" + self._TABLE + "\n\nprose\n**Snacks:**\n" + self._TABLE
        assert normalize_tables(md).count("\n\n| Item") == 2

    def test_table_inside_code_fence_is_untouched(self):
        md = "Example:\n```\n| a | b |\n|---|---|\n```"
        assert normalize_tables(md) == md

    def test_alignment_row_variants(self):
        for delim in ("| :--- | ---: |", "|:-:|:-:|", "--- | ---"):
            md = "caption\n| a | b |\n" + delim
            assert normalize_tables(md) == "caption\n\n| a | b |\n" + delim
