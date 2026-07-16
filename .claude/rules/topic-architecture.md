# Topic-Only Architecture

The bot operates exclusively in Telegram Forum (topics) mode. There is **no** `active_sessions` mapping, **no** `/list` command, **no** General topic routing, and **no** backward-compatibility logic for older non-topic modes. Every code path assumes named topics.

## 1 Topic = 1 Binding = 1 Session

```
┌─────────────┐      ┌─────────────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │ Binding value       │ ───▶ │ Session ID  │
│  (Telegram) │      │  "@<id>"   (tmux)   │      │  (Claude)   │
│             │      │  "docker:<agent>"   │      │             │
└─────────────┘      └─────────────────────┘      └─────────────┘
     thread_bindings     session_map.json (host tmux side)
     (state.json)        + <agent>.session_map_path (per docker agent)
```

Two binding shapes share the same string slot; `session_manager._is_docker_binding(value)` distinguishes them, and `resolve_binding(user, thread) → ("tmux"|"docker", target) | None` is the typed API. Tmux window IDs (`@0`, `@12`) are unique within a tmux server session; `window_display_names` holds display names for them. Docker binding values embed the agent name directly — no separate display map needed (the agent's configured name is the display name).

## Mapping 1: Topic → Binding value (thread_bindings)

```python
# session.py: SessionManager
thread_bindings: dict[int, dict[int, str]]  # user_id → {thread_id → binding_value}
window_display_names: dict[str, str]        # binding_value → display name
```

- Storage: memory + `state.json`
- Written when:
  - Tmux: user creates a session via the directory browser.
  - Docker: user runs `/bind <agent>` in the topic (see `handlers/commands.py::bind_command`), **or** the topic was freshly created with a name matching an active docker agent — `topic_created_handler` auto-binds off the `forum_topic_created` service message.
- Purpose: route user messages to the correct transport (tmux window or docker container).

## Mapping 2: Binding → Session (session_map sources)

Two sources, merged on read into a single `binding_value → session_id` dict:

**Host side — `~/.ccbot/session_map.json`** (tmux). Prefix stripped on read.
```json
{
  "ccbot:@0": {"session_id": "uuid-xxx", "cwd": "/path/to/project", "window_name": "project"},
  "ccbot:@5": {"session_id": "uuid-yyy", "cwd": "/path/to/project", "window_name": "project-2"}
}
```

**Per-docker-agent — `<agent>.session_map_path`** (written by the in-container hook, bind-mounted to host). Key is already the binding value:
```json
{
  "docker:assistant": {"session_id": "uuid-zzz", "cwd": "/workspace", "window_name": "assistant"}
}
```

- Written when: Claude Code's `SessionStart` hook fires (host or in-container respectively).
- Property: one binding maps to one session; session_id changes after `/clear`.
- Purpose: SessionMonitor uses the merged view to decide which sessions to watch.

## Message Flows

**Outbound** (user → Claude):
```
User sends "hello" in topic (thread_id=42)
  → thread_bindings[user_id][42] → "@0"  OR  "docker:assistant"
  → session_manager.send_to_window(binding_value, "hello")
      ├─ "@..."   → tmux_manager.send_keys
      └─ "docker:..." → docker_driver.send_keys (docker exec tmux -t claude …)
```

**Inbound** (Claude → user):
```
SessionMonitor iterates ALL project roots (host + each docker agent's
claude_home/projects) → reads JSONL → finds new session_ids.
  → find_users_for_session(session_id) walks thread_bindings, uses the
    binding's own projects_root to resolve the JSONL file.
  → Deliver message to each matching (user, thread).
```

**Name-based auto-bind (`_try_auto_bind_topic` in `handlers/commands.py`)**: fired only from `topic_created_handler` (i.e. off the `forum_topic_created` service message, where the name is right there in the event). Order of matches:
1. **Docker agent** — name hits `DOCKER_AGENTS` (`config.get_docker_agent`) → bind `docker:<name>` immediately; the container *is* the "directory" (no tmux window involved). Wins over the tmux-directory branch when names collide (e.g. `~/agents/assistant` exists *and* `assistant` is a docker agent).
2. **Tmux directory** — `_find_matching_dir_for_topic` checks `~/projects/<name>` then `~/agents/<name>` (projects wins on duplicate; underscore-prefixed and dotfile names are skipped — infra/hidden dirs). The bind is **runtime-aware**: `_auto_bind_to_directory` resolves the topic's remembered runtime (`thread_runtime_memory`; falls back to `runtimes.default_runtime()` for never-bound topics, and degrades to the default if the remembered CLI is uninstalled) and uses **that** runtime's `list_sessions` / `create_window(runtime=…)` — a codex topic must rebind as codex, not silently as Claude. On hit: if sessions already exist in that dir, surface the session picker right away (state shape reuses what `_handle_session_{select,cancel}` already consume, so no new callback wiring) — **unless `CCBOT_AUTO_RESUME_AGENTS` is set, which silently resumes the newest session instead of the picker** (for non-technical users in agent topics whose session would otherwise restart fresh after a container/tmux restart dropped the window); otherwise create a fresh tmux window and bind silently (hookless runtimes get the same-cwd guard + `tag_window_runtime` stamp; the hook wait runs only for `uses_session_map` runtimes). On `create_window` collision (`ccbot` window already taken → `ccbot-2`), `edit_forum_topic` renames the topic to match the dedup'd window name.

**Scope and known limitation**: the auto-bind only fires when `forum_topic_created` is delivered to ccbot — i.e. the topic was created with ccbot online. Topics created while ccbot was offline, or topics that were *renamed* after creation (the rename produces a separate `forum_topic_edited` service message; the original `forum_topic_created` still carries the old name), fall back to the regular first-message flow: window picker (unbound tmux windows) → directory browser → select directory → session picker (if existing sessions) or create window → bind topic → forward pending message. The Bot API has no `getForumTopic`, so we deliberately don't try to recover the current name out-of-band — anything along those lines (probe-replies, MTProto/telethon, etc.) is rejected as a workaround. Inbound photos/documents for docker bindings land in `<agent.workspace_host_path>/.inbox/` (in-container path `/workspace/.inbox/`), and the marker sent to Claude uses the in-container path.

**Resume session flow (runtime-tabbed picker)**: On a directory confirm the bot shows one picker with a **runtime tab per installed runtime** (Claude Code / Codex / …, from `runtimes.pickable_runtimes()` — gated by `AgentRuntime.is_available`, so a host without the `codex` binary never sees a Codex tab; the initially active tab is `default_runtime()` = `CCBOT_DEFAULT_RUNTIME`, availability-checked) on top; the active tab's resumable sessions list below, plus a `➕ New session` button that starts a fresh window on that runtime. Tapping a tab (`CB_RUNTIME_TAB`) re-enumerates that runtime's sessions (`AgentRuntime.list_sessions` → Claude globs `~/.claude/projects/<cwd>/`, Codex matches `session_meta.cwd` across `~/.codex/sessions/**`) and re-renders in place; a resume tap (`CB_SESSION_SELECT`) launches on the active tab's runtime. There is **no separate runtime picker** — the empty-sessions case is just a tab with no rows. Choosing a Claude session runs `claude --resume <session_id>` (the `--resume` hook reports a new session_id but messages keep writing to the original JSONL; window_state is overridden to track the original id); a Codex session runs `codex resume <id>` (tracking is by cwd, so the rollout is picked up regardless). A **new runtime** becomes a tab automatically by registering in `RUNTIMES` and implementing `list_sessions` — the builder never hardcodes a runtime.

**Topic lifecycle**: *Closing* a topic fires `FORUM_TOPIC_CLOSED` → auto-kills the associated tmux window (if tmux binding) and unbinds the thread. Hard-*deleting* a topic gives no event, so it's caught when a real send to it bounces with `Topic_id_invalid` — the queue worker runs `cleanup.purge_deleted_topic` (kill window + unbind + clear state); a backstop probe (`reopen_forum_topic` — `unpin_all_forum_topic_messages` and `send_chat_action` deceptively return **OK** on a hard-deleted topic, only `reopen` raises `Topic_id_invalid`; on a live *open* topic it's a no-op `Topic_not_modified`, and every bound topic is open since closing unbinds → no visible effect; round-robin one bound topic per `TOPIC_CHECK_INTERVAL`, **plus every worktree topic each `WT_TOPIC_CHECK_INTERVAL`** so a deleted worktree agent reclaims in ~seconds, all in `background_context()`) covers topics deleted while completely idle. Stale tmux bindings (window deleted externally) are cleaned up by the status polling loop. Conversely, **orphan tmux windows** (window alive, no thread binding points to it — e.g. topic deleted while ccbot was offline so neither path fired) are killed by the janitor in `status_polling.py` after a 90s grace. Invariant: every non-`__main__` tmux window must be bound to exactly one topic, otherwise it is reaped. This prevents `ccbot`, `ccbot-2`, `ccbot-3`… from accumulating in the same directory whenever a topic gets re-created. Docker bindings are skipped from both checks — their lifecycle is the container's lifecycle, not a tmux window's.

## Session Lifecycle

**Startup cleanup**: On bot startup, all tracked sessions not present in any session_map source (host + per-agent) are cleaned up, preventing monitoring of closed sessions.

**Runtime change detection**: Each polling cycle checks for session_map changes (merged view):
- Binding's session_id changed (e.g., after `/clear`) → clean up old session.
- Binding removed from every source → clean up corresponding session.
