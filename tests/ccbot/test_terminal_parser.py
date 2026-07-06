"""Tests for terminal_parser — regex-based detection of Claude Code UI elements."""

import pytest

from ccbot.terminal_parser import (
    detect_model_switch,
    extract_bash_output,
    extract_interactive_content,
    is_claude_working,
    is_interactive_ui,
    is_tui_ready,
    parse_login_url,
    parse_status_line,
    strip_pane_chrome,
)


# ── is_claude_working ────────────────────────────────────────────────────

_SEP = "────────────────────────────────────────"


def _pane(*above_separator: str, transcript: str = "") -> str:
    """Build a pane: optional transcript, then <lines> directly above the
    chrome separator, then the input box."""
    parts: list[str] = []
    if transcript:
        parts += [transcript, ""]
    parts += list(above_separator)
    parts += [_SEP, "  ❯ ", _SEP, "  [Opus 4.7] Context: 41%"]
    return "\n".join(parts) + "\n"


class TestIsClaudeWorking:
    def test_active_turn_interrupt_hint(self):
        # Admin test (ii): active spinner line with the interrupt hint.
        pane = _pane("✶ Orbiting… (3m 13s · ↓ 13.9k tokens · esc to interrupt)")
        assert is_claude_working(pane) is True

    def test_active_turn_live_counter_no_hint(self):
        # Admin test (iii): extended-thinking turn — shows the running
        # counter "(3m 13s · …" but not "esc to interrupt". Still active.
        pane = _pane("✶ Orbiting… (3m 13s · ↓ tokens · thought for 34s)")
        assert is_claude_working(pane) is True

    def test_completed_turn_marker_with_hint_in_transcript_is_not_working(self):
        # Admin test (i) — the regression this fix exists for: "esc to
        # interrupt" appears in the transcript (we're discussing the
        # feature), but the line above the separator is the done marker.
        pane = _pane(
            "✻ Cooked for 12s",
            transcript="...discussing how to detect 'esc to interrupt' in the pane...",
        )
        assert "esc to interrupt" in pane  # it really is in there
        assert is_claude_working(pane) is False

    @pytest.mark.parametrize(
        "marker",
        [
            "Cooked for 12s",
            "Brewed for 2m 33s",
            "Cogitated for 2m 49s",
            "Sautéed for 53s",
        ],
    )
    def test_done_markers_not_working(self, marker: str):
        # Admin test (iv) + variants.
        assert is_claude_working(_pane(f"✻ {marker}")) is False

    def test_interrupt_hint_only_in_transcript_no_status_line(self):
        # "esc to interrupt" deep in the transcript, plain prompt below —
        # the old blind .search() over the whole pane read this as busy.
        pane = _pane(transcript="The marker is the literal string 'esc to interrupt'.")
        assert "esc to interrupt" in pane
        assert is_claude_working(pane) is False

    def test_case_insensitive(self):
        assert is_claude_working(_pane("✶ Working… (3s · ESC TO INTERRUPT)")) is True

    def test_empty_pane_not_working(self):
        assert is_claude_working("") is False
        assert is_claude_working("   \n  \n") is False

    def test_plain_prompt_not_working(self):
        assert is_claude_working(_pane()) is False

    def test_no_chrome_separator_not_working(self):
        assert is_claude_working("just some\noutput\nno chrome\n") is False


# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Claude has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Claude has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_ask_user_numbered_ascii(self, sample_pane_ask_user_numbered_ascii: str):
        # Claude Code reworked AskUserQuestion to numbered ASCII checkboxes
        # (`  1. [ ] …`, `❯ 2. [ ] …`); the unicode-only patterns missed it,
        # so docker bindings stopped getting the photo+nav notification.
        result = extract_interactive_content(sample_pane_ask_user_numbered_ascii)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content
        assert "[ ] Option A" in result.content

    def test_permission_prompt(self, sample_pane_permission: str):
        result = extract_interactive_content(sample_pane_permission)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_min_gap_too_small_returns_none(self):
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")


# Historical note: TestParseToolFromStatus and TestExtractPermissionContextWithPaneText
# were removed here along with parse_tool_from_status / extract_permission_context
# in terminal_parser.py. Those helpers existed to enrich permission prompts with
# the originating tool name and path so the old prompt UI could render "Allow
# Write /tmp/foo?" with direct Allow/Deny buttons. That UI is gone — all
# interactive prompts now render uniformly as a pane screenshot plus a
# ↑/↓/⏎/⎋/🔄 nav keyboard (see handlers/interactive_ui.py), so the extractor
# helpers have no callers and their tests had no meaningful assertion target.


class TestUnrecognizedStatusCanary:
    """A status line that matches neither the done-form nor the live-form
    means the Claude Code TUI render changed — every detector downstream
    degrades silently, so is_claude_working must WARN (rate-limited)."""

    def test_unknown_status_logs_warning(self, caplog):
        import logging as logging_mod

        from ccbot import terminal_parser as tp

        tp._unrecognized_status_seen.clear()
        pane = (
            "some output\n✻ Mysterious new format without counters\n"
            + "─" * 40
            + "\n❯ \n"
        )
        with caplog.at_level(logging_mod.WARNING, logger="ccbot.terminal_parser"):
            assert tp.is_claude_working(pane) is False
        assert any("Unrecognized" in r.message for r in caplog.records)

    def test_known_done_form_does_not_warn(self, caplog):
        import logging as logging_mod

        from ccbot import terminal_parser as tp

        tp._unrecognized_status_seen.clear()
        pane = "✻ Cooked for 12s\n" + "─" * 40 + "\n❯ \n"
        with caplog.at_level(logging_mod.WARNING, logger="ccbot.terminal_parser"):
            assert tp.is_claude_working(pane) is False
        assert not caplog.records

    def test_warning_is_rate_limited(self, caplog):
        import logging as logging_mod

        from ccbot import terminal_parser as tp

        tp._unrecognized_status_seen.clear()
        pane = "✻ Weird status\n" + "─" * 40 + "\n❯ \n"
        with caplog.at_level(logging_mod.WARNING, logger="ccbot.terminal_parser"):
            tp.is_claude_working(pane)
            tp.is_claude_working(pane)
        assert len([r for r in caplog.records if "Unrecognized" in r.message]) == 1

    def test_first_warning_fires_on_freshly_booted_clock(self, caplog, monkeypatch):
        """monotonic() counts from boot; with uptime <1h a 0.0 not-seen
        sentinel reads as "logged recently" and mutes the first warning
        (bit CI runners and post-reboot hosts)."""
        import logging as logging_mod

        from ccbot import terminal_parser as tp

        tp._unrecognized_status_seen.clear()
        monkeypatch.setattr(tp.time, "monotonic", lambda: 500.0)
        pane = "✻ Weird status\n" + "─" * 40 + "\n❯ \n"
        with caplog.at_level(logging_mod.WARNING, logger="ccbot.terminal_parser"):
            assert tp.is_claude_working(pane) is False
        assert any("Unrecognized" in r.message for r in caplog.records)


class TestHasQueuedMessages:
    """has_queued_messages: detect Claude Code's buffered-input hint."""

    def test_queued_hint_present(self):
        from ccbot.terminal_parser import has_queued_messages

        pane = (
            "❄ Swooping… (7m 45s · ↓ 38.5k tokens)\n"
            "┃ queued text line\n"
            "❯ Press up to edit queued messages\n"
            + "─" * 60
            + "\n  Opus 4.8 (1M context)\n"
        )
        assert has_queued_messages(pane) is True

    def test_no_queue_when_drained(self):
        from ccbot.terminal_parser import has_queued_messages

        pane = "❄ Swooping… (7m 45s · ↓ 38.5k tokens)\n" + "─" * 60 + "\n❯ \n"
        assert has_queued_messages(pane) is False

    def test_empty_pane(self):
        from ccbot.terminal_parser import has_queued_messages

        assert has_queued_messages("") is False

    def test_phrase_in_old_transcript_above_chrome_is_ignored(self):
        from ccbot.terminal_parser import has_queued_messages

        # "queued messages" far up in scrollback (not the bottom input chrome)
        # must not false-fire — only the last lines (the input area) are scanned.
        pane = (
            "  some old output mentioning queued messages here\n"
            + "\n" * 20
            + "❄ Swooping… (1m · ↓ 1k tokens)\n"
            + "─" * 60
            + "\n❯ \n  Opus 4.8\n"
        )
        assert has_queued_messages(pane) is False


class TestUltracodeLabeledSeparator:
    """Regression: is_claude_working was blind to ultracode-mode panes — the
    input-box top border carries a label ("──── ultracode ─") and a "⎿ Tip"
    line sits between the spinner and the border. Captured live 2026-06-17."""

    BUSY = (
        "✢ Propagating… (2m 22s · ↓ 1.0k tokens)\n"
        "  ⎿  Tip: Use /btw to ask a quick side question without interrupting\n"
        "\n" + "─" * 88 + " ultracode ─\n"
        "❯ \n" + "─" * 100 + "\n"
        "  лимит ░ 9%  контекст ▓ 40%  ↑402k ↓0k  $12.34   /rc active\n"
        "  Opus 4.8 (1M context)\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )

    def test_labeled_separator_recognized(self):
        from ccbot.terminal_parser import _is_chrome_separator

        assert _is_chrome_separator("─" * 88 + " ultracode ─") is True
        assert _is_chrome_separator("─" * 88 + " /rc active ─") is True
        assert _is_chrome_separator("─" * 100) is True
        # prose that merely contains dashes is not chrome
        assert _is_chrome_separator("a long sentence — with an em dash in it") is False

    def test_busy_ultracode_detected_working(self):
        from ccbot.terminal_parser import is_claude_working, parse_status_line

        assert parse_status_line(self.BUSY) == "Propagating… (2m 22s · ↓ 1.0k tokens)"
        assert is_claude_working(self.BUSY) is True

    def test_idle_ultracode_done_marker(self):
        from ccbot.terminal_parser import is_claude_working

        idle = (
            "✻ Cooked for 44s\n"
            "\n" + "─" * 88 + " ultracode ─\n"
            "❯ \n" + "─" * 100 + "\n"
            "  Opus 4.8 (1M context)\n"
        )
        assert is_claude_working(idle) is False


# ── is_tui_ready ─────────────────────────────────────────────────────────


class TestIsTuiReady:
    """The post-restart screenshot waits on this: True once Claude Code's
    input box (chrome separator) has rendered, False while it's still
    booting (empty/black pane)."""

    def test_rendered_prompt_is_ready(self):
        assert is_tui_ready(_pane()) is True

    def test_splash_with_input_box_is_ready(self):
        # Fresh `claude` splash: banner, blank rows, then the input box.
        splash = (
            " ▐▛███▜▌   Claude Code v2.1.181\n"
            "▝▜█████▛▘  Opus 4.8 (1M context) · Claude Max\n"
            "  ▘▘ ▝▝    /workspace\n"
            "\n" + "─" * 100 + "\n"
            "❯ \n" + "─" * 100 + "\n"
            "  Opus 4.8 (1M context)\n"
        )
        assert is_tui_ready(splash) is True

    def test_empty_pane_not_ready(self):
        assert is_tui_ready("") is False
        assert is_tui_ready("   \n  \n\n") is False

    def test_booting_pane_no_input_box_not_ready(self):
        # Mid-boot: some output but the input box hasn't drawn yet.
        assert is_tui_ready("loading mcp servers...\nstarting up\n") is False

    def test_labeled_separator_is_ready(self):
        # ultracode/`/rc active` label on the input-box border still counts.
        pane = "❯ \n" + "─" * 88 + " ultracode ─\n" + "─" * 100 + "\n"
        assert is_tui_ready(pane) is True

    def test_input_box_at_top_with_blank_padding_below_is_ready(self):
        # A FRESH session (no transcript) draws the splash + input box near
        # the top of a tall (50-row) pane, with the rest blank. A bottom-only
        # scan misses the separator — this is the live regression the
        # whole-pane search fixes.
        pane = (
            " ▐▛███▜▌   Claude Code v2.1.181\n"
            "  ▘▘ ▝▝    /workspace\n" + "─" * 100 + "\n"
            "❯ \n" + "─" * 100 + "\n"
            "  Opus 4.8 (1M context)\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
            + "\n"
            * 40  # blank padding to the bottom of the pane
        )
        assert is_tui_ready(pane) is True


# ── parse_login_url / LoginPrompt detection ──────────────────────────────

# Real-shaped Claude Code OAuth sign-in URL.
_LOGIN_URL = (
    "https://claude.com/cai/oauth/authorize?code=true"
    "&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference"
    "&code_challenge=FakePkceChallengeFakePkceChallengeFakePkc43"
    "&code_challenge_method=S256&state=FakeOauthStateValueFakeOauthStateValueFak43"
)


def _wrap(s: str, width: int = 100) -> str:
    """Split like tmux wraps a long URL with no -J: full-width rows, no spaces."""
    return "\n".join(s[i : i + width] for i in range(0, len(s), width))


def _login_pane(url_block: str) -> str:
    return (
        "  Login\n"
        "\n"
        "  Browser didn't open? Use the url below to sign in (c to copy)\n"
        "\n"
        f"{url_block}\n"
        "\n"
        "  Paste code here if prompted >\n"
        "\n"
        "  Esc to cancel\n"
    )


class TestParseLoginUrl:
    def test_reconstructs_wrapped_url(self):
        pane = _login_pane(_wrap(_LOGIN_URL))
        # sanity: the URL really did wrap (multiple rows)
        assert "\n" in _wrap(_LOGIN_URL)
        assert parse_login_url(pane) == _LOGIN_URL

    def test_unwrapped_url(self):
        pane = _login_pane(_LOGIN_URL)
        assert parse_login_url(pane) == _LOGIN_URL

    def test_osc8_hyperlink_preferred(self):
        # When Claude marks the URL clickable, the full target is in the escape
        # even if the visible label wrapped to a stub.
        pane = _login_pane(f"\x1b]8;;{_LOGIN_URL}\x1b\\sign in\x1b]8;;\x1b\\")
        assert parse_login_url(pane) == _LOGIN_URL

    def test_strips_sgr_color(self):
        pane = _login_pane("\x1b[34m" + _wrap(_LOGIN_URL) + "\x1b[0m")
        assert parse_login_url(pane) == _LOGIN_URL

    def test_not_a_login_screen(self):
        assert parse_login_url("just some pane\nhttps://example.com\n") is None

    def test_empty(self):
        assert parse_login_url("") is None


class TestLoginPromptDetection:
    def test_detected_as_interactive_ui(self):
        pane = _login_pane(_wrap(_LOGIN_URL))
        assert is_interactive_ui(pane) is True

    def test_extract_names_loginprompt(self):
        pane = _login_pane(_wrap(_LOGIN_URL))
        content = extract_interactive_content(pane)
        assert content is not None
        assert content.name == "LoginPrompt"

    def test_detected_without_esc_to_cancel_hint(self):
        """Regression: Claude Code v2.1.197 dropped the trailing
        `Esc to cancel` hint from the OAuth URL screen. The old classifier
        keyed on it as the bottom marker and silently returned None, so the
        URL was never surfaced. The pattern now uses top markers only
        (no-bottom mode) and this new-shape screen classifies correctly.
        """
        pane = (
            "  Browser didn't open? Use the url below to sign in (c to copy)\n"
            "\n"
            f"{_wrap(_LOGIN_URL)}\n"
            "\n"
            "  Paste code here if prompted >\n"
        )
        content = extract_interactive_content(pane)
        assert content is not None
        assert content.name == "LoginPrompt"
        assert parse_login_url(pane) == _LOGIN_URL


# ── detect_model_switch ──────────────────────────────────────────────────


class TestDetectModelSwitch:
    """Fable 5's safeguard notice ("Switched to Opus 4.8.") lives in the pane
    transcript only, never the JSONL — the status poll catches it here."""

    def test_full_wrapped_notice_returns_model(self):
        # Word-wrapped exactly as the pane renders it (4 rows for one sentence).
        pane = (
            "● Fable 5's safeguards flagged this message. The safeguards are "
            "intentionally broad right now and\n"
            "  may flag safe and routine coding, cybersecurity, or biology work. "
            "These measures let us bring you\n"
            "  Mythos-level capabilities sooner, and we're working to refine "
            "them. Switched to Opus 4.8. Send\n"
            "  feedback with /feedback or Learn more\n"
            "  └ Tip: You can configure model switch behavior in /config\n"
        )
        # Not clipped to "Opus 4" at the "4.8" dot.
        assert detect_model_switch(pane) == "Opus 4.8"

    def test_model_at_end_no_trailing_sentence(self):
        pane = "safeguards flagged this message ... Switched to Opus 4.8."
        assert detect_model_switch(pane) == "Opus 4.8"

    def test_notice_present_but_model_unparseable_returns_empty(self):
        # Empty string (falsy) signals "notice present, name unknown" so the
        # caller still notifies (with a generic model label).
        assert detect_model_switch("safeguards flagged this message, no target") == ""

    def test_no_notice_returns_none(self):
        # The word "safeguards" alone in a transcript must not false-fire.
        pane = "esc to interrupt · a normal reply mentioning safeguards in prose."
        assert detect_model_switch(pane) is None

    def test_empty_pane_returns_none(self):
        assert detect_model_switch("") is None
