"""🔄 brings the agent back even after its window is gone.

An agent that crashes (Claude's trust gate, a killed pane) is reaped as a dead
window 30 s later, taking the topic's binding with it. Restart used to answer
«window gone», and since auto-bind no longer starts sessions the only way back
was the picker. revive_topic_agent rebuilds the window from what the topic
remembers — same folder, same CLI, same session.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers import agent_restart as ar

NEWEST = "11111111-2222-3333-4444-555555555555"
PINNED = "99999999-8888-7777-6666-555555555555"


def _mocks(tmp_path, *, sessions=None, window_state=None):
    sm = MagicMock()
    sm.get_remembered_directory.return_value = str(tmp_path)
    sm.get_remembered_runtime.return_value = "claude"
    sm.get_window_for_thread.return_value = None
    sm.window_states = {}
    sm.has_live_agent_on_cwd = AsyncMock(return_value=False)
    sm.wait_for_session_map_entry = AsyncMock(return_value=True)
    sm.get_window_state.return_value = window_state or SimpleNamespace(
        session_id="", cwd="", window_name=""
    )
    tm = MagicMock()
    tm.create_window = AsyncMock(return_value=(True, "ok", "proj", "@9"))
    rt = MagicMock()
    rt.name = "claude"
    rt.is_available.return_value = True
    rt.uses_session_map = True
    rt.list_sessions = AsyncMock(
        return_value=[SimpleNamespace(session_id=s) for s in (sessions or [])]
    )
    return sm, tm, rt


@pytest.mark.asyncio
async def test_resumes_the_newest_session_of_the_folder(tmp_path):
    sm, tm, rt = _mocks(tmp_path, sessions=[NEWEST])
    with (
        patch.object(ar, "session_manager", sm),
        patch.object(ar, "tmux_manager", tm),
        patch.object(ar, "get_runtime", return_value=rt),
    ):
        wid, display = await ar.revive_topic_agent(1, 42)

    assert (wid, display) == ("@9", "proj")
    assert tm.create_window.await_args.kwargs["resume_session_id"] == NEWEST
    assert tm.create_window.await_args.kwargs["runtime"] == "claude"
    sm.bind_thread.assert_called_once()


@pytest.mark.asyncio
async def test_live_window_state_wins_over_the_folder_listing(tmp_path):
    sm, tm, rt = _mocks(tmp_path, sessions=[NEWEST])
    sm.get_window_for_thread.return_value = "@7"
    sm.window_states = {"@7": SimpleNamespace(session_id=PINNED)}
    with (
        patch.object(ar, "session_manager", sm),
        patch.object(ar, "tmux_manager", tm),
        patch.object(ar, "get_runtime", return_value=rt),
    ):
        await ar.revive_topic_agent(1, 42)

    assert tm.create_window.await_args.kwargs["resume_session_id"] == PINNED


@pytest.mark.asyncio
async def test_fresh_skips_the_resume(tmp_path):
    sm, tm, rt = _mocks(tmp_path, sessions=[NEWEST])
    with (
        patch.object(ar, "session_manager", sm),
        patch.object(ar, "tmux_manager", tm),
        patch.object(ar, "get_runtime", return_value=rt),
    ):
        await ar.revive_topic_agent(1, 42, fresh=True)

    assert tm.create_window.await_args.kwargs["resume_session_id"] is None


@pytest.mark.asyncio
async def test_nothing_remembered_is_a_typed_error(tmp_path):
    sm, tm, rt = _mocks(tmp_path)
    sm.get_remembered_directory.return_value = None
    with (
        patch.object(ar, "session_manager", sm),
        patch.object(ar, "tmux_manager", tm),
        patch.object(ar, "get_runtime", return_value=rt),
    ):
        with pytest.raises(ar.ReviveError) as e:
            await ar.revive_topic_agent(1, 42)

    assert e.value.key == "restart.nothing_to_revive"
    tm.create_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_timeout_pins_the_resumed_session(tmp_path):
    # The monitor tracks the ORIGINAL JSONL: --resume makes the hook report a
    # new id, and a timed-out hook reports nothing at all.
    ws = SimpleNamespace(session_id="", cwd="", window_name="")
    sm, tm, rt = _mocks(tmp_path, sessions=[NEWEST], window_state=ws)
    sm.wait_for_session_map_entry = AsyncMock(return_value=False)
    with (
        patch.object(ar, "session_manager", sm),
        patch.object(ar, "tmux_manager", tm),
        patch.object(ar, "get_runtime", return_value=rt),
    ):
        await ar.revive_topic_agent(1, 42)

    assert ws.session_id == NEWEST
    assert ws.cwd == str(tmp_path)
    assert ws.window_name == "proj"


@pytest.mark.asyncio
async def test_hookless_runtime_refuses_a_second_window_on_the_cwd(tmp_path):
    sm, tm, rt = _mocks(tmp_path)
    rt.uses_session_map = False
    rt.display_name = "Codex"
    sm.has_live_agent_on_cwd = AsyncMock(return_value=True)
    with (
        patch.object(ar, "session_manager", sm),
        patch.object(ar, "tmux_manager", tm),
        patch.object(ar, "get_runtime", return_value=rt),
    ):
        with pytest.raises(ar.ReviveError) as e:
            await ar.revive_topic_agent(1, 42)

    assert e.value.key == "bot.same_dir_conflict"
    tm.create_window.assert_not_awaited()


class TestReviveDockerAgent:
    """A container agent dies with its container's tmux server — the topic,
    its files and its transcripts all survive, so recovery is just starting
    that session again (never re-creating the topic)."""

    def _docker_mocks(self, *, alive: bool = True, started: bool = True):
        sm = MagicMock()
        sm.resolve_docker_target.return_value = SimpleNamespace(
            agent=SimpleNamespace(container="ctn", name="assistant"),
            tmux_session="claude-notes",
            sub="notes",
        )
        sm.get_window_state.return_value = SimpleNamespace(
            session_id="old", cwd="/workspace"
        )
        sm.start_docker_agent = AsyncMock(return_value=started)
        sm.send_lock = MagicMock(return_value=_NullLock())
        sm._save_state = MagicMock()
        drv = MagicMock()
        drv.is_container_alive = AsyncMock(return_value=alive)
        drv.kill_session = AsyncMock(return_value=True)
        return sm, drv

    @pytest.mark.asyncio
    async def test_resume_pins_the_chosen_transcript(self):
        sm, drv = self._docker_mocks()
        state = SimpleNamespace(session_id="whatever-the-hook-said", cwd="/workspace")
        sm.get_window_state.return_value = state
        with (
            patch.object(ar, "session_manager", sm),
            patch("ccbot.docker_driver.docker_driver", drv),
            patch("ccbot.handlers.agent_restart.asyncio.sleep", AsyncMock()),
        ):
            await ar.revive_docker_agent("docker:assistant/notes", session_id=PINNED)

        kwargs = sm.start_docker_agent.await_args.kwargs
        assert kwargs["resume_session_id"] == PINNED
        assert kwargs["new_session_id"] is None
        assert kwargs["cwd"] == "/workspace"
        # The monitor must read the conversation the user picked, not whatever
        # id the container's hook reports for a --resume.
        assert state.session_id == PINNED

    @pytest.mark.asyncio
    async def test_fresh_start_pins_a_new_id(self):
        sm, drv = self._docker_mocks()
        with (
            patch.object(ar, "session_manager", sm),
            patch("ccbot.docker_driver.docker_driver", drv),
            patch("ccbot.handlers.agent_restart.asyncio.sleep", AsyncMock()),
        ):
            await ar.revive_docker_agent("docker:assistant/notes", session_id=None)

        kwargs = sm.start_docker_agent.await_args.kwargs
        assert kwargs["resume_session_id"] is None
        assert kwargs["new_session_id"]  # ccbot picks the id up front

    @pytest.mark.asyncio
    async def test_dead_container_is_refused_not_started(self):
        sm, drv = self._docker_mocks(alive=False)
        with (
            patch.object(ar, "session_manager", sm),
            patch("ccbot.docker_driver.docker_driver", drv),
        ):
            with pytest.raises(ar.ReviveError) as e:
                await ar.revive_docker_agent("docker:assistant/notes", session_id=None)
        assert e.value.key == "revive.container_down"
        sm.start_docker_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_crafted_session_id_never_reaches_the_command_line(self):
        sm, drv = self._docker_mocks()
        with (
            patch.object(ar, "session_manager", sm),
            patch("ccbot.docker_driver.docker_driver", drv),
        ):
            with pytest.raises(ar.ReviveError) as e:
                await ar.revive_docker_agent(
                    "docker:assistant/notes", session_id="x; rm -rf /"
                )
        assert e.value.key == "revive.bad_session"
        sm.start_docker_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_start_raises(self):
        sm, drv = self._docker_mocks(started=False)
        with (
            patch.object(ar, "session_manager", sm),
            patch("ccbot.docker_driver.docker_driver", drv),
            patch("ccbot.handlers.agent_restart.asyncio.sleep", AsyncMock()),
        ):
            with pytest.raises(ar.ReviveError) as e:
                await ar.revive_docker_agent("docker:assistant/notes", session_id=None)
        assert e.value.key == "revive.start_failed"


class TestReviveOptions:
    @pytest.mark.asyncio
    async def test_a_conversation_another_topic_holds_is_not_offered(self):
        """One container = one claude-home, so the list is every agent's. Two
        agents resuming one transcript would mirror it into two topics."""
        sm = MagicMock()
        sm.window_states = {"docker:a/one": SimpleNamespace(session_id="mine")}
        sm.list_agent_sessions = AsyncMock(
            return_value=[
                SimpleNamespace(session_id="mine", summary="mine", file_path="/m"),
                SimpleNamespace(session_id="busy", summary="parent", file_path="/b"),
                SimpleNamespace(session_id="free", summary="older", file_path="/f"),
            ]
        )
        sm.session_ids_of_other_bindings.return_value = {"busy"}
        with patch.object(ar, "session_manager", sm):
            last, others = await ar.docker_revive_options("docker:a/one")

        assert last is not None and last.session_id == "mine"
        assert [s.session_id for s in others] == ["free"]


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestReviveOffer:
    @pytest.mark.asyncio
    async def test_a_stopped_container_says_so_instead_of_offering_buttons(self):
        sm = MagicMock()
        sm.get_display_name.return_value = "assistant-notes"
        sm.resolve_docker_target.return_value = SimpleNamespace(
            agent=SimpleNamespace(container="ctn", name="assistant"),
            tmux_session="claude-notes",
            sub="notes",
        )
        drv = MagicMock()
        drv.is_container_alive = AsyncMock(return_value=False)
        send = AsyncMock()
        with (
            patch.object(ar, "session_manager", sm),
            patch("ccbot.docker_driver.docker_driver", drv),
            patch.object(ar, "safe_send", send),
        ):
            sent = await ar.offer_docker_revive(
                MagicMock(), -100, 7, "docker:assistant/notes"
            )

        assert sent is True
        assert send.await_args.kwargs.get("reply_markup") is None

    @pytest.mark.asyncio
    async def test_offer_carries_continue_fresh_and_earlier(self):
        last = SimpleNamespace(session_id=PINNED, summary="ui work", file_path="/a")
        others = [SimpleNamespace(session_id=NEWEST, summary="older", file_path="/b")]
        kb = ar.build_revive_keyboard(last, others)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        assert len(labels) == 3
        assert any(PINNED in b.callback_data for row in kb.inline_keyboard for b in row)

    def test_earlier_list_is_capped_and_ends_with_back(self):
        many = [
            SimpleNamespace(session_id=f"{i}" * 8, summary=f"s{i}", file_path="/x")
            for i in range(10)
        ]
        kb = ar.build_revive_session_list(many)
        assert len(kb.inline_keyboard) == ar.REVIVE_SESSION_ROWS + 1
        assert kb.inline_keyboard[-1][0].callback_data.endswith("back")
