"""A message to a docker topic whose container is down keeps the binding.

A container restart (OOM, healthcheck) is transient; unbinding stranded
sub-agent topics, since /bind only reached configured agents.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot import bot
from ccbot.i18n import tr


@pytest.mark.asyncio
async def test_container_down_replies_without_unbinding():
    update = MagicMock()
    update.message = MagicMock()
    reply = AsyncMock()
    driver = MagicMock()
    driver.is_container_alive = AsyncMock(return_value=False)
    with (
        patch.object(bot, "session_manager") as sm,
        patch.object(bot, "safe_reply", new=reply),
        patch("ccbot.docker_driver.docker_driver", driver),
    ):
        sm._is_docker_binding.return_value = True
        sm.get_display_name.return_value = "assistant-notes"
        await bot._forward_text_to_agent(
            update, MagicMock(), 1, 42, "docker:assistant/notes", "hi"
        )
    sm.unbind_thread.assert_not_called()
    assert reply.call_args.args[1] == tr(
        "bot.docker_agent_down", name="assistant-notes"
    )
