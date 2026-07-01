"""Startup-time sanity checks for the voice subsystem.

Called from the bot's post_init hook. Does not raise — if a runtime
dependency is missing, logs a warning and lets the provider chain fall
back (Gemini needs ffmpeg; ElevenLabs/OpenAI return OGG directly).
"""

import logging
import shutil

from .providers import GeminiProvider, get_active_provider

logger = logging.getLogger(__name__)


def check_runtime_dependencies() -> None:
    """Warn if the active provider needs something that isn't installed."""
    active = get_active_provider()
    if active is None:
        logger.info("Voice mode: no TTS provider configured")
        return

    logger.info("Voice mode: active provider = %s", active.name)

    if isinstance(active, GeminiProvider) and shutil.which("ffmpeg") is None:
        logger.warning(
            "Voice mode: GEMINI_API_KEY is set but ffmpeg is not installed. "
            "Gemini TTS returns raw PCM and needs ffmpeg to encode OGG/Opus. "
            "Install: sudo apt install ffmpeg. "
            "Without it every Gemini synthesis will fail and voice messages "
            "will silently fall back to text."
        )
