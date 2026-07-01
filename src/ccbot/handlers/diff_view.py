"""Opt-in /diff feature: send Claude Code's *native* edit-diff blocks as screenshots.

When diff mode is on for a topic (``session_manager.is_diff_mode``, default off),
the bot watches for edit-tool activity and screenshots the diff exactly as Claude
Code already drew it in the pane — the green ``●`` header, ``⎿`` stats, line
numbers, red/green backgrounds, word-level highlighting, all wrapped to the pane
width. We do **not** reconstruct the diff: capturing the pane keeps the native
style, word-highlighting, and wrapping for free, and matches what the user sees.

Flow: an edit's JSONL event (handled in ``bot.handle_new_message``) triggers
``capture_and_send`` → capture pane scrollback (ANSI) → ``extract_diff_blocks``
crops each ``● Update/Write(...)`` block → send the unseen ones as photos
(``content_type="diff"``, silent — the turn already pinged via the agent's text).

Dedup is by block content hash, per window (a block re-appears in every capture as
it scrolls). ``prime`` marks whatever's already on screen as seen when /diff is
turned on, so flipping the toggle doesn't dump the backlog.

Trade-off accepted (per design): Claude Code collapses large diffs in the pane
(``⎿ … +N lines (ctrl+o to expand)``); we screenshot that collapsed view rather
than driving ``ctrl+o`` into the live pane.

Key functions: extract_diff_blocks, capture_and_send, prime, reset.
"""

import hashlib
import logging
import re
from collections import OrderedDict

from telegram import Bot

from ..screenshot import text_to_image
from ..session import session_manager
from .message_queue import enqueue_content_message

logger = logging.getLogger(__name__)

# JSONL tool names whose activity should trigger a diff scan.
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})

# How many rows of pane scrollback to capture — deep enough that a just-rendered
# diff block is fully present even after the agent moved on to the next tool.
_SCROLLBACK_LINES = 400
# Per-window cap on remembered block hashes (LRU). Bounds memory; restart resets.
_SEEN_MAX = 300

# A pane line is a diff-block header if, with ANSI/OSC stripped, it is a tool
# bullet (● completed, ⏺ some themes) followed by an edit verb + "(". Claude Code
# renders the Edit tool as "Update(path)"; Write/Create keep their names. Prose
# replies also start with ● but never with "<verb>(" — the verb disambiguates.
_HEADER_RE = re.compile(r"^[●⏺]\s+(Update|Write|Create|Edit|MultiEdit|NotebookEdit)\(")
# A diff body line: gutter line-number followed by - or + (the changed lines).
_GUTTER_RE = re.compile(r"^\s*\d+\s*[-+]")
# Strip SGR colors and OSC-8 hyperlink wrappers to get the visible text.
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_OSC8_RE = re.compile(r"\x1b\]8;[^\x1b]*\x1b\\")

# window_id -> LRU set of seen block hashes.
_seen: dict[str, "OrderedDict[str, None]"] = {}


def _clean(line: str) -> str:
    return _SGR_RE.sub("", _OSC8_RE.sub("", line))


def extract_diff_blocks(pane: str) -> list[str]:
    """Crop each native edit-diff block out of a captured pane (ANSI preserved).

    A block runs from a ``● Update/Write(...)`` header to the first following
    blank line or next bullet. Only blocks that actually contain ``+``/``-``
    gutter lines are returned — an edit that errored (or a bare "Wrote N lines"
    confirmation) has no diff worth a screenshot.
    """
    raw = pane.split("\n")
    clean = [_clean(line) for line in raw]
    blocks: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if not _HEADER_RE.match(clean[i]):
            i += 1
            continue
        j = i + 1
        has_gutter = False
        while j < n:
            cj = clean[j]
            if cj.strip() == "" or cj.startswith(("●", "⏺")):
                break
            if _GUTTER_RE.match(cj):
                has_gutter = True
            j += 1
        if has_gutter:
            blocks.append("\n".join(raw[i:j]))
        i = j
    return blocks


def _hash(block: str) -> str:
    return hashlib.sha1(_clean(block).encode("utf-8", "ignore")).hexdigest()


def _mark(window_id: str, h: str) -> None:
    seen = _seen.setdefault(window_id, OrderedDict())
    seen[h] = None
    seen.move_to_end(h)
    while len(seen) > _SEEN_MAX:
        seen.popitem(last=False)


def _already_seen(window_id: str, h: str) -> bool:
    return h in _seen.get(window_id, ())


def reset(window_id: str) -> None:
    """Forget seen blocks for a window (on /diff off, unbind, or restart)."""
    _seen.pop(window_id, None)


async def prime(window_id: str) -> None:
    """Mark diffs already on screen as seen, so enabling /diff doesn't flush them."""
    pane = await session_manager.capture_pane(
        window_id, scrollback_lines=_SCROLLBACK_LINES, with_ansi=True
    )
    if not pane:
        return
    for block in extract_diff_blocks(pane):
        _mark(window_id, _hash(block))


async def capture_and_send(
    bot: Bot, user_id: int, window_id: str, thread_id: int | None
) -> int:
    """Capture the pane, screenshot any not-yet-sent diff blocks. Returns count sent.

    Idempotent: safe to call on every edit event (and again on the turn's final
    text) — dedup by content hash means each native block is sent exactly once.
    """
    pane = await session_manager.capture_pane(
        window_id, scrollback_lines=_SCROLLBACK_LINES, with_ansi=True
    )
    if not pane:
        return 0
    sent = 0
    for block in extract_diff_blocks(pane):
        h = _hash(block)
        if _already_seen(window_id, h):
            continue
        _mark(window_id, h)
        try:
            png = await text_to_image(block, with_ansi=True, square=False)
        except Exception:
            logger.exception("Diff block render failed (window=%s)", window_id)
            continue
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=window_id,
            parts=[],
            content_type="diff",
            thread_id=thread_id,
            image_data=[("image/png", png)],
        )
        sent += 1
    if sent:
        logger.info(
            "Sent %d diff screenshot(s) (user=%d thread=%s window=%s)",
            sent,
            user_id,
            thread_id,
            window_id,
        )
    return sent
