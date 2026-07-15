"""Runtime-agnostic descriptor of a resumable agent session.

One resumable conversation of *any* agent runtime (Claude Code JSONL, Codex
rollout, …) reduced to what the session picker needs: an id to resume, a human
summary, a message count, and the transcript path. Lives in its own dependency-
free module so both ``session.py`` (Claude enumeration) and ``runtimes.py``
(``AgentRuntime.list_sessions``, incl. Codex) can share the type without an
import cycle. ``session.ClaudeSession`` is a backward-compatible alias.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentSession:
    """A resumable session of an agent runtime, as shown in the picker."""

    session_id: str
    summary: str
    message_count: int
    file_path: str
