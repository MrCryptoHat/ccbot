# Docker agents (advanced)

Route a Telegram topic to a Claude Code instance running **inside a Docker
container** instead of a host tmux window. This is an advanced,
bring-your-own-container feature: ccbot ships no image and doesn't manage the
container's lifecycle — it only *drives* a container you run yourself. If you
just want several parallel sessions, plain tmux topics (and worktree agents)
need none of this.

## How ccbot drives a container

For a topic bound to `docker:<agent>`, every keystroke and pane capture goes
through:

```
docker exec -e TERM=xterm-256color <container> tmux send-keys / capture-pane -t claude ...
```

So the contract is small, but strict:

1. **The container runs a tmux session named exactly `claude`** with one
   long-lived Claude Code process in it. Your entrypoint creates it, e.g.:

   ```sh
   tmux new-session -d -s claude -c /workspace \
       "claude --dangerously-skip-permissions"
   ```

   ⚠️ Note on `/restart` from Telegram: it does **not** re-run your
   entrypoint's command — it always launches
   `claude --dangerously-skip-permissions [--resume <id>]`
   (`docker_driver.start_session`'s built-in default). If your entrypoint
   deliberately runs plain `claude` (with permission prompts), a Telegram
   restart currently loses that choice — keep it in mind until a per-agent
   command override exists.

2. **The agent's working directory is `/workspace`**, bind-mounted from the
   host. Hard expectation, not a convention: the `(send file: …)` path
   whitelist accepts **only** `/workspace/*` from a docker agent, and inbound
   photos/documents are saved to `<workspace>/.inbox/` on the host and
   referenced as `/workspace/.inbox/...` in the marker.

3. **Claude Code's home (`~/.claude` in the container) is bind-mounted to the
   host** so the session monitor can read transcripts: it scans
   `<claude_home>/projects/*.jsonl` on the **host** side. Log in once
   (`docker exec -it <ctn> claude`) and the credentials persist in that mount.

4. **A `SessionStart` hook inside the container writes the agent's
   session map** into a bind-mounted host **directory**, keyed by the binding
   value (`docker:<agent>` — the key *is* the binding). Without it the monitor
   never learns the session id and replies won't be delivered.

   ⚠ **Mount the directory, never the map file itself.** A file bind-mount is
   pinned to the inode, so any host-side write that *replaces* the file
   (atomic tmp+rename, an editor, a backup restore) leaves the container
   writing into an orphaned inode while ccbot keeps reading a frozen copy.
   Nothing looks broken — the container is healthy, the hook succeeds, the
   agent just stops being heard from.

   Minimal example (container-side, requires `jq`; `/ipc` bind-mounted from
   the host as a directory):

   ```sh
   #!/bin/sh
   # /usr/local/bin/ccbot-session-hook — SessionStart hook
   # AGENT_NAME comes from the container env; ccbot overrides it per tmux
   # session so sibling agents (below) write their own key, not this one.
   payload=$(cat)
   sid=$(printf '%s' "$payload" | jq -r .session_id)
   cwd=$(printf '%s' "$payload" | jq -r .cwd)
   name=${AGENT_NAME:-assistant}
   jq -n --arg name "$name" --arg sid "$sid" --arg cwd "$cwd" \
     '{("docker:" + $name): {session_id:$sid, cwd:$cwd, window_name:$name}}' \
     > /ipc/session-map.json.tmp && mv /ipc/session-map.json.tmp /ipc/session-map.json
   ```

   The host-side hook (`ccbot hook`) also briefs the starting session on the
   `(send file: …)` protocol — nothing in Claude Code hints at that marker, so
   an agent that was never told about it answers "I saved it to /tmp/report.pdf"
   and no attachment is ever sent. A container hook is your own script, so add
   the same briefing to it — print it on stdout as SessionStart context (mind
   the `/workspace` whitelist, which is stricter than the host's):

   ```sh
   jq -n --arg ctx 'Your replies are relayed to a Telegram topic. To deliver a
   file, put the marker `(send file: /workspace/<path>)` in your reply text —
   absolute, under /workspace, under 50 MB, and the file must still exist.
   ccbot uploads it as a document and strips the marker. Naming a path without
   the marker sends nothing.' \
     '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
   ```

   registered in the container's `~/.claude/settings.json`:

   ```json
   { "hooks": { "SessionStart": [ { "hooks": [
     { "type": "command", "command": "/usr/local/bin/ccbot-session-hook", "timeout": 5 }
   ] } ] } }
   ```

## Host-side configuration

```ini
DOCKER_AGENTS_ENABLED=true
DOCKER_AGENTS=assistant
```

Per-agent paths default to the layout below; override any of them with
`DOCKER_AGENT_<NAME>_{CONTAINER,WORKSPACE,CLAUDE_HOME,IPC,SESSION_MAP,VNC_URL}`:

| Setting     | Default host path                          | Container side          |
| ----------- | ------------------------------------------ | ----------------------- |
| container   | `<name>` (container name)                  | —                       |
| workspace   | `~/agents/<name>`                          | `/workspace`            |
| claude_home | `~/.local/share/<name>/claude-home`        | `~/.claude`             |
| session_map | `~/.local/share/<name>/hostmap/session-map.json` | wherever your hook writes (e.g. `/ipc/session-map.json`) |
| ipc         | `~/.local/share/<name>/ipc`                | `/ipc` (optional — live browser dashboard) |

**Upgrading from before 2026-08-16:** the `session_map` default used to be
`~/.local/share/<name>/session-map.json` (no `hostmap/`). Either move the file
into `hostmap/` and switch the container to mounting that directory, or pin the
old path with `DOCKER_AGENT_<NAME>_SESSION_MAP`. ccbot does **not** fall back to
the old path — an unmigrated agent goes quiet without an error.

A matching `docker run` skeleton:

```bash
docker run -d --name assistant \
  -v ~/agents/assistant:/workspace \
  -v ~/.local/share/assistant/claude-home:/root/.claude \
  -v ~/.local/share/assistant/hostmap:/host \
  -v ~/.local/share/assistant/ipc:/ipc \
  your-claude-image
```

(that hook writes `/host/session-map.json`; point it at the ipc mount instead
with `DOCKER_AGENT_ASSISTANT_SESSION_MAP=~/.local/share/assistant/ipc/session-map.json`
if you'd rather keep one mount.)

## Binding a topic

- Create a topic **named after the agent** while ccbot is online — it
  auto-binds (`forum_topic_created` name match), or
- run `/bind <agent>` in any topic.

Docker bindings survive ccbot/tmux restarts verbatim (no window id to go
stale); their lifecycle is the container's. `/restart` from the agent panel
kills and recreates the in-container tmux session, resuming the current
Claude session.

## Sibling agents in one container

The agent panel's ➕ button starts **another** Claude Code beside the current
one, in the same container, on the same `/workspace` — a second pair of hands
on the same files, in its own Telegram topic. Naming a sibling `notes` under
agent `assistant` gives:

| | |
| --- | --- |
| binding | `docker:assistant/notes` |
| topic | `➕ assistant-notes` |
| in-container tmux session | `claude-notes` |
| workspace, claude-home, session-map path | the parent agent's (shared) |

ccbot creates it with the equivalent of

```sh
docker exec <ctn> tmux new-session -d -s claude-notes -e AGENT_NAME=assistant/notes \
    -c /workspace "claude --dangerously-skip-permissions --session-id <uuid>"
```

Two details make this work with an unmodified container:

- **`-e AGENT_NAME=...`** — the tmux *server* is already running, so `docker
  exec -e` would not reach the new process; tmux's per-session environment
  does. A hook that keys off `AGENT_NAME` (as above) therefore writes
  `docker:assistant/notes` on its own. (Needs tmux ≥ 3.2 in the image for
  `new-session -e`; on an older tmux the sibling simply fails to start.)
- **`--session-id <uuid>`** — ccbot picks the session id up front, so the
  sibling is tracked from its first second, with no wait on the hook.

**Your hook must key off `AGENT_NAME`** for siblings to work. One that
hardcodes its agent name reports the sibling's *every* session — its first
`/clear` above all — under the parent's key, which would silently re-point the
parent's topic at its child's transcript. ccbot checks for this right after
starting a sibling: if the hook answers with the parent's key, the sibling is
stopped and its topic removed again, and you get told to fix the hook instead
of inheriting a trap. (Merging into the existing JSON rather than overwriting
is also worth doing — ccbot tolerates the single-key rewrite, but the file then
only ever shows whichever agent started last.)

Lifecycle: unlike the container's own agent (which outlives its topic — that's
the container's job), a sibling is ccbot-created and dies with its topic. Close
the topic, delete it, or hit 🗑 in the panel and its `claude-<slug>` session is
killed; `⏹ Завершить` leaves the binding so `🔄 Перезапуск` can revive it.
Siblings show up in `/status` beside the configured agents.

For parallel work on a **git repo** where the agents must not step on each
other's edits, use 🌳 worktree agents instead — each gets its own branch and
directory.

## Notes

- The image needs `tmux`, the `claude` CLI, and (for the hook example) `jq`.
- Some deployments layer optional plugins on top of docker agents (rclone
  remounts that restart agents, live browser dashboards on the `ipc` mount) —
  those ship in separate plugin packages, not in this repo; the core seams
  they use are `ipc_dir` and per-agent `vnc_url`.
