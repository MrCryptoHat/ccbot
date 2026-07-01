"""Tests for voice/hints.py — segment parser and tag stripping."""

from ccbot.voice.hints import split_voice_segments, strip_output_tags


class TestSplitVoiceSegments:
    def test_plain_text_is_single_voice_segment(self):
        assert split_voice_segments("привет как дела") == [("voice", "привет как дела")]

    def test_empty_input_returns_empty(self):
        assert split_voice_segments("") == []

    def test_whitespace_only_returns_empty(self):
        assert split_voice_segments("   \n  ") == []

    def test_single_chat_block(self):
        result = split_voice_segments(
            "вот ссылка [chat]https://example.com[/chat] посмотри"
        )
        assert result == [
            ("voice", "вот ссылка"),
            ("chat", "https://example.com"),
            ("voice", "посмотри"),
        ]

    def test_chat_at_start(self):
        result = split_voice_segments("[chat]foo[/chat] дальше голосом")
        assert result == [
            ("chat", "foo"),
            ("voice", "дальше голосом"),
        ]

    def test_chat_at_end(self):
        result = split_voice_segments("голосом [chat]foo[/chat]")
        assert result == [
            ("voice", "голосом"),
            ("chat", "foo"),
        ]

    def test_entire_message_is_chat(self):
        result = split_voice_segments("[chat]https://example.com[/chat]")
        assert result == [("chat", "https://example.com")]

    def test_interleaved_voice_chat(self):
        result = split_voice_segments("а [chat]x[/chat] б [chat]y[/chat] в")
        assert result == [
            ("voice", "а"),
            ("chat", "x"),
            ("voice", "б"),
            ("chat", "y"),
            ("voice", "в"),
        ]

    def test_unclosed_chat_consumes_to_end(self):
        result = split_voice_segments("hey [chat]forgot to close")
        assert result == [
            ("voice", "hey"),
            ("chat", "forgot to close"),
        ]

    def test_empty_chat_block_is_dropped(self):
        result = split_voice_segments("a [chat][/chat] b")
        assert result == [
            ("voice", "a"),
            ("voice", "b"),
        ]

    def test_whitespace_only_chat_block_is_dropped(self):
        result = split_voice_segments("a [chat]   [/chat] b")
        assert result == [
            ("voice", "a"),
            ("voice", "b"),
        ]

    def test_case_insensitive_tags(self):
        result = split_voice_segments("x [CHAT]y[/Chat] z")
        assert result == [
            ("voice", "x"),
            ("chat", "y"),
            ("voice", "z"),
        ]

    def test_stray_close_tag_stays_in_voice_segment(self):
        # A dangling [/chat] outside any open block remains inline;
        # strip_output_tags removes it if we ever send that chunk as text.
        result = split_voice_segments("hello [/chat] world")
        assert result == [("voice", "hello [/chat] world")]

    def test_balanced_nesting_literal_inner_pair(self):
        # A full [chat]...[/chat] pair inside an open block is kept as
        # literal content of the outer block — balanced matching.
        result = split_voice_segments("a [chat]outer [chat]inner[/chat] tail[/chat] b")
        assert result == [
            ("voice", "a"),
            ("chat", "outer [chat]inner[/chat] tail"),
            ("voice", "b"),
        ]

    def test_literal_chat_pair_in_block_survives(self):
        # Regression: a commit message mentioning [chat]...[/chat] inside
        # a git log output used to close the outer block at the first
        # inner [/chat], leaking the tail into the next voice segment.
        result = split_voice_segments(
            "ага. [chat]log\nfeat(voice): [chat]...[/chat] protocol\n"
            "hash docs: docker agents[/chat] готово"
        )
        assert result == [
            ("voice", "ага."),
            (
                "chat",
                "log\nfeat(voice): [chat]...[/chat] protocol\nhash docs: docker agents",
            ),
            ("voice", "готово"),
        ]

    def test_unclosed_nested_open_consumes_to_end(self):
        # An inner [chat] with no matching close leaves depth>0 forever;
        # unclosed block consumes to end-of-text.
        result = split_voice_segments("a [chat]x [chat]y[/chat]")
        assert result == [
            ("voice", "a"),
            ("chat", "x [chat]y[/chat]"),
        ]

    def test_multiline_preserved(self):
        result = split_voice_segments(
            "скажу\n[chat]```py\nprint(1)\n```[/chat]\nвот код"
        )
        assert result == [
            ("voice", "скажу"),
            ("chat", "```py\nprint(1)\n```"),
            ("voice", "вот код"),
        ]


class TestStripOutputTags:
    def test_removes_audio_tags(self):
        assert strip_output_tags("[warmly] привет") == "привет"

    def test_removes_chat_markers(self):
        assert strip_output_tags("[chat]link[/chat]") == "link"

    def test_removes_style_prefix(self):
        assert strip_output_tags("Say cheerfully: hello") == "hello"

    def test_preserves_plain_text(self):
        assert strip_output_tags("обычный текст") == "обычный текст"

    def test_case_insensitive_chat_markers(self):
        assert strip_output_tags("[CHAT]x[/CHAT]") == "x"

    def test_preserves_code_block_indentation(self):
        """Every text reply passes through here — runs of spaces (code
        indentation, aligned output) must come out untouched. Regression:
        a whitespace-collapse pass used to mangle all code blocks."""
        code = "```py\ndef f():\n    if x:\n        return  # two  spaces\n```"
        assert strip_output_tags(code) == code

    def test_preserves_aligned_columns(self):
        table = "name      size\nfoo.txt     12\nbar.txt   3456"
        assert strip_output_tags(table) == table
