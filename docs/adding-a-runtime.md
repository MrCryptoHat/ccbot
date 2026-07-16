# Adding an agent runtime

ccbot can drive any terminal AI agent, not just Claude Code — Codex is the
built-in second runtime and the reference for everything below. A *runtime* is
the CLI a topic's window runs; it is orthogonal to the *transport* (tmux
window vs docker container). All runtime-specific behavior lives in one class:
`runtimes.AgentRuntime`. Adding an agent = one subclass + one registry entry —
the monitor, session picker, agent panel, rebind, history and restart flows
pick it up automatically.

## Is the CLI a viable candidate?

Two hard requirements, check them before writing any code:

1. **It runs as an interactive TUI inside tmux.** ccbot types into the pane
   and screenshots it; a pure batch CLI won't work.
2. **It writes session transcripts to files on disk** (JSONL or similar, in a
   discoverable location). The monitor is a file poller — an agent that keeps
   history only in memory or a remote API can't be tailed.

Nice to have: a `--resume <id>` flag, a stable "busy" indicator in its TUI,
and a `SessionStart`-style hook (without one you'll track sessions by cwd,
like Codex).

## The contract, member by member

Subclass `AgentRuntime` in `src/ccbot/runtimes.py` and register the singleton
in `RUNTIMES`. Everything not listed here inherits a sane Claude-shaped
default.

**Identity & availability**

| Member | What it does |
| --- | --- |
| `name` | Stable id persisted in `WindowState.runtime` and env config. |
| `display_name`, `picker_icon` | Label + emoji on the session-picker tab. |
| `cli_command()` | Configured launch command (add a `Config` field with a `CCBOT_*`/env override). First token is probed by `is_available()` — no binary ⇒ no picker tab, zero config needed. |

**Lifecycle**

| Member | What it does |
| --- | --- |
| `launch_command(window_name, resume_session_id)` | Shell line typed into a fresh pane. MUST validate the resume id (`is_valid_session_id`) — it reaches the shell. |
| `exit_command()` | TUI quit command (`/exit`, `/quit`, …) used by restart. |
| `pane_alive_commands` | tmux `pane_current_command` values meaning "still running". Get this wrong and the health check reaps your windows 30 s after launch. |
| `interrupt_keys` | Keys `/esc` sends. Check what an idle Ctrl-C does in the TUI first (in Codex it arms quit). |

**Bootstrap capabilities** (never branch on `runtime.name` at call sites)

| Member | What it does |
| --- | --- |
| `uses_session_map` | True iff a SessionStart hook registers sessions. False ⇒ window cwd is persisted and transcripts resolve by cwd. |
| `auto_forward_first_message` | False if a fresh TUI can open on a menu (sign-in, onboarding) where blind typing would select something. |
| `ready_message_key(resumed)` | i18n key of the "window ready" message (add the key to `i18n.py`, ru + en). |

**Transcripts** (the monitor)

| Member | What it does |
| --- | --- |
| `iter_transcripts(...)` | `(session_id, path)` per live window, called every ~2 s tick. Keep it cheap; cache what you can. |
| `parse_entries(raw, pending)` | Raw JSON lines → the shared `ParsedEntry` list. This is the real work: a dedicated parser module (see `codex_transcript_parser.py`) mapping the CLI's schema to text / tool_use / tool_result / thinking. Everything downstream (queue, voice, tables, pins) is runtime-agnostic. |
| `list_sessions(sm, cwd)` | Resumable sessions for the picker tab (→ `AgentSession`). |
| `history_transcript(sm, window_id)` | Transcript path for the `/commands` history view. |
| `latest_context_tokens(raw)` | Context fill for limit alerts; return `None` to skip. |

**TUI detection** (`terminal_parser.py` holds the pure functions)

| Member | What it does |
| --- | --- |
| `is_working(pane)` | "A turn is running" — gates every don't-barge decision (restart guard, task pin, reactions, `/inject`). Find a stable anchor in the busy status line. |
| `has_queued_input(pane)` | The CLI's queued-input hint; drives the 👀 read-ack timing. |

Interactive menus usually need **no work**: the generic `ChoiceMenu` pattern
(numbered options + confirm/cancel footer) and the provider-agnostic login
flow (`is_login` patterns) already catch most TUIs. Add a named pattern only
if the generic one misses.

**Optional features**

| Member | What it does |
| --- | --- |
| `panel_actions`, `panel_slash_commands` | Which agent-panel buttons show, and the slash each types (e.g. Codex maps "context" → `/status`). |
| `edit_tool_names`, `diff_header_re`, `diff_boundary_re` | `/diff` edit screenshots: the tool_use names that trigger a scan + regexes cropping the native diff block. Empty ⇒ `/diff` no-ops. |
| `native_image_input`, `composer_image_token` | True if typing an image path into the composer attaches it client-side (Codex); False ⇒ the `(image attached: <path>)` text marker. |

## Capturing TUI reference panes

Don't guess the TUI chrome — capture it:

```bash
tmux new-session -d -s probe -x 100 -y 40 -c /tmp/probe
tmux send-keys -t probe "your-cli" Enter
sleep 10 && tmux capture-pane -t probe -p     # idle state
# ...ask it something long-running, capture the busy state, menus, etc.
```

Pin every captured layout in tests (see `tests/ccbot/test_runtimes.py` and
`test_terminal_parser.py` — fixtures are string literals of real panes,
**sanitized**: no real paths, names or session ids; this repo is public).
Note the CLI version you captured against: agent CLIs self-update, and ccbot's
version canary (`_check_runtime_versions`) will warn when the installed
version drifts from the captured one.

## Checklist

- [ ] Subclass in `runtimes.py`, singleton registered in `RUNTIMES`.
- [ ] Parser module producing `ParsedEntry`, with a synthetic fixture in
      `tests/fixtures/<name>/`.
- [ ] Busy / queued-input detectors + tests pinning real (sanitized) panes.
- [ ] `pane_alive_commands` verified against `tmux display -p
      '#{pane_current_command}'` while the CLI runs.
- [ ] i18n keys (ready message) in both languages.
- [ ] `tests/` green: `uv run ruff check && uv run pyright src/ccbot/ &&
      uv run pytest`.
- [ ] One live end-to-end pass: create a topic, send a message, get the reply,
      restart the session from the panel.

What you get for free once registered: a picker tab with resume list, monitor
delivery, runtime-aware busy gating everywhere, the agent panel (gated to your
`panel_actions`), image/document routing, `/esc`, history, topic rebind that
remembers your runtime, and the self-update version canary.
