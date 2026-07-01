"""Voice-mode safety net: anti-replay and daily budget.

Two independent guards that both must pass before a text chunk is sent
through TTS. Either one's verdict to skip → text fallback. The TTS API
is never called past these checks.

  * is_fresh_for_voice(ts_iso) — JSONL entries older than VOICE_FRESH_WINDOW
    seconds are treated as replays (e.g. monitor re-reading a JSONL after
    a session_id change or restart with stale offset). The historical
    incident: replay bugs in session_monitor (fixed Apr 22 2026, commits
    6b5b2e3 + d77e4c1) re-emitted hours of old assistant text in voice
    mode and Gemini billed every chunk. Even after those specific bugs
    were closed, this guard makes the *class* of bug financially harmless:
    no replayed message reaches the TTS API regardless of which code
    path resurrected it.

  * VoiceBudget — global daily char ceiling for TTS input. Persisted
    across restarts in state.json. When exhausted, voice auto-disables
    in every topic and the user is notified. Defensive backstop in case
    any future bug bypasses Layer 1.

Both checks are cheap and side-effect-free until you call record(); the
caller invokes record() only after a successful synth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# JSONL entries older than this are treated as replays and skip TTS.
# 60s is comfortably above normal end-to-end latency (Claude generates,
# JSONL flushes, monitor polls every 2s, queue dispatches) and well below
# any plausible "user is reading this in real time" interval. Nothing in
# the live path needs more than a few seconds of slack.
VOICE_FRESH_WINDOW_SEC = 60.0

# Host's local timezone — used for the daily budget reset boundary
# (00:00 local). Single-user bot, so the user's wall clock is the
# natural reset; override the host TZ (env TZ) to change it.
_BUDGET_TZ = datetime.now().astimezone().tzinfo


def parse_iso_to_epoch(ts_iso: str | None) -> float | None:
    """Parse an ISO timestamp from JSONL into epoch seconds.

    Returns None on missing or malformed input — caller treats None as
    "unknown age", which the freshness check rejects (fail-closed).
    """
    if not ts_iso:
        return None
    s = ts_iso.replace("Z", "+00:00") if ts_iso.endswith("Z") else ts_iso
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def is_fresh_for_voice(ts_iso: str | None, now: float) -> bool:
    """True if the JSONL entry is recent enough to send through TTS.

    Fail-closed: missing/unparseable timestamps are not fresh. We'd
    rather drop voice on a malformed entry than burn tokens on what
    might be a replay.
    """
    epoch = parse_iso_to_epoch(ts_iso)
    if epoch is None:
        return False
    return (now - epoch) <= VOICE_FRESH_WINDOW_SEC


@dataclass
class VoiceBudget:
    """Global daily TTS character budget with persisted state.

    Reset boundary is local midnight (matches the user's wall
    clock). State lives in SessionManager.state under
    "voice_budget". The dataclass holds an in-memory snapshot that
    SessionManager loads at startup and persists on every record().

    Thresholds:
      * warned_80pct flips when daily_used crosses 80% of daily_limit.
        Caller posts a one-shot warning to the notifications topic.
      * exhausted: daily_used >= daily_limit. Caller auto-disables
        voice in every topic and posts a one-shot exhaustion notice.

    Both flags reset to False when the date rolls over.
    """

    daily_limit: int = 50_000
    date: str = ""  # YYYY-MM-DD in local time
    chars_used: int = 0
    warned_80pct: bool = False
    notified_exhausted: bool = False

    @staticmethod
    def _today() -> str:
        return datetime.now(_BUDGET_TZ).date().isoformat()

    def _maybe_reset(self) -> bool:
        """Roll over daily counters at the local midnight boundary.

        Returns True if a reset happened (caller may want to log).
        """
        today = self._today()
        if self.date != today:
            logger.info(
                "Voice budget rolled over: date %s → %s, previous day used %d chars",
                self.date or "(none)",
                today,
                self.chars_used,
            )
            self.date = today
            self.chars_used = 0
            self.warned_80pct = False
            self.notified_exhausted = False
            return True
        return False

    def can_spend(self, chars: int) -> bool:
        """True if recording `chars` would stay within the daily limit.

        Mutates internal state by rolling the day if needed; doesn't
        record the spend itself. Caller invokes record() after a
        successful TTS synth.
        """
        self._maybe_reset()
        return (self.chars_used + chars) <= self.daily_limit

    def record(self, chars: int) -> "BudgetEvent":
        """Record TTS spend; return what crossed.

        Caller persists state and posts warnings/notices based on the
        returned event. Always call after a successful synth — never
        before, otherwise a failed synth still bills the budget.
        """
        self._maybe_reset()
        prev = self.chars_used
        self.chars_used = prev + chars

        crossed_80 = (
            not self.warned_80pct
            and prev < int(self.daily_limit * 0.8)
            and self.chars_used >= int(self.daily_limit * 0.8)
        )
        if crossed_80:
            self.warned_80pct = True

        crossed_exhaust = (
            not self.notified_exhausted
            and prev < self.daily_limit
            and self.chars_used >= self.daily_limit
        )
        if crossed_exhaust:
            self.notified_exhausted = True

        return BudgetEvent(
            crossed_80pct=crossed_80,
            crossed_exhausted=crossed_exhaust,
            chars_used=self.chars_used,
            daily_limit=self.daily_limit,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "daily_limit": self.daily_limit,
            "date": self.date,
            "chars_used": self.chars_used,
            "warned_80pct": self.warned_80pct,
            "notified_exhausted": self.notified_exhausted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "VoiceBudget":
        if not data:
            return cls()
        return cls(
            daily_limit=int(data.get("daily_limit", 50_000)),
            date=str(data.get("date", "")),
            chars_used=int(data.get("chars_used", 0)),
            warned_80pct=bool(data.get("warned_80pct", False)),
            notified_exhausted=bool(data.get("notified_exhausted", False)),
        )


@dataclass
class BudgetEvent:
    """Result of VoiceBudget.record() — what (if anything) crossed."""

    crossed_80pct: bool = False
    crossed_exhausted: bool = False
    chars_used: int = 0
    daily_limit: int = 0

    def any(self) -> bool:
        return self.crossed_80pct or self.crossed_exhausted
