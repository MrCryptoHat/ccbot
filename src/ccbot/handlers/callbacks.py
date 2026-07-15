"""Callback query dispatch — routes inline keyboard presses to handlers.

Replaces the monolithic 590-line callback_handler in bot.py with a dispatch
table keyed by callback data prefixes. Each handler is a small focused function.

Core responsibilities:
  - Dispatch table routing by CB_* prefix
  - History pagination callbacks
  - Directory browser navigation callbacks
  - Session picker callbacks
  - Window picker callbacks
  - Screenshot refresh and quick-key callbacks
  - Interactive UI navigation callbacks (arrows, enter, esc, etc.)
"""

import asyncio
import io
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telegram import (
    CallbackQuery,
    InputMediaPhoto,
    Update,
    User,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from . import get_thread_id
from .. import plugins
from ..config import config
from ..i18n import tr
from ..screenshot import text_to_image
from ..session import session_manager
from ..terminal_parser import is_tui_ready
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_CMD_CANCEL,
    CB_CMD_CLEAR,
    CB_CMD_COMPACT,
    CB_CMD_CONFIRM,
    CB_CMD_CONTEXT,
    CB_CMD_EFFORT,
    CB_CMD_FRESH,
    CB_CMD_KILL,
    CB_CMD_MCP,
    CB_CMD_MODE_CYCLE,
    CB_CMD_MODEL,
    CB_CMD_REFRESH,
    CB_CMD_RESTART,
    CB_CMD_RESUME,
    CB_CMD_TAB,
    CB_CMD_WIPE_INPUT,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_KEYS_PREFIX,
    CB_RUNTIME_CANCEL,
    CB_RUNTIME_SELECT,
    CB_SCREENSHOT_REFRESH,
    CB_SESSION_BROWSE,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_STATUS_REFRESH,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
    CB_WT_CANCEL,
    CB_WT_DEL,
    CB_WT_DELNO,
    CB_WT_DELOK,
    CB_WT_DROP,
    CB_WT_KEEP,
    CB_WT_NEW,
)
from .directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_RUNTIME,
    STATE_SELECTING_SESSION,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_runtime_picker,
    build_session_picker,
    clear_browse_state,
    clear_runtime_picker_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from . import pane_cache
from .codex_status_parser import (
    STATUS_MARKER as CODEX_STATUS_MARKER,
    format_status_message as format_codex_status,
    parse_status_output as parse_codex_status,
)
from .context_parser import format_context_message, parse_context_output
from .history import send_history
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_msg_id,
    handle_interactive_ui,
)
from .message_sender import safe_edit, safe_send
from .worktrees import (
    _handle_wt_cancel,
    _handle_wt_del,
    _handle_wt_delno,
    _handle_wt_delok,
    _handle_wt_drop,
    _handle_wt_keep,
    _handle_wt_new,
)

logger = logging.getLogger(__name__)


# --- Helpers ---


def _get_user_data(
    context: ContextTypes.DEFAULT_TYPE, key: str, default: Any = None
) -> Any:
    """Safely get a value from context.user_data."""
    return context.user_data.get(key, default) if context.user_data else default


def _validate_pending_thread(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Check if callback comes from the same topic that started the picker/browser.

    Returns True if valid (same topic or no pending topic), False if stale.
    """
    pending_tid = _get_user_data(context, "_pending_thread_id")
    if pending_tid is not None and get_thread_id(update) != pending_tid:
        return False
    return True


async def _answer_stale(query: CallbackQuery, entity: str = "picker") -> None:
    """Answer a stale callback query with a mismatch alert.

    ``entity`` is kept for the log trail only — the user-facing toast is a
    single translated string (the distinction browser/picker doesn't help
    the user, only the developer).
    """
    logger.debug("stale callback ignored (entity=%s)", entity)
    await query.answer(tr("cb.stale_ui"), show_alert=True)


def _extract_suffix(data: str, prefix: str) -> str:
    """Extract the suffix after a callback data prefix."""
    return data[len(prefix) :]


# key_id → (tmux_key, enter, literal). literal=True means the value is
# sent as text rather than interpreted as a tmux key name — needed for
# "/" so tmux types a real slash that opens Claude Code's slash menu
# instead of looking up a nonexistent "slash" key binding.
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
    # Ctrl-B — Claude Code "background this run" (sends a long bash command /
    # subagent to the background so you can keep chatting). tmux send-keys
    # delivers the literal keystroke to the program, so tmux's own C-b prefix
    # doesn't swallow it.
    "cb": ("C-b", False, False),
    "slash": ("/", False, True),
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "Ctrl + C",
    "cb": "Ctrl + B",
    "slash": "/",
}


# Interactive UI key → (tmux_key, toast_label, clear_after)
# clear_after=True means the interactive msg is cleared (e.g. Escape dismisses UI)
_INTERACTIVE_KEY_MAP: dict[str, tuple[str, str, bool]] = {
    CB_ASK_UP: ("Up", "↑", False),
    CB_ASK_DOWN: ("Down", "↓", False),
    CB_ASK_LEFT: ("Left", "←", False),
    CB_ASK_RIGHT: ("Right", "→", False),
    CB_ASK_ESC: ("Escape", "⎋ Esc", True),
    CB_ASK_ENTER: ("Enter", "⏎ Enter", False),
    CB_ASK_SPACE: ("Space", "␣ Space", False),
    CB_ASK_TAB: ("Tab", "⇥ Tab", False),
}


def _build_nav_tab_keyboard(window_id: str) -> Any:
    """Build the Nav-tab inline keyboard.

    Thin lazy wrapper around :func:`commands._build_commands_keyboard`
    so screenshot-key handlers (which can't import commands.py at
    module load — circular import via .callbacks ← .commands ← .callbacks)
    can rebuild the keyboard after a key press.
    """
    from .commands import _build_commands_keyboard

    return _build_commands_keyboard(window_id, tab="nav")


# --- Individual callback handlers ---


async def _handle_history(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Handle history pagination (older/newer)."""
    prefix_len = len(CB_HISTORY_PREV)  # same length for both
    rest = data[prefix_len:]
    try:
        parts = rest.split(":")
        if len(parts) < 4:
            offset_str, window_id = rest.split(":", 1)
            start_byte, end_byte = 0, 0
        else:
            offset_str = parts[0]
            start_byte = int(parts[-2])
            end_byte = int(parts[-1])
            window_id = ":".join(parts[1:-2])
        offset = int(offset_str)
    except (ValueError, IndexError):
        await query.answer(tr("cb.invalid_data"))
        return

    # History reads JSONL via session_manager — transport-agnostic, so
    # docker bindings work the same as tmux. We only need to confirm the
    # binding still exists.
    if session_manager._is_docker_binding(
        window_id
    ) or await tmux_manager.find_window_by_id(window_id):
        await send_history(
            query,
            window_id,
            offset=offset,
            edit=True,
            start_byte=start_byte,
            end_byte=end_byte,
        )
    else:
        await safe_edit(query, tr("cb.window_gone"))
    await query.answer(tr("cb.page_updated"))


async def _handle_dir_select(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Navigate into a subdirectory."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query, "browser")
        return

    try:
        idx = int(_extract_suffix(data, CB_DIR_SELECT))
    except ValueError:
        await query.answer(tr("cb.invalid_data"))
        return

    cached_dirs: list[str] = _get_user_data(context, BROWSE_DIRS_KEY, [])
    if idx < 0 or idx >= len(cached_dirs):
        await query.answer(tr("cb.list_changed"), show_alert=True)
        return
    subdir_name = cached_dirs[idx]

    current_path = _get_user_data(context, BROWSE_PATH_KEY)
    if current_path is None:
        # user_data died with a bot restart; browsing from the bot's own
        # cwd would eventually bind the topic to a nonsense directory.
        await _answer_stale(query, "browser")
        return
    new_path = (Path(current_path) / subdir_name).resolve()

    if not new_path.exists() or not new_path.is_dir():
        await query.answer(tr("cb.dir_not_found"), show_alert=True)
        return

    new_path_str = str(new_path)
    if context.user_data is not None:
        context.user_data[BROWSE_PATH_KEY] = new_path_str
        context.user_data[BROWSE_PAGE_KEY] = 0

    msg_text, keyboard, subdirs = build_directory_browser(new_path_str)
    if context.user_data is not None:
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    await safe_edit(query, msg_text, reply_markup=keyboard)
    await query.answer()


async def _handle_dir_up(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Navigate to parent directory."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query, "browser")
        return

    current_path = _get_user_data(context, BROWSE_PATH_KEY)
    if current_path is None:
        await _answer_stale(query, "browser")
        return
    parent_path = str(Path(current_path).resolve().parent)

    if context.user_data is not None:
        context.user_data[BROWSE_PATH_KEY] = parent_path
        context.user_data[BROWSE_PAGE_KEY] = 0

    msg_text, keyboard, subdirs = build_directory_browser(parent_path)
    if context.user_data is not None:
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    await safe_edit(query, msg_text, reply_markup=keyboard)
    await query.answer()


async def _handle_dir_page(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Paginate directory listing."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query, "browser")
        return

    try:
        pg = int(_extract_suffix(data, CB_DIR_PAGE))
    except ValueError:
        await query.answer(tr("cb.invalid_data"))
        return

    current_path = _get_user_data(context, BROWSE_PATH_KEY)
    if current_path is None:
        await _answer_stale(query, "browser")
        return
    if context.user_data is not None:
        context.user_data[BROWSE_PAGE_KEY] = pg

    msg_text, keyboard, subdirs = build_directory_browser(current_path, pg)
    if context.user_data is not None:
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    await safe_edit(query, msg_text, reply_markup=keyboard)
    await query.answer()


async def _handle_dir_confirm(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Confirm directory selection — show session picker or runtime picker."""
    selected_path = _get_user_data(context, BROWSE_PATH_KEY)
    if selected_path is None:
        # Stale Select button from before a bot restart: without this check
        # the topic would be bound to the bot process's own cwd.
        await _answer_stale(query, "browser")
        return
    pending_thread_id: int | None = _get_user_data(context, "_pending_thread_id")

    confirm_thread_id = get_thread_id(update)
    if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        await _answer_stale(query, "browser")
        return

    clear_browse_state(context.user_data)

    sessions = await session_manager.list_sessions_for_directory(selected_path)
    if sessions:
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
            context.user_data[SESSIONS_KEY] = sessions
            context.user_data["_selected_path"] = selected_path
        text, keyboard = build_session_picker(sessions, selected_path)
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer()
        return

    # No existing sessions → pick the runtime (Claude Code / Codex) for the
    # fresh window. _handle_runtime_select does the actual create+bind.
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_SELECTING_RUNTIME
        context.user_data["_selected_path"] = selected_path
    text, keyboard = build_runtime_picker(selected_path)
    await safe_edit(query, text, reply_markup=keyboard)
    await query.answer()


async def _handle_dir_cancel(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Cancel directory browsing."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query, "browser")
        return

    clear_browse_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
    await safe_edit(query, tr("cb.cancelled"))
    await query.answer(tr("cb.cancelled"))


async def _handle_session_select(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Resume an existing session."""
    from ..bot import _create_and_bind_window

    pending_tid = _get_user_data(context, "_pending_thread_id")
    if pending_tid is None:
        pending_tid = get_thread_id(update)
    if pending_tid is not None and get_thread_id(update) != pending_tid:
        await _answer_stale(query)
        return

    try:
        idx = int(_extract_suffix(data, CB_SESSION_SELECT))
    except ValueError:
        await query.answer(tr("cb.invalid_data"))
        return

    cached_sessions = _get_user_data(context, SESSIONS_KEY, [])
    if idx < 0 or idx >= len(cached_sessions):
        await query.answer(tr("cb.session_not_found"))
        return

    session = cached_sessions[idx]
    selected_path = _get_user_data(context, "_selected_path")
    if selected_path is None:
        await _answer_stale(query)
        return
    clear_session_picker_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_selected_path", None)

    await _create_and_bind_window(
        query,
        context,
        user,
        selected_path,
        pending_tid,
        resume_session_id=session.session_id,
    )


async def _handle_session_new(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Start a new Claude session (from session picker).

    ``New Session`` is Claude-specific by design: the picker lists Claude
    sessions to resume, and its sibling ``🟠 Codex`` button (CB_RUNTIME_SELECT)
    covers starting a Codex agent — so no runtime step is needed here.
    """
    from ..bot import _create_and_bind_window

    pending_tid = _get_user_data(context, "_pending_thread_id")
    if pending_tid is None:
        pending_tid = get_thread_id(update)
    if pending_tid is not None and get_thread_id(update) != pending_tid:
        await _answer_stale(query)
        return

    selected_path = _get_user_data(context, "_selected_path")
    if selected_path is None:
        # ➕ New Session on a picker from before a bot restart would create
        # a window in the bot's own cwd and bind the topic to it.
        await _answer_stale(query)
        return
    clear_session_picker_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_selected_path", None)

    await _create_and_bind_window(query, context, user, selected_path, pending_tid)


async def _handle_runtime_select(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Runtime chosen (Claude Code / Codex) → create + bind the fresh window."""
    from ..bot import _create_and_bind_window
    from ..runtimes import get_runtime

    pending_tid = _get_user_data(context, "_pending_thread_id")
    if pending_tid is None:
        pending_tid = get_thread_id(update)
    if pending_tid is not None and get_thread_id(update) != pending_tid:
        await _answer_stale(query)
        return

    selected_path = _get_user_data(context, "_selected_path")
    if selected_path is None:
        # Runtime button from a picker that predates a bot restart — the path
        # is gone, so binding would target the bot's own cwd.
        await _answer_stale(query)
        return

    # get_runtime normalises any unknown/garbled suffix to "claude" — no
    # separate validation needed, and an unrecognised value degrades safely.
    runtime = get_runtime(_extract_suffix(data, CB_RUNTIME_SELECT)).name

    clear_runtime_picker_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_selected_path", None)
        # When 🟠 Codex is tapped straight from the session picker, the cached
        # Claude-session list is now stale — drop it too.
        context.user_data.pop(SESSIONS_KEY, None)

    await _create_and_bind_window(
        query, context, user, selected_path, pending_tid, runtime=runtime
    )


async def _handle_runtime_cancel(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Cancel the runtime picker."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query)
        return
    clear_runtime_picker_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_selected_path", None)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
    await safe_edit(query, tr("cb.cancelled"))
    await query.answer(tr("cb.cancelled"))


async def _handle_session_browse(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Session picker → directory browser, rooted at the matched folder.

    Escape hatch for a name-based auto-bind that resolved to the wrong
    directory: the picker shows the matched folder, and this opens the
    standard browser there so the user can navigate up («📁 ..») or
    elsewhere and re-confirm. Every db:* handler is reused — only the entry
    point is new. ``_pending_thread_id`` / ``_pending_thread_text`` are kept
    so the eventual dir-confirm rebinds this same topic and forwards the
    pending message.
    """
    if not _validate_pending_thread(update, context):
        await _answer_stale(query)
        return

    selected_path = _get_user_data(context, "_selected_path", str(Path.home()))
    clear_session_picker_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_selected_path", None)

    msg_text, keyboard, subdirs = build_directory_browser(selected_path)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = selected_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    await safe_edit(query, msg_text, reply_markup=keyboard)
    await query.answer()


async def _handle_session_cancel(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Cancel session picker."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query)
        return

    clear_session_picker_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop("_selected_path", None)
    await safe_edit(query, tr("cb.cancelled"))
    await query.answer(tr("cb.cancelled"))


async def _handle_win_bind(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Bind an existing unbound window to this topic."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query)
        return

    try:
        idx = int(_extract_suffix(data, CB_WIN_BIND))
    except ValueError:
        await query.answer(tr("cb.invalid_data"))
        return

    cached_windows: list[str] = _get_user_data(context, UNBOUND_WINDOWS_KEY, [])
    if idx < 0 or idx >= len(cached_windows):
        await query.answer(tr("cb.list_changed"), show_alert=True)
        return
    selected_wid = cached_windows[idx]

    w = await tmux_manager.find_window_by_id(selected_wid)
    if not w:
        display = session_manager.get_display_name(selected_wid)
        await query.answer(tr("commands.window_gone", name=display), show_alert=True)
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await query.answer(tr("cb.not_in_topic"), show_alert=True)
        return

    display = w.window_name
    clear_window_picker_state(context.user_data)
    session_manager.bind_thread(user.id, thread_id, selected_wid, window_name=display)
    if w.cwd:
        # Remember the window's directory so this topic auto-rebinds here next
        # time without the picker.
        session_manager.record_thread_directory(user.id, thread_id, w.cwd)

    resolved_chat = session_manager.resolve_chat_id(user.id, thread_id)
    try:
        await context.bot.edit_forum_topic(
            chat_id=resolved_chat,
            message_thread_id=thread_id,
            name=display,
        )
    except Exception as e:
        logger.debug(f"Failed to rename topic: {e}")

    await safe_edit(query, tr("bot.bound_to_window", name=display))

    pending_text = _get_user_data(context, "_pending_thread_text")
    if context.user_data is not None:
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop("_pending_thread_id", None)
    if pending_text:
        send_ok, send_msg = await session_manager.send_to_window(
            selected_wid, pending_text
        )
        if not send_ok:
            logger.warning("Failed to forward pending text: %s", send_msg)
            await safe_send(
                context.bot,
                resolved_chat,
                tr("bot.pending_send_failed", err=send_msg),
                message_thread_id=thread_id,
            )
    await query.answer(tr("cb.bound"))


async def _handle_win_new(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Window picker → new session → transition to directory browser."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query)
        return

    clear_window_picker_state(context.user_data)
    start_path = str(Path.home())
    msg_text, keyboard, subdirs = build_directory_browser(start_path)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
    await safe_edit(query, msg_text, reply_markup=keyboard)
    await query.answer()


async def _handle_win_cancel(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Cancel window picker."""
    if not _validate_pending_thread(update, context):
        await _answer_stale(query)
        return

    clear_window_picker_state(context.user_data)
    if context.user_data is not None:
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
    await safe_edit(query, tr("cb.cancelled"))
    await query.answer(tr("cb.cancelled"))


async def _handle_screenshot_refresh(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Refresh a screenshot."""
    window_id = _extract_suffix(data, CB_SCREENSHOT_REFRESH)
    # Close the spinner immediately; we'll either do a fast skip or the
    # heavy render below. Either way the user stops staring at ⏳.
    await query.answer()
    t0 = time.perf_counter()
    text = await session_manager.capture_pane(window_id, with_ansi=True)
    t_cap = time.perf_counter()
    if not text:
        return

    msg_id = query.message.message_id if query.message else None
    new_hash = pane_cache.pane_hash(text)
    if msg_id is not None and pane_cache.get_hash(msg_id) == new_hash:
        # Pane identical to what this message already shows — don't
        # pay render + upload. Telegram would answer "not modified"
        # anyway, but only after we've already burned the bytes.
        logger.debug(
            "TIMING screenshot_refresh SKIP: cap=%.0fms total=%.0fms",
            (t_cap - t0) * 1000,
            (t_cap - t0) * 1000,
        )
        return

    cached_file_id = pane_cache.get_file_id(new_hash)
    keyboard = _build_nav_tab_keyboard(window_id)
    if cached_file_id is not None:
        media: InputMediaPhoto = InputMediaPhoto(media=cached_file_id)
        png_size_label = "skipped"
        t_render = time.perf_counter()
    else:
        png_bytes = await text_to_image(text, with_ansi=True)
        media = InputMediaPhoto(media=io.BytesIO(png_bytes))
        png_size_label = f"{len(png_bytes)}B"
        t_render = time.perf_counter()
    try:
        result = await query.edit_message_media(media=media, reply_markup=keyboard)
        t_edit = time.perf_counter()
        if msg_id is not None:
            pane_cache.set_hash(msg_id, new_hash)
        if (
            cached_file_id is None
            and hasattr(result, "photo")
            and getattr(result, "photo", None)
        ):
            photos = result.photo  # type: ignore[union-attr]
            if photos:
                pane_cache.set_file_id(new_hash, photos[-1].file_id)
        logger.debug(
            "TIMING screenshot_refresh%s: cap=%.0fms render=%.0fms edit=%.0fms total=%.0fms png=%s",
            " (file_id reuse)" if cached_file_id else "",
            (t_cap - t0) * 1000,
            (t_render - t_cap) * 1000,
            (t_edit - t_render) * 1000,
            (t_edit - t0) * 1000,
            png_size_label,
        )
    except BadRequest as e:
        if "not modified" in str(e).lower():
            if msg_id is not None:
                pane_cache.set_hash(msg_id, new_hash)
            return
        if cached_file_id is not None:
            pane_cache.forget_file_id(new_hash)
        logger.error(f"Failed to refresh screenshot: {e}")
    except Exception as e:
        if cached_file_id is not None:
            pane_cache.forget_file_id(new_hash)
        logger.error(f"Failed to refresh screenshot: {e}")


async def _handle_noop(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """No-op callback."""
    await query.answer()


async def _handle_interactive_key(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Handle interactive UI key presses (Up, Down, Left, Right, Esc, Enter, Space, Tab)."""
    # Find which prefix matched
    matched_prefix: str | None = None
    for prefix in _INTERACTIVE_KEY_MAP:
        if data.startswith(prefix):
            matched_prefix = prefix
            break

    if matched_prefix is None:
        await query.answer(tr("cb.unknown_key"))
        return

    tmux_key, toast_label, clear_after = _INTERACTIVE_KEY_MAP[matched_prefix]
    window_id = _extract_suffix(data, matched_prefix)
    thread_id = get_thread_id(update)

    # Close the Telegram loading spinner immediately so the UX feels
    # instant. Actual work continues below.
    await query.answer(toast_label)
    t0 = time.perf_counter()
    sent = await session_manager.send_keys(
        window_id, tmux_key, enter=False, literal=False
    )
    t_send = time.perf_counter()
    if not sent:
        return
    if clear_after:
        await clear_interactive_msg(user.id, context.bot, thread_id)
        return

    # Adaptive wait: instead of a fixed 500 ms, poll the pane until its
    # hash differs from what the interactive-UI message currently shows.
    # Common case — the tmux pane repaints in ~150 ms — returns right
    # after the minimum settle; pathological case is bounded at 600 ms.
    msg_id = get_interactive_msg_id(user.id, thread_id)
    prior_hash = pane_cache.get_hash(msg_id) if msg_id is not None else None
    text, _ = await pane_cache.wait_pane_change(window_id, prior_hash)
    t_wait = time.perf_counter()
    if text:
        await handle_interactive_ui(
            context.bot, user.id, window_id, thread_id, pane_ansi=text
        )
    t_ui = time.perf_counter()
    logger.debug(
        "TIMING interactive_key=%s: send=%.0fms wait=%.0fms ui=%.0fms total=%.0fms",
        tmux_key,
        (t_send - t0) * 1000,
        (t_wait - t_send) * 1000,
        (t_ui - t_wait) * 1000,
        (t_ui - t0) * 1000,
    )


async def _handle_interactive_refresh(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Refresh interactive UI display."""
    window_id = _extract_suffix(data, CB_ASK_REFRESH)
    thread_id = get_thread_id(update)
    await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
    await query.answer("🔄")


async def _handle_screenshot_keys(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Handle screenshot quick-key presses."""
    rest = _extract_suffix(data, CB_KEYS_PREFIX)
    colon_idx = rest.find(":")
    if colon_idx < 0:
        await query.answer(tr("cb.invalid_data"))
        return
    key_id = rest[:colon_idx]
    window_id = rest[colon_idx + 1 :]

    key_info = _KEYS_SEND_MAP.get(key_id)
    if not key_info:
        await query.answer(tr("cb.unknown_key"))
        return

    tmux_key, enter, literal = key_info
    await query.answer(_KEY_LABELS.get(key_id, key_id))
    t0 = time.perf_counter()
    sent = await session_manager.send_keys(
        window_id, tmux_key, enter=enter, literal=literal
    )
    t_send = time.perf_counter()
    if not sent:
        return

    # Adaptive wait for the pane to redraw. prior_hash = what this very
    # screenshot message is currently showing; early-exit as soon as
    # capture_pane returns something different.
    msg_id = query.message.message_id if query.message else None
    prior_hash = pane_cache.get_hash(msg_id) if msg_id is not None else None
    text, new_hash = await pane_cache.wait_pane_change(window_id, prior_hash)
    t_wait = time.perf_counter()
    if not text:
        return

    if msg_id is not None and new_hash == prior_hash:
        # Timed out with no visible change — nothing to edit.
        logger.debug(
            "TIMING screenshot_keys=%s SKIP: send=%.0fms wait=%.0fms total=%.0fms",
            key_id,
            (t_send - t0) * 1000,
            (t_wait - t_send) * 1000,
            (t_wait - t0) * 1000,
        )
        return

    cached_file_id = pane_cache.get_file_id(new_hash) if new_hash else None
    keyboard = _build_nav_tab_keyboard(window_id)
    if cached_file_id is not None:
        media = InputMediaPhoto(media=cached_file_id)
        png_size_label = "skipped"
        t_render = time.perf_counter()
    else:
        png_bytes = await text_to_image(text, with_ansi=True)
        media = InputMediaPhoto(media=io.BytesIO(png_bytes))
        png_size_label = f"{len(png_bytes)}B"
        t_render = time.perf_counter()
    try:
        result = await query.edit_message_media(media=media, reply_markup=keyboard)
        t_edit = time.perf_counter()
        if msg_id is not None and new_hash is not None:
            pane_cache.set_hash(msg_id, new_hash)
        if (
            cached_file_id is None
            and new_hash
            and hasattr(result, "photo")
            and getattr(result, "photo", None)
        ):
            photos = result.photo  # type: ignore[union-attr]
            if photos:
                pane_cache.set_file_id(new_hash, photos[-1].file_id)
        logger.debug(
            "TIMING screenshot_keys=%s%s: send=%.0fms wait=%.0fms render=%.0fms edit=%.0fms total=%.0fms png=%s",
            key_id,
            " (file_id reuse)" if cached_file_id else "",
            (t_send - t0) * 1000,
            (t_wait - t_send) * 1000,
            (t_render - t_wait) * 1000,
            (t_edit - t_render) * 1000,
            (t_edit - t0) * 1000,
            png_size_label,
        )
    except Exception:
        if cached_file_id is not None and new_hash:
            pane_cache.forget_file_id(new_hash)  # stale file_id


def _parse_cmd_payload(data: str, prefix: str) -> str:
    """Extract window_id suffix from a cm:<action>:<wid> callback string."""
    return data[len(prefix) :]


async def _cmd_refresh_photo(
    query: CallbackQuery, window_id: str, *, tab: str = "nav"
) -> bool:
    """Recapture the pane and swap the photo + caption in place.

    ``tab`` selects which keyboard tab to rebuild — "nav" after a key
    press, the action's home tab ("act"/"ses") after an action button.
    Returns True when the message is in the desired state (either edited
    or already identical — Telegram's "not modified" is treated as a
    success since there's nothing to show differently). Returns False
    only on real failures (agent gone, parse error, network).
    """
    from telegram.helpers import escape_markdown

    from ..screenshot import text_to_image
    from .commands import Tab, _build_commands_keyboard

    tab_typed: Tab
    if tab == "act":
        tab_typed = "act"
    elif tab == "ses":
        tab_typed = "ses"
    else:
        tab_typed = "nav"

    pane_text = await session_manager.capture_pane(window_id, with_ansi=True)
    if not pane_text:
        return False
    new_hash = pane_cache.pane_hash(pane_text)
    cached_file_id = pane_cache.get_file_id(new_hash)
    display = session_manager.get_display_name(window_id)
    safe_name = escape_markdown(display, version=2)
    caption = tr("cb.agent_header", name=safe_name)
    if cached_file_id is not None:
        media: InputMediaPhoto = InputMediaPhoto(
            media=cached_file_id, caption=caption, parse_mode="MarkdownV2"
        )
    else:
        png_bytes = await text_to_image(pane_text, with_ansi=True)
        media = InputMediaPhoto(
            media=io.BytesIO(png_bytes), caption=caption, parse_mode="MarkdownV2"
        )
    try:
        result = await query.edit_message_media(
            media=media,
            reply_markup=_build_commands_keyboard(window_id, tab=tab_typed),
        )
        if (
            cached_file_id is None
            and hasattr(result, "photo")
            and getattr(result, "photo", None)
        ):
            photos = result.photo  # type: ignore[union-attr]
            if photos:
                pane_cache.set_file_id(new_hash, photos[-1].file_id)
        msg_id = query.message.message_id if query.message else None
        if msg_id is not None:
            pane_cache.set_hash(msg_id, new_hash)
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg:
            # Pane hasn't changed since last render; treat as success.
            return True
        if cached_file_id is not None:
            pane_cache.forget_file_id(new_hash)  # stale
        logger.warning("commands photo refresh BadRequest: %s", e)
        return False
    except Exception as e:
        if cached_file_id is not None:
            pane_cache.forget_file_id(new_hash)
        logger.warning("commands photo refresh failed: %s", e)
        return False


# Map of slash-command prefixes → the DEFAULT (Claude Code) text typed into the
# pane. The real command is resolved through the window's runtime — codex's
# Context button types /status, not /context (runtimes.panel_slash_commands) —
# with this as the fallback for prefixes no runtime overrides.
_CMD_SLASH_MAP: dict[str, str] = {
    CB_CMD_CLEAR: "/clear",
    CB_CMD_COMPACT: "/compact",
    CB_CMD_MODEL: "/model",
    CB_CMD_MCP: "/mcp",
    CB_CMD_RESUME: "/resume",
    CB_CMD_CONTEXT: "/context",
    CB_CMD_EFFORT: "/effort",
}

# Slash-command prefix → logical panel action id (the same ids
# runtimes.AgentRuntime.panel_actions / panel_slash_commands key on), so the
# runtime can resolve the real command / a per-runtime post-slash renderer.
_CMD_ACTION_ID: dict[str, str] = {
    CB_CMD_CLEAR: "clear",
    CB_CMD_COMPACT: "compact",
    CB_CMD_MODEL: "model",
    CB_CMD_MCP: "mcp",
    CB_CMD_RESUME: "resume",
    CB_CMD_CONTEXT: "context",
    CB_CMD_EFFORT: "effort",
}

_CMD_DESTRUCTIVE_ACTIONS: dict[str, str] = {
    CB_CMD_CLEAR: "clear",
    CB_CMD_COMPACT: "compact",
    CB_CMD_KILL: "kill",
}

# Slash-buttons living on the «Сессия» tab — their post-action repaint
# returns there instead of «Действия» (see commands._action_home_tab for
# the confirm-flow counterpart).
_SES_TAB_PREFIXES = {CB_CMD_MODEL, CB_CMD_MCP, CB_CMD_RESUME, CB_CMD_CONTEXT}


# The Context button surfaces TUI-only diagnostic output as a chat message, and
# what that output IS depends on the runtime: Claude's /context (token
# breakdown) vs codex's /status (session status — codex has no /context). Each
# renderer is (pane marker to wait for, parse, format); a 3rd agent adds a row.
_CONTEXT_RENDERERS: dict[
    str, tuple[str, Callable[[str], dict | None], Callable[[dict], str]]
] = {
    "claude": ("Context Usage", parse_context_output, format_context_message),
    "codex": (CODEX_STATUS_MARKER, parse_codex_status, format_codex_status),
}


async def _post_slash_context(
    query: CallbackQuery,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    window_id: str,
) -> None:
    """Publish the Context button's TUI output as a Telegram chat message.

    The output (Claude /context or codex /status) is rendered purely in the pane
    (no JSONL trace), so we scrape the pane after it settles, dispatching parse +
    format by the window's runtime (``_CONTEXT_RENDERERS``). Two pane quirks:

    * The output can exceed the visible viewport (Claude /context is 40-70 lines
      with the MCP tool list), so the marker scrolls off — `scrollback_lines=200`
      includes enough history.
    * Render time varies, so poll up to ~5s for the parser to succeed instead of
      a fixed sleep, and bail with a debug log if it never does. The photo
      refresh that runs after still gives a visual fallback."""
    if not query.message:
        return
    spec = _CONTEXT_RENDERERS.get(session_manager.window_runtime(window_id))
    if spec is None:
        return
    marker, parse, fmt = spec
    parsed = None
    for _ in range(10):
        pane = await session_manager.capture_pane(window_id, scrollback_lines=200)
        if pane and marker in pane:
            parsed = parse(pane)
            if parsed:
                break
        await asyncio.sleep(0.5)
    if not parsed:
        logger.debug("post-slash context: no parseable block in pane after ~5s")
        return
    body = fmt(parsed)
    thread_id = get_thread_id(update)
    chat_id = (
        session_manager.resolve_chat_id(user.id, thread_id) or query.message.chat.id
    )
    try:
        await safe_send(
            context.bot,
            chat_id,
            body,
            message_thread_id=thread_id,
        )
    except Exception as e:
        logger.warning("post-slash context publish failed: %s", e)


# Per-slash-command post-action hooks. Run after the slash is sent and the
# pane has had ~1s to redraw. Photo refresh still runs after — these add
# *side effects* (like surfacing TUI-only data as text) and don't replace
# the default keyboard repaint. Add an entry here when a new slash command
# has data worth publishing as a chat message.
_POST_SLASH_HANDLERS = {
    CB_CMD_CONTEXT: _post_slash_context,
}


async def _handle_cmd_slash(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Entry point for the slash-commands and Убить buttons.

    For destructive actions (clear, kill) shows a confirmation keyboard
    first — actual work happens in _handle_cmd_confirm. For safe
    commands, sends the slash to Claude and refreshes the screenshot.
    """
    # Destructive first — swap to confirmation UI and bail. _show_confirm
    # also writes the action's CONFIRM_COPY description into the caption:
    # the grid buttons for Clear/End are deliberately neutral, so this
    # caption is the one place the data-loss warning is spelled out.
    for dest_prefix, action in _CMD_DESTRUCTIVE_ACTIONS.items():
        if data.startswith(dest_prefix):
            window_id = _parse_cmd_payload(data, dest_prefix)
            await _show_confirm(query, window_id, action)
            return

    # Safe slash-command path. Resolve the actual slash through the window's
    # runtime — codex's Context button types /status, not Claude's /context
    # (runtimes.panel_slash_commands); _CMD_SLASH_MAP is the fallback.
    from ..runtimes import get_runtime

    prefix = next(p for p in _CMD_SLASH_MAP if data.startswith(p))
    window_id = _parse_cmd_payload(data, prefix)
    action = _CMD_ACTION_ID.get(prefix)
    runtime = get_runtime(session_manager.window_runtime(window_id))
    slash = (runtime.panel_slash(action) if action else None) or _CMD_SLASH_MAP[prefix]

    success, msg = await session_manager.send_to_window(window_id, slash)
    await query.answer(f"{slash}" if success else f"❌ {msg}")
    await asyncio.sleep(1.0)
    post = _POST_SLASH_HANDLERS.get(prefix)
    if post:
        await post(query, update, context, user, window_id)
    # Repaint on the tab the button lives on (Model/MCP/Resume/Effort are on
    # «Сессия»; Context/Compact on «Действия»).
    home = "ses" if prefix in _SES_TAB_PREFIXES else "act"
    await _cmd_refresh_photo(query, window_id, tab=home)


async def _handle_cmd_tab(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Switch between Nav and Act tabs without touching the photo.

    Payload: cm:tab:<nav|act>:<window_id>. Pure keyboard swap via
    ``edit_message_reply_markup`` — no upload, no caption change. If
    the user taps the already-active tab the result is "not modified",
    which we silently swallow.
    """
    from .commands import Tab, _build_commands_keyboard

    rest = _parse_cmd_payload(data, CB_CMD_TAB)
    tab_id, _, window_id = rest.partition(":")
    if not window_id or tab_id not in ("nav", "act", "ses"):
        await query.answer(tr("cb.invalid_data"), show_alert=True)
        return
    tab_typed: Tab
    if tab_id == "act":
        tab_typed = "act"
    elif tab_id == "ses":
        tab_typed = "ses"
    else:
        tab_typed = "nav"
    try:
        await query.edit_message_reply_markup(
            reply_markup=_build_commands_keyboard(window_id, tab=tab_typed)
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug("tab switch edit failed: %s", e)
    except Exception as e:
        logger.debug("tab switch edit failed: %s", e)
    await query.answer()


async def _handle_cmd_mode_cycle(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Send Shift+Tab to cycle Claude Code between modes.

    Claude Code cycles normal → auto-accept → plan → normal on each
    Shift+Tab press. We don't know the current mode without parsing
    the terminal footer; the user sees the new one via the refreshed
    screenshot.
    """
    window_id = _parse_cmd_payload(data, CB_CMD_MODE_CYCLE)
    # BTab is tmux's name for back-tab (Shift+Tab).
    sent = await session_manager.send_keys(
        window_id, "BTab", enter=False, literal=False
    )
    if not sent:
        await query.answer(tr("cb.agent_unavailable"), show_alert=True)
        return
    await query.answer(tr("cb.mode_cycled"))
    await asyncio.sleep(0.3)
    await _cmd_refresh_photo(query, window_id, tab="act")


async def _handle_cmd_wipe_input(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Wipe whatever is typed in Claude's prompt input via Ctrl+U.

    Ctrl+U deletes from cursor to line start; repeating it clears
    across lines in multiline input (documented Claude Code behavior).
    Three presses cover the realistic worst case without the side
    effects of the alternatives: Esc interrupts a running turn, Ctrl+C
    exits Claude Code on a second press when the input is empty.
    """
    window_id = _parse_cmd_payload(data, CB_CMD_WIPE_INPUT)
    for _ in range(3):
        sent = await session_manager.send_keys(
            window_id, "C-u", enter=False, literal=False
        )
        if not sent:
            await query.answer(tr("cb.agent_unavailable"), show_alert=True)
            return
    await query.answer(tr("cb.input_wiped"))
    await asyncio.sleep(0.3)
    await _cmd_refresh_photo(query, window_id, tab="nav")


async def _handle_cmd_refresh(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Plain screenshot refresh — no side effects. Payload carries the tab."""
    rest = _parse_cmd_payload(data, CB_CMD_REFRESH)
    tab_id, _, window_id = rest.partition(":")
    if not window_id or tab_id not in ("nav", "act", "ses"):
        await query.answer(tr("cb.invalid_data"), show_alert=True)
        return
    ok = await _cmd_refresh_photo(query, window_id, tab=tab_id)
    await query.answer(
        tr("cb.refreshed") if ok else tr("cb.refresh_failed"), show_alert=not ok
    )


async def _wait_pane_ready(window_id: str, *, timeout: float = 20.0) -> None:
    """Poll until Claude Code's TUI has rendered, bounded by ``timeout``.

    After a kill+relaunch the pane is briefly empty/black while Claude boots
    — slower when the agent loads many MCP servers, which a fixed delay can't
    cover. Screenshotting then catches that black frame (the panel looks
    dead). Instead we wait for the input box to appear so the post-restart
    photo shows the live prompt. On timeout we fall through and screenshot
    whatever's there (e.g. a crashed launch) — fail-visible, never hangs.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = await session_manager.capture_pane(window_id)
        if pane and is_tui_ready(pane):
            return
        await asyncio.sleep(0.5)


async def _restart_agent(query: CallbackQuery, window_id: str, *, fresh: bool) -> None:
    """Relaunch the agent's Claude process. ``fresh=False`` resumes the
    current session_id; ``fresh=True`` starts a brand-new one (the old
    session's JSONL is untouched, so it stays in `/resume`).
    """
    from ..docker_driver import docker_driver

    ws = session_manager.get_window_state(window_id)
    resume_id = "" if fresh else (ws.session_id if ws else "")
    # Error alerts must come BEFORE the progress answer — a callback query
    # can be answered only once, a second answer() is silently ignored.

    if session_manager._is_docker_binding(window_id):
        agent = config.get_docker_agent(window_id[len("docker:") :])
        if not agent or not await docker_driver.is_container_alive(agent.container):
            await query.answer(tr("cb.container_not_running"), show_alert=True)
            return
        await query.answer(
            tr("cb.new_session_toast") if fresh else tr("cb.restarting_toast")
        )
        # send_lock: no other writer may type into the pane mid-restart.
        async with session_manager.send_lock(window_id):
            await docker_driver.kill_session(agent.container)
            await asyncio.sleep(1)
            await docker_driver.start_session(
                agent.container, resume_session_id=resume_id or None
            )
        await _wait_pane_ready(window_id)
        await _cmd_refresh_photo(query, window_id, tab="ses")
        return

    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        await query.answer(tr("cb.window_gone"), show_alert=True)
        return
    # /exit + relaunch are typed into the pane — on a busy agent they'd
    # land in the agent's prompt as text and it would keep running. Runtime-
    # aware: codex's busy state has different chrome than Claude's.
    pane = await tmux_manager.capture_pane(window_id)
    if pane and session_manager.is_agent_working(window_id, pane):
        await query.answer(
            tr("cb.agent_busy_stop_first"),
            show_alert=True,
        )
        return
    await query.answer(
        tr("cb.new_session_toast") if fresh else tr("cb.restarting_toast")
    )
    # Runtime-aware exit + relaunch (claude: /exit → `claude --name…`; codex:
    # /quit → `codex [resume …]`). launch_command validates/quotes and knows
    # each CLI's resume flag, so no per-runtime branch here.
    from ..runtimes import get_runtime

    runtime = get_runtime(ws.runtime if ws else None)
    # send_lock across exit → relaunch: anything typed into the pane in the 3s
    # gap would land in bash, not the agent.
    async with session_manager.send_lock(window_id):
        await tmux_manager.send_keys(window_id, runtime.exit_command())
        await asyncio.sleep(3)
        cmd = runtime.launch_command(w.window_name, resume_id or None)
        await tmux_manager.send_keys(window_id, cmd)
    await _wait_pane_ready(window_id)
    await _cmd_refresh_photo(query, window_id, tab="ses")


async def _show_confirm(query: CallbackQuery, window_id: str, action: str) -> None:
    """Swap the panel into the yes/cancel confirmation for ``action``.

    The explanation of what the action does goes into the panel photo's
    *caption* (above the button), not into the button label — Telegram clips
    long labels, so a description-in-button got truncated. The button stays a
    short «Да, …». The panel photo always carries a caption (👾 Агент: …), so
    editMessageCaption is reliable here. Actual work runs in
    _handle_cmd_confirm after the user confirms; _handle_cmd_cancel restores
    the base caption.
    """
    from telegram.helpers import escape_markdown

    from .commands import CONFIRM_COPY, _build_commands_keyboard

    description, _ = CONFIRM_COPY.get(action, ("", ""))
    display = session_manager.get_display_name(window_id)
    caption = tr("cb.agent_header", name=escape_markdown(display, version=2))
    if description:
        caption += f"\n\n{escape_markdown(description, version=2)}"
    try:
        await query.edit_message_caption(
            caption=caption,
            parse_mode="MarkdownV2",
            reply_markup=_build_commands_keyboard(window_id, confirming=action),
        )
        await query.answer(tr("cb.confirm_action"))
    except Exception as e:
        # Caption edit can fail (rare); fall back to keyboard-only so the
        # confirm button is still reachable.
        logger.warning("confirm caption edit failed, keyboard-only: %s", e)
        try:
            await query.edit_message_reply_markup(
                reply_markup=_build_commands_keyboard(window_id, confirming=action)
            )
            await query.answer(tr("cb.confirm_action"))
        except Exception as e2:
            logger.warning("confirmation ui failed: %s", e2)


async def _handle_cmd_restart(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Confirm, then restart the agent (resume same session). «Рестарт» and
    «Новая» both relaunch Claude and read identically from the panel, so each
    routes through a confirm step whose label explains what it does."""
    window_id = _parse_cmd_payload(data, CB_CMD_RESTART)
    await _show_confirm(query, window_id, "restart")


async def _handle_cmd_fresh(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Confirm, then restart the agent with a brand-new session_id; old session
    JSONL stays in the `/resume` picker so it's not lost."""
    window_id = _parse_cmd_payload(data, CB_CMD_FRESH)
    await _show_confirm(query, window_id, "fresh")


async def _handle_cmd_kill_confirmed(
    query: CallbackQuery,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    window_id: str,
) -> None:
    """Stop the agent and confirm in chat after the user pressed «⚠️ Да».

    Confirmation flow: clear the inline keyboard so the stale Action panel
    can't be tapped again, then post a fresh text message into the topic
    explaining what happened and how to revive the session. Earlier this
    code tried `edit_message_caption` on the panel photo, but a photo
    that was uploaded without a caption silently rejects the edit on some
    Telegram clients — the user saw no feedback. A new text message is
    100% reliable and leaves a clear trace in the topic history."""
    display = session_manager.get_display_name(window_id)
    killed = await session_manager.kill_agent(window_id)
    if not killed:
        await query.answer(tr("cb.kill_failed"), show_alert=True)
        return
    is_docker = session_manager._is_docker_binding(window_id)
    # Tmux: window is gone → unbind every topic pointing at it.
    # Docker: container stays up; /restart can revive the in-container
    # tmux session, so the topic binding stays intact.
    if not is_docker:
        for uid, bindings in list(session_manager.thread_bindings.items()):
            for tid, bound_wid in list(bindings.items()):
                if bound_wid == window_id:
                    session_manager.unbind_thread(uid, tid)

    # Drop the panel keyboard so the user can't tap stale buttons. Ignore
    # failure — the chat message below is the load-bearing feedback.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug("kill: keyboard clear failed: %s", e)

    # Confirmation message — explicit + actionable. Docker bindings get
    # a /restart hint because the topic is still useful; tmux bindings
    # have no agent to revive (the user creates a new session in a fresh
    # topic).
    thread_id = get_thread_id(update)
    chat_id = session_manager.resolve_chat_id(user.id, thread_id) or (
        query.message.chat.id if query.message else None
    )
    if chat_id is not None:
        if is_docker:
            body = tr("cb.session_ended_docker", display=display)
        else:
            body = tr("cb.session_ended_tmux", display=display)
        try:
            await safe_send(
                context.bot,
                chat_id,
                body,
                message_thread_id=thread_id,
            )
        except Exception as e:
            logger.warning("kill confirmation send failed: %s", e)
    await query.answer(tr("cb.done"))


async def _handle_cmd_confirm(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Execute a destructive action after user pressed the confirm button.

    Payload: cm:cfm:<action>:<window_id> — action ∈
    {clear, compact, kill, restart, fresh}.
    """
    rest = _parse_cmd_payload(data, CB_CMD_CONFIRM)
    action, _, window_id = rest.partition(":")
    if not action or not window_id:
        await query.answer(tr("cb.invalid_data"), show_alert=True)
        return

    if action == "kill":
        await _handle_cmd_kill_confirmed(query, update, context, user, window_id)
        return

    if action in ("restart", "fresh"):
        await _restart_agent(query, window_id, fresh=action == "fresh")
        return

    if action in ("clear", "compact"):
        slash = f"/{action}"
        success, msg = await session_manager.send_to_window(window_id, slash)
        # /clear creates a new session_id — drop our cached one so hook writes fresh
        if action == "clear" and success:
            session_manager.clear_window_session(window_id)
        toast = f"🧹 {slash}" if action == "clear" else f"🗜 {slash}"
        await query.answer(toast if success else f"❌ {msg}")
        await asyncio.sleep(1.0)
        await _cmd_refresh_photo(query, window_id, tab="act")
        return

    await query.answer(f"Unknown action: {action}", show_alert=True)


async def _handle_cmd_cancel(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Return the keyboard to its normal layout after cancelling confirmation.

    Cancel returns to the tab the confirmation was triggered from (carried
    in the payload: cm:can:<tab>:<wid>; a legacy payload without the tab
    falls back to «Действия»), so the user lands back on the same buttons.
    Also restores the base caption (👾 Агент: …), undoing the action
    description _show_confirm wrote above the button.
    """
    from telegram.helpers import escape_markdown

    from .commands import Tab, _build_commands_keyboard

    rest = _parse_cmd_payload(data, CB_CMD_CANCEL)
    tab_id, sep, wid_rest = rest.partition(":")
    tab_typed: Tab = "act"
    if sep and tab_id in ("nav", "act", "ses"):
        window_id = wid_rest
        if tab_id == "ses":
            tab_typed = "ses"
        elif tab_id == "nav":
            tab_typed = "nav"
    else:
        window_id = rest
    display = session_manager.get_display_name(window_id)
    caption = tr("cb.agent_header", name=escape_markdown(display, version=2))
    try:
        await query.edit_message_caption(
            caption=caption,
            parse_mode="MarkdownV2",
            reply_markup=_build_commands_keyboard(window_id, tab=tab_typed),
        )
    except Exception as e:
        logger.debug("cancel caption edit failed, keyboard-only: %s", e)
        try:
            await query.edit_message_reply_markup(
                reply_markup=_build_commands_keyboard(window_id, tab=tab_typed)
            )
        except Exception as e2:
            logger.debug("cancel edit failed: %s", e2)
    await query.answer(tr("cb.cancelled"))


async def _handle_status_refresh(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Rebuild the status body and edit the existing message in place."""
    from .commands import _build_status_text, _status_keyboard
    from .message_sender import safe_edit

    await query.answer(tr("cb.refreshing"))
    text = await _build_status_text()
    try:
        await safe_edit(query, text, reply_markup=_status_keyboard())
    except Exception as e:
        logger.warning("status refresh edit failed: %s", e)


# --- Dispatch table ---
# Each entry: (prefix, handler, is_startswith)
# Order matters: longer/more-specific prefixes first for startswith matching.

_EXACT_DISPATCH: dict[str, Any] = {
    CB_DIR_UP: _handle_dir_up,
    CB_DIR_CONFIRM: _handle_dir_confirm,
    CB_DIR_CANCEL: _handle_dir_cancel,
    CB_SESSION_NEW: _handle_session_new,
    CB_SESSION_BROWSE: _handle_session_browse,
    CB_SESSION_CANCEL: _handle_session_cancel,
    CB_RUNTIME_CANCEL: _handle_runtime_cancel,
    CB_STATUS_REFRESH: _handle_status_refresh,
    CB_WIN_NEW: _handle_win_new,
    CB_WIN_CANCEL: _handle_win_cancel,
    CB_WT_CANCEL: _handle_wt_cancel,
    "noop": _handle_noop,
}

# Ordered list of (prefix, handler) for startswith matching
_PREFIX_DISPATCH: list[tuple[str, Any]] = [
    (CB_HISTORY_PREV, _handle_history),
    (CB_HISTORY_NEXT, _handle_history),
    (CB_DIR_SELECT, _handle_dir_select),
    (CB_DIR_PAGE, _handle_dir_page),
    (CB_SESSION_SELECT, _handle_session_select),
    (CB_RUNTIME_SELECT, _handle_runtime_select),
    (CB_WIN_BIND, _handle_win_bind),
    (CB_SCREENSHOT_REFRESH, _handle_screenshot_refresh),
    # Permission relay
    # Interactive UI keys
    (CB_ASK_UP, _handle_interactive_key),
    (CB_ASK_DOWN, _handle_interactive_key),
    (CB_ASK_LEFT, _handle_interactive_key),
    (CB_ASK_RIGHT, _handle_interactive_key),
    (CB_ASK_ESC, _handle_interactive_key),
    (CB_ASK_ENTER, _handle_interactive_key),
    (CB_ASK_SPACE, _handle_interactive_key),
    (CB_ASK_TAB, _handle_interactive_key),
    (CB_ASK_REFRESH, _handle_interactive_refresh),
    # Screenshot quick keys
    (CB_KEYS_PREFIX, _handle_screenshot_keys),
    # Agent panel. Slash-command buttons share _handle_cmd_slash (it
    # routes destructive ones through the confirmation flow). Each
    # other action has its own handler; CB_CMD_TAB switches tabs
    # without touching the photo; CB_CMD_REFRESH carries the active
    # tab in its payload so the rebuilt keyboard matches what the
    # user was looking at.
    (CB_CMD_CLEAR, _handle_cmd_slash),
    (CB_CMD_COMPACT, _handle_cmd_slash),
    (CB_CMD_MODEL, _handle_cmd_slash),
    (CB_CMD_MCP, _handle_cmd_slash),
    (CB_CMD_RESUME, _handle_cmd_slash),
    (CB_CMD_CONTEXT, _handle_cmd_slash),
    (CB_CMD_EFFORT, _handle_cmd_slash),
    (CB_CMD_KILL, _handle_cmd_slash),
    (CB_CMD_MODE_CYCLE, _handle_cmd_mode_cycle),
    (CB_CMD_WIPE_INPUT, _handle_cmd_wipe_input),
    (CB_CMD_RESTART, _handle_cmd_restart),
    (CB_CMD_FRESH, _handle_cmd_fresh),
    (CB_CMD_TAB, _handle_cmd_tab),
    (CB_CMD_REFRESH, _handle_cmd_refresh),
    (CB_CMD_CONFIRM, _handle_cmd_confirm),
    (CB_CMD_CANCEL, _handle_cmd_cancel),
    # Worktree agents (parallel agents on one project)
    (CB_WT_NEW, _handle_wt_new),
    (CB_WT_DELOK, _handle_wt_delok),
    (CB_WT_DELNO, _handle_wt_delno),
    (CB_WT_DEL, _handle_wt_del),
    (CB_WT_DROP, _handle_wt_drop),
    (CB_WT_KEEP, _handle_wt_keep),
]


# Prefixes whose payload is exactly the window_id — and MUST match the
# topic's current binding before the action runs. tmux window ids (@N) are
# reused after a tmux-server restart: an old panel message in the chat can
# silently point at a *different* project's agent, and a tap on 🧹/⏹ there
# would clear or kill someone else's session. Splitting is prefix-strip
# (never rsplit) because docker binding values embed a colon
# ("docker:<agent>").
_WID_GUARD_SIMPLE: tuple[str, ...] = (
    CB_ASK_UP,
    CB_ASK_DOWN,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_ESC,
    CB_ASK_ENTER,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_REFRESH,
    CB_SCREENSHOT_REFRESH,
    CB_CMD_CLEAR,
    CB_CMD_COMPACT,
    CB_CMD_MODEL,
    CB_CMD_MCP,
    CB_CMD_RESUME,
    CB_CMD_CONTEXT,
    CB_CMD_MODE_CYCLE,
    CB_CMD_EFFORT,
    CB_CMD_WIPE_INPUT,
    CB_CMD_RESTART,
    CB_CMD_FRESH,
    CB_CMD_KILL,
    CB_WT_NEW,
    CB_WT_DEL,
)
# Prefixes with one routing field before the window_id
# (kb:<key>:<wid>, cm:ref:<tab>:<wid>, cm:tab:<tab>:<wid>, cm:cfm:<action>:<wid>).
# cm:can: is deliberately NOT guarded: a legacy cancel payload has no tab
# field (ambiguous parse for docker ids) and cancelling repaints only.
_WID_GUARD_ONE_FIELD: tuple[str, ...] = (
    CB_KEYS_PREFIX,
    CB_CMD_REFRESH,
    CB_CMD_TAB,
    CB_CMD_CONFIRM,
)


def _guarded_wid(data: str) -> str | None:
    """Extract the window_id from guarded callback payloads, else None."""
    for prefix in _WID_GUARD_SIMPLE:
        if data.startswith(prefix):
            return data[len(prefix) :] or None
    for prefix in _WID_GUARD_ONE_FIELD:
        if data.startswith(prefix):
            rest = data[len(prefix) :]
            _, sep, wid = rest.partition(":")
            return wid if sep and wid else None
    return None


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main callback query dispatcher — routes to specific handlers."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        await query.answer(tr("cb.not_authorized_toast"))
        return

    data = query.data

    # Capture group chat_id for supergroup forum topic routing.
    cb_thread_id = get_thread_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, cb_thread_id, chat.id)

    # Stale-panel guard: the tapped button must target the agent this topic
    # is CURRENTLY bound to. On mismatch, disarm the stale keyboard instead
    # of acting on whatever agent now owns the recycled window id.
    guarded = _guarded_wid(data)
    if guarded is not None:
        bound = session_manager.resolve_window_for_thread(user.id, cb_thread_id)
        if bound != guarded:
            logger.info(
                "Stale panel tap: data=%s bound=%s (user=%d thread=%s)",
                data,
                bound,
                user.id,
                cb_thread_id,
            )
            await query.answer(tr("cb.stale_panel"), show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.debug("stale panel keyboard clear failed: %s", e)
            return

    # Try exact match first
    handler = _EXACT_DISPATCH.get(data)
    if handler:
        await handler(query, data, update, context, user)
        return

    # Try prefix match
    for prefix, handler in _PREFIX_DISPATCH:
        if data.startswith(prefix):
            await handler(query, data, update, context, user)
            return

    # Plugin-contributed buttons (see plugins.callback_dispatch)
    for prefix, handler in plugins.callback_dispatch():
        if data.startswith(prefix):
            await handler(query, data, update, context, user)
            return

    logger.warning("Unknown callback data: %s", data)
