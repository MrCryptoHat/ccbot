"""Updates-JSONL parser for xAI Grok CLI sessions.

Grok stores each session in ``~/.grok/sessions/<url-encoded-cwd>/<session-id>/``;
``updates.jsonl`` is the append-only ACP session-update stream (one JSON event
per line) and the ONLY per-session file that survives ``/compact`` without a
rewrite — ``chat_history.jsonl`` is truncated in place by compaction, which
would break the monitor's byte-offset incremental reads. This module reads the
updates stream and emits the SAME ``ParsedEntry`` objects
``transcript_parser.TranscriptParser`` does, so everything downstream (monitor
→ NewMessage → message queue → voice / tables / pins) needs no grok branch.

Event line shape (captured live on grok 0.2.111)::

    {"timestamp": <unix-sec>, "method": "session/update" | "_x.ai/session/update",
     "params": {"sessionId": "...",
                "update": {"sessionUpdate": <kind>, ...}, "_meta": {...}}}

Kinds consumed:
  - ``user_message_chunk`` / ``agent_message_chunk`` / ``agent_thought_chunk``
    — streamed text (grok persists ~one consolidated chunk per message, but
    chunks are accumulated defensively and flushed on the next non-chunk
    event; a tail still streaming at batch end is carried in ``pending_tools``
    under a reserved key until a later poll completes it — ``turn_completed``
    always arrives, so the flush is bounded).
  - ``tool_call`` → tool_use (name from ``_meta["x.ai/tool"].name``, falling
    back to ``title``); ``tool_call_update`` with status completed/failed →
    tool_result (intermediate in_progress updates are skipped).
  - everything else (``turn_completed``, ``hook_execution``,
    ``compaction_checkpoint``, …) is a flush boundary, not content.

Key class: GrokTranscriptParser (static ``parse_entries``, mirrors
TranscriptParser's signature so the monitor calls either interchangeably).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .transcript_parser import ParsedEntry

logger = logging.getLogger(__name__)

# Reserved pending_tools key carrying a still-streaming chunk buffer across
# poll batches. Real keys in that dict are grok toolCallIds ("call-<uuid>-N"),
# so a dunder name can never collide.
_BUF_KEY = "__grok_chunk_buf__"

# sessionUpdate kind → (ParsedEntry role, content_type)
_CHUNK_KINDS: dict[str, tuple[str, str]] = {
    "agent_message_chunk": ("assistant", "text"),
    "agent_thought_chunk": ("assistant", "thinking"),
    "user_message_chunk": ("user", "text"),
}


def _iso(ts: Any) -> str | None:
    """Unix-seconds timestamp → ISO string (ParsedEntry.timestamp shape)."""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return ts if isinstance(ts, str) else None


def _update_of(entry: Any) -> dict | None:
    """The ``params.update`` dict of one updates.jsonl line (None if malformed)."""
    if not isinstance(entry, dict):
        return None
    params = entry.get("params")
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    return update if isinstance(update, dict) else None


def _chunk_text(update: dict) -> str:
    """Text of a *_chunk update (``content`` is ``{"type": "text", "text": …}``)."""
    content = update.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        txt = content.get("text")
        return txt if isinstance(txt, str) else ""
    return ""


def _tool_name(update: dict) -> str:
    """Tool name of a tool_call update (x.ai/tool meta, falling back to title)."""
    meta = update.get("_meta")
    if isinstance(meta, dict):
        tool = meta.get("x.ai/tool")
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            return tool["name"]
    title = update.get("title")
    return title if isinstance(title, str) and title else "tool"


def _tool_output_text(update: dict) -> str:
    """Best-effort text of a completed tool_call_update (suppressed downstream,
    so this only needs to be non-empty and readable)."""
    parts: list[str] = []
    content = update.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content")
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                parts.append(inner["text"])
    if parts:
        return "".join(parts)
    raw = update.get("rawOutput")
    if isinstance(raw, dict):
        for key in ("output_for_prompt", "output", "text", "stdout"):
            val = raw.get(key)
            if isinstance(val, str) and val:
                return val
        return json.dumps(raw, ensure_ascii=False)[:2000]
    return ""


class GrokTranscriptParser:
    """Parser for Grok updates JSONL. Emits transcript_parser.ParsedEntry."""

    @staticmethod
    def parse_entries(
        entries: list[dict],
        pending_tools: dict[str, dict] | None = None,
    ) -> tuple[list[ParsedEntry], dict[str, dict]]:
        """Parse raw updates lines into ParsedEntry list.

        Signature mirrors ``TranscriptParser.parse_entries`` so the monitor can
        dispatch on runtime without special-casing the call. ``pending_tools``
        carries tool_call → tool_call_update pairing across poll cycles (by
        toolCallId) plus the reserved chunk buffer (module docstring).
        """
        return GrokTranscriptParser._parse(entries, pending_tools, flush_tail=False)

    @staticmethod
    def _parse(
        entries: list[dict],
        pending_tools: dict[str, dict] | None,
        *,
        flush_tail: bool,
    ) -> tuple[list[ParsedEntry], dict[str, dict]]:
        pending = pending_tools if pending_tools is not None else {}
        buf_raw = pending.pop(_BUF_KEY, None)
        buf: dict | None = buf_raw if isinstance(buf_raw, dict) else None
        results: list[ParsedEntry] = []

        def flush() -> None:
            nonlocal buf
            if buf is not None:
                text = (buf.get("text") or "").strip()
                kind = buf.get("kind")
                if text and kind in _CHUNK_KINDS:
                    role, ctype = _CHUNK_KINDS[kind]
                    results.append(
                        ParsedEntry(
                            role=role,
                            text=text,
                            content_type=ctype,
                            timestamp=buf.get("ts"),
                        )
                    )
            buf = None

        for entry in entries:
            update = _update_of(entry)
            if update is None:
                continue
            kind = update.get("sessionUpdate")
            if not isinstance(kind, str):
                continue
            ts = _iso(entry.get("timestamp"))

            if kind in _CHUNK_KINDS:
                text = _chunk_text(update)
                if not text:
                    continue
                if buf is not None and buf.get("kind") != kind:
                    flush()
                if buf is None:
                    buf = {"kind": kind, "text": "", "ts": ts}
                buf["text"] += text

            elif kind == "tool_call":
                flush()
                call_id = update.get("toolCallId") or ""
                name = _tool_name(update)
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

            elif kind == "tool_call_update":
                flush()
                if update.get("status") in ("completed", "failed"):
                    call_id = update.get("toolCallId") or ""
                    results.append(
                        ParsedEntry(
                            role="user",
                            text=_tool_output_text(update),
                            content_type="tool_result",
                            tool_use_id=call_id,
                            timestamp=ts,
                        )
                    )
                    pending.pop(call_id, None)

            else:
                # turn_completed / hook_execution / compaction_* / plan / … —
                # a boundary: whatever streamed before it is complete.
                flush()

        if flush_tail:
            flush()
        elif buf is not None:
            pending[_BUF_KEY] = buf
        return results, pending

    @staticmethod
    def summarize(entries: list[dict]) -> tuple[str, int]:
        """``(summary, message_count)`` for a session, for the picker.

        ``summary`` is the first user line; ``count`` is the number of
        user/assistant text messages. The tail buffer IS flushed here (unlike
        the monitor path) — a listing must count a message even when its
        turn_completed hasn't landed yet. Mirrors CodexTranscriptParser.summarize
        so both hookless runtimes' picker rows read the same.
        """
        parsed, _ = GrokTranscriptParser._parse(entries, {}, flush_tail=True)
        summary = ""
        count = 0
        for p in parsed:
            if p.content_type != "text":
                continue
            count += 1
            if not summary and p.role == "user" and p.text:
                summary = p.text[:50]
        return summary or "Untitled", count
