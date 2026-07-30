"""Tests for ➕ sibling agents — the name step, provisioning, teardown.

A sibling shares the parent's files (same container / same directory) and only
gets its own session and topic. What has to hold: the docker branch targets a
NEW in-container tmux session (never the parent's), a failed start leaves no
orphan topic behind, and only a sub-agent is killed with its topic.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.config import DockerAgentConfig
from ccbot.handlers.siblings import (
    PENDING_KEY,
    SIB_NAMING_TTL_SEC,
    cancel_pending_naming,
    consume_sibling_name,
    provision_sibling_agent,
    teardown_sibling,
)
from ccbot.session import DockerTarget

AGENT = DockerAgentConfig(
    name="assistant",
    container="assistant-ctn",
    workspace_host_path=Path("/tmp/ws"),
    claude_home_host_path=Path("/tmp/ch"),
    ipc_dir=Path("/tmp/ipc"),
    session_map_path=Path("/tmp/sm.json"),
)


def _bot(thread_id: int = 777) -> MagicMock:
    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(
        return_value=MagicMock(message_thread_id=thread_id)
    )
    bot.delete_forum_topic = AsyncMock()
    return bot


def _hook_ok():
    """Patch the container-hook probe: it names the sibling correctly."""
    return patch(
        "ccbot.handlers.siblings._hook_names_the_sibling", AsyncMock(return_value=True)
    )


def _docker_manager(*, start_ok: bool = True) -> MagicMock:
    sm = MagicMock()
    sm._is_docker_binding.return_value = True
    sm.resolve_docker_target.return_value = DockerTarget(
        agent=AGENT, tmux_session="claude", sub=None
    )
    sm.taken_sub_slugs = AsyncMock(return_value=set())
    sm.get_window_state.return_value = MagicMock(cwd="/workspace")
    sm.start_docker_agent = AsyncMock(return_value=start_ok)
    sm.kill_agent = AsyncMock(return_value=True)
    return sm


class TestProvisionDockerSibling:
    @pytest.mark.asyncio
    async def test_topic_name_and_binding_follow_the_parent(self) -> None:
        sm = _docker_manager()
        bot = _bot()
        with (
            patch("ccbot.handlers.siblings.session_manager", sm),
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
            _hook_ok(),
        ):
            ok, _ = await provision_sibling_agent(
                bot, 100, -100123, "docker:assistant", "фитнес-приложение"
            )
        assert ok is True
        # Topic named <agent>-<slug>, slug transliterated from the user's words.
        assert (
            bot.create_forum_topic.await_args.kwargs["name"]
            == "➕ assistant-fitnes-prilozhenie"
        )
        binding = sm.bind_thread.call_args.args[2]
        assert binding == "docker:assistant/fitnes-prilozhenie"
        # Started in the parent's cwd, with its own pinned session id.
        kwargs = sm.start_docker_agent.await_args.kwargs
        assert kwargs["cwd"] == "/workspace"
        assert kwargs["new_session_id"]

    @pytest.mark.asyncio
    async def test_slug_collision_is_deduped(self) -> None:
        sm = _docker_manager()
        sm.taken_sub_slugs = AsyncMock(return_value={"notes"})
        bot = _bot()
        with (
            patch("ccbot.handlers.siblings.session_manager", sm),
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
            _hook_ok(),
        ):
            await provision_sibling_agent(
                bot, 100, -100123, "docker:assistant", "notes"
            )
        assert sm.bind_thread.call_args.args[2] == "docker:assistant/notes-2"

    @pytest.mark.asyncio
    async def test_failed_start_rolls_the_topic_back(self) -> None:
        sm = _docker_manager(start_ok=False)
        bot = _bot(thread_id=555)
        with (
            patch("ccbot.handlers.siblings.session_manager", sm),
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
        ):
            ok, _ = await provision_sibling_agent(
                bot, 100, -100123, "docker:assistant", "notes"
            )
        assert ok is False
        bot.delete_forum_topic.assert_awaited_once()
        assert bot.delete_forum_topic.await_args.kwargs["message_thread_id"] == 555
        sm.bind_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_hook_that_hardcodes_the_parent_is_refused(self) -> None:
        """Such a hook reports the sibling's NEXT session (its first /clear)
        under the parent's key, which would re-point the parent's topic at its
        child. Undo instead of leaving that trap armed."""
        sm = _docker_manager()
        bot = _bot(thread_id=555)
        with (
            patch("ccbot.handlers.siblings.session_manager", sm),
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
            patch(
                "ccbot.handlers.siblings._hook_names_the_sibling",
                AsyncMock(return_value=False),
            ),
        ):
            ok, _ = await provision_sibling_agent(
                bot, 100, -100123, "docker:assistant", "notes"
            )
        assert ok is False
        sm.kill_agent.assert_awaited_once_with("docker:assistant/notes")
        sm.forget_binding.assert_called_once_with("docker:assistant/notes")
        bot.delete_forum_topic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_silent_hook_still_provisions(self) -> None:
        """No verdict within the timeout: the sibling is tracked by its pinned
        session id, so it works — refusing here would break every container
        whose hook is merely slow."""
        sm = _docker_manager()
        with (
            patch("ccbot.handlers.siblings.session_manager", sm),
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
            patch(
                "ccbot.handlers.siblings._hook_names_the_sibling",
                AsyncMock(return_value=None),
            ),
        ):
            ok, _ = await provision_sibling_agent(
                _bot(), 100, -100123, "docker:assistant", "notes"
            )
        assert ok is True

    @pytest.mark.asyncio
    async def test_unknown_parent_never_creates_a_topic(self) -> None:
        sm = _docker_manager()
        sm.resolve_docker_target.return_value = None
        bot = _bot()
        with patch("ccbot.handlers.siblings.session_manager", sm):
            ok, _ = await provision_sibling_agent(
                bot, 100, -100123, "docker:assistant", "notes"
            )
        assert ok is False
        bot.create_forum_topic.assert_not_awaited()


class TestProvisionTmuxSibling:
    @pytest.mark.asyncio
    async def test_new_window_on_the_same_directory(self, tmp_path) -> None:
        sm = MagicMock()
        sm._is_docker_binding.return_value = False
        sm.get_window_state.return_value = MagicMock(
            cwd=str(tmp_path), runtime="claude"
        )
        sm.get_display_name.return_value = "proj"
        sm.wait_for_session_map_entry = AsyncMock(return_value=True)
        tm = MagicMock()
        tm.create_window = AsyncMock(return_value=(True, "", "proj-notes", "@7"))
        bot = _bot()
        with (
            patch("ccbot.handlers.siblings.session_manager", sm),
            patch("ccbot.handlers.siblings.tmux_manager", tm),
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
        ):
            ok, _ = await provision_sibling_agent(bot, 100, -100123, "@1", "notes")
        assert ok is True
        assert tm.create_window.await_args.args[0] == str(tmp_path)
        assert sm.bind_thread.call_args.args[2] == "@7"

    @pytest.mark.asyncio
    async def test_missing_cwd_is_refused(self) -> None:
        sm = MagicMock()
        sm._is_docker_binding.return_value = False
        sm.get_window_state.return_value = MagicMock(cwd="", runtime="claude")
        bot = _bot()
        with patch("ccbot.handlers.siblings.session_manager", sm):
            ok, _ = await provision_sibling_agent(bot, 100, -100123, "@1", "notes")
        assert ok is False
        bot.create_forum_topic.assert_not_awaited()


class TestNamingStep:
    def _context(self, thread_id: int = 42) -> MagicMock:
        context = MagicMock()
        context.user_data = {
            PENDING_KEY: {
                thread_id: (
                    "docker:assistant",
                    -100123,
                    time.monotonic() + SIB_NAMING_TTL_SEC,
                )
            }
        }
        context.bot = AsyncMock()
        return context

    def _update(self, thread_id: int = 42, text: str = "фитнес") -> MagicMock:
        update = MagicMock()
        update.effective_user.id = 1
        update.message.text = text
        update.message.message_thread_id = thread_id
        return update

    @pytest.mark.asyncio
    async def test_name_is_consumed_and_provisions(self) -> None:
        context = self._context()
        with (
            patch(
                "ccbot.handlers.siblings.provision_sibling_agent",
                AsyncMock(return_value=(True, "ok")),
            ) as prov,
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
        ):
            assert await consume_sibling_name(self._update(), context) is True
        assert prov.await_args.args[4] == "фитнес"
        assert context.user_data[PENDING_KEY] == {}

    @pytest.mark.asyncio
    async def test_expired_state_falls_through_to_the_agent(self) -> None:
        context = self._context()
        context.user_data[PENDING_KEY][42] = ("docker:assistant", -100123, 0.0)
        assert await consume_sibling_name(self._update(), context) is False
        assert context.user_data[PENDING_KEY] == {}  # and the trap is gone

    @pytest.mark.asyncio
    async def test_other_topic_is_not_consumed(self) -> None:
        context = self._context(thread_id=42)
        assert await consume_sibling_name(self._update(thread_id=99), context) is False
        # State belongs to the other topic — left intact for it.
        assert 42 in context.user_data[PENDING_KEY]

    @pytest.mark.asyncio
    async def test_two_topics_arm_independently(self) -> None:
        """A ➕ in topic B must not disarm the one waiting in topic A: the
        shared-slot version silently forwarded A's name to its agent."""
        context = self._context(thread_id=42)
        context.user_data[PENDING_KEY][99] = (
            "@7",
            -100123,
            time.monotonic() + SIB_NAMING_TTL_SEC,
        )
        with (
            patch(
                "ccbot.handlers.siblings.provision_sibling_agent",
                AsyncMock(return_value=(True, "ok")),
            ) as prov,
            patch("ccbot.handlers.siblings.safe_send", AsyncMock()),
        ):
            assert (
                await consume_sibling_name(self._update(thread_id=99), context) is True
            )
        assert prov.await_args.args[3] == "@7"  # topic B's own parent
        assert 42 in context.user_data[PENDING_KEY]  # topic A still armed

    @pytest.mark.asyncio
    async def test_voice_or_photo_disarms_the_step(self) -> None:
        context = self._context(thread_id=42)
        cancel_pending_naming(context.user_data, 42)
        assert await consume_sibling_name(self._update(), context) is False

    @pytest.mark.asyncio
    async def test_no_state_is_not_consumed(self) -> None:
        context = MagicMock()
        context.user_data = {}
        assert await consume_sibling_name(self._update(), context) is False


class TestTeardown:
    @pytest.mark.asyncio
    async def test_sub_agent_is_killed_and_forgotten(self) -> None:
        sm = MagicMock()
        sm.is_docker_sub_agent.return_value = True
        sm.kill_agent = AsyncMock(return_value=True)
        with patch("ccbot.handlers.siblings.session_manager", sm):
            assert await teardown_sibling(100, 42, "docker:assistant/notes") is True
        sm.kill_agent.assert_awaited_once_with("docker:assistant/notes")
        sm.forget_binding.assert_called_once_with("docker:assistant/notes")

    @pytest.mark.asyncio
    async def test_main_docker_binding_is_left_alone(self) -> None:
        """The container's own agent outlives its topic — its lifecycle is the
        container's, and /restart must still be able to revive it."""
        sm = MagicMock()
        sm.is_docker_sub_agent.return_value = False
        sm.kill_agent = AsyncMock(return_value=True)
        with patch("ccbot.handlers.siblings.session_manager", sm):
            assert await teardown_sibling(100, 42, "docker:assistant") is False
        sm.kill_agent.assert_not_awaited()
        sm.forget_binding.assert_not_called()
