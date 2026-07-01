"""Pure URL helpers: pull http(s) links out of text and format them for chat.

Used to surface links *separately* in Telegram so they are easy to tap even
when the inline copy is word-wrapped or cropped:
  - every agent text reply (message_queue) — a compact "🔗 Ссылки" list;
  - the Claude Code `/login` screen (interactive_ui) — the full OAuth URL,
    via the dedicated reconstruction in terminal_parser.parse_login_url.

All functions are pure and unit-tested (tests/ccbot/test_links.py).
"""

import re

from .i18n import tr

# http/https URLs. We stop the match at whitespace and at the bracket/quote
# characters that hug a URL in prose or markdown (`[label](url)`, "url", `url`),
# then trim trailing sentence punctuation below. This deliberately excludes
# `()` from the URL body so a markdown link's closing paren isn't swallowed —
# the rare URL that genuinely contains parens loses its tail, an acceptable
# trade for never mangling the common `[…](…)` case.
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}`'\"]+", re.IGNORECASE)

# Punctuation that commonly trails a URL but isn't part of it.
_TRAILING_PUNCT = ".,;:!?…"

# Cap the rendered list so a reply full of links can't paginate.
MAX_LINKS = 20

# Display-label length before we ellipsize (scheme already stripped).
_LABEL_MAX = 60


def extract_urls(text: str) -> list[str]:
    """Return the http(s) URLs in ``text``, de-duplicated in first-seen order.

    Trailing sentence punctuation is trimmed; a URL must have a dot in its host
    (so bare ``https://`` and dotless hosts like ``localhost`` are dropped).
    Order is preserved so the surfaced list reads top-to-bottom the way the
    links appear in the reply.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(_TRAILING_PUNCT)
        # Require a dot after the scheme — rejects bare "https://" and dotless
        # hosts (localhost, intranet names) that aren't useful as tap targets.
        if "." not in url.split("://", 1)[-1]:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def shorten_url(url: str, max_len: int = _LABEL_MAX) -> str:
    """A readable label for a link: scheme dropped, ellipsized if long."""
    display = re.sub(r"^https?://", "", url)
    if len(display) <= max_len:
        return display
    return display[: max_len - 1] + "…"


def format_links_block(urls: list[str], header: str | None = None) -> str:
    """Render a markdown bullet list of clickable links (or "" if none).

    Output is plain markdown (``• [label](url)``) — the send layer's
    ``convert_markdown`` turns it into clickable MarkdownV2. Labels and URLs
    are free of ``[]()`` by construction (``extract_urls`` excludes them), so
    the link syntax can't be broken by the content.
    """
    if not urls:
        return ""
    if header is None:
        header = tr("links.header")
    lines = [f"{header}:"]
    shown = urls[:MAX_LINKS]
    for url in shown:
        lines.append(f"• [{shorten_url(url)}]({url})")
    extra = len(urls) - len(shown)
    if extra > 0:
        lines.append(tr("links.overflow", extra=extra))
    return "\n".join(lines)
