"""Tests for CodexTranscriptParser against a synthetic rollout fixture.

The fixture (tests/fixtures/codex/rollout-sample.jsonl) mirrors the real Codex
rollout schema but carries no operator / third-party data (public repo). It
exercises: session_meta extraction, system-wrapper filtering (developer +
<environment_context>), a real user message, reasoning->thinking,
function_call(+output) pairing, assistant text, and event_msg noise skipping.
"""

import json
from pathlib import Path

from ccbot.codex_transcript_parser import CodexTranscriptParser

FIXTURE = Path(__file__).parent.parent / "fixtures" / "codex" / "rollout-sample.jsonl"


def _load() -> list[dict]:
    return [
        json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()
    ]


class TestCodexTranscriptParser:
    def test_extracts_assistant_and_user_text(self):
        entries, _ = CodexTranscriptParser.parse_entries(_load())
        pairs = [(e.role, e.content_type, e.text) for e in entries]
        assert ("user", "text", "list files") in pairs
        assert (
            "assistant",
            "text",
            "There are two entries: README.md and src/.",
        ) in pairs

    def test_filters_system_wrappers(self):
        """developer + <environment_context> messages are machinery, not turns."""
        entries, _ = CodexTranscriptParser.parse_entries(_load())
        texts = [e.text for e in entries]
        assert not any("permissions instructions" in t for t in texts)
        assert not any("<environment_context>" in t for t in texts)

    def test_reasoning_becomes_thinking(self):
        entries, _ = CodexTranscriptParser.parse_entries(_load())
        thinking = [e for e in entries if e.content_type == "thinking"]
        assert len(thinking) == 1
        assert "directory listing" in thinking[0].text

    def test_function_call_pairs_by_call_id(self):
        entries, pending = CodexTranscriptParser.parse_entries(_load())
        tool_use = [e for e in entries if e.content_type == "tool_use"]
        tool_result = [e for e in entries if e.content_type == "tool_result"]
        assert len(tool_use) == 1 and tool_use[0].tool_use_id == "call_1"
        assert tool_use[0].tool_name == "shell"
        assert len(tool_result) == 1 and tool_result[0].tool_use_id == "call_1"
        # a matched call leaves nothing pending
        assert pending == {}

    def test_no_duplicate_from_event_msg_echo(self):
        """The reply appears in both response_item and event_msg/agent_message;
        only the response_item form is emitted (no double-send)."""
        entries, _ = CodexTranscriptParser.parse_entries(_load())
        replies = [
            e for e in entries if e.role == "assistant" and e.content_type == "text"
        ]
        assert len(replies) == 1

    def test_custom_tool_call_becomes_tool_use(self):
        """apply_patch (file edits) is a custom_tool_call, not a function_call —
        it must still emit a tool_use so /diff can trigger on a codex edit."""
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call_p",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** Update File: hello.py\n@@\n-x\n+y",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_p",
                    "output": "Success. Updated the following files:\nM hello.py",
                },
            },
        ]
        parsed, pending = CodexTranscriptParser.parse_entries(entries)
        tool_use = [e for e in parsed if e.content_type == "tool_use"]
        tool_result = [e for e in parsed if e.content_type == "tool_result"]
        assert len(tool_use) == 1
        assert tool_use[0].tool_name == "apply_patch"
        assert tool_use[0].tool_use_id == "call_p"
        assert len(tool_result) == 1 and tool_result[0].tool_use_id == "call_p"
        assert pending == {}  # paired by call_id

    def test_session_meta(self):
        meta = CodexTranscriptParser.session_meta(_load())
        assert meta is not None
        assert meta["session_id"] == "00000000-0000-7000-8000-000000000001"
        assert meta["cwd"] == "/home/user/project"

    def test_pending_carried_across_batches(self):
        """A function_call with no output yet stays pending for the next batch."""
        entries = _load()
        # split so the function_call is in batch 1, its output in batch 2
        call_idx = next(
            i
            for i, e in enumerate(entries)
            if e.get("payload", {}).get("type") == "function_call"
        )
        b1, b2 = entries[: call_idx + 1], entries[call_idx + 1 :]
        _, pending = CodexTranscriptParser.parse_entries(b1)
        assert "call_1" in pending
        _, pending2 = CodexTranscriptParser.parse_entries(b2, pending)
        assert pending2 == {}


class TestSummarize:
    """summarize() drives the session-picker row: first real user line +
    count of user/assistant messages (system machinery excluded)."""

    def test_summary_and_count_on_fixture(self):
        summary, count = CodexTranscriptParser.summarize(_load())
        # First real user turn (the <environment_context> wrapper is skipped).
        assert summary == "list files"
        # user "list files" + assistant reply = 2 (tool_call/reasoning/event_msg
        # aren't messages).
        assert count == 2

    def test_empty_rollout_is_untitled_zero(self):
        summary, count = CodexTranscriptParser.summarize([])
        assert summary == "Untitled"
        assert count == 0

    def test_header_only_rollout_counts_zero(self):
        """A rollout with only session_meta + a system wrapper has no real turn."""
        entries = [
            {"type": "session_meta", "payload": {"cwd": "/x"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "<environment_context>"}
                    ],
                },
            },
        ]
        summary, count = CodexTranscriptParser.summarize(entries)
        assert summary == "Untitled"
        assert count == 0

    def test_summary_truncated_to_50(self):
        long = "x" * 80
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": long}],
                },
            }
        ]
        summary, count = CodexTranscriptParser.summarize(entries)
        assert summary == "x" * 50
        assert count == 1
