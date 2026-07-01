"""Pure input sanitization for the /inject endpoint — the security perimeter.

Separated from ``server`` (aiohttp plumbing) so the one security-critical
function here is unit-tested in isolation, with no socket or Docker stack.
``sanitize_inject_text`` is THE thing standing between a third-party HTTP
payload and a host shell; treat changes to it accordingly.
"""

from __future__ import annotations

import re

# Strip every C0 control char and DEL, EXCEPT newline (\x0a) — multi-line
# task prompts are legitimate. This removes, among others:
#   - ESC (\x1b)  → terminal escape / CSI sequences (cursor moves, title
#                   sets, etc.) that could rewrite the pane
#   - CR  (\x0d)  → tmux send-keys -l would treat it as a premature submit
#   - TAB (\x09)  → could trigger the TUI's tab handling (mode cycle)
#   - NUL and other C0 noise
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def sanitize_inject_text(text: str) -> str:
    """Neutralize a third-party task string so it lands as a *prompt*.

    Two defenses, applied in order:

    1. **Strip control/escape bytes** — everything ``0x00``–``0x1f`` except
       newline, plus DEL. No ESC-sequence or stray CR can reach the pane.

    2. **Defuse the TUI's leading-character commands.** A first character
       typed at an *empty* prompt is interpreted out-of-band, not as text:

         - ``!`` → bash command-mode: a shell command executed *outside*
           the LLM = host RCE for host agents. This is the critical one.
           ``send_keys`` routes a leading ``!`` there verbatim (it branches
           on ``text.startswith("!")``).
         - ``/`` → Claude Code slash command (``/clear``, ``/exit``, …):
           session control, not shell, but still not a prompt. Since the
           endpoint only fires while the agent is idle (reject-if-busy),
           the prompt is empty and a leading ``/`` *would* trigger it.

       If the cleaned text starts with either, prepend a single space: the
       first byte sent is then a space, the payload lands as an ordinary
       prompt, and the original char is preserved in the visible text.
       Only the **leading** char matters — these modes are entered solely
       from the first keystroke at an empty prompt; a ``!``/``/`` after a
       newline stays in the editor buffer, so no per-line rewriting is
       needed. (``#``/``@`` are left as-is: file-mention / memory
       affordances with no out-of-band side effect.)

    Returns the cleaned string (possibly empty after stripping — the
    server rejects an empty/blank result with 400). Without this function
    the /inject endpoint is a remote shell.
    """
    cleaned = _CONTROL_RE.sub("", text)
    if cleaned[:1] in ("!", "/"):
        cleaned = " " + cleaned
    return cleaned
