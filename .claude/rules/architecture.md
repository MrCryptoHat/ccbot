---
paths:
  - "src/ccbot/**"
  - "tests/**"
---

# System Architecture

The module inventory is the docstrings: every `src/ccbot/**/*.py` opens with a
one-line summary, so `ls` + first lines *is* the map. This file carries only what
reading the code won't tell you — decisions that look arbitrary until you know
why, and invariants that span two files and break silently.

Topic ↔ binding ↔ session: topic-architecture.md. Queue, rate limits, rendering:
message-handling.md. Per-runtime behaviour: runtimes.md. Worktree agents:
worktree-agents.md.

## Cross-file invariants — these break silently

- **`_action_home_tab` (`handlers/commands.py`) ↔ `_SES_TAB_PREFIXES` (`handlers/callbacks.py`)** — the agent panel returns to a button's *home tab* after the action and after a cancel. Moving a button between tabs means editing BOTH; miss one and the panel repaints on the wrong tab with no error. Cancel carries the tab in its payload (`cm:can:<tab>:<wid>`).
- **`codex_transcript_parser` / `grok_transcript_parser` must emit the SAME `ParsedEntry` as `transcript_parser`** — everything downstream (queue, rendering, voice, diff) is runtime-agnostic because of that. A parser that invents a field forks the pipeline.
- **`docker_driver` mirrors `tmux_manager`'s chunking and pacing** (200-char chunks, 0.5–1.5 s post-text delay, 1 s gap after `!`). The numbers are tuned against the TUI's input handling, not arbitrary — changing one transport without the other makes docker agents drop characters. The in-container tmux session name is hard-coded `claude`.
- **`session_monitor` holds no per-runtime branch** — it iterates `runtimes.monitored_runtimes()` and calls the interface. A new CLI is one `AgentRuntime` subclass, never an `if` here.
- **Busy/queue checks go through `session_manager.is_agent_working` / `agent_has_queued_input`**, never `terminal_parser.is_claude_working` directly — the wrappers dispatch on `WindowState.runtime`, and codex/grok have no `─` chrome to anchor on.

## Agent panel — decisions that look arbitrary

Layout and colour came from live use and a design review; the code shows *what*, not *why*.

- Active tab is marked `▸ Label` — the icon is *replaced* by the pointer. A suffix marker was tried first and overflowed the 3-buttons-per-row width, so Telegram clipped it.
- The nav row keeps `⏎` next to `↓` — a design review proposed isolating them and it was reverted; the single row is the user-confirmed layout.
- «Actions» is deliberately **two** rows so the pane photo stays on screen. 🌳 and ➕ get a full-width row each: paired, Telegram clipped «➕ One more agent» on a phone.
- Colour grammar (`KeyboardButtonStyle`): red in the always-visible grid = only 🗑 delete-agent. Clear and End are **neutral in the grid by user preference** — their red confirm step is where the loss warning lives. Blue = the primary tap. Green = restart/new confirms only, and green **never** sits in the grid (red-adjacent green reads as blotchy). `_DESTRUCTIVE_CONFIRMS` / `_FORWARD_CONFIRMS` pick the confirm colour.
- Restart and New are not destructive, but ride the same confirm keyboard: the confirm-button label is the ONLY place the «same dialog» vs «from scratch» difference is spelled out.
- Buttons are runtime-capability-gated (`session_manager.agent_supports` → `AgentRuntime.panel_actions`), slashes resolve via `AgentRuntime.panel_slash`. Survivors repack into rows so no gaps appear. No `if codex:` in the builder — a third agent is a capability set plus optional slash/renderer entries.
- 🌳 appears only when `session_manager.can_offer_worktree(wid)` — a plain non-repo folder hides it rather than erroring on tap.
- Tab switch = `editMessageReplyMarkup` only (no upload); refresh and post-action repaints = `editMessageMedia`.

## Interactive prompts — why text is surfaced at all

Normally the user just reads the screenshot. Two widgets are exceptions, and both exist because **Claude Code holds the whole turn out of JSONL until the user answers** — pre-answer, the pane (or the plan file) is the only source.

- **AskUserQuestion** → prose above the widget + the question text, posted once per appearance (`_auq_text_sent`). Options stay on the photo; the question is surfaced because it often runs past the crop. After the answer the turn lands in JSONL and two de-dups fire from `bot.handle_new_message`: `consume_pending_prose_upgrade` (re-delivers clean prose, running the same `render_tables_for_chat` as the normal path, so a table in that prose still becomes its own photo) and `consume_pending_ask_tool_use` (suppresses the would-be `**AskUserQuestion**(…)` message).
- **ExitPlanMode** → surfaced from the plan **FILE**, not the pane. `plan_parser` takes **only the basename** from the widget and resolves it against `session_manager.plans_dir_for_binding`: agent-controlled pane text must never supply a usable path. Post-answer the JSONL plan entry is skipped via `consume_pending_plan_text`.
- The ExitPlanMode UI pattern anchors on `ctrl[-+]g to edit`. **A stale anchor degrades silently** — the widget falls through to the generic permission-prompt match, the photo still works, and only plan surfacing quietly stops.
- Pane-lifted text is home-relativized (`/home/<user>/…` → `~/…`) and dash-only decoration rows are dropped. Agent-authored JSONL content is **never** rewritten.
- Everything here fails open: parse miss, unreadable plan file, nothing parseable → photo only.

## Media — codex's composer

Inbound images for a `native_image_input` runtime go through `session_manager.send_composer_image`, and the sequence matters: type path → poll for `composer_image_token` → caption → **one** Enter, all under a single `send_lock`. **In codex Enter SUBMITS** — pressing it to "attach" sends the image alone and splits the caption into a second turn. Claude's runtimes instead get the `(image attached: <path>)` text marker and read the file themselves. After a successful attach the bot echoes the image back to the chat (`media.image_echo`) — parity with Claude, whose Read tool_result surfaces it.

## Status polling — what the 1 s loop owns

Interactive-UI detection, dead-window cleanup (30 s grace; liveness is runtime-aware via `AgentRuntime.pane_alive_commands`), the orphan-window janitor (90 s grace — reaps tmux windows no binding points at, which is what stops `ccbot-2`, `ccbot-3`… piling up on one directory), the typing heartbeat, the CLI self-update canary, and the backstop topic-existence probe. Why `reopen_forum_topic` is the only probe that works: topic-architecture.md. It publishes **no** chat status line — see message-handling.md «What reaches the chat».

`task_pin`'s idle check runs on the **pre-send** pane: after the send the agent is busy with this very message. Pinning needs *Pin messages* + *Delete messages* admin rights and fails soft to a WARNING without them.

## Extension points

- **Post-slash renderers** (`callbacks._POST_SLASH_HANDLERS`) — for TUI-only slash commands that leave no JSONL trace (`/context`, `/status`). The hook captures the pane, parses it, posts a message before the default photo refresh; a parse miss silently falls back to the photo. Another data renderer = one parser module + one dict entry.
- **Plugins** (`plugins.py`) — the rclone mount stack (`drive`) and preview fleet + live dashboards (`fleet`) live in ccbot-plugins. Core keeps only the seams they consume: `notifications_chat_id`, `preview_bin`/`preview_registry_path`, `session.live_dashboard_message_ids`, per-agent `vnc_url`, and the hooks `status_sections`/`status_buttons`/`callback_dispatch`. Contract in the module docstring.

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
- **Restart is the one transport-specific handler** — `_restart_agent(window_id, *, fresh)` keeps a local branch (tmux: `/exit` + relaunch `claude` ±`--resume`; docker: `kill_session` + `start_session(resume_session_id=…)`) and is shared by `_handle_cmd_restart` (fresh=False, resumes current session) and `_handle_cmd_fresh` (fresh=True, brand-new session_id; old session JSONL untouched → still in `/resume` picker). The restart dance is genuinely different per transport. **When there is no window left to restart** (agent crashed, dead-window reaper took it with the binding), both 🔄 and `/restart` fall through to `handlers/agent_restart.revive_topic_agent`, which rebuilds it from the topic's memory — folder, runtime, and the newest session of that folder (or the dead window_state's own id while it survives) as a `--resume`. That is the ONLY path that creates a window without a picker tap, and it is deliberate: the user asked for this exact agent back. Everything else routes through `SessionManager` wrappers. (`claude --resume` semantics / `window_state` override — see topic-architecture.md.)
- **MarkdownV2 with fallback / no parse-layer truncation** — see CLAUDE.md; `transcript_parser` preserves full content, `split_message` is the only split point.
