"""TTS_DAILY_BUDGET_USD parsing — the one number bounding daily TTS spend."""

import pytest

from ccbot.config import _TTS_BUDGET_MAX_USD, _parse_tts_budget


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", 1.0),
        ("2.5", 2.5),
        ("0", 0.0),  # valid kill switch
        ("nan", 1.0),
        ("inf", 1.0),
        ("-inf", 1.0),
        ("-1", 1.0),
        ("one dollar", 1.0),
        ("1e9", _TTS_BUDGET_MAX_USD),
        ("100", _TTS_BUDGET_MAX_USD),
    ],
)
def test_budget_is_always_finite_and_bounded(raw: str, expected: float) -> None:
    assert _parse_tts_budget(raw) == expected
