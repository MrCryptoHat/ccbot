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
from telegram.error import BadRequest, RetryAfter

from ..markdown_v2 import convert_markdown
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
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as primary_exc:
        if is_topic_gone_error(primary_exc):
            raise  # plain-text retry would fail the same way; let the worker purge
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            if is_topic_gone_error(e):
                raise
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    caption: str | None = None,
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

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
        return
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                caption=caption,
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(
                    media=io.BytesIO(raw_bytes),
                    caption=caption if i == 0 else None,
                )
                for i, (_media_type, raw_bytes) in enumerate(image_data)
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        if is_topic_gone_error(e):
            raise
        logger.error("Failed to send photo to %d: %s", chat_id, e)


async def send_document(
    bot: Bot,
    chat_id: int,
    file_path: str,
    filename: str | None = None,
    **kwargs: Any,
) -> None:
    """Send a document file to chat.

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
        return
    if filename is None:
        filename = os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filename,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send document to %d: %s", chat_id, e)


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
    except Exception as primary_exc:
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
