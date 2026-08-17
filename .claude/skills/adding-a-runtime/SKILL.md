---
name: adding-a-runtime
description: Procedure for adding a new terminal AI agent CLI (a "runtime") to ccbot as an AgentRuntime subclass — viability check, TUI pane capture, parser, detectors, wiring, verification gates. Use when the task is to make ccbot drive a CLI it doesn't support yet (Gemini CLI, Aider, Cursor CLI, Amp, a local agent…), when someone asks to "add support for <agent>", "make ccbot work with <CLI>", "register a new runtime", or when a runtime landed and its busy-detection / transcripts / picker tab need finishing. NOT for changing behaviour of an already-registered runtime (edit its subclass directly) and NOT for adding a transport (tmux vs docker is a different axis).
---

# Adding an agent runtime to ccbot

A *runtime* is the CLI a topic's window runs. It is orthogonal to the transport
(tmux window vs docker container), and every runtime-specific behaviour lives in
one class: `runtimes.AgentRuntime`. One subclass plus one registry entry, and the
monitor, session picker, agent panel, rebind, history and restart flows pick the
new agent up on their own.

**The member-by-member contract lives in `docs/adding-a-runtime.md`** — what each
member does, which have sane defaults, and what you get for free. Read it before
step 3; this skill is the order of work and the verification gates, not a second
copy of that table.

## Inputs

- `$CLI` — the agent's command (e.g. `gemini`, `aider`).
- `$NAME` — the stable runtime id you'll persist in `WindowState.runtime`.

## Goal

`$NAME` is selectable in ccbot's session picker, a topic bound to it delivers the
agent's replies to Telegram, and the panel's restart brings the same session
back — with `ruff`, `pyright` and `pytest` green, and one live end-to-end pass
done by hand.

## Steps

### 1. Check viability before writing any code

Two hard requirements: the CLI runs as an **interactive TUI inside tmux** (ccbot
types into the pane and screenshots it), and it writes **session transcripts to
files on disk** (the monitor is a file poller — memory-only or API-only history
can't be tailed).

**Success criteria:** both confirmed by running the CLI yourself, not by reading
its README. If either fails, stop and tell the user — no amount of subclassing
fixes it.

### 2. Capture the real TUI, never guess it

```bash
tmux new-session -d -s probe -x 100 -y 40 -c /tmp/probe
tmux send-keys -t probe "$CLI" Enter
sleep 10 && tmux capture-pane -t probe -p          # idle
tmux display -p -t probe '#{pane_current_command}' # → pane_alive_commands
```

If that prints a version rather than a name, the CLI is a symlink into a
per-version directory: declare the stable name and let `is_pane_alive()`
resolve the moving one — a version pinned into the set rots at the next update.

Capture idle, busy (ask it something long-running), any approval menu, and the
sign-in screen. Two things bite here: `pane_current_command` is what the health
check reaps windows by, and an idle `Ctrl-C` arms "quit" in some TUIs — check
what your interrupt key actually does before wiring `interrupt_keys`.

**Success criteria:** you have literal pane text for idle, busy and one menu, plus
the verified `pane_current_command` value, and you noted the CLI version — agent
CLIs self-update and every anchor you're about to write is pinned to that version.

### 3. Parser first, wiring second

Write the transcript parser as its own module (`<name>_transcript_parser.py`,
modelled on `codex_transcript_parser.py`) mapping the CLI's schema onto the shared
`ParsedEntry`. Everything downstream — queue, voice, tables, pins, `/diff` — is
runtime-agnostic *because* every parser produces that one shape, so a parser that
invents a field forks the whole pipeline.

Build a **synthetic** fixture in `tests/fixtures/<name>/`. This repo is public: a
transcript captured from a live session leaks paths, names and session ids, and
the criterion is «is it real», not «is it dangerous».

**Success criteria:** parser tests pass against the synthetic fixture, with no
real path, name or id anywhere in it.

### 4. Subclass, register, wire the detectors

Subclass `AgentRuntime` in `runtimes.py`, register the singleton in `RUNTIMES`,
and put the busy / queued-input detectors as pure functions in `terminal_parser.py`.
Express every divergence from Claude as a **capability** (`uses_session_map`,
`auto_forward_first_message`, `native_image_input`, …) — never as a
`runtime.name == "codex"` comparison at a call site, which silently mis-treats
the next runtime as codex-like. Add the ready-message i18n key in **both** ru and en.

Interactive menus usually need no work: the generic `ChoiceMenu` pattern and the
provider-agnostic login flow already catch most TUIs. Add a named pattern only
when the generic one demonstrably misses.

**Success criteria:** `uv run ruff check src/ tests/ && uv run pyright src/ccbot/ && uv run pytest` all green, with tests pinning the captured panes.

### 5. Prove it live

Create a topic, bind it, send a message, get a reply, then restart the session
from the agent panel and confirm the same conversation comes back.

**Success criteria:** the round trip works in Telegram — the unit tests can all
pass while the window dies 30 s after launch because `pane_alive_commands` was
wrong, and only a live pass catches that.

### 6. Record what the code can't say

Add the runtime's non-obvious behaviours to `.claude/rules/runtimes.md` — the
things a future reader would otherwise rediscover the hard way (which file to
tail and which one lies, what the preselected menu option is, what Enter does in
the composer). Keep the contract table in `docs/adding-a-runtime.md` as the one
place it lives.

**Success criteria:** someone changing this runtime in six months learns the traps
from the rule file instead of from an incident.
