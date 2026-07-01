"""Parser for Claude Code's `/context` TUI output.

/context is a client-side picker — its output is rendered directly into
the tmux pane via ANSI escape codes and never reaches the JSONL stream
that SessionMonitor watches. To surface it as a normal Telegram chat
message we capture the pane after the slash command settles and parse
the rendered text here.

The parsing is regex-based against a known stable layout:

    Context Usage
    [chart]  Opus 4.7 (1M context)
             claude-opus-4-7[1m]
             185.5k/1m tokens (19%)

             Estimated usage by category
             ⛁ System prompt: 8.6k tokens (0.9%)
             ⛁ System tools: 11.7k tokens (1.2%)
             ⛁ Memory files: 31.4k tokens (3.1%)
             ⛁ Skills: 808 tokens (0.1%)
             ⛁ Messages: 132.9k tokens (13.3%)
             ⛶ Free space: 814.5k (81.5%)

    MCP tools · /mcp (loaded on-demand)
    …

    Memory files · /memory
    ├ ~/CLAUDE.md: 2.6k tokens
    ├ CLAUDE.md: 10.4k tokens
    …

`parse_context_output` returns None when the pane doesn't actually carry
a Context Usage block (e.g. /context never reached the TUI, or Claude
Code's layout changed and our regex misses) — the caller falls back to
photo-refresh, so the button still does something useful.
"""

import re

from ..i18n import tr

CATEGORY_NAMES = (
    "System prompt",
    "System tools",
    "Memory files",
    "Skills",
    "Messages",
    "Free space",
)

# Lines like "⛁ System prompt: 8.6k tokens (0.9%)" or "⛶ Free space: 814.5k (81.5%)".
# Token unit can be "k"/"m" or absent ("808 tokens"). "tokens" word is
# optional — Free space drops it.
_CATEGORY_RE = re.compile(
    r"(?P<name>"
    + "|".join(re.escape(n) for n in CATEGORY_NAMES)
    + r"):\s+(?P<tokens>\S+)(?:\s+tokens)?\s+\((?P<pct>[\d.]+%)\)"
)

# Total line: "185.5k/1m tokens (19%)".
_TOTAL_RE = re.compile(r"(?P<used>\S+?)/(?P<total>\S+?)\s+tokens\s+\((?P<pct>\d+%)\)")

# Model header line: "Opus 4.7 (1M context)" — the rendered chart sits
# to the left; only the rightmost text matters here.
_MODEL_RE = re.compile(r"((?:Opus|Sonnet|Haiku)[^()]*\([^)]*context\))")

# Memory files items: "├ <path>: <tokens> tokens" or "└ ...". Path may
# contain dots, slashes, dashes — anything except colon.
_MEMORY_ITEM_RE = re.compile(r"[├└]\s+(?P<path>[^:]+?):\s+(?P<tokens>\S+)\s+tokens")


def parse_context_output(pane_text: str) -> dict | None:
    """Extract the Context Usage block from a captured pane.

    Returns a dict with `model`, `total` (used, total, pct), `categories`
    (list of name/tokens/pct), and `memory_files` (list of path/tokens).
    Returns None if the pane doesn't carry a recognizable Context Usage
    block — the caller should fall back to its default behavior.

    With a large scrollback window the same pane can carry several past
    /context invocations stacked above one another — we slice from the
    *last* `Context Usage` header so only the freshest invocation is
    parsed and categories don't get duplicated."""
    last_marker = pane_text.rfind("Context Usage")
    if last_marker < 0:
        return None
    pane_text = pane_text[last_marker:]

    model_match = _MODEL_RE.search(pane_text)
    total_match = _TOTAL_RE.search(pane_text)
    categories = [
        {"name": m.group("name"), "tokens": m.group("tokens"), "pct": m.group("pct")}
        for m in _CATEGORY_RE.finditer(pane_text)
    ]
    if not categories:
        # Without a category breakdown the message would be near-empty;
        # treat as a parse miss so the photo-refresh fallback kicks in.
        return None

    # Memory files section — find the heading, then scan items below
    # until we hit a blank line or a different section heading.
    memory_files: list[dict] = []
    in_memory_section = False
    for line in pane_text.splitlines():
        stripped = line.strip()
        if "Memory files" in stripped and "/memory" in stripped:
            in_memory_section = True
            continue
        if not in_memory_section:
            continue
        if not stripped:
            # Blank line ends the section.
            if memory_files:
                break
            continue
        # Next major section ("Skills · /skills", "MCP tools · /mcp" …)
        # has " · /" but no tree prefix.
        if " · /" in stripped and not stripped.startswith(("├", "└")):
            break
        m = _MEMORY_ITEM_RE.search(line)
        if m:
            memory_files.append(
                {"path": m.group("path").strip(), "tokens": m.group("tokens")}
            )

    return {
        "model": model_match.group(1) if model_match else None,
        "total": (
            {
                "used": total_match.group("used"),
                "total": total_match.group("total"),
                "pct": total_match.group("pct"),
            }
            if total_match
            else None
        ),
        "categories": categories,
        "memory_files": memory_files,
    }


def _tree_lines(items: list[str]) -> list[str]:
    """Render items with ├/└ tree prefixes — matches /status visual style."""
    n = len(items)
    return [f"{'└' if i == n - 1 else '├'} {it}" for i, it in enumerate(items)]


def _shorten_path(path: str, maxlen: int = 48) -> str:
    """Trim long memory paths from the middle: '~/.claude/.../MEMORY.md'."""
    if len(path) <= maxlen:
        return path
    head = path[:20]
    tail = path[-(maxlen - 20 - 5) :]
    return f"{head}…{tail}"


def format_context_message(data: dict) -> str:
    """Build the Markdown body for the Telegram message."""
    lines: list[str] = []

    pct = data["total"]["pct"] if data.get("total") else ""
    header = "📊 Context"
    if pct:
        header += f" · {pct}"
    lines.append(header)
    lines.append("")

    if data.get("model"):
        lines.append(data["model"])
    if data.get("total"):
        t = data["total"]
        lines.append(f"{t['used']} / {t['total']} tokens")
    lines.append("")

    if data.get("categories"):
        lines.append(tr("ctxp.categories"))
        cat_items = [
            f"{c['name']} — {c['tokens']} ({c['pct']})" for c in data["categories"]
        ]
        lines.extend(_tree_lines(cat_items))

    memory = data.get("memory_files") or []
    if memory:
        # Find the corresponding category total to put in the heading —
        # gives a quick "Memory files (31.4k):" anchor before the list.
        memory_total = next(
            (c["tokens"] for c in data["categories"] if c["name"] == "Memory files"),
            None,
        )
        lines.append("")
        heading = "Memory files"
        if memory_total:
            heading += f" ({memory_total})"
        lines.append(heading + ":")
        mem_items = [
            f"{_shorten_path(item['path'])} — {item['tokens']}" for item in memory
        ]
        lines.extend(_tree_lines(mem_items))

    return "\n".join(lines)
