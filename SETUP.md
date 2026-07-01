# Setup

A first-run walkthrough for a fresh install. The bot bridges Telegram Forum
**topics** to Claude Code sessions running in **tmux** — so all you need is a
Linux/macOS host with tmux, the `claude` CLI, and `uv`.

## 1. Prerequisites

- **tmux** — `tmux -V` should print a version.
- **Claude Code** — the `claude` CLI on your `PATH`
  (install: https://claude.com/claude-code).
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
git clone <this-repo-url> ccbot
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

## Troubleshooting

- **Bot doesn't respond** — confirm it's an admin in the group and your id is
  in `ALLOWED_USERS`. Check the pane: `tmux attach -t ccbot`.
- **No directory browser / auto-bind** — the topic must be created while the
  bot is online; a topic renamed after creation falls back to the browser on
  first message.
- **Voice not transcribed** — set `DEEPGRAM_API_KEY` or `OPENAI_API_KEY`.
- **Logs** — `tmux capture-pane -t ccbot:__main__ -p | tail -50`, or raise
  `CCBOT_LOG_LEVEL=DEBUG`.
