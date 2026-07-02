"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCBOT_DIR/.env (default ~/.ccbot).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from . import plugins as _plugins
from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux)
SENSITIVE_ENV_VARS = {
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_USERS",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "GEMINI_API_KEY",
    "CCBOT_INJECT_TOKEN",
}


@dataclass(frozen=True)
class DockerAgentConfig:
    """Config for a Claude Code agent running inside a Docker container.

    Alongside the default tmux-window agents, ccbot can route a topic to
    an isolated container. Everything a tmux agent uses (JSONL transcripts,
    session_map, workspace files) lives on host paths bind-mounted into the
    container; ccbot reads/writes them via these fields.

    Attributes:
        name: Short agent name matched against the "docker:<name>" binding
            prefix and used as the topic handle.
        container: Docker container name for `docker exec`.
        workspace_host_path: Host path bind-mounted as /workspace in the
            container. Used to resolve `(send file: /workspace/...)` markers.
        claude_home_host_path: Host path bind-mounted as /home/<user>/.claude
            in the container. Under this, projects/*.jsonl holds session logs
            the monitor reads.
        ipc_dir: Host path bind-mounted as /ipc in the container. Holds
            browser-live.json + current.png written by the live daemon.
        session_map_path: Host path of the per-agent session_map.json written
            by the container's SessionStart hook. Merged by the session
            monitor alongside the main session_map.
        vnc_url: Optional URL the live-dashboard renders as a "📺 VNC"
            button under each screenshot. Typically ``vnc://<host>:5900``
            so a tap on iOS hands off to RealVNC Viewer. ``None`` → no button.
    """

    name: str
    container: str
    workspace_host_path: Path
    claude_home_host_path: Path
    ipc_dir: Path
    session_map_path: Path
    vnc_url: str | None = None


@dataclass(frozen=True)
class InjectConfig:
    """Config for the ``/inject`` endpoint — fire-and-forget task injection.

    An optional local-only endpoint (a **unix socket**, not TCP) that types
    a task into an agent's pane *as a prompt* — a hook for local automation
    (a shortcut or script that hands work to a running agent one-way; the
    agent's reply goes back to the user in Telegram as usual).

    Why unix socket + token + sanitizer (the security model):

    - The socket lives at ``socket_path`` (mode ``0660`` under a ``0700``
      parent dir) — reachable only by the same local user, not from docker
      containers or other uids.
    - ``token`` (``CCBOT_INJECT_TOKEN``) is required in the ``X-Inject-Token``
      header; an empty token disables the whole endpoint (server doesn't
      start).
    - ``allowed_agents`` (``CCBOT_INJECT_AGENTS``)
      gates which agents may be targeted — an unlisted agent is 403.

    The leading-``!`` RCE shield (a leading ``!`` would drop Claude Code's
    TUI into bash command-mode = host shell) lives in
    ``inject.core.sanitize_inject_text``, not here — it applies to every
    payload regardless of config.
    """

    token: str
    socket_path: Path
    allowed_agents: frozenset[str]

    def is_enabled(self) -> bool:
        """True iff the endpoint should run (token is set)."""
        return bool(self.token)


def _parse_inject_config(env: Mapping[str, str], home: Path) -> InjectConfig:
    """Build an ``InjectConfig`` from an env mapping.

    Pure (no ``os.environ`` access) so tests can feed a synthetic mapping.
    Empty/missing ``CCBOT_INJECT_TOKEN`` yields ``is_enabled() == False``.
    The default socket path is ``~/.ccbot/run/inject.sock``; override with
    ``CCBOT_INJECT_SOCKET`` (e.g. when ``CCBOT_DIR`` is non-default).
    """
    raw_agents = env.get("CCBOT_INJECT_AGENTS", "assistant")
    agents = frozenset(a.strip() for a in raw_agents.split(",") if a.strip())
    if not agents:
        agents = frozenset({"assistant"})
    return InjectConfig(
        token=env.get("CCBOT_INJECT_TOKEN", "").strip(),
        socket_path=Path(
            env.get(
                "CCBOT_INJECT_SOCKET",
                str(home / ".ccbot" / "run" / "inject.sock"),
            )
        ).expanduser(),
        allowed_agents=agents,
    )


def _parse_docker_agents(env: Mapping[str, str], home: Path) -> list[DockerAgentConfig]:
    """Parse docker-agent definitions from an env mapping.

    ``DOCKER_AGENTS`` is a comma-separated list of agent names. Every
    per-agent path defaults to a standard location derived from the
    name — the common layout requires zero extra env vars:

      container    = <name>
      workspace    = ~/agents/<name>
      claude_home  = ~/.local/share/<name>/claude-home
      ipc          = ~/.local/share/<name>/ipc
      session_map  = ~/.local/share/<name>/session-map.json

    Any of these can still be overridden per-agent via
    ``DOCKER_AGENT_<NAME>_{CONTAINER,WORKSPACE,CLAUDE_HOME,IPC,SESSION_MAP}``
    for odd layouts. The overrides are optional; pre-convention ``.env``
    files with all five set are also respected.

    Optional ``DOCKER_AGENT_<NAME>_VNC_URL`` adds a "📺 VNC" button under
    every live-dashboard frame for that agent. No default — unset means
    no button.

    Factored out of ``Config._load_docker_agents`` so tests can exercise
    it without booting the full Config (which requires a telegram token).
    Returns ``[]`` when ``DOCKER_AGENTS`` is unset or empty.
    """
    raw = env.get("DOCKER_AGENTS", "").strip()
    if not raw:
        return []
    agents: list[DockerAgentConfig] = []
    for name in (n.strip() for n in raw.split(",")):
        if not name:
            continue
        key = name.upper().replace("-", "_")
        container = env.get(f"DOCKER_AGENT_{key}_CONTAINER") or name
        workspace = env.get(f"DOCKER_AGENT_{key}_WORKSPACE") or str(
            home / "agents" / name
        )
        claude_home = env.get(f"DOCKER_AGENT_{key}_CLAUDE_HOME") or str(
            home / ".local" / "share" / name / "claude-home"
        )
        ipc = env.get(f"DOCKER_AGENT_{key}_IPC") or str(
            home / ".local" / "share" / name / "ipc"
        )
        session_map = env.get(f"DOCKER_AGENT_{key}_SESSION_MAP") or str(
            home / ".local" / "share" / name / "session-map.json"
        )
        vnc_url = env.get(f"DOCKER_AGENT_{key}_VNC_URL", "").strip() or None
        agents.append(
            DockerAgentConfig(
                name=name,
                container=container,
                workspace_host_path=Path(workspace).expanduser(),
                claude_home_host_path=Path(claude_home).expanduser(),
                ipc_dir=Path(ipc).expanduser(),
                session_map_path=Path(session_map).expanduser(),
                vnc_url=vnc_url,
            )
        )
    return agents


def _parse_rclone_mounts(raw: str, home: Path) -> list[tuple[str, str]]:
    """Parse ``name:path,name:path`` into ``[(remote, expanded_path), …]``.

    Consumed by ``/status`` (mount health) and ``/mount|/umount|/remount``.
    Empty string → ``[]`` (a plain host with no rclone remotes, i.e. every
    deployment that isn't this server). ``~`` at the start of a path is
    expanded against *home* so the function stays pure/testable (no
    ``Path.home()`` side effect). Malformed items (no ``:``, empty half)
    are skipped rather than raising — a bad mount line must not crash boot.
    """
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, _, path = item.partition(":")
        name, path = name.strip(), path.strip()
        if not name or not path:
            continue
        if path.startswith("~"):
            path = str(home) + path[1:]
        out.append((name, path))
    return out


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccbot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
        if self.telegram_bot_token == "your_bot_token_here":
            # The literal from .env.example — a copied-but-unedited config.
            # PTB would otherwise fail much later with a cryptic InvalidToken.
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is still the placeholder from .env.example — "
                "replace it with your real bot token from @BotFather"
            )

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccbot")
        self.tmux_main_window_name = "__main__"

        # Claude command to run in new windows
        self.claude_command = os.getenv("CLAUDE_COMMAND", "claude")

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Claude Code session monitoring configuration
        # Support custom projects path for Claude variants (e.g., cc-mirror, zai)
        # Priority: CCBOT_CLAUDE_PROJECTS_PATH > CLAUDE_CONFIG_DIR/projects > default
        custom_projects_path = os.getenv("CCBOT_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")

        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = (
            os.getenv("CCBOT_SHOW_USER_MESSAGES", "false").lower() == "true"
        )

        # Show tool call notifications (tool_use/tool_result) in Telegram
        # When False, only text responses, thinking, and interactive prompts are sent
        self.show_tool_calls = (
            os.getenv("CCBOT_SHOW_TOOL_CALLS", "false").lower() == "true"
        )

        # Show thinking blocks in Telegram
        # When False, thinking content is hidden
        self.show_thinking = os.getenv("CCBOT_SHOW_THINKING", "false").lower() == "true"

        # Show tool results (output of tool calls) in Telegram
        # When False, only tool_use headers are shown, not their output
        self.show_tool_results = (
            os.getenv("CCBOT_SHOW_TOOL_RESULTS", "false").lower() == "true"
        )

        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("CCBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # Default UI language (ru/en) for ccbot's own chrome. A global
        # setting persisted in state.json overrides this; this is just the
        # boot default before state loads / on a fresh install.
        _lang = os.getenv("CCBOT_DEFAULT_LANG", "en").strip().lower()
        self.default_lang = _lang if _lang in ("ru", "en") else "en"

        # Deepgram API for voice message transcription (preferred)
        self.deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
        # Transcription language: empty = auto-detect (Deepgram detect_language,
        # covers ~35 langs incl. ru/en/id). Set a BCP-47 code (e.g. "ru") to pin
        # one language if auto-detect ever misbehaves. OpenAI fallback always
        # auto-detects regardless.
        self.deepgram_language: str = os.getenv("DEEPGRAM_LANGUAGE", "").strip()

        # OpenAI API for voice message transcription (fallback) and TTS
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )

        # TTS provider selection policy. "auto" = walk configured providers
        # by priority with fallback (Gemini → ElevenLabs → OpenAI). Any other
        # value locks to that single provider — no fallback, raises if it's
        # unavailable. Pinning avoids the "different voice on different
        # message" effect users see when Gemini times out and OpenAI silently
        # takes over. Values: "auto" | "gemini" | "elevenlabs" | "openai".
        self.tts_provider: str = os.getenv("TTS_PROVIDER", "auto").strip().lower()

        # Gemini TTS (preferred — expressive, audio tags, human-like)
        self.gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
        self.gemini_tts_model: str = os.getenv(
            "GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview"
        )
        self.gemini_tts_voice: str = os.getenv("GEMINI_TTS_VOICE", "Sulafat")
        # Temperature controls expressiveness (0.0=monotone, 2.0=wild).
        # ~1.0 gives natural conversational variation without overacting.
        self.gemini_tts_temperature: float = float(
            os.getenv("GEMINI_TTS_TEMPERATURE", "1.0")
        )
        # BCP-47 language code; explicit beats auto-detect per Google docs.
        self.gemini_tts_language_code: str = os.getenv("GEMINI_TTS_LANGUAGE", "ru-RU")
        # Style prefix prepended to every Gemini TTS prompt. Gemini parses
        # "Say/Speak X: ..." prefixes and applies the style to the whole
        # utterance — more reliable than inline pace tags (which burn off
        # after the first phrase). Empty string disables.
        self.gemini_tts_style_prefix: str = os.getenv(
            "GEMINI_TTS_STYLE_PREFIX",
            "Speak warmly like you're chatting with a close friend, "
            "at a brisk natural pace:",
        )

        # ElevenLabs TTS (secondary)
        self.elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
        self.elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "")
        self.elevenlabs_model: str = os.getenv(
            "ELEVENLABS_MODEL", "eleven_multilingual_v2"
        )

        # OpenAI TTS (fallback)
        self.tts_voice: str = os.getenv("OPENAI_TTS_VOICE", "nova")
        self.tts_model: str = os.getenv("OPENAI_TTS_MODEL", "tts-1")

        # chat_id of a supergroup used for cross-cutting notices and the
        # live-dashboard topic (see live_dashboard_target). Unset → those
        # features are disabled; the core bot doesn't need it. Optional
        # plugins (e.g. mail) may also read it.
        notif_raw = os.getenv("NOTIFICATIONS_CHAT_ID", "").strip()
        self.notifications_chat_id: int | None
        if notif_raw:
            try:
                self.notifications_chat_id = int(notif_raw)
            except ValueError as e:
                raise ValueError(
                    f"NOTIFICATIONS_CHAT_ID must be an integer (got {notif_raw!r})"
                ) from e
        else:
            self.notifications_chat_id = None

        # Live-dashboard topic — thread_id (within NOTIFICATIONS_CHAT_ID) of
        # a single shared topic where every isolated docker agent's browser
        # screenshot lives as one always-edited message. Decouples the live
        # view from the agent's own topic so new chat doesn't bury it. The
        # topic is created by hand (right-click group → New Topic), id is
        # read from the URL. Disabled when either var is unset.
        live_thread_raw = os.getenv("LIVE_DASHBOARD_THREAD_ID", "").strip()
        self.live_dashboard_thread_id: int | None
        if live_thread_raw:
            try:
                self.live_dashboard_thread_id = int(live_thread_raw)
            except ValueError as e:
                raise ValueError(
                    f"LIVE_DASHBOARD_THREAD_ID must be an integer "
                    f"(got {live_thread_raw!r})"
                ) from e
        else:
            self.live_dashboard_thread_id = None

        # Optional deep-link rendered as "🔗 Tailscale" in every live-dashboard
        # caption. Used so the user can tap the VPN open before tapping VNC —
        # the VNC link only resolves while Tailscale is connected. Empty string
        # disables the link.
        self.live_dashboard_tailscale_url: str | None = (
            os.getenv("LIVE_DASHBOARD_TAILSCALE_URL", "").strip() or None
        )

        # Reaction-to-confirm: 👍 on an agent-originated topic message means
        # "yes, go ahead" — press Enter on an interactive prompt, or type «да»
        # to an idle agent (busy agent → ignored). A short debounce lets an
        # accidental tap be taken back. Adds "message_reaction" to the polled
        # update types when enabled. See handlers/reaction_confirm.py.
        self.reaction_confirm_enabled: bool = (
            os.getenv("REACTION_CONFIRM_ENABLED", "true").lower() != "false"
        )
        self.reaction_confirm_emoji: str = (
            os.getenv("REACTION_CONFIRM_EMOJI", "").strip() or "👍"
        )
        try:
            self.reaction_confirm_debounce_sec: float = float(
                os.getenv("REACTION_CONFIRM_DEBOUNCE_SEC", "2.5")
            )
        except ValueError as e:
            raise ValueError(
                f"REACTION_CONFIRM_DEBOUNCE_SEC must be a number "
                f"(got {os.getenv('REACTION_CONFIRM_DEBOUNCE_SEC')!r})"
            ) from e

        # Reaction-ack default (/react persists the choice per-install; this is
        # only the value a fresh install starts with). ON by default — the bot
        # puts 👀 on your message the instant the agent takes it into context (a
        # read-receipt). Note: Telegram pushes a reaction notification per
        # message; set false (or mute reactions client-side) if that's noise.
        self.reaction_ack_default: bool = (
            os.getenv("CCBOT_REACTION_ACK", "true").lower() != "false"
        )

        # Transparent session resume on auto-bind/rebind. OFF by default (the
        # interactive session picker is the norm). When ON, a topic auto-binding
        # to a folder that already has Claude history silently continues the most
        # recent session instead of showing a picker — for non-technical users
        # in agent topics (e.g. an in-container ccbot driving agents as tmux
        # windows) whose sessions would otherwise restart fresh after a
        # container/tmux restart dropped the window. See _auto_bind_to_directory.
        self.auto_resume_agents: bool = (
            os.getenv("CCBOT_AUTO_RESUME_AGENTS", "false").lower() == "true"
        )

        # --- Server-layout knobs (portability) -------------------------------
        # These default to this server's layout but every one is overridable
        # so the bot runs unchanged on a plain host. Friends without the
        # external `preview` CLI / Caddy fleet get graceful degradation: the
        # registry file simply won't exist and the helpers return nothing.

        # Base domain for preview-server URLs (`preview-<slug>.<domain>`),
        # surfaced in /status, the Live board, and worktree welcome messages.
        self.preview_domain: str = os.getenv("CCBOT_PREVIEW_DOMAIN", "").strip()
        # Path to the external `preview` CLI binary and its registry file
        # (written by that CLI). Consolidated here so worktrees.py and
        # preview.py share one source of truth.
        self.preview_bin: Path = Path(
            os.getenv(
                "CCBOT_PREVIEW_BIN", str(Path.home() / ".local" / "bin" / "preview")
            )
        ).expanduser()
        self.preview_registry_path: Path = Path(
            os.getenv(
                "CCBOT_PREVIEW_REGISTRY",
                str(Path.home() / ".local" / "state" / "preview" / "registry.json"),
            )
        ).expanduser()
        # Directory of Caddy app-host configs scanned by the Live board.
        # Missing dir → no app-hosts listed (already graceful).
        self.caddy_apps_dir: Path = Path(
            os.getenv("CCBOT_CADDY_APPS_DIR", "/etc/caddy/apps.d")
        ).expanduser()
        # Roots (relative to $HOME) for name-based topic auto-bind: a topic
        # named "foo" binds to ~/<root>/foo, first root wins. Default matches
        # this server's ~/projects + ~/agents layout.
        _roots_raw = os.getenv("CCBOT_TOPIC_DIR_ROOTS", "projects,agents")
        self.topic_dir_roots: tuple[str, ...] = tuple(
            r.strip() for r in _roots_raw.split(",") if r.strip()
        ) or ("projects", "agents")
        # rclone remotes shown in /status and driven by /mount|/umount|/remount.
        # Empty by default (rclone mounts are inherently host-specific — a
        # non-empty default would show a phantom "down" mount everywhere else);
        # set CCBOT_RCLONE_MOUNTS="name:~/path,…" on a host that has them.
        self.rclone_mounts: list[tuple[str, str]] = _parse_rclone_mounts(
            os.getenv("CCBOT_RCLONE_MOUNTS", ""),
            Path.home(),
        )

        # Docker-agent integration (optional). When DOCKER_AGENTS_ENABLED=true,
        # topic bindings of the form "docker:<name>" route to containers
        # instead of tmux windows. Default off — existing tmux-only deploys
        # are unaffected until this flag flips.
        self.docker_agents_enabled = (
            os.getenv("DOCKER_AGENTS_ENABLED", "").lower() == "true"
        )
        self.docker_agents: list[DockerAgentConfig] = self._load_docker_agents()

        # /inject endpoint (fire-and-forget task injection). Feature-
        # flagged on CCBOT_INJECT_TOKEN — empty → server doesn't start.
        self.inject: InjectConfig = _parse_inject_config(os.environ, Path.home())

        # Scrub sensitive vars from os.environ so child processes never inherit
        # them. Values are already captured in Config attributes above. A
        # deployment can name EXTRA sensitive vars (e.g. plugin webhook tokens)
        # via CCBOT_SENSITIVE_EXTRA — they're stripped here, BEFORE the tmux
        # server starts, so the core stays plugin-agnostic (a plugin's own late
        # scrub at import is only a fallback). `tmux_manager._scrub_session_env`
        # consults this same resolved set.
        self.sensitive_env_vars: set[str] = SENSITIVE_ENV_VARS | {
            v.strip()
            for v in os.getenv("CCBOT_SENSITIVE_EXTRA", "").split(",")
            if v.strip()
        }
        for var in self.sensitive_env_vars:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.claude_projects_path,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users

    def live_dashboard_target(self) -> tuple[int, int] | None:
        """Return (chat_id, thread_id) for the live-dashboard topic, or None.

        Both NOTIFICATIONS_CHAT_ID and LIVE_DASHBOARD_THREAD_ID must be set;
        otherwise the dashboard is disabled and ``browser_live_loop`` idles.
        """
        if self.notifications_chat_id is None or self.live_dashboard_thread_id is None:
            return None
        return self.notifications_chat_id, self.live_dashboard_thread_id

    def preview_host(self, slug: str) -> str:
        """Preview hostname for a slug: ``preview-<slug>.<preview_domain>``.

        Bare host (no scheme) — callers add ``https://`` where they need it.
        """
        return f"preview-{slug}.{self.preview_domain}"

    def _load_docker_agents(self) -> list[DockerAgentConfig]:
        """Read docker-agent configs from process environment.

        Thin wrapper around :func:`_parse_docker_agents` — keeps the
        env-reading side effect out of the pure parser so tests can
        feed a mapping directly.
        """
        return _parse_docker_agents(os.environ, Path.home())

    def get_docker_agent(self, name: str) -> DockerAgentConfig | None:
        """Look up a docker agent by its short name (case-insensitive).

        Topic names are often capitalized ("Assistant") while agent slugs are
        lowercase — match leniently and return the canonical agent.
        """
        lname = name.lower()
        for agent in self.docker_agents:
            if agent.name.lower() == lname:
                return agent
        return None

    def active_docker_agents(self) -> list[DockerAgentConfig]:
        """Docker agents that should be read/driven right now.

        Returns ``[]`` when the feature flag is off even if
        ``DOCKER_AGENTS`` lists entries — this is the single gate that
        session_monitor, session_manager and friends consult so the
        flag reliably disables every docker code path at once.
        """
        if not self.docker_agents_enabled:
            return []
        return list(self.docker_agents)


def _preload_dotenv() -> None:
    """Load `.env` files into `os.environ` at module import.

    Duplicates the load done inside `Config.__init__` (cheap, idempotent with
    `load_dotenv`'s default `override=False`) so plugin `.config` submodules,
    which we import BEFORE `Config()` runs (see below), can see their tokens
    in `os.environ`. Kept inside `Config.__init__` too so tests that construct
    a `Config()` directly (in a fresh process without our module-level pass)
    still get `.env` loading.
    """
    local_env = Path(".env")
    global_env = ccbot_dir() / ".env"
    if local_env.is_file():
        load_dotenv(local_env)
    if global_env.is_file():
        load_dotenv(global_env)


# Ordering matters:
#   1. Load `.env` into `os.environ`.
#   2. Give plugins a chance to capture their own tokens: each plugin's
#      `.config` submodule reads its tokens at import time and pops them
#      (plugin-owned scrub). Doing this BEFORE `Config()` guarantees the
#      plugin captures its token even when the deployment lists that token
#      in `CCBOT_SENSITIVE_EXTRA`.
#   3. `Config()` then reads the rest and scrubs its own list.
# After steps 2 + 3, `os.environ` is clean and any subsequently-spawned tmux
# server inherits no secrets. `plugins` doesn't import `config`, so no
# circular import risk.
_preload_dotenv()
_plugins.preload_configs()

config = Config()
