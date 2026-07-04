# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Bot Orchestrator (bot.py)                         │
│  - Handler registration + lifecycle (post_init, post_shutdown)     │
│  - Text handler: topic routing, directory browser, session binding │
│  - handle_new_message: streaming response delivery                 │
│  Delegates to handlers/: commands, callbacks, media, interactive_ui │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD → MarkdownV2     │  split_message (4096 limit)                 │
│  + expandable quotes │                                             │
├──────────────────────┴──────────────────────────────────────────────┤
│  terminal_parser.py                                                 │
│  - Detect interactive UIs (AskUserQuestion, ExitPlanMode, etc.)    │
│  - parse_status_line / is_claude_working (status line only)        │
└──────────┬──────────────────────────────────────────────────────────┘
           │                              │
           │ Notify (NewMessage callback) │ Send (transport by binding type)
           │                              │
┌──────────┴──────────────┐    ┌──────────┴──────────────────────┐
│  SessionMonitor         │    │  session.send_to_window         │
│  (session_monitor.py)   │    │  branches on binding value:     │
│  - Poll JSONL every 2s  │    │    "@<id>" → TmuxManager        │
│  - Multi-root scan      │    │    "docker:<agent>" → DockerDrvr│
│    (host + each active  │    └────────┬──────────────┬─────────┘
│    docker agent)        │             │              │
│  - Merge session_maps   │             ▼              ▼
│    (main + per-agent)   │      ┌──────────────┐  ┌──────────────────┐
│  - mtime + byte-offset  │      │ TmuxManager  │  │ DockerDriver     │
│    incremental reads    │      │ (tmux_       │  │ docker exec      │
│  - Track pending tools  │      │  manager.py) │  │   <ctn> tmux ... │
└──────────┬──────────────┘      │ libtmux wrap │  │   -t claude      │
           │                     └──────┬───────┘  └────────┬─────────┘
           ▼                            │                   │
┌────────────────────────┐              ▼                   ▼
│  TranscriptParser      │      ┌──────────────┐     ┌──────────────────┐
│  (transcript_parser.py)│      │ Tmux windows │     │ Docker container │
│  - Parse JSONL entries │      │ on host      │     │ tmux sess=claude │
│  - Pair tool_use ↔     │      └──────┬───────┘     │ Claude Code      │
│    tool_result         │             │             │ + Chromium       │
│  - Expandable quotes   │             ▼             │ + live-daemon    │
│  - Extract history     │      ┌──────────────┐     │                  │
└────────────────────────┘      │ SessionStart │     │ SessionStart hook│
                                │ hook (host): │     │ inside ctn →     │
┌────────────────────────┐      │ hook.py →    │     │ <agent>.session_ │
│  SessionManager        │      │ ~/.ccbot/    │     │ map_path (bind-  │
│  (session.py)          │      │ session_map  │     │ mounted host file)│
│  - Binding types       │      │ .json        │     └────────┬─────────┘
│    (tmux / docker)     │      └──────┬───────┘              │
│  - Thread bindings     │             │                      │
│  - resolve_binding     │             └──────────┬───────────┘
│  - transport-agnostic  │                        ▼
│    wrappers (send_*,   │      ┌──────────────────────────────────┐
│    capture_pane, …)    │      │ Merged session_map view           │
│  - (send file:)        │      │ main keyed "<tmux>:@<id>" plus    │
│    /workspace whitelist│      │ per-agent keyed "docker:<agent>"  │
│  - Off-loop state save │      │ → unified binding-value dict      │
└────────────────────────┘      └──────────────────┬───────────────┘
┌────────────────────────┐                         ▼
│  MonitorState          │         ┌──────────────────────────────────┐
│  (byte offset per file,│         │ JSONL roots: main ~/.claude/      │
│   truncation detect)   │         │ projects/ + each agent's          │
└────────────────────────┘         │ claude_home/projects/ (bind-mount)│
                                    └──────────────────────────────────┘
```

## Module map

Every `.py` carries a module docstring — that's the canonical inventory. This is just the orientation map; load-bearing modules only.

Core:
- `bot.py` — handler registration + lifecycle, text handler (topic routing, directory browser, binding), `handle_new_message` streaming delivery, AskUserQuestion post-answer de-dup (`consume_pending_prose_upgrade` re-delivers the surfaced text from clean JSONL — in-place edit of the lead chunk + any embedded table/box-art/long-code sent out-of-band; `consume_pending_ask_tool_use` skips the would-be `**AskUserQuestion**(…)` message — see below), `_bump_read_offset_to_eof`.
- `session.py` (`SessionManager`) — thread bindings, `resolve_binding`, transport-agnostic wrappers (`send_to_window`/`send_keys`/`capture_pane`/`kill_agent`/…), `(send file:)` path whitelist (`resolve_agent_file_path`), message history, off-loop state persistence (`schedule_async_json_write`), dashboard message-id bookkeeping, `worktree_meta` (keyed by thread_id) + reconcile.
- `worktrees.py` — pure git/disk core for worktree agents (ru-translit `slugify`, `dedup_slug`, `SEED_*` seed tables, `parse_porcelain` dirty-guard, `decide_delete_safety`, async git helpers, `WorktreeMeta`). See worktree-agents.md.
- `session_monitor.py` — polls JSONL every 2 s across all roots (host `~/.claude/projects` + each active docker agent's `claude_home/projects`), merges session_map sources, mtime cache, byte-offset incremental reads, tracks pending tool_use ↔ tool_result.
- `transcript_parser.py` — parse JSONL, pair tool_use/tool_result, expandable quotes for thinking, history extraction, `_ask_question_prose_indices` (cross-message flag for the AskUserQuestion surface-upgrade path; tests in `test_transcript_parser.py::TestPrecedesInteractivePrompt`).
- `tmux_manager.py` / `docker_driver.py` — the two transports. `docker_driver` mirrors `tmux_manager`'s chunking/pacing (200-char chunks, 0.5–1.5 s post-text delay, 1 s gap after `!`) over `docker exec <ctn> tmux ... -t claude`; container session name hard-coded `claude`.
- `terminal_parser.py` — detect interactive UIs (AskUserQuestion, ExitPlanMode, …), `parse_status_line`, `is_claude_working` (status-line only — see CLAUDE.md gotcha), `has_queued_messages` (input-queue hint → reaction-ack timing, message-handling.md).
- `screenshot.py` — pane text → PNG (ANSI colour, font fallback, JPEG-survivable tuning — detail in message-handling.md).
- `transcribe.py` — voice→text (Deepgram Nova-3 primary, OpenAI fallback).
- `rate_limiter.py` — `CcbotRateLimiter` (AIORateLimiter subclass) + `stream_context()` / `background_context()` ContextVars (three traffic classes: stream → parent's group bucket on the real chat_id = the per-chat governor; interactive → bypass; background → shared slow lane, no retry loop) — detail in message-handling.md.
- `config.py` / `utils.py` / `main.py` / `hook.py` — config + pure `_parse_docker_agents(env, home)`; `ccbot_dir`/`atomic_write_json`/`schedule_async_json_write`; CLI entry; the `SessionStart` hook.
- `i18n.py` — bilingual UI-string catalog (`STRINGS`) + `tr()`/`set_language()`/`all_variants()`. Global language (module-level, single-user), synced from `session.ui_language` on load and `/lang`. Scope + non-obvious rules in CLAUDE.md «Core Design Constraints».
- `plugins.py` — optional-integration registry. Lazily imports the `ccbot.<name>` packages named in `CCBOT_PLUGINS`, tolerating absence; each may expose `STRINGS` / `bot_commands()` / `register_handlers(app)` / `on_startup(app)` / `on_shutdown()`, looked up by `bot.py` + `commands.py`. Public build ships no plugins; the contract lives in the module docstring.

Handlers (`handlers/`):
- `commands.py` — `/status`, `/commands` (alias `/screenshot`), `/esc`, `/kill`, `/restart`, `/voice`, `/menu`, `/bind`; `topic_created_handler` + `_try_auto_bind_topic` (auto-bind by topic name on `forum_topic_created` → docker agent OR `~/projects/<name>` / `~/agents/<name>`; only fires for topics created while ccbot is online — renamed/legacy topics fall back to the dir-browser flow, see `topic-architecture.md` for the deliberate-non-fix); `MENU_KEYBOARD` (persistent 2-button ReplyKeyboard 🖥️ Сервер / 👾 Агент) + `menu_button_dispatcher` (taps arrive as plain text, routed to the underlying slash commands); `_build_commands_keyboard(tab="nav"|"act"|"ses")` — the unified agent-panel keyboard (pane photo + three tabs, active tab marked «▸ Label» (icon swapped for a pointer — a suffix marker overflowed the 3-per-row width and Telegram clipped it): «Клавиши» = raw TUI key presses `⎋ ^C ^B` (`^B` = Claude Code "send the running task to the background") / `/ ← → ↑ ↓ ⏎` + 🧽 Стереть ввод (an input-line op, so it lives with the keys; the single nav row is the user-confirmed layout — a design-review attempt to isolate ⏎ from ↓ was reverted); «Действия» = the everyday on-the-fly pairs 🎯 Режим (Shift+Tab cycle: normal → auto-accept → plan) / ⚡ Усилие, 🗜 Сжать / 🧹 Clear — deliberately two rows so the pane photo stays on screen; «Сессия» = session config & diagnostics 🤖 Model / 📊 Context / 🔌 MCP + lifecycle ⏪ Resume / 🆕 New / 🔄 Restart / ⏹ End / 🌳 (Restart relaunches Claude with `--resume <current>`; New relaunches without `--resume` so a fresh session_id starts and the old one stays in the /resume picker); common 📸 Обновить). Post-action repaints and confirm-cancel return to the action's **home tab** (`_action_home_tab` in commands.py + `_SES_TAB_PREFIXES` in callbacks.py — keep them in sync when moving a button between tabs; cancel carries the tab in its payload `cm:can:<tab>:<wid>`). Tab switch = `editMessageReplyMarkup`-only (no upload); refresh/post-action repaints = `editMessageMedia`. Destructive actions (Clear/Compact/End) route through a two-step `CB_CMD_CONFIRM` keyboard; Restart/New ride the same confirm — not destructive, but the confirm-button label is the only place the «same dialog» vs «from scratch» difference is spelled out. Button-colour grammar (`KeyboardButtonStyle`, from the design panel): red in the grid = only 🗑 delete-agent (shares 🌳's row on worktree topics to keep the tab inside the row budget); Clear and End are neutral in the grid by user preference — their red confirm step carries the loss warning (destructive confirms stay red), blue = the primary tap (📸 Обновить + the compact confirm), green = restart/new confirms only (go-forward, recoverable) — green never sits in the always-visible grid (a red-adjacent green reads as blotchy); everything else neutral. `_DESTRUCTIVE_CONFIRMS` / `_FORWARD_CONFIRMS` select the confirm colour. The first panel send reuses a cached `pane_cache` `file_id` when the pane matches a recent upload (agent idle → zero render, zero upload), else renders fresh and caches it.
- `callbacks.py` — inline-keyboard dispatch (nav key presses via `CB_KEYS_PREFIX = kb:<key_id>:<wid>`, agent-panel actions, pickers, confirmations). `_POST_SLASH_HANDLERS: dict[prefix → async fn]` — for TUI-only slash commands that leave no JSONL trace (`/context` today, `/cost` plausibly next): the registered hook captures the pane (with scrollback), parses it, and posts a chat message *after* sending the slash, *before* the default photo refresh. Parse miss → silent skip; the photo refresh is the fallback. Adding another data-renderer = one parser module + one dict entry.
- `interactive_ui.py` — unified interactive-prompt handler: pane photo + `↑ ↓ ⏎ Esc 🔄` keyboard, no parsed buttons (the user reads the screenshot — covers AskUserQuestion, ExitPlanMode, permission prompts, RestoreCheckpoint, /model picker, …). **AskUserQuestion is the one exception that also gets one text message — the agent's preceding prose + the question text** (Claude Code holds the whole turn — prose + question + options — out of JSONL until the user answers, so pre-answer the pane is the only source; the answer options stay on the screenshot, not repeated as text, but the question itself often runs past the screenshot crop so it's surfaced). `_surface_ask_question_text` captures the pane with scrollback → `askquestion_parser.parse_ask_question` → ccbot posts `[prose + question] → [photo]`, once per widget appearance (`_auq_text_sent`, cleared in `clear_interactive_msg`). `(message_id, question)` goes into `_pending_auq[session_id]`; after the answer, the held turn lands in JSONL and two de-dups fire from `handle_new_message`: the assistant prose text block → `consume_pending_prose_upgrade` re-delivers `<clean markdown prose> + <question>` via `_deliver_upgraded_prose` (it runs the **same** `render_tables_for_chat` the normal send path uses — a table / wide box-art / long-code block in the prose above the question is extracted to its own photo / document, not inlined; the lead text chunk edits the surfaced message in place, the rest is sent as new messages in source order; >4096-char prose splits instead of bailing — the old single in-place edit left the box-art pane capture); the AskUserQuestion tool_use → `consume_pending_ask_tool_use` skips the would-be `**AskUserQuestion**(…)` message and evicts the entry. Fail-open everywhere: parse miss / nothing parseable → photo only, post-answer copies delivered normally.
- `ask_question_router.py` / `askquestion_parser.py` — routing + pure parser for Claude Code's AskUserQuestion picker in the pane: extracts the prose block above the widget border (`""` when a tool result / user prompt / meta bullet is there instead) and the question text (both surfaced as one Telegram message before the answer), plus option labels (dropping the always-present `Type something.` / `Chat about this`, used only for the "is this a real widget" check and the log line — options aren't surfaced). `test_askquestion_parser.py` pins the v2.1.x layout.
- `context_parser.py` — regex parser for `/context` TUI output: slices from the *last* `Context Usage` marker (scrollback may carry several past invocations), extracts model header / token total / category breakdown / Memory files; `format_context_message` renders a `/status`-style tree (├/└, `📊 Context · 27%`). Fail-open: parse miss → photo fallback.
- `message_queue.py` / `message_sender.py` / `response_builder.py` / `status_polling.py` — per-topic queue + worker (keyed `(user_id, thread_id_or_0)`; merge + `stream_context()` wrapping; `_process_content_task` short-circuits `tool_use`/`tool_result` to images-only — chat never sees tool plumbing); `safe_*`/`send_voice`/`is_topic_gone_error` (re-raised by `send_with_fallback`/`safe_send`/`send_photo`/`_try_edit_with_fallback` so the worker can `cleanup.purge_deleted_topic` — the *primary* deleted-topic signal); pagination; 1 s poll that does **only** interactive-UI detection + dead-window cleanup (30 s grace, claude crashed) + orphan-window janitor (90 s grace — reaps tmux windows with no thread binding, e.g. topic deleted while ccbot was offline, or `bind_thread` overwrote a binding without killing the old window — prevents `ccbot`, `ccbot-2`, `ccbot-3` piling up on one directory) + typing-heartbeat + backstop topic-existence probe (`reopen_forum_topic` — `unpin`/`send_chat_action` return OK on a deleted topic, only `reopen` raises `Topic_id_invalid`; live open topic → no-op `Topic_not_modified`; one bound topic per `TOPIC_CHECK_INTERVAL` round-robin + all worktree topics per `WT_TOPIC_CHECK_INTERVAL`, in `background_context()` — covers idle topics; active ones caught immediately by the worker above). No chat-side status spinner — see message-handling.md "What reaches the chat".
- `media.py` — photo/document/voice handlers; `_inbound_save_path` routes docker-binding media to `<workspace>/.inbox/<ts>_<name>` (in-container path in the marker), tmux to legacy `ccbot/images|files/` host paths.
- *(moved to plugins)* the rclone mount stack (`drive`) and the preview fleet + live dashboards (`fleet`: `preview.py`, `live_board.py`, `browser_live.py`) live in ccbot-plugins — see that repo. Core keeps the seams they consume: `notifications_chat_id`, `preview_bin`/`preview_registry_path` (worktree teardown), `session.live_dashboard_message_ids`, per-agent `vnc_url`, and the plugin hooks `status_sections`/`status_buttons`/`callback_dispatch` (contract in `plugins.py`).
- `pane_cache.py` — tap-latency helpers (detail in message-handling.md): `message_id→pane_hash` dict (hash-skip), `pane_hash→file_id` LRU `FILE_ID_CACHE_MAX=256` (zero-byte photo swaps via `InputMediaPhoto(media=file_id)`), `wait_pane_change(window, prior_hash)` (adaptive poll replacing fixed `asyncio.sleep(0.5)` after a key press). Consumed by `callbacks._handle_screenshot_{refresh,keys}`, `_handle_interactive_key`, `_cmd_refresh_photo`, `interactive_ui._handle_interactive_ui_locked`, `commands.commands_command` (first agent-panel send).
- `reaction_emit.py` — the inverse of `reaction_confirm`: bot *emits* a 👀 acking a user message entered the agent's context (**default on**, `CCBOT_REACTION_ACK`; runtime toggle `/react`; armed in `deliver_user_text`, fired by the poll on queue-drain). Full rationale in message-handling.md.
- `diff_view.py` — opt-in `/diff` edit-diff screenshots: crops Claude Code's native `● Update/Write` diff block out of a scrollback `capture_pane` (`extract_diff_blocks`) and screenshots it via `screenshot.text_to_image` — captured, not reconstructed, so style/word-highlight/wrapping stay native. Per-edit, hash-dedup'd per window; triggered from `bot.handle_new_message` on edit `tool_use`. Full rationale in message-handling.md.
- `task_pin.py` — task pinning, ON by default in every topic (`CCBOT_PIN_DEFAULT`; `/pin` opts a topic out, overrides in `session.pin_topic_overrides`): a user message ≥ `CCBOT_PIN_MIN_CHARS` (default 200) delivered to an **idle** agent pins in its topic, so the pinned list reads as the task history. The idle check runs on the PRE-send pane (`should_pin_task` before `deliver_user_text`; post-send the agent is working on this very message), and only `"sent"` pins — `"routed"` is a widget answer. Voice tasks pin the 🎤 transcript reply, not the audio bubble. The bot's own «pinned a message» service lines are deleted (`pinned_service_message_handler`; user's manual pins keep theirs). Needs the *Pin messages* + *Delete messages* admin rights; both fail soft to a WARNING.
- `reaction_confirm.py` — 👍-to-confirm: `MessageReactionHandler` → on `REACTION_CONFIRM_EMOJI` added to a tracked topic message, after a debounce (removable), `decide_confirm_action(pane)` picks Enter (live interactive prompt) / type «да» (idle agent) / skip (busy). `(chat_id, message_id) → (user, thread)` LRU index (no `message_thread_id` in the update) — `note_topic_message` is called from `message_queue._process_content_task` and `interactive_ui`. Feature-flagged `config.reaction_confirm_enabled` (default on; also gates the `message_reaction` entry in `main.py`'s `allowed_updates`).
- `worktrees.py` (handlers) — worktree-agent orchestration: transactional provision, close-topic guard (two-tier teardown), `handle_deleted_worktree_topic` (headless clean-only), 🌳/🗑 + `wt:*` callbacks, task-name capture. See worktree-agents.md.
- `directory_browser.py` / `history.py` / `cleanup.py` / `callback_data.py` / `__init__.py` — directory + session-picker UI for new tmux topics; history pagination; topic-state cleanup on close/delete (`clear_topic_state`; `purge_deleted_topic` = full teardown on `Topic_id_invalid` — kill tmux window + unbind + `clear_topic_state`, called from the queue worker and the backstop probe); callback-data constants; shared `get_thread_id`/`is_user_allowed`.

Voice (`voice/`): `providers.py` (Gemini → ElevenLabs → OpenAI; each `name`/`available()`/`synthesize()`/`tag_catalog()`; Gemini PCM → ffmpeg → OGG; `TTS_PROVIDER=<name>` pins one provider with **no fallback** so the voice stays consistent; `_resolve_chain()` is the single resolver both `synthesize_speech` and `get_active_provider` consult), `hints.py` (`build_on_directive()` per active provider's tag catalog, `OFF_DIRECTIVE`, `strip_output_tags`, `split_voice_segments(text) → ordered [("voice"|"chat", str)]`), `safety.py`, `startup.py` (ffmpeg check). Detail in message-handling.md.

inject (`inject/`): `core.py` (pure `sanitize_inject_text` — the leading-`!`/`/` RCE shield + control/ESC stripping, the security perimeter), `server.py` (aiohttp on a **unix socket** `~/.ccbot/run/inject.sock` `0660`, `X-Inject-Token` via `secrets.compare_digest`, allowlist + reject-if-busy gating, push via `send_to_window`). Resolves the agent name to a binding via `session_manager.resolve_agent_binding` (docker → `docker:<name>`, host tmux → `@<id>`, neither → 503 `not_running`), so it drives both docker and host-tmux agents. Feature-flagged on `CCBOT_INJECT_TOKEN`; serves a fire-and-forget task-injection path. Lifecycle (`start_server`/`runner.cleanup`) in `bot.py`. Key invariant in CLAUDE.md «Core Design Constraints».

## State files

- `~/.ccbot/state.json` — thread bindings, window states, display names, read offsets, `voice_mode_topics`, `diff_mode_topics`, `live_dashboard_message_ids`.
- `~/.ccbot/session_map.json` — host-side hook output, keyed `<tmux_session>:<window_id>`.
- `~/.ccbot/monitor_state.json` — poll byte offset per JSONL file.
- Per docker agent (paths from `DOCKER_AGENT_<NAME>_*`): `workspace_host_path` (= `/workspace` in-container), `claude_home/projects` (extra JSONL root the monitor scans), `ipc_dir/{browser-live.json,current.png}` (daemon-owned), `session_map_path` (per-agent hook output keyed `docker:<agent>`, merged with the host map on read).

## Key Design Decisions

- **Topic-centric, binding value is the universal route key** — `thread_bindings[user][thread]` holds `@<id>` or `docker:<agent>`; everything that used to key on "window_id" keys on this. No centralized session list — topics *are* the list.
- **Feature flag `DOCKER_AGENTS_ENABLED`** — off ⇒ `config.active_docker_agents() == []` and every docker path is a no-op; tmux-only deployments see zero change.
- **Hook-based tracking, two sources** — host hook → `session_map.json` keyed `<tmux>:<wid>` (prefix stripped on read); each docker agent's in-container hook → its own map keyed `docker:<agent>` (key *is* the binding value); merged on read.
- **Startup re-resolution** — tmux window IDs reset on tmux-server restart; `resolve_stale_ids()` re-maps by persisted display name; old name-keyed `state.json` is auto-migrated; docker bindings are kept verbatim (no tmux window to re-resolve — otherwise a restart would silently drop the topic↔container link). (Topic lifecycle / orphan-window reaping — see topic-architecture.md.)
- **Restart is the one transport-specific handler** — `_restart_agent(window_id, *, fresh)` keeps a local branch (tmux: `/exit` + relaunch `claude` ±`--resume`; docker: `kill_session` + `start_session(resume_session_id=…)`) and is shared by `_handle_cmd_restart` (fresh=False, resumes current session) and `_handle_cmd_fresh` (fresh=True, brand-new session_id; old session JSONL untouched → still in `/resume` picker). The restart dance is genuinely different per transport. Everything else routes through `SessionManager` wrappers. (`claude --resume` semantics / `window_state` override — see topic-architecture.md.)
- **MarkdownV2 with fallback / no parse-layer truncation** — see CLAUDE.md; `transcript_parser` preserves full content, `split_message` is the only split point.
