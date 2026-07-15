"""Tests for SessionManager pure dict operations."""

import json

import pytest

from ccbot.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestMenuShownTopics:
    """Per-topic menu-keyboard exactly-once flag (see menu_shown_topics)."""

    def test_default_not_shown(self, mgr: SessionManager) -> None:
        assert mgr.is_menu_shown(100, 42) is False

    def test_none_thread_not_shown(self, mgr: SessionManager) -> None:
        assert mgr.is_menu_shown(100, None) is False

    def test_mark_and_check(self, mgr: SessionManager) -> None:
        mgr.mark_menu_shown(100, 42)
        assert mgr.is_menu_shown(100, 42) is True
        # A different topic of the same user stays unmarked.
        assert mgr.is_menu_shown(100, 43) is False

    def test_mark_is_idempotent(self, mgr: SessionManager) -> None:
        mgr.mark_menu_shown(100, 42)
        mgr.mark_menu_shown(100, 42)
        assert mgr.menu_shown_topics == {"100:42"}

    def test_clear(self, mgr: SessionManager) -> None:
        mgr.mark_menu_shown(100, 42)
        mgr.clear_menu_shown(100, 42)
        assert mgr.is_menu_shown(100, 42) is False


class TestThreadDirectoryMemory:
    """Learned topic→directory memory (auto-rebind by permanent thread_id)."""

    def test_record_and_get(self, mgr: SessionManager) -> None:
        mgr.record_thread_directory(100, 42, "/home/user/projects/myapp")
        assert mgr.get_remembered_directory(100, 42) == "/home/user/projects/myapp"

    def test_get_unknown_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.get_remembered_directory(100, 42) is None

    def test_record_overwrites(self, mgr: SessionManager) -> None:
        mgr.record_thread_directory(100, 42, "/a")
        mgr.record_thread_directory(100, 42, "/b")
        assert mgr.get_remembered_directory(100, 42) == "/b"

    def test_empty_directory_ignored(self, mgr: SessionManager) -> None:
        mgr.record_thread_directory(100, 42, "")
        assert mgr.get_remembered_directory(100, 42) is None

    def test_users_and_threads_independent(self, mgr: SessionManager) -> None:
        mgr.record_thread_directory(100, 1, "/u100t1")
        mgr.record_thread_directory(100, 2, "/u100t2")
        mgr.record_thread_directory(200, 1, "/u200t1")
        assert mgr.get_remembered_directory(100, 1) == "/u100t1"
        assert mgr.get_remembered_directory(100, 2) == "/u100t2"
        assert mgr.get_remembered_directory(200, 1) == "/u200t1"

    def test_idempotent_record_skips_save(self, mgr: SessionManager) -> None:
        # Re-recording the same dir must not trigger a redundant state write.
        calls = {"n": 0}
        mgr._save_state = lambda: calls.__setitem__("n", calls["n"] + 1)  # type: ignore[method-assign]
        mgr.record_thread_directory(100, 42, "/same")
        mgr.record_thread_directory(100, 42, "/same")
        assert calls["n"] == 1

    def test_load_coerces_int_keys(self, monkeypatch, tmp_path) -> None:
        # JSON object keys are strings; load must coerce user_id/thread_id to int
        # so lookups (which pass ints) hit. The riskiest part of the new field.
        from ccbot import session as session_mod

        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {"thread_directory_memory": {"100": {"42": "/home/user/agents/demo"}}}
            )
        )
        monkeypatch.setattr(session_mod.config, "state_file", state_file)
        mgr = SessionManager()
        assert mgr.get_remembered_directory(100, 42) == "/home/user/agents/demo"


class TestWorktreeMeta:
    """Worktree-agent metadata keyed by permanent thread_id."""

    def _meta(self, **kw):
        from ccbot.worktrees import WorktreeMeta

        base = dict(
            repo="/home/user/projects/ccbot",
            repo_name="ccbot",
            branch="wt/hero",
            base_branch="main",
            path="/home/user/.ccbot/worktrees/ccbot/hero",
            task_title="редизайн шапки",
        )
        base.update(kw)
        return WorktreeMeta(**base)  # type: ignore[arg-type]

    def test_set_and_get(self, mgr: SessionManager) -> None:
        mgr.set_worktree_meta(100, 42, self._meta())
        got = mgr.get_worktree_meta(100, 42)
        assert got is not None and got.branch == "wt/hero" and got.status == "active"

    def test_get_unknown_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.get_worktree_meta(100, 42) is None

    def test_clear_removes(self, mgr: SessionManager) -> None:
        mgr.set_worktree_meta(100, 42, self._meta())
        mgr.clear_worktree_meta(100, 42)
        assert mgr.get_worktree_meta(100, 42) is None

    def test_mark_orphaned_flips_once(self, mgr: SessionManager) -> None:
        mgr.set_worktree_meta(100, 42, self._meta())
        assert mgr.mark_worktree_orphaned(100, 42) is True
        assert mgr.get_worktree_meta(100, 42).status == "orphaned"  # type: ignore[union-attr]
        assert mgr.mark_worktree_orphaned(100, 42) is False  # already orphaned

    def test_mark_orphaned_unknown_returns_false(self, mgr: SessionManager) -> None:
        assert mgr.mark_worktree_orphaned(100, 999) is False

    def test_iter(self, mgr: SessionManager) -> None:
        mgr.set_worktree_meta(100, 1, self._meta())
        mgr.set_worktree_meta(200, 2, self._meta(repo_name="myapp"))
        rows = sorted((u, t, m.repo_name) for u, t, m in mgr.iter_worktree_meta())
        assert rows == [(100, 1, "ccbot"), (200, 2, "myapp")]

    def test_is_worktree_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@5")
        assert mgr.is_worktree_window("@5") is False  # bound, but no meta
        mgr.set_worktree_meta(100, 42, self._meta())
        assert mgr.is_worktree_window("@5") is True
        assert mgr.is_worktree_window("@99") is False  # unknown window

    def test_reconcile_drops_only_unbound_and_gone(self, mgr, tmp_path) -> None:
        live = tmp_path / "live"
        live.mkdir()
        # (a) unbound + path gone → dropped
        mgr.set_worktree_meta(100, 1, self._meta(path="/nope/gone"))
        # (b) bound + path gone → kept (a live agent whose window just died)
        mgr.set_worktree_meta(100, 2, self._meta(path="/nope/gone2"))
        mgr.bind_thread(100, 2, "@7")
        # (c) unbound + path exists → kept (worktree still on disk)
        mgr.set_worktree_meta(100, 3, self._meta(path=str(live)))

        assert mgr.reconcile_worktree_meta() == 1
        assert mgr.get_worktree_meta(100, 1) is None
        assert mgr.get_worktree_meta(100, 2) is not None
        assert mgr.get_worktree_meta(100, 3) is not None

    def test_load_coerces_int_keys(self, monkeypatch, tmp_path) -> None:
        # JSON object keys are strings; load must coerce user_id/thread_id to int
        # (lookups pass ints) and rebuild WorktreeMeta from its dict form.
        from ccbot import session as session_mod

        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "worktree_meta": {
                        "100": {
                            "42": {
                                "repo": "/home/user/projects/ccbot",
                                "repo_name": "ccbot",
                                "branch": "wt/hero",
                                "base_branch": "main",
                                "path": "/home/user/.ccbot/worktrees/ccbot/hero",
                                "task_title": "редизайн шапки",
                                "status": "orphaned",
                            }
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(session_mod.config, "state_file", state_file)
        mgr = SessionManager()
        got = mgr.get_worktree_meta(100, 42)
        assert got is not None
        assert got.task_title == "редизайн шапки" and got.status == "orphaned"


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False


class TestDockerBindingHelpers:
    """Binding-type helpers that let callers branch between tmux and docker
    transports without parsing binding strings ad-hoc.
    """

    def test_is_docker_binding_positive(self) -> None:
        assert SessionManager._is_docker_binding("docker:assistant") is True
        assert SessionManager._is_docker_binding("docker:a") is True

    def test_is_docker_binding_negative(self) -> None:
        assert SessionManager._is_docker_binding("@12") is False
        assert SessionManager._is_docker_binding("docker:") is False
        assert SessionManager._is_docker_binding("") is False
        assert SessionManager._is_docker_binding("myproject") is False

    def test_resolve_binding_tmux(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@12")
        assert mgr.resolve_binding(100, 1) == ("tmux", "@12")

    def test_resolve_binding_docker(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "docker:assistant")
        assert mgr.resolve_binding(100, 1) == ("docker", "assistant")

    def test_resolve_binding_unbound_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_binding(100, 42) is None

    def test_resolve_binding_none_thread_returns_none(
        self, mgr: SessionManager
    ) -> None:
        assert mgr.resolve_binding(100, None) is None


class TestResolveStaleIdsPreservesDocker:
    """resolve_stale_ids rebuilds mappings from live tmux windows. Docker
    bindings have no tmux window to match and must survive verbatim —
    otherwise a bot restart (or tmux server restart) would silently drop
    the topic-to-container link.
    """

    async def test_docker_thread_binding_survives_empty_tmux(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import tmux_manager as tm_mod

        async def _no_windows():
            return []

        monkeypatch.setattr(tm_mod.tmux_manager, "list_windows", _no_windows)
        mgr.bind_thread(100, 1, "docker:assistant")
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 1) == "docker:assistant"

    async def test_docker_binding_preserved_alongside_stale_tmux(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """Docker binding stays even when a sibling tmux binding gets dropped."""
        from ccbot import tmux_manager as tm_mod

        async def _no_windows():
            return []

        monkeypatch.setattr(tm_mod.tmux_manager, "list_windows", _no_windows)
        mgr.bind_thread(100, 1, "docker:assistant")
        mgr.bind_thread(100, 2, "@99")  # stale — no matching live window
        await mgr.resolve_stale_ids()
        assert mgr.get_window_for_thread(100, 1) == "docker:assistant"
        assert mgr.get_window_for_thread(100, 2) is None

    async def test_docker_window_state_preserved(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import tmux_manager as tm_mod

        async def _no_windows():
            return []

        monkeypatch.setattr(tm_mod.tmux_manager, "list_windows", _no_windows)
        ws = mgr.get_window_state("docker:assistant")
        ws.session_id = "sid-1"
        ws.cwd = "/workspace"
        await mgr.resolve_stale_ids()
        assert "docker:assistant" in mgr.window_states
        assert mgr.window_states["docker:assistant"].session_id == "sid-1"


class TestSendToWindowRouting:
    """send_to_window is the one place where the transport branch happens.
    Legacy tmux bindings must keep using tmux_manager; docker bindings
    must route through docker_driver. Docker routing is gated by the
    ``docker_agents_enabled`` flag so a stale docker:* value in
    state.json with the flag off can't accidentally drive a container.
    """

    async def _setup_docker_agent(self, monkeypatch):
        from pathlib import Path

        from ccbot import session as session_mod
        from ccbot.config import DockerAgentConfig

        agent = DockerAgentConfig(
            name="assistant",
            container="assistant-ctn",
            workspace_host_path=Path("/tmp/ws"),
            claude_home_host_path=Path("/tmp/ch"),
            ipc_dir=Path("/tmp/ipc"),
            session_map_path=Path("/tmp/sm.json"),
        )
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])
        return agent

    async def test_docker_routes_to_driver_when_flag_on(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        await self._setup_docker_agent(monkeypatch)

        calls: list[tuple[str, str]] = []

        async def fake_send(container, text, **kw):
            calls.append((container, text))
            return True

        async def fake_alive(_container):
            return True

        monkeypatch.setattr(session_mod.docker_driver, "send_keys", fake_send)
        monkeypatch.setattr(session_mod.docker_driver, "is_container_alive", fake_alive)

        ok, _msg = await mgr.send_to_window("docker:assistant", "hi")
        assert ok is True
        assert calls == [("assistant-ctn", "hi")]

    async def test_docker_disabled_flag_returns_error(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """Flag off + docker binding must fail explicitly (not silently
        fall through to tmux and report a confusing 'Window not found')."""
        from ccbot import session as session_mod

        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", False)

        ok, msg = await mgr.send_to_window("docker:assistant", "hi")
        assert ok is False
        assert "disabled" in msg.lower()

    async def test_docker_unknown_agent_returns_error(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [])

        ok, msg = await mgr.send_to_window("docker:ghost", "hi")
        assert ok is False
        assert "ghost" in msg

    async def test_docker_dead_container_returns_error(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        await self._setup_docker_agent(monkeypatch)

        async def fake_alive(_c):
            return False

        monkeypatch.setattr(session_mod.docker_driver, "is_container_alive", fake_alive)
        ok, msg = await mgr.send_to_window("docker:assistant", "hi")
        assert ok is False
        assert "not running" in msg.lower()

    async def test_tmux_path_unchanged(self, mgr: SessionManager, monkeypatch) -> None:
        """Legacy @<id> bindings must NOT touch docker_driver."""
        from ccbot import session as session_mod
        from ccbot.tmux_manager import TmuxWindow

        async def fake_find(wid):
            return TmuxWindow(window_id=wid, window_name="x", cwd="/tmp")

        async def fake_tmux_send(_wid, _text):
            return True

        async def forbidden(*a, **kw):
            raise AssertionError("docker_driver must not be called for tmux bindings")

        monkeypatch.setattr(session_mod.tmux_manager, "find_window_by_id", fake_find)
        monkeypatch.setattr(session_mod.tmux_manager, "send_keys", fake_tmux_send)
        monkeypatch.setattr(session_mod.docker_driver, "send_keys", forbidden)
        monkeypatch.setattr(session_mod.docker_driver, "is_container_alive", forbidden)

        ok, _msg = await mgr.send_to_window("@12", "hi")
        assert ok is True


class TestResolveAgentBinding:
    """resolve_agent_binding maps a bare agent name → binding value for the
    /inject endpoint: docker agent → docker:<name>, live tmux window →
    @<id>, neither → None (the "agent not running" signal)."""

    async def test_docker_agent_resolves_to_docker_binding(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        monkeypatch.setattr(
            session_mod.config, "get_docker_agent", lambda name: object()
        )

        # A docker agent must NOT fall through to a tmux lookup.
        async def forbidden(_name):
            raise AssertionError("tmux lookup must not run for a docker agent")

        monkeypatch.setattr(session_mod.tmux_manager, "find_window_by_name", forbidden)
        assert await mgr.resolve_agent_binding("assistant") == "docker:assistant"

    async def test_tmux_window_resolves_to_window_id(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import session as session_mod
        from ccbot.tmux_manager import TmuxWindow

        monkeypatch.setattr(session_mod.config, "get_docker_agent", lambda name: None)

        async def fake_find(name):
            assert name == "example.com"
            return TmuxWindow(window_id="@7", window_name=name, cwd="/tmp")

        monkeypatch.setattr(session_mod.tmux_manager, "find_window_by_name", fake_find)
        assert await mgr.resolve_agent_binding("example.com") == "@7"

    async def test_unknown_name_resolves_to_none(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        monkeypatch.setattr(session_mod.config, "get_docker_agent", lambda name: None)

        async def fake_find(_name):
            return None

        monkeypatch.setattr(session_mod.tmux_manager, "find_window_by_name", fake_find)
        assert await mgr.resolve_agent_binding("ghost") is None


class TestDockerJsonlResolution:
    """resolve_session_for_window must read the docker agent's JSONL from
    the agent-specific projects root (bind-mounted claude-home), not the
    host's ~/.claude/projects — that's the whole point of keeping the
    container isolated. These tests exercise the full
    binding → projects_root → file_path resolution path.
    """

    def _agent(self, tmp_path, name="assistant"):
        from ccbot.config import DockerAgentConfig

        return DockerAgentConfig(
            name=name,
            container=f"{name}-ctn",
            workspace_host_path=tmp_path / name / "workspace",
            claude_home_host_path=tmp_path / name / "claude-home",
            ipc_dir=tmp_path / name / "ipc",
            session_map_path=tmp_path / name / "session-map.json",
        )

    def test_projects_root_for_docker(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        agent = self._agent(tmp_path)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])
        root = mgr._projects_root_for_binding("docker:assistant")
        assert root == agent.claude_home_host_path / "projects"

    def test_projects_root_for_tmux_uses_default(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from pathlib import Path

        from ccbot import session as session_mod

        monkeypatch.setattr(
            session_mod.config, "claude_projects_path", Path("/fake/default")
        )
        assert mgr._projects_root_for_binding("@12") == Path("/fake/default")

    def test_projects_root_for_unknown_docker_falls_back(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        """Docker binding pointing at an agent that isn't configured: fall
        back to the default projects path so the caller fails by missing
        the file rather than blowing up on None."""
        from pathlib import Path

        from ccbot import session as session_mod

        monkeypatch.setattr(
            session_mod.config, "claude_projects_path", Path("/fake/default")
        )
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [])
        assert mgr._projects_root_for_binding("docker:ghost") == Path("/fake/default")

    async def test_resolve_session_for_docker_binding(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """End-to-end: a docker binding with persisted sid+cwd resolves to
        the JSONL under the agent's projects root."""
        from ccbot import session as session_mod

        agent = self._agent(tmp_path)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])

        encoded = SessionManager._encode_cwd("/workspace")
        project_dir = agent.claude_home_host_path / "projects" / encoded
        project_dir.mkdir(parents=True)
        sid = "docker-session-uuid"
        jsonl = project_dir / f"{sid}.jsonl"
        jsonl.write_text(
            '{"type":"summary","summary":"hello from container"}\n'
            '{"type":"user","message":{"content":"hi"}}\n'
        )

        ws = mgr.get_window_state("docker:assistant")
        ws.session_id = sid
        ws.cwd = "/workspace"

        session = await mgr.resolve_session_for_window("docker:assistant")
        assert session is not None
        assert session.session_id == sid
        assert str(jsonl) == session.file_path

    async def test_resolve_missing_jsonl_keeps_session_id(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """A freshly-launched session (after «Новая»/restart) has no transcript
        file until its first turn, yet the hook already reported its id. resolve
        must return None WITHOUT clearing session_id — otherwise it ping-pongs
        against load_session_map (which re-applies the id every poll) and the
        bot can never track the new session."""
        from ccbot import session as session_mod

        agent = self._agent(tmp_path)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])
        # projects root may exist, but no <sid>.jsonl has been written yet.
        (agent.claude_home_host_path / "projects").mkdir(parents=True)

        ws = mgr.get_window_state("docker:assistant")
        ws.session_id = "fresh-session-uuid"
        ws.cwd = "/workspace"

        session = await mgr.resolve_session_for_window("docker:assistant")
        assert session is None
        # The binding must survive so a later poll resolves it once the
        # transcript appears.
        survived = mgr.get_window_state("docker:assistant")
        assert survived.session_id == "fresh-session-uuid"
        assert survived.cwd == "/workspace"


class TestLoadSessionMapMerge:
    """load_session_map merges the main session_map with every active
    docker agent's session_map so window_states carry both kinds of
    entries under their binding values.
    """

    def _agent(self, tmp_path, name="assistant"):
        from ccbot.config import DockerAgentConfig

        return DockerAgentConfig(
            name=name,
            container=f"{name}-ctn",
            workspace_host_path=tmp_path / name / "workspace",
            claude_home_host_path=tmp_path / name / "claude-home",
            ipc_dir=tmp_path / name / "ipc",
            session_map_path=tmp_path / name / "session-map.json",
        )

    async def test_merges_main_and_docker(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        import json

        from ccbot import session as session_mod

        main_map = tmp_path / "main.json"
        main_map.write_text(
            json.dumps(
                {
                    "ccbot:@12": {
                        "session_id": "tmux-sid",
                        "cwd": "/host/proj",
                        "window_name": "tmux-proj",
                    }
                }
            )
        )
        monkeypatch.setattr(session_mod.config, "session_map_file", main_map)
        agent = self._agent(tmp_path)
        agent.session_map_path.parent.mkdir(parents=True, exist_ok=True)
        agent.session_map_path.write_text(
            json.dumps(
                {
                    "docker:assistant": {
                        "session_id": "docker-sid",
                        "cwd": "/workspace",
                        "window_name": "assistant",
                    }
                }
            )
        )
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])

        await mgr.load_session_map()

        assert mgr.window_states["@12"].session_id == "tmux-sid"
        assert mgr.window_states["docker:assistant"].session_id == "docker-sid"
        assert mgr.window_display_names["docker:assistant"] == "assistant"


class TestAgentFilePathResolver:
    """resolve_agent_file_path enforces the /workspace whitelist for
    docker bindings so a compromised agent can't exfiltrate
    /auth/.credentials.json or any other bind-mounted secret by writing
    ``(send file: /auth/...)``. Also guards against `..` traversal.
    """

    def _agent(self, tmp_path, name="assistant"):
        from ccbot.config import DockerAgentConfig

        workspace = tmp_path / name / "workspace"
        workspace.mkdir(parents=True)
        return DockerAgentConfig(
            name=name,
            container=f"{name}-ctn",
            workspace_host_path=workspace,
            claude_home_host_path=tmp_path / name / "claude-home",
            ipc_dir=tmp_path / name / "ipc",
            session_map_path=tmp_path / name / "session-map.json",
        )

    def test_tmux_binding_passes_through(self, mgr: SessionManager) -> None:
        from pathlib import Path

        assert mgr.resolve_agent_file_path("@12", "/tmp/foo.txt") == Path(
            "/tmp/foo.txt"
        )

    def test_docker_workspace_path_resolves(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        agent = self._agent(tmp_path)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])
        resolved = mgr.resolve_agent_file_path(
            "docker:assistant", "/workspace/outputs/x.png"
        )
        assert resolved == (agent.workspace_host_path / "outputs/x.png").resolve()

    def test_docker_rejects_non_workspace(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """Attempts to send secrets or system files from the container
        must be refused — this is the security perimeter."""
        from ccbot import session as session_mod

        agent = self._agent(tmp_path)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])
        for bad in [
            "/auth/.credentials.json",
            "/root/.ssh/id_rsa",
            "/etc/passwd",
            "/workspace-sibling/x",  # prefix-match style probing
            "relative/path.txt",
        ]:
            assert mgr.resolve_agent_file_path("docker:assistant", bad) is None, bad

    def test_docker_rejects_traversal(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """``/workspace/../auth/x`` must not escape workspace_host_path."""
        from ccbot import session as session_mod

        agent = self._agent(tmp_path)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])
        assert (
            mgr.resolve_agent_file_path("docker:assistant", "/workspace/../etc/passwd")
            is None
        )

    def test_docker_unknown_agent_rejected(
        self, mgr: SessionManager, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        monkeypatch.setattr(session_mod.config, "docker_agents", [])
        assert mgr.resolve_agent_file_path("docker:ghost", "/workspace/x.png") is None


class TestLoadSessionMapMergeNoop:
    """Separate from TestLoadSessionMapMerge so the noop check stands alone."""

    async def test_no_sources_is_noop(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """No session_map source anywhere → preserve window_states. Matches
        the old early-return on first boot before any hook fires."""
        from ccbot import session as session_mod

        mgr.get_window_state("@7").session_id = "preserved"
        monkeypatch.setattr(
            session_mod.config, "session_map_file", tmp_path / "absent.json"
        )
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", False)
        monkeypatch.setattr(session_mod.config, "docker_agents", [])

        await mgr.load_session_map()
        assert mgr.window_states["@7"].session_id == "preserved"


class TestEncodeCwd:
    def test_encoding_replaces_non_alnum(self) -> None:
        assert SessionManager._encode_cwd("/home/user/a_b.c") == "-home-user-a-b-c"

    def test_empty(self) -> None:
        assert SessionManager._encode_cwd("") == ""

    def test_symlink_resolved(self, tmp_path) -> None:
        # Real directory and a symlink pointing to it must encode to the same
        # string — otherwise Claude Code splits session history.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert SessionManager._encode_cwd(str(link)) == SessionManager._encode_cwd(
            str(real)
        )

    def test_normalize_missing_path_no_crash(self) -> None:
        # realpath on a non-existent path just returns the absolute form —
        # must not raise.
        result = SessionManager._encode_cwd("/nonexistent/some/path")
        assert result.startswith("-")


class TestLoadSessionMapUnreadableSource:
    """A session_map source that exists but fails to read/parse must not
    trigger the stale-window_states cleanup — one corrupt or mid-write
    file would otherwise wipe every tracked window in a single poll."""

    async def test_corrupt_main_map_preserves_window_states(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        from ccbot import session as session_mod

        st = mgr.get_window_state("@7")
        st.session_id = "sid-7"

        main_map = tmp_path / "main.json"
        main_map.write_text("{ this is not json")
        monkeypatch.setattr(session_mod.config, "session_map_file", main_map)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", False)

        await mgr.load_session_map()

        assert "@7" in mgr.window_states
        assert mgr.window_states["@7"].session_id == "sid-7"

    async def test_readable_empty_map_still_cleans_up(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        """Contrast case: a VALID map without the entry does clean up —
        the guard keys on readability, not on emptiness."""
        import json as json_mod

        from ccbot import session as session_mod

        st = mgr.get_window_state("@7")
        st.session_id = "sid-7"

        main_map = tmp_path / "main.json"
        main_map.write_text(json_mod.dumps({}))
        monkeypatch.setattr(session_mod.config, "session_map_file", main_map)
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", False)

        await mgr.load_session_map()

        assert "@7" not in mgr.window_states


class TestPerAgentSessionMapSpoof:
    """A per-agent session_map is written inside the container — untrusted.
    Only the agent's own binding key may be ingested; a foreign docker:*
    key would let a compromised agent overwrite another agent's state."""

    def _agent(self, tmp_path, name="assistant"):
        from ccbot.config import DockerAgentConfig

        return DockerAgentConfig(
            name=name,
            container=f"{name}-ctn",
            workspace_host_path=tmp_path / name / "workspace",
            claude_home_host_path=tmp_path / name / "claude-home",
            ipc_dir=tmp_path / name / "ipc",
            session_map_path=tmp_path / name / "session-map.json",
        )

    async def test_foreign_key_in_agent_map_is_ignored(
        self, mgr: SessionManager, tmp_path, monkeypatch
    ) -> None:
        import json as json_mod

        from ccbot import session as session_mod

        agent = self._agent(tmp_path, "assistant")
        agent.session_map_path.parent.mkdir(parents=True, exist_ok=True)
        agent.session_map_path.write_text(
            json_mod.dumps(
                {
                    "docker:assistant": {
                        "session_id": "own-sid",
                        "cwd": "/workspace",
                        "window_name": "assistant",
                    },
                    "docker:admin": {
                        "session_id": "EVIL",
                        "cwd": "/pwned",
                        "window_name": "admin",
                    },
                }
            )
        )
        monkeypatch.setattr(
            session_mod.config, "session_map_file", tmp_path / "absent.json"
        )
        monkeypatch.setattr(session_mod.config, "docker_agents_enabled", True)
        monkeypatch.setattr(session_mod.config, "docker_agents", [agent])

        await mgr.load_session_map()

        assert mgr.window_states["docker:assistant"].session_id == "own-sid"
        assert "docker:admin" not in mgr.window_states


class TestSendLockSerialization:
    """Per-binding send lock: concurrent multi-step sends into one pane
    must not interleave (chunked typing + Enter is a critical section);
    different bindings must still run in parallel."""

    class _FakeWindow:
        window_id = "@1"

    def _patch_transport(self, monkeypatch, events: list[str], delay: float = 0.01):
        import asyncio as aio

        from ccbot import session as session_mod

        async def fake_find(window_id):
            w = self._FakeWindow()
            w.window_id = window_id
            return w

        async def fake_send(window_id, text):
            events.append(f"start:{window_id}:{text}")
            await aio.sleep(delay)
            events.append(f"end:{window_id}:{text}")
            return True

        monkeypatch.setattr(session_mod.tmux_manager, "find_window_by_id", fake_find)
        monkeypatch.setattr(session_mod.tmux_manager, "send_keys", fake_send)

    async def test_same_binding_serializes(self, mgr: SessionManager, monkeypatch):
        import asyncio as aio

        events: list[str] = []
        self._patch_transport(monkeypatch, events)

        await aio.gather(
            mgr.send_to_window("@1", "first"),
            mgr.send_to_window("@1", "second"),
        )

        # Each send completes before the next begins.
        assert [e.split(":")[0] for e in events] == ["start", "end", "start", "end"]

    async def test_different_bindings_parallel(self, mgr: SessionManager, monkeypatch):
        import asyncio as aio

        events: list[str] = []
        self._patch_transport(monkeypatch, events)

        await aio.gather(
            mgr.send_to_window("@1", "a"),
            mgr.send_to_window("@2", "b"),
        )

        # Both started before either finished — no cross-binding serialization.
        kinds = [e.split(":")[0] for e in events]
        assert kinds[:2] == ["start", "start"]


class TestRuntimeAwareBusyChecks:
    """is_agent_working / agent_has_queued_input dispatch on WindowState.runtime,
    so the same 'don't barge' call sites work for a Claude window and a Codex
    window without an `if codex:` branch (CLAUDE.md 'runtime is a SECOND axis')."""

    _SEP = "─" * 40
    _CLAUDE_WORKING = f"✶ Orbiting… (3m 13s · esc to interrupt)\n{_SEP}\n  ❯ \n{_SEP}\n"
    _CODEX_WORKING = (
        "◦ Working (4s • esc to interrupt)\n"
        "› Summarize recent commits\n"
        "  gpt-5.5 medium · /home/user/project\n"
    )
    _CODEX_QUEUED = (
        "◦ Working (7s • esc to interrupt)\n"
        "• Messages to be submitted after next tool call "
        "(press esc to interrupt and send immediately)\n"
        "  gpt-5.5 medium · /home/user/project\n"
    )

    def _mgr(self):
        from ccbot.session import WindowState

        mgr = SessionManager()
        mgr.window_states["@5"] = WindowState(runtime="claude")
        mgr.window_states["@9"] = WindowState(runtime="codex")
        return mgr

    def test_window_runtime(self):
        mgr = self._mgr()
        assert mgr.window_runtime("@5") == "claude"
        assert mgr.window_runtime("@9") == "codex"
        # Unknown / docker binding with no state → default claude.
        assert mgr.window_runtime("docker:x") == "claude"

    def test_is_agent_working_dispatches(self):
        mgr = self._mgr()
        assert mgr.is_agent_working("@5", self._CLAUDE_WORKING) is True
        assert mgr.is_agent_working("@9", self._CODEX_WORKING) is True
        # Cross: the codex window's detector doesn't fire on Claude chrome
        # (no ─ separator match), and Claude's doesn't fire on the codex pane.
        assert mgr.is_agent_working("@9", self._CLAUDE_WORKING) is False
        assert mgr.is_agent_working("@5", self._CODEX_WORKING) is False

    def test_empty_pane_not_working(self):
        mgr = self._mgr()
        assert mgr.is_agent_working("@9", None) is False
        assert mgr.is_agent_working("@9", "") is False

    def test_agent_has_queued_input_dispatches(self):
        mgr = self._mgr()
        assert mgr.agent_has_queued_input("@9", self._CODEX_QUEUED) is True
        assert mgr.agent_has_queued_input("@9", self._CODEX_WORKING) is False
        assert mgr.agent_has_queued_input("@9", None) is False


class TestSendComposerImage:
    """Codex image delivery: type path → Enter (attach [Image #N]) → confirm →
    caption → Enter (submit), all under one send_lock via the tmux primitives."""

    @pytest.mark.asyncio
    async def test_deterministic_attach_sequence(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from ccbot.session import WindowState
        import ccbot.session as sess

        mgr = SessionManager()
        mgr.window_states["@9"] = WindowState(runtime="codex")

        tm = MagicMock()
        tm.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="w9"))
        tm.send_keys = AsyncMock(return_value=True)
        # pane already shows the attach token → the confirm poll breaks at once.
        tm.capture_pane = AsyncMock(return_value="› [Image #1]\n  model · dir\n")
        monkeypatch.setattr(sess, "tmux_manager", tm)
        monkeypatch.setattr(sess.asyncio, "sleep", AsyncMock())

        ok = await mgr.send_composer_image("@9", "/tmp/x.png", "what is this")
        assert ok is True

        sends = [(c.args[1], c.kwargs) for c in tm.send_keys.await_args_list]
        # path (literal, no enter) → Enter key → caption (literal) → Enter key
        assert sends[0] == ("/tmp/x.png", {"enter": False, "literal": True})
        assert sends[1] == ("Enter", {"enter": False, "literal": False})
        assert sends[2] == (" what is this", {"enter": False, "literal": True})
        assert sends[3] == ("Enter", {"enter": False, "literal": False})

    @pytest.mark.asyncio
    async def test_no_caption_still_submits(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from ccbot.session import WindowState
        import ccbot.session as sess

        mgr = SessionManager()
        mgr.window_states["@9"] = WindowState(runtime="codex")
        tm = MagicMock()
        tm.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="w9"))
        tm.send_keys = AsyncMock(return_value=True)
        tm.capture_pane = AsyncMock(return_value="› [Image #1]\n")
        monkeypatch.setattr(sess, "tmux_manager", tm)
        monkeypatch.setattr(sess.asyncio, "sleep", AsyncMock())

        ok = await mgr.send_composer_image("@9", "/tmp/x.png", "")
        assert ok is True
        sends = [c.args[1] for c in tm.send_keys.await_args_list]
        # no caption send: path, Enter, Enter (submit)
        assert sends == ["/tmp/x.png", "Enter", "Enter"]

    @pytest.mark.asyncio
    async def test_missing_window_returns_false(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import ccbot.session as sess

        mgr = SessionManager()
        tm = MagicMock()
        tm.find_window_by_id = AsyncMock(return_value=None)
        monkeypatch.setattr(sess, "tmux_manager", tm)
        assert await mgr.send_composer_image("@9", "/tmp/x.png", "cap") is False
