"""Media handlers — photo, document, and voice message processing.

Downloads media and forwards file paths or transcribed text to Claude
Code via tmux.
Rejects oversized files up front (Telegram caps bot `getFile` downloads
at 20 MB) with a clear message instead of a silent failure, and flags
compressed archives so the agent knows to unpack them.
"""

import logging
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from . import get_thread_id, is_user_allowed
from .delivery import deliver_user_text
from .message_sender import safe_reply
from ..config import config
from ..i18n import tr
from ..session import session_manager
from ..tmux_manager import tmux_manager
from ..transcribe import transcribe_voice
from ..utils import ccbot_dir

logger = logging.getLogger(__name__)


# --- Directories for incoming files ---
_IMAGES_DIR = ccbot_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

_FILES_DIR = ccbot_dir() / "files"
_FILES_DIR.mkdir(parents=True, exist_ok=True)

# Host path fragment under the agent workspace where inbound media lands.
# Claude inside a docker container sees the same files under
# ``/workspace/.inbox/``.
_DOCKER_INBOX_DIRNAME = ".inbox"

# Telegram caps bot file *downloads* (getFile) at 20 MB — there's no
# server-side workaround short of running a local Bot API server. We
# check ``Document.file_size`` up front (and catch the BadRequest as a
# backstop) so a too-big file gets a clear "split it up" reply instead
# of a silent failure the agent never even hears about.
_TG_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024

# Extensions we flag as "compressed archive the agent must unpack before
# it can read the contents". Deliberately excludes zip-based document
# formats (.docx / .xlsx / .pptx / .apkg / .epub) — those are handled as
# their own type, not unpacked.
_ARCHIVE_SUFFIXES = (
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".tar.zst",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
)


def _is_archive(name: str) -> bool:
    """True if ``name`` looks like a compressed archive (needs unpacking)."""
    low = name.lower()
    return any(low.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES)


def _safe_filename_component(name: str) -> str:
    """Strip directory components from a Telegram-supplied file name.

    Telegram clients can set ``document.file_name`` to anything —
    ``"../../.credentials.json"``, ``"/etc/passwd"``, backslash-separated
    paths on Windows, or a leading dot that would hide the file. We take
    just the basename, reject empty / dot-only / traversal results, and
    replace any residual slashes. The caller falls back to a unique-id
    name if we return empty.
    """
    # Handle both unix and windows separators defensively; Path(...).name
    # only strips one. Replacing first guarantees basename semantics.
    cleaned = name.replace("\\", "/").split("/")[-1]
    # Reject names whose basename is empty, pure dots, or starts with a
    # dot (would make ``<ts>_.hidden`` but still flagged — too close to
    # dotfile territory for a bulk inbox dir).
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("."):
        return ""
    return cleaned


def _inbound_save_path(wid: str, filename: str, default_dir: Path) -> tuple[Path, str]:
    """Pick the host path to save inbound media and the marker Claude will see.

    Tmux bindings: save to ``default_dir`` on host; marker = host path (Claude
    reads directly from ccbot's dir).

    Docker bindings: save under ``<agent.workspace_host_path>/.inbox/`` so it's
    visible inside the container as ``/workspace/.inbox/``. The marker uses
    the in-container path — the host-side ccbot dirs aren't bind-mounted
    into the container and would be dead references there.

    Returns (host_path, marker_str).
    """
    if session_manager._is_docker_binding(wid):
        agent = config.get_docker_agent(wid[len("docker:") :])
        if agent:
            host_dir = agent.workspace_host_path / _DOCKER_INBOX_DIRNAME
            host_dir.mkdir(parents=True, exist_ok=True)
            host_path = host_dir / filename
            marker = f"/workspace/{_DOCKER_INBOX_DIRNAME}/{filename}"
            return host_path, marker
    host_path = default_dir / filename
    return host_path, str(host_path)


async def _validate_media_context(
    update: Update,
    user_id: int,
    thread_id: int | None,
) -> tuple[str, None] | tuple[None, str]:
    """Validate that a media message can be forwarded to Claude Code.

    Returns (window_id, None) on success, or (None, error_message) on failure.
    """
    if thread_id is None:
        if update.message:
            await safe_reply(
                update.message,
                tr("media.use_topic"),
            )
        return None, "no_topic"

    wid = session_manager.get_window_for_thread(user_id, thread_id)
    if wid is None:
        if update.message:
            await safe_reply(
                update.message,
                tr("media.no_binding"),
            )
        return None, "no_binding"

    # Stale-check is tmux-specific (find_window_by_id returns None for
    # docker bindings — would spuriously unbind a healthy container). For
    # docker bindings we let send_to_window do its own container-alive
    # check inline.
    if not session_manager._is_docker_binding(wid):
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            session_manager.unbind_thread(user_id, thread_id)
            if update.message:
                await safe_reply(
                    update.message,
                    tr("media.window_gone", name=display),
                )
            return None, "window_gone"

    return wid, None


async def _deliver_media_text(
    update: Update,
    user_id: int,
    thread_id: int | None,
    wid: str,
    text_to_send: str,
) -> tuple[str, str]:
    """Deliver a media marker through the shared text pipeline.

    Returns ("ok", ""), ("blocked", "") after telling the user what to do,
    or ("error", detail). "routed" (marker typed into an open
    AskUserQuestion's free-text option) counts as ok — answering a
    question with a photo/file is legitimate.
    """
    status, detail = await deliver_user_text(
        user_id,
        thread_id,
        wid,
        text_to_send,
        ack_chat_id=update.message.chat.id if update.message else None,
        ack_message_id=update.message.message_id if update.message else None,
    )
    if status in ("routed", "sent"):
        return "ok", ""
    if status in ("blocked_no_text_option", "blocked_widget"):
        if update.message:
            await safe_reply(update.message, tr("media.blocked_widget"))
        return "blocked", ""
    return "error", detail


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(
                update.message,
                tr("common.not_authorized", uid=user.id if user else "?"),
            )
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    wid, err = await _validate_media_context(update, user.id, thread_id)
    if err or not wid:
        return

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    host_path, marker_path = _inbound_save_path(wid, filename, _IMAGES_DIR)
    await tg_file.download_to_drive(host_path)

    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(image attached: {marker_path})"
    else:
        text_to_send = f"(image attached: {marker_path})"

    await update.message.chat.send_action(ChatAction.TYPING)

    # Same pre-send pipeline as typed text: with an interactive widget on
    # screen a raw send_to_window would type the marker into the widget and
    # press Enter — i.e. activate the highlighted option (on a permission
    # prompt: grant it). The file is already saved; only delivery waits.
    status, detail = await _deliver_media_text(
        update, user.id, thread_id, wid, text_to_send
    )
    if status == "blocked":
        return
    if status == "error":
        await safe_reply(update.message, f"❌ {detail}")
        return

    await safe_reply(update.message, tr("media.image_sent"))


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle documents sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(
                update.message,
                tr("common.not_authorized", uid=user.id if user else "?"),
            )
        return

    if not update.message or not update.message.document:
        return

    chat = update.message.chat
    thread_id = get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    wid, err = await _validate_media_context(update, user.id, thread_id)
    if err or not wid:
        return

    doc = update.message.document

    if doc.file_size and doc.file_size > _TG_DOWNLOAD_LIMIT_BYTES:
        mb = round(doc.file_size / 1024 / 1024)
        await safe_reply(
            update.message,
            tr("media.file_too_big", mb=mb),
        )
        return

    raw_name = doc.file_name or f"file_{doc.file_unique_id}"
    original_name = _safe_filename_component(raw_name) or f"file_{doc.file_unique_id}"
    filename = f"{int(time.time())}_{original_name}"
    host_path, marker_path = _inbound_save_path(wid, filename, _FILES_DIR)

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(host_path)
    except TelegramError as e:
        logger.warning("Failed to download document %r: %s", original_name, e)
        too_big = "too big" in str(e).lower()
        await safe_reply(
            update.message,
            tr("media.download_failed")
            + (tr("media.download_failed_too_big") if too_big else f" ({e})"),
        )
        return

    is_archive = _is_archive(original_name)
    if is_archive:
        attach_line = (
            f"(archive attached: {marker_path} — it's a compressed archive: "
            "unpack it first (e.g. `unzip` / `tar -xf`), then work with the "
            "contents. Ask me if it's not clear what I want done with it.)"
        )
    else:
        attach_line = f"(file attached: {marker_path})"

    caption = update.message.caption or ""
    text_to_send = f"{caption}\n\n{attach_line}" if caption else attach_line

    await update.message.chat.send_action(ChatAction.TYPING)

    # Same widget guard as photos — see photo_handler.
    status, detail = await _deliver_media_text(
        update, user.id, thread_id, wid, text_to_send
    )
    if status == "blocked":
        return
    if status == "error":
        await safe_reply(update.message, f"❌ {detail}")
        return

    if is_archive:
        await safe_reply(
            update.message,
            tr("media.archive_sent", name=original_name),
        )
    else:
        await safe_reply(update.message, tr("media.file_sent", name=original_name))


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(
                update.message,
                tr("common.not_authorized", uid=user.id if user else "?"),
            )
        return

    if not update.message or not update.message.voice:
        return

    if not config.deepgram_api_key and not config.openai_api_key:
        await safe_reply(
            update.message,
            tr("media.voice_needs_key"),
        )
        return

    chat = update.message.chat
    thread_id = get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    wid, err = await _validate_media_context(update, user.id, thread_id)
    if err or not wid:
        return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    try:
        text = await transcribe_voice(ogg_data)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, tr("media.transcribe_failed", err=e))
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    # Same pre-send pipeline as typed text: a dictated answer to an open
    # AskUserQuestion must land in its text option, not in the option
    # picker (where Enter would select the highlighted default and the
    # dictated words would be lost); other widgets block the send.
    status, detail = await deliver_user_text(
        user.id,
        thread_id,
        wid,
        text,
        ack_chat_id=update.message.chat.id if update.message else None,
        ack_message_id=update.message.message_id if update.message else None,
    )
    if status in ("routed", "sent"):
        await safe_reply(update.message, f'🎤 "{text}"')
        return
    if status == "blocked_no_text_option":
        await safe_reply(
            update.message,
            tr("media.voice_blocked_no_text_option", text=text),
        )
        return
    if status == "blocked_widget":
        await safe_reply(
            update.message,
            tr("media.voice_blocked_widget", text=text),
        )
        return
    await safe_reply(update.message, f"❌ {detail}")
