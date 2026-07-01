"""Auto-route plain text into the "Type something." option of an
AskUserQuestion TUI.

When Claude Code shows AskUserQuestion with options like:

    1. Foo
    2. Bar
    ...
    N. Type something.

— pressing the digit N jumps the cursor to the "Type something." line,
which is an inline text-input field (the placeholder gets replaced by
typed characters). Pressing Enter then submits the typed text as a
free-form answer (recorded in JSONL as ``User answered → <text>``).

Without this router, plain text from Telegram lands in option-selection
mode: characters are filtered/discarded and the trailing Enter selects
the highlighted option (usually the first one), so the user's intended
free-text answer is silently lost and Claude sees a misleading "yes to
the default" answer.

Discovered via direct ``tmux send-keys`` experiments on a live
AskUserQuestion TUI, see CLAUDE.md history for the trace.
"""

import asyncio
import logging
import re

from ..session import session_manager
from ..terminal_parser import extract_interactive_content, is_interactive_ui

logger = logging.getLogger(__name__)

# Matches the "N. Type something." line. The "❯" cursor may or may not
# be present depending on which option is currently highlighted; the
# digit before the dot is what we need either way.
_TYPE_SOMETHING_RE = re.compile(
    r"^\s*[❯>]?\s*(\d+)\.\s+Type something\.\s*$",
    re.MULTILINE,
)


async def try_route_to_text_option(wid: str, text: str) -> tuple[bool, str | None]:
    """Try to deliver ``text`` as a free-form AskUserQuestion answer.

    Returns:
        (True, None)              — text was routed and submitted
        (False, "no_text_option") — AskUserQuestion is up but has no
                                    "Type something." option (caller
                                    should block and warn the user)
        (False, "blocking_widget:<name>")
                                  — a different interactive widget
                                    (PermissionPrompt, ExitPlanMode,
                                    model picker, …) is on screen.
                                    Caller must NOT send plain text:
                                    the TUI discards the characters and
                                    the trailing Enter silently
                                    activates the highlighted option —
                                    e.g. grants a permission the user
                                    never saw.
        (False, None)             — no interactive UI, OR the OAuth login
                                    prompt (a live text field); caller
                                    should proceed with normal send
    """
    pane = await session_manager.capture_pane(wid)
    if not pane:
        return False, None

    if not is_interactive_ui(pane):
        return False, None

    content = extract_interactive_content(pane)

    # The OAuth login screen ("Paste code here if prompted >") is detected as
    # an interactive widget, but its prompt is a live text-input field, not an
    # option menu — typing the auth code + Enter via the normal send path
    # pastes it correctly. Fall through so the caller delivers it verbatim
    # instead of refusing it as a blocking widget (the bug: pasted login codes
    # were silently dropped).
    if content and content.name == "LoginPrompt":
        return False, None

    if not content or content.name != "AskUserQuestion":
        # Fail safe when the widget didn't parse (content is None):
        # better to withhold one message than to confirm a dialog blind.
        name = content.name if content and content.name else "unknown"
        return False, f"blocking_widget:{name}"

    match = _TYPE_SOMETHING_RE.search(pane)
    if not match:
        return False, "no_text_option"

    digit = match.group(1)

    # 1) Digit shortcut moves the cursor onto "Type something." and
    #    switches Claude's TUI into the inline text-input mode for that
    #    option.
    await session_manager.send_keys(wid, digit, enter=False, literal=True)
    # Tiny gap so Claude finishes the mode switch before the text bytes
    # arrive — without it a leading-digit text could occasionally still
    # be interpreted as another option-jump shortcut.
    await asyncio.sleep(0.05)

    # 2) Type the user's text and submit with Enter.
    await session_manager.send_keys(wid, text, enter=True, literal=True)

    logger.info(
        "AskUserQuestion routed via digit shortcut: wid=%s digit=%s text_len=%d",
        wid,
        digit,
        len(text),
    )
    return True, None
