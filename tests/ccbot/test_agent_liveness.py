"""Tests for agent liveness — who holds a pane's terminal, never the process name.

The dead-window reaper, /status and the restart's exit wait all ask
TmuxManager.agent_running_ids. These pin the rule (pane_agent_running), the
single-call window listing it feeds on, and — on a throwaway tmux server, when
tmux is installed — the kernel behaviour the rule stands on.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import time
from types import SimpleNamespace

import libtmux
import pytest

from ccbot import tmux_manager as tm_mod
from ccbot.tmux_manager import PaneState, TmuxManager, TmuxWindow, pane_agent_running

SHELL = 100  # pid of a pane's shell in the pure tests


def _pane(**overrides) -> PaneState:
    fields = {"pid": SHELL, "dead": False, "start_command": ""}
    fields.update(overrides)
    return PaneState(**fields)


class TestPaneAgentRunning:
    def test_idle_shell_is_not_running(self) -> None:
        assert pane_agent_running(_pane(), {SHELL: SHELL}, "") is False

    def test_foreground_job_is_running_whatever_its_name(self) -> None:
        # Another group holds the terminal. Its name — claude, node, codex, or
        # a native install's "2.1.267" after the CLI updated underneath — is
        # never consulted; that is what reaped live windows before.
        assert pane_agent_running(_pane(), {SHELL: 250}, "") is True

    def test_dead_pane_is_not_running_even_without_data(self) -> None:
        # remain-on-exit keeps a stale pid nobody can read; "unknown means
        # running" must not keep that pane alive forever.
        assert pane_agent_running(_pane(dead=True), {}, "") is False
        assert (
            pane_agent_running(_pane(dead=True, start_command="claude"), {}, "")
            is False
        )

    def test_unknown_foreground_counts_as_running(self) -> None:
        assert pane_agent_running(_pane(), {}, "") is True
        assert pane_agent_running(_pane(pid=None), {}, "") is True
        # A process without a controlling terminal reports 0 or -1.
        assert pane_agent_running(_pane(), {SHELL: 0}, "") is True
        assert pane_agent_running(_pane(), {SHELL: -1}, "") is True

    def test_pane_started_with_a_program_runs_while_it_lives(self) -> None:
        # new-window 'claude': the root IS the program and holds the terminal
        # itself, which the shell rule would read as an idle shell.
        pane = _pane(start_command="claude")
        assert pane_agent_running(pane, {SHELL: SHELL}, "") is True

    @pytest.mark.parametrize(
        ("start_command", "default_command"),
        [
            ('"zsh -l"', "zsh -l"),
            ("/bin/zsh", "/bin/zsh"),
            (
                "\"sh -c 'echo \\$HOME; exec zsh -i'\"",
                "sh -c 'echo $HOME; exec zsh -i'",
            ),
        ],
    )
    def test_default_command_pane_is_judged_as_a_shell(
        self, start_command: str, default_command: str
    ) -> None:
        # tmux copies default-command into every command-less pane, in its own
        # quoting (renderings captured from tmux 3.7b). Unmatched, a host with
        # `set -g default-command "$SHELL"` would never reap a window.
        pane = _pane(start_command=start_command)
        assert pane_agent_running(pane, {SHELL: SHELL}, default_command) is False
        assert pane_agent_running(pane, {SHELL: 250}, default_command) is True

    def test_unparsable_start_command_counts_as_a_program(self) -> None:
        pane = _pane(start_command='"unterminated')
        assert pane_agent_running(pane, {SHELL: SHELL}, "unterminated") is True


def _row(window_id: str, window_name: str, **overrides: str) -> SimpleNamespace:
    """A libtmux pane row: every field is a string, as libtmux delivers it."""
    fields = {
        "window_id": window_id,
        "window_name": window_name,
        "pane_active": "1",
        "pane_current_path": "/w",
        "pane_pid": "100",
        "pane_dead": "0",
        "pane_start_command": "",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestListWindows:
    async def test_one_listing_groups_panes_by_window(self, monkeypatch) -> None:
        manager = TmuxManager(session_name="test")
        session = SimpleNamespace(
            panes=[
                _row("@0", "__main__"),
                _row("@1", "proj", pane_active="0", pane_current_path="/split"),
                _row("@1", "proj", pane_current_path="/proj", pane_pid="200"),
                _row("@2", "other", pane_pid="", pane_dead="1"),
            ]
        )
        monkeypatch.setattr(manager, "get_session", lambda: session)

        windows = await manager.list_windows()

        assert [w.window_id for w in windows] == ["@1", "@2"]  # __main__ skipped
        proj, other = windows
        assert proj.cwd == "/proj"  # the ACTIVE pane's directory
        assert proj.panes == [PaneState(100, False, ""), PaneState(200, False, "")]
        # "0" must not read as dead, nor "" as a pid.
        assert other.panes == [PaneState(None, True, "")]

    async def test_no_session(self, monkeypatch) -> None:
        manager = TmuxManager(session_name="test")
        monkeypatch.setattr(manager, "get_session", lambda: None)
        assert await manager.list_windows() == []


class TestAgentRunningIds:
    @staticmethod
    def _manager(
        monkeypatch, foreground: dict[int, int], default_command: str = ""
    ) -> tuple[TmuxManager, list[list[int]], list[str]]:
        manager = TmuxManager(session_name="test")
        looked_up: list[list[int]] = []
        asked: list[str] = []

        def fake_foreground(pids):
            looked_up.append(list(pids))
            return dict(foreground)

        def fake_default() -> str:
            asked.append("default-command")
            return default_command

        monkeypatch.setattr(tm_mod, "foreground_process_groups", fake_foreground)
        monkeypatch.setattr(manager, "_default_command", fake_default)
        return manager, looked_up, asked

    async def test_any_running_pane_keeps_the_window(self, monkeypatch) -> None:
        # A split window whose focused half is a bare shell must not be reaped
        # while the agent runs in the other half.
        manager, _, _ = self._manager(monkeypatch, {100: 100, 200: 250, 300: 300})
        split = TmuxWindow(
            "@1", "proj", "/p", [PaneState(100, False, ""), PaneState(200, False, "")]
        )
        idle = TmuxWindow("@2", "idle", "/p", [PaneState(300, False, "")])
        assert await manager.agent_running_ids([split, idle]) == {"@1"}

    async def test_window_without_pane_data_counts_as_running(
        self, monkeypatch
    ) -> None:
        manager, _, _ = self._manager(monkeypatch, {})
        window = TmuxWindow("@1", "x", "/p")
        assert await manager.agent_running_ids([window]) == {"@1"}
        assert await manager.is_agent_running(window) is True

    async def test_dead_pane_pids_are_not_looked_up(self, monkeypatch) -> None:
        manager, looked_up, _ = self._manager(monkeypatch, {200: 200})
        window = TmuxWindow(
            "@1", "x", "/p", [PaneState(100, True, ""), PaneState(200, False, "")]
        )
        assert await manager.agent_running_ids([window]) == set()
        assert looked_up == [[200]]

    async def test_default_command_asked_only_when_a_pane_needs_it(
        self, monkeypatch
    ) -> None:
        manager, _, asked = self._manager(monkeypatch, {100: 100}, "zsh -l")
        plain = TmuxWindow("@1", "a", "/p", [PaneState(100, False, "")])
        assert await manager.agent_running_ids([plain]) == set()
        assert asked == []
        from_default = TmuxWindow("@2", "b", "/p", [PaneState(100, False, '"zsh -l"')])
        assert await manager.agent_running_ids([from_default]) == set()
        assert asked == ["default-command"]


class TestDefaultCommand:
    def test_reads_the_effective_option(self, monkeypatch) -> None:
        manager = TmuxManager(session_name="test")
        calls: list[tuple[str, ...]] = []

        def cmd(*args: str) -> SimpleNamespace:
            calls.append(args)
            return SimpleNamespace(stdout=["zsh -l"])

        monkeypatch.setattr(manager, "get_session", lambda: SimpleNamespace(cmd=cmd))
        assert manager._default_command() == "zsh -l"
        assert calls == [("show-options", "-Av", "default-command")]

    def test_unset_or_failing_reads_empty(self, monkeypatch) -> None:
        manager = TmuxManager(session_name="test")
        unset = SimpleNamespace(cmd=lambda *args: SimpleNamespace(stdout=[]))
        monkeypatch.setattr(manager, "get_session", lambda: unset)
        assert manager._default_command() == ""

        def boom(*args: str) -> SimpleNamespace:
            raise RuntimeError("tmux went away")

        monkeypatch.setattr(manager, "get_session", lambda: SimpleNamespace(cmd=boom))
        assert manager._default_command() == ""


TMUX = shutil.which("tmux")


@pytest.fixture
def tmux_server():
    """A throwaway tmux server on a private socket, /bin/sh panes, no config.

    /bin/sh and -f /dev/null keep the user's dotfiles out of the timing, and
    the private socket keeps the test away from any real ccbot session.
    Yields ``(tmux, manager, signal)`` — ``signal`` is the shell prefix that
    fires a ``wait-for`` channel from inside a pane.
    """
    assert TMUX is not None
    binary: str = TMUX
    socket = f"ccbot-liveness-{os.getpid()}"
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env.update(SHELL="/bin/sh", ENV="")

    def tmux(*args: str) -> str:
        return subprocess.run(
            [binary, "-L", socket, "-f", "/dev/null", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout

    tmux("new-session", "-d", "-s", "t", "-n", "agent")
    manager = TmuxManager(session_name="t")
    manager._server = libtmux.Server(socket_name=socket)
    try:
        yield tmux, manager, f"{shlex.quote(binary)} -L {socket} wait-for -S"
    finally:
        subprocess.run(
            [binary, "-L", socket, "kill-server"],
            env=env,
            capture_output=True,
            timeout=10,
        )


async def _settle(manager: TmuxManager, name: str, expected: bool) -> bool:
    """The window's liveness once it equals ``expected``, or after 10 s."""
    deadline = time.monotonic() + 10
    while True:
        window = await manager.find_window_by_name(name)
        running = window is not None and await manager.is_agent_running(window)
        if running is expected or time.monotonic() > deadline:
            return running
        await asyncio.sleep(0.05)


def _run_job(tmux, signal: str, target: str, channel: str) -> None:
    """Start a foreground job in ``target`` and wait until it holds the tty."""
    tmux("send-keys", "-t", target, f"( {signal} {channel}; exec sleep 300 )", "Enter")
    tmux("wait-for", channel)


@pytest.mark.skipif(TMUX is None, reason="tmux not installed")
class TestOnRealTmux:
    async def test_shell_pane_follows_its_foreground_job(self, tmux_server) -> None:
        tmux, manager, signal = tmux_server
        tmux("send-keys", "-t", "t:agent", f"{signal} ready", "Enter")
        tmux("wait-for", "ready")
        assert await _settle(manager, "agent", False) is False  # idle shell

        _run_job(tmux, signal, "t:agent", "started")
        assert await _settle(manager, "agent", True) is True  # job holds the tty

        tmux("send-keys", "-t", "t:agent", "C-c")
        assert await _settle(manager, "agent", False) is False  # back at the shell

    async def test_pane_started_with_a_program(self, tmux_server) -> None:
        tmux, manager, _ = tmux_server
        tmux("new-window", "-d", "-t", "t", "-n", "direct", "sleep 300")
        assert await _settle(manager, "direct", True) is True

    async def test_default_command_panes_keep_the_shell_rule(self, tmux_server) -> None:
        tmux, manager, signal = tmux_server
        tmux("set-option", "-t", "t", "default-command", "exec /bin/sh")
        tmux("new-window", "-d", "-t", "t", "-n", "dc")
        tmux("send-keys", "-t", "t:dc", f"{signal} dc-ready", "Enter")
        tmux("wait-for", "dc-ready")
        assert await _settle(manager, "dc", False) is False

        _run_job(tmux, signal, "t:dc", "dc-started")
        assert await _settle(manager, "dc", True) is True

    @pytest.mark.parametrize(
        "default_command",
        [
            "exec /bin/sh -l",  # a space → "…"
            "/bin/sh",  # nothing special → verbatim
            "sh -c 'echo $HOME; exit'",  # $ ' ; inside "…"
            'true "a b" `x`',  # " and ` escaped inside "…"
            "~/no-such-shell",  # leading ~ escaped
            'true"x"',  # a lone " → '…'
            "true\tx",  # control byte → C-style escape
            "sh -c 'printf %s a\\b; exit'",  # a literal backslash
            "sh -c 'echo привет; exit'",  # UTF-8 kept
        ],
    )
    def test_start_command_quoting_is_undone(
        self, tmux_server, default_command: str
    ) -> None:
        # Ground truth for _tmux_unescape: whatever this tmux renders for a
        # command-less pane must decode back to the option's value.
        tmux, _, _ = tmux_server
        tmux("set-option", "-wg", "remain-on-exit", "on")  # keep failed ones
        tmux("set-option", "-t", "t", "default-command", default_command)
        tmux("new-window", "-d", "-t", "t", "-n", "probe")
        start = tmux("display", "-p", "-t", "t:probe", "#{pane_start_command}")
        assert tm_mod._starts_default_shell(start.rstrip("\n"), default_command)

    async def test_remain_on_exit_dead_pane(self, tmux_server) -> None:
        tmux, manager, _ = tmux_server
        tmux("set-option", "-wg", "remain-on-exit", "on")
        tmux("new-window", "-d", "-t", "t", "-n", "gone", "true")
        assert await _settle(manager, "gone", False) is False
