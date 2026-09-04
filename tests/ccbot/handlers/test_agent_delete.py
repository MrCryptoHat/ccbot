"""Tests for 🗑 delete-agent-and-topic on non-worktree topics.

Worktree topics keep their own guarded flow; everything else lands here. What
must hold: the confirm says what actually gets killed for THIS kind of binding,
the confirmed delete runs the standard teardown and then removes the topic, and
cancel puts the panel back on the tab the button lives on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.agent_delete import (
    _confirm_caption,
    _handle_agent_del,
    _handle_agent_delno,
    _handle_agent_delok,
)
from ccbot.handlers.callback_data import CB_AGENT_DEL, CB_AGENT_DELNO, CB_AGENT_DELOK


def _query() -> MagicMock:
    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_caption = AsyncMock()
    return query


def _context() -> MagicMock:
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.delete_forum_topic = AsyncMock()
    return context


def _update(thread_id: int | None = 42) -> MagicMock:
    update = MagicMock()
    update.message.message_thread_id = thread_id
    return update


class TestConfirmCaption:
    def test_each_binding_kind_gets_its_own_copy(self) -> None:
        captions = {
            "@12": _confirm_caption("@12", "proj"),
            "docker:assistant": _confirm_caption("docker:assistant", "assistant"),
            "docker:assistant/notes": _confirm_caption(
                "docker:assistant/notes", "assistant-notes"
            ),
        }
        assert len(set(captions.values())) == 3
        assert all("proj" in captions["@12"] for _ in [0])
        # The container's own agent keeps running — the copy must not promise
        # otherwise, since purge_deleted_topic deliberately doesn't kill it.
        assert "assistant" in captions["docker:assistant"]


class TestConfirmStep:
    @pytest.mark.asyncio
    async def test_shows_confirm_with_thread_in_payload(self) -> None:
        query = _query()
        context = _context()
        context.user_data = {}
        with patch("ccbot.handlers.agent_delete.session_manager") as sm:
            sm.is_docker_sub_agent.return_value = False
            sm._is_docker_binding.return_value = False
            sm.get_display_name.return_value = "proj"
            sm.get_window_for_thread.return_value = "@12"
            await _handle_agent_del(
                query, f"{CB_AGENT_DEL}@12", _update(42), context, MagicMock(id=1)
            )
        # What the caption described is recorded for the ✅ step to verify.
        assert context.user_data["_agentdel_target"] == (42, "@12")
        markup = query.edit_message_caption.await_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == f"{CB_AGENT_DELOK}42"
        assert markup.inline_keyboard[1][0].callback_data == f"{CB_AGENT_DELNO}@12"

    @pytest.mark.asyncio
    async def test_outside_a_topic_is_refused(self) -> None:
        query = _query()
        with patch("ccbot.handlers.agent_delete.session_manager"):
            await _handle_agent_del(
                query, f"{CB_AGENT_DEL}@12", _update(None), _context(), MagicMock(id=1)
            )
        query.edit_message_caption.assert_not_awaited()
        assert query.answer.await_args.kwargs.get("show_alert") is True


class TestConfirmedDelete:
    @pytest.mark.asyncio
    async def test_tears_down_then_deletes_the_topic(self) -> None:
        query, context = _query(), _context()
        user = MagicMock(id=100)
        # What the confirm caption described (recorded by _handle_agent_del).
        context.user_data = {"_agentdel_target": (42, "docker:assistant/notes")}
        with (
            patch("ccbot.handlers.agent_delete.session_manager") as sm,
            patch(
                "ccbot.handlers.agent_delete.purge_deleted_topic", AsyncMock()
            ) as purge,
        ):
            sm.get_window_for_thread.return_value = "docker:assistant/notes"
            sm.resolve_chat_id.return_value = -100123
            await _handle_agent_delok(
                query, f"{CB_AGENT_DELOK}42", _update(), context, user
            )
        # Teardown first (kills the agent, unbinds, clears state), then the topic.
        purge.assert_awaited_once_with(context.bot, 100, 42, "docker:assistant/notes")
        assert context.bot.delete_forum_topic.await_args.kwargs == {
            "chat_id": -100123,
            "message_thread_id": 42,
        }

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_rebound_topic_is_not_deleted(self) -> None:
        """The ✅ payload carries only the topic; between the two taps the
        topic can rebind (tmux-server restart re-maps @N). Destroying whatever
        is bound NOW instead of what the caption named is the surprise."""
        query, context = _query(), _context()
        context.user_data = {"_agentdel_target": (42, "@12")}  # what was shown
        with (
            patch("ccbot.handlers.agent_delete.session_manager") as sm,
            patch(
                "ccbot.handlers.agent_delete.purge_deleted_topic", AsyncMock()
            ) as purge,
        ):
            sm.get_window_for_thread.return_value = "@99"  # rebound since
            await _handle_agent_delok(
                query, f"{CB_AGENT_DELOK}42", _update(), context, MagicMock(id=100)
            )
        purge.assert_not_awaited()
        context.bot.delete_forum_topic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unbound_thread_is_a_no_op(self) -> None:
        query, context = _query(), _context()
        with (
            patch("ccbot.handlers.agent_delete.session_manager") as sm,
            patch(
                "ccbot.handlers.agent_delete.purge_deleted_topic", AsyncMock()
            ) as purge,
        ):
            sm.get_window_for_thread.return_value = None
            await _handle_agent_delok(
                query, f"{CB_AGENT_DELOK}42", _update(), context, MagicMock(id=100)
            )
        purge.assert_not_awaited()
        context.bot.delete_forum_topic.assert_not_awaited()


class TestCancel:
    @pytest.mark.asyncio
    async def test_restores_the_panel_on_the_tab_the_button_lives_on(self) -> None:
        query = _query()
        with (
            patch("ccbot.handlers.agent_delete.session_manager") as sm,
            patch(
                "ccbot.handlers.commands._build_commands_keyboard",
                return_value="KEYBOARD",
            ) as build,
        ):
            sm.get_display_name.return_value = "proj"
            await _handle_agent_delno(
                query, f"{CB_AGENT_DELNO}@12", _update(), _context(), MagicMock(id=1)
            )
        assert build.call_args.kwargs["tab"] == "ses"
        assert (
            query.edit_message_caption.await_args.kwargs["reply_markup"] == "KEYBOARD"
        )


class TestMainTopicIsProtected:
    """An old panel scrolled back to still carries 🗑, so both taps re-check
    that the topic's agent is an extra — a main topic is never deletable here."""

    @pytest.mark.asyncio
    async def test_confirm_is_refused_for_a_main_topic(self) -> None:
        query, context = _query(), _context()
        context.user_data = {}
        with patch("ccbot.handlers.agent_delete.session_manager") as sm:
            sm.get_window_for_thread.return_value = "@12"
            sm.can_delete_agent.return_value = False
            await _handle_agent_del(
                query, f"{CB_AGENT_DEL}@12", _update(42), context, MagicMock(id=1)
            )
        query.edit_message_caption.assert_not_awaited()
        assert query.answer.await_args.kwargs.get("show_alert") is True
        assert "_agentdel_target" not in context.user_data

    @pytest.mark.asyncio
    async def test_confirmed_tap_is_refused_for_a_main_topic(self) -> None:
        query, context = _query(), _context()
        context.user_data = {"_agentdel_target": (42, "@12")}
        with (
            patch("ccbot.handlers.agent_delete.session_manager") as sm,
            patch(
                "ccbot.handlers.agent_delete.purge_deleted_topic", AsyncMock()
            ) as purge,
        ):
            sm.get_window_for_thread.return_value = "@12"
            sm.can_delete_agent.return_value = False
            await _handle_agent_delok(
                query, f"{CB_AGENT_DELOK}42", _update(), context, MagicMock(id=100)
            )
        purge.assert_not_awaited()
        context.bot.delete_forum_topic.assert_not_awaited()
