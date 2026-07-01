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

from telegram import Update

from ..config import config


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
