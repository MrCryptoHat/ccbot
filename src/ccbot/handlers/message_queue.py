"""Per-topic message queue management for ordered message delivery.

Provides a queue-based message processing system that ensures:
  - Messages within a topic are sent in receive order (FIFO)
  - Status messages always follow content messages within a topic
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Queues are keyed by (user_id, thread_id_or_0). This means a long content
stream in one topic does not serialize deliveries in the user's other
topics — each topic gets its own worker task. Ordering guarantees are
only meaningful within a topic anyway, so there is no semantic loss.

Rate limiting is handled globally by AIORateLimiter on the Application;
flood-control state is kept per user (Telegram's 429 is per-bot), so a
429 caused by one topic correctly pauses all of that user's topics.

Key components:
  - MessageTask: Dataclass representing a queued message task (with thread_id)
  - get_or_create_queue: Get or create queue and worker for (user, topic)
  - Message queue worker: Background task processing one topic's queue
  - Content task processing with tool_use/tool_result handling
  - Status message tracking and conversion (keyed by (user_id, thread_id))
"""

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from telegram import Bot
from telegram.error import BadRequest, RetryAfter

from ..i18n import tr
from ..links import extract_urls, format_links_block
from ..markdown_v2 import PLACEHOLDER_RE, convert_markdown
from ..rate_limiter import stream_context
from ..screenshot import text_to_image
from ..session import session_manager
from ..voice import (
    BudgetEvent,
    is_fresh_for_voice,
    split_voice_segments,
    strip_output_tags,
    synthesize_speech,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    is_topic_gone_error,
    send_document,
    send_photo,
    send_voice,
    send_with_fallback,
    strip_sentinels,
)
from .reaction_confirm import note_topic_message

logger = logging.getLogger(__name__)


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal["content"]
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    # Aligned monospace text for wide tables / box-art, rendered to PNG at
    # send time. Referenced by IMG placeholder lines in ``parts`` (see
    # markdown_v2.render_tables_for_chat).
    table_texts: list[str] = field(default_factory=list)
    # (filename, content) for long code blocks sent as document attachments.
    # Referenced by FILE placeholder lines in ``parts``. A task carrying
    # either of these out-of-band lists is never merged.
    code_files: list[tuple[str, str]] = field(default_factory=list)
    # Voice-mode state captured at enqueue time (composition-time snapshot)
    # to avoid TOCTOU: if /voice flips between enqueue and dequeue, the
    # message still delivers in the mode it was composed for.
    voice_mode: bool = False
    # How many times this task was requeued after a RetryAfter (flood
    # control) — bounds the retry loop, see _requeue_content_task.
    flood_requeues: int = 0


# Per-topic message queues and worker tasks, keyed by (user_id, thread_id_or_0).
# Sharding by topic (not user) means one topic's heavy output does not delay
# message delivery in the user's other topics.
QueueKey = tuple[int, int]
_message_queues: dict[QueueKey, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[QueueKey, asyncio.Task[None]] = {}
_queue_locks: dict[QueueKey, asyncio.Lock] = {}  # Protect drain/refill operations

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking lives in session_manager (persisted across restarts).
# This module only holds ephemeral coordination state for the rolling status:
# the per-topic "last edit monotonic time" used for debouncing and the
# tool-status protection timer. Both are safe to reset on restart — they only
# affect the next second or two of edit pacing, not user-visible message state.

# Minimum interval between successive edits of the same rolling status message,
# Flood control: user_id -> monotonic time when ban expires
_flood_until: dict[int, float] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10


def get_message_queue(
    user_id: int, thread_id: int | None = None
) -> asyncio.Queue[MessageTask] | None:
    """Get the message queue for a topic (if exists)."""
    return _message_queues.get((user_id, thread_id or 0))


def get_or_create_queue(
    bot: Bot, user_id: int, thread_id: int | None = None
) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a topic."""
    key: QueueKey = (user_id, thread_id or 0)
    if key not in _message_queues:
        _message_queues[key] = asyncio.Queue()
        _queue_locks[key] = asyncio.Lock()
        # Start worker task for this topic
        _queue_workers[key] = asyncio.create_task(_message_queue_worker(bot, key))
    return _message_queues[key]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    # tool_use/tool_result break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    if base.content_type in ("tool_use", "tool_result"):
        return False
    if candidate.content_type in ("tool_use", "tool_result"):
        return False
    # Voice-mode snapshots must match — merging a voice-composed task with
    # a text-composed task would deliver one of them in the wrong mode.
    if base.voice_mode != candidate.voice_mode:
        return False
    # Content types must match: the merged task inherits the first task's
    # content_type, so merging text into a thinking-first task would
    # deliver the actual reply silently (and vice versa would ring for
    # thinking).
    if base.content_type != candidate.content_type:
        return False
    # Tasks carrying out-of-band blocks (table/box-art images, code files)
    # aren't merged: the merged task would drop those lists (not copied in
    # _merge_content_tasks) and placeholder indices from two tasks would
    # collide. Such messages are rare, so losing the merge is cheap.
    if base.table_texts or candidate.table_texts:
        return False
    if base.code_files or candidate.code_files:
        return False
    # Image-carrying tasks (diff screenshots, tool_result images) must not
    # merge: _merge_content_tasks rebuilds the task from parts only and would
    # silently drop image_data. (content_type already differs for these, but
    # two diff tasks share content_type "diff" — guard explicitly.)
    if base.image_data or candidate.image_data:
        return False
    return True


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            thread_id=first.thread_id,
            voice_mode=first.voice_mode,
        ),
        merge_count,
    )


MAX_FLOOD_REQUEUES = 3


async def _requeue_content_task(
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    task: MessageTask,
) -> None:
    """Put a flood-controlled content task back at the head of its queue.

    The worker's RetryAfter handler only waits out the ban; without this
    the content that hit the 429 would be silently dropped. Queue counter
    note: the original count is consumed by the worker's task_done(), the
    put_nowait here adds the replacement; drained items get the same
    put/task_done compensation as in _merge_content_tasks.
    """
    if not task.parts and not task.image_data:
        return
    if task.flood_requeues >= MAX_FLOOD_REQUEUES:
        logger.error(
            "Dropping content task after %d flood-control requeues "
            "(window=%s, %d parts)",
            task.flood_requeues,
            task.window_id,
            len(task.parts),
        )
        return
    task.flood_requeues += 1
    async with lock:
        pending = _inspect_queue(queue)
        queue.put_nowait(task)
        for item in pending:
            queue.put_nowait(item)
            queue.task_done()


async def _dispatch_task(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    task: MessageTask,
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
) -> None:
    """Handle one dequeued task. Assumes caller holds ``stream_context()``."""
    # Flood control is per-user (Telegram 429 is per-bot): pause every
    # topic belonging to this user until the ban expires.
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > 0:
        remaining = flood_end - time.monotonic()
        if remaining > 0:
            # Content is actual Claude output — wait then send
            logger.debug(
                "Flood controlled: waiting %.0fs for content (user %d)",
                remaining,
                user_id,
            )
            await asyncio.sleep(remaining)
        # Ban expired
        _flood_until.pop(user_id, None)
        logger.info("Flood control lifted for user %d", user_id)

    merged_task, merge_count = await _merge_content_tasks(queue, task, lock)
    if merge_count > 0:
        logger.debug(
            "Merged %d tasks for user=%d thread=%d",
            merge_count,
            user_id,
            thread_id_or_0,
        )
        for _ in range(merge_count):
            queue.task_done()
    try:
        await _process_content_task(bot, user_id, merged_task)
    except RetryAfter:
        # Keep the unsent content (the task trims already-delivered
        # parts before re-raising); the worker waits out the ban.
        await _requeue_content_task(queue, lock, merged_task)
        raise


async def _message_queue_worker(bot: Bot, key: QueueKey) -> None:
    """Process message tasks for one topic sequentially.

    Runs task handling inside ``stream_context()`` so ``CcbotRateLimiter``
    tags every send / edit reached from here as stream traffic and
    applies the per-topic 20/60s bucket. Ad-hoc sends from command
    handlers and other call sites stay outside the context and skip
    the group limiter entirely.
    """
    user_id, thread_id_or_0 = key
    queue = _message_queues[key]
    lock = _queue_locks[key]
    logger.info(
        "Message queue worker started for user=%d thread=%d", user_id, thread_id_or_0
    )

    while True:
        try:
            task = await queue.get()
            try:
                with stream_context():
                    await _dispatch_task(
                        bot, user_id, thread_id_or_0, task, queue, lock
                    )
            except RetryAfter as e:
                retry_secs = (
                    e.retry_after
                    if isinstance(e.retry_after, int)
                    else int(e.retry_after.total_seconds())
                )
                if retry_secs > FLOOD_CONTROL_MAX_WAIT:
                    _flood_until[user_id] = time.monotonic() + retry_secs
                    logger.warning(
                        "Flood control for user %d: retry_after=%ds, "
                        "pausing queues until ban expires",
                        user_id,
                        retry_secs,
                    )
                else:
                    logger.warning(
                        "Flood control for user %d: waiting %ds",
                        user_id,
                        retry_secs,
                    )
                    await asyncio.sleep(retry_secs)
            except Exception as e:
                if is_topic_gone_error(e):
                    # A send bounced because the user deleted this topic — the
                    # authoritative, immediate signal (the periodic probe is
                    # only a backstop for idle topics). Tear it down.
                    from .cleanup import purge_deleted_topic

                    try:
                        await purge_deleted_topic(
                            bot,
                            user_id,
                            thread_id_or_0,
                            task.window_id or "",
                        )
                    except Exception as purge_exc:
                        logger.warning(
                            "purge_deleted_topic failed (user=%d thread=%d): %s",
                            user_id,
                            thread_id_or_0,
                            purge_exc,
                        )
                else:
                    logger.error(
                        "Error processing message task (user=%d thread=%d): %s",
                        user_id,
                        thread_id_or_0,
                        e,
                    )
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(
                "Message queue worker cancelled for user=%d thread=%d",
                user_id,
                thread_id_or_0,
            )
            break
        except Exception as e:
            logger.error(
                "Unexpected error in queue worker (user=%d thread=%d): %s",
                user_id,
                thread_id_or_0,
                e,
            )


# Telegram errors that mean "this message can no longer be edited" — either the
# user deleted it, it's too old for edit (48h+), or the message_id was bogus.
# These force the caller to drop its tracked id and send a fresh message.
_DEAD_MSG_MARKERS = (
    "message to edit not found",
    "message_id_invalid",
    "message can't be edited",
    "message to edit is too old",
)


def _is_dead_message_error(exc: BaseException) -> bool:
    """Return True if the error indicates the message cannot be edited at all."""
    if not isinstance(exc, BadRequest):
        return False
    s = str(exc).lower()
    return any(marker in s for marker in _DEAD_MSG_MARKERS)


async def _try_edit_with_fallback(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    raw_text: str | None = None,
) -> bool | None:
    """Try editing a message with MarkdownV2, falling back to plain text.

    Returns:
        True  — edit succeeded.
        False — edit failed but the message exists (transient). Caller keeps tracking.
        None  — the message is dead (deleted / too old / invalid). Caller should
                drop its tracked id and send a fresh message.

    Re-raises RetryAfter (flood control) and topic-gone errors (let the worker
    purge the deleted topic).
    """
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return True
    except RetryAfter:
        raise
    except Exception as primary_exc:
        if is_topic_gone_error(primary_exc):
            raise
        if _is_dead_message_error(primary_exc):
            return None
        try:
            plain = strip_sentinels(raw_text or text)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return True
        except RetryAfter:
            raise
        except Exception as fallback_exc:
            if is_topic_gone_error(fallback_exc):
                raise
            if _is_dead_message_error(fallback_exc):
                return None
            return False


class _SendKwargs(TypedDict, total=False):
    message_thread_id: int
    disable_notification: bool


# Plumbing content types that always send silently. ``text`` is NOT
# here — every reply from the agent is a message the user wants to know
# about, and we'd rather over-notify than silently swallow one. Anything
# outside this set defaults to ringing.
_SILENT_CONTENT_TYPES: frozenset[str] = frozenset(
    {"tool_use", "tool_result", "thinking", "local_command", "diff"}
)


def _is_silent_content_type(content_type: str) -> bool:
    """Return True iff this content_type should send with disable_notification."""
    return content_type in _SILENT_CONTENT_TYPES


def _send_kwargs(thread_id: int | None, *, silent: bool = False) -> _SendKwargs:
    """Build common bot.send_*() kwargs.

    ``silent=True`` adds ``disable_notification=True`` so the message
    arrives without a sound/banner. Use it for plumbing the user doesn't
    need to be pinged about (tool events, thinking, status spinner).
    """
    kw: _SendKwargs = {}
    if thread_id is not None:
        kw["message_thread_id"] = thread_id
    if silent:
        kw["disable_notification"] = True
    return kw


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send images attached to a task, if any.

    For a tool_result task the image is something the agent is *looking at*
    (a screenshot it Read, a browser capture, a Bash-generated plot), so it
    gets a «👀 Агент смотрит» caption to set it apart from images the agent
    produced as its own reply. text-task images (rare) go uncaptioned.
    """
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    caption = tr("mq.agent_looking") if task.content_type == "tool_result" else None
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        caption=caption,
        # Images riding along with a silent task (tool_result screenshot,
        # intermediate-turn diagram, …) shouldn't ping either.
        **_send_kwargs(  # type: ignore[arg-type]
            task.thread_id, silent=_is_silent_content_type(task.content_type)
        ),
    )


async def _notify_budget_warning(bot: Bot, event: BudgetEvent) -> None:
    """Post a one-shot 80%-of-daily-limit notice to the General notifications topic.

    Quietly returns when ``NOTIFICATIONS_CHAT_ID`` is unset — the budget
    still works, the user just doesn't get the heads-up. HTML parse mode
    numbers don't need escaping.
    """
    from ..config import config

    chat_id = config.notifications_chat_id
    if chat_id is None:
        return
    used = event.chars_used
    limit = event.daily_limit
    remaining = max(0, limit - used)
    text = tr(
        "mq.voice_budget_warning",
        used=f"{used:,}".replace(",", " "),
        limit=f"{limit:,}".replace(",", " "),
        remaining=f"{remaining:,}".replace(",", " "),
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning("voice budget 80%% notice failed: %s", e)


async def _notify_budget_exhausted(bot: Bot) -> None:
    """Disable voice in every topic and notify each + the General topic.

    Called once when ``record()`` reports ``crossed_exhausted``. Also
    safe to call from the pre-check path as a fallback if a parallel
    synth race left voice_mode_topics partially populated past
    exhaustion — disable_all is idempotent (clears whatever's there
    and returns the list).
    """
    from ..config import config

    disabled = session_manager.voice_budget_disable_all()
    per_topic_text = tr("mq.voice_exhausted_topic")
    for uid, tid in disabled:
        try:
            chat_id = session_manager.resolve_chat_id(uid, tid)
            await bot.send_message(
                chat_id=chat_id,
                text=per_topic_text,
                message_thread_id=tid if tid else None,
            )
        except Exception as e:
            logger.warning(
                "voice exhaustion notice failed (user=%d thread=%d): %s",
                uid,
                tid,
                e,
            )

    chat_id = config.notifications_chat_id
    if chat_id is not None:
        budget = session_manager.voice_budget
        summary = tr(
            "mq.voice_exhausted_summary",
            used=f"{budget.chars_used:,}".replace(",", " "),
            limit=f"{budget.daily_limit:,}".replace(",", " "),
        )
        try:
            await bot.send_message(chat_id=chat_id, text=summary, parse_mode="HTML")
        except Exception as e:
            logger.warning("voice exhaustion summary failed: %s", e)


async def _ensure_voice_disabled_for_exhausted_budget(bot: Bot) -> None:
    """Pre-check path: budget already exhausted, but a topic may still
    have ``voice_mode_topics`` populated (race between parallel synths
    crossing the line). Idempotent — does nothing if already cleared
    AND already notified.
    """
    if session_manager.voice_mode_topics:
        await _notify_budget_exhausted(bot)


async def _send_table_image(
    bot: Bot, chat_id: int, aligned_text: str, *, thread_id: int | None, silent: bool
) -> None:
    """Render an aligned monospace table to a PNG and send it as a photo.

    Falls back to a code block on render failure so a wide table is never
    lost — at worst it wraps, which is still the data.
    """
    try:
        png = await text_to_image(aligned_text, with_ansi=False, square=False)
    except Exception as e:
        logger.warning("table image render failed, falling back to code block: %s", e)
        await send_with_fallback(
            bot,
            chat_id,
            "```\n" + aligned_text + "\n```",
            **_send_kwargs(thread_id, silent=silent),  # type: ignore[arg-type]
        )
        return
    n_lines = aligned_text.count("\n") + 1
    logger.info("table image: %d lines → %d bytes, sending photo", n_lines, len(png))
    await send_photo(
        bot,
        chat_id,
        [("image/png", png)],
        **_send_kwargs(thread_id, silent=silent),  # type: ignore[arg-type]
    )


async def _send_code_file(
    bot: Bot,
    chat_id: int,
    filename: str,
    content: str,
    *,
    thread_id: int | None,
    silent: bool,
) -> None:
    """Send a long code block as a document so it copies/saves whole.

    Writes a host-side temp file (the bot owns it — not the agent (send file:)
    path, so no /workspace perimeter applies) and removes it after upload.
    Falls back to an inline code block if writing the temp file fails.
    """
    n_lines = content.count("\n") + 1
    caption = tr("mq.code_file_caption", filename=filename, n_lines=n_lines)
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="ccbot-code-", suffix="_" + filename)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        logger.warning("code file write failed, falling back to code block: %s", e)
        await send_with_fallback(
            bot,
            chat_id,
            "```\n" + content + "\n```",
            **_send_kwargs(thread_id, silent=silent),  # type: ignore[arg-type]
        )
        return
    try:
        logger.info("code file: %s, %d lines, sending document", filename, n_lines)
        await send_document(
            bot,
            chat_id,
            tmp_path,
            filename=filename,
            caption=caption,
            **_send_kwargs(thread_id, silent=silent),  # type: ignore[arg-type]
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


async def _send_part_with_tables(
    bot: Bot,
    chat_id: int,
    part: str,
    task: MessageTask,
    *,
    silent: bool,
    user_id: int,
    tid: int,
) -> int | None:
    """Send one text part, rendering embedded placeholders in source order:
    IMG → image (wide table / box-art), FILE → document (long code). Returns
    the last text message_id sent (for tool_use editing), or None if the part
    was out-of-band/empty only.

    With no placeholders this is exactly one ``send_with_fallback`` — the
    common case is unchanged.
    """
    last_msg_id: int | None = None
    # PLACEHOLDER_RE has two capture groups, so re.split yields
    # [text, kind, ref, text, kind, ref, …, text].
    segments = PLACEHOLDER_RE.split(part)
    i = 0
    while i < len(segments):
        text_segment = segments[i].strip("\n")
        if text_segment.strip():
            sent = await send_with_fallback(
                bot,
                chat_id,
                text_segment,
                **_send_kwargs(task.thread_id, silent=silent),  # type: ignore[arg-type]
            )
            if sent:
                last_msg_id = sent.message_id
                note_topic_message(chat_id, sent.message_id, user_id, tid)
        if i + 2 < len(segments):
            kind, ref = segments[i + 1], int(segments[i + 2])
            if kind == "IMG" and 0 <= ref < len(task.table_texts):
                await _send_table_image(
                    bot,
                    chat_id,
                    task.table_texts[ref],
                    thread_id=task.thread_id,
                    silent=silent,
                )
            elif kind == "FILE" and 0 <= ref < len(task.code_files):
                fname, content = task.code_files[ref]
                await _send_code_file(
                    bot,
                    chat_id,
                    fname,
                    content,
                    thread_id=task.thread_id,
                    silent=silent,
                )
        i += 3
    return last_msg_id


async def _emit_links(
    bot: Bot,
    chat_id: int,
    task: MessageTask,
    source_text: str,
    user_id: int,
    tid: int,
) -> None:
    """Surface the http(s) links in an agent text reply as a separate, silent
    «🔗 Ссылки» list, so they're easy to tap even when the inline copy is
    word-wrapped or cropped.

    Best-effort: every failure (including a flood ``RetryAfter``) is swallowed.
    The reply itself is already delivered by the time this runs, so it must
    never re-raise into the worker — that would requeue and duplicate the
    content. Silent (no notification) to avoid a second ping per reply.
    """
    urls = extract_urls(source_text)
    if not urls:
        return
    block = format_links_block(urls)
    try:
        sent = await send_with_fallback(
            bot,
            chat_id,
            block,
            **_send_kwargs(task.thread_id, silent=True),  # type: ignore[arg-type]
        )
    except Exception as e:
        logger.debug("Link surfacing failed (user=%d): %s", user_id, e)
        return
    if sent:
        note_topic_message(chat_id, sent.message_id, user_id, tid)


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)

    # Capture the link source before parts are stripped/cleared below — the
    # agent's links are re-surfaced as a separate tappable list after the reply
    # is delivered (text replies only; tool/thinking plumbing is excluded).
    link_source = "\n\n".join(task.parts) if task.content_type == "text" else ""

    # Tool plumbing isn't sent to the chat anymore — the rolling status
    # spinner shows live tool activity and /screenshot covers the
    # history. Even with disable_notification, iOS still flashes a
    # silent banner per send, which is the loudest part of a tool-heavy
    # turn. Images attached to a tool_result (Bash plot, browser
    # screenshot, …) are substantive and still go through.
    #
    # We deliberately DO NOT clear the rolling status message here — the
    # spinner is allowed to live across multiple tools in one turn (it
    # gets re-painted by the 1s polling loop, which is an *edit* not a
    # *send*, so it doesn't create a new banner on iOS). Clearing it on
    # every tool_result would tear it down and the next tool_use would
    # have to send a fresh status message — one silent banner per tool,
    # which is exactly the noise this whole change is trying to remove.
    if task.content_type in ("tool_use", "tool_result"):
        if task.image_data:
            await _send_task_images(bot, chat_id, task)
        return

    # Diff screenshot (opt-in /diff): a native edit-diff block captured from
    # the agent's pane. Sent silently — /diff is per-edit and opt-in, so a
    # ping per edit would just be noise.
    if task.content_type == "diff":
        if task.image_data:
            await send_photo(
                bot,
                chat_id,
                task.image_data,
                caption=tr("mq.diff_changes"),
                **_send_kwargs(task.thread_id, silent=True),  # type: ignore[arg-type]
            )
        return

    # 2. Voice mode: split response into voice/chat segments and deliver
    #    each in source order. Voice goes to TTS; chat goes as regular
    #    markdown text. See voice/hints.split_voice_segments for the
    #    [chat]...[/chat] protocol Claude uses to opt content out of TTS.
    #    task.voice_mode was snapshotted at enqueue time — don't re-check
    #    session_manager here, otherwise a /voice toggle between enqueue
    #    and dequeue would voice a response composed in text mode.
    if task.content_type == "text" and task.voice_mode:
        raw_text = strip_sentinels("\n\n".join(task.parts))
        segments = split_voice_segments(raw_text)
        if segments:
            send_kw = _send_kwargs(task.thread_id)
            for kind, chunk in segments:
                if kind == "chat":
                    await send_with_fallback(
                        bot,
                        chat_id,
                        strip_output_tags(chunk),
                        **send_kw,  # type: ignore[arg-type]
                    )
                    continue
                # Layer 3: daily budget pre-check. If exhausted, fall back to
                # text without calling Gemini. ``can_spend`` rolls the date
                # over when needed but doesn't record — record() runs only
                # after a successful synth.
                chunk_chars = len(chunk)
                if not session_manager.voice_budget_can_spend(chunk_chars):
                    logger.warning(
                        "Voice budget exhausted; falling back to text "
                        "(chunk=%dch, used=%d/%d)",
                        chunk_chars,
                        session_manager.voice_budget.chars_used,
                        session_manager.voice_budget.daily_limit,
                    )
                    await _ensure_voice_disabled_for_exhausted_budget(bot)
                    await send_with_fallback(
                        bot,
                        chat_id,
                        strip_output_tags(chunk),
                        **send_kw,  # type: ignore[arg-type]
                    )
                    continue
                try:
                    audio_data = await synthesize_speech(chunk)
                    await send_voice(
                        bot,
                        chat_id,
                        audio_data,
                        **send_kw,  # type: ignore[arg-type]
                    )
                    event = session_manager.voice_budget_record(chunk_chars)
                    logger.info(
                        "TTS billed: chunk=%dch, daily=%d/%d",
                        chunk_chars,
                        event.chars_used,
                        event.daily_limit,
                    )
                    if event.crossed_80pct:
                        await _notify_budget_warning(bot, event)
                    if event.crossed_exhausted:
                        await _notify_budget_exhausted(bot)
                except Exception as e:
                    logger.warning(
                        "TTS failed for segment, falling back to text: %s", e
                    )
                    await send_with_fallback(
                        bot,
                        chat_id,
                        strip_output_tags(chunk),
                        **send_kw,  # type: ignore[arg-type]
                    )
            await _send_task_images(bot, chat_id, task)
            await _emit_links(bot, chat_id, task, link_source, user_id, tid)
            return
        # Empty after split (e.g. all-whitespace) — fall through defensively.

    # Defensive: strip TTS audio tags and [chat] markers from text parts
    # unconditionally. Regex only matches specific voice tokens — no-op
    # when the content doesn't contain them, so there's no risk of
    # touching unrelated topics. The previous gate (is_session_voice_aware)
    # cleared too early: when voice was toggled off, voice_announced_sessions
    # dropped the session, the gate went False, and markers Claude emitted
    # by inertia on the very next message slipped through to chat raw.
    if task.content_type == "text":
        task.parts = [strip_output_tags(p) for p in task.parts]

    # 3. Send content messages
    silent = _is_silent_content_type(task.content_type)
    last_msg_id: int | None = None
    part_idx = 0
    try:
        for part_idx, part in enumerate(task.parts):
            sent_id = await _send_part_with_tables(
                bot,
                chat_id,
                part,
                task,
                silent=silent,
                user_id=user_id,
                tid=tid,
            )
            if sent_id is not None:
                last_msg_id = sent_id
    except RetryAfter:
        # Drop the parts already delivered so the requeue in
        # _dispatch_task retries only from the in-flight part — no
        # duplicates after the flood ban.
        task.parts = task.parts[part_idx:]
        raise
    # All parts delivered: a RetryAfter from the images below must not
    # resend the text on requeue.
    task.parts = []

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    await _send_task_images(bot, chat_id, task)

    # 5. Surface the reply's links as a separate tappable list (text only).
    await _emit_links(bot, chat_id, task, link_source, user_id, tid)

    # Status will be picked up by the 1s polling loop if Claude is still working


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    entry_ts_iso: str | None = None,
    table_texts: list[str] | None = None,
    code_files: list[tuple[str, str]] | None = None,
) -> None:
    """Enqueue a content message task.

    ``entry_ts_iso`` is the JSONL timestamp of the source message.
    Anything older than ``VOICE_FRESH_WINDOW_SEC`` is treated as a
    replay and forced to text fallback even in voice-mode topics —
    this is the financial safety net against any monitor regression
    that resurrects old assistant text (cf. the Apr 2026 incident).
    Missing/unparseable timestamps fail closed (no voice).
    """
    logger.debug(
        "Enqueue content: user=%d, thread=%s, window_id=%s, content_type=%s",
        user_id,
        thread_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id, thread_id)

    # Snapshot voice_mode now (enqueue-time) so a later /voice toggle
    # doesn't change how this composed-under-mode-X message is delivered.
    voice_mode = session_manager.is_voice_mode(user_id, thread_id)

    # Anti-replay: if the JSONL entry is older than the freshness window,
    # this is almost certainly a replay (stale offset, --resume, new
    # session_id discovery). Skip TTS to avoid burning Gemini tokens on
    # content the user already heard or read.
    if voice_mode and content_type == "text":
        if not is_fresh_for_voice(entry_ts_iso, time.time()):
            logger.warning(
                "Voice skipped (stale entry, ts=%s): user=%d thread=%s "
                "— forcing text fallback to avoid replay billing",
                entry_ts_iso,
                user_id,
                thread_id,
            )
            voice_mode = False

    task = MessageTask(
        task_type="content",
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
        voice_mode=voice_mode,
        table_texts=table_texts or [],
        code_files=code_files or [],
    )
    queue.put_nowait(task)


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def cleanup_user(user_id: int) -> None:
    """Stop all topic queue workers and clean state for a user.

    Prevents memory leaks when users leave or become inactive. Iterates
    every (user_id, thread_id) queue that belongs to this user — with
    per-topic sharding there is one worker per topic, not per user.
    """
    keys = [k for k in list(_queue_workers.keys()) if k[0] == user_id]
    for key in keys:
        worker = _queue_workers.pop(key, None)
        if worker:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        _message_queues.pop(key, None)
        _queue_locks.pop(key, None)
    _flood_until.pop(user_id, None)
    # Clean tool_msg_ids for all threads of this user
    keys_to_remove = [k for k in _tool_msg_ids if k[1] == user_id]
    for k in keys_to_remove:
        _tool_msg_ids.pop(k, None)
    logger.info("Cleaned up state for user %d", user_id)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    logger.info("Message queue workers stopped")
