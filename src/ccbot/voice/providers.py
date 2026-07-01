"""TTS provider implementations and dispatch.

Each provider exposes the same shape:
  - name: str  (stable identifier used in logs and capability lookup)
  - available() -> bool  (is it configured and ready)
  - synthesize(text) -> bytes  (returns OGG/Opus audio)
  - tag_catalog() -> list[str] | None  (inline audio tags the model understands,
    None if the provider is a plain-text reader)

The active provider is picked at each synthesis call by priority:
Gemini → ElevenLabs → OpenAI. This lets the voice-mode directive query
the active provider for its capabilities and build a hint that only
mentions features the chosen TTS can actually use.

Gemini returns raw PCM (24 kHz mono 16-bit) which we pipe through
ffmpeg to produce OGG/Opus. ElevenLabs/OpenAI return OGG/Opus directly.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from abc import ABC, abstractmethod

import httpx

from ..config import config

logger = logging.getLogger(__name__)

TTS_MAX_INPUT_LENGTH = 4096
GEMINI_PCM_RATE = 24000
GEMINI_PCM_CHANNELS = 1

# Upper bound per provider attempt. Gemini TTS preview has long tails
# (180+ sec); prefer failing fast and moving to the next provider over
# waiting out the tail. Total worst-case = this × number of providers.
PROVIDER_BUDGET_SEC = 45.0
HTTP_TIMEOUT_SEC = 30.0

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
    return _client


async def close_client() -> None:
    """Close the shared httpx client (bot shutdown hook)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _pcm_to_ogg_opus(pcm: bytes) -> bytes:
    """Encode raw PCM s16le mono @24kHz to OGG/Opus via ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(GEMINI_PCM_RATE),
        "-ac",
        str(GEMINI_PCM_CHANNELS),
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        "64k",
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=pcm)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (rc={proc.returncode}): {stderr.decode(errors='replace')}"
        )
    return stdout


class Provider(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    async def synthesize(self, text: str) -> bytes: ...

    def tag_catalog(self) -> list[str] | None:
        """Inline audio tags the model honors, or None for plain readers."""
        return None


class GeminiProvider(Provider):
    name = "gemini"

    def available(self) -> bool:
        return bool(config.gemini_api_key)

    def tag_catalog(self) -> list[str]:
        # Subset of Gemini's open-ended tag vocabulary — enough to steer
        # expressiveness without overwhelming the directive. Gemini also
        # accepts free-form style prefixes ("Say cheerfully: ...").
        return [
            "[warmly]",
            "[laughing]",
            "[sighs]",
            "[thoughtfully]",
            "[pause]",
            "[long pause]",
            "[excited]",
            "[curious]",
            "[concerned]",
            "[quickly]",
        ]

    async def _request(self, text: str) -> bytes:
        prefix = config.gemini_tts_style_prefix.strip()
        if prefix:
            text = f"{prefix} {text}"
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.gemini_tts_model}:generateContent"
        )
        response = await _get_client().post(
            url,
            headers={
                "x-goog-api-key": config.gemini_api_key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "temperature": config.gemini_tts_temperature,
                    "speechConfig": {
                        "languageCode": config.gemini_tts_language_code,
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": config.gemini_tts_voice,
                            }
                        },
                    },
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        try:
            b64 = payload["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
        except (KeyError, IndexError, TypeError) as e:
            finish = payload.get("candidates", [{}])[0].get("finishReason", "?")
            raise RuntimeError(f"Gemini TTS no audio (finish={finish})") from e
        return base64.b64decode(b64)

    async def synthesize(self, text: str) -> bytes:
        """Synthesize via Gemini.

        No retry on empty-audio responses: every Gemini call is billed
        regardless of whether ``inlineData`` was returned, so retrying
        a "finishReason=OTHER" doubles the cost in the failure path.
        Caller (``synthesize_speech`` → ``_process_content_task``)
        already has a clean text fallback, which is the right answer
        when Gemini decides not to emit audio for this prompt.
        """
        pcm = await self._request(text)
        return await _pcm_to_ogg_opus(pcm)


class ElevenLabsProvider(Provider):
    name = "elevenlabs"

    def available(self) -> bool:
        return bool(config.elevenlabs_api_key and config.elevenlabs_voice_id)

    async def synthesize(self, text: str) -> bytes:
        url = (
            f"https://api.elevenlabs.io/v1/text-to-speech/{config.elevenlabs_voice_id}"
            "?output_format=opus_48000_64"
        )
        response = await _get_client().post(
            url,
            headers={
                "xi-api-key": config.elevenlabs_api_key,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": config.elevenlabs_model,
            },
        )
        response.raise_for_status()
        return response.content


class OpenAIProvider(Provider):
    name = "openai"

    def available(self) -> bool:
        return bool(config.openai_api_key)

    async def synthesize(self, text: str) -> bytes:
        url = f"{config.openai_base_url.rstrip('/')}/audio/speech"
        response = await _get_client().post(
            url,
            headers={"Authorization": f"Bearer {config.openai_api_key}"},
            json={
                "model": config.tts_model,
                "input": text,
                "voice": config.tts_voice,
                "response_format": "opus",
            },
        )
        response.raise_for_status()
        return response.content


# Priority order: Gemini (expressive) → ElevenLabs (natural) → OpenAI (fallback)
_PROVIDERS: tuple[Provider, ...] = (
    GeminiProvider(),
    ElevenLabsProvider(),
    OpenAIProvider(),
)


def _provider_by_name(name: str) -> Provider | None:
    for p in _PROVIDERS:
        if p.name == name:
            return p
    return None


def _resolve_chain() -> list[Provider]:
    """Which provider(s) to attempt for one synthesize_speech call.

    - "auto": every available provider by priority (fallback on failure)
    - "<name>": only that provider, no fallback — surfaces errors instead
      of silently switching voice. Empty list if it's not configured, the
      caller then raises.
    """
    policy = config.tts_provider
    if policy == "auto":
        return [p for p in _PROVIDERS if p.available()]
    pinned = _provider_by_name(policy)
    if pinned is None or not pinned.available():
        return []
    return [pinned]


def get_active_provider() -> Provider | None:
    """Provider that build_on_directive should describe to Claude.

    When TTS_PROVIDER is pinned, return that one (even if it'd fallback
    in auto mode). This keeps the tag-catalog hint in sync with whoever
    will actually synthesize.
    """
    policy = config.tts_provider
    if policy != "auto":
        pinned = _provider_by_name(policy)
        if pinned is not None and pinned.available():
            return pinned
        return None
    for p in _PROVIDERS:
        if p.available():
            return p
    return None


async def synthesize_speech(text: str) -> bytes:
    """Convert text to OGG/Opus audio.

    Policy comes from ``config.tts_provider``:
      - "auto" (default): walk the chain Gemini → ElevenLabs → OpenAI,
        each with a hard PROVIDER_BUDGET_SEC timeout; fall through to the
        next on any failure so one slow provider can't block the user.
      - "<name>": only attempt that provider. Any failure raises. Chosen
        when the operator wants a single consistent voice — see the
        TTS_PROVIDER env in config.

    On success, logs INFO with provider name and elapsed seconds so the
    operator can see who actually synthesized a given message (fallback
    transitions used to be silent at DEBUG).

    Raises:
        ValueError: no provider is available under the current policy.
        Exception: from the last attempt if every configured provider fails.
    """
    if len(text) > TTS_MAX_INPUT_LENGTH:
        text = text[: TTS_MAX_INPUT_LENGTH - 20] + "... (обрезано)"

    configured = _resolve_chain()
    if not configured:
        policy = config.tts_provider
        if policy == "auto":
            raise ValueError(
                "No TTS API configured. Set GEMINI_API_KEY, "
                "ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID, or OPENAI_API_KEY."
            )
        raise ValueError(
            f"TTS_PROVIDER={policy!r} but that provider is not configured "
            "(missing API key) or not a known provider name."
        )

    last_err: Exception | None = None
    for provider in configured:
        logger.debug("TTS attempt via %s", provider.name)
        started = time.monotonic()
        try:
            audio = await asyncio.wait_for(
                provider.synthesize(text),
                timeout=PROVIDER_BUDGET_SEC,
            )
            logger.info(
                "TTS synthesized via %s in %.2fs (%d bytes)",
                provider.name,
                time.monotonic() - started,
                len(audio),
            )
            return audio
        except (TimeoutError, Exception) as e:
            last_err = e
            logger.warning(
                "TTS provider %s failed (%s); %s",
                provider.name,
                type(e).__name__,
                "trying next provider"
                if len(configured) > 1
                else "no fallback (TTS_PROVIDER pinned)",
            )

    assert last_err is not None
    raise last_err
