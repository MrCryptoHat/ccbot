# Worktree agents — parallel agents on one project

Run several Claude agents on one repo at once, each isolated in its own `git
worktree` + branch, the whole lifecycle driven from Telegram. A worktree agent
is **just a tmux topic whose `cwd` is a worktree dir** — no new transport, no
new binding shape; the monitor, panel, send path all key on the `@<id>` binding
unchanged. Two modules: pure git/disk core `worktrees.py`, Telegram
orchestration `handlers/worktrees.py`.

## Teardown is two-tier — the safety invariant

Destructive git (`git worktree remove` + `branch -D`) runs **ONLY from
interactive paths** where the user can see what's lost: the 🗑 panel button and
`topic_closed_handler`'s guard. ⚪ clean+merged → auto-teardown; 🟢/🟡 dirty/
unmerged → keep the agent alive, reopen the topic, offer `[🧨 Удалить] [↩ Оставить]`.

**Headless paths NEVER run destructive git** — `purge_deleted_topic` (hard-
delete caught by the probe/bounce) and the orphan-window janitor only flag
`worktree_meta` `orphaned` and leave the worktree on disk for a future GC.
Don't add a `worktree remove`/`branch -D` to a headless path: there's no UI to
show the guard, so it would silently destroy unmerged work (CLAUDE.md «never
silently destroy»). `handle_deleted_worktree_topic` is the seam — it runs the
guard and only tears down when clean.

## Deletion detection — probe with `reopen_forum_topic`, not `unpin`

Telegram sends no event on topic *delete*. The existence probe MUST use
`reopen_forum_topic`: `unpin_all_forum_topic_messages` and `send_chat_action`
deceptively return **OK** on a hard-deleted topic (verified live) — only
`reopen` raises `Topic_id_invalid`. On a live *open* topic reopen is a no-op
(`Topic_not_modified`), and every bound topic is open (closing unbinds), so no
visible effect. Worktree topics are probed **every `WT_TOPIC_CHECK_INTERVAL`
(~10 s)**, all of them (not round-robin), so a natively-deleted agent reclaims
in seconds. (Detail also in topic-architecture.md / message-handling.md.)

## State, naming, layout — the non-obvious bits

- **`worktree_meta` keyed by `thread_id`** (like every per-topic structure), value = repo / branch / base / path / task_title / status. Holds only what git can't recover. Deleting the whole map degrades worktree topics to plain tmux topics — routing never breaks. `reconcile_worktree_meta()` (post-init) drops rows whose worktree is gone AND thread is unbound.
- **Worktrees live in `~/.ccbot/worktrees/<repo>/<slug>`** — outside the scanned `~/projects`/`~/agents` roots, so name-based auto-bind never mistakes a worktree for a bindable project.
- **`slugify` transliterates** (task names are Russian; a naive `[^a-z0-9-]` strip yields all-dashes) and the result must be a filesystem-safe slug (`^[a-z0-9][a-z0-9-]{0,29}$` — leading alnum, 1-30 chars). Dedup the slug against `taken_slugs` (existing worktree dirs **and** `wt/*` branches) — a leftover branch makes `git worktree add -b` hard-fail mid-provision. Provisioning is transactional: a failure after `create_forum_topic` rolls the topic back.
- **Dirty-guard subtracts seeded artifacts** — a symlinked `.env` / installed `.venv`/`node_modules` read as untracked unless the target repo gitignores them, so `parse_porcelain` filters `SEED_*` before deciding 🟢 vs ⚪. Seeding: `.env` symlinked, `.venv` via `uv sync`, node via pnpm/npm (never symlink `.venv` — it bakes absolute paths).
- **Unmerged-guard (🟡) is `git cherry` against MULTIPLE bases, not `rev-list`** — `count_unmerged` counts unmerged commits by patch-id (`git cherry <ref> <branch>`, `+`-lines) against every resolvable candidate base (local `<base>`, `origin/<base>`, upstreams of base and branch) and takes the MINIMUM («integrated anywhere ⇒ integrated»). A plain `rev-list --count <base>..<branch>` against the *local* base raised false 🟡 alarms when work landed by **direct push to the remote base** (`git push origin wt/x:master` — local base lags origin) or by **squash/rebase merge** (diff under a new sha); patch-id + multi-base covers both. Falls back to `rev-list` only when no candidate ref resolves.
- **🌳 always forks the BASE repo**, even from inside a worktree topic (`_resolve_base_repo` → `meta.repo`) — you get a sibling agent, never a worktree-of-a-worktree.
- **Reuse `create_window` + `bind_thread` directly, NOT `_auto_bind_to_directory`** — the latter runs the whole rebind flow (session picker / auto-resume, hook wait, directory-memory recording keyed to the browse path), none of which fits a freshly provisioned worktree.
- **Runtime is chosen at 🌳 time** — the flow is 🌳 → runtime picker (`CB_WT_RUNTIME`, one button per `pickable_runtimes()`) → name → provision. `provision_worktree_agent(runtime=…)` passes it to `create_window` and tags `WindowState.runtime`/`cwd` (codex: also skips the claude-only `wait_for_session_map_entry`, and the monitor resolves its rollout by the worktree cwd). A worktree agent is just a tmux topic whose cwd is the worktree dir, so a codex worktree needs no new plumbing. NOTE: codex's own panel hides 🌳 (`panel_actions` has no `worktree`), so you fork worktrees FROM claude topics; the new worktree agent itself can be either runtime.

## Not built (phase 2+)

`📋 Агенты проекта` list (🟢🟡⚪ overview), in-bot merge/PR, GC for orphaned
worktrees, agent-pane welcome injection (today: user-facing message + dir-name=slug).
