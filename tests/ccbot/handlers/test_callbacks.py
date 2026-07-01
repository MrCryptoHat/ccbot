"""Tests for callback handlers — agent-panel restart guard.

The tmux restart types /exit + the relaunch command into the pane; on a
busy agent both would land in Claude's prompt as user text and the
"restarted" success check would still pass (the process never exited).
The guard refuses with an alert instead.
"""

from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callbacks import _restart_agent, _wait_pane_ready


@pytest.fixture
def query():
    q = MagicMock()
    q.answer = AsyncMock()
    return q


class TestWaitPaneReady:
    """After a kill+relaunch the panel must screenshot only once Claude's
    input box has rendered — never the black boot frame, and never hang."""

    @pytest.mark.asyncio
    async def test_returns_once_input_box_renders(self):
        ready = "❯ \n" + "─" * 100 + "\n"
        # First poll: still booting (empty). Second: input box drawn.
        cap = AsyncMock(side_effect=["", ready])
        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.asyncio.sleep", new=AsyncMock()),
        ):
            sm.capture_pane = cap
            await _wait_pane_ready("docker:assistant", timeout=5.0)
        assert cap.await_count == 2

    @pytest.mark.asyncio
    async def test_times_out_without_hanging(self, monkeypatch):
        # Pane never renders an input box → must give up at the deadline
        # (and fall through to screenshot whatever's there), not loop forever.
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "ccbot.handlers.callbacks.time.monotonic", lambda: clock["t"]
        )

        async def _tick(_):
            clock["t"] += 1.0

        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.asyncio.sleep", new=_tick),
        ):
            sm.capture_pane = AsyncMock(return_value="loading...\n")
            await _wait_pane_ready("docker:assistant", timeout=3.0)
        # Bounded: stopped at the deadline rather than spinning.
        assert sm.capture_pane.await_count <= 4


class TestRestartBusyGuard:
    @pytest.mark.asyncio
    async def test_busy_tmux_agent_refused_with_alert(self, query):
        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.tmux_manager") as tm,
            patch("ccbot.handlers.callbacks.is_claude_working", return_value=True),
        ):
            sm._is_docker_binding.return_value = False
            sm.get_window_state.return_value = MagicMock(session_id="sid")
            tm.find_window_by_id = AsyncMock(return_value=MagicMock())
            tm.capture_pane = AsyncMock(return_value="✻ Cooking… (esc to interrupt)")
            tm.send_keys = AsyncMock()

            await _restart_agent(query, "@5", fresh=False)

            tm.send_keys.assert_not_called()
            assert query.answer.await_count == 1
            assert query.answer.await_args.kwargs.get("show_alert") is True

    @pytest.mark.asyncio
    async def test_idle_tmux_agent_restarts(self, query, monkeypatch):
        async def _no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.tmux_manager") as tm,
            patch("ccbot.handlers.callbacks.is_claude_working", return_value=False),
            patch("ccbot.handlers.callbacks.config") as cfg,
            patch("ccbot.handlers.callbacks._cmd_refresh_photo", new=AsyncMock()),
        ):
            cfg.claude_command = "claude"
            sm._is_docker_binding.return_value = False
            sm.get_window_state.return_value = MagicMock(session_id="sid-1")
            tm.find_window_by_id = AsyncMock(return_value=MagicMock())
            tm.capture_pane = AsyncMock(return_value="❯ ")
            tm.send_keys = AsyncMock()
            # _wait_pane_ready polls session_manager.capture_pane until the
            # input box (chrome separator) renders — return a ready pane so it
            # returns on the first poll instead of looping to its timeout.
            sm.capture_pane = AsyncMock(return_value="❯ \n" + "─" * 100 + "\n")

            await _restart_agent(query, "@5", fresh=False)

            sent = [c.args[1] for c in tm.send_keys.await_args_list]
            assert sent == ["/exit", "claude --resume sid-1"]
