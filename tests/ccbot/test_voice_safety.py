"""Tests for voice/safety.py — the financial guards in front of the TTS API.

Pins the layers that make replay and runaway-loop bugs financially harmless
(cf. the Apr 2026 billing incident documented in the module): fail-closed
freshness semantics, and the USD spend ledger — worst-case reservation
before every call, no refund on failure, rate/failure breakers. The
enqueue-time wiring of the freshness gate is covered behaviorally in
handlers/test_message_queue.py::TestVoiceModeSnapshot.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from ccbot.voice.safety import (
    DENY_BUDGET,
    DENY_TRIPPED,
    MAX_CALLS_PER_HOUR,
    MAX_CALLS_PER_MINUTE,
    MAX_CONSECUTIVE_FAILURES,
    TRIP_FAILURES,
    TRIP_RATE_HOUR,
    TRIP_RATE_MINUTE,
    VOICE_FRESH_WINDOW_SEC,
    Reservation,
    SpendDenied,
    VoiceBudget,
    is_fresh_for_voice,
    parse_iso_to_epoch,
)


def _iso_ago(age_sec: float, now: float) -> str:
    dt = datetime.fromtimestamp(now - age_sec, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


class TestFreshness:
    def test_recent_entry_is_fresh(self):
        now = time.time()
        assert is_fresh_for_voice(_iso_ago(2.0, now), now) is True

    def test_entry_past_window_is_stale(self):
        now = time.time()
        stale = _iso_ago(VOICE_FRESH_WINDOW_SEC + 1, now)
        assert is_fresh_for_voice(stale, now) is False

    def test_missing_ts_fails_closed(self):
        assert is_fresh_for_voice(None, time.time()) is False
        assert is_fresh_for_voice("", time.time()) is False

    def test_malformed_ts_fails_closed(self):
        assert is_fresh_for_voice("yesterday", time.time()) is False
        assert parse_iso_to_epoch("not-a-date") is None

    def test_offset_form_parsed_like_z_form(self):
        now = time.time()
        dt = datetime.fromtimestamp(now - 1.0, tz=timezone(timedelta(hours=8)))
        assert is_fresh_for_voice(dt.isoformat(), now) is True


class TestSpendLedger:
    """VoiceBudget as a USD ledger: reserve before, settle after."""

    def _b(self, limit: float = 1.0) -> VoiceBudget:
        return VoiceBudget(daily_limit_usd=limit, date=VoiceBudget._today())

    def test_reserve_charges_worst_case_up_front(self):
        b = self._b()
        r = b.reserve(0.10, 50, now=1000.0)
        assert r.cost_usd == 0.10
        assert b.spent_usd == pytest.approx(0.10)
        assert b.calls == 1
        assert b.chars_used == 50

    def test_over_budget_is_refused_without_charging(self):
        b = self._b(limit=0.25)
        b.reserve(0.20, 1, now=1000.0)
        with pytest.raises(SpendDenied) as exc:
            b.reserve(0.10, 1, now=1001.0)
        assert exc.value.reason == DENY_BUDGET
        assert b.spent_usd == pytest.approx(0.20)
        assert b.tripped == ""  # budget is a ceiling, not a breaker trip

    def test_success_with_reported_cost_replaces_reservation(self):
        b = self._b()
        r = b.reserve(0.10, 1, now=1000.0)
        b.settle(r, 0.01, ok=True)
        assert b.spent_usd == pytest.approx(0.01)

    def test_success_without_reported_cost_keeps_worst_case(self):
        b = self._b()
        r = b.reserve(0.10, 1, now=1000.0)
        b.settle(r, None, ok=True)
        assert b.spent_usd == pytest.approx(0.10)

    def test_failure_is_never_refunded(self):
        b = self._b()
        r = b.reserve(0.10, 1, now=1000.0)
        b.settle(r, 0.0, ok=False)
        assert b.spent_usd == pytest.approx(0.10)

    def test_settle_from_previous_day_does_not_touch_today(self):
        b = self._b()
        r = Reservation(cost_usd=0.50, date="2000-01-01")
        b.reserve(0.10, 1, now=1000.0)
        b.settle(r, 0.0, ok=True)
        assert b.spent_usd == pytest.approx(0.10)

    def test_rate_per_minute_trips_breaker(self):
        b = self._b(limit=100.0)
        for i in range(MAX_CALLS_PER_MINUTE):
            b.reserve(0.001, 1, now=1000.0 + i)
        with pytest.raises(SpendDenied) as exc:
            b.reserve(0.001, 1, now=1000.0 + MAX_CALLS_PER_MINUTE)
        assert exc.value.reason == TRIP_RATE_MINUTE
        assert b.tripped == TRIP_RATE_MINUTE
        # Tripped stays tripped even once the minute has passed.
        with pytest.raises(SpendDenied) as exc:
            b.reserve(0.001, 1, now=5000.0)
        assert exc.value.reason == DENY_TRIPPED

    def test_rate_per_hour_trips_breaker(self):
        b = self._b(limit=100.0)
        # Spread so the per-minute cap never fires.
        for i in range(MAX_CALLS_PER_HOUR):
            b.reserve(0.001, 1, now=1000.0 + i * 30)
        with pytest.raises(SpendDenied) as exc:
            b.reserve(0.001, 1, now=1000.0 + MAX_CALLS_PER_HOUR * 30 - 10)
        assert exc.value.reason == TRIP_RATE_HOUR

    def test_hour_window_slides(self):
        b = self._b(limit=100.0)
        for i in range(MAX_CALLS_PER_HOUR):
            b.reserve(0.001, 1, now=1000.0 + i * 30)
        # An hour after the first call, the oldest have aged out.
        b.reserve(0.001, 1, now=1000.0 + 3600 + 31)
        assert b.tripped == ""

    def test_consecutive_failures_trip_breaker(self):
        b = self._b()
        for i in range(MAX_CONSECUTIVE_FAILURES):
            r = b.reserve(0.001, 1, now=1000.0 + i * 61)
            b.settle(r, None, ok=False)
        assert b.tripped == TRIP_FAILURES
        with pytest.raises(SpendDenied):
            b.reserve(0.001, 1, now=9999.0)

    def test_success_resets_failure_streak(self):
        b = self._b()
        for i in range(MAX_CONSECUTIVE_FAILURES - 1):
            b.settle(b.reserve(0.001, 1, now=1000.0 + i * 61), None, ok=False)
        b.settle(b.reserve(0.001, 1, now=2000.0), 0.0, ok=True)
        b.settle(b.reserve(0.001, 1, now=2100.0), None, ok=False)
        assert b.tripped == ""

    def test_clear_trip_rearms_but_keeps_spend(self):
        b = self._b(limit=100.0)
        for i in range(MAX_CALLS_PER_MINUTE + 1):
            try:
                b.reserve(0.01, 1, now=1000.0 + i)
            except SpendDenied:
                pass
        spent = b.spent_usd
        b.clear_trip()
        assert b.tripped == ""
        b.reserve(0.01, 1, now=1012.0)
        assert b.spent_usd == pytest.approx(spent + 0.01)

    def test_runaway_loop_is_bounded(self):
        """The incident shape: a bug fires TTS as fast as it can.

        However many attempts the loop makes, only MAX_CALLS_PER_MINUTE
        reach the provider and the rest are refused for the day.
        """
        b = self._b(limit=100.0)
        admitted = 0
        for i in range(10_000):
            try:
                b.reserve(0.30, 4000, now=1000.0 + i * 0.01)
                admitted += 1
            except SpendDenied:
                pass
        assert admitted == MAX_CALLS_PER_MINUTE
        assert b.tripped == TRIP_RATE_MINUTE

    def test_80pct_warning_fires_once(self):
        b = self._b(limit=1.0)
        b.reserve(0.79, 1, now=1000.0)
        assert b.take_80pct_warning() is False
        b.reserve(0.02, 1, now=1001.0)
        assert b.take_80pct_warning() is True
        assert b.take_80pct_warning() is False

    def test_day_rollover_resets_counters_and_trip(self):
        b = VoiceBudget(
            daily_limit_usd=1.0,
            date="2000-01-01",
            spent_usd=1.0,
            calls=50,
            tripped=TRIP_FAILURES,
            warned_80pct=True,
        )
        b.reserve(0.01, 1, now=time.time())
        assert b.date == VoiceBudget._today()
        assert b.spent_usd == pytest.approx(0.01)
        assert b.calls == 1
        assert b.tripped == ""
        assert b.warned_80pct is False

    def test_dict_roundtrip(self):
        b = VoiceBudget(
            daily_limit_usd=2.0,
            date="2026-07-16",
            spent_usd=0.3,
            calls=4,
            chars_used=7,
            consecutive_failures=1,
            recent_calls=[1.0, 2.0],
            tripped=TRIP_RATE_HOUR,
            warned_80pct=True,
        )
        assert VoiceBudget.from_dict(b.to_dict(), 2.0) == b

    def test_limit_comes_from_config_not_state(self):
        b = VoiceBudget.from_dict({"date": "2026-07-16", "spent_usd": 0.1}, 3.0)
        assert b.daily_limit_usd == 3.0

    def test_legacy_chars_state_loads_as_fresh_usd_ledger(self):
        legacy = {
            "daily_limit": 50_000,
            "date": "2026-07-16",
            "chars_used": 900,
            "warned_80pct": False,
            "notified_exhausted": False,
        }
        b = VoiceBudget.from_dict(legacy, 1.0)
        assert b.spent_usd == 0.0
        assert b.tripped == ""
        assert b.chars_used == 900

    def test_from_dict_none_gives_defaults(self):
        b = VoiceBudget.from_dict(None)
        assert b.daily_limit_usd == 1.0
        assert b.spent_usd == 0.0
