"""Tests for the 🌳 "name the task" step — cancel button and TTL.

The naming state used to live forever with no way out: a curious 🌳 tap
meant the NEXT message in the topic (however much later) was silently
consumed as a worktree task name instead of reaching the agent.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.directory_browser import STATE_KEY
from ccbot.handlers.worktrees import (
    STATE_WT_NAMING,
    WT_NAMING_TTL_SEC,
    _clear_wt_naming,
    _handle_wt_cancel,
    _handle_wt_runtime,
    consume_worktree_name,
)


def _naming_context(thread_id: int = 42) -> MagicMock:
    context = MagicMock()
    context.user_data = {
        STATE_KEY: STATE_WT_NAMING,
        "_wt_repo": "/home/u/projects/x",
        "_wt_chat_id": -100123,
        "_wt_source_thread": thread_id,
        "_wt_deadline": time.monotonic() + WT_NAMING_TTL_SEC,
    }
    context.bot = AsyncMock()
    return context


def _update(thread_id: int = 42, text: str = "обычное сообщение") -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    update.message.text = text
    # get_thread_id reads update.message.message_thread_id
    update.message.message_thread_id = thread_id
    return update


class TestNamingTtl:
    @pytest.mark.asyncio
    async def test_expired_state_falls_through_to_normal_routing(self):
        context = _naming_context()
        context.user_data["_wt_deadline"] = time.monotonic() - 1.0
        consumed = await consume_worktree_name(_update(), context)
        assert consumed is False  # message continues to the agent
        assert STATE_KEY not in context.user_data  # and the trap is gone
        assert "_wt_repo" not in context.user_data

    @pytest.mark.asyncio
    async def test_fresh_state_consumes_the_name(self):
        context = _naming_context()
        with patch(
            "ccbot.handlers.worktrees.provision_worktree_agent",
            new=AsyncMock(return_value=(True, "ok")),
        ):
            with patch("ccbot.handlers.worktrees.safe_send", new=AsyncMock()):
                consumed = await consume_worktree_name(
                    _update(text="новая фича"), context
                )
        assert consumed is True
        assert STATE_KEY not in context.user_data


class TestRuntimeChoice:
    """🌳 → runtime pick → name. The chosen runtime rides through to
    provision_worktree_agent so the new branch runs Claude Code or Codex."""

    @pytest.mark.asyncio
    async def test_runtime_pick_sets_state_and_runtime(self):
        # After 🌳 (repo stashed, NO naming state yet), picking codex sets the
        # runtime and transitions to naming.
        context = MagicMock()
        context.user_data = {
            "_wt_repo": "/home/u/projects/x",
            "_wt_chat_id": -100123,
            "_wt_source_thread": 42,
        }
        context.bot = AsyncMock()
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        await _handle_wt_runtime(query, "wt:rt:codex", _update(), context, MagicMock())
        assert context.user_data["_wt_runtime"] == "codex"
        assert context.user_data[STATE_KEY] == STATE_WT_NAMING

    @pytest.mark.asyncio
    async def test_stale_runtime_pick_is_ignored(self):
        # No stashed repo (bot restart) → no state written, just a dismiss.
        context = MagicMock()
        context.user_data = {}
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        await _handle_wt_runtime(query, "wt:rt:codex", _update(), context, MagicMock())
        assert STATE_KEY not in context.user_data
        assert "_wt_runtime" not in context.user_data

    @pytest.mark.asyncio
    async def test_consume_forwards_chosen_runtime(self):
        context = _naming_context()
        context.user_data["_wt_runtime"] = "codex"
        prov = AsyncMock(return_value=(True, "ok"))
        with patch("ccbot.handlers.worktrees.provision_worktree_agent", new=prov):
            with patch("ccbot.handlers.worktrees.safe_send", new=AsyncMock()):
                await consume_worktree_name(_update(text="новая фича"), context)
        # runtime is the last positional arg to provision_worktree_agent.
        assert prov.await_args.args[-1] == "codex"

    @pytest.mark.asyncio
    async def test_consume_defaults_to_claude(self):
        context = _naming_context()  # no _wt_runtime set
        prov = AsyncMock(return_value=(True, "ok"))
        with patch("ccbot.handlers.worktrees.provision_worktree_agent", new=prov):
            with patch("ccbot.handlers.worktrees.safe_send", new=AsyncMock()):
                await consume_worktree_name(_update(text="фича"), context)
        assert prov.await_args.args[-1] == "claude"


class TestNamingCancel:
    @pytest.mark.asyncio
    async def test_cancel_button_clears_state(self):
        context = _naming_context()
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        await _handle_wt_cancel(query, "wt:abort", MagicMock(), context, MagicMock())
        assert STATE_KEY not in context.user_data
        assert "_wt_deadline" not in context.user_data
        query.answer.assert_awaited_once()

    def test_clear_helper_tolerates_none(self):
        _clear_wt_naming(None)  # must not raise
