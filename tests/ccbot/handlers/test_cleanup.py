"""Tests for handlers.cleanup.purge_deleted_topic.

Called when Telegram reports a topic gone (Topic_id_invalid) — from a real send
in the queue worker (primary) or the periodic backstop probe. Must kill the
bound tmux window (but NOT for docker bindings, which have no window), unbind
the thread, and clear per-topic state.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.cleanup import purge_deleted_topic


@pytest.mark.asyncio
async def test_purge_tmux_binding_kills_window_and_unbinds():
    bot = AsyncMock()
    with (
        patch("ccbot.handlers.cleanup.session_manager") as mock_sm,
        patch("ccbot.handlers.cleanup.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.cleanup.clear_topic_state", AsyncMock()) as mock_clear,
    ):
        mock_sm._is_docker_binding.return_value = False
        mock_sm.get_worktree_meta.return_value = None
        window = MagicMock()
        window.window_id = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.kill_window = AsyncMock()

        await purge_deleted_topic(bot, user_id=1, thread_id=42, wid="@5")

        mock_tmux.kill_window.assert_awaited_once_with("@5")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)
        mock_clear.assert_awaited_once_with(1, 42, bot)


@pytest.mark.asyncio
async def test_purge_docker_binding_skips_window_kill():
    bot = AsyncMock()
    with (
        patch("ccbot.handlers.cleanup.session_manager") as mock_sm,
        patch("ccbot.handlers.cleanup.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.cleanup.clear_topic_state", AsyncMock()) as mock_clear,
    ):
        mock_sm._is_docker_binding.return_value = True
        mock_sm.get_worktree_meta.return_value = None
        mock_tmux.find_window_by_id = AsyncMock()
        mock_tmux.kill_window = AsyncMock()

        await purge_deleted_topic(bot, user_id=1, thread_id=99, wid="docker:assistant")

        mock_tmux.find_window_by_id.assert_not_called()
        mock_tmux.kill_window.assert_not_called()
        mock_sm.unbind_thread.assert_called_once_with(1, 99)
        mock_clear.assert_awaited_once_with(1, 99, bot)


@pytest.mark.asyncio
async def test_purge_tmux_binding_with_no_live_window_still_unbinds():
    bot = AsyncMock()
    with (
        patch("ccbot.handlers.cleanup.session_manager") as mock_sm,
        patch("ccbot.handlers.cleanup.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.cleanup.clear_topic_state", AsyncMock()) as mock_clear,
    ):
        mock_sm._is_docker_binding.return_value = False
        mock_sm.get_worktree_meta.return_value = None
        mock_tmux.find_window_by_id = AsyncMock(return_value=None)
        mock_tmux.kill_window = AsyncMock()

        await purge_deleted_topic(bot, user_id=7, thread_id=3, wid="@12")

        mock_tmux.kill_window.assert_not_called()
        mock_sm.unbind_thread.assert_called_once_with(7, 3)
        mock_clear.assert_awaited_once_with(7, 3, bot)


@pytest.mark.asyncio
async def test_purge_normal_topic_never_touches_git_or_worktree():
    # SAFETY LOCK: deleting a NORMAL (non-worktree) topic must never reach any
    # worktree teardown — no git, no filesystem removal. The base repo is safe.
    bot = AsyncMock()
    with (
        patch("ccbot.handlers.cleanup.session_manager") as mock_sm,
        patch("ccbot.handlers.cleanup.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.cleanup.clear_topic_state", AsyncMock()),
        patch(
            "ccbot.handlers.worktrees.handle_deleted_worktree_topic",
            AsyncMock(return_value=True),
        ) as mock_h,
    ):
        mock_sm.get_worktree_meta.return_value = None  # normal topic
        mock_sm._is_docker_binding.return_value = False
        window = MagicMock()
        window.window_id = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.kill_window = AsyncMock()

        await purge_deleted_topic(bot, user_id=1, thread_id=42, wid="@5")

        # the worktree teardown handler is NEVER invoked for a normal topic
        mock_h.assert_not_awaited()
        # only the standard window-kill + unbind happens
        mock_tmux.kill_window.assert_awaited_once_with("@5")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)


@pytest.mark.asyncio
async def test_purge_clean_worktree_does_full_teardown():
    # Hard-deleted CLEAN worktree topic → handler tears it down fully and the
    # standard window/unbind path is skipped (delete works like close).
    bot = AsyncMock()
    with (
        patch("ccbot.handlers.cleanup.session_manager") as mock_sm,
        patch("ccbot.handlers.cleanup.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.cleanup.clear_topic_state", AsyncMock()) as mock_clear,
        patch(
            "ccbot.handlers.worktrees.handle_deleted_worktree_topic",
            AsyncMock(return_value=True),
        ) as mock_h,
    ):
        mock_sm.get_worktree_meta.return_value = MagicMock()  # a worktree topic
        mock_tmux.kill_window = AsyncMock()

        await purge_deleted_topic(bot, user_id=1, thread_id=42, wid="@5")

        mock_h.assert_awaited_once()
        mock_tmux.kill_window.assert_not_called()
        mock_sm.unbind_thread.assert_not_called()
        mock_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_dirty_worktree_falls_through_to_standard():
    # Hard-deleted DIRTY/unmerged worktree → handler returns False (preserved on
    # disk, flagged orphaned), then the standard window/unbind teardown runs.
    bot = AsyncMock()
    with (
        patch("ccbot.handlers.cleanup.session_manager") as mock_sm,
        patch("ccbot.handlers.cleanup.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.cleanup.clear_topic_state", AsyncMock()) as mock_clear,
        patch(
            "ccbot.handlers.worktrees.handle_deleted_worktree_topic",
            AsyncMock(return_value=False),
        ),
    ):
        mock_sm._is_docker_binding.return_value = False
        mock_sm.get_worktree_meta.return_value = MagicMock()
        window = MagicMock()
        window.window_id = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=window)
        mock_tmux.kill_window = AsyncMock()

        await purge_deleted_topic(bot, user_id=1, thread_id=42, wid="@5")

        mock_tmux.kill_window.assert_awaited_once_with("@5")
        mock_sm.unbind_thread.assert_called_once_with(1, 42)
        mock_clear.assert_awaited_once_with(1, 42, bot)
