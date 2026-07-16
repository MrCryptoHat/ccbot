"""Tests for voice/safety.py — the financial guards in front of the TTS API.

Pins the layers that make replay bugs financially harmless (cf. the Apr 2026
billing incident documented in the module): fail-closed freshness semantics
and the daily VoiceBudget ceiling with its one-shot threshold flags. The
enqueue-time wiring of the freshness gate is covered behaviorally in
handlers/test_message_queue.py::TestVoiceModeSnapshot.
"""

import time
from datetime import datetime, timedelta, timezone

from ccbot.voice.safety import (
    VOICE_FRESH_WINDOW_SEC,
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


class TestVoiceBudget:
    def test_can_spend_within_and_over_limit(self):
        b = VoiceBudget(daily_limit=100, date=VoiceBudget._today())
        assert b.can_spend(100) is True
        b.record(100)
        assert b.can_spend(1) is False

    def test_can_spend_does_not_record(self):
        b = VoiceBudget(daily_limit=100, date=VoiceBudget._today())
        b.can_spend(60)
        b.can_spend(60)
        assert b.chars_used == 0

    def test_80pct_threshold_fires_once(self):
        b = VoiceBudget(daily_limit=100, date=VoiceBudget._today())
        assert b.record(79).crossed_80pct is False
        assert b.record(1).crossed_80pct is True
        # Past the line already — the flag is one-shot for the day.
        assert b.record(5).crossed_80pct is False

    def test_exhaustion_threshold_fires_once(self):
        b = VoiceBudget(daily_limit=100, date=VoiceBudget._today())
        b.record(99)
        assert b.record(1).crossed_exhausted is True
        assert b.record(50).crossed_exhausted is False

    def test_day_rollover_resets_counters_and_flags(self):
        b = VoiceBudget(
            daily_limit=100,
            date="2000-01-01",
            chars_used=100,
            warned_80pct=True,
            notified_exhausted=True,
        )
        # First touch on a new day rolls everything over.
        assert b.can_spend(100) is True
        assert b.chars_used == 0
        assert b.warned_80pct is False
        assert b.notified_exhausted is False
        assert b.date == VoiceBudget._today()

    def test_dict_roundtrip(self):
        b = VoiceBudget(daily_limit=42, date="2026-07-16", chars_used=7)
        b2 = VoiceBudget.from_dict(b.to_dict())
        assert b2 == b

    def test_from_dict_none_gives_defaults(self):
        b = VoiceBudget.from_dict(None)
        assert b.daily_limit == 50_000
        assert b.chars_used == 0
