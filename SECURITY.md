# Security

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/MrCryptoHat/ccbot/security/advisories/new)
rather than a public issue. You should get a response within a week.

## Trust model (read this before deploying)

ccbot remote-controls a real Claude Code CLI on your machine. Understand the
boundaries before exposing anything:

- **The Telegram group is the read boundary.** Everything the bot posts —
  agent replies with code, pane screenshots, `(send file:)` attachments — is
  visible to **every member** of the group. `ALLOWED_USERS` restricts who can
  *drive* the bot, not who can see its output. Keep the group private.
- **`ALLOWED_USERS` is the control boundary.** Only listed numeric user ids
  can bind topics, send text to agents, press panel buttons, or trigger
  reactions. Everyone else is refused.
- **The agent itself is inside your trust boundary.** An agent run with
  `--dangerously-skip-permissions` executes shell commands without asking.
  For tmux (host) agents, the `(send file: /path)` marker delivers any file
  readable by the bot's user to the chat — a prompt-injected agent could
  abuse that. Docker agents are confined to `/workspace/*` by a path
  whitelist.
- **The inject endpoint is local-only by construction.** It binds a unix
  socket (`0660` under a `0700` dir), never TCP; it requires a token, an
  agent allowlist, and refuses to interrupt a busy agent. Input is sanitised
  against control bytes and leading `!`/`/` (shell/command escape in the
  Claude Code TUI). It does not start at all without `CCBOT_INJECT_TOKEN`.
- **Secrets hygiene.** `TELEGRAM_BOT_TOKEN` and API keys are scrubbed from
  `os.environ` before the tmux server spawns, so agent subprocesses don't
  inherit them (`CCBOT_SENSITIVE_EXTRA` extends the list). Never commit
  `.env`.
