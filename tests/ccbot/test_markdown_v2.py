"""Tests for Markdown → Telegram MarkdownV2 conversion."""

import pytest

from ccbot.markdown_v2 import (
    TABLE_IMAGE_MIN_WIDTH,
    _escape_mdv2,
    convert_markdown,
    convert_markdown_tables,
    fence_bare_box_art,
    render_tables_for_chat,
)
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestEscapeMdv2:
    @pytest.mark.parametrize(
        "input_text,expected",
        [
            (
                "_*[]()~>#+\\-=|{}.!",
                "\\_\\*\\[\\]\\(\\)\\~\\>\\#\\+\\\\\\-\\=\\|\\{\\}\\.\\!",
            ),
            ("hello world 123", "hello world 123"),
            ("", ""),
        ],
        ids=["special-chars", "alphanumeric-unchanged", "empty-string"],
    )
    def test_escape(self, input_text: str, expected: str) -> None:
        assert _escape_mdv2(input_text) == expected


class TestConvertMarkdown:
    def test_plain_text(self) -> None:
        result = convert_markdown("hello world")
        assert "hello world" in result

    def test_bold(self) -> None:
        result = convert_markdown("**bold text**")
        assert "*bold text*" in result
        assert "**bold text**" not in result

    def test_code_block_preserved(self) -> None:
        result = convert_markdown("```python\nprint('hi')\n```")
        assert "```" in result
        assert "print" in result

    def test_expandable_quote_sentinels(self) -> None:
        text = f"{EXP_START}quoted content{EXP_END}"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert ">quoted content||" in result

    def test_mixed_text_and_expandable_quote(self) -> None:
        text = f"before {EXP_START}inside quote{EXP_END} after"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert ">inside quote||" in result
        assert "before" in result
        assert "after" in result


class TestConvertMarkdownTables:
    def test_table_becomes_aligned_code_block(self) -> None:
        text = (
            "| Изменение | Чинит | Риск |\n"
            "|---|---|---|\n"
            "| A | #2 | низкий |\n"
            "| Гибрид-вердикт | #1,#3 | средний |"
        )
        result = convert_markdown_tables(text)
        lines = result.split("\n")
        assert lines[0] == "```"
        assert lines[-1] == "```"
        # No card-style key:value output anymore
        assert "**Изменение**" not in result
        assert "────" not in result
        # Columns padded to a common width → header/data starts line up
        header, first_row = lines[1], lines[2]
        assert header.index("Чинит") == first_row.index("#2")
        # Last column carries no trailing padding
        assert lines[2] == lines[2].rstrip()

    def test_ragged_row_padded(self) -> None:
        text = "| A | B | C |\n|---|---|---|\n| x |"
        result = convert_markdown_tables(text)
        # Missing cells don't raise and the fence stays well-formed
        assert result.startswith("```\n")
        assert result.endswith("\n```")

    def test_idempotent_skips_existing_code_block(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        once = convert_markdown_tables(text)
        twice = convert_markdown_tables(once)
        assert once == twice

    def test_table_inside_code_block_untouched(self) -> None:
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        assert convert_markdown_tables(text) == text


class TestRenderTablesForChat:
    def test_narrow_table_stays_inline_code_block(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        out, tables, _ = render_tables_for_chat(text)
        assert tables == []
        assert out.startswith("```\n")
        assert "\x00" not in out

    def test_wide_table_extracted_with_placeholder(self) -> None:
        wide = "a" * (TABLE_IMAGE_MIN_WIDTH + 10)
        text = f"before\n\n| Col | Other |\n|---|---|\n| {wide} | x |\n\nafter"
        out, tables, _ = render_tables_for_chat(text)
        assert len(tables) == 1
        assert "\x00CCBOT_IMG:0\x00" in out
        before, after = out.split("\x00CCBOT_IMG:0\x00")
        assert "before" in before
        assert "after" in after
        # The original pipe markdown rides along for the /tables=rich path
        # (an ImageBlock — still a str carrying the grid text for the PNG).
        md = getattr(tables[0], "rich_markdown", "")
        assert md.startswith("| Col | Other |")
        assert f"| {wide} | x |" in md
        # Extracted text is aligned monospace (no pipe syntax, header kept)
        assert "Col" in tables[0]
        assert "|" not in tables[0]
        assert "```" not in tables[0]

    def test_no_table_returns_text_unchanged(self) -> None:
        text = "just some text\nwith no table"
        assert render_tables_for_chat(text) == (text, [], [])

    def test_two_wide_tables_get_distinct_indices(self) -> None:
        wide = "z" * (TABLE_IMAGE_MIN_WIDTH + 5)
        one = f"| H | K |\n|---|---|\n| {wide} | a |"
        text = f"{one}\n\nmiddle\n\n{one}"
        out, tables, _ = render_tables_for_chat(text)
        assert len(tables) == 2
        assert "\x00CCBOT_IMG:0\x00" in out
        assert "\x00CCBOT_IMG:1\x00" in out

    def test_wide_box_art_tree_becomes_image(self) -> None:
        pad = "x" * TABLE_IMAGE_MIN_WIDTH
        text = f"Структура:\n\n```\nroot/\n├── src/  # {pad}\n└── tests/\n```\n\nпосле."
        out, tables, _ = render_tables_for_chat(text)
        assert len(tables) == 1
        assert "\x00CCBOT_IMG:0\x00" in out
        # The image text is the tree verbatim — no grid border added
        assert "├── src/" in tables[0]
        assert "┌" not in tables[0]
        # Box-art is drawn art — never delivered as a rich table.
        assert getattr(tables[0], "rich_markdown", "") == ""
        # Surrounding prose preserved in order
        before, after = out.split("\x00CCBOT_IMG:0\x00")
        assert "Структура" in before
        assert "после" in after

    def test_narrow_box_art_stays_code_block(self) -> None:
        text = "```\nroot/\n├── a\n└── b\n```"
        out, tables, _ = render_tables_for_chat(text)
        assert tables == []
        assert "```" in out
        assert "├── a" in out

    def test_wide_plain_indent_dir_tree_becomes_image(self) -> None:
        tree = (
            "myapp/\n"
            "  core/                 # reusable engine\n"
            "    session.py          # auth, rate-limit, backoff\n"
            "    store.py            # SQLite access\n"
            "  sources/  a.py  b.py  # one package per provider\n"
            "  bootstrap.sh          # clone + restore the database\n"
        )
        text = f"Структура:\n\n```\n{tree}```\n\nдальше."
        out, images, files = render_tables_for_chat(text)
        assert files == []
        assert len(images) == 1
        assert "\x00CCBOT_IMG:0\x00" in out
        assert "session.py" in images[0]
        # Drawn art — never delivered as a rich table.
        assert getattr(images[0], "rich_markdown", "") == ""
        before, after = out.split("\x00CCBOT_IMG:0\x00")
        assert "Структура" in before
        assert "дальше" in after

    def test_narrow_dir_tree_stays_inline(self) -> None:
        text = "```\napp/\n  a.py\n  b.py\n```"
        out, images, _ = render_tables_for_chat(text)
        assert images == []
        assert "```" in out

    def test_lang_tagged_fence_never_tree_checked(self) -> None:
        # Same shape as a tree, but tagged python → copyable code, inline.
        pad = "x" * TABLE_IMAGE_MIN_WIDTH
        text = f"```python\npkg/\n  mod.py  # {pad}\n  sub/\n```"
        _, images, _ = render_tables_for_chat(text)
        assert images == []

    def test_flat_path_list_stays_inline(self) -> None:
        pad = "y" * TABLE_IMAGE_MIN_WIDTH
        text = f"```\nsrc/a.py  # {pad}\nsrc/b.py\nsrc/c.py\ntests/d.py\n```"
        _, images, _ = render_tables_for_chat(text)
        assert images == []  # one indent depth — a copyable list, not a tree

    def test_code_in_bare_fence_stays_inline(self) -> None:
        pad = "z" * TABLE_IMAGE_MIN_WIDTH
        code = f'def main():  # {pad}\n    print("hi")\n    return 1'
        _, images, _ = render_tables_for_chat(f"```\n{code}\n```")
        assert images == []  # operators/quotes fail the tree-line match

    def test_plain_wide_code_block_untouched(self) -> None:
        wide_line = "result = compute(" + "a" * 60 + ")"
        text = f"```python\n{wide_line}\n```"
        out, tables, _ = render_tables_for_chat(text)
        assert tables == []  # code is for copying — never imaged
        assert wide_line in out
        assert "```python" in out

    def test_code_block_round_trips_through_convert(self) -> None:
        text = "before\n\n```python\nx = [1, 2, 3]\n```\n\nafter"
        # No pipe table, no box-art → convert_markdown_tables is a no-op
        assert convert_markdown_tables(text) == text

    def test_long_code_block_becomes_file(self) -> None:
        body = "\n".join(f"line_{i} = {i}" for i in range(80))  # > 50 lines
        text = f"Вот код:\n\n```python\n{body}\n```\n\nготово."
        out, images, files = render_tables_for_chat(text)
        assert images == []
        assert len(files) == 1
        filename, content = files[0]
        assert filename == "snippet.py"
        assert "line_0 = 0" in content
        assert "```" not in content  # raw code, no fence
        before, after = out.split("\x00CCBOT_FILE:0\x00")
        assert "Вот код" in before
        assert "готово" in after

    def test_short_code_block_stays_inline(self) -> None:
        text = "```python\nx = 1\ny = 2\n```"
        out, images, files = render_tables_for_chat(text)
        assert files == []
        assert images == []
        assert "x = 1" in out
        assert "```python" in out

    def test_long_code_filename_from_fence_lang(self) -> None:
        for lang, ext in [("js", "js"), ("bash", "sh"), ("", "txt")]:
            body = "\n".join(f"x{i}" for i in range(60))
            _, _, files = render_tables_for_chat(f"```{lang}\n{body}\n```")
            assert files[0][0] == f"snippet.{ext}"


class TestFenceBareBoxArt:
    """Pane-lifted text (AskUserQuestion prose surfacing): the TUI renders
    markdown, so a drawn table arrives as bare box-art lines with the fences
    stripped — fence_bare_box_art re-creates the block boundary so
    render_tables_for_chat images the wide ones instead of letting a phone
    wrap them to soup."""

    # The 2026-07-18 live case: a ┌─┬─┐ table drawn in the prose above the
    # question, delivered as plain text → every border line wrapped to soup.
    _BOX_TABLE = (
        "┌─────────────────────────────┬──────────┬──────────────┐\n"
        "│             Что             │ Осталось │ Готово через │\n"
        "├─────────────────────────────┼──────────┼──────────────┤\n"
        "│ Ниши (Thai/ME/Indian/лапша) │ 2 118    │ ~30 мин      │\n"
        "│ Полный хвост (все 48k)      │ 45 387   │ ~11 ч        │\n"
        "└─────────────────────────────┴──────────┴──────────────┘"
    )

    def test_bare_box_table_gets_fenced_and_imaged(self) -> None:
        text = f"Смотри по приоритетам:\n\n{self._BOX_TABLE}\n\nЧто делаем?"
        fenced = fence_bare_box_art(text)
        assert fenced.count("```") == 2
        # Content is preserved verbatim — only fence lines are added.
        assert fenced.replace("```\n", "").replace("\n```", "") == text
        # End-to-end: the wide drawn table leaves the text as one image.
        out, images, files = render_tables_for_chat(fenced)
        assert len(images) == 1
        assert files == []
        assert "Ниши (Thai/ME/Indian/лапша)" in str(images[0])
        assert "┌" not in out  # no box-art left inline
        assert "Смотри по приоритетам:" in out
        assert "Что делаем?" in out

    def test_single_stray_box_char_line_stays_prose(self) -> None:
        text = "Первая строка ─ с тире-псевдографикой.\nОбычная вторая строка."
        assert fence_bare_box_art(text) == text

    def test_no_box_art_is_untouched(self) -> None:
        text = "Просто текст.\n\nИ ещё абзац — с обычным тире."
        assert fence_bare_box_art(text) is text

    def test_existing_fences_not_double_fenced(self) -> None:
        text = "```\n├── a.py\n└── b.py\n```"
        assert fence_bare_box_art(text) == text

    def test_two_line_tree_is_fenced(self) -> None:
        text = "Структура:\n├── app.py\n└── tests/"
        fenced = fence_bare_box_art(text)
        assert fenced.split("\n")[1] == "```"
        assert fenced.count("```") == 2
