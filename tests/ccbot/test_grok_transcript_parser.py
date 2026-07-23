"""Tests for GrokTranscriptParser — updates.jsonl (ACP event stream) parsing.

Synthetic fixture lines only (this repo is public): shapes mirror what grok
0.2.111 writes, captured live and rebuilt minimal — chunk accumulation with
boundary flushes, tool_call/tool_call_update pairing, the carried tail buffer,
and summarize() for the session picker.
"""

from ccbot.grok_transcript_parser import _BUF_KEY, GrokTranscriptParser


def _ev(kind: str, ts: int = 1700000000, method: str = "session/update", **fields):
    """One updates.jsonl line: session/update envelope around an update dict."""
    return {
        "timestamp": ts,
        "method": method,
        "params": {
            "sessionId": "019f0000-0000-7000-8000-00000000aaaa",
            "update": {"sessionUpdate": kind, **fields},
        },
    }


def _text_chunk(kind: str, text: str, **kw):
    return _ev(kind, content={"type": "text", "text": text}, **kw)


def _tool_call(call_id: str, name: str):
    return _ev(
        "tool_call",
        toolCallId=call_id,
        title=name,
        rawInput={"command": "echo hi"},
        _meta={"x.ai/tool": {"version": 1, "name": name}},
    )


def _tool_done(call_id: str, output: str, status: str = "completed"):
    return _ev(
        "tool_call_update",
        toolCallId=call_id,
        status=status,
        content=[{"type": "content", "content": {"type": "text", "text": output}}],
    )


_TURN_DONE = _ev(
    "turn_completed",
    method="_x.ai/session/update",
    prompt_id="p-1",
    stop_reason="end_turn",
    usage={"inputTokens": 100, "totalTokens": 120},
)


class TestBasicTurn:
    def test_user_and_agent_text(self):
        entries = [
            _text_chunk("user_message_chunk", "hello"),
            _text_chunk("agent_message_chunk", "hi there"),
            _TURN_DONE,
        ]
        parsed, pending = GrokTranscriptParser.parse_entries(entries, {})
        assert [(p.role, p.content_type, p.text) for p in parsed] == [
            ("user", "text", "hello"),
            ("assistant", "text", "hi there"),
        ]
        assert _BUF_KEY not in pending

    def test_thinking_chunk(self):
        entries = [
            _text_chunk("agent_thought_chunk", "pondering"),
            _text_chunk("agent_message_chunk", "answer"),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})
        assert [(p.content_type, p.text) for p in parsed] == [
            ("thinking", "pondering"),
            ("text", "answer"),
        ]

    def test_multi_chunk_message_is_one_entry(self):
        # Streaming chunks of ONE message concatenate; the boundary flushes.
        entries = [
            _text_chunk("agent_message_chunk", "part one, "),
            _text_chunk("agent_message_chunk", "part two"),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})
        assert len(parsed) == 1
        assert parsed[0].text == "part one, part two"

    def test_timestamp_converted_to_iso(self):
        parsed, _ = GrokTranscriptParser.parse_entries(
            [_text_chunk("agent_message_chunk", "x", ts=1700000000), _TURN_DONE], {}
        )
        assert parsed[0].timestamp is not None
        assert parsed[0].timestamp.startswith("2023-11-14T")


class TestToolPairing:
    def test_tool_call_and_result(self):
        entries = [
            _tool_call("call-1", "run_terminal_command"),
            _tool_done("call-1", "hi\n"),
            _TURN_DONE,
        ]
        parsed, pending = GrokTranscriptParser.parse_entries(entries, {})
        assert parsed[0].content_type == "tool_use"
        assert parsed[0].tool_name == "run_terminal_command"
        assert parsed[0].tool_use_id == "call-1"
        assert parsed[1].content_type == "tool_result"
        assert parsed[1].text == "hi\n"
        assert "call-1" not in pending

    def test_in_progress_updates_skipped(self):
        # Streamed intermediate tool output must not emit per-update results.
        entries = [
            _tool_call("call-2", "run_terminal_command"),
            _tool_done("call-2", "partial", status="in_progress"),
            _tool_done("call-2", "full output"),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})
        results = [p for p in parsed if p.content_type == "tool_result"]
        assert len(results) == 1
        assert results[0].text == "full output"

    def test_pending_carried_across_batches(self):
        parsed1, pending = GrokTranscriptParser.parse_entries(
            [_tool_call("call-3", "write")], {}
        )
        assert parsed1[0].content_type == "tool_use"
        assert pending["call-3"] == {"name": "write"}
        parsed2, pending = GrokTranscriptParser.parse_entries(
            [_tool_done("call-3", "done"), _TURN_DONE], pending
        )
        assert parsed2[0].content_type == "tool_result"
        assert "call-3" not in pending

    def test_tool_name_falls_back_to_title(self):
        entries = [
            _ev("tool_call", toolCallId="call-4", title="fancy_tool"),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})
        assert parsed[0].tool_name == "fancy_tool"

    def test_tool_output_falls_back_to_raw_output(self):
        entries = [
            _ev(
                "tool_call_update",
                toolCallId="call-5",
                status="completed",
                rawOutput={"type": "Bash", "output_for_prompt": "exit: 0\nok"},
            ),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})
        assert parsed[0].text == "exit: 0\nok"


class TestTailBuffer:
    def test_unterminated_chunk_held_not_emitted(self):
        # A message still streaming at batch end must not be delivered as a
        # fragment — it's carried in pending until a boundary arrives.
        parsed, pending = GrokTranscriptParser.parse_entries(
            [_text_chunk("agent_message_chunk", "still stream")], {}
        )
        assert parsed == []
        assert pending[_BUF_KEY]["text"] == "still stream"

    def test_held_chunk_flushes_next_batch(self):
        _, pending = GrokTranscriptParser.parse_entries(
            [_text_chunk("agent_message_chunk", "part one")], {}
        )
        parsed, pending = GrokTranscriptParser.parse_entries(
            [_text_chunk("agent_message_chunk", ", part two"), _TURN_DONE], pending
        )
        assert len(parsed) == 1
        assert parsed[0].text == "part one, part two"
        assert _BUF_KEY not in pending

    def test_kind_switch_is_a_boundary(self):
        # thought → message without an intervening event still splits cleanly.
        entries = [
            _text_chunk("agent_thought_chunk", "hmm"),
            _text_chunk("agent_message_chunk", "reply"),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})
        assert [p.content_type for p in parsed] == ["thinking", "text"]


class TestMachineryIgnored:
    def test_hook_and_compaction_events_emit_nothing(self):
        entries = [
            _ev("hook_execution", method="_x.ai/session/update", event_name="stop"),
            _ev("compaction_checkpoint", method="_x.ai/session/update"),
            _ev("auto_compact_completed", method="_x.ai/session/update"),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})
        assert parsed == []

    def test_malformed_lines_skipped(self):
        entries = [
            "not a dict",
            {"timestamp": 1, "method": "session/update"},  # no params
            {"params": {"update": "not-a-dict"}},
            _text_chunk("agent_message_chunk", "ok"),
            _TURN_DONE,
        ]
        parsed, _ = GrokTranscriptParser.parse_entries(entries, {})  # type: ignore[arg-type]
        assert [p.text for p in parsed] == ["ok"]


class TestSummarize:
    def test_summary_and_count(self):
        entries = [
            _text_chunk("user_message_chunk", "fix the login bug please"),
            _text_chunk("agent_thought_chunk", "thinking"),
            _text_chunk("agent_message_chunk", "done"),
            _TURN_DONE,
        ]
        summary, count = GrokTranscriptParser.summarize(entries)
        assert summary == "fix the login bug please"
        assert count == 2  # user + assistant text; thinking excluded

    def test_summarize_flushes_tail(self):
        # No turn_completed yet — the listing must still count the messages.
        entries = [
            _text_chunk("user_message_chunk", "task"),
            _text_chunk("agent_message_chunk", "streaming answer"),
        ]
        summary, count = GrokTranscriptParser.summarize(entries)
        assert summary == "task"
        assert count == 2

    def test_empty_session(self):
        summary, count = GrokTranscriptParser.summarize(
            [_ev("hook_execution", method="_x.ai/session/update")]
        )
        assert summary == "Untitled"
        assert count == 0
