"""Markdown → Telegram MarkdownV2 conversion layer.

Wraps `telegramify_markdown` and adds special handling for expandable
blockquotes (delimited by sentinel tokens from TranscriptParser).
Expandable quotes are escaped and formatted as Telegram >…|| syntax
separately, so the library doesn't mangle them.

Key functions:
  - convert_markdown(text) → MarkdownV2 string (the send-layer converter).
  - render_tables_for_chat(text) → (text, images, files): extracts blocks a
    phone can't render in a chat bubble — wide tables / box-art → image,
    long code → file attachment — leaving NUL placeholders the send layer
    fills in source order (see message-handling.md "Block rendering").
"""

import re
from dataclasses import dataclass

import mistletoe
from mistletoe.block_token import BlockCode, remove_token
from telegramify_markdown import _update_block, escape_latex
from telegramify_markdown.render import TelegramMarkdownRenderer

from .transcript_parser import TranscriptParser

_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")


def _split_table_row(line: str) -> list[str]:
    """Split a table row by pipes, respecting escaped pipes (\\|)."""
    content = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", content)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def _align_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Space-align a parsed table into monospace lines (no code fence).

    Columns are padded to a common width so they line up under a monospace
    font; the last column carries no trailing padding.
    """
    n_cols = len(headers)
    widths = [0] * n_cols
    for row in (headers, *rows):
        for j in range(n_cols):
            cell = row[j] if j < len(row) else ""
            widths[j] = max(widths[j], len(cell))

    def fmt(row: list[str]) -> str:
        cells = [
            (row[j] if j < len(row) else "").ljust(widths[j]) for j in range(n_cols)
        ]
        return "  ".join(cells).rstrip()

    return [fmt(headers), *[fmt(row) for row in rows]]


def _format_table_block(headers: list[str], rows: list[list[str]]) -> str:
    """Render a parsed table as a fenced monospace code block."""
    return "```\n" + "\n".join(_align_table(headers, rows)) + "\n```"


def _format_table_grid(headers: list[str], rows: list[list[str]]) -> str:
    """Render a parsed table with box-drawing borders.

    Used for the image path (wide tables): the grid reads as a "real" table
    — the same look Claude Code draws in the terminal — once it's a picture
    that doesn't have to fit a phone's code-block width.
    """
    n_cols = len(headers)
    widths = [0] * n_cols
    for row in (headers, *rows):
        for j in range(n_cols):
            cell = row[j] if j < len(row) else ""
            widths[j] = max(widths[j], len(cell))

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def row_line(row: list[str]) -> str:
        cells = [
            (row[j] if j < len(row) else "").ljust(widths[j]) for j in range(n_cols)
        ]
        return "│ " + " │ ".join(cells) + " │"

    lines = [border("┌", "┬", "┐"), row_line(headers), border("├", "┼", "┤")]
    lines.extend(row_line(row) for row in rows)
    lines.append(border("└", "┴", "┘"))
    return "\n".join(lines)


@dataclass
class _TextSpan:
    text: str


@dataclass
class _TableSpan:
    headers: list[str]
    rows: list[list[str]]


@dataclass
class _CodeSpan:
    fence: str  # the opening fence line verbatim (indent + ```lang)
    inner: list[str]
    closing: str | None  # closing fence line, or None if the block was unclosed


_Block = _TextSpan | _TableSpan | _CodeSpan

# Box-drawing characters (trees, ASCII-art diagrams). NOTE: the ASCII pipe "|"
# is deliberately NOT here — a Python `a | b` must not look like box-art.
_BOX_ART_CHARS = frozenset("│─├└┌┐┘┴┬┼╭╮╰╯═║╔╗╚╝╠╣╦╩╬")


def _is_box_art(lines: list[str]) -> bool:
    """True if any line carries a box-drawing glyph (tree / diagram)."""
    return any(ch in _BOX_ART_CHARS for ch in "".join(lines))


def _reconstruct_code(block: _CodeSpan) -> str:
    """Re-emit a fenced code block verbatim (idempotent round-trip)."""
    parts = [block.fence, *block.inner]
    if block.closing is not None:
        parts.append(block.closing)
    return "\n".join(parts)


def _iter_table_blocks(text: str) -> list[_Block]:
    """Split text into ordered text-spans, pipe-tables, and fenced code blocks.

    Fenced code blocks are emitted as their own span so the chat path can
    decide whether wide box-art inside one should become an image; the
    idempotent pass re-emits them verbatim.
    """
    lines = text.split("\n")
    out: list[_Block] = []
    buf: list[str] = []
    i = 0

    def flush() -> None:
        if buf:
            out.append(_TextSpan("\n".join(buf)))
            buf.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            # Consume the whole fenced block as one span.
            flush()
            fence = line
            inner: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                inner.append(lines[i])
                i += 1
            if i < len(lines):
                closing = lines[i]
                i += 1
            else:
                closing = None  # unclosed fence (malformed input)
            out.append(_CodeSpan(fence, inner, closing))
            continue

        # Table header row, followed by a separator (---|---|---)
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and "|" in stripped[1:-1]
            and i + 1 < len(lines)
        ):
            sep_line = lines[i + 1].strip()
            if sep_line.startswith("|") and _TABLE_SEP_RE.match(sep_line):
                headers = _split_table_row(stripped)
                i += 2  # skip header + separator
                rows: list[list[str]] = []
                while i < len(lines):
                    data_line = lines[i].strip()
                    if data_line.startswith("|") and data_line.endswith("|"):
                        rows.append(_split_table_row(data_line))
                        i += 1
                    else:
                        break
                flush()
                out.append(_TableSpan(headers, rows))
                continue

        buf.append(line)
        i += 1

    flush()
    return out


def convert_markdown_tables(text: str) -> str:
    """Inline every markdown table as a space-aligned monospace code block.

    Telegram has no table rendering; a fenced code block keeps columns
    aligned. Skips tables inside code blocks (idempotent — a second pass
    sees the fence and leaves it alone). Used by the idempotent send-layer
    pass; the chat send path uses ``render_tables_for_chat`` instead.
    """
    if "|" not in text:
        return text
    out: list[str] = []
    for block in _iter_table_blocks(text):
        if isinstance(block, _TextSpan):
            out.append(block.text)
        elif isinstance(block, _CodeSpan):
            out.append(_reconstruct_code(block))
        else:
            out.append(_format_table_block(block.headers, block.rows))
    return "\n".join(out)


# Wide tables wrap into unreadable soup inside a phone's narrow code block,
# so anything wider than this (chars in the longest aligned line) is rendered
# as a PNG by the send layer. Narrow tables stay copyable code blocks.
TABLE_IMAGE_MIN_WIDTH = 34

# A code block this long would be paginated into several messages, each with
# its own re-opened fence + [k/N] suffix — impossible to copy as one clean
# block. Past either bound it's sent as a file attachment instead (copies /
# saves whole). Short code stays inline where copy already works fine.
CODE_FILE_MAX_CHARS = 2500
CODE_FILE_MAX_LINES = 50

# Fence language → file extension for code sent as an attachment.
_CODE_LANG_EXT = {
    "python": "py",
    "py": "py",
    "javascript": "js",
    "js": "js",
    "typescript": "ts",
    "ts": "ts",
    "bash": "sh",
    "sh": "sh",
    "shell": "sh",
    "zsh": "sh",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "html": "html",
    "css": "css",
    "sql": "sql",
    "go": "go",
    "rust": "rs",
    "rs": "rs",
    "c": "c",
    "cpp": "cpp",
    "java": "java",
    "ruby": "rb",
    "rb": "rb",
    "php": "php",
    "markdown": "md",
    "md": "md",
    "diff": "diff",
}

# Placeholders left in the text for blocks rendered out-of-band by the send
# layer (image / file attachment), in source order. NUL-delimited so they can
# never collide with real content, single short lines so split_message keeps
# them intact across pagination.
_IMG_PLACEHOLDER = "\x00CCBOT_IMG:{}\x00"
_FILE_PLACEHOLDER = "\x00CCBOT_FILE:{}\x00"
PLACEHOLDER_RE = re.compile("\x00CCBOT_(IMG|FILE):(\\d+)\x00")


def _code_filename(fence: str) -> str:
    """Derive an attachment filename from a code fence's language tag."""
    lang = fence.strip().lstrip("`").strip().lower()
    return f"snippet.{_CODE_LANG_EXT.get(lang, 'txt')}"


def render_tables_for_chat(
    text: str,
) -> tuple[str, list[str], list[tuple[str, str]]]:
    """Prepare markdown tables, wide box-art, and long code for chat delivery.

    - Narrow tables (<= TABLE_IMAGE_MIN_WIDTH) → inline monospace code block.
    - Wide tables → bordered-grid image (placeholder + collected text).
    - Wide box-art code blocks (trees, diagrams with ├└│─) → image, verbatim
      (it's already drawn — no grid wrapping). Narrow ones stay code blocks.
    - Long code blocks (would paginate) → file attachment, so they copy/save
      whole instead of arriving as [k/N]-split fragments.
    - Short plain code blocks are left inline (copy already works there).

    Returns ``(text_with_placeholders, images, files)`` where ``images`` are
    aligned texts to render as PNGs and ``files`` are ``(filename, content)``
    pairs to send as documents — both referenced by placeholders in source
    order.
    """
    # Fast path: nothing to extract. Box-art uses │ (U+2502), not ASCII |.
    if (
        "|" not in text
        and "```" not in text
        and not any(ch in _BOX_ART_CHARS for ch in text)
    ):
        return text, [], []
    out: list[str] = []
    images: list[str] = []
    files: list[tuple[str, str]] = []
    for block in _iter_table_blocks(text):
        if isinstance(block, _TextSpan):
            out.append(block.text)
        elif isinstance(block, _CodeSpan):
            width = max((len(line) for line in block.inner), default=0)
            content = "\n".join(block.inner)
            if _is_box_art(block.inner) and width > TABLE_IMAGE_MIN_WIDTH:
                # Wide tree/diagram → image (already aligned, render as-is).
                out.append(_IMG_PLACEHOLDER.format(len(images)))
                images.append(content)
            elif (
                len(content) > CODE_FILE_MAX_CHARS
                or len(block.inner) > CODE_FILE_MAX_LINES
            ):
                # Long code → file attachment (copies/saves whole).
                out.append(_FILE_PLACEHOLDER.format(len(files)))
                files.append((_code_filename(block.fence), content))
            else:
                out.append(_reconstruct_code(block))
        else:
            aligned = _align_table(block.headers, block.rows)
            width = max((len(line) for line in aligned), default=0)
            if width <= TABLE_IMAGE_MIN_WIDTH:
                # Narrow: copyable inline code block (plain space-aligned).
                out.append("```\n" + "\n".join(aligned) + "\n```")
            else:
                # Wide: rendered to a bordered-grid image by the send layer.
                out.append(_IMG_PLACEHOLDER.format(len(images)))
                images.append(_format_table_grid(block.headers, block.rows))
    return "\n".join(out), images, files


_EXPQUOTE_RE = re.compile(
    re.escape(TranscriptParser.EXPANDABLE_QUOTE_START)
    + r"([\s\S]*?)"
    + re.escape(TranscriptParser.EXPANDABLE_QUOTE_END)
)

# Characters that must be escaped in Telegram MarkdownV2 plain text
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_mdv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


# Max rendered chars for a single expandable quote block.
# Leaves room for surrounding text within Telegram's 4096 char message limit.
_EXPQUOTE_MAX_RENDERED = 3800


def _render_expandable_quote(m: re.Match[str]) -> str:
    """Render an expandable blockquote block in raw MarkdownV2.

    Truncates the rendered output to _EXPQUOTE_MAX_RENDERED chars
    to ensure the final message fits within Telegram's 4096 limit.
    """
    inner = m.group(1)
    escaped = _escape_mdv2(inner)
    lines = escaped.split("\n")
    # Build quoted lines, truncating if needed to stay within budget
    built: list[str] = []
    total_len = 0
    suffix = "\n>… \\(truncated\\)||"
    budget = _EXPQUOTE_MAX_RENDERED - len(suffix)
    truncated = False
    for line in lines:
        # +1 for ">" prefix, +1 for "\n" separator
        line_cost = 1 + len(line) + 1
        if total_len + line_cost > budget:
            # Try to fit a partial line
            remaining = budget - total_len - 2  # -2 for ">" and "\n"
            if remaining > 20:
                built.append(f">{line[:remaining]}")
            truncated = True
            break
        built.append(f">{line}")
        total_len += line_cost
    if truncated:
        return "\n".join(built) + suffix
    return "\n".join(built) + "||"


def _markdownify(text: str) -> str:
    """Custom markdownify with our rendering rules.

    Wraps TelegramMarkdownRenderer directly (instead of calling
    telegramify_markdown.markdownify) so we can tweak token rules
    inside the context manager — reset_tokens() in __exit__ would
    otherwise undo any module-level changes.

    Custom rules:
      - Disable indented code blocks (only fenced ``` blocks are code).
    """
    with TelegramMarkdownRenderer(normalize_whitespace=False) as renderer:
        remove_token(BlockCode)
        content = escape_latex(text)
        document = mistletoe.Document(content)
        _update_block(document)
        return renderer.render(document)


def convert_markdown(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format.

    Expandable blockquote sections (marked by sentinel tokens from
    TranscriptParser) are extracted, escaped, and formatted separately
    so that telegramify_markdown doesn't mangle the >...|| syntax.
    """
    # Convert markdown tables to a monospace code block before telegramify
    text = convert_markdown_tables(text)

    # Extract expandable quote blocks before telegramify
    segments: list[tuple[bool, str]] = []  # (is_quote, content)
    last_end = 0
    for m in _EXPQUOTE_RE.finditer(text):
        if m.start() > last_end:
            segments.append((False, text[last_end : m.start()]))
        segments.append((True, m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        segments.append((False, text[last_end:]))

    if not segments:
        return _markdownify(text)

    parts: list[str] = []
    for is_quote, segment in segments:
        if is_quote:
            parts.append(_EXPQUOTE_RE.sub(_render_expandable_quote, segment))
        else:
            parts.append(_markdownify(segment))
    return "".join(parts)
