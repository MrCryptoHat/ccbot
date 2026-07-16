"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread
  - For AskUserQuestion: surfacing the agent's preceding prose + the question
    text as one readable message *before* the photo (parsed from the pane via
    askquestion_parser, since JSONL holds the whole turn until the user answers;
    the answer options stay on the screenshot only), then de-duplicating the
    JSONL copies that arrive after the answer — upgrading the surfaced message in
    place to the clean markdown prose (`consume_pending_prose_upgrade`) and
    skipping the `**AskUserQuestion**(…)` tool_use message
    (`consume_pending_ask_tool_use`) — see `_surface_ask_question_text`.
  - For ExitPlanMode: surfacing the FULL plan text *before* approval by reading
    the plan file whose basename the widget footer shows (Claude Code writes
    `<claude-home>/plans/<slug>.md` before asking; the JSONL copy is held until
    the user answers, and the screenshot crops all but the plan's tail), then
    skipping that JSONL copy after the answer (`consume_pending_plan_text`) —
    see `_surface_plan_text`.

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import asyncio
import io
import logging
import re
import time
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest

from ..i18n import tr
from ..markdown_v2 import PLACEHOLDER_RE, render_tables_for_chat
from ..screenshot import text_to_image
from ..session import session_manager
from ..telegram_sender import split_message
from ..terminal_parser import (
    extract_interactive_content,
    parse_login_code,
    parse_login_url,
    strip_osc,
)
from . import pane_cache
from .askquestion_parser import parse_ask_question
from .plan_parser import extract_plan_file_name
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_SPACE,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_UP,
)
from .message_sender import safe_edit, send_with_fallback
from .reaction_confirm import note_topic_message

# Strip SGR (color) escapes so the ANSI-decorated pane capture can be
# pattern-matched by terminal_parser (whose patterns anchor on ^\s*).
# Avoids a second capture_pane docker-exec round-trip.
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}

# Track interactive mode: (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}

# Dedup: (user_id, thread_id_or_0) -> monotonic time of last send
_interactive_last_sent: dict[tuple[int, int], float] = {}

# Serialize concurrent UI sends per (user_id, thread_id) so JSONL and polling
# paths can't both emit a fresh message before the first one registers its msg_id.
_interactive_locks: dict[tuple[int, int], asyncio.Lock] = {}

# AskUserQuestion only — Claude Code renders the agent's preceding prose AND the
# question text into the pane but doesn't write them to JSONL until the user
# answers. We parse them from the pane and post them as one readable text message
# *before* the photo, so the user can read the full question (the screenshot
# crops long ones) before deciding — the answer options stay on the screenshot
# only. Done once per widget appearance (cleared by clear_interactive_msg); the
# value is the surfaced message_id, or 0 when nothing was parseable.
_auq_text_sent: dict[tuple[int, int], int] = {}

# session_id -> (surfaced_msg_ids, question_text). Set by
# _surface_ask_question_text; msg_ids is a tuple because a long surfaced text
# splits across several messages (the first is the upgrade-in-place target,
# the rest are deleted before the clean re-delivery). After the user answers,
# the held turn lands in JSONL and this drives two de-duplications in
# handle_new_message:
#   • the assistant prose text block (precedes_interactive_prompt) → edit the
#     surfaced message in place to the clean markdown prose + the question
#     (consume_pending_prose_upgrade) instead of sending a duplicate;
#   • the AskUserQuestion tool_use → skip the would-be `**AskUserQuestion**(…)`
#     message (consume_pending_ask_tool_use), which finally evicts the entry.
# Tiny, in-memory; a restart drops it (worst case: the pane render isn't upgraded
# and the post-answer copies aren't suppressed). Soft-capped against abandoned
# (never-answered) prompts.
_pending_auq: dict[str, tuple[tuple[int, ...], str]] = {}
_PENDING_AUQ_CAP = 100

# ExitPlanMode only — Claude Code writes the proposed plan to
# `<claude-home>/plans/<slug>.md` and shows that path in the widget footer,
# while the JSONL copy (the ExitPlanMode tool_use input) is held until the
# user answers. Pre-approval the file is the only full-text source: read it
# and post the plan as text BEFORE the photo, so the user can actually read
# what they're approving (the screenshot crops all but the tail). Done once
# per widget appearance (cleared by clear_interactive_msg); the value is the
# first surfaced message_id, or 0 when nothing was readable.
_plan_text_sent: dict[tuple[int, int], int] = {}

# Sessions whose plan text was surfaced from the file. After the answer the
# held turn lands in JSONL and transcript_parser re-emits the plan as a text
# entry (is_plan_text) — handle_new_message consumes this to skip the
# would-be duplicate. Same in-memory/fail-open semantics as _pending_auq.
_pending_plan_sessions: dict[str, bool] = {}
_PENDING_PLAN_CAP = 100

# LoginPrompt only — the `/login` OAuth URL lives in the pane (TUI-only, never
# in JSONL, like AskUserQuestion). We post it once per appearance as a clickable
# link + one-tap button, so the user doesn't have to copy a wrapped URL out of
# the screenshot. Value: surfaced message_id, or 0 when nothing was parseable.
# Cleared by clear_interactive_msg so the next /login re-surfaces.
_login_url_sent: dict[tuple[int, int], int] = {}


def _record_pending_auq(
    session_id: str, msg_ids: tuple[int, ...], question: str
) -> None:
    if len(_pending_auq) >= _PENDING_AUQ_CAP and session_id not in _pending_auq:
        _pending_auq.pop(next(iter(_pending_auq)), None)
    _pending_auq[session_id] = (msg_ids, question)


def _record_pending_plan(session_id: str) -> None:
    if (
        len(_pending_plan_sessions) >= _PENDING_PLAN_CAP
        and session_id not in _pending_plan_sessions
    ):
        _pending_plan_sessions.pop(next(iter(_pending_plan_sessions)), None)
    _pending_plan_sessions[session_id] = True


def consume_pending_plan_text(session_id: str) -> bool:
    """The plan text just reached JSONL — i.e. the user answered the widget.

    Returns ``True`` when ``_surface_plan_text`` already posted this session's
    plan from the plan file, so the caller skips the would-be duplicate.
    ``False`` (surfacing failed / never ran / restart dropped the bookkeeping)
    → the caller delivers the JSONL copy normally, exactly as before this
    feature existed.
    """
    return _pending_plan_sessions.pop(session_id, None) is not None


# The bot's own home prefix, for de-noising pane-parsed surfaced text: the pane
# shows absolute paths (`/home/user/project/...`) that read as clutter — and,
# screenshotted for docs, leak the operator's username. Agent-authored content
# from JSONL is never rewritten; this applies only to text ccbot itself lifts
# off the pane.
_HOME_PREFIX_RE = re.compile(re.escape(str(Path.home())) + r"(?=/|\s|$)")


def _relativize_home(text: str) -> str:
    return _HOME_PREFIX_RE.sub("~", text)


def _join_prose_question(prose: str, question: str) -> str:
    """The surfaced text: prose then a blank line then the question, dropping
    whichever part is empty."""
    return "\n\n".join(p for p in (prose, question) if p)


async def _deliver_upgraded_prose(
    bot: Bot,
    chat_id: int,
    msg_id: int,
    full_text: str,
    thread_id: int | None,
    user_id: int,
) -> None:
    """Deliver the upgraded AskUserQuestion prose + question, rendering embedded
    blocks out-of-band exactly like a normal assistant reply does.

    A markdown table, wide box-art, or long code block sitting in the prose above
    the question is extracted by ``render_tables_for_chat`` and sent as its own
    photo / document — a plain in-place edit could only ever *inline* it, and a
    phone wraps a monospace table to soup (the whole reason the normal send path
    images them). The FIRST text chunk edits the already-surfaced message in place
    so the prose isn't duplicated; embedded blocks and any following text/question
    chunks are sent as new messages in source order. Splitting also covers the
    >4096-char prose the old single-edit path bailed on entirely (it left the
    box-art pane capture sitting there, the bug this fixes).
    """
    # Reuse the queue's out-of-band senders (table→PNG, long code→document) with
    # their render/write fallbacks; no circular import (message_queue doesn't
    # import this module). Local import keeps it off the module-load path.
    from .message_queue import _send_code_file, _send_table_image

    text_wph, images, files = render_tables_for_chat(full_text)

    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    edited = False

    async def _emit_text(chunk: str) -> None:
        nonlocal edited
        if not chunk.strip():
            return
        if not edited:
            edited = True
            try:
                await safe_edit(bot, chunk, chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass  # gone / too old / RetryAfter — still no duplicate of the prose
        else:
            sent = await send_with_fallback(bot, chat_id, chunk, **thread_kwargs)
            if sent:
                note_topic_message(chat_id, sent.message_id, user_id, thread_id or 0)

    # PLACEHOLDER_RE has two capture groups → split yields
    # [text, kind, ref, text, kind, ref, …, text].
    segments = PLACEHOLDER_RE.split(text_wph)
    i = 0
    while i < len(segments):
        for chunk in split_message(segments[i].strip("\n")):
            await _emit_text(chunk)
        if i + 2 < len(segments):
            kind, ref = segments[i + 1], int(segments[i + 2])
            if kind == "IMG" and 0 <= ref < len(images):
                await _send_table_image(
                    bot, chat_id, images[ref], thread_id=thread_id, silent=False
                )
            elif kind == "FILE" and 0 <= ref < len(files):
                fname, content = files[ref]
                await _send_code_file(
                    bot, chat_id, fname, content, thread_id=thread_id, silent=False
                )
        i += 3

    # The question is always appended as text, so at least one text chunk edits
    # the tracked message — but be defensive: if the prose was somehow all blocks,
    # don't leave the stale box-art message hanging above the freshly-sent ones.
    if not edited:
        try:
            await safe_edit(bot, "⬇️", chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass


async def consume_pending_prose_upgrade(
    bot: Bot,
    session_id: str,
    user_id: int,
    thread_id: int | None,
    jsonl_text: str,
) -> bool:
    """Upgrade a pane-parsed AskUserQuestion surface to the JSONL prose.

    Called from ``handle_new_message`` for an assistant text block flagged
    ``precedes_interactive_prompt``. If ``_surface_ask_question_text`` posted that
    block earlier (prose + question, from the pane), re-deliver it from the clean
    JSONL markdown: edit the first text chunk in place — flattened markdown (links
    etc.) comes back, the appended question survives — and send any embedded table
    / box-art / long-code block out-of-band as its own photo / document (the pane
    render and the old single in-place edit could only inline them). Returns
    ``True`` so the caller skips the would-be duplicate send. If nothing was
    surfaced (parse miss / send failed / not an AskUserQuestion turn), returns
    ``False`` and the caller delivers the JSONL prose normally.

    Doesn't evict the bookkeeping — the AskUserQuestion tool_use, which always
    follows an answer, does that via ``consume_pending_ask_tool_use``.
    """
    entry = _pending_auq.get(session_id)
    if entry is None:
        return False
    msg_ids, question = entry
    full_text = _join_prose_question(jsonl_text, question)
    if full_text and msg_ids:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        # A long surfaced text was split across several messages; the clean
        # re-delivery below sends its own follow-ups, so drop the extras first
        # (best-effort) and upgrade the first message in place.
        for extra_id in msg_ids[1:]:
            await _safe_delete_message(bot, chat_id, extra_id)
        await _deliver_upgraded_prose(
            bot, chat_id, msg_ids[0], full_text, thread_id, user_id
        )
    logger.info(
        "AskUserQuestion surface upgraded in place (msg %s, session %s)",
        msg_ids[0] if msg_ids else "?",
        session_id[:8],
    )
    return True


def consume_pending_ask_tool_use(session_id: str) -> bool:
    """The AskUserQuestion tool_use just reached JSONL — i.e. the user answered.

    If ``_surface_ask_question_text`` already posted this question's text from the
    pane before the answer, evict the bookkeeping and return ``True`` so the
    caller skips the would-be duplicate ``**AskUserQuestion**(…)`` message.
    Returns ``False`` when nothing was surfaced (parse miss, or the answer beat
    the 1 s status poll) — then the caller delivers the tool_use message as the
    fallback, exactly as before this feature existed.
    """
    return _pending_auq.pop(session_id, None) is not None


def _get_lock(ikey: tuple[int, int]) -> asyncio.Lock:
    lock = _interactive_locks.get(ikey)
    if lock is None:
        lock = asyncio.Lock()
        _interactive_locks[ikey] = lock
    return lock


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop((user_id, thread_id or 0), None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def _build_interactive_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Compact nav keyboard attached to the Claude-ждёт-ответа photo.

    Row 1 is the navigation in reading order — ← → ↑ ↓ (left, right, up, down) —
    with ⏎ on the right; row 2 is ⎋ Esc / ␣ / 🔄 Обновить. The user reads the
    captured pane image to see the options and navigates via these keys. ←/→
    switch between an AskUserQuestion's question tabs (the `← N Вопрос …`
    columns), which ↑/↓ alone can't reach; ␣ toggles checkboxes in
    multi-select questions (without it a phone user literally cannot pick
    options — whitespace can't be sent as a text message). Replaces the old
    option-by-option button list which required fragile text-parsing from the
    terminal output.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
                InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]),
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "⏎", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
                ),
            ],
            [
                InlineKeyboardButton(
                    "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "␣", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    tr("iui.btn_refresh"),
                    callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64],
                ),
            ],
        ]
    )


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    pane_ansi: str | None = None,
) -> bool:
    """Capture terminal and send interactive UI content as a photo.

    Handles any Claude Code interactive prompt (AskUserQuestion,
    ExitPlanMode, Permission Prompt, RestoreCheckpoint, ...) uniformly:
    a PNG snapshot of the pane plus a compact ↑/↓/⏎/⎋/🔄 keyboard. The
    user reads the screenshot for context and drives the cursor via
    the nav keys. Returns True if a prompt was detected and the UI
    message sent (or updated in place), False otherwise.

    If `pane_ansi` is provided (e.g. the caller already polled the pane
    via `pane_cache.wait_pane_change`), reuse it instead of doing
    another docker-exec capture round-trip.
    """
    ikey = (user_id, thread_id or 0)

    async with _get_lock(ikey):
        return await _handle_interactive_ui_locked(
            bot, user_id, window_id, thread_id, ikey, pane_ansi=pane_ansi
        )


async def _safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _try_edit_photo(
    bot: Bot,
    chat_id: int,
    message_id: int,
    photo: bytes | str,
    caption: str,
    keyboard: InlineKeyboardMarkup,
) -> object | None:
    """Edit a photo message in place. ``photo`` is PNG bytes (new upload)
    or a Telegram file_id string (cache reuse — no upload).

    Returns the resulting Message on success (so callers can extract
    its file_id for caching), True on Telegram's "not modified", or
    None on failure.
    """
    media_arg: object = io.BytesIO(photo) if isinstance(photo, bytes) else photo
    try:
        return await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=InputMediaPhoto(
                media=media_arg,  # type: ignore[arg-type]
                caption=caption,
                parse_mode="MarkdownV2",
            ),
            reply_markup=keyboard,
        )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True
        return None
    except Exception:
        return None


async def _session_id_for(window_id: str) -> str | None:
    try:
        session = await session_manager.resolve_session_for_window(window_id)
        return session.session_id if session else None
    except Exception:
        return None


async def _surface_ask_question_text(
    bot: Bot,
    chat_id: int,
    window_id: str,
    ikey: tuple[int, int],
    thread_kwargs: dict[str, int],
) -> None:
    """Post the agent's preceding prose + the AskUserQuestion question as one text
    message, so the full question (the screenshot crops long ones) can be read
    before the photo's nav keyboard. The answer options stay on the screenshot —
    not repeated as text.

    Captures with scrollback (the prose can run past the visible viewport) and
    parses the pane; a long surfaced text splits across several messages instead
    of being dropped (the old >4096 bail surfaced nothing at all — exactly the
    long-preamble case where reading it matters most). Absolute home paths are
    rewritten to ``~`` (pane-lifted text only). Records ``_auq_text_sent[ikey]``
    either way so the 1 s status poll doesn't retry every tick; the photo that
    follows is the fallback (parse miss / nothing parseable → photo only). When
    text is posted, ``(msg_ids, question)`` goes into ``_pending_auq`` so the
    JSONL copies that arrive after the answer don't duplicate it (see
    ``consume_pending_prose_upgrade`` / ``consume_pending_ask_tool_use``).
    """
    pane = await session_manager.capture_pane(window_id, scrollback_lines=400)
    parsed = parse_ask_question(pane) if pane else None
    surfaced = _join_prose_question(parsed.prose, parsed.question) if parsed else ""
    if parsed is None or not surfaced:
        _auq_text_sent[ikey] = 0
        logger.debug(
            "AskUserQuestion: nothing to surface for %s (parsed=%s)",
            window_id,
            "miss"
            if parsed is None
            else f"prose={len(parsed.prose)}ch q={len(parsed.question)}ch",
        )
        return

    surfaced = _relativize_home(surfaced)
    sent_ids: list[int] = []
    for chunk in split_message(surfaced):
        sent = await send_with_fallback(bot, chat_id, chunk, **thread_kwargs)
        if sent is None:
            break
        sent_ids.append(sent.message_id)
        note_topic_message(chat_id, sent.message_id, ikey[0], ikey[1])
    msg_id = sent_ids[0] if sent_ids else 0
    if msg_id:
        session_id = await _session_id_for(window_id)
        if session_id:
            _record_pending_auq(session_id, tuple(sent_ids), parsed.question)
    _auq_text_sent[ikey] = msg_id
    logger.info(
        "AskUserQuestion text surfaced for %s: %dch in %d msg(s) (%s), question=%r",
        window_id,
        len(surfaced),
        len(sent_ids),
        "sent" if msg_id else "send failed",
        parsed.question[:60],
    )


async def _surface_plan_text(
    bot: Bot,
    chat_id: int,
    window_id: str,
    ikey: tuple[int, int],
    thread_kwargs: dict[str, int],
    pane_plain: str,
) -> None:
    """Post the full plan text before the ExitPlanMode approval photo.

    Claude Code writes the plan to ``<claude-home>/plans/<slug>.md`` before
    asking for approval and shows that path in the widget footer; the JSONL
    copy (the tool_use ``input.plan``) is held until the user answers, and the
    screenshot crops all but the tail — pre-approval, the file is the only
    readable source. Only the basename is taken from the pane (agent-controlled
    text); it resolves against the binding's own plans dir, so a hostile pane
    can't point us at an arbitrary host file. Long plans split across messages.

    Records ``_plan_text_sent[ikey]`` either way (no per-tick retry); the photo
    is the fallback on any miss, and the JSONL copy after the answer is the
    delivery of last resort (suppressed via ``_pending_plan_sessions`` only
    when the surface actually went out).
    """
    name = extract_plan_file_name(pane_plain)
    if not name:
        _plan_text_sent[ikey] = 0
        logger.debug("ExitPlanMode: no plan-file path in pane for %s", window_id)
        return
    path = session_manager.plans_dir_for_binding(window_id) / name
    try:
        content = (await asyncio.to_thread(path.read_text, "utf-8")).strip()
    except OSError as e:
        _plan_text_sent[ikey] = 0
        logger.warning("ExitPlanMode: plan file unreadable (%s): %s", path, e)
        return
    if not content:
        _plan_text_sent[ikey] = 0
        return

    sent_ids: list[int] = []
    for chunk in split_message(tr("iui.plan_header") + "\n\n" + content):
        sent = await send_with_fallback(bot, chat_id, chunk, **thread_kwargs)
        if sent is None:
            break
        sent_ids.append(sent.message_id)
        note_topic_message(chat_id, sent.message_id, ikey[0], ikey[1])
    msg_id = sent_ids[0] if sent_ids else 0
    if msg_id:
        session_id = await _session_id_for(window_id)
        if session_id:
            _record_pending_plan(session_id)
    _plan_text_sent[ikey] = msg_id
    logger.info(
        "Plan text surfaced for %s from %s: %dch in %d msg(s) (%s)",
        window_id,
        name,
        len(content),
        len(sent_ids),
        "sent" if msg_id else "send failed",
    )


async def _surface_login_url(
    bot: Bot,
    chat_id: int,
    window_id: str,
    ikey: tuple[int, int],
    thread_kwargs: dict[str, int],
    pane_ansi: str,
) -> None:
    """Post the Claude Code sign-in URL as a clickable link + one-tap button.

    The `/login` OAuth URL is wrapped across the screenshot and never reaches
    JSONL, so the pane is the only source. We re-capture with scrollback (a long
    URL can push the top of the screen off) and parse it; the raw URL goes in a
    copyable code block (Telegram's in-app browser sometimes chokes on the OAuth
    redirect — paste into a real browser then), plus a 🔗 button for one tap.
    Recorded once per appearance (``_login_url_sent``); the photo is the
    fallback when nothing parses.
    """
    pane = await session_manager.capture_pane(
        window_id, with_ansi=True, scrollback_lines=100
    )
    url = parse_login_url(pane or pane_ansi)
    if not url:
        _login_url_sent[ikey] = 0
        logger.debug("Login URL: nothing parseable for %s", window_id)
        return

    text = tr("iui.login_prompt", url=url)
    # If the sign-in screen shows a one-time code (Codex device flow), append it
    # as a copyable code span — reading it off the photo can't be copied.
    code = parse_login_code(pane or pane_ansi)
    if code:
        text += "\n\n" + tr("iui.login_code", code=code)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("iui.btn_open_login"), url=url)]]
    )
    sent = await send_with_fallback(
        bot, chat_id, text, reply_markup=keyboard, **thread_kwargs
    )
    msg_id = sent.message_id if sent is not None else 0
    if msg_id:
        note_topic_message(chat_id, msg_id, ikey[0], ikey[1])
    _login_url_sent[ikey] = msg_id
    logger.info(
        "Login URL surfaced for %s (%s): %s",
        window_id,
        "sent" if msg_id else "send failed",
        url[:60],
    )


async def _handle_interactive_ui_locked(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    ikey: tuple[int, int],
    *,
    pane_ansi: str | None = None,
) -> bool:
    # Dedup: don't send a new interactive UI if one was sent in the last 3 seconds
    last_sent = _interactive_last_sent.get(ikey, 0)
    if not _interactive_msgs.get(ikey) and time.monotonic() - last_sent < 3.0:
        return True  # Already sent recently, skip duplicate

    t0 = time.perf_counter()
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)

    # Single capture_pane suffices: we used to grab the pane twice (once
    # plain for is_interactive_ui, once ANSI for rendering); stripping
    # SGR escapes is <1 ms, vs. ~30 ms per docker-exec round-trip.
    # session_manager.capture_pane branches by binding type so this works
    # for both tmux ("@<id>") and docker ("docker:<agent>") bindings.
    if pane_ansi is None:
        pane_ansi = await session_manager.capture_pane(window_id, with_ansi=True)
    if not pane_ansi:
        logger.debug("No ANSI pane text captured for window_id %s", window_id)
        return False
    t_cap = time.perf_counter()

    pane_plain = _ANSI_SGR_RE.sub("", strip_osc(pane_ansi))
    iui = extract_interactive_content(pane_plain)
    if iui is None:
        logger.debug(
            "No interactive UI detected in window_id %s (last 3 lines: %s)",
            window_id,
            pane_plain.strip().split("\n")[-3:],
        )
        return False

    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    # AskUserQuestion: post the agent's preceding prose + the question as a text
    # message *before* the photo (neither is in JSONL until the user answers, so
    # the pane is the only source). The answer options stay on the screenshot
    # only. Done once per widget appearance — _auq_text_sent is cleared when the
    # widget goes away (clear_interactive_msg).
    if iui.name == "AskUserQuestion" and ikey not in _auq_text_sent:
        try:
            await _surface_ask_question_text(
                bot, chat_id, window_id, ikey, thread_kwargs
            )
        except Exception as e:  # never let this block the photo, which is the fallback
            logger.warning("AskUserQuestion text surfacing failed: %s", e)
            _auq_text_sent.setdefault(ikey, 0)

    # ExitPlanMode: post the plan file's content as text before the photo —
    # the plan is held out of JSONL until the user answers, and the screenshot
    # shows only the widget's tail, so the plan file (whose path the widget
    # footer shows) is the only readable pre-approval source. Once per widget
    # appearance — _plan_text_sent is cleared when the widget goes away.
    if iui.name == "ExitPlanMode" and ikey not in _plan_text_sent:
        try:
            await _surface_plan_text(
                bot, chat_id, window_id, ikey, thread_kwargs, pane_plain
            )
        except Exception as e:  # never block the photo, which is the fallback
            logger.warning("Plan text surfacing failed: %s", e)
            _plan_text_sent.setdefault(ikey, 0)

    # Any login screen (is_login): surface the sign-in URL as a clickable link
    # + button, once per appearance. Provider-agnostic — Claude Code's /login
    # and Codex's device/browser flow share this one path (no per-CLI branch).
    # The photo (below) is the fallback and also carries the one-time code.
    if iui.is_login and ikey not in _login_url_sent:
        try:
            await _surface_login_url(
                bot, chat_id, window_id, ikey, thread_kwargs, pane_ansi
            )
        except Exception as e:  # never block the photo fallback
            logger.warning("Login URL surfacing failed: %s", e)
            _login_url_sent.setdefault(ikey, 0)

    existing_msg_id = _interactive_msgs.get(ikey)

    # Hash-skip: if an existing interactive-UI message already shows
    # this exact pane, don't re-render/upload. Telegram would answer
    # "not modified", but only after we've spent render + upload RTT.
    new_hash = pane_cache.pane_hash(pane_ansi)
    if existing_msg_id and pane_cache.get_hash(existing_msg_id) == new_hash:
        logger.debug(
            "TIMING interactive_ui_edit SKIP: cap=%.0fms total=%.0fms",
            (t_cap - t0) * 1000,
            (t_cap - t0) * 1000,
        )
        _interactive_mode[ikey] = window_id
        return True

    caption = tr("iui.caption_waiting")
    keyboard = _build_interactive_keyboard(window_id)

    # file_id reuse: if we've uploaded this exact pane before in this
    # bot's lifetime, skip render+upload entirely and let Telegram
    # serve the cached file_id. AskUserQuestion users typically cycle
    # through the same cursor positions (↓↓↑↑) — file_id reuse turns
    # those re-visits into a single editMessageMedia round-trip with
    # zero bytes uploaded.
    cached_file_id = pane_cache.get_file_id(new_hash)
    png_bytes: bytes | None = None
    photo_arg: bytes | str
    if cached_file_id is not None:
        photo_arg = cached_file_id
        t_render = time.perf_counter()  # no render happened
    else:
        png_bytes = await text_to_image(pane_ansi, with_ansi=True)
        photo_arg = png_bytes
        t_render = time.perf_counter()

    if existing_msg_id:
        edit_result = await _try_edit_photo(
            bot, chat_id, existing_msg_id, photo_arg, caption, keyboard
        )
        if edit_result is not None:
            t_edit = time.perf_counter()
            pane_cache.set_hash(existing_msg_id, new_hash)
            # Cache the file_id Telegram returned (only on a real
            # upload, not on cache hit / not modified).
            if (
                cached_file_id is None
                and hasattr(edit_result, "photo")
                and getattr(edit_result, "photo", None)
            ):
                photos = edit_result.photo  # type: ignore[union-attr]
                if photos:
                    pane_cache.set_file_id(new_hash, photos[-1].file_id)
            logger.debug(
                "TIMING interactive_ui_edit%s: cap=%.0fms render=%.0fms edit=%.0fms total=%.0fms png=%s",
                " (file_id reuse)" if cached_file_id else "",
                (t_cap - t0) * 1000,
                (t_render - t_cap) * 1000,
                (t_edit - t_render) * 1000,
                (t_edit - t0) * 1000,
                f"{len(png_bytes)}B" if png_bytes else "skipped",
            )
            _interactive_mode[ikey] = window_id
            return True
        # Edit failed (not "not modified"). Drop the cached file_id —
        # Telegram may have garbage-collected it — and try a fresh send.
        if cached_file_id is not None:
            pane_cache.forget_file_id(new_hash)
        await _safe_delete_message(bot, chat_id, existing_msg_id)
        _interactive_msgs.pop(ikey, None)

    logger.info(
        "Sending interactive UI photo to user %d for window_id %s%s",
        user_id,
        window_id,
        " (file_id reuse)" if cached_file_id else "",
    )
    # If we're doing a fresh send and don't have png_bytes yet (cached
    # file_id path that fell through), render now.
    if not isinstance(photo_arg, str) and png_bytes is None:
        png_bytes = await text_to_image(pane_ansi, with_ansi=True)
        photo_arg = png_bytes
    send_arg: object = (
        photo_arg if isinstance(photo_arg, str) else io.BytesIO(photo_arg)
    )
    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=send_arg,  # type: ignore[arg-type]
            caption=caption,
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as e:
        # Stamp the cooldown even on failure: Telegram occasionally commits
        # an upload server-side but the HTTP response times out client-side,
        # leaving _interactive_msgs unset. Without this, the next status-poll
        # tick (1s later) would send a fresh photo and the user sees two.
        _interactive_last_sent[ikey] = time.monotonic()
        logger.error("Failed to send interactive UI photo: %s", e)
        return False
    if sent:
        _interactive_msgs[ikey] = sent.message_id
        _interactive_mode[ikey] = window_id
        _interactive_last_sent[ikey] = time.monotonic()
        note_topic_message(chat_id, sent.message_id, ikey[0], ikey[1])
        pane_cache.set_hash(sent.message_id, new_hash)
        if cached_file_id is None and sent.photo:
            pane_cache.set_file_id(new_hash, sent.photo[-1].file_id)
        return True
    return False


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    # The widget is gone — next AskUserQuestion / plan / login re-surfaces.
    _auq_text_sent.pop(ikey, None)
    _plan_text_sent.pop(ikey, None)
    _login_url_sent.pop(ikey, None)
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # Message may already be deleted or too old
