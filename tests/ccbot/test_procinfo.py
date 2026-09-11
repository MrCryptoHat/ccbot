"""Tests for procinfo — the /proc and ps readers behind pane liveness and the hook.

Both legs run without depending on the host: /proc through a fake proc root
(the Linux layout), ps through a stubbed subprocess.run (the macOS path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ccbot import procinfo
from ccbot.procinfo import foreground_process_groups, read_proc_stat


def _stat(root: Path, pid: int, line: str) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_text(line)


class TestReadProcStat:
    def test_comm_with_parens_and_spaces(self, tmp_path: Path) -> None:
        _stat(tmp_path, 7, "7 (weird (a) b) S 6 7 7 34816 9 0\n")
        entry = read_proc_stat(tmp_path, 7)
        assert entry is not None
        comm, fields = entry
        assert comm == "weird (a) b"
        assert fields[:2] == [b"S", b"6"]
        assert fields[5] == b"9"

    def test_missing_pid(self, tmp_path: Path) -> None:
        assert read_proc_stat(tmp_path, 7) is None


class TestForegroundProcessGroupsProc:
    """Linux: tpgid is proc(5) field 8."""

    def test_reads_tpgid(self, tmp_path: Path) -> None:
        # pid (comm) state ppid pgrp session tty_nr tpgid …
        _stat(tmp_path, 100, "100 (-zsh) S 99 100 100 34816 100 4194304\n")
        _stat(tmp_path, 200, "200 (zsh) S 99 200 200 34817 250 4194304\n")
        assert foreground_process_groups([100, 200], tmp_path) == {100: 100, 200: 250}

    def test_unreadable_pids_are_absent(self, tmp_path: Path) -> None:
        _stat(tmp_path, 100, "100 (zsh) S 99 100\n")  # truncated before tpgid
        _stat(tmp_path, 101, "101 (zsh) S 99 101 101 0 x 0\n")  # garbage tpgid
        assert foreground_process_groups([100, 101, 102], tmp_path) == {}

    def test_no_pids_reads_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(procinfo, "_ps_tpgids", lambda pids: pytest.fail("ps"))
        assert foreground_process_groups([], tmp_path / "nope") == {}


class TestForegroundProcessGroupsPs:
    """No /proc (macOS): one ps call for every pid."""

    @staticmethod
    def _stub(monkeypatch, stdout: str, returncode: int = 0) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        monkeypatch.setattr(procinfo.subprocess, "run", fake_run)
        return calls

    def test_one_call_for_all_pids(self, tmp_path: Path, monkeypatch) -> None:
        calls = self._stub(monkeypatch, "  100   100\n  200   250\n")
        result = foreground_process_groups([200, 100, 100], tmp_path / "nope")
        assert result == {100: 100, 200: 250}
        assert calls == [["ps", "-o", "pid=,tpgid=", "-p", "100,200"]]

    def test_nonzero_exit_keeps_printed_answers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A pid that vanished between listing and asking must not discard
        # the answers ps did print, whatever its exit status says.
        self._stub(monkeypatch, "  100   100\n", returncode=1)
        assert foreground_process_groups([100, 200], tmp_path / "nope") == {100: 100}

    def test_garbage_lines_are_skipped(self, tmp_path: Path, monkeypatch) -> None:
        self._stub(monkeypatch, "ps: warning\n  100   100\n  x y\n")
        assert foreground_process_groups([100], tmp_path / "nope") == {100: 100}

    def test_ps_unavailable(self, tmp_path: Path, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise OSError("no ps")

        monkeypatch.setattr(procinfo.subprocess, "run", boom)
        assert foreground_process_groups([100], tmp_path / "nope") == {}
