"""Rollout-JSONL parser for OpenAI Codex sessions.

Codex writes each session to ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
in a schema unrelated to Claude Code's. This module reads that schema and emits
the SAME ``ParsedEntry`` objects ``transcript_parser.TranscriptParser`` does, so
everything downstream (monitor → NewMessage → message queue → voice / tables /
pins) is transport- and runtime-agnostic and needs no codex branch.

Rollout line shapes (each line is one JSON object with a ``type`` + ``payload``):
  - ``session_meta``  — header: payload.session_id / id, payload.cwd. (routing)
  - ``response_item`` — the canonical conversation items:
        payload.type == "message"           → role user/assistant/developer,
                                                content=[{type, text}]
        payload.type == "function_call"      → tool_use  (name, call_id, arguments)
        payload.type == "function_call_output"→ tool_result (call_id, output)
        payload.type == "reasoning"          → thinking
  - ``event_msg``     — an event stream that DUPLICATES the messages
        (user_message / agent_message / token_count / task_*); ignored for text
        so a reply isn't emitted twice. (token_count could feed context alerts
        later — not wired yet.)
  - ``world_state`` / ``turn_context`` — metadata; ignored.

Codex injects ``role=developer`` (base/permissions instructions) and a first
``role=user`` message wrapping ``<environment_context>`` — both are machinery,
not real turns, and are filtered out.

Key class: CodexTranscriptParser (static ``parse_entries``, mirrors
TranscriptParser's signature so the monitor calls either interchangeably).
"""

import json
import logging
from typing import Any

from .transcript_parser import ParsedEntry

logger = logging.getLogger(__name__)

# A role=user message whose text opens with one of these is a system-context
# injection Codex adds to the transcript, not something the user typed.
_SYSTEM_WRAPPERS = (
    "<environment_context>",
    "<user_instructions>",
    "<permissions instructions>",
    "<permissions_instructions>",
)


def _extract_text(content: Any) -> str:
    """Join the text of a response_item message's content blocks.

    ``content`` is a list of ``{"type": "input_text"|"output_text"|..., "text": …}``
    (Codex also accepts a bare string). Non-text blocks (images) contribute
    nothing here.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            txt = block.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "".join(parts).strip()


def _is_system_wrapper(text: str) -> bool:
    """True if a user-role message is actually a Codex system-context block."""
    stripped = text.lstrip()
    return stripped.startswith(_SYSTEM_WRAPPERS)


def _format_output(output: Any) -> str:
    """Best-effort text of a function_call_output payload (suppressed downstream,
    so this only needs to be non-empty and readable)."""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("content", "output", "text", "stdout"):
            val = output.get(key)
            if isinstance(val, str):
                return val
        return json.dumps(output, ensure_ascii=False)[:2000]
    return str(output) if output is not None else ""


class CodexTranscriptParser:
    """Parser for Codex rollout JSONL. Emits transcript_parser.ParsedEntry.

    Only ``response_item`` lines carry conversation content; every other line
    type is skipped. Assistant text is the payload that actually reaches chat
    (tool_use / tool_result / user are suppressed or off by default downstream,
    exactly as for Claude).
    """

    @staticmethod
    def parse_entries(
        entries: list[dict],
        pending_tools: dict[str, dict] | None = None,
    ) -> tuple[list[ParsedEntry], dict[str, dict]]:
        """Parse raw rollout dicts into ParsedEntry list.

        Signature mirrors ``TranscriptParser.parse_entries`` so the monitor can
        dispatch on runtime without special-casing the call. ``pending_tools``
        carries function_call → function_call_output pairing across poll cycles
        (by call_id), like the Claude parser's tool pairing.
        """
        pending = pending_tools if pending_tools is not None else {}
        results: list[ParsedEntry] = []

        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "response_item":
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            ts = entry.get("timestamp")
            ptype = payload.get("type")

            if ptype == "message":
                role = payload.get("role")
                text = _extract_text(payload.get("content"))
                if role == "assistant":
                    if text:
                        results.append(
                            ParsedEntry(
                                role="assistant",
                                text=text,
                                content_type="text",
                                timestamp=ts,
                            )
                        )
                elif role == "user":
                    if text and not _is_system_wrapper(text):
                        results.append(
                            ParsedEntry(
                                role="user",
                                text=text,
                                content_type="text",
                                timestamp=ts,
                            )
                        )
                # role == "developer" (base/permissions instructions) → skip

            elif ptype == "reasoning":
                text = _extract_text(payload.get("content")) or (
                    payload.get("text") if isinstance(payload.get("text"), str) else ""
                )
                if text:
                    results.append(
                        ParsedEntry(
                            role="assistant",
                            text=text,
                            content_type="thinking",
                            timestamp=ts,
                        )
                    )

            elif ptype == "function_call":
                call_id = payload.get("call_id") or ""
                name = payload.get("name") or "tool"
                pending[call_id] = {"name": name}
                results.append(
                    ParsedEntry(
                        role="assistant",
                        text=f"**{name}**",
                        content_type="tool_use",
                        tool_use_id=call_id,
                        tool_name=name,
                        timestamp=ts,
                    )
                )

            elif ptype == "function_call_output":
                call_id = payload.get("call_id") or ""
                results.append(
                    ParsedEntry(
                        role="user",
                        text=_format_output(payload.get("output")),
                        content_type="tool_result",
                        tool_use_id=call_id,
                        timestamp=ts,
                    )
                )
                pending.pop(call_id, None)

        return results, pending

    @staticmethod
    def session_meta(entries: list[dict]) -> dict | None:
        """Return the session_meta payload from a batch of rollout lines, if any."""
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") == "session_meta":
                payload = entry.get("payload")
                if isinstance(payload, dict):
                    return payload
        return None
