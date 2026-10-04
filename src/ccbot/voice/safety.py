"""Voice-mode safety net: anti-replay plus a USD spend ledger with breakers.

Every TTS API call is gated here. A denied call never reaches the provider;
the caller falls back to text.

  * is_fresh_for_voice(ts_iso) — JSONL entries older than VOICE_FRESH_WINDOW
    seconds are treated as replays (e.g. monitor re-reading a JSONL after
    a session_id change or restart with stale offset). The historical
    incident: replay bugs in session_monitor (fixed Apr 22 2026, commits
    6b5b2e3 + d77e4c1) re-emitted hours of old assistant text in voice
    mode and Gemini billed every chunk. Even after those specific bugs
    were closed, this guard makes the *class* of bug financially harmless:
    no replayed message reaches the TTS API regardless of which code
    path resurrected it.

  * VoiceBudget — the spend ledger, persisted in state.json. Its contract
    is what makes a runaway loop financially bounded:
      - reserve() runs BEFORE every provider attempt and charges the
        provider's worst-case cost for that request up front. A call
        that times out, errors or returns garbage stays charged at worst
        case — the provider may well have billed it.
      - settle() replaces the reservation with the real cost when the
        provider reports it (Gemini usageMetadata), never refunds a
        failure.
      - rate breaker: more than MAX_CALLS_PER_MINUTE / MAX_CALLS_PER_HOUR
        attempts, or MAX_CONSECUTIVE_FAILURES failures in a row, TRIPS
        the ledger: every further call is refused until the user turns
        voice back on (/voice) or the day rolls over. A loop is stopped
        after a handful of calls instead of after the budget.
    The provider side caps each request's own output (Gemini
    maxOutputTokens) so "worst case" is a real ceiling, not a guess.
    Reserve → call → settle all run under providers' single global lock,
    so parallel topics can't race past the limit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

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


# Breaker thresholds. One reply can carry a few voice segments and several
# topics may be in voice mode at once, so the per-minute cap leaves room for
# that; anything faster is a loop. Counted per provider ATTEMPT (a fallback
# chain spends one attempt per provider), success or not.
MAX_CALLS_PER_MINUTE = 10
MAX_CALLS_PER_HOUR = 100
MAX_CONSECUTIVE_FAILURES = 3

DEFAULT_DAILY_LIMIT_USD = 1.0

# Why the ledger refused a call. "budget" is the ordinary daily ceiling; the
# rest are breaker trips (persisted in VoiceBudget.tripped).
TRIP_RATE_MINUTE = "rate_minute"
TRIP_RATE_HOUR = "rate_hour"
TRIP_FAILURES = "failures"
DENY_BUDGET = "budget"
DENY_TRIPPED = "tripped"


class SpendDenied(Exception):
    """The ledger refused a TTS call; ``reason`` says why (DENY_*/TRIP_*)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Reservation:
    """A worst-case charge taken before a provider call, settled after it."""

    cost_usd: float
    date: str


class SpendLedger(Protocol):
    """What synthesize_speech needs from its owner (SessionManager persists)."""

    def tts_reserve(self, cost_usd: float, chars: int) -> Reservation: ...

    def tts_settle(
        self, reservation: Reservation, actual_usd: float | None, ok: bool
    ) -> None: ...


@dataclass
class VoiceBudget:
    """Daily TTS spend ledger (USD) with rate/failure breakers.

    Reset boundary is local midnight (matches the user's wall clock). State
    lives in SessionManager.state under "voice_budget"; ``daily_limit_usd``
    is NOT persisted — it comes from config on every load, so changing
    TTS_DAILY_BUDGET_USD takes effect on restart.

    ``spent_usd`` is an upper-bound estimate: worst case for every attempt
    whose real cost is unknown (failures, providers without usage data),
    the reported cost otherwise.
    """

    daily_limit_usd: float = DEFAULT_DAILY_LIMIT_USD
    date: str = ""  # YYYY-MM-DD in local time
    spent_usd: float = 0.0
    calls: int = 0
    chars_used: int = 0
    consecutive_failures: int = 0
    recent_calls: list[float] = field(default_factory=list)  # epoch, last hour
    tripped: str = ""  # TRIP_* reason; non-empty = every call refused
    warned_80pct: bool = False

    @staticmethod
    def _today() -> str:
        return datetime.now(_BUDGET_TZ).date().isoformat()

    def _maybe_reset(self) -> bool:
        """Roll over daily counters (and clear a trip) at local midnight."""
        today = self._today()
        if self.date != today:
            logger.info(
                "Voice budget rolled over: date %s → %s, previous day "
                "$%.4f over %d calls",
                self.date or "(none)",
                today,
                self.spent_usd,
                self.calls,
            )
            self.date = today
            self.spent_usd = 0.0
            self.calls = 0
            self.chars_used = 0
            self.consecutive_failures = 0
            self.tripped = ""
            self.warned_80pct = False
            return True
        return False

    def _trip(self, reason: str) -> None:
        self.tripped = reason
        logger.error(
            "Voice spend breaker TRIPPED (%s): calls today=%d, spent=$%.4f, "
            "last-hour attempts=%d — TTS refused until /voice or midnight",
            reason,
            self.calls,
            self.spent_usd,
            len(self.recent_calls),
        )

    def reserve(self, cost_usd: float, chars: int, now: float) -> Reservation:
        """Charge ``cost_usd`` (worst case) for one provider attempt.

        Raises SpendDenied when tripped, when the attempt would exceed a
        rate cap (which trips the breaker), or when it would push spend
        past the daily limit.
        """
        self._maybe_reset()
        if self.tripped:
            raise SpendDenied(DENY_TRIPPED)
        self.recent_calls = [t for t in self.recent_calls if now - t < 3600]
        if sum(1 for t in self.recent_calls if now - t < 60) >= MAX_CALLS_PER_MINUTE:
            self._trip(TRIP_RATE_MINUTE)
            raise SpendDenied(TRIP_RATE_MINUTE)
        if len(self.recent_calls) >= MAX_CALLS_PER_HOUR:
            self._trip(TRIP_RATE_HOUR)
            raise SpendDenied(TRIP_RATE_HOUR)
        if self.spent_usd + cost_usd > self.daily_limit_usd:
            raise SpendDenied(DENY_BUDGET)
        self.spent_usd += cost_usd
        self.calls += 1
        self.chars_used += chars
        self.recent_calls.append(now)
        return Reservation(cost_usd=cost_usd, date=self.date)

    def settle(
        self, reservation: Reservation, actual_usd: float | None, ok: bool
    ) -> None:
        """Close a reservation.

        Success with a reported cost swaps the worst case for it; success
        without one, or any failure, keeps the worst case. Failures count
        toward the consecutive-failure breaker. A reservation from a
        previous day is not touched (that day's ledger is gone).
        """
        if ok:
            self.consecutive_failures = 0
            if actual_usd is not None and reservation.date == self.date:
                self.spent_usd = max(
                    0.0, self.spent_usd - reservation.cost_usd + actual_usd
                )
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self.tripped:
            self._trip(TRIP_FAILURES)

    def take_80pct_warning(self) -> bool:
        """True exactly once per day, when spend first reaches 80% of limit."""
        self._maybe_reset()
        if not self.warned_80pct and self.spent_usd >= 0.8 * self.daily_limit_usd:
            self.warned_80pct = True
            return True
        return False

    def clear_trip(self) -> None:
        """Explicit user action (/voice on): re-arm after a breaker trip.

        The daily budget is NOT reset — only the breaker and its windows.
        """
        if self.tripped:
            logger.warning("Voice spend breaker cleared by user (was %s)", self.tripped)
        self.tripped = ""
        self.consecutive_failures = 0
        self.recent_calls = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "spent_usd": self.spent_usd,
            "calls": self.calls,
            "chars_used": self.chars_used,
            "consecutive_failures": self.consecutive_failures,
            "recent_calls": list(self.recent_calls),
            "tripped": self.tripped,
            "warned_80pct": self.warned_80pct,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        daily_limit_usd: float = DEFAULT_DAILY_LIMIT_USD,
    ) -> "VoiceBudget":
        # Pre-USD state (chars ledger) carries only date/chars_used of use;
        # unknown keys are ignored, so it loads as a fresh USD ledger.
        if not data:
            return cls(daily_limit_usd=daily_limit_usd)
        return cls(
            daily_limit_usd=daily_limit_usd,
            date=str(data.get("date", "")),
            spent_usd=float(data.get("spent_usd", 0.0)),
            calls=int(data.get("calls", 0)),
            chars_used=int(data.get("chars_used", 0)),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            recent_calls=[float(t) for t in data.get("recent_calls", [])],
            tripped=str(data.get("tripped", "")),
            warned_80pct=bool(data.get("warned_80pct", False)),
        )
