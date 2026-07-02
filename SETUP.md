# Setup

A first-run walkthrough for a fresh install. The bot bridges Telegram Forum
**topics** to Claude Code sessions running in **tmux** — so all you need is a
Linux/macOS host with tmux, the `claude` CLI, and `uv`.

## 1. Prerequisites

- **tmux** — `tmux -V` should print a version.
- **Claude Code** — the `claude` CLI on your `PATH`
  (install: https://claude.com/claude-code). Run `claude` once and complete
  the sign-in before the first session — otherwise the first topic you open
  will greet you with an OAuth screen (the bot detects it and sends you the
  sign-in link, so it's recoverable, but logging in beforehand is smoother).
- **uv** — https://docs.astral.sh/uv/ (installs Python + deps).
- **ffmpeg** — only if you want Gemini voice replies (PCM→OGG); skip otherwise.

## 2. Create the bot and enable topics

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the **token**.
2. Create a Telegram **group**, then enable **Topics** in the group settings
   (Manage group → Topics). The bot works only in topic (forum) mode.
3. Add your bot to the group and make it an **admin** (needed to read topics
   and — for the 👍-to-confirm feature — to receive reactions).
4. Get your own numeric Telegram user id (e.g. via
   [@userinfobot](https://t.me/userinfobot)) — that's `ALLOWED_USERS`.

## 3. Install

```bash
git clone https://github.com/MrCryptoHat/ccbot.git
cd ccbot
uv sync
```

## 4. Configure

```bash
cp .env.example .env      # then edit .env
```

Set the two required values:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
ALLOWED_USERS=123456789
```

Everything else in `.env.example` is optional and off by default. Add a voice
key (Deepgram/OpenAI/Gemini/ElevenLabs) if you want speech; leave the whole
**SERVER-SPECIFIC** section blank — it wires into one particular server's
extra infra and does nothing unless configured.

Set `CCBOT_DEFAULT_LANG=ru` if you'd rather the bot's own UI be in Russian
(default is English; a `/lang` switch is available at runtime either way).

On a headless VPS with no terminal to approve tool permissions you may want
`CLAUDE_COMMAND=claude --dangerously-skip-permissions`. **Understand the
trade-off first**: the agent then runs shell commands and edits files without
asking — only do this on a host where that's acceptable.

## 5. Install the session-tracking hook

```bash
uv run ccbot hook --install
```

This adds a `SessionStart` hook to `~/.claude/settings.json` so the bot can map
each tmux window to its Claude session (survives `/clear` and restarts). Or add
it manually — see the README "Hook Setup" section.

## 6. Run

```bash
./scripts/restart.sh      # creates the tmux session and launches the bot
```

`restart.sh` is idempotent: it stops any running instance, (re)creates the
`ccbot` tmux session/window, and starts the bot. Re-run it after code changes.
To run in the foreground instead: `uv run ccbot`.

## 7. First session

1. In your group, create a **new topic** (name it after a project dir, e.g.
   `myproject`, and if `~/projects/myproject` or `~/agents/myproject` exists it
   auto-binds; otherwise a directory browser appears).
2. Send any message in the topic.
3. Pick the project directory (and resume an existing Claude session or start
   fresh). A tmux window is created, `claude` launches, and your message is
   forwarded.
4. From then on, text/voice in that topic goes to Claude; its replies come
   back to the topic. Closing the topic kills the window.
5. The persistent menu keyboard (🖥️ Server / 👾 Agent) appears automatically
   with the bind confirmation or the first reply — Telegram scopes reply
   keyboards per topic. If you ever dismiss it, `/menu` brings it back.

## 8. Start on boot (optional)

`restart.sh` is idempotent and self-healing, so the simplest autostart is a
`@reboot` cron entry (run `crontab -e`):

```cron
@reboot sleep 10 && /home/YOU/ccbot/scripts/restart.sh >> /tmp/ccbot-boot.log 2>&1
```

(The `sleep` gives the network a moment to come up.) Without this, the bot
stays down after a server reboot until you run `restart.sh` by hand.

## Troubleshooting

- **Bot doesn't respond** — confirm it's an admin in the group and your id is
  in `ALLOWED_USERS`. Check the pane: `tmux attach -t ccbot`.
- **Messages go out, but no replies come back** — the `SessionStart` hook is
  probably missing (step 5): run `uv run ccbot hook --install` and restart the
  agent's session. The bot also warns about this at startup (log) and in the
  topic when it detects the hook is absent.
- **No directory browser / auto-bind** — the topic must be created while the
  bot is online; a topic renamed after creation falls back to the browser on
  first message.
- **Voice not transcribed** — set `DEEPGRAM_API_KEY` or `OPENAI_API_KEY`.
- **Logs** — `tmux capture-pane -t ccbot:__main__ -p | tail -50`, or raise
  `CCBOT_LOG_LEVEL=DEBUG`.
