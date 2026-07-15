"""Tests for the AgentRuntime abstraction — runtime-aware busy / queue detection.

The monitor and launch/restart paths that key on runtime are covered elsewhere
(test_session_monitor, test_callbacks); this pins the terminal-detection seam:
each runtime's is_working / has_queued_input dispatches to its own TUI chrome,
so a busy Codex pane (no ─ separator, "(Ns • esc to interrupt)" counter) is not
mistaken for idle by Claude's status-line detector, and vice-versa.
"""

from ccbot.runtimes import CLAUDE, CODEX, get_runtime

_SEP = "─" * 40

# Claude: status line above a ─ chrome separator.
_CLAUDE_WORKING = (
    f"✶ Orbiting… (3m 13s · ↓ 13.9k tokens · esc to interrupt)\n{_SEP}\n  ❯ \n{_SEP}\n"
)
_CLAUDE_IDLE = f"  some reply\n{_SEP}\n  ❯ \n{_SEP}\n  [Opus 4.7] Context: 41%\n"

# Codex: no ─ separator; "(Ns • esc to interrupt)" counter above the input box.
_CODEX_WORKING = (
    "• streaming reply text\n"
    "◦ Working (4s • esc to interrupt)\n"
    "\n"
    "› Summarize recent commits\n"
    "  gpt-5.5 medium · /home/user/project\n"
)
_CODEX_IDLE = (
    "• streaming reply text\n"
    "› Summarize recent commits\n"
    "  gpt-5.5 medium · /home/user/project\n"
)
_CODEX_QUEUED = (
    "◦ Working (7s • esc to interrupt)\n"
    "• Messages to be submitted after next tool call "
    "(press esc to interrupt and send immediately)\n"
    "› Summarize recent commits\n"
    "  gpt-5.5 medium · /home/user/project\n"
)


class TestGetRuntime:
    def test_known_names(self):
        assert get_runtime("claude") is CLAUDE
        assert get_runtime("codex") is CODEX

    def test_unknown_falls_back_to_claude(self):
        # Legacy rows carry no runtime; an unrecognised value must degrade to
        # the default, never crash.
        assert get_runtime(None) is CLAUDE
        assert get_runtime("") is CLAUDE
        assert get_runtime("gemini-cli") is CLAUDE


class TestIsWorkingDispatch:
    def test_claude_detects_its_status_line(self):
        assert CLAUDE.is_working(_CLAUDE_WORKING) is True
        assert CLAUDE.is_working(_CLAUDE_IDLE) is False

    def test_codex_detects_its_counter(self):
        assert CODEX.is_working(_CODEX_WORKING) is True
        assert CODEX.is_working(_CODEX_IDLE) is False

    def test_codex_detector_blind_to_claude_chrome(self):
        # Codex has no ─ separator; a Claude working pane's "(3m 13s · …"
        # counter (first token "3m", not "Ns") doesn't match codex's anchor.
        assert CODEX.is_working(_CLAUDE_WORKING) is False

    def test_claude_detector_blind_to_codex_pane(self):
        # Codex pane has no chrome separator → Claude's status-line detector
        # finds no anchor and reports idle (the very gap this feature fixes).
        assert CLAUDE.is_working(_CODEX_WORKING) is False


class TestHasQueuedInputDispatch:
    def test_claude_queue_hint(self):
        pane = f"  text\n{_SEP}\n  ❯ Press up to edit queued messages\n{_SEP}\n"
        assert CLAUDE.has_queued_input(pane) is True
        assert CLAUDE.has_queued_input(_CLAUDE_IDLE) is False

    def test_codex_queue_hint(self):
        assert CODEX.has_queued_input(_CODEX_QUEUED) is True
        assert CODEX.has_queued_input(_CODEX_IDLE) is False


class TestRuntimeMetadata:
    def test_exit_commands(self):
        assert CLAUDE.exit_command() == "/exit"
        assert CODEX.exit_command() == "/quit"

    def test_names(self):
        assert CLAUDE.name == "claude"
        assert CODEX.name == "codex"
