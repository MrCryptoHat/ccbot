"""Unit tests for inject.core.sanitize_inject_text — the RCE shield.

The security perimeter: these pin that (1) a leading "!" can never reach
the pane as the first byte (Claude Code bash command-mode), and (2) ESC /
CR / other control bytes are stripped while newlines survive.
"""

from __future__ import annotations

from ccbot.inject.core import sanitize_inject_text


def test_plain_text_unchanged() -> None:
    assert sanitize_inject_text("купи молоко") == "купи молоко"


def test_leading_bang_gets_space_prefix() -> None:
    # The "!" must NOT be the first byte sent — a space defuses bash mode
    # while preserving the visible text.
    out = sanitize_inject_text("!rm -rf /")
    assert out == " !rm -rf /"
    assert not out.startswith("!")


def test_leading_bang_preserved_in_content() -> None:
    out = sanitize_inject_text("!важно")
    assert "!важно" in out


def test_bang_not_leading_is_left_alone() -> None:
    assert sanitize_inject_text("привет! как дела") == "привет! как дела"


def test_leading_slash_command_defused() -> None:
    # "/clear", "/exit" etc. would run as a TUI slash command on an idle
    # prompt — prefix a space so they land as prompt text.
    out = sanitize_inject_text("/clear")
    assert out == " /clear"
    assert not out.startswith("/")


def test_slash_not_leading_is_left_alone() -> None:
    assert sanitize_inject_text("путь a/b/c") == "путь a/b/c"


def test_hash_and_at_left_as_content() -> None:
    # No out-of-band side effect → not rewritten.
    assert sanitize_inject_text("#тег") == "#тег"
    assert sanitize_inject_text("@file") == "@file"


def test_whitespace_then_bang_not_prefixed() -> None:
    # First byte is a space already → command-mode can't trigger, no extra
    # prefix needed.
    assert sanitize_inject_text(" !ls") == " !ls"


def test_esc_sequence_stripped() -> None:
    # CSI cursor-move + title-set must not reach the pane.
    out = sanitize_inject_text("\x1b[2Jhello\x1b]0;title\x07")
    assert "\x1b" not in out
    assert "hello" in out


def test_carriage_return_stripped() -> None:
    # A CR would be a premature submit through tmux send-keys -l.
    assert sanitize_inject_text("line1\rline2") == "line1line2"


def test_newline_preserved() -> None:
    assert sanitize_inject_text("line1\nline2") == "line1\nline2"


def test_tab_and_nul_stripped() -> None:
    assert sanitize_inject_text("a\tb\x00c") == "abc"


def test_all_control_payload_becomes_empty() -> None:
    # Server treats an empty/blank result as a 400.
    assert sanitize_inject_text("\x00\x1b\r\x07").strip() == ""


def test_esc_then_bang_still_defused() -> None:
    # After stripping the ESC, the text starts with "!" → must be prefixed.
    out = sanitize_inject_text("\x1b!danger")
    assert out == " !danger"
    assert not out.startswith("!")
