"""Tests for TmuxManager.send_keys — literal sends must survive tmux flag parsing.

libtmux's pane.send_keys(literal=True) builds `send-keys -l <text>` without
`--`, so text starting with "-" is eaten as tmux flags and the send silently
fails. The manager therefore issues `send-keys -l -- <text>` via pane.cmd();
these tests pin that argv shape with fake libtmux objects (no real tmux).
"""

from __future__ import annotations

import asyncio

import pytest

from ccbot.tmux_manager import TmuxManager


class _FakeCmdResult:
    def __init__(self, stderr: list[str] | None = None):
        self.stderr = stderr or []


class _FakePane:
    def __init__(self, stderr: list[str] | None = None):
        self.cmd_calls: list[tuple] = []
        self.send_keys_calls: list[tuple] = []
        self._stderr = stderr

    def cmd(self, *args):
        self.cmd_calls.append(args)
        return _FakeCmdResult(self._stderr)

    def send_keys(self, text, enter=True, literal=False):
        self.send_keys_calls.append((text, enter, literal))


class _FakeWindows:
    def __init__(self, window):
        self._window = window

    def get(self, window_id=None):
        return self._window


class _FakeWindow:
    def __init__(self, pane):
        self.active_pane = pane


class _FakeSession:
    def __init__(self, window):
        self.windows = _FakeWindows(window)


@pytest.fixture
def manager_with_fake_pane(monkeypatch):
    """TmuxManager whose get_session returns fakes; sleeps neutered."""
    manager = TmuxManager(session_name="test")
    pane = _FakePane()
    session = _FakeSession(_FakeWindow(pane))
    monkeypatch.setattr(manager, "get_session", lambda: session)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return manager, pane


class TestLiteralSendArgv:
    async def test_leading_dash_text_survives_flag_parsing(
        self, manager_with_fake_pane
    ) -> None:
        manager, pane = manager_with_fake_pane
        ok = await manager.send_keys("@1", "-1 за такой вариант")
        assert ok is True
        assert pane.cmd_calls[0] == ("send-keys", "-l", "--", "-1 за такой вариант")
        # Enter is sent as an interpreted key afterwards.
        assert pane.send_keys_calls == [("", True, False)]

    async def test_literal_no_enter_uses_guarded_argv(
        self, manager_with_fake_pane
    ) -> None:
        manager, pane = manager_with_fake_pane
        ok = await manager.send_keys("@1", "--help", enter=False, literal=True)
        assert ok is True
        assert pane.cmd_calls == [("send-keys", "-l", "--", "--help")]
        assert pane.send_keys_calls == []

    async def test_named_key_keeps_interpreted_path(
        self, manager_with_fake_pane
    ) -> None:
        manager, pane = manager_with_fake_pane
        ok = await manager.send_keys("@1", "Escape", enter=False, literal=False)
        assert ok is True
        assert pane.cmd_calls == []
        assert pane.send_keys_calls == [("Escape", False, False)]

    async def test_long_text_chunked_each_chunk_guarded(
        self, manager_with_fake_pane
    ) -> None:
        manager, pane = manager_with_fake_pane
        text = "a" * 450  # chunks of 200, 200, 50
        ok = await manager.send_keys("@1", text)
        assert ok is True
        chunks = [c[-1] for c in pane.cmd_calls]
        assert chunks == ["a" * 200, "a" * 200, "a" * 50]
        for call in pane.cmd_calls:
            assert call[:3] == ("send-keys", "-l", "--")

    async def test_send_keys_stderr_reports_failure(self, monkeypatch) -> None:
        manager = TmuxManager(session_name="test")
        pane = _FakePane(stderr=["unknown flag"])
        session = _FakeSession(_FakeWindow(pane))
        monkeypatch.setattr(manager, "get_session", lambda: session)

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        ok = await manager.send_keys("@1", "text")
        assert ok is False


class TestSessionEnvExport:
    """get_or_create_session must publish CCBOT_DIR into the tmux session env.

    The SessionStart hook runs in agent panes and inherits the tmux server
    environment, not the bot's — a `.env`-only CCBOT_DIR would silently split
    session_map.json across two directories without this export.
    """

    def test_existing_session_gets_ccbot_dir(self, monkeypatch) -> None:
        from ccbot.config import config

        class _EnvSession:
            def __init__(self):
                self.env_set: list[tuple[str, str]] = []
                self.env_unset: list[str] = []
                self.windows = []

            def set_environment(self, name, value):
                self.env_set.append((name, value))

            def unset_environment(self, name):
                self.env_unset.append(name)

        manager = TmuxManager(session_name="test")
        session = _EnvSession()
        monkeypatch.setattr(manager, "get_session", lambda: session)

        result = manager.get_or_create_session()

        assert result is session
        assert ("CCBOT_DIR", str(config.config_dir)) in session.env_set
