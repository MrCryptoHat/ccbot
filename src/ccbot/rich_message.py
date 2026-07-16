"""Flatten an incoming Telegram RichMessage (Bot API 10.1+) into markdown text.

Telegram clients can now compose messages with rich blocks — tables, headings,
lists, quotes, collapsibles (Bot API 10.1, June 2026). python-telegram-bot up
to Bot API 10.0 doesn't know ``Message.rich_message``, so the raw payload
surfaces in ``message.api_kwargs`` and ``message.text`` is None — without
handling, such messages fell through to the «unsupported content» reply and
never reached the agent. :func:`flatten_rich_message` walks the block tree and
renders compact markdown the agent reads natively (a rich table becomes a pipe
table, a heading becomes ``## …``).

Defensive by design: the shapes come from a community spec, not yet from PTB —
unknown block/text nodes degrade to their concatenated inner strings, never an
exception. Media blocks (photo/video/…) inside a rich message can't be
downloaded here, so they flatten to a ``(photo: caption)`` stub.

Also hosts :func:`is_rich_safe` — the outbound gate deciding whether a given
markdown text may go through ``sendRichMessage`` at all (Telegram's own parser
mangles some constructs; details on the function).
"""

from __future__ import annotations

import re
from typing import Any

# Spec cap is 16; leave headroom but bound recursion against hostile payloads.
_MAX_DEPTH = 32

# Inline RichText wrappers rendered with markdown markers the agent knows.
_INLINE_MARKS = {
    "bold": "**",
    "italic": "*",
    "strikethrough": "~~",
    "code": "`",
    "marked": "==",
}

_MEDIA_BLOCKS = frozenset(
    {
        "photo",
        "video",
        "animation",
        "audio",
        "voice_note",
        "map",
        "collage",
        "slideshow",
    }
)


def _text(node: Any, depth: int = 0) -> str:
    """Render a RichText node (str | list | typed dict) to inline markdown."""
    if node is None or depth > _MAX_DEPTH:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_text(x, depth + 1) for x in node)
    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type")
    inner = _text(node.get("text"), depth + 1)
    mark = _INLINE_MARKS.get(kind or "")
    if mark:
        return f"{mark}{inner}{mark}" if inner else ""
    if kind == "url":
        url = node.get("url") or ""
        if url and url != inner:
            return f"[{inner}]({url})" if inner else url
        return inner or url
    if kind == "mathematical_expression":
        return str(node.get("expression") or "")
    if kind == "custom_emoji":
        return str(node.get("alternative_text") or "")
    # mention / hashtag / phone / email / spoiler / date_time / sub / sup /
    # anchor_link / reference … — the visible text already carries the content.
    return inner


def _table(block: dict) -> str:
    """RichBlockTable → markdown pipe table (agent-readable)."""
    rows = block.get("cells") or []
    lines: list[str] = []
    width = 0
    for r, row in enumerate(rows):
        if not isinstance(row, list):
            continue
        cells = []
        for cell in row:
            cell = cell if isinstance(cell, dict) else {}
            s = _text(cell.get("text")).replace("\n", " ").replace("|", "\\|").strip()
            cells.append(s)
        width = max(width, len(cells))
        lines.append("| " + " | ".join(cells) + " |")
        if r == 0:
            # Markdown requires a header separator; emit one after the first
            # row regardless of is_header so the table stays parseable.
            lines.append("|" + "---|" * max(width, 1))
    caption = _text(block.get("caption"))
    if caption:
        lines.append(f"_{caption}_")
    return "\n".join(lines)


def _list(block: dict, depth: int) -> str:
    lines: list[str] = []
    for i, item in enumerate(block.get("items") or [], 1):
        if not isinstance(item, dict):
            continue
        if item.get("has_checkbox"):
            prefix = "- [x]" if item.get("is_checked") else "- [ ]"
        else:
            label = str(item.get("label") or "").strip()
            prefix = label if label else f"{i}."
            if not item.get("type") and not label:
                prefix = "-"
        body_parts: list[str] = []
        for sub in item.get("blocks") or []:
            _block(sub, body_parts, depth + 1)
        body = " ".join(p.replace("\n", " ") for p in body_parts if p.strip())
        lines.append(f"{prefix} {body}".rstrip())
    return "\n".join(lines)


def _quote(parts: list[str], credit: str) -> str:
    quoted = "\n".join(
        "> " + line for part in parts for line in part.splitlines() if part.strip()
    )
    if credit:
        quoted += f"\n> — {credit}"
    return quoted


def _block(block: Any, out: list[str], depth: int = 0) -> None:
    """Render one RichBlock into ``out`` (one markdown chunk per block)."""
    if depth > _MAX_DEPTH:
        return
    if not isinstance(block, dict):
        s = _text(block, depth)
        if s.strip():
            out.append(s)
        return

    kind = block.get("type")
    if kind in ("paragraph", "footer"):
        s = _text(block.get("text"), depth)
        if s.strip():
            out.append(s)
    elif kind == "heading":
        try:
            size = min(max(int(block.get("size") or 2), 1), 6)
        except (TypeError, ValueError):
            size = 2
        out.append(f"{'#' * size} {_text(block.get('text'), depth)}")
    elif kind == "pre":
        lang = str(block.get("language") or "")
        out.append(f"```{lang}\n{_text(block.get('text'), depth)}\n```")
    elif kind == "divider":
        out.append("---")
    elif kind == "anchor":
        pass  # invisible navigation marker
    elif kind == "mathematical_expression":
        expr = str(block.get("expression") or "")
        if expr:
            out.append(f"$$ {expr} $$")
    elif kind == "table":
        out.append(_table(block))
    elif kind == "list":
        out.append(_list(block, depth))
    elif kind in ("blockquote", "pullquote", "details"):
        parts: list[str] = []
        if kind == "pullquote":
            parts.append(_text(block.get("text"), depth))
        for sub in block.get("blocks") or []:
            _block(sub, parts, depth + 1)
        if kind == "details":
            summary = _text(block.get("summary"), depth)
            body = "\n\n".join(p for p in parts if p.strip())
            out.append(f"{summary}\n{body}".strip())
        else:
            out.append(_quote(parts, _text(block.get("credit"), depth)))
    elif kind in _MEDIA_BLOCKS:
        cap = block.get("caption")
        cap_text = _text(cap.get("text"), depth) if isinstance(cap, dict) else ""
        out.append(f"({kind}: {cap_text})" if cap_text else f"({kind})")
    else:
        # Unknown block — salvage its text/children rather than dropping it.
        s = _text(block.get("text"), depth)
        if s.strip():
            out.append(s)
        for sub in block.get("blocks") or []:
            _block(sub, out, depth + 1)


# Outbound safety gate. Telegram's rich-markdown parser is NOT CommonMark:
# an emphasis marker (* or _) INSIDE an inline code span is taken as an
# emphasis delimiter, which consumes one backtick — every later code span
# then pairs shifted by one, turning whole prose stretches into monospace
# and merging words where span-edge spaces get trimmed (verified live on a
# `*.json` glob inside a code span). Fenced blocks are parsed at block level
# and are fine, so they're stripped before the scan.
_FENCED_BLOCK_RE = re.compile(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", re.M | re.S)
_RISKY_INLINE_CODE_RE = re.compile(r"`[^`\n]*[*_][^`\n]*`")


def is_rich_safe(markdown: str) -> bool:
    """True when *markdown* has no construct known to desync Telegram's parser.

    False → the caller must keep the legacy MarkdownV2 path (which escapes
    everything itself and renders these texts correctly).
    """
    prose = _FENCED_BLOCK_RE.sub("", markdown)
    return _RISKY_INLINE_CODE_RE.search(prose) is None


def flatten_rich_message(rich: Any) -> str:
    """RichMessage dict (from ``message.api_kwargs``) → markdown text."""
    if not isinstance(rich, dict):
        return ""
    out: list[str] = []
    for block in rich.get("blocks") or []:
        _block(block, out)
    return "\n\n".join(p for p in out if p and p.strip()).strip()
