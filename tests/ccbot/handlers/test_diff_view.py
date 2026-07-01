"""Tests for handlers.diff_view — native-pane diff-block extraction + dedup."""

from pathlib import Path

import pytest

from ccbot.handlers import diff_view

FIXTURE = Path(__file__).parent / "fixtures" / "edit_block.ansi"


@pytest.fixture(autouse=True)
def _clear_seen():
    diff_view._seen.clear()
    yield
    diff_view._seen.clear()


@pytest.fixture
def edit_pane() -> str:
    """A real captured pane: one ● Update block, a blank line, then a prose ● line."""
    return FIXTURE.read_text()


class TestExtractDiffBlocks:
    def test_finds_the_update_block(self, edit_pane):
        blocks = diff_view.extract_diff_blocks(edit_pane)
        assert len(blocks) == 1
        clean = diff_view._clean(blocks[0])
        assert clean.startswith("● Update(")
        # Path survives inside the (OSC-stripped) header.
        assert "scratch.txt" in clean

    def test_block_stops_before_trailing_prose(self, edit_pane):
        # The fixture's last line is a prose "● Now immediately…" bullet — it must
        # NOT be swallowed into the diff block (stop at the blank line before it).
        block = diff_view.extract_diff_blocks(edit_pane)[0]
        assert "Now immediately" not in diff_view._clean(block)
        assert "line four" in diff_view._clean(block)

    def test_block_includes_red_and_green_lines(self, edit_pane):
        clean = diff_view._clean(diff_view.extract_diff_blocks(edit_pane)[0])
        assert "2 -line two" in clean
        assert "2 +line two CHANGED" in clean

    def test_no_blocks_in_plain_pane(self):
        assert (
            diff_view.extract_diff_blocks("just a shell prompt\n$ ls\nfoo bar\n") == []
        )

    def test_header_without_gutter_is_skipped(self):
        # An errored edit shows the header + an error line, no +/- gutter → skip.
        pane = "● Update(/x.py)\n  ⎿  Error: String to replace not found\n\n"
        assert diff_view.extract_diff_blocks(pane) == []

    def test_two_blocks(self):
        pane = (
            "● Update(/a.py)\n"
            "  ⎿  Added 1 lines, removed 1 line\n"
            "      1 -old a\n"
            "      1 +new a\n"
            "\n"
            "● Update(/b.py)\n"
            "      2 +added b\n"
            "\n"
        )
        blocks = diff_view.extract_diff_blocks(pane)
        assert len(blocks) == 2
        assert "/a.py" in diff_view._clean(blocks[0])
        assert "/b.py" in diff_view._clean(blocks[1])

    def test_prose_bullet_is_not_a_header(self):
        pane = "● Here is my explanation of the change.\nsome more text\n"
        assert diff_view.extract_diff_blocks(pane) == []


class TestDedup:
    def test_mark_and_seen(self):
        assert not diff_view._already_seen("@1", "h1")
        diff_view._mark("@1", "h1")
        assert diff_view._already_seen("@1", "h1")

    def test_seen_is_per_window(self):
        diff_view._mark("@1", "h1")
        assert not diff_view._already_seen("@2", "h1")

    def test_reset_clears_window(self):
        diff_view._mark("@1", "h1")
        diff_view.reset("@1")
        assert not diff_view._already_seen("@1", "h1")

    def test_lru_evicts_oldest(self):
        for i in range(diff_view._SEEN_MAX + 5):
            diff_view._mark("@1", f"h{i}")
        # Oldest few evicted, newest retained.
        assert not diff_view._already_seen("@1", "h0")
        assert diff_view._already_seen("@1", f"h{diff_view._SEEN_MAX + 4}")

    def test_hash_ignores_ansi(self):
        # Same visible content, different SGR codes → same hash (dedup survives
        # cosmetic color drift between captures).
        a = "\x1b[31m● Update(/x)\x1b[0m\n 1 +y"
        b = "\x1b[32m● Update(/x)\x1b[0m\n 1 +y"
        assert diff_view._hash(a) == diff_view._hash(b)
