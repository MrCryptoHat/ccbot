# ccbot: control Claude Code from Telegram

[![Tests](https://github.com/MrCryptoHat/ccbot/actions/workflows/tests.yml/badge.svg)](https://github.com/MrCryptoHat/ccbot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)

A Telegram remote for **Claude Code**. Sessions run in **tmux** on your
machine or server; the bot streams replies into forum **topics** (one topic =
one session) and types your answers back into the pane. Press keys, approve
permission prompts, switch models, run several projects side by side.

Start a refactor at your desk, approve it from your phone, `tmux attach` back
into the same terminal whenever you want.

<!-- screenshots: drop 2-3 images here (agent panel, interactive prompt,
     streaming reply). Recommended width ~1000px. -->

## Why ccbot?

Claude Code runs in your terminal. When you step away — commuting, on the
couch, away from the desk — the session keeps working, but you lose visibility
and control.

ccbot lets you **continue the same session from Telegram**. The key design
choice is that it drives **tmux**, not the Claude Code SDK. Your Claude Code
process stays exactly where it is; ccbot reads its transcript and sends
keystrokes to the pane. So:

- **Switch desktop → phone mid-conversation** — Claude is on a long refactor?
  Walk away and keep monitoring/answering from Telegram.
- **Switch back anytime** — the tmux session was never interrupted; just
  `tmux attach` and you're back in the terminal with full scrollback.
- **Run sessions in parallel** — each topic maps to its own tmux window, so you
  juggle several projects from one chat group.
- **Nothing to migrate** — no SDK wrapper, no cloud middleman. Your existing
  Claude Code setup, plan and MCP servers work as-is.

Because it's a thin control layer over tmux, the terminal stays the source of
truth and you never lose the ability to switch back.

## Features

**Core (any install):**

- **Topic-based sessions** — 1 topic = 1 tmux window = 1 Claude session.
- **Live delivery** — assistant replies (and, opt-in, edit diffs / thinking)
  stream to the bound topic; a `typing…` indicator signals liveness.
- **Interactive prompts as screenshots** — AskUserQuestion, ExitPlanMode,
  permission prompts and the model/MCP pickers are sent as pane screenshots
  with an inline `↑ ↓ ⏎ Esc 🔄` keyboard.
- **Agent panel** — `/screenshot` opens a live pane view with key-press and
  session controls (mode, model, context, compact, clear, restart, resume…).
- **Voice** — inbound voice messages are transcribed (Deepgram → OpenAI); with
  a TTS key the bot can reply in voice (`/voice`).
- **Directory browser & resume** — start or resume Claude sessions from a topic.
- **MarkdownV2 rendering** with auto-fallback; long code/tables/box-art are
  delivered out-of-band so a phone never mangles them.
- **Bilingual UI** — `ru` / `en`, switchable at runtime with `/lang`.
- **Hook-based tracking** — a `SessionStart` hook maps windows ↔ sessions,
  surviving `/clear` and restarts.

**Optional (off unless configured):**

- **Docker agents** — route a topic to Claude Code inside a container
  (advanced, bring-your-own-container — see [docs/docker-agents.md](docs/docker-agents.md)).
- **Worktree agents** — fork a repo into a `git worktree` + branch and run a
  parallel agent, all from Telegram.
- **Reaction controls** — 👍-to-confirm and 👀 read-acks, both **on by default**
  (env switches: `REACTION_CONFIRM_ENABLED=false` / `CCBOT_REACTION_ACK=false`;
  the runtime `/react` toggle flips the 👀 read-ack only).
- **Automation hooks** — a localhost task-injection socket
  (`CCBOT_INJECT_TOKEN`) for scripting tasks into agents.

See **"Core vs optional"** below and [`.env.example`](.env.example) for the
full switch list.

## Quick start

Prerequisites: **tmux**, the **`claude`** CLI, and **`uv`**
(https://docs.astral.sh/uv/). Then:

```bash
git clone https://github.com/MrCryptoHat/ccbot.git
cd ccbot
uv sync
cp .env.example .env         # set TELEGRAM_BOT_TOKEN + ALLOWED_USERS
uv run ccbot hook --install  # session-tracking hook
./scripts/restart.sh         # create the tmux session and launch
```

A full walkthrough (BotFather, enabling group Topics, first session) is in
**[SETUP.md](SETUP.md)**.

## Configuration

Only two variables are required:

| Variable             | Description                                        |
| -------------------- | -------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS`      | Comma-separated Telegram numeric user IDs          |

Everything else is optional with a sane default. All variables — core toggles,
voice keys, docker agents, and the server-specific integrations — are
documented inline in **[`.env.example`](.env.example)**. `.env` is loaded from
the repo root or from `$CCBOT_DIR` (default `~/.ccbot/`) and is gitignored;
never commit real tokens.

> On a headless VPS with no terminal to approve permissions:
> `CLAUDE_COMMAND=claude --dangerously-skip-permissions`
> ⚠️ This lets the agent run shell commands and edit files without asking —
> use it only on a host where that's acceptable.

## Core vs optional

ccbot is one codebase that runs as a **minimal tmux↔Claude bridge** out of the
box, and grows extra capabilities as you set env vars. Nothing optional runs
unless you turn it on, so a plain deployment carries no dead code paths.

| Feature                    | Enabled by                                   |
| -------------------------- | -------------------------------------------- |
| Core tmux bridge           | always on (the two required vars)            |
| Voice transcription / TTS  | a provider key (`DEEPGRAM_/OPENAI_/GEMINI_/ELEVENLABS_…`) |
| Docker agents              | `DOCKER_AGENTS_ENABLED=true` + `DOCKER_AGENTS` |
| 👍-to-confirm reactions     | `REACTION_CONFIRM_ENABLED` (on by default)   |
| Task-injection socket      | `CCBOT_INJECT_TOKEN`                          |

Everything **server-specific** lives as separate `ccbot.<name>` **plugin
packages** rather than in the core tree — the public repo ships none. The
reference deployment runs an inter-agent mail bus, external chat gateways,
`drive` (rclone mounts: `/mount`/`/umount`/`/remount`, a Mounts section and
Fix-Drive button in `/status`) and `fleet` (a preview-server fleet + live
browser dashboards for docker agents). List the ones you have in
`CCBOT_PLUGINS` (comma-separated) and they load at startup; the loader
tolerates a missing package, so the core always runs standalone. The plugin
hook contract (i18n, commands, handlers, startup/shutdown, `/status`
sections/buttons, callback dispatch) is documented in
[`src/ccbot/plugins.py`](src/ccbot/plugins.py).

## Writing a plugin

Extensions that don't belong in core (a new gateway, a notification bus, …)
live as self-contained `ccbot.<name>` packages: drop `src/ccbot/<name>/` into
your checkout, list `<name>` in `CCBOT_PLUGINS`, and the loader picks it up at
startup. Core never references specific plugins — new plugins are pure
additions, no core edit needed.

A plugin package optionally exposes: `STRINGS` (i18n catalog merges),
`bot_commands()`, `register_handlers(app)`, `async on_startup(app)`,
`async on_shutdown()`, `status_sections()` / `status_buttons()` (contribute
to `/status`), and `callback_dispatch()` (own inline-button prefixes). If it
has secrets, put them in a `config.py` submodule
that reads env at import — the plugin loader imports it before the tmux server
spawns, so tokens get captured and scrubbed cleanly.

Full contract with docstrings: [`src/ccbot/plugins.py`](src/ccbot/plugins.py).
Missing/broken plugin names are logged and skipped, so a config referencing an
uninstalled plugin never crashes the bot.

## Hook setup

```bash
uv run ccbot hook --install    # or just `ccbot hook --install` if on PATH
```

Or add manually to `~/.claude/settings.json` (or
`$CLAUDE_CONFIG_DIR/settings.json` if you relocate Claude's config):

```json
{ "hooks": { "SessionStart": [ { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] } ] } }
```

This writes window↔session mappings to `$CCBOT_DIR/session_map.json` so the bot
tracks which Claude session runs in each window, even after `/clear`.

Uninstalling ccbot? Run `uv run ccbot hook --uninstall` **before** deleting the
checkout — the hook entry records an absolute path into it, and a deleted repo
would leave every future Claude Code session running a dead hook command.

## Usage

```bash
uv run ccbot            # or just `ccbot` if installed on PATH
```

**1 Topic = 1 Window = 1 Session.** Create a topic, send a message, pick a
directory (or resume an existing session); text/voice then flow to Claude and
its replies come back to the topic. Close the topic to kill the window. Any
unrecognized `/command` (e.g. `/clear`, `/compact`, `/review`) is forwarded to
Claude Code as-is.

The persistent menu keyboard (🖥️ Server / 👾 Agent) attaches automatically to
each topic's bind confirmation or first reply (Telegram scopes reply keyboards
per forum topic); `/menu` re-attaches it if dismissed.

## Architecture & internals

The design, module map, topic/binding lifecycle, and per-subsystem rules live
in [`CLAUDE.md`](CLAUDE.md) and [`.claude/rules/`](.claude/rules/) —
`architecture.md` is the orientation map. Every `.py` file also carries a
module docstring describing its responsibilities.

Tech stack: Python, [python-telegram-bot](https://python-telegram-bot.org/),
tmux, [uv](https://docs.astral.sh/uv/).

Dev checks (must pass before committing):

```bash
uv run ruff check src/ tests/
uv run pyright src/ccbot/
uv run pytest -q
```

## FAQ

**How do I control Claude Code from my phone?**
Run ccbot on the machine where Claude Code runs (laptop, home server, VPS)
and add its bot to a Telegram group with Topics enabled. Each topic becomes
a remote for one session: replies stream in, you answer, press keys, approve
prompts.

**Is this a separate AI agent or a wrapper around the API?**
Neither. ccbot doesn't call any LLM API and doesn't re-implement an agent —
it remote-controls the **real Claude Code CLI** in tmux by reading its
transcript and typing into its pane. Your subscription/plan, config, memory
and MCP servers apply unchanged, and there's no extra token cost.

**What happens if the bot dies or the server reboots?**
Nothing is lost: sessions live in tmux/Claude Code, not in the bot. ccbot
reconnects on restart, each topic remembers its project directory, and any
session can be resumed from the panel (`⏪ Resume`).

**Can I run several projects at once?**
Yes — that's the core design. Each Telegram topic binds to its own tmux
window (or Docker container). There are also worktree agents: fork a repo
into a `git worktree` and run a parallel agent on a branch, from the chat.

**Does it work with voice?**
Yes, both ways (optional): send voice messages (Deepgram/OpenAI
transcription) and get spoken replies (Gemini / ElevenLabs / OpenAI TTS).

**Is my code exposed to a third party?**
No middleman: traffic flows between your server and Telegram's Bot API under
your own bot token. But mind the trust boundary inside Telegram itself:
`ALLOWED_USERS` controls who can *drive* the bot, while everything the bot
posts — agent replies with code, pane screenshots, sent files — is visible to
**every member of the group**. Keep the group private and invite only people
you'd let read your terminal.

## Credits & license

Forked from and originally created by [six-ddc/ccmux](https://github.com/six-ddc/ccmux).
MIT licensed — see [LICENSE](LICENSE).
