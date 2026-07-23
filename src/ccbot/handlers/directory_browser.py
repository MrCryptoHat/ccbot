"""Directory browser and window picker UI for session creation.

Provides UIs in Telegram for:
  - Window picker: list unbound tmux windows for quick binding
  - Directory browser: navigate directory hierarchies to create new sessions

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - User state keys for tracking browse/picker session
  - build_window_picker: Build unbound window picker UI
  - build_directory_browser: Build directory browser UI
  - clear_window_picker_state: Clear picker state from user_data
  - clear_browse_state: Clear browsing state from user_data
"""

import os
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..agent_session import AgentSession

from ..config import config
from ..i18n import tr
from ..runtimes import get_runtime, pickable_runtimes
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_RUNTIME_MENU,
    CB_RUNTIME_SELECT,
    CB_RUNTIME_TAB,
    CB_SESSION_BROWSE,
    CB_SESSION_CANCEL,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path
UNBOUND_WINDOWS_KEY = "unbound_windows"  # Cache of (name, cwd) tuples
STATE_SELECTING_SESSION = "selecting_session"
SESSIONS_KEY = "cached_sessions"  # Cache of AgentSession list (active runtime's)
PICKER_RUNTIME_KEY = "picker_runtime"  # active runtime in the session picker


def browse_start_path() -> str:
    """Where a fresh directory-browser session opens.

    ``CCBOT_BROWSE_ROOT`` when configured (the operator's sandbox), else the
    legacy default — the home directory.
    """
    return str(config.browse_root or Path.home())


def _can_go_up(path: Path) -> bool:
    """May «📁 ..» leave ``path``?

    No browse root configured → legacy behavior (anywhere above, until /).
    With a root: only while strictly INSIDE it — and a path outside the root
    (a remembered dir from an older config, a session-picker escape hatch)
    gets no "up" either, so it can't become an escape out of the sandbox.
    """
    if path == path.parent:
        return False  # filesystem root
    root = config.browse_root
    if root is None:
        return True
    return path != root and root in path.parents


def clamp_parent_path(current: str) -> str:
    """Parent of ``current`` for the «..» handler, clamped to the browse root.

    The up button is hidden at/outside the root, but a stale or crafted
    callback must not escape either.
    """
    cur = Path(current).expanduser().resolve()
    if _can_go_up(cur):
        return str(cur.parent)
    return str(config.browse_root) if config.browse_root is not None else str(cur)


def clear_browse_state(user_data: dict | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)


def clear_window_picker_state(user_data: dict | None) -> None:
    """Clear window picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(UNBOUND_WINDOWS_KEY, None)


def clear_session_picker_state(user_data: dict | None) -> None:
    """Clear session picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(SESSIONS_KEY, None)
        user_data.pop(PICKER_RUNTIME_KEY, None)


def build_window_picker(
    windows: list[tuple[str, str, str]],
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build window picker UI for unbound tmux windows.

    Args:
        windows: List of (window_id, window_name, cwd) tuples.

    Returns: (text, keyboard, window_ids) where window_ids is the ordered list for caching.
    """
    window_ids = [wid for wid, _, _ in windows]

    lines = [tr("dirb.winp_header")]
    for _wid, name, cwd in windows:
        display_cwd = cwd.replace(str(Path.home()), "~")
        lines.append(f"• `{name}` — {display_cwd}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(windows), 2):
        row = []
        for j in range(min(2, len(windows) - i)):
            name = windows[i + j][1]
            display = name[:12] + "…" if len(name) > 13 else name
            row.append(
                InlineKeyboardButton(
                    f"🖥 {display}", callback_data=f"{CB_WIN_BIND}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(tr("dirb.new_session"), callback_data=CB_WIN_NEW),
            InlineKeyboardButton(tr("commands.cancel"), callback_data=CB_WIN_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons), window_ids


def build_directory_browser(
    current_path: str, page: int = 0
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path(browse_start_path())

    try:
        subdirs = sorted(
            [
                d.name
                for d in path.iterdir()
                if d.is_dir()
                and (config.show_hidden_dirs or not d.name.startswith("."))
            ]
        )
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    # Navigation row (up/cancel), then the commit action on its OWN full-width
    # row — three buttons in one row clip the "select" label on a phone
    # («✅ This fol…»), and the primary action is the one that must stay
    # readable.
    nav_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root / the configured browse root
    if _can_go_up(path):
        nav_row.append(InlineKeyboardButton(tr("dirb.up"), callback_data=CB_DIR_UP))
    nav_row.append(
        InlineKeyboardButton(tr("commands.cancel"), callback_data=CB_DIR_CANCEL)
    )
    buttons.append(nav_row)
    buttons.append(
        [InlineKeyboardButton(tr("dirb.select_here"), callback_data=CB_DIR_CONFIRM)]
    )

    display_path = str(path).replace(str(Path.home()), "~")
    key = "dirb.header_empty" if not subdirs else "dirb.header"
    text = tr(key, path=display_path)

    return text, InlineKeyboardMarkup(buttons), subdirs


def _relative_time_short(file_path: str) -> str:
    """Compact age for a resume-button label («7д» / "7d") — a full-width
    button fits ~30 chars, so the session title gets the room."""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return ""
    delta = int(time.time() - mtime)
    if delta < 60:
        return tr("dirb.ts_now")
    if delta < 3600:
        return tr("dirb.ts_min", n=delta // 60)
    if delta < 86400:
        return tr("dirb.ts_hour", n=delta // 3600)
    return tr("dirb.ts_day", n=delta // 86400)


def _folder_lines(directory: str | None) -> list[str]:
    """The 📂 folder line shared by the picker and the runtime menu."""
    if not directory:
        return []
    try:
        shown = "~/" + str(Path(directory).relative_to(Path.home()))
    except ValueError:
        shown = directory
    # Inside a MarkdownV2 code span only backtick and backslash escape.
    code = shown.replace("\\", "\\\\").replace("`", "\\`")
    return [f"📂 `{code}`\n"]


# Session rows shown in the picker. Full-width rows cost vertical space, so
# the newest N (list_sessions delivers newest-first) are offered; anything
# older is reachable via the CLI's own /resume once a session is open.
PICKER_SESSION_ROWS = 5


def build_session_picker(
    sessions: list[AgentSession],
    directory: str | None = None,
    active_runtime: str = "claude",
) -> tuple[str, InlineKeyboardMarkup]:
    """Session picker with an agent-switcher row.

    The active runtime's resumable sessions are FULL-WIDTH resume buttons
    (title + short age — the info lives on the button, no numbered list in
    the message text), then «➕ Новая сессия — <agent>», then ONE
    «🤖 Агент: <agent> ▾» switcher row that opens the agent list
    (:func:`build_runtime_menu`). The switcher replaces the old wrapping tab
    row: it stays one row tall for ANY number of runtimes, and is hidden
    entirely when only one CLI is installed. Chosen over flat tabs / an
    agent-first two-step flow in a design review (2026-07-23): switching
    agents is the rare action, resuming is the common one.

    Args:
        sessions: resumable sessions of ``active_runtime`` for the folder
            (newest first). May be empty — the picker still offers "new
            session" and the switcher.
        directory: absolute path of the folder (``$HOME`` collapsed to ``~`` in
            the header) so the user can confirm the topic resolved as expected.
        active_runtime: the runtime whose sessions these are.

    Returns: (text, keyboard).
    """
    rt = get_runtime(active_runtime)
    lines = [tr("dirb.resume_header")]
    lines.extend(_folder_lines(directory))
    lines.append(tr("dirb.resume_found") if sessions else tr("dirb.no_sessions"))

    buttons: list[list[InlineKeyboardButton]] = []

    # Resume buttons, one full-width row per session — a 2-per-row grid
    # squeezed titles into ~14 chars of mush (the redesign's trigger). Indices
    # address the SESSIONS_KEY cache, so showing a prefix keeps them aligned.
    for i, s in enumerate(sessions[:PICKER_SESSION_ROWS]):
        title = s.summary[:24] + "…" if len(s.summary) > 24 else s.summary
        age = _relative_time_short(s.file_path)
        label = f"▶ {title} · {age}" if age else f"▶ {title}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"{CB_SESSION_SELECT}{i}")]
        )

    # ➕ New session names the agent it will start — with the tab row gone,
    # this button and the switcher are what carry the active runtime.
    buttons.append(
        [
            InlineKeyboardButton(
                tr("dirb.new_session_on", agent=rt.display_name),
                callback_data=f"{CB_RUNTIME_SELECT}{active_runtime}",
            )
        ]
    )
    if len(pickable_runtimes()) > 1:
        buttons.append(
            [
                InlineKeyboardButton(
                    tr("dirb.agent_row", agent=rt.display_name),
                    callback_data=CB_RUNTIME_MENU,
                )
            ]
        )
    # Escape hatch: the picker is shown after a name-based auto-bind, which
    # can resolve to the wrong folder. Browse drops into the directory browser
    # rooted at the matched dir (where "📁 .." navigates up) to pick another.
    buttons.append(
        [
            InlineKeyboardButton(
                tr("dirb.change_folder"), callback_data=CB_SESSION_BROWSE
            ),
            InlineKeyboardButton(
                tr("commands.cancel"), callback_data=CB_SESSION_CANCEL
            ),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)


def build_runtime_menu(
    directory: str | None = None,
    active_runtime: str = "claude",
    session_count: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """The agent list behind the picker's «🤖 Агент: … ▾» switcher row.

    One full-width row per installed runtime — vertical, so any number of
    CLIs fits without the wrapping-tab problem. The active runtime is marked
    ``●`` and shows its session count for the folder (counts for the OTHER
    runtimes would cost a full list_sessions scan each — deliberately not
    fetched). Every row routes through CB_RUNTIME_TAB, which re-enumerates
    that runtime's sessions and re-renders the picker, and «← Назад» is just
    CB_RUNTIME_TAB for the active runtime — the menu holds no state of its
    own.
    """
    lines = [tr("dirb.resume_header")]
    lines.extend(_folder_lines(directory))

    buttons: list[list[InlineKeyboardButton]] = []
    for rt in pickable_runtimes():
        if rt.name == active_runtime:
            # ▸ (the panel's active-tab convention), NOT ● — a filled circle
            # reads as just another runtime icon next to Grok's ⚫.
            label = f"▸ {rt.display_name} — {tr('dirb.sessions_n', n=session_count)}"
        else:
            label = f"{rt.picker_icon} {rt.display_name}".strip()
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"{CB_RUNTIME_TAB}{rt.name}")]
        )
    buttons.append(
        [
            InlineKeyboardButton(
                tr("dirb.back"), callback_data=f"{CB_RUNTIME_TAB}{active_runtime}"
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(buttons)
