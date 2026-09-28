"""Telegram bot handlers package — modular handler organization.

This package contains the Telegram bot handlers split by functionality:
  - callback_data: Callback data constants (CB_* prefixes)
  - callbacks: Callback query dispatch (inline keyboard presses)
  - commands: Command handlers (/start, /history, etc.) and topic lifecycle
  - media: Photo, document, voice message handlers
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers with MarkdownV2 fallback
  - history: Message history pagination
  - directory_browser: Directory selection UI
  - interactive_ui: Interactive UI (AskUserQuestion, Permission Prompt, etc.)
  - status_polling: Terminal status line polling
  - response_builder: Build paginated response messages

Shared utilities (get_thread_id, is_user_allowed) are defined here
and imported by the handler submodules.
"""

from telegram import Update, User

from ..config import config
from ..i18n import tr

ANONYMOUS_ADMIN_ID = 1087968824
"""@GroupAnonymousBot — the service account Telegram substitutes as the
sender whenever a group admin posts with «Remain anonymous» enabled."""


def effective_user(update: Update) -> User | None:
    """``update.effective_user`` with any configured alias resolved to its
    canonical id (``CCBOT_USER_ALIASES``). Every per-user structure keys on
    ``user.id``, so aliased accounts (a second personal account, an
    anonymous-admin post arriving as @GroupAnonymousBot) must collapse to one
    identity BEFORE the id is used as a state key — otherwise each alias gets
    its own parallel binding universe.

    The same collapse applies per TOPIC: an allowed user with no binding in a
    topic another allowed user's agent already serves acts as that owner
    (``session_manager.topic_owner``) — one topic, one agent, whoever types.
    Without it the second writer got a picker and a second window on the same
    session, and every reply arrived twice.

    Caveat: the rewritten ``User`` keeps the alias's own name/username and
    carries no bot binding — only ``.id`` is canonical. Read ids off it;
    don't feed it to ``mention_html()``/``send_message()``-style calls."""
    user = update.effective_user
    if user is None:
        return None
    canonical = config.canonical_user_id(user.id)
    owner = _topic_owner(update, canonical)
    if owner is not None:
        canonical = owner
    if canonical == user.id:
        return user
    data = user.to_dict()
    data["id"] = canonical
    return User.de_json(data, None)


def _topic_owner(update: Update, user_id: int) -> int | None:
    """The user whose agent serves this update's topic, if not ``user_id``.

    Only for an allowed sender (never a way past the allowlist) with no
    binding of its own in the topic.
    """
    from ..session import session_manager

    chat = update.effective_chat
    thread_id = get_thread_id(update)
    if chat is None or thread_id is None or user_id not in config.allowed_users:
        return None
    if session_manager.get_window_for_thread(user_id, thread_id) is not None:
        return None
    return session_manager.topic_owner(chat.id, thread_id, exclude=user_id)


def not_authorized_text(user_id: int | None) -> str:
    """Denial reply for an unauthorized sender. The anonymous-admin service
    id gets tailored advice — the generic «add the id to ALLOWED_USERS» is
    the classic wrong fix for it (see ``common.not_authorized_anon``)."""
    if user_id == ANONYMOUS_ADMIN_ID:
        return tr("common.not_authorized_anon", uid=user_id)
    return tr("common.not_authorized", uid=user_id if user_id is not None else "?")


def get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


def is_user_allowed(user_id: int | None) -> bool:
    """Check if a user ID is in the allowed list."""
    return user_id is not None and config.is_user_allowed(user_id)
