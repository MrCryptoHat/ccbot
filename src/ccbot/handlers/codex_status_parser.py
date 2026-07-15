"""Parser for Codex's ``/status`` TUI output — the runtime's session-status
command, surfaced by the panel's Context button (Codex has no ``/context``).

Like Claude's ``/context``, ``/status`` renders straight into the pane (never
the rollout JSONL), so we scrape the settled pane. Codex draws a single box::

    ╭───────────────────────────────────────────────╮
    │  >_ OpenAI Codex (v0.144.4)                    │
    │                                                │
    │ Visit https://chatgpt.com/codex/settings/usage │
    │                                                │
    │  Model:                gpt-5.5 (reasoning medium, summaries auto) │
    │  Directory:            /home/user/project      │
    │  Permissions:          Custom (workspace …)     │
    │  Account:              user@example.com (Plus) │
    │  Collaboration mode:   Default                 │
    │  Session:              0199…                    │
    │  Weekly limit:         [████████████] 99% left (resets 15:18 on 22 Jul) │
    ╰───────────────────────────────────────────────╯

Unlike Claude's ``/context`` this carries no per-category token breakdown (codex
shows "N% context left" only in the footer), so we surface the useful session
fields — model, account, permissions, collaboration mode, weekly limit.

``parse_status_output`` returns None when the pane carries no recognizable
status box (parse miss / layout changed) — the caller falls back to the photo
refresh, so the button still does something.
"""

import re

from ..i18n import tr

# Marker the poll loop waits for before trying to parse — distinctive to the
# /status box (present for any signed-in account), not codex reply prose.
STATUS_MARKER = "Account:"

# One "Label:   value" row inside the box. Value runs to the right border; the
# trailing box char + padding are stripped by the caller. Labels are matched
# case-sensitively ("Model:" — the top banner uses lowercase "model:").
_FIELD_TEMPLATE = r"{label}:\s+(?P<value>.+)"

# Weekly limit value leads with a progress bar "[████░░░] 99% left …" — drop the
# bracketed bar, keep the human part.
_BAR_RE = re.compile(r"^\[[^\]]*\]\s*")


def _field(pane_text: str, label: str) -> str | None:
    """Value of the LAST ``<label>: …`` row (freshest /status if several)."""
    matches = re.findall(_FIELD_TEMPLATE.format(label=re.escape(label)), pane_text)
    if not matches:
        return None
    # Strip the right box border + padding the capture greedily swept up.
    val = matches[-1].strip().rstrip("│").strip()
    return val or None


def parse_status_output(pane_text: str) -> dict | None:
    """Extract Codex's /status fields from a captured pane.

    Returns a dict (model / account / permissions / collaboration_mode /
    weekly_limit — any may be None), or None when the box isn't present
    (Model + Account both missing → treat as a parse miss so the photo
    fallback kicks in).
    """
    if not pane_text:
        return None
    model = _field(pane_text, "Model")
    account = _field(pane_text, "Account")
    if not model and not account:
        return None
    weekly = _field(pane_text, "Weekly limit")
    if weekly:
        weekly = _BAR_RE.sub("", weekly).strip()
    return {
        "model": model,
        "account": account,
        "permissions": _field(pane_text, "Permissions"),
        "collaboration_mode": _field(pane_text, "Collaboration mode"),
        "weekly_limit": weekly,
    }


def _tree_lines(items: list[str]) -> list[str]:
    """Render items with ├/└ tree prefixes — matches the /status tree style."""
    n = len(items)
    return [f"{'└' if i == n - 1 else '├'} {it}" for i, it in enumerate(items)]


def format_status_message(data: dict) -> str:
    """Build the Markdown body for the Telegram message."""
    lines: list[str] = [tr("csp.header"), ""]
    if data.get("model"):
        lines.append(data["model"])
        lines.append("")

    rows: list[str] = []
    if data.get("account"):
        rows.append(f"{tr('csp.account')} — {data['account']}")
    if data.get("permissions"):
        rows.append(f"{tr('csp.permissions')} — {data['permissions']}")
    if data.get("collaboration_mode"):
        rows.append(f"{tr('csp.mode')} — {data['collaboration_mode']}")
    if data.get("weekly_limit"):
        rows.append(f"{tr('csp.weekly_limit')} — {data['weekly_limit']}")
    lines.extend(_tree_lines(rows))
    return "\n".join(lines).rstrip()
