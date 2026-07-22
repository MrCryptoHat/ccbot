"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Uses telegramify-markdown for MarkdownV2 formatting.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Photo sending (single or media group)
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting, fallback to plain text
  - safe_send: Send message with formatting, fallback to plain text

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter — and "this topic is gone" (Topic_id_invalid) — are re-raised so the
queue worker can handle them (flood control / purge the deleted topic); the
plain-text fallback is skipped for those since it'd fail the same way.
"""

import io
import logging
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import BadRequest, NetworkError, RetryAfter

from ..markdown_v2 import convert_markdown
from ..rich_message import normalize_tables
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)


# Telegram errors that mean the destination forum topic no longer exists — the
# user deleted it. Distinct from "bad markdown" (retry as plain text won't
# help) and from "message can't be edited" (that's _is_dead_message_error in
# message_queue): the whole topic is gone, so re-raise and let the queue worker
# purge the binding (handlers/cleanup.purge_deleted_topic).
_TOPIC_GONE_MARKERS = ("topic_id_invalid", "message thread not found")


def is_topic_gone_error(exc: BaseException) -> bool:
    """True if ``exc`` means the destination forum topic was deleted."""
    if not isinstance(exc, BadRequest):
        return False
    s = str(exc).lower()
    return any(m in s for m in _TOPIC_GONE_MARKERS)


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter and NetworkError (incl. TimedOut) are re-raised for caller
    handling: after a client-side timeout the send may or may not have been
    committed by Telegram (the exact race the bumped photo timeouts in
    bot.py exist for) — an immediate plain-text resend here used to produce
    a duplicate. The queue worker retries these with a bounded backoff
    instead; the plain-text fallback stays reserved for what it was built
    for, formatting/entity BadRequests.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)

    async def _plain_fallback(primary_exc: Exception) -> Message | None:
        if is_topic_gone_error(primary_exc):
            raise primary_exc  # plain retry fails the same way; worker purges
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except BadRequest as e:
            if is_topic_gone_error(e):
                raise
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None
        except NetworkError:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None

    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    # ORDER MATTERS: in PTB, BadRequest is a SUBCLASS of NetworkError — a
    # bare `except NetworkError` first would swallow every parse error and
    # kill both the plain-text fallback and topic-gone detection.
    except BadRequest as primary_exc:
        return await _plain_fallback(primary_exc)
    except NetworkError:
        raise
    except Exception as primary_exc:
        return await _plain_fallback(primary_exc)


# sendRichMessage input cap is 32768 UTF-8 chars (Bot API 10.2); headroom for
# the auto-detected entities Telegram may add.
RICH_MESSAGE_MAX_CHARS = 32000


async def send_rich_message(
    bot: Bot,
    chat_id: int,
    markdown: str,
    *,
    thread_id: int | None = None,
    silent: bool = False,
    reply_markup: Any = None,
) -> int | None:
    """Send a Bot API 10.2 rich message (native tables / headings / code).

    PTB has no native method for it yet, so this goes through ``bot._post`` —
    which still rides ExtBot's rate limiter (``_do_post`` is the documented
    rate-limiting seam), so the stream/interactive/background contexts apply
    exactly like every other send. Telegram parses the ``markdown`` itself —
    no MarkdownV2 escaping on our side.

    Returns the sent message_id, or None on any non-flood failure — the caller
    falls back to the legacy MarkdownV2 path, so a rejected rich message never
    loses content. RetryAfter and topic-gone propagate unchanged: the worker's
    requeue / purge logic must stay uniform across send styles.
    """
    if not markdown or len(markdown) > RICH_MESSAGE_MAX_CHARS:
        return None
    # Normalized at the transport seam, not at the call sites: every rich send
    # must go through it (a table that interrupts a paragraph renders as pipe
    # soup — see rich_message.normalize_tables).
    markdown = normalize_tables(markdown)
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": {"markdown": markdown},
    }
    if thread_id is not None:
        data["message_thread_id"] = thread_id
    if silent:
        data["disable_notification"] = True
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    try:
        result = await bot._post("sendRichMessage", data)  # noqa: SLF001
    except RetryAfter:
        raise
    except BadRequest as e:
        if is_topic_gone_error(e):
            raise
        logger.warning("sendRichMessage rejected (%s) — legacy fallback", e)
        return None
    except Exception as e:  # noqa: BLE001 — any transport trouble → fallback
        logger.warning("sendRichMessage failed (%s) — legacy fallback", e)
        return None
    if isinstance(result, dict):
        mid = result.get("message_id")
        return int(mid) if mid else None
    return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    caption: str | None = None,
    **kwargs: Any,
) -> Message | None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.
    Returns the sent Message (the first one for a media group) on success,
    None on failure or empty input.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        caption: Optional caption — on the single photo, or on the first
            item of a media group (Telegram renders the group's caption
            from the first item).
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return None
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            return await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                caption=caption,
                **kwargs,
            )
        media = [
            InputMediaPhoto(
                media=io.BytesIO(raw_bytes),
                caption=caption if i == 0 else None,
            )
            for i, (_media_type, raw_bytes) in enumerate(image_data)
        ]
        sent = await bot.send_media_group(
            chat_id=chat_id,
            media=media,
            **kwargs,
        )
        return sent[0] if sent else None
    except RetryAfter:
        raise
    except Exception as e:
        if is_topic_gone_error(e):
            raise
        logger.error("Failed to send photo to %d: %s", chat_id, e)
        return None


async def send_document(
    bot: Bot,
    chat_id: int,
    file_path: str,
    filename: str | None = None,
    **kwargs: Any,
) -> Message | None:
    """Send a document file to chat.

    Returns the sent Message on success, None on failure.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        file_path: Path to file on disk
        filename: Override filename (defaults to basename of file_path)
        **kwargs: Extra kwargs passed to send_document
    """
    import os

    if not os.path.isfile(file_path):
        logger.error("File not found: %s", file_path)
        return None
    if filename is None:
        filename = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            return await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filename,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send document to %d: %s", chat_id, e)
        return None


async def send_voice(
    bot: Bot,
    chat_id: int,
    audio_data: bytes,
    **kwargs: Any,
) -> Message | None:
    """Send OGG/Opus audio as a Telegram voice message.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    try:
        return await bot.send_voice(
            chat_id=chat_id,
            voice=audio_data,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send voice to %d: %s", chat_id, e)
        return None


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await message.reply_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    # BadRequest before NetworkError — it's a subclass (see send_with_fallback).
    except BadRequest:
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise
    except NetworkError as e:
        # Ambiguous outcome (the reply may have been committed server-side);
        # a plain-text resend here would risk a duplicate for a one-off
        # interactive reply — surface the failure instead.
        logger.warning("Reply failed with a network error: %s", e)
        raise
    except Exception:
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        await target.edit_message_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await target.edit_message_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Send message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    # BadRequest before NetworkError — it's a subclass (see send_with_fallback).
    except BadRequest as primary_exc:
        if is_topic_gone_error(primary_exc):
            raise
        try:
            await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            if is_topic_gone_error(e):
                raise
            logger.error(f"Failed to send message to {chat_id}: {e}")
    except NetworkError as e:
        # See safe_reply: don't risk a duplicate on an ambiguous outcome.
        logger.warning("Send to %s failed with a network error: %s", chat_id, e)
    except Exception:
        try:
            await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            if is_topic_gone_error(e):
                raise
            logger.error(f"Failed to send message to {chat_id}: {e}")
