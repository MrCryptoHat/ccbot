"""Extract the plan-file name from Claude Code's ExitPlanMode widget pane.

Claude Code (v2.x) writes the proposed plan to a markdown file under
``<claude-home>/plans/`` BEFORE the approval widget is answered, and renders
the file's path in the widget footer::

    ctrl+g to edit in Vim  ·
                          ~/.claude/plans/plan-a-tiny-refactor-greedy-treasure.md

The JSONL copy of the plan (the ExitPlanMode tool_use ``input.plan``) is held
out of the transcript until the user answers — so pre-approval, that file is
the only full-text source (the screenshot crops all but the plan's tail).

Security: the pane is agent-controlled text, so only the file's **basename**
is extracted here — never a usable path. The caller resolves it against the
binding's own known plans dir (host ``~/.claude/plans`` or the docker agent's
bind-mounted claude-home); a hostile pane drawing a fake footer can therefore
never point the bot at an arbitrary host file. The basename charset is strict
and contains no ``/``, so path traversal cannot be expressed at all.

Fails open: no match → ``None`` → the caller falls back to the photo-only
behaviour (and the JSONL copy still arrives after the answer).
"""

import re

# `<anything>/plans/<basename>.md` — the basename must start with an
# alphanumeric and may contain only [A-Za-z0-9._-] (no `/`, no spaces), which
# both matches Claude Code's slug naming and makes traversal inexpressible.
_PLAN_PATH_RE = re.compile(r"[\w~./-]*/plans/([A-Za-z0-9][A-Za-z0-9._-]*\.md)\b")


def extract_plan_file_name(pane_text: str) -> str | None:
    """Return the plan file's basename from an ExitPlanMode pane, or ``None``.

    Takes the LAST match — scrollback may still show an earlier plan widget
    above the one currently on screen.
    """
    if not pane_text:
        return None
    matches = _PLAN_PATH_RE.findall(pane_text)
    return matches[-1] if matches else None
