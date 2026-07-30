---
paths:
  - "src/ccbot/handlers/siblings.py"
  - "src/ccbot/docker_driver.py"
  - "tests/**siblings**"
---

# Sibling agents — a second agent on the SAME files

➕ next to 🌳. A worktree agent forks a repo (own branch + dir); a sibling
shares the parent's files verbatim and only gets its own session and topic.
Tmux parent → another window on the same cwd (plain `@<id>`, no new
machinery). Docker parent → another Claude Code **in the same container**:
binding `docker:<agent>/<slug>`, tmux session `claude-<slug>`, topic
`<agent>-<slug>`.

## The container contract (works on an unmodified image)

- **`tmux new-session -e AGENT_NAME=<agent>/<slug>`** — the tmux *server* is
  already running, so `docker exec -e` never reaches the new process; only
  tmux's per-session env does. A hook keying off `AGENT_NAME` then writes the
  sub's own session_map key.
- **`claude --session-id <uuid>`** — ccbot picks the id up front and records it
  immediately, so a sibling is trackable from its first second. Both live in
  `session_manager.start_docker_agent`, the one launcher shared by provisioning
  and restart — don't re-issue `start_session` from a call site or the two
  drift.
- **A hook that hardcodes its agent name is REFUSED, not tolerated.**
  `_hook_names_the_sibling` watches the map right after the start: parent's key
  carrying the sibling's pinned id ⇒ that hook reports every future session of
  the sibling (its first `/clear`!) as the parent's, so provisioning rolls back
  with an explanation. `_session_id_taken_by_other` only catches the *first*
  such claim — a fresh id ccbot never pinned has nothing to be compared against,
  which is exactly why the refusal exists.
- **Only keys ccbot already knows are ingested** (`_owns_map_key`): the agent's
  own binding, plus sub-agents that exist in `window_states`/bindings. ccbot is
  the only thing that creates a sibling, so an unknown sub key is a container
  inventing rows — and since docker rows skip the stale sweep, they would
  accumulate, keep dead sessions in the monitor, and resurrect torn-down
  siblings. Session ids from that file are validated too (they end up in a
  transcript path).

## Docker window_states are never reaped by session_map absence (except…)

A container hook typically rewrites its file with a single key, so any given
read shows only whichever session started last — absence proves nothing. Hence
`load_session_map` skips docker rows in its stale sweep, and the monitor takes
its docker half from `session_manager.docker_session_map()` (i.e. window_states)
instead of re-reading the files. Rows go away on teardown, via
`forget_binding` — plus one exception the sweep keeps: a row whose agent is no
longer in the config (dropped from `DOCKER_AGENTS`) has nothing left to own it
and would otherwise sit in state.json forever, its session ids still counted
active by the monitor every tick.

A dead sibling is invisible from the outside — its container is healthy, only
its tmux session is gone (a `docker restart` recreates the entrypoint's agent
and none of the siblings) — so `status_polling._notify_dead_sibling` turns the
failed pane capture into one message in the topic pointing at 🔄. That is the
sole caller of `session_manager.docker_agent_running`.

## tmux targets must be exact

`-t claude` prefix-matches `claude-<slug>` once the main session is gone (tmux
falls back to prefix, then fnmatch) — it would report a dead agent alive and
aim keys at a sibling's pane. `docker_driver` addresses sessions as `=name`
and panes as `=name:` (a pane target rejects a bare `=name`).

## Lifecycle asymmetry — the thing to not "unify"

A configured docker agent outlives its topic (its lifecycle is the
container's); a sibling is ccbot-created and dies with its topic —
`teardown_sibling` kills `claude-<slug>` on close/delete, since nothing else
could ever reach that session again. `⏹ Завершить` still only kills, keeping
the binding so 🔄 can revive it. Sub display names are ccbot's (the hook
reports the raw binding as `window_name`, a routing key, not a label).

➕ is hidden on worktree topics (`can_offer_sibling`): the worktree teardown
removes the directory, which would strand a sibling running inside it — bound,
so the orphan janitor never reaps it. Fork another worktree with 🌳 instead.

The panel's 🗑 (`handlers/agent_delete.py`) deletes agent **and** topic for any
non-worktree topic — it reuses `purge_deleted_topic`, so each kind loses
exactly what that path already kills, and the confirm copy is per-kind for the
same reason. Worktree topics keep their own 🗑: only that flow weighs unmerged
git work before destroying anything.
