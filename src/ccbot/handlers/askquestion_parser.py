"""Parser for Claude Code's AskUserQuestion picker rendered in the tmux pane.

When Claude calls ``AskUserQuestion`` it renders the question + options into
the pane as a TUI widget, and the assistant's prose (written in the same turn,
right before the tool call) sits immediately above it. **Neither reaches the
JSONL stream until the user answers** — so to surface the agent's prose as a
readable Telegram message *before* the answer, the pane is the only source, and
this module parses it. (The question + options themselves stay on the
screenshot ccbot already sends — they're parsed here only for the "is this a
real widget" check and for logging. Same idea as ``context_parser`` for
``/context``.)

Captured-pane layout (Claude Code v2.1.x)::

    ● Окей, рендер-тест. Текст ради текста — про кофе.        ← assistant prose:

      Кофе — это, по сути, ...                                  ``● `` on the
      ... которую можно заменить таблеткой.                      first line of the
    ──────────────────────────────────────────────             block, 2-space
     ☐ Кофе                                ← question "header"  indent on
                                              (a.k.a. tab)       continuations
    А ты сам как пьёшь кофе?                ← question text

    ❯ 1. На автомате                        ← option (❯ = cursor)
         Растворимый или капсула, ...        ← option description (more-indented)
      2. С заморочкой
         Свежее зерно, весы, ...
      3. Не пью вообще
         Чай, вода или ничего.
      4. Type something.                     ← always-present meta options:
    ──────────────────────────────────────────────             dropped from the
      5. Chat about this                                         parsed result
                                                                 (the photo still
    Enter to select · ↑/↓ to navigate · Esc to cancel ← footer   shows them)

Fails open: any parse miss → ``parse_ask_question`` returns ``None`` and the
caller falls back to the photo-only behaviour, so a Claude Code TUI redesign
degrades cleanly. ``tests/ccbot/handlers/test_askquestion_parser.py`` pins the
current layout — a redesign breaks those tests rather than silently regressing.
"""

import re
from dataclasses import dataclass, field

# Assistant-message bullet glyphs Claude Code uses at the start of a rendered
# block.  ● (U+25CF) is what current builds emit; ⏺ (U+23FA) has shown up too.
_BULLET_CHARS = ("●", "⏺")

# A "●" line that is NOT prose — a tool-call summary or a status line Claude Code
# prints with the same bullet.  If the block immediately above the widget starts
# with one of these, there is no assistant prose to surface.
_META_BULLET_RE = re.compile(
    r"^(?:"
    r"[\w./-]+\(.*\)\s*$"  # tool call: Bash(...), Read(file.py), mcp__x__y(...)
    r"|Ran\s+\d+\s+.*hook"  # "Ran 1 stop hook (ctrl+o to expand)"
    r"|Called\s+\S+\s+\d+\s+times?"  # "Called mytool 4 times"
    r"|Updat(?:e|ed|ing)\s+Todos?"  # todo updates
    r"|User\s+(?:declined|rejected|cancelled)"  # "User declined to answer questions"
    r"|No\s+\(tell\s+Claude"  # rejection note
    r")",
    re.IGNORECASE,
)

# A run of box-drawing horizontal line.  Claude Code draws the widget's borders
# as a full-width line of pure ─; allow a small fraction of stray glyphs (the
# pane-chrome separator ends with " <window> ──", but that one is much shorter
# in the dash-prefix sense — we only treat lines that are mostly dashes here).
_DASH_LINE_RE = re.compile(r"^─{20,}")


# The widget footer (single-question form).  Older builds: just "Enter to
# select"; current: "Enter to select · ↑/↓ to navigate · Esc to cancel".
def _is_footer(line: str) -> bool:
    return "to select" in line


# The "tab" line that opens the widget body: " ☐ Кофе" / " ✔ Кофе" / multi-tab
# "← ☐ Кофе  ☐ Чай →".
_TAB_LINE_RE = re.compile(r"^\s*(?:←\s+)?[☐✔☒]")

# An option row: "❯ 1. Label" / "  2. Label" (the leading "❯" marks the cursor).
_OPTION_RE = re.compile(r"^(?P<cursor>\s*❯)?\s*\d+\.\s+(?P<label>\S.*?)\s*$")

# Claude Code's always-present meta options — not real choices, so drop them
# from the parsed list. (We don't render the option list anyway — it's on the
# screenshot; ``options`` is parsed for the "is this a real widget" check and
# for logging.)
_META_OPTION_LABELS = frozenset({"type something", "chat about this"})


@dataclass
class ParsedAskQuestion:
    """What we managed to pull out of the AskUserQuestion widget + the prose."""

    question: str  # the question text ("" if we couldn't find it)
    options: list[tuple[str, str]] = field(default_factory=list)  # (label, description)
    prose: str = ""  # the assistant prose above the widget ("" if none / unclear)
    cursor_label: str = ""  # label of the option currently under "❯" ("" if none)


def _strip_continuation_indent(line: str) -> str:
    """Drop Claude Code's 2-space continuation indent from a prose body line."""
    if line.startswith("  "):
        return line[2:]
    return line.strip()


def _extract_prose(lines: list[str], boundary_idx: int) -> str:
    """Pull the assistant prose block sitting immediately above ``boundary_idx``.

    Walks upward from just above the widget: skips spacing blanks, then collects
    lines until it hits the block's opening ``● `` bullet.  Returns "" when the
    thing above the widget isn't a clean prose block — a tool result (``⎿``), a
    user prompt (``❯``), or a meta bullet (``● Bash(...)`` / ``● Ran 1 stop
    hook`` / …) — i.e. when the assistant wrote nothing before the question.
    """
    i = boundary_idx - 1
    # Skip the blank spacing rows between the prose and the widget border.
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i < 0:
        return ""

    collected: list[str] = []
    while i >= 0:
        line = lines[i]
        stripped = line.lstrip()
        # Tool-result output, or the user's own prompt → no prose here.
        if stripped.startswith("⎿") or stripped.startswith("❯"):
            return ""
        if stripped[:1] in _BULLET_CHARS:
            body = stripped[1:].strip()
            if not body or _META_BULLET_RE.match(body):
                return ""  # the bullet above is a tool call / status line
            collected.append(body)
            return "\n".join(reversed(collected)).strip()
        # A continuation line of the prose block (or a blank inside it).
        collected.append(_strip_continuation_indent(line) if line.strip() else "")
        i -= 1
        if len(collected) > 80:
            # Walked a long way without finding the opening bullet — bail rather
            # than glue unrelated transcript into the "prose".
            return ""
    return ""


def _find_widget(lines: list[str]) -> tuple[int, int | None] | None:
    """Locate the current AskUserQuestion widget, scanning bottom-up.

    Returns ``(tab_idx, footer_idx)`` — the index of the "☐ header" line that
    opens the widget body and the index of the "Enter to select …" footer (or
    ``None`` for the multi-tab form, which has no footer).  Returns ``None`` if
    no widget is recognisable.
    """
    n = len(lines)
    # The footer, if present, is near the bottom (the widget takes over the
    # screen, so the pane chrome below it is usually hidden).
    footer_idx: int | None = None
    for i in range(n - 1, max(n - 80, -1), -1):
        if _is_footer(lines[i]):
            footer_idx = i
            break

    # The "☐ header" line: first one scanning up from the footer (or from the
    # bottom).  Bottom-most occurrence = the widget currently on screen, even if
    # scrollback carries older widgets above it.
    search_from = footer_idx if footer_idx is not None else n - 1
    tab_idx: int | None = None
    for i in range(search_from, max(search_from - 80, -1), -1):
        if _TAB_LINE_RE.match(lines[i]):
            tab_idx = i
            break
    if tab_idx is None:
        return None
    return tab_idx, footer_idx


def parse_ask_question(pane_text: str) -> ParsedAskQuestion | None:
    """Parse the AskUserQuestion widget (and the prose above it) from a pane.

    ``pane_text`` should be a plain (no-ANSI) capture, ideally with scrollback so
    a long preamble isn't cut off.  Returns ``None`` when no AskUserQuestion
    widget is recognisable or it carries neither a question nor any options — the
    caller then falls back to the photo-only behaviour.
    """
    if not pane_text:
        return None
    lines = pane_text.rstrip("\n").split("\n")
    found = _find_widget(lines)
    if found is None:
        return None
    tab_idx, footer_idx = found

    # The widget's top border: the ───── line just above the "☐ header" line
    # (allow a blank or two of slack).  Prose, if any, sits above that border.
    boundary_idx = tab_idx
    j = tab_idx - 1
    while j >= 0 and j >= tab_idx - 3:
        if not lines[j].strip():
            j -= 1
            continue
        if _DASH_LINE_RE.match(lines[j].strip()):
            boundary_idx = j
        break
    prose = _extract_prose(lines, boundary_idx)

    # Body = the "☐ header" line down to (not including) the footer.
    body_end = footer_idx if footer_idx is not None else len(lines)
    body = lines[tab_idx:body_end]

    # Question text: the non-blank lines between the header line and the first
    # option row.  May wrap across two pane rows → join with a space.
    question_parts: list[str] = []
    body_iter = iter(enumerate(body))
    next(body_iter, None)  # skip body[0] — the "☐ header" line
    first_opt_pos: int | None = None
    for pos, line in body_iter:
        if _OPTION_RE.match(line):
            first_opt_pos = pos
            break
        s = line.strip()
        if s:
            question_parts.append(s)
    question = " ".join(question_parts).strip()

    # Options: each "N. Label" row, with the more-indented line(s) below it as
    # the description.  Skip ───── separators that appear inside the option list
    # (Claude Code puts one before "Chat about this").
    options: list[tuple[str, str]] = []
    cursor_label = ""
    if first_opt_pos is not None:
        cur_label: str | None = None
        cur_desc: list[str] = []
        cur_is_cursor = False

        def _flush() -> None:
            nonlocal cursor_label
            if cur_label is None:
                return
            desc = " ".join(cur_desc).strip()
            if cur_label.rstrip(".").strip().lower() not in _META_OPTION_LABELS:
                options.append((cur_label, desc))
                if cur_is_cursor:
                    cursor_label = cur_label

        for line in body[first_opt_pos:]:
            m = _OPTION_RE.match(line)
            if m:
                _flush()
                cur_label = m.group("label").strip()
                cur_desc = []
                cur_is_cursor = m.group("cursor") is not None
                continue
            s = line.strip()
            if not s or _DASH_LINE_RE.match(s):
                continue
            if cur_label is not None:
                cur_desc.append(s)
        _flush()

    if not question and not options:
        return None
    return ParsedAskQuestion(
        question=question, options=options, prose=prose, cursor_label=cursor_label
    )
