"""Tests for per-user message queue mechanics, focused on voice_mode
snapshot behavior — regression guard for the TOCTOU race that used to
deliver text-composed responses via TTS when /voice flipped mid-queue.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ccbot.handlers.message_queue import (
    MAX_FLOOD_REQUEUES,
    MessageTask,
    _can_merge_tasks,
    _is_silent_content_type,
    _requeue_content_task,
    _send_kwargs,
    enqueue_content_message,
)


def _fresh_ts() -> str:
    """ISO timestamp 'now' — passes the anti-replay freshness check."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TestVoiceModeSnapshot:
    @pytest.mark.asyncio
    async def test_enqueue_captures_current_voice_mode(self):
        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.get_or_create_queue") as mock_gocq,
        ):
            import asyncio

            q: asyncio.Queue[MessageTask] = asyncio.Queue()
            mock_gocq.return_value = q
            mock_sm.is_voice_mode.return_value = True

            await enqueue_content_message(
                bot=None,  # type: ignore[arg-type]
                user_id=1,
                window_id="@0",
                parts=["hello"],
                thread_id=42,
                entry_ts_iso=_fresh_ts(),
            )
            task = q.get_nowait()
            assert task.voice_mode is True

    @pytest.mark.asyncio
    async def test_later_voice_toggle_does_not_affect_queued_task(self):
        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.get_or_create_queue") as mock_gocq,
        ):
            import asyncio

            q: asyncio.Queue[MessageTask] = asyncio.Queue()
            mock_gocq.return_value = q

            # Enqueued while voice is OFF
            mock_sm.is_voice_mode.return_value = False
            await enqueue_content_message(
                bot=None,  # type: ignore[arg-type]
                user_id=1,
                window_id="@0",
                parts=["some markdown **response**"],
                thread_id=42,
                entry_ts_iso=_fresh_ts(),
            )

            # User flips /voice ON; task is still in queue
            mock_sm.is_voice_mode.return_value = True

            task = q.get_nowait()
            # Snapshot wins: task stays text-mode despite later flip.
            assert task.voice_mode is False

    @pytest.mark.asyncio
    async def test_stale_entry_forces_text_fallback(self):
        """Anti-replay: voice_mode collapses to False for stale JSONL ts."""
        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.get_or_create_queue") as mock_gocq,
        ):
            import asyncio

            q: asyncio.Queue[MessageTask] = asyncio.Queue()
            mock_gocq.return_value = q
            mock_sm.is_voice_mode.return_value = True

            # Far-past ISO — should be treated as a replay even if voice is on.
            stale = "2020-01-01T00:00:00Z"
            await enqueue_content_message(
                bot=None,  # type: ignore[arg-type]
                user_id=1,
                window_id="@0",
                parts=["resurrected from a replay loop"],
                thread_id=42,
                entry_ts_iso=stale,
            )
            task = q.get_nowait()
            assert task.voice_mode is False

    @pytest.mark.asyncio
    async def test_missing_timestamp_is_not_fresh(self):
        """Anti-replay fails closed: no timestamp → no voice."""
        with (
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch("ccbot.handlers.message_queue.get_or_create_queue") as mock_gocq,
        ):
            import asyncio

            q: asyncio.Queue[MessageTask] = asyncio.Queue()
            mock_gocq.return_value = q
            mock_sm.is_voice_mode.return_value = True

            await enqueue_content_message(
                bot=None,  # type: ignore[arg-type]
                user_id=1,
                window_id="@0",
                parts=["mystery message"],
                thread_id=42,
                entry_ts_iso=None,
            )
            task = q.get_nowait()
            assert task.voice_mode is False


class TestCanMergeTasks:
    def _task(self, **overrides) -> MessageTask:
        defaults: dict = {
            "task_type": "content",
            "window_id": "@0",
            "parts": ["x"],
            "content_type": "text",
            "thread_id": 1,
            "voice_mode": False,
        }
        defaults.update(overrides)
        return MessageTask(**defaults)

    def test_merges_when_voice_modes_match(self):
        a = self._task(voice_mode=True)
        b = self._task(voice_mode=True)
        assert _can_merge_tasks(a, b) is True

    def test_refuses_to_merge_mismatched_voice_modes(self):
        a = self._task(voice_mode=True)
        b = self._task(voice_mode=False)
        assert _can_merge_tasks(a, b) is False

    def test_refuses_to_merge_across_windows(self):
        a = self._task(window_id="@0")
        b = self._task(window_id="@1")
        assert _can_merge_tasks(a, b) is False

    def test_refuses_to_merge_mismatched_content_types(self):
        """thinking is delivered silently, text rings — a merged task
        inherits the first task's content_type, so merging them would
        deliver the actual reply without a notification."""
        a = self._task(content_type="thinking")
        b = self._task(content_type="text")
        assert _can_merge_tasks(a, b) is False

    def test_merges_matching_content_types(self):
        a = self._task(content_type="thinking")
        b = self._task(content_type="thinking")
        assert _can_merge_tasks(a, b) is True

    def test_refuses_to_merge_rich_tasks(self):
        """A merged task would drop rich_markdown (or deliver one task's rich
        text and silently lose the other's) — rich-first tasks stay solo."""
        a = self._task(rich_markdown="# original")
        b = self._task()
        assert _can_merge_tasks(a, b) is False
        assert _can_merge_tasks(b, a) is False


class TestSilentNotificationPolicy:
    """Plumbing content types (tool events, thinking, slash-command echoes,
    status spinner) must send with ``disable_notification=True``. Text
    always rings — every word from the agent is a message the user wants
    to know about; we'd rather over-notify than silently swallow one.
    Note: tool_use/tool_result are also short-circuited entirely from
    the chat in _process_content_task (status spinner covers live state,
    /screenshot covers history), so the silent flag for them only ever
    applies to images riding inside a tool_result."""

    def test_text_rings(self) -> None:
        """The agent's actual prose reply — must ring."""
        assert _is_silent_content_type("text") is False

    def test_tool_use_is_silent(self) -> None:
        assert _is_silent_content_type("tool_use") is True

    def test_tool_result_is_silent(self) -> None:
        assert _is_silent_content_type("tool_result") is True

    def test_thinking_is_silent(self) -> None:
        assert _is_silent_content_type("thinking") is True

    def test_local_command_is_silent(self) -> None:
        """``❯ /context`` and similar TUI echoes are plumbing."""
        assert _is_silent_content_type("local_command") is True

    def test_unknown_content_type_defaults_to_ringing(self) -> None:
        """Fail-loud: if we ever add a new content_type and forget to
        classify it, the user still gets a ping rather than silent loss."""
        assert _is_silent_content_type("brand_new_type_42") is False

    def test_send_kwargs_silent_true_sets_disable_notification(self) -> None:
        kw = _send_kwargs(42, silent=True)
        assert kw == {"message_thread_id": 42, "disable_notification": True}

    def test_send_kwargs_silent_false_omits_disable_notification(self) -> None:
        """Don't set the field at all when ringing — Telegram defaults to
        notifying, and an explicit ``False`` would clutter the wire."""
        kw = _send_kwargs(42, silent=False)
        assert kw == {"message_thread_id": 42}
        assert "disable_notification" not in kw

    def test_send_kwargs_no_thread_id_no_silent(self) -> None:
        assert _send_kwargs(None) == {}


class TestFloodRequeue:
    """RetryAfter must not drop content: _requeue_content_task puts the
    failed task back at the head of its queue, bounded by
    MAX_FLOOD_REQUEUES, keeping the join() counter balanced."""

    def _task(self, **overrides) -> MessageTask:
        defaults: dict = {
            "task_type": "content",
            "window_id": "@0",
            "parts": ["x"],
            "content_type": "text",
            "thread_id": 1,
        }
        defaults.update(overrides)
        return MessageTask(**defaults)

    @pytest.mark.asyncio
    async def test_requeued_task_goes_to_head(self):
        q: asyncio.Queue[MessageTask] = asyncio.Queue()
        lock = asyncio.Lock()
        failed = self._task(parts=["lost?"])
        # Simulate the worker flow: `failed` was get()'ed, b/c still pend.
        q.put_nowait(failed)
        await q.get()
        q.put_nowait(self._task(parts=["b"]))
        q.put_nowait(self._task(parts=["c"]))

        await _requeue_content_task(q, lock, failed)

        order = [q.get_nowait().parts[0] for _ in range(3)]
        assert order == ["lost?", "b", "c"]
        assert failed.flood_requeues == 1
        # Counter math: 3 items left 4 outstanding counts (failed counted
        # by both its original put and the re-put). One task_done per item
        # plus the worker's `finally` task_done must zero it exactly.
        for _ in range(4):
            q.task_done()
        await asyncio.wait_for(q.join(), timeout=1)

    @pytest.mark.asyncio
    async def test_drops_after_max_requeues(self):
        q: asyncio.Queue[MessageTask] = asyncio.Queue()
        t = self._task(flood_requeues=MAX_FLOOD_REQUEUES)
        await _requeue_content_task(q, asyncio.Lock(), t)
        assert q.empty()

    @pytest.mark.asyncio
    async def test_skips_task_with_nothing_left_to_send(self):
        """All parts delivered before the 429 (e.g. it came from the
        images phase with no images pending) — nothing to requeue."""
        q: asyncio.Queue[MessageTask] = asyncio.Queue()
        t = self._task(parts=[])
        await _requeue_content_task(q, asyncio.Lock(), t)
        assert q.empty()


class TestSendImageBlock:
    """/tables routing for one extracted IMG-placeholder block: a table goes
    as a native rich-table message when the style is "rich" (PNG fallback on
    rejection); box-art and the "image" style always take the PNG path."""

    def _block(self, rich: bool = True):
        from ccbot.markdown_v2 import ImageBlock

        return ImageBlock(
            "grid text", rich_markdown="| a | b |\n|---|---|\n| 1 | 2 |" if rich else ""
        )

    async def _run(self, block, style: str, rich_result):
        from unittest.mock import AsyncMock

        from ccbot.handlers import message_queue as mq

        bot = AsyncMock()
        with (
            patch.object(mq, "session_manager") as mock_sm,
            patch.object(
                mq, "send_rich_message", AsyncMock(return_value=rich_result)
            ) as mock_rich,
            patch.object(mq, "_send_table_image", AsyncMock()) as mock_png,
        ):
            mock_sm.table_style = style
            await mq._send_image_block(bot, 100, block, thread_id=1, silent=False)
        return mock_rich, mock_png

    @pytest.mark.asyncio
    async def test_rich_style_sends_native_table(self):
        mock_rich, mock_png = await self._run(self._block(), "rich", rich_result=555)
        mock_rich.assert_called_once()
        assert "| a | b |" in mock_rich.call_args.args[2]
        mock_png.assert_not_called()

    @pytest.mark.asyncio
    async def test_rich_rejection_falls_back_to_png(self):
        mock_rich, mock_png = await self._run(self._block(), "rich", rich_result=None)
        mock_rich.assert_called_once()
        mock_png.assert_called_once()
        assert mock_png.call_args.args[2] == "grid text"

    @pytest.mark.asyncio
    async def test_image_style_goes_straight_to_png(self):
        mock_rich, mock_png = await self._run(self._block(), "image", rich_result=555)
        mock_rich.assert_not_called()
        mock_png.assert_called_once()

    @pytest.mark.asyncio
    async def test_box_art_never_rich(self):
        mock_rich, mock_png = await self._run(
            self._block(rich=False), "rich", rich_result=555
        )
        mock_rich.assert_not_called()
        mock_png.assert_called_once()

    @pytest.mark.asyncio
    async def test_plain_str_block_is_png(self):
        # Defensive: a plain str (no rich_markdown attribute) takes the PNG path.
        mock_rich, mock_png = await self._run("plain grid", "rich", rich_result=555)
        mock_rich.assert_not_called()
        mock_png.assert_called_once()
