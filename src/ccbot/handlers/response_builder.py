"""Response message building for Telegram delivery.

Builds paginated response messages from Claude Code output:
  - Handles different content types (text, thinking, tool_use, tool_result)
  - Splits long messages into pages within Telegram's 4096 char limit
  - Truncates thinking content to keep messages compact

Markdown conversion is NOT done here — the send layer (message_sender,
message_queue) handles convert_markdown() so each message is converted
exactly once.

Key function:
  - build_response_parts: Build paginated response messages
"""

from ..markdown_v2 import render_tables_for_chat
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser


def build_response_parts(
    text: str,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Build paginated response messages for Telegram.

    Returns ``(parts, images, files)``: ``parts`` are raw markdown strings,
    each within Telegram's 4096 char limit (multi-part messages get a [1/N]
    suffix); ``images`` are aligned monospace strings the send layer renders
    as PNGs (wide tables / box-art) and ``files`` are ``(filename, content)``
    pairs sent as documents (long code) — both referenced by placeholder
    lines embedded in ``parts``. Markdown→MarkdownV2 conversion is done by
    the send layer, not here.
    """
    text = text.strip()

    # User messages: add emoji prefix (no newline)
    if role == "user":
        prefix = "👤 "
        separator = ""
        # User messages are typically short, no special processing needed
        if len(text) > 3000:
            text = text[:3000] + "…"
        return [f"{prefix}{text}"], [], []

    # Truncate thinking content to keep it compact
    if content_type == "thinking" and is_complete:
        start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
        end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
        max_thinking = 500
        if start_tag in text and end_tag in text:
            inner = text[text.index(start_tag) + len(start_tag) : text.index(end_tag)]
            if len(inner) > max_thinking:
                inner = inner[:max_thinking] + "\n\n… (thinking truncated)"
            text = start_tag + inner + end_tag
        elif len(text) > max_thinking:
            text = text[:max_thinking] + "\n\n… (thinking truncated)"

    # Format based on content type
    if content_type == "thinking":
        # Thinking: prefix with "∴ Thinking…" and single newline
        prefix = "∴ Thinking…"
        separator = "\n"
    else:
        # Plain text: no prefix
        prefix = ""
        separator = ""

    # If text contains expandable quote sentinels, don't split —
    # the quote must stay atomic. Truncation is handled by
    # _render_expandable_quote in markdown_v2.py.
    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
        if prefix:
            return [f"{prefix}{separator}{text}"], [], []
        return [text], [], []

    # Tables/box-art → image placeholders, long code → file placeholders;
    # narrow tables stay inline code blocks. Done before splitting so a block
    # isn't broken across messages — the send layer's convert_markdown() table
    # pass is idempotent.
    text, images, files = render_tables_for_chat(text)

    # Split first, then assemble each chunk.
    # Use conservative max to leave room for MarkdownV2 expansion at send layer.
    max_text = 3000 - len(prefix) - len(separator)

    text_chunks = split_message(text, max_length=max_text)
    total = len(text_chunks)

    if total == 1:
        if prefix:
            return [f"{prefix}{separator}{text_chunks[0]}"], images, files
        return [text_chunks[0]], images, files

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        if prefix:
            parts.append(f"{prefix}{separator}{chunk}\n\n[{i}/{total}]")
        else:
            parts.append(f"{chunk}\n\n[{i}/{total}]")
    return parts, images, files
