"""Tests for the mid-provision hold — a topic that exists before its agent does.

The bug this guards: a docker sibling's topic appears in Telegram seconds
before the container's Claude is up and bound, so a message typed right away
used to reach the unbound-topic fallback and draw the HOST directory browser
in a container-bound topic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.config import DockerAgentConfig
from ccbot.handlers import provisioning
from ccbot.handlers.provisioning import claim_topic, wait_for_topic
from ccbot.handlers.siblings import provision_sibling_agent
from ccbot.session import DockerTarget

AGENT = DockerAgentConfig(
    name="assistant",
    container="assistant-ctn",
    workspace_host_path=Path("/tmp/ws"),
    claude_home_host_path=Path("/tmp/ch"),
    ipc_dir=Path("/tmp/ipc"),
    session_map_path=Path("/tmp/sm.json"),
)


class TestClaim:
    @pytest.mark.asyncio
    async def test_unclaimed_topic_does_not_wait(self) -> None:
        assert await wait_for_topic(1, 42) is False

    @pytest.mark.asyncio
    async def test_no_thread_does_not_wait(self) -> None:
        assert await wait_for_topic(1, None) is False

    @pytest.mark.asyncio
    async def test_waiter_is_released_when_the_claim_ends(self) -> None:
        released = asyncio.Event()

        async def provision() -> None:
            with claim_topic(1, 42):
                await asyncio.sleep(0.05)
                released.set()

        task = asyncio.create_task(provision())
        await asyncio.sleep(0)  # let the claim register
        waited = await wait_for_topic(1, 42)
        await task
        assert waited is True
        assert released.is_set()  # we came back AFTER provisioning finished

    @pytest.mark.asyncio
    async def test_claim_is_released_on_failure(self) -> None:
        with pytest.raises(RuntimeError):
            with claim_topic(1, 42):
                raise RuntimeError("rolled back")
        assert await wait_for_topic(1, 42) is False

    @pytest.mark.asyncio
    async def test_a_stuck_provision_does_not_hold_the_message_forever(self) -> None:
        with (
            claim_topic(1, 42),
            patch.object(provisioning, "PROVISION_WAIT_TIMEOUT_SEC", 0.01),
        ):
            assert await wait_for_topic(1, 42) is True

    @pytest.mark.asyncio
    async def test_claims_are_per_topic(self) -> None:
        with claim_topic(1, 42):
            assert await wait_for_topic(1, 43) is False
            assert await wait_for_topic(2, 42) is False


class TestDockerSiblingHoldsItsFirstMessage:
    @pytest.mark.asyncio
    async def test_binding_is_visible_by_the_time_the_waiter_returns(self) -> None:
        """The whole point: a message sent while the container agent is
        starting waits, then finds a binding — no host directory browser."""
        bindings: dict[int, str] = {}
        sm = MagicMock()
        sm._is_docker_binding.return_value = True
        sm.resolve_docker_target.return_value = DockerTarget(
            agent=AGENT, tmux_session="claude", sub=None
        )
        sm.taken_sub_slugs = AsyncMock(return_value=set())
        sm.get_window_state.return_value = MagicMock(cwd="/workspace")
        sm.bind_thread.side_effect = lambda uid, th, binding, **kw: (
            bindings.__setitem__(th, binding)
        )

        async def slow_start(*_a, **_kw) -> bool:
            await asyncio.sleep(0.05)  # the container taking its time
            return True

        sm.start_docker_agent = AsyncMock(side_effect=slow_start)
        bot = MagicMock()
        bot.create_forum_topic = AsyncMock(
            return_value=MagicMock(message_thread_id=777)
        )

        with (
            patch("ccbot.handlers.siblings.session_manager", sm),
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
            patch(
                "ccbot.handlers.siblings._hook_names_the_sibling",
                AsyncMock(return_value=True),
            ),
        ):
            task = asyncio.create_task(
                provision_sibling_agent(bot, 100, -100123, "docker:assistant", "notes")
            )
            await asyncio.sleep(0.01)  # the user types the moment the topic shows up
            assert bindings == {}  # nothing bound yet — the old bug's window
            waited = await wait_for_topic(100, 777)
            ok, _ = await task

        assert ok is True
        assert waited is True
        assert bindings[777] == "docker:assistant/notes"
