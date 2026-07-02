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

   (`/restart` from Telegram recreates the session with the same invocation —
   see `docker_driver.start_session`.)

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
   session map** to a bind-mounted host file, keyed by the binding value
   (`docker:<agent>` — the key *is* the binding). Without it the monitor never
   learns the session id and replies won't be delivered. Minimal example
   (container-side, requires `jq`; `/ipc` bind-mounted from the host):

   ```sh
   #!/bin/sh
   # /usr/local/bin/ccbot-session-hook — SessionStart hook for agent "assistant"
   payload=$(cat)
   sid=$(printf '%s' "$payload" | jq -r .session_id)
   cwd=$(printf '%s' "$payload" | jq -r .cwd)
   jq -n --arg sid "$sid" --arg cwd "$cwd" \
     '{"docker:assistant": {session_id:$sid, cwd:$cwd, window_name:"assistant"}}' \
     > /ipc/session-map.json.tmp && mv /ipc/session-map.json.tmp /ipc/session-map.json
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
| session_map | `~/.local/share/<name>/session-map.json`   | wherever your hook writes (e.g. `/ipc/session-map.json`) |
| ipc         | `~/.local/share/<name>/ipc`                | `/ipc` (optional — live browser dashboard) |

A matching `docker run` skeleton:

```bash
docker run -d --name assistant \
  -v ~/agents/assistant:/workspace \
  -v ~/.local/share/assistant/claude-home:/root/.claude \
  -v ~/.local/share/assistant/ipc:/ipc \
  your-claude-image
```

(with `session_map` pointed at the ipc mount:
`DOCKER_AGENT_ASSISTANT_SESSION_MAP=~/.local/share/assistant/ipc/session-map.json`.)

## Binding a topic

- Create a topic **named after the agent** while ccbot is online — it
  auto-binds (`forum_topic_created` name match), or
- run `/bind <agent>` in any topic.

Docker bindings survive ccbot/tmux restarts verbatim (no window id to go
stale); their lifecycle is the container's. `/restart` from the agent panel
kills and recreates the in-container tmux session, resuming the current
Claude session.

## Notes

- The image needs `tmux`, the `claude` CLI, and (for the hook example) `jq`.
- `/remount` (rclone deployments) restarts every active docker agent —
  a remount invalidates the FUSE snapshot the bind-mounts hold.
- The live browser dashboard (`browser_live`) and VNC links are separate,
  optional layers on the `ipc` mount; see `.env.example`.
