"""Tests for Claude Code session tracking hook."""

import io
import json
import sys
from pathlib import Path

import pytest

import ccbot.hook as hook
from ccbot.hook import (
    _UUID_RE,
    _count_claude_ancestors,
    _is_hook_installed,
    hook_main,
)


class TestUuidRegex:
    @pytest.mark.parametrize(
        "value",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "00000000-0000-0000-0000-000000000000",
            "abcdef01-2345-6789-abcd-ef0123456789",
        ],
        ids=["standard", "all-zeros", "all-hex"],
    )
    def test_valid_uuid_matches(self, value: str) -> None:
        assert _UUID_RE.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "550e8400-e29b-41d4-a716",
            "550e8400-e29b-41d4-a716-44665544000g",
            "",
        ],
        ids=["gibberish", "truncated", "invalid-hex-char", "empty"],
    )
    def test_invalid_uuid_no_match(self, value: str) -> None:
        assert _UUID_RE.match(value) is None


class TestIsHookInstalled:
    def test_hook_present(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "ccbot hook", "timeout": 5}
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True

    def test_no_hooks_key(self) -> None:
        assert _is_hook_installed({}) is False

    def test_different_hook_command(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "other-tool hook"}]}
                ]
            }
        }
        assert _is_hook_installed(settings) is False

    def test_full_path_matches(self) -> None:
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/usr/bin/ccbot hook",
                                "timeout": 5,
                            }
                        ]
                    }
                ]
            }
        }
        assert _is_hook_installed(settings) is True


class TestHookInstalledInSettings:
    """The file-reading wrapper used by the boot/bind-time first-run check."""

    def test_missing_file_is_not_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", tmp_path / "settings.json")
        assert hook.hook_installed_in_settings() is False

    def test_invalid_json_is_not_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "settings.json"
        f.write_text("{not json")
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", f)
        assert hook.hook_installed_in_settings() is False

    def test_non_dict_json_is_not_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = tmp_path / "settings.json"
        f.write_text('["a list"]')
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", f)
        assert hook.hook_installed_in_settings() is False

    @staticmethod
    def _write_settings(tmp_path: Path, command: str) -> Path:
        f = tmp_path / "settings.json"
        f.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": command}]}
                        ]
                    }
                }
            )
        )
        return f

    def test_installed_hook_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        exe = tmp_path / "bin" / "ccbot"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        f = self._write_settings(tmp_path, f"{exe} hook")
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", f)
        assert hook.hook_installed_in_settings() is True

    def test_stale_executable_path_counts_as_not_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The recorded venv path died (repo renamed/moved) — the hook is
        # present in settings but can never run; must NOT count as installed.
        f = self._write_settings(tmp_path, f"{tmp_path}/gone/.venv/bin/ccbot hook")
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", f)
        assert hook.hook_installed_in_settings() is False

    def test_bare_name_resolves_via_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = self._write_settings(tmp_path, "ccbot hook")
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", f)
        monkeypatch.setattr(hook.shutil, "which", lambda _name: "/usr/bin/ccbot")
        assert hook.hook_installed_in_settings() is True
        monkeypatch.setattr(hook.shutil, "which", lambda _name: None)
        assert hook.hook_installed_in_settings() is False


class TestInstallHookRepair:
    def test_stale_path_is_repaired_in_place(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        f = TestHookInstalledInSettings._write_settings(
            tmp_path, "/old/gone/.venv/bin/ccbot hook"
        )
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", f)
        monkeypatch.setattr(hook, "_find_ccbot_path", lambda: "/new/venv/bin/ccbot")
        assert hook._install_hook() == 0
        saved = json.loads(f.read_text())
        commands = [
            h["command"]
            for entry in saved["hooks"]["SessionStart"]
            for h in entry["hooks"]
        ]
        assert commands == ["/new/venv/bin/ccbot hook"]

    def test_healthy_install_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        exe = tmp_path / "bin" / "ccbot"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        f = TestHookInstalledInSettings._write_settings(tmp_path, f"{exe} hook")
        monkeypatch.setattr(hook, "_CLAUDE_SETTINGS_FILE", f)
        before = f.read_text()
        assert hook._install_hook() == 0
        assert f.read_text() == before


def _fake_proc(tmp_path: Path, chain: list[tuple[int, str, int]]) -> Path:
    """Build a minimal fake /proc from (pid, comm, ppid) triples.

    Returns the proc root. Each /proc/<pid>/stat mimics the real layout
    ("<pid> (<comm>) <state> <ppid> …") closely enough for the parser.
    """
    root = tmp_path / "proc"
    for pid, comm, ppid in chain:
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0 0 -1 0 0 0\n")
    return root


class TestCountClaudeAncestors:
    def test_no_proc_returns_none(self, tmp_path: Path) -> None:
        assert _count_claude_ancestors(tmp_path / "nope", start_pid=1) is None

    def test_single_interactive_claude(self, tmp_path: Path) -> None:
        # ccbot-hook -> sh -> claude -> pane-shell -> tmux -> init
        root = _fake_proc(
            tmp_path,
            [
                (100, "ccbot", 99),
                (99, "sh", 50),
                (50, "claude", 40),
                (40, "bash", 10),
                (10, "tmux: server", 1),
            ],
        )
        assert _count_claude_ancestors(root, start_pid=100) == 1

    def test_nested_claude_p(self, tmp_path: Path) -> None:
        # ccbot-hook -> sh -> claude(-p) -> timeout -> bash -> claude(interactive)
        #   -> pane-shell -> tmux -> init
        root = _fake_proc(
            tmp_path,
            [
                (200, "ccbot", 199),
                (199, "sh", 150),
                (150, "claude", 145),
                (145, "timeout", 140),
                (140, "bash", 50),
                (50, "claude", 40),
                (40, "bash", 10),
                (10, "tmux: server", 1),
            ],
        )
        assert _count_claude_ancestors(root, start_pid=200) == 2

    def test_no_claude_in_chain(self, tmp_path: Path) -> None:
        root = _fake_proc(tmp_path, [(5, "ccbot", 4), (4, "bash", 1)])
        assert _count_claude_ancestors(root, start_pid=5) == 0

    def test_comm_with_parens_and_spaces(self, tmp_path: Path) -> None:
        root = tmp_path / "proc"
        (root / "7").mkdir(parents=True)
        (root / "7" / "stat").write_text("7 (weird (a) b) S 6 0 0\n")
        (root / "6").mkdir(parents=True)
        (root / "6" / "stat").write_text("6 (claude) S 1 0 0\n")
        assert _count_claude_ancestors(root, start_pid=7) == 1

    def test_cycle_is_bounded(self, tmp_path: Path) -> None:
        root = _fake_proc(tmp_path, [(3, "claude", 2), (2, "claude", 3)])
        assert _count_claude_ancestors(root, start_pid=3) == 2

    def test_missing_stat_stops_walk(self, tmp_path: Path) -> None:
        # pid 2 has no /proc entry — walk stops cleanly at the gap.
        root = _fake_proc(tmp_path, [(4, "claude", 2)])
        assert _count_claude_ancestors(root, start_pid=4) == 1


class TestHookMainValidation:
    def _run_hook_main(
        self, monkeypatch: pytest.MonkeyPatch, payload: dict, *, tmux_pane: str = ""
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ccbot", "hook"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        if tmux_pane:
            monkeypatch.setenv("TMUX_PANE", tmux_pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        hook_main()

    def test_missing_session_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {"cwd": "/tmp", "hook_event_name": "SessionStart"},
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_invalid_uuid_format(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "not-a-uuid",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_relative_cwd(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "relative/path",
                "hook_event_name": "SessionStart",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_non_session_start_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/tmp",
                "hook_event_name": "Stop",
            },
        )
        assert not (tmp_path / "session_map.json").exists()

    def test_nested_claude_does_not_clobber_map(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # A nested `claude -p` (2+ claude ancestors) must leave session_map alone.
        monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
        monkeypatch.setattr("ccbot.hook._count_claude_ancestors", lambda: 2)
        self._run_hook_main(
            monkeypatch,
            {
                "session_id": "550e8400-e29b-41d4-a716-446655440000",
                "cwd": "/home/user/agents/demo",
                "hook_event_name": "SessionStart",
            },
            tmux_pane="%34",
        )
        assert not (tmp_path / "session_map.json").exists()
