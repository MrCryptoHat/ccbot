# ccbot

ccbot — Telegram bot that bridges Telegram Forum topics to Claude Code sessions. A topic binds to either a host tmux window (legacy, default) or a docker container (optional, feature-flagged), each running one Claude Code instance.

Tech stack: Python, python-telegram-bot, tmux, uv.

## Common Commands

```bash
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/ccbot/             # Type check — MUST be 0 errors before committing
./scripts/restart.sh                  # Restart the ccbot service after code changes
ccbot hook --install                  # Auto-install Claude Code SessionStart hook
```

## Core Design Constraints

- **1 Topic = 1 Binding = 1 Session** — every internal route is keyed by the binding value. Two shapes share that string slot:
  - **Tmux** (default): `@<id>` (`@0`, `@12`) — a tmux window on the host. Window name = display name (`window_display_names`); the same directory may have several windows.
  - **Docker** (optional, `DOCKER_AGENTS_ENABLED=true`): `docker:<agent>` — Claude Code inside a container, driven via `docker exec <ctn> tmux send-keys/capture-pane -t claude`.
  - `session_manager.resolve_binding(user, thread) → ("tmux","@12") | ("docker","assistant") | None` is the transport-selection entry point; `send_to_window` branches internally so most call sites stay transport-agnostic. **Add new transport-agnostic operations as `SessionManager` wrappers** (`send_keys`/`capture_pane`/`kill_agent`/…), not as per-call-site `if docker:` branches.
- **Topic-only** — no non-topic mode, no `active_sessions`, no `/list`, no General-topic routing.
- **No truncation at the parse layer** — full content is preserved; the only split point is the send layer (`split_message`, 4096-char limit).
- **MarkdownV2 only** — user-facing sends go through `safe_reply`/`safe_edit`/`safe_send` (auto-fallback to plain text). Internal queue/UI code calls the bot API directly with its own fallback.
- **Hook-based session tracking** — Claude Code's `SessionStart` hook writes `session_map.json`; the monitor polls it to detect session changes. Only sessions present in a session_map source are monitored.
- **User-facing strings are bilingual (ru/en) through `i18n.py` — a new one goes through `tr("ns.key")` + a catalog entry, NEVER a hardcoded literal.** The `ru` value is written for a non-technical reader (plain words over jargon: «завершить сессию», not «Kill»); missing keys fall back to `ru`, so a gap degrades, never crashes. Language is one global setting (`/lang`, persisted; default `CCBOT_DEFAULT_LANG`). NOT translated (leave as-is): agent-directed text (voice protocol), logs, and Claude Code's own pane markers matched for parsing. The menu-button `filters.Regex` dispatcher matches **every** language's label via `i18n.all_variants` (built once at startup) and `BotCommand` descriptions come from `build_bot_commands()` — so a label change is a catalog edit only, no dispatcher/description drift.
- **`is_claude_working` reads only the status line, not the whole pane** — `esc to interrupt` legitimately appears in transcripts, tool output, and this codebase's own source, so a blind `.search()` over the pane marks every such agent permanently busy. It checks the spinner/status line above the chrome separator for the *active* form (`esc to interrupt` or a running `(Ns · …` counter) — not the lingering `✻ Cooked for 12s` done-marker.
- **`(send file: /path)` is the security perimeter against a compromised agent** — for docker bindings `session_manager.resolve_agent_file_path` accepts **only** `/workspace/*` (→ `agent.workspace_host_path`); any other container prefix (`/auth/...`, `/root/...`) or `..` traversal is rejected. Tmux bindings pass through verbatim (legacy host paths). Don't loosen this — bind-mounted secrets like `/auth/.credentials.json` sit one path away.
- **`/remount` cascades a `docker restart`** to every active docker agent — rclone remount invalidates the FUSE snapshots their bind-mounts hold. Live Claude sessions inside containers are interrupted; that's the intended trade-off vs. stale mounts.
- **Inbound media for docker bindings** is saved to `<agent.workspace_host_path>/.inbox/<ts>_<name>` and the marker sent to Claude uses the in-container path `/workspace/.inbox/...` — the host-side `ccbot/images/` and `ccbot/files/` dirs aren't bind-mounted into the container. Tmux bindings keep the legacy host-path behavior.
- **PTB HTTP timeouts are deliberately bumped** in `bot.py`'s `Application.builder()` (`read/write=30`, `connect=15`, `media_write=60`) over PTB's stock 5 s — on a high-latency link a fresh-start `send_photo` would time out client-side while Telegram had already committed the upload, producing a duplicate photo on the next poll tick. Don't restore the stock defaults.
- **`message_reaction` must stay in `main.py`'s `allowed_updates`** (added there iff `config.reaction_confirm_enabled`, default on) — Telegram delivers reaction updates only when the type is explicitly listed *and* the bot is a chat admin; drop it and 👍-to-confirm silently dies. The feature: a 👍 (`REACTION_CONFIRM_EMOJI`) on an agent-originated topic message → `handlers/reaction_confirm.py` confirms after a `REACTION_CONFIRM_DEBOUNCE_SEC` window (removing the reaction in that window cancels it) — Enter on a live interactive prompt, «да» typed to an idle agent, nothing if the agent is busy. Resolving the topic needs the `(chat_id, message_id) → (user, thread)` LRU in that module (the update carries no `message_thread_id`), populated by `message_queue._process_content_task` and `interactive_ui`.

- **`/inject` is a localhost RCE perimeter — never loosen `sanitize_inject_text`** — the optional fire-and-forget endpoint (`inject/`, feature-flagged on `CCBOT_INJECT_TOKEN`) types a task into an agent's pane *as a prompt*. It addresses agents by name; `session_manager.resolve_agent_binding(name)` maps it to a binding — `docker:<name>` for a docker agent, `@<id>` for a live host tmux window of that name — so it drives **both docker and host-tmux** agents. It binds a **unix socket** (`~/.ccbot/run/inject.sock`, `0660` under a `0700` dir), NOT TCP — unreachable from containers/other uids; that's the whole point (a local client reaches it as the bot's own user). `inject.core.sanitize_inject_text` is the security shield: it strips control/ESC bytes and **defuses a leading `!` or `/`** by prefixing a space — a leading `!` would drop Claude Code's TUI into bash command-mode = shell run *outside* the LLM (for a host-tmux agent that's RCE on the host, not just inside a container); a leading `/` would run a slash command (`/clear`, `/exit`) on the idle prompt instead of landing as text. Codes: 403 `forbidden_agent` (not allowlisted) vs 503 `not_running` (allowlisted but no docker agent and no live window — host-tmux agents exist only while running), 409 `busy` (`is_claude_working`/`is_interactive_ui`, never barge a live turn), 503 `unavailable` (resolved but pane/send failed). Allowlist `CCBOT_INJECT_AGENTS`; no token → server never starts.

## Code Conventions

- Every `.py` file starts with a module-level docstring: one-sentence summary on the first line, then core responsibilities — purpose clear within 10 lines. (The docstrings *are* the module inventory; don't re-list it in CLAUDE.md.)
- Telegram: prefer inline keyboards over reply keyboards; `edit_message_text` for in-place updates; callback data under 64 bytes; `answer_callback_query` for instant feedback.

## Configuration

- Config dir: `~/.ccbot/` by default, override with `CCBOT_DIR`.
- `.env` priority: local `.env` > config-dir `.env`.
- State files: `state.json` (thread bindings, window states, display names, read offsets, voice-mode topics, diff-mode topics, UI language, dashboard message ids), `session_map.json` (hook-generated), `monitor_state.json` (byte offsets).
- `ffmpeg` is required when `GEMINI_API_KEY` is set (Gemini TTS returns raw PCM @ 24 kHz piped through `ffmpeg` → OGG/Opus); not needed for ElevenLabs/OpenAI. `voice/startup.check_runtime_dependencies()` logs a WARNING at boot if it's missing.
- Docker agents (convention-over-config): `DOCKER_AGENTS_ENABLED=true` + `DOCKER_AGENTS=name1,name2`. Per-agent paths default to `~/agents/<name>` (workspace) and `~/.local/share/<name>/{claude-home,ipc,session-map.json}`, container `<name>`; override any via `DOCKER_AGENT_<NAME>_{CONTAINER,WORKSPACE,CLAUDE_HOME,IPC,SESSION_MAP,VNC_URL}`. Parser is the pure, unit-tested `config._parse_docker_agents(env, home)`. Each agent's in-container `SessionStart` hook writes its own `session_map.json` to the host via bind-mount, keyed `docker:<agent>` (no prefix to strip — the key *is* the binding value); the monitor and `session_manager.load_session_map` merge per-agent maps with the host map on every read. The container must run a tmux session named `claude` (hard-coded in `docker_driver`).
- `SENSITIVE_ENV_VARS` (e.g. `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`) are scrubbed from `os.environ` after `Config.__init__` so they can't leak into Claude Code subprocesses.
- **Portability (this is a public repo): deployment-specific integrations are default-off; never hardcode a host literal.** The live dashboards (`browser_live`/`live_board`) stay dormant unless `NOTIFICATIONS_CHAT_ID` + `LIVE_DASHBOARD_THREAD_ID` are set, and preview URLs are suppressed until `CCBOT_PREVIEW_DOMAIN` is. Every host-specific value (preview domain, Caddy apps dir, topic-scan roots, rclone mounts — full list in `config.py`) is a `Config` field with a `CCBOT_*` env override defaulting to this deployment's layout. Add a `Config` knob for any new one; don't bake it into a handler.
- **Optional plugins (`plugins.py`):** heavier deployment-specific integrations live as separate `ccbot.<name>` packages, loaded only when named in `CCBOT_PLUGINS` (comma-separated); the public tree ships none. A plugin optionally exposes `STRINGS` / `bot_commands()` / `register_handlers(app)` / `on_startup(app)` / `on_shutdown()` (all `getattr`-looked-up); a configured-but-absent package is logged and skipped, so the core always runs standalone. Full hook contract in the `plugins.py` docstring.

## Hook Configuration

Auto-install: `ccbot hook --install`. Or manually in `~/.claude/settings.json`:
```json
{ "hooks": { "SessionStart": [ { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] } ] } }
```

## Rule files

Source of truth for their topics — CLAUDE.md links here, doesn't restate them.

- `@.claude/rules/architecture.md` — system diagram, module map, agent-panel & menu keyboards, interactive-prompt rendering, post-slash handlers, key design decisions
- `@.claude/rules/topic-architecture.md` — topic ↔ window ↔ session mapping, binding lifecycle, name-based auto-bind (docker agent / `~/projects/` / `~/agents/`) on `forum_topic_created`
- `@.claude/rules/message-handling.md` — message queue, merging, rate limiting, voice mode, inline-keyboard tap-latency
- `@.claude/rules/worktree-agents.md` — parallel agents on one project via git worktree (🌳 panel button); two-tier teardown safety invariant, `reopen_forum_topic` deletion probe, `worktree_meta` state
