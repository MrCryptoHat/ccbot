"""Tests for command handlers — /bind canonical-name binding, /start group hints.

get_docker_agent matches case-insensitively (phone keyboards capitalize
the first word), but every session_map key and route uses the canonical
lowercase binding value — binding the user's spelling silently kills
inbound delivery for the topic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.config import DockerAgentConfig
from ccbot.handlers.commands import bind_command, start_command
from ccbot.i18n import tr


def _agent(name: str = "assistant") -> DockerAgentConfig:
    return DockerAgentConfig(
        name=name,
        container=f"{name}-ctn",
        workspace_host_path=Path(f"/tmp/{name}/workspace"),
        claude_home_host_path=Path(f"/tmp/{name}/claude-home"),
        ipc_dir=Path(f"/tmp/{name}/ipc"),
        session_map_path=Path(f"/tmp/{name}/session-map.json"),
    )


class TestBindCanonicalName:
    @pytest.mark.asyncio
    async def test_bind_uses_canonical_agent_name(self):
        """/bind Assistant must bind docker:assistant, not docker:Assistant."""
        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        update.effective_chat.id = -100123
        context = MagicMock()
        context.args = ["Assistant"]

        with (
            patch("ccbot.handlers.commands.is_user_allowed", return_value=True),
            patch("ccbot.handlers.commands.get_thread_id", return_value=42),
            patch("ccbot.handlers.commands.config") as mock_cfg,
            patch("ccbot.handlers.commands.session_manager") as mock_sm,
            patch("ccbot.handlers.commands.safe_reply", new=AsyncMock()),
        ):
            mock_cfg.docker_agents_enabled = True
            mock_cfg.get_docker_agent.return_value = _agent("assistant")

            await bind_command(update, context)

            mock_sm.bind_thread.assert_called_once_with(
                1, 42, "docker:assistant", window_name="assistant"
            )

    @pytest.mark.asyncio
    async def test_bind_unknown_agent_does_not_bind(self):
        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        context = MagicMock()
        context.args = ["nosuch"]

        with (
            patch("ccbot.handlers.commands.is_user_allowed", return_value=True),
            patch("ccbot.handlers.commands.get_thread_id", return_value=42),
            patch("ccbot.handlers.commands.config") as mock_cfg,
            patch("ccbot.handlers.commands.session_manager") as mock_sm,
            patch("ccbot.handlers.commands.safe_reply", new=AsyncMock()),
        ):
            mock_cfg.docker_agents_enabled = True
            mock_cfg.get_docker_agent.return_value = None
            mock_cfg.docker_agents = []

            await bind_command(update, context)

            mock_sm.bind_thread.assert_not_called()


class TestStartCommandGroupHints:
    """/start in a group detects the two setup traps (no admin / no Topics).

    Commands are the only updates Telegram delivers to a non-admin bot
    (privacy mode), so /start must explain the broken setup instead of
    greeting into silence.
    """

    def _update(self, chat_type: str = "supergroup", is_forum: bool = False):
        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        chat = update.effective_chat
        chat.type = chat_type
        chat.is_forum = is_forum
        chat.get_member = AsyncMock(return_value=MagicMock(status="administrator"))
        context = MagicMock()
        context.bot.id = 999
        return update, context

    async def _run(self, update, context):
        reply = AsyncMock()
        with (
            patch("ccbot.handlers.commands.is_user_allowed", return_value=True),
            patch("ccbot.handlers.commands.get_thread_id", return_value=None),
            patch("ccbot.handlers.commands.session_manager"),
            patch("ccbot.handlers.commands.safe_reply", new=reply),
        ):
            await start_command(update, context)
        return reply.call_args.args[1]

    @pytest.mark.asyncio
    async def test_not_admin_asks_for_admin_rights(self):
        update, context = self._update(is_forum=True)
        update.effective_chat.get_member = AsyncMock(
            return_value=MagicMock(status="member")
        )
        assert await self._run(update, context) == tr("bot.make_me_admin")

    @pytest.mark.asyncio
    async def test_admin_without_topics_asks_to_enable_topics(self):
        update, context = self._update(is_forum=False)
        assert await self._run(update, context) == tr("bot.enable_topics_hint")

    @pytest.mark.asyncio
    async def test_admin_with_topics_gets_welcome(self):
        update, context = self._update(is_forum=True)
        assert await self._run(update, context) == tr("bot.start_welcome")

    @pytest.mark.asyncio
    async def test_get_member_failure_does_not_nag(self):
        update, context = self._update(is_forum=True)
        update.effective_chat.get_member = AsyncMock(side_effect=RuntimeError("api"))
        assert await self._run(update, context) == tr("bot.start_welcome")

    @pytest.mark.asyncio
    async def test_private_chat_gets_dm_walkthrough(self):
        update, context = self._update(chat_type="private")
        assert await self._run(update, context) == tr("bot.private_start")
