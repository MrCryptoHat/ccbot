"""Voice-to-text transcription via Deepgram Nova-3 (primary) or OpenAI (fallback).

Provides a single async function to transcribe voice messages.
If DEEPGRAM_API_KEY is set, uses Deepgram Nova-3.
Otherwise falls back to OpenAI gpt-4o-transcribe if OPENAI_API_KEY is set.

Multilingual by default: Deepgram runs with `detect_language=true` (auto-detects
~35 languages incl. Russian, English, Indonesian — one language per message,
which fits voice notes); set DEEPGRAM_LANGUAGE to a BCP-47 code to pin one.
The OpenAI fallback always auto-detects (no language param sent).

Key function: transcribe_voice(ogg_data) -> str
"""

import logging

import httpx

from .config import config

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Return a lazily-initialized httpx client singleton."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def _transcribe_deepgram(ogg_data: bytes) -> str:
    """Transcribe via Deepgram Nova-3 API.

    Language is auto-detected (`detect_language=true`) unless DEEPGRAM_LANGUAGE
    pins one — detection covers ru/en/id among ~35 languages, picking a single
    language per request, which matches one-speaker voice notes.
    """
    lang_param = (
        f"language={config.deepgram_language}"
        if config.deepgram_language
        else "detect_language=true"
    )
    url = (
        "https://api.deepgram.com/v1/listen"
        f"?model=nova-3&{lang_param}&smart_format=true&punctuate=true"
    )
    client = _get_client()
    response = await client.post(
        url,
        headers={
            "Authorization": f"Token {config.deepgram_api_key}",
            "Content-Type": "audio/ogg",
        },
        content=ogg_data,
    )
    response.raise_for_status()

    text = (
        response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    ).strip()
    if not text:
        raise ValueError("Empty transcription returned by Deepgram")
    return text


async def _transcribe_openai(ogg_data: bytes) -> str:
    """Transcribe via OpenAI API (fallback)."""
    url = f"{config.openai_base_url.rstrip('/')}/audio/transcriptions"
    client = _get_client()
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {config.openai_api_key}"},
        files={"file": ("voice.ogg", ogg_data, "audio/ogg")},
        data={"model": "gpt-4o-transcribe"},
    )
    response.raise_for_status()

    text = response.json().get("text", "").strip()
    if not text:
        raise ValueError("Empty transcription returned by OpenAI")
    return text


async def transcribe_voice(ogg_data: bytes) -> str:
    """Transcribe OGG voice data to text.

    Uses Deepgram Nova-3 if DEEPGRAM_API_KEY is configured,
    otherwise falls back to OpenAI if OPENAI_API_KEY is configured.

    Raises:
        httpx.HTTPStatusError: On API errors (401, 429, 5xx, etc.)
        ValueError: If no transcription API is configured or result is empty.
    """
    if config.deepgram_api_key:
        logger.debug("Transcribing with Deepgram Nova-3")
        return await _transcribe_deepgram(ogg_data)
    if config.openai_api_key:
        logger.debug("Transcribing with OpenAI (fallback)")
        return await _transcribe_openai(ogg_data)
    raise ValueError(
        "No transcription API configured. Set DEEPGRAM_API_KEY or OPENAI_API_KEY."
    )


async def close_client() -> None:
    """Close the httpx client (call on shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
