"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt,
    RestoreCheckpoint) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Whether a turn is actively running (``is_claude_working`` — inspects
    *only* the status line above the chrome separator and tells the active
    form, carrying ``esc to interrupt`` / a running ``(Ns · …`` counter,
    from the lingering completed-turn marker ``✻ Cooked for 12s``; never a
    blind scan of the whole pane).
  - Status line (spinner characters + working text) by scanning from bottom up
    — for display in chat; use ``is_claude_working`` to gate input.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

Key functions: is_interactive_ui(), extract_interactive_content(),
is_claude_working(), parse_status_line(), detect_model_switch(),
strip_pane_chrome(), extract_bash_output().
"""

import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        # New AskUserQuestion render (Claude Code ≥ early 2026): numbered list
        # with ASCII `[ ]` / `[x]` instead of unicode `☐ / ✔`. The cursor `❯`
        # may sit at column 0 on the focused row.
        name="AskUserQuestion",
        top=(re.compile(r"^[\s❯]*\d+\.\s+\[[\sxX✓✔]\]"),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
    UIPattern(
        name="FeedbackSurvey",
        top=(re.compile(r"How is Claude doing"),),
        bottom=(re.compile(r"Dismiss"),),
    ),
    UIPattern(
        # OAuth login screen (`/login`, or a fresh `claude` needing auth). The
        # long sign-in URL is the payload — surfaced as a clickable link by
        # interactive_ui via parse_login_url, the photo is just context.
        # No-bottom mode (extends to last non-empty line): the previous bottom
        # marker `^\s*Esc to cancel` matched Claude Code up to v2.1.196 but
        # was removed from the login-URL screen in v2.1.197 — the classifier
        # then silently returned None and the URL was never surfaced. Relying
        # on the two top markers (both very specific to this exact screen) is
        # robust enough on its own.
        name="LoginPrompt",
        top=(
            re.compile(r"Use the url below to sign in"),
            re.compile(r"Paste code here if prompted"),
        ),
        bottom=(),
        min_gap=1,
    ),
]


# ── ANSI helpers ──────────────────────────────────────────────────────────

# OSC (Operating System Command) sequences: `ESC ] ... ST` where ST is
# either BEL (`\x07`) or ESC+`\\`. The most common case in a tmux
# capture-pane -e dump is OSC 8 hyperlinks (`\x1b]8;;URL\x1b\\TEXT\x1b]8;;\x1b\\`)
# emitted by Claude Code / shell programs that mark URLs as clickable.
# Our SGR-only ANSI parser leaves these untouched, so the URL bytes leak
# into rendered output and bloat visible line widths into ribbon-shaped
# screenshots. Strip every OSC, keep the visible text outside the wrappers.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_osc(text: str) -> str:
    """Remove OSC escape sequences (hyperlinks, titles, color queries).

    Visible text between paired OSC 8 hyperlink wrappers survives; only
    the wrapper bytes (including the URL) go away.
    """
    return _OSC_RE.sub("", text)


# OSC 8 hyperlink with the URL captured: `ESC ] 8 ; <params> ; <URI> ST`.
# The URI in the escape is the FULL target even when tmux wraps the visible
# label across rows — so when Claude Code marks the login URL as a hyperlink
# this recovers it whole, no reconstruction needed.
_OSC8_URL_RE = re.compile(r"\x1b\]8;[^;]*;([^\x07\x1b]+)(?:\x07|\x1b\\)")

# SGR (color) escapes — stripped before line-by-line text analysis.
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# A login screen carries one of these (the URL itself always has
# `oauth/authorize`, but a wrap could push that off the captured window).
_LOGIN_SCREEN_RE = re.compile(
    r"Use the url below to sign in|Paste code here if prompted|oauth/authorize",
    re.IGNORECASE,
)
_URL_IN_LINE_RE = re.compile(r"https?://\S+")


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


def _looks_like_login_url(url: str) -> bool:
    """Guard against grabbing some unrelated OSC 8 link on the login screen."""
    low = url.lower()
    return "oauth" in low or "claude.com/cai" in low or "/login" in low


def parse_login_url(pane_ansi: str) -> str | None:
    """Reconstruct the full Claude Code sign-in URL from a captured pane.

    Two sources, tried in order:
      1. **OSC 8 hyperlink** — if Claude Code marked the URL clickable, the
         full target sits in the escape sequence regardless of how the visible
         label wrapped. Most robust; used when present.
      2. **Wrapped plain text** — the pane wraps a long URL across rows at the
         column boundary with no spaces (capture-pane runs without ``-J``), so
         we find the ``https://`` row and glue on the following no-space
         fragments until a blank or a prose line ends it.

    Returns the URL, or None when the pane isn't a login screen / no URL is
    recoverable (caller falls back to the screenshot).
    """
    if not pane_ansi:
        return None

    # 1. OSC 8 hyperlink (full URL in the escape, immune to wrapping).
    for m in _OSC8_URL_RE.finditer(pane_ansi):
        url = m.group(1).strip()
        if _looks_like_login_url(url):
            return url

    # 2. Wrapped plain text. Strip OSC wrappers then SGR colour so the URL
    #    bytes (which strip_osc would otherwise delete along with the wrapper)
    #    survive for the line scan — strip_osc removes the OSC *sequence*; the
    #    visible plain URL on the login screen is real text, not an OSC label.
    text = _SGR_RE.sub("", strip_osc(pane_ansi))
    if not _LOGIN_SCREEN_RE.search(text):
        return None

    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _URL_IN_LINE_RE.search(line)
        if not m:
            continue
        url = m.group(0)
        # Glue wrapped continuation rows: a wrapped URL row is full-width with
        # no internal spaces; the first blank or space-bearing line ends it.
        for cont in lines[i + 1 :]:
            frag = cont.strip()
            if not frag or " " in frag:
                break
            url += frag
        return url.rstrip(").,;:!?'\"")
    return None


# Option parsing and permission-context extraction used to live here but
# were removed when the bot switched to a uniform photo+nav UI for every
# interactive prompt. See git history if the old logic is needed.


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])

# Tells the *active-turn* status line from the *completed-turn* marker.
# Both sit on the line directly above the chrome separator and both start
# with a spinner glyph, so the glyph alone is useless:
#   active : "✶ Orbiting… (3m 13s · ↓ 13.9k tokens · esc to interrupt)"
#            "✶ Orbiting… (3m 13s · ↓ tokens · thought for 34s)"   (ext. thinking)
#   done   : "✻ Cooked for 12s" / "✻ Brewed for 2m 33s" / "✻ Sautéed for 53s"
# After parse_status_line() strips the glyph we match the remaining text:
#   • done form  — "<Verb>ed for <duration>" anchored at the start.
#   • live form  — the running counter "(3m 13s · …" (paren, digits, optional
#                  unit letters, "·") or the literal "esc to interrupt" hint.
# A blind substring scan over the *whole* pane is wrong: "esc to interrupt"
# legitimately appears in transcripts, tool output, mail bodies, and source
# files (this very feature's discussion included).
_DONE_FORM_RE = re.compile(r"^\s*\w+ for \d", re.UNICODE)
# Running counter inside the active status line: "(3m 13s · …" / "(12s · …" —
# a "(" followed by a digit, then any run of word/space chars, then the "·"
# that separates the first counter segment from the rest. The leading digit
# rules out plain parenthetical text; only the status line ever sits where
# we look (directly above the chrome separator), so a false positive there
# would be an unusual transient and skipping its wake is the safe direction.
_LIVE_COUNTER_RE = re.compile(r"\(\d[\w\s]*·")
_INTERRUPT_HINT_RE = re.compile(r"esc to interrupt", re.IGNORECASE)


def _is_chrome_separator(stripped: str) -> bool:
    """A Claude Code chrome separator: a run of box-drawing dashes, optionally
    carrying a short label the TUI appends to the input-box top border
    (``──── ultracode ─`` in fast/ultracode mode, ``──── /rc active ─``, …).

    The old ``all(c == "─")`` test missed the labeled form, so for a busy
    ultracode agent parse_status_line fell through to the *bottom* border, read
    the ``❯`` prompt above it, and reported idle — silently breaking every
    is_claude_working consumer (reaction_confirm, /inject). The label
    is bounded (≤30 non-dash chars) so prose lines that merely contain dashes
    aren't mistaken for chrome.
    """
    if stripped.count("─") < 20:
        return False
    return len(stripped.replace("─", "").strip()) <= 30


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) sits a few lines above the
    input-box top border (a chrome separator). We locate the separator first,
    then scan upward for the spinner — skipping blanks and ``⎿`` continuation
    lines (a rotating Tip / tool-result child line can sit between the spinner
    and the border). Anchoring on the separator avoids false positives from
    ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Find the chrome separator: topmost one in the bottom of the pane (12 lines
    # of headroom covers the input box plus a workflow/agent footer below it).
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 12)
    for i in range(search_start, len(lines)):
        if _is_chrome_separator(lines[i].strip()):
            chrome_idx = i
            break

    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Scan up for the spinner, skipping blanks and ``⎿`` continuation lines
    # (Tip / tool-result child lines) that sit between it and the border.
    for i in range(chrome_idx - 1, max(chrome_idx - 7, -1), -1):
        line = lines[i].strip()
        if not line or line.startswith("⎿"):
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First real line above the separator isn't a spinner → no status
        return None
    return None


def is_claude_working(pane_text: str) -> bool:
    """True iff the pane shows Claude Code *mid-turn* (interruptible).

    Looks **only** at the spinner/status line directly above the chrome
    separator (via :func:`parse_status_line`) — never the whole pane —
    and then distinguishes the active-turn form from the lingering
    completed-turn marker:

      - no status line above the separator (idle prompt, or no chrome) → False
      - "<Verb>ed for <duration>" (done marker, "Cooked for 12s")       → False
      - contains "esc to interrupt", or a running "(Ns · …" counter
        (also covers extended-thinking turns that show the counter but
        not the interrupt hint)                                          → True
      - any other status-line text we don't recognise                    → False

    Use this — not ``parse_status_line() is not None`` — to decide "is
    the agent free to receive input"; ``parse_status_line`` also matches
    the done marker, which is the bug this function exists to avoid.
    """
    status = parse_status_line(pane_text)
    if status is None:
        return False
    if _DONE_FORM_RE.match(status):
        return False
    if _INTERRUPT_HINT_RE.search(status) or _LIVE_COUNTER_RE.search(status):
        return True
    _warn_unrecognized_status(status)
    return False


def is_tui_ready(pane_text: str) -> bool:
    """True iff Claude Code's input box has rendered anywhere in the pane.

    The input box is bounded by chrome separators (the same border
    :func:`parse_status_line` anchors on). While Claude boots after a
    kill+relaunch the pane is empty/black — no separator yet — so this
    distinguishes "TUI is up and waiting for input" from "still loading".

    Unlike :func:`parse_status_line`, the search spans the WHOLE visible
    pane, not just the bottom rows: a fresh session (no transcript) draws
    the input box near the *top* of a tall pane with blank padding below,
    so a bottom-only scan would miss it. Used to gate the post-restart
    screenshot so the panel photo shows the live prompt, not a boot frame.
    """
    if not pane_text:
        return False
    return any(_is_chrome_separator(line.strip()) for line in pane_text.split("\n"))


# Claude Code buffers typed input while a turn is mid-flight and shows a hint in
# the input chrome — "Press up to edit queued messages" / "esc to edit queued
# messages". Its presence means the latest message has NOT yet entered the
# agent's context (it's queued behind the running turn); its absence means the
# queue drained — everything typed was taken up. Used to ack a user message
# exactly when it lands in context, not merely when it was delivered to the pane.
_QUEUED_MSG_RE = re.compile(r"queued messages?", re.IGNORECASE)


def has_queued_messages(pane_text: str) -> bool:
    """True if Claude Code is showing buffered (not-yet-ingested) input.

    Scanned only in the bottom chrome (the input area, pinned to the pane
    bottom) so the phrase appearing in transcript/tool output can't false-fire.
    """
    if not pane_text:
        return False
    tail = "\n".join(pane_text.split("\n")[-15:])
    return bool(_QUEUED_MSG_RE.search(tail))


# Canary for Claude Code TUI updates: a status line we found but could not
# classify means the render changed (it has, repeatedly — see the dated
# layout notes around UI_PATTERNS) and every detector downstream degrades
# *silently*: busy agents look idle (mail wake types into a running turn,
# reaction-confirm presses Enter mid-turn). Rate-limited so a weird-but-
# harmless transient doesn't spam: one WARNING per unique text per hour.
_unrecognized_status_seen: dict[str, float] = {}
_UNRECOGNIZED_STATUS_LOG_INTERVAL = 3600.0


def _warn_unrecognized_status(status: str) -> None:
    now = time.monotonic()
    key = status[:80]
    last = _unrecognized_status_seen.get(key, 0.0)
    if now - last < _UNRECOGNIZED_STATUS_LOG_INTERVAL:
        return
    _unrecognized_status_seen[key] = now
    logger.warning(
        "Unrecognized Claude Code status line (TUI format may have "
        "changed; busy-detection degraded): %r",
        key,
    )


# ── Automatic model-switch (safeguard) notice ───────────────────────────

# Fable 5 (and future peers) silently downgrade to a fallback model when their
# safeguards flag a message, printing a yellow notice block into the transcript
# — NOT the JSONL, so SessionMonitor never sees it. We catch it by scanning the
# live pane. The trigger phrase is stable across the notice's wording; the
# fallback model ("Switched to Opus 4.8.") is parsed when present. Whitespace is
# flattened before matching so the pane's word-wrapping can't split the phrase.
_SAFEGUARD_NOTICE_RE = re.compile(r"safeguards flagged this message", re.IGNORECASE)
# ". " / end anchors the model name so "Opus 4.8" isn't clipped to "Opus 4" at
# the "4.8" dot; length-capped to stay a model name, not a runaway match.
_SWITCHED_TO_RE = re.compile(r"Switched to (.{1,40}?)\.(?:\s|$)", re.IGNORECASE)


def detect_model_switch(pane_text: str) -> str | None:
    """Detect Claude Code's safeguard model-switch notice in a captured pane.

    Returns the fallback model name (e.g. ``"Opus 4.8"``) when the notice is on
    screen, an empty string when the notice is present but the model name
    couldn't be parsed, or ``None`` when there is no such notice.
    """
    if not pane_text:
        return None
    flat = " ".join(pane_text.split())
    if not _SAFEGUARD_NOTICE_RE.search(flat):
        return None
    m = _SWITCHED_TO_RE.search(flat)
    return m.group(1).strip() if m else ""


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
