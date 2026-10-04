"""Startup-time sanity checks for the voice subsystem.

Called from the bot's post_init hook. Does not raise — if a runtime
dependency is missing, logs a warning and lets the provider chain fall
back (Gemini needs ffmpeg; ElevenLabs/OpenAI return OGG directly).
"""

import logging
import shutil

from .providers import TTS_MAX_INPUT_LENGTH, GeminiProvider, get_active_provider

logger = logging.getLogger(__name__)


def check_runtime_dependencies() -> None:
    """Warn if the active provider needs something that isn't installed."""
    active = get_active_provider()
    if active is None:
        logger.info("Voice mode: no TTS provider configured")
        return

    from ..config import config

    logger.info(
        "Voice mode: active provider = %s%s, daily spend limit $%.2f",
        active.name,
        f" ({config.gemini_tts_model})" if isinstance(active, GeminiProvider) else "",
        config.tts_daily_budget_usd,
    )

    # A request that alone costs more than the day's budget is always refused
    # (and stops voice in every topic) — say so now, not on the first reply.
    longest = "x" * TTS_MAX_INPUT_LENGTH
    worst = active.max_cost_usd(longest)
    if worst > config.tts_daily_budget_usd:
        logger.warning(
            "Voice mode: one maximal reply (%d chars) can cost up to $%.2f via "
            "%s, above the $%.2f daily budget — long voice replies will be "
            "refused. Raise TTS_DAILY_BUDGET_USD or pick a cheaper model.",
            TTS_MAX_INPUT_LENGTH,
            worst,
            active.name,
            config.tts_daily_budget_usd,
        )

    if isinstance(active, GeminiProvider) and shutil.which("ffmpeg") is None:
        logger.warning(
            "Voice mode: Gemini TTS is configured but ffmpeg is not installed. "
            "Gemini TTS returns raw PCM and needs ffmpeg to encode OGG/Opus. "
            "Install: sudo apt install ffmpeg. "
            "Without it every Gemini synthesis will fail and voice messages "
            "will silently fall back to text."
        )
