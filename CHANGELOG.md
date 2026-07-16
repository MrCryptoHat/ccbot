# Changelog

Notable changes to ccbot. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/) (0.x: minor = features, patch = fixes).

## [0.1.0] — 2026-07-16

First tagged release — the feature set the public repo launched with.

### Core

- **Topic-based sessions**: one Telegram Forum topic = one binding = one agent
  session; tmux windows on the host or (opt-in) Claude Code inside docker
  containers.
- **Live delivery** of agent replies via transcript polling (hook-based
  session tracking, byte-offset incremental reads); MarkdownV2 with automatic
  plain-text fallback; long tables / box-art / code delivered out-of-band as
  images or file attachments.
- **Interactive prompts as screenshots** (AskUserQuestion, plan mode,
  permission prompts, sign-in menus, generic numbered menus) with an inline
  `↑ ↓ ⏎ Esc` keyboard; AskUserQuestion additionally surfaces its prose as
  text.
- **Agent panel** (`/screenshot`): live pane view with three tabs of key
  presses, everyday actions and session lifecycle (resume / new / restart /
  end), gated to what the bound agent's runtime actually supports.
- **Agent runtimes as a first-class axis**: Claude Code (default) and OpenAI
  Codex ship built-in; a runtime-tabbed session picker (tabs appear only for
  installed CLIs), per-topic runtime memory across window deaths, runtime-aware
  busy detection, `/diff` edit screenshots, image input (Codex native
  composer), history, `/esc`, and a CLI self-update canary. Adding a runtime
  is one `AgentRuntime` subclass — see `docs/adding-a-runtime.md`.
- **Worktree agents**: fork a repo into a `git worktree` + branch from a
  button, run a parallel agent (either runtime) in its own topic, with a
  two-tier teardown guard that never silently destroys unmerged work.
- **Voice**: inbound transcription (Deepgram → OpenAI fallback) and optional
  TTS replies (Gemini / ElevenLabs / OpenAI) with a `[chat]…[/chat]`
  text/voice interleave protocol.
- **Quality-of-life**: task pinning, 👍-to-confirm, 👀 read-acks, bilingual
  UI (ru/en, `/lang`), per-topic message queues with merging and a
  three-lane rate limiter tuned to Telegram's real limits.
- **Deployment**: two required env vars (`TELEGRAM_BOT_TOKEN`,
  `ALLOWED_USERS`), everything else optional with sane defaults; optional
  plugin packages for server-specific integrations; localhost task-injection
  socket (`CCBOT_INJECT_TOKEN`) with a hardened input sanitizer.

[0.1.0]: https://github.com/MrCryptoHat/ccbot/releases/tag/v0.1.0
