"""Unit tests for Config — env var loading, validation, and user access."""

from pathlib import Path

import pytest

from ccbot.config import Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_custom_tmux_session_name(self, monkeypatch):
        monkeypatch.setenv("TMUX_SESSION_NAME", "mysession")
        cfg = Config()
        assert cfg.tmux_session_name == "mysession"

    def test_custom_monitor_poll_interval(self, monkeypatch):
        monkeypatch.setenv("MONITOR_POLL_INTERVAL", "5.0")
        cfg = Config()
        assert cfg.monitor_poll_interval == 5.0

    def test_is_user_allowed_true(self):
        cfg = Config()
        assert cfg.is_user_allowed(12345) is True

    def test_is_user_allowed_false(self):
        cfg = Config()
        assert cfg.is_user_allowed(99999) is False


@pytest.mark.usefixtures("_base_env")
class TestConfigMissingEnv:
    def test_missing_telegram_bot_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()

    def test_missing_allowed_users(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            Config()

    def test_non_numeric_allowed_users(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_USERS", "abc")
        with pytest.raises(ValueError, match="non-numeric"):
            Config()


@pytest.mark.usefixtures("_base_env")
class TestConfigClaudeProjectsPath:
    def test_default_claude_projects_path(self, monkeypatch):
        """Default path is ~/.claude/projects when no env vars are set."""
        # Ensure no custom path env vars are set
        monkeypatch.delenv("CCBOT_CLAUDE_PROJECTS_PATH", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        cfg = Config()
        assert cfg.claude_projects_path == Path.home() / ".claude" / "projects"

    def test_custom_claude_projects_path(self, monkeypatch):
        """CCBOT_CLAUDE_PROJECTS_PATH overrides the default path."""
        custom_path = "/custom/projects/path"
        monkeypatch.setenv("CCBOT_CLAUDE_PROJECTS_PATH", custom_path)
        cfg = Config()
        assert cfg.claude_projects_path == Path(custom_path)

    def test_browse_root_unset_is_none(self):
        """Unset → legacy behavior (browser starts at $HOME, up to /)."""
        cfg = Config()
        assert cfg.browse_root is None

    def test_browse_root_valid_dir(self, monkeypatch, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        monkeypatch.setenv("CCBOT_BROWSE_ROOT", str(root))
        cfg = Config()
        assert cfg.browse_root == root.resolve()

    def test_browse_root_invalid_is_ignored(self, monkeypatch, tmp_path):
        """A typo must not dark the browser — warn and fall back to unset."""
        monkeypatch.setenv("CCBOT_BROWSE_ROOT", str(tmp_path / "nope"))
        cfg = Config()
        assert cfg.browse_root is None

    def test_claude_config_dir_projects_path(self, monkeypatch):
        """CLAUDE_CONFIG_DIR sets path to $CLAUDE_CONFIG_DIR/projects."""
        custom_config_dir = "/custom/claude/config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", custom_config_dir)
        cfg = Config()
        assert cfg.claude_projects_path == Path(custom_config_dir) / "projects"

    def test_ccbot_projects_path_takes_priority(self, monkeypatch):
        """CCBOT_CLAUDE_PROJECTS_PATH takes priority over CLAUDE_CONFIG_DIR."""
        monkeypatch.setenv("CCBOT_CLAUDE_PROJECTS_PATH", "/priority/path")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/lower/priority")
        cfg = Config()
        assert cfg.claude_projects_path == Path("/priority/path")


@pytest.mark.usefixtures("_base_env")
class TestConfigOpenAI:
    def test_openai_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        cfg = Config()
        assert cfg.openai_api_key == ""
        assert cfg.openai_base_url == "https://api.openai.com/v1"

    def test_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        cfg = Config()
        assert cfg.openai_api_key == "sk-test-123"

    def test_openai_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
        cfg = Config()
        assert cfg.openai_base_url == "https://proxy.example.com/v1"

    def test_openai_api_key_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        Config()
        assert os.environ.get("OPENAI_API_KEY") is None


class TestParseDockerAgents:
    """Convention-over-config for docker-agent env parsing.

    The parser lives as a pure function (``_parse_docker_agents``) so
    we can feed it an env mapping directly, no telegram token needed.
    Covers: defaults derived from name, explicit overrides, mix, and
    the empty-string-override fall-through (a stale blank in .env
    must not silently replace a sane default).
    """

    def test_empty_env_returns_empty(self) -> None:
        from ccbot.config import _parse_docker_agents

        assert _parse_docker_agents({}, Path("/home/u")) == []

    def test_blank_docker_agents_returns_empty(self) -> None:
        from ccbot.config import _parse_docker_agents

        assert _parse_docker_agents({"DOCKER_AGENTS": "  "}, Path("/home/u")) == []

    def test_single_agent_all_defaults(self) -> None:
        from ccbot.config import _parse_docker_agents

        env = {"DOCKER_AGENTS": "assistant"}
        home = Path("/home/u")
        agents = _parse_docker_agents(env, home)
        assert len(agents) == 1
        a = agents[0]
        assert a.name == "assistant"
        assert a.container == "assistant"
        assert a.workspace_host_path == home / "agents" / "assistant"
        assert (
            a.claude_home_host_path
            == home / ".local" / "share" / "assistant" / "claude-home"
        )
        assert a.ipc_dir == home / ".local" / "share" / "assistant" / "ipc"
        assert (
            a.session_map_path
            == home / ".local" / "share" / "assistant" / "session-map.json"
        )

    def test_hyphenated_name_normalizes_env_key(self) -> None:
        from ccbot.config import _parse_docker_agents

        env = {
            "DOCKER_AGENTS": "pilot-browser",
            "DOCKER_AGENT_PILOT_BROWSER_WORKSPACE": "/custom/ws",
        }
        agents = _parse_docker_agents(env, Path("/home/u"))
        assert len(agents) == 1
        assert agents[0].name == "pilot-browser"
        assert agents[0].workspace_host_path == Path("/custom/ws")

    def test_explicit_overrides_respected(self) -> None:
        from ccbot.config import _parse_docker_agents

        env = {
            "DOCKER_AGENTS": "assistant",
            "DOCKER_AGENT_ASSISTANT_CONTAINER": "custom-ctn",
            "DOCKER_AGENT_ASSISTANT_WORKSPACE": "/elsewhere/ws",
            "DOCKER_AGENT_ASSISTANT_CLAUDE_HOME": "/elsewhere/ch",
            "DOCKER_AGENT_ASSISTANT_IPC": "/elsewhere/ipc",
            "DOCKER_AGENT_ASSISTANT_SESSION_MAP": "/elsewhere/session.json",
        }
        agents = _parse_docker_agents(env, Path("/home/u"))
        assert len(agents) == 1
        a = agents[0]
        assert a.container == "custom-ctn"
        assert a.workspace_host_path == Path("/elsewhere/ws")
        assert a.claude_home_host_path == Path("/elsewhere/ch")
        assert a.ipc_dir == Path("/elsewhere/ipc")
        assert a.session_map_path == Path("/elsewhere/session.json")

    def test_partial_overrides_mix_with_defaults(self) -> None:
        from ccbot.config import _parse_docker_agents

        env = {
            "DOCKER_AGENTS": "assistant",
            "DOCKER_AGENT_ASSISTANT_WORKSPACE": "/custom/ws",
        }
        home = Path("/home/u")
        a = _parse_docker_agents(env, home)[0]
        assert a.workspace_host_path == Path("/custom/ws")
        # Untouched fields fall back to derived defaults.
        assert a.ipc_dir == home / ".local" / "share" / "assistant" / "ipc"

    def test_multiple_agents(self) -> None:
        from ccbot.config import _parse_docker_agents

        env = {"DOCKER_AGENTS": "assistant, pilot"}
        agents = _parse_docker_agents(env, Path("/h"))
        assert [a.name for a in agents] == ["assistant", "pilot"]
        assert agents[0].workspace_host_path == Path("/h/agents/assistant")
        assert agents[1].workspace_host_path == Path("/h/agents/pilot")

    def test_empty_names_in_list_skipped(self) -> None:
        from ccbot.config import _parse_docker_agents

        agents = _parse_docker_agents({"DOCKER_AGENTS": "a,,,b"}, Path("/h"))
        assert [a.name for a in agents] == ["a", "b"]

    def test_empty_string_override_falls_through_to_default(self) -> None:
        # A stale blank value in .env shouldn't silently break paths —
        # treat it like absence and use the derived default.
        from ccbot.config import _parse_docker_agents

        env = {
            "DOCKER_AGENTS": "assistant",
            "DOCKER_AGENT_ASSISTANT_WORKSPACE": "",
        }
        a = _parse_docker_agents(env, Path("/h"))[0]
        assert a.workspace_host_path == Path("/h/agents/assistant")


class TestParseInjectConfig:
    """`_parse_inject_config` — the /inject endpoint feature-flag + gating.

    Defaults, token-disables, agent allowlist parsing, and overrides.
    """

    def test_defaults_are_derived_from_home(self) -> None:
        from ccbot.config import _parse_inject_config

        cfg = _parse_inject_config({"CCBOT_INJECT_TOKEN": "tok"}, Path("/home/u"))
        assert cfg.token == "tok"
        assert cfg.socket_path == Path("/home/u/.ccbot/run/inject.sock")
        assert cfg.allowed_agents == frozenset({"assistant"})
        assert cfg.is_enabled() is True

    def test_empty_token_disables_endpoint(self) -> None:
        from ccbot.config import _parse_inject_config

        cfg = _parse_inject_config({}, Path("/home/u"))
        assert cfg.token == ""
        assert cfg.is_enabled() is False
        # Defaults still present so the intended layout is inspectable.
        assert cfg.allowed_agents == frozenset({"assistant"})

    def test_whitespace_token_treated_as_disabled(self) -> None:
        from ccbot.config import _parse_inject_config

        cfg = _parse_inject_config({"CCBOT_INJECT_TOKEN": "   "}, Path("/home/u"))
        assert cfg.is_enabled() is False

    def test_agents_list_parsed_and_trimmed(self) -> None:
        from ccbot.config import _parse_inject_config

        cfg = _parse_inject_config(
            {"CCBOT_INJECT_TOKEN": "tok", "CCBOT_INJECT_AGENTS": " assistant , scout "},
            Path("/home/u"),
        )
        assert cfg.allowed_agents == frozenset({"assistant", "scout"})

    def test_blank_agents_falls_back_to_assistant(self) -> None:
        from ccbot.config import _parse_inject_config

        cfg = _parse_inject_config(
            {"CCBOT_INJECT_TOKEN": "tok", "CCBOT_INJECT_AGENTS": " , "},
            Path("/home/u"),
        )
        assert cfg.allowed_agents == frozenset({"assistant"})

    def test_socket_path_override_respected(self) -> None:
        from ccbot.config import _parse_inject_config

        cfg = _parse_inject_config(
            {"CCBOT_INJECT_TOKEN": "tok", "CCBOT_INJECT_SOCKET": "/run/custom.sock"},
            Path("/home/u"),
        )
        assert cfg.socket_path == Path("/run/custom.sock")


@pytest.mark.usefixtures("_base_env")
class TestPortabilityKnobs:
    """Server-layout knobs default to this server's values but are all
    overridable (CCBOT_TOPIC_DIR_ROOTS; preview paths for the worktree
    teardown hook)."""

    def test_topic_dir_roots_default(self, monkeypatch):
        monkeypatch.delenv("CCBOT_TOPIC_DIR_ROOTS", raising=False)
        assert Config().topic_dir_roots == ("projects", "agents")

    def test_topic_dir_roots_override_trims_blanks(self, monkeypatch):
        monkeypatch.setenv("CCBOT_TOPIC_DIR_ROOTS", "repos, work ,")
        assert Config().topic_dir_roots == ("repos", "work")
