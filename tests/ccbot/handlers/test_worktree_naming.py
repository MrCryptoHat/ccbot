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


class TestNoRuntimeStep:
    """🌳 goes straight from the repo to the task name. The agent (and the
    session) are picked in the NEW topic, on its first message — a worktree
    exists to run the same files differently, so inheriting the parent's CLI
    took away the choice it is for."""

    @pytest.mark.asyncio
    async def test_provision_takes_no_runtime(self):
        import inspect

        from ccbot.handlers.worktrees import provision_worktree_agent

        assert "runtime" not in inspect.signature(provision_worktree_agent).parameters

    @pytest.mark.asyncio
    async def test_consume_forwards_only_the_task(self):
        context = _naming_context()
        prov = AsyncMock(return_value=(True, "ok"))
        with patch("ccbot.handlers.worktrees.provision_worktree_agent", new=prov):
            with patch("ccbot.handlers.worktrees.safe_send", new=AsyncMock()):
                await consume_worktree_name(_update(text="new feature"), context)
        # (bot, user_id, chat_id, base_repo, repo_name, task_title)
        assert prov.await_args.args[-1] == "new feature"
        assert len(prov.await_args.args) == 6


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
