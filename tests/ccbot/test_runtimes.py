"""Tests for the AgentRuntime abstraction — runtime-aware busy / queue detection.

The monitor and launch/restart paths that key on runtime are covered elsewhere
(test_session_monitor, test_callbacks); this pins the terminal-detection seam:
each runtime's is_working / has_queued_input dispatches to its own TUI chrome,
so a busy Codex pane (no ─ separator, "(Ns • esc to interrupt)" counter) is not
mistaken for idle by Claude's status-line detector, and vice-versa.
"""

import json

import pytest

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


class TestEditToolDispatch:
    """/diff triggers on the runtime's edit tool: Claude's Edit/Write/… vs
    codex's apply_patch (a custom_tool_call). Each runtime also carries the
    header/boundary patterns that crop its native diff block."""

    def test_is_edit_tool(self):
        assert CLAUDE.is_edit_tool("Edit") is True
        assert CLAUDE.is_edit_tool("Write") is True
        assert CLAUDE.is_edit_tool("apply_patch") is False
        assert CODEX.is_edit_tool("apply_patch") is True
        assert CODEX.is_edit_tool("Edit") is False

    def test_none_name_is_not_edit(self):
        assert CLAUDE.is_edit_tool(None) is False
        assert CODEX.is_edit_tool(None) is False

    def test_both_runtimes_carry_diff_patterns(self):
        for rt in (CLAUDE, CODEX):
            assert rt.diff_header_re is not None
            assert rt.diff_boundary_re is not None


class TestCodexSandboxBypass:
    """Opt-in --dangerously-bypass-approvals-and-sandbox (config.codex_bypass_
    sandbox) — needed where codex's bundled bwrap can't initialize."""

    _SID = "019f0000-0000-7000-8000-000000000000"

    def _cfg(self, monkeypatch, bypass):
        from unittest.mock import MagicMock

        import ccbot.runtimes as rt

        monkeypatch.setattr(
            rt,
            "config",
            MagicMock(codex_command="codex", codex_bypass_sandbox=bypass),
        )

    def test_off_by_default(self, monkeypatch):
        self._cfg(monkeypatch, False)
        assert CODEX.launch_command("proj") == "codex"
        assert CODEX.launch_command("proj", self._SID) == f"codex resume {self._SID}"

    def test_on_appends_flag_to_fresh_and_resume(self, monkeypatch):
        self._cfg(monkeypatch, True)
        flag = "--dangerously-bypass-approvals-and-sandbox"
        assert CODEX.launch_command("proj") == f"codex {flag}"
        assert (
            CODEX.launch_command("proj", self._SID)
            == f"codex resume {self._SID} {flag}"
        )


class TestImageInput:
    """Claude reads an image from a text marker; Codex attaches the path in its
    composer (native multimodal, client-side read → bypasses the sandbox)."""

    def test_claude_uses_text_marker(self):
        assert CLAUDE.native_image_input is False
        assert CLAUDE.image_marker("/p/x.jpg") == "(image attached: /p/x.jpg)"

    def test_codex_uses_native_composer(self):
        assert CODEX.native_image_input is True
        assert CODEX.composer_image_token == "[Image #"


class TestPaneAliveCommands:
    """The dead-window health check keys on the runtime's foreground-command
    set. Claude's is claude/node; codex's foreground is `codex` — keying on the
    claude set reaped every codex window 30 s after launch (the bug)."""

    def test_claude_set(self):
        assert CLAUDE.pane_alive_commands == frozenset({"claude", "node"})

    def test_codex_set(self):
        # `codex`, NOT node — codex is a native binary.
        assert CODEX.pane_alive_commands == frozenset({"codex"})
        assert "codex" in CODEX.pane_alive_commands
        assert "node" not in CODEX.pane_alive_commands

    def test_unknown_runtime_degrades_to_claude_set(self):
        # get_runtime(None) → CLAUDE, so an untracked window uses claude's set.
        assert get_runtime(None).pane_alive_commands == frozenset({"claude", "node"})


class TestPickerIcon:
    """Tab colours: Codex 🔵, Claude Code 🟠 (per operator preference)."""

    def test_icons(self):
        assert CLAUDE.picker_icon == "🟠"
        assert CODEX.picker_icon == "🔵"


class TestBootstrapCapabilities:
    """Window-bootstrap divergence is capabilities, never name comparisons —
    a third runtime with its own hook must not inherit codex's treatment just
    for not being named "claude"."""

    def test_session_map(self):
        assert CLAUDE.uses_session_map is True
        assert CODEX.uses_session_map is False

    def test_first_message_forward(self):
        # Codex can open on its sign-in menu — blind typing + Enter would pick
        # a menu option the user never chose.
        assert CLAUDE.auto_forward_first_message is True
        assert CODEX.auto_forward_first_message is False

    def test_interrupt_keys(self):
        # Codex must not get the trailing Ctrl-C: on its TUI an idle Ctrl-C
        # arms the quit sequence instead of being a no-op.
        assert CLAUDE.interrupt_keys == ("Escape", "C-c")
        assert CODEX.interrupt_keys == ("Escape",)

    def test_ready_message_keys(self):
        assert CLAUDE.ready_message_key(resumed=False) == "bot.window_ready"
        assert CLAUDE.ready_message_key(resumed=True) == "bot.window_resumed"
        # Codex explains its sign-in flow for fresh AND resumed windows.
        assert CODEX.ready_message_key(resumed=False) == "bot.window_codex_ready"
        assert CODEX.ready_message_key(resumed=True) == "bot.window_codex_ready"


class TestAvailabilityGating:
    """pickable_runtimes offers only installed CLIs — a fresh install without
    codex must never see a Codex tab whose "new session" types a missing
    command into the pane (dead window, no explanation)."""

    def _patch_which(self, monkeypatch, present: set[str]):
        import shutil as _shutil

        monkeypatch.setattr(
            _shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd in present else None
        )

    def _patch_cfg(self, monkeypatch, default="claude"):
        from unittest.mock import MagicMock

        import ccbot.runtimes as rt

        monkeypatch.setattr(
            rt,
            "config",
            MagicMock(
                claude_command="claude",
                codex_command="codex",
                default_runtime=default,
            ),
        )

    def test_both_installed_both_pickable(self, monkeypatch):
        from ccbot.runtimes import pickable_runtimes

        self._patch_cfg(monkeypatch)
        self._patch_which(monkeypatch, {"claude", "codex"})
        assert pickable_runtimes() == [CLAUDE, CODEX]

    def test_missing_codex_hides_its_tab(self, monkeypatch):
        from ccbot.runtimes import pickable_runtimes

        self._patch_cfg(monkeypatch)
        self._patch_which(monkeypatch, {"claude"})
        assert pickable_runtimes() == [CLAUDE]

    def test_nothing_installed_falls_back_to_claude(self, monkeypatch):
        # The picker must never be empty; main.py already warns at boot.
        from ccbot.runtimes import pickable_runtimes

        self._patch_cfg(monkeypatch)
        self._patch_which(monkeypatch, set())
        assert pickable_runtimes() == [CLAUDE]

    def test_availability_uses_first_token_of_command(self, monkeypatch):
        # CLAUDE_COMMAND may carry flags — only the binary is probed.
        from unittest.mock import MagicMock

        import ccbot.runtimes as rt

        monkeypatch.setattr(
            rt,
            "config",
            MagicMock(claude_command="claude --dangerously-skip-permissions"),
        )
        self._patch_which(monkeypatch, {"claude"})
        assert CLAUDE.is_available() is True

    def test_default_runtime_honors_config(self, monkeypatch):
        from ccbot.runtimes import default_runtime

        self._patch_cfg(monkeypatch, default="codex")
        self._patch_which(monkeypatch, {"claude", "codex"})
        assert default_runtime() is CODEX

    def test_default_runtime_degrades_when_uninstalled(self, monkeypatch):
        # CCBOT_DEFAULT_RUNTIME=codex on a host without codex must not
        # produce windows that can't launch.
        from ccbot.runtimes import default_runtime

        self._patch_cfg(monkeypatch, default="codex")
        self._patch_which(monkeypatch, {"claude"})
        assert default_runtime() is CLAUDE

    def test_default_runtime_unknown_name_is_claude(self, monkeypatch):
        from ccbot.runtimes import default_runtime

        self._patch_cfg(monkeypatch, default="gemini-cli")
        self._patch_which(monkeypatch, {"claude", "codex"})
        assert default_runtime() is CLAUDE


def _write_rollout(root, sid, cwd, *, ts="2026-07-15T10-00-00", turns=("hi",)):
    """Write a minimal codex rollout file under root and return its path."""
    day = root / "2026" / "07" / "15"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{ts}-{sid}.jsonl"
    lines = [{"type": "session_meta", "payload": {"session_id": sid, "cwd": cwd}}]
    for t in turns:
        lines.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": t}],
                },
            }
        )
    path.write_text("\n".join(json.dumps(x) for x in lines))
    return path


class TestCodexListSessions:
    """CodexRuntime.list_sessions enumerates rollouts by matching
    session_meta.cwd — the codex analogue of Claude's per-directory glob, so the
    picker's Codex tab shows resumable codex sessions for the folder."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self):
        # CODEX is a module singleton; clear its per-path caches so tmp paths
        # from a prior test don't leak.
        CODEX._meta_cwd.clear()
        CODEX._last_rollout.clear()
        yield
        CODEX._meta_cwd.clear()
        CODEX._last_rollout.clear()

    def _patch_root(self, monkeypatch, root):
        from unittest.mock import MagicMock

        import ccbot.runtimes as rt

        monkeypatch.setattr(
            rt, "config", MagicMock(codex_command="codex", codex_sessions_path=root)
        )

    @pytest.mark.asyncio
    async def test_lists_only_matching_cwd_newest_first(self, tmp_path, monkeypatch):
        root = tmp_path / "sessions"
        _write_rollout(
            root,
            "019f0000-0000-7000-8000-000000000001",
            "/home/user/project",
            ts="2026-07-15T09-00-00",
            turns=["old task"],
        )
        _write_rollout(
            root,
            "019f0000-0000-7000-8000-000000000002",
            "/home/user/project",
            ts="2026-07-15T11-00-00",
            turns=["new task", "more"],
        )
        _write_rollout(
            root,
            "019f0000-0000-7000-8000-000000000003",
            "/home/user/other",  # different cwd → excluded
            turns=["elsewhere"],
        )
        self._patch_root(monkeypatch, root)

        sessions = await CODEX.list_sessions(None, "/home/user/project")
        assert [s.session_id[-1] for s in sessions] == ["2", "1"]  # newest first
        assert sessions[0].summary == "new task"
        assert sessions[0].message_count == 2

    @pytest.mark.asyncio
    async def test_missing_root_returns_empty(self, tmp_path, monkeypatch):
        self._patch_root(monkeypatch, tmp_path / "does-not-exist")
        assert await CODEX.list_sessions(None, "/home/user/project") == []

    @pytest.mark.asyncio
    async def test_header_only_rollout_skipped(self, tmp_path, monkeypatch):
        root = tmp_path / "sessions"
        # No real turns → count 0 → not listed.
        _write_rollout(root, "019f0000-0000-7000-8000-000000000009", "/w", turns=[])
        self._patch_root(monkeypatch, root)
        assert await CODEX.list_sessions(None, "/w") == []

    def test_claude_delegates_to_session_manager(self):
        """Claude's list_sessions reuses SessionManager.list_sessions_for_directory."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        sm = MagicMock()
        sm.list_sessions_for_directory = AsyncMock(return_value=["sentinel"])
        out = asyncio.run(CLAUDE.list_sessions(sm, "/some/dir"))
        assert out == ["sentinel"]
        sm.list_sessions_for_directory.assert_awaited_once_with("/some/dir")


class TestStickyRolloutResolution:
    """The per-tick rollout scan is capped; a quiet window whose rollout fell
    out of the newest-N prefix must keep tracking via the last-resolved path
    instead of silently going dark."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self):
        CODEX._meta_cwd.clear()
        CODEX._last_rollout.clear()
        yield
        CODEX._meta_cwd.clear()
        CODEX._last_rollout.clear()

    def _patch_root(self, monkeypatch, root):
        from unittest.mock import MagicMock

        import ccbot.runtimes as rt

        monkeypatch.setattr(
            rt, "config", MagicMock(codex_command="codex", codex_sessions_path=root)
        )

    def test_scan_miss_falls_back_to_last_resolved(self, tmp_path, monkeypatch):
        import ccbot.runtimes as rt

        root = tmp_path / "sessions"
        path = _write_rollout(
            root, "019f0000-0000-7000-8000-000000000001", "/home/user/project"
        )
        self._patch_root(monkeypatch, root)
        assert CODEX._resolve_rollout("/home/user/project") == path  # populates cache

        # Simulate the rollout dropping out of the scanned prefix.
        monkeypatch.setattr(rt, "_CODEX_SCAN_CAP", 0)
        assert CODEX._resolve_rollout("/home/user/project") == path  # sticky

    def test_sticky_ignores_deleted_file(self, tmp_path, monkeypatch):
        import ccbot.runtimes as rt

        root = tmp_path / "sessions"
        path = _write_rollout(
            root, "019f0000-0000-7000-8000-000000000002", "/home/user/project"
        )
        self._patch_root(monkeypatch, root)
        assert CODEX._resolve_rollout("/home/user/project") == path
        path.unlink()
        monkeypatch.setattr(rt, "_CODEX_SCAN_CAP", 0)
        assert CODEX._resolve_rollout("/home/user/project") is None


class TestHistoryTranscript:
    """get_recent_messages resolves the transcript through the runtime — a
    codex window's history comes from its rollout, not the (never-existing)
    Claude projects JSONL."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self):
        CODEX._meta_cwd.clear()
        CODEX._last_rollout.clear()
        yield
        CODEX._meta_cwd.clear()
        CODEX._last_rollout.clear()

    @pytest.mark.asyncio
    async def test_codex_resolves_rollout_by_window_cwd(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        import ccbot.runtimes as rt

        root = tmp_path / "sessions"
        path = _write_rollout(
            root, "019f0000-0000-7000-8000-000000000003", "/home/user/project"
        )
        monkeypatch.setattr(
            rt, "config", MagicMock(codex_command="codex", codex_sessions_path=root)
        )
        sm = MagicMock()
        sm.get_window_state.return_value = MagicMock(cwd="/home/user/project")
        assert await CODEX.history_transcript(sm, "@5") == path

    @pytest.mark.asyncio
    async def test_codex_without_cwd_returns_none(self):
        from unittest.mock import MagicMock

        sm = MagicMock()
        sm.get_window_state.return_value = MagicMock(cwd="")
        assert await CODEX.history_transcript(sm, "@5") is None

    @pytest.mark.asyncio
    async def test_claude_delegates_to_session_resolution(self):
        from unittest.mock import AsyncMock, MagicMock

        sm = MagicMock()
        sm.resolve_session_for_window = AsyncMock(
            return_value=MagicMock(file_path="/tmp/x.jsonl")
        )
        from pathlib import Path as _P

        assert await CLAUDE.history_transcript(sm, "@1") == _P("/tmp/x.jsonl")
