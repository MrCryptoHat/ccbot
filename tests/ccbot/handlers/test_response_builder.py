"""Tests for response_builder.build_response_parts."""

from ccbot.handlers.response_builder import build_response_parts
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestBuildResponseParts:
    def test_user_message_has_emoji_prefix(self):
        parts, tables, _ = build_response_parts("hello", is_complete=True, role="user")
        assert len(parts) == 1
        assert "\U0001f464" in parts[0]
        assert tables == []

    def test_user_message_truncated_at_3000_chars(self):
        long_text = "a" * 4000
        parts, _, _ = build_response_parts(long_text, is_complete=True, role="user")
        assert len(parts) == 1
        short_parts, _, _ = build_response_parts(
            "b" * 100, is_complete=True, role="user"
        )
        assert len(parts[0]) < len(long_text)
        assert len(short_parts[0]) < len(parts[0])

    def test_thinking_content_truncated_at_500_chars(self):
        inner = "x" * 800
        text = f"{EXP_START}{inner}{EXP_END}"
        parts, _, _ = build_response_parts(
            text, is_complete=True, content_type="thinking"
        )
        assert len(parts) == 1
        assert "truncated" in parts[0].lower()

    def test_plain_text_single_part(self):
        parts, _, _ = build_response_parts("short text", is_complete=True)
        assert len(parts) == 1

    def test_plain_text_multi_part_has_page_suffix(self):
        long_text = "\n".join(f"line {i} " + "padding" * 50 for i in range(200))
        parts, _, _ = build_response_parts(long_text, is_complete=True)
        assert len(parts) > 1
        assert "1/" in parts[0]

    def test_expandable_quote_stays_atomic(self):
        inner = "thought " * 100
        text = f"{EXP_START}{inner}{EXP_END}"
        parts, _, _ = build_response_parts(
            text, is_complete=False, content_type="thinking"
        )
        assert len(parts) == 1

    def test_thinking_has_prefix(self):
        parts, _, _ = build_response_parts(
            "some thought", is_complete=True, content_type="thinking"
        )
        assert len(parts) == 1
        assert "Thinking" in parts[0]

    def test_assistant_text_no_prefix(self):
        parts, _, _ = build_response_parts(
            "hello world", is_complete=True, content_type="text", role="assistant"
        )
        assert len(parts) == 1
        assert "\U0001f464" not in parts[0]
        assert "Thinking" not in parts[0]

    def test_narrow_table_inlined_as_code_block(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        parts, tables, _ = build_response_parts(text, is_complete=True)
        assert tables == []
        joined = "\n".join(parts)
        assert "```" in joined
        assert "\x00" not in joined  # no placeholder for a narrow table

    def test_wide_table_extracted_to_image_with_surrounding_text(self):
        wide_row = "| Локер/покупки | медленно | вкладка Покупки, загрузки агента |"
        text = (
            "Вот разбор ситуации:\n\n"
            "| Индекс | Меняется | Другие |\n"
            "|---|---|---|\n"
            f"{wide_row}\n\n"
            "А это идёт после таблицы."
        )
        parts, tables, _ = build_response_parts(text, is_complete=True)
        assert len(tables) == 1
        joined = "\n".join(parts)
        # Placeholder sits between the before/after prose, in source order
        assert "\x00CCBOT_IMG:0\x00" in joined
        before, after = joined.split("\x00CCBOT_IMG:0\x00")
        assert "Вот разбор" in before
        assert "после таблицы" in after
        # The extracted table text is aligned (header present, no pipes)
        assert "Индекс" in tables[0]
        assert "|" not in tables[0]
