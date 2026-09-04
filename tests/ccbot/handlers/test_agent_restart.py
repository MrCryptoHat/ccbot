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
