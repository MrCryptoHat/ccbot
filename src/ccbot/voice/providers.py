"""TTS provider implementations and dispatch.

Each provider exposes the same shape:
  - name: str  (stable identifier used in logs and capability lookup)
  - available() -> bool  (is it configured and ready)
  - max_cost_usd(text) -> float  (worst-case bill for this one request —
    the spend ledger reserves it BEFORE the call, see voice/safety.py)
  - synthesize(text) -> SynthResult  (OGG/Opus audio + real cost if known)
  - tag_catalog() -> list[str] | None  (inline audio tags the model understands,
    None if the provider is a plain-text reader)

Every attempt runs under one global lock: reserve → HTTP call → settle. One
TTS request in flight at a time, so parallel topics can't race the ledger.
Prices below are deliberately the HIGHER of current/announced rates (Gemini
3.8 promo ends 2027-01-01 and doubles) — the ledger is an upper bound.

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
from dataclasses import dataclass

import httpx

from ..config import config
from .safety import SpendLedger

logger = logging.getLogger(__name__)

TTS_MAX_INPUT_LENGTH = 4096
GEMINI_PCM_RATE = 24000
GEMINI_PCM_CHANNELS = 1

# Upper bound per provider attempt. Gemini TTS preview has long tails
# (180+ sec); prefer failing fast and moving to the next provider over
# waiting out the tail. Total worst-case = this × number of providers.
PROVIDER_BUDGET_SEC = 45.0
HTTP_TIMEOUT_SEC = 30.0

# Gemini bills audio at 25 tokens per second of speech.
GEMINI_AUDIO_TOKENS_PER_SEC = 25
# Hard API ceiling for the TTS models (models.get outputTokenLimit) ≈ 11 min.
GEMINI_MAX_OUTPUT_TOKENS = 16384
# Speech-length allowance per request: a generous floor plus ~7 chars per
# second (conversational speech runs ~12-15), i.e. ≥2x slack over real
# speech. The model can't run past it — maxOutputTokens cuts it — so a
# glitching generation can't bill minutes of noise for a short line.
_SPEECH_FLOOR_SEC = 15.0
_SPEECH_CHARS_PER_SEC = 7.0

# USD per 1M tokens: (text input, audio output). Source: ai.google.dev
# pricing, Oct 2026. 3.8 rates are the post-promo (2027) ones — 2x today's.
_GEMINI_PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash-tts": (1.00, 18.00),
    "gemini-3.8-flash-lite-tts": (1.00, 12.00),
    "gemini-3.1-flash-tts-preview": (1.00, 20.00),
    "gemini-2.5-flash-preview-tts": (0.50, 10.00),
    "gemini-2.5-pro-preview-tts": (1.00, 20.00),
}
# A model missing from the table is priced at double the dearest known one.
_GEMINI_UNKNOWN_PRICE = (2.00, 40.00)

# USD per character for the plain-text readers — conservative list prices
# (ElevenLabs creator-plan overage; OpenAI tts-1-hd), they don't report usage.
_ELEVENLABS_USD_PER_CHAR = 0.30 / 1000
_OPENAI_USD_PER_CHAR = 30.0 / 1_000_000

_client: httpx.AsyncClient | None = None
_tts_lock = asyncio.Lock()


@dataclass(frozen=True)
class SynthResult:
    audio: bytes  # OGG/Opus
    cost_usd: float | None  # what the provider reported; None = unknown


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
    def max_cost_usd(self, text: str) -> float:
        """Upper bound on what one synthesize(text) call can be billed."""

    @abstractmethod
    async def synthesize(self, text: str) -> SynthResult: ...

    def tag_catalog(self) -> list[str] | None:
        """Inline audio tags the model honors, or None for plain readers."""
        return None


class GeminiProvider(Provider):
    name = "gemini"

    def available(self) -> bool:
        return bool(config.gemini_api_key)

    @staticmethod
    def _prices() -> tuple[float, float]:
        return _GEMINI_PRICES.get(config.gemini_tts_model, _GEMINI_UNKNOWN_PRICE)

    @staticmethod
    def max_audio_tokens(text: str) -> int:
        secs = _SPEECH_FLOOR_SEC + len(text) / _SPEECH_CHARS_PER_SEC
        return min(GEMINI_MAX_OUTPUT_TOKENS, int(secs * GEMINI_AUDIO_TOKENS_PER_SEC))

    def max_cost_usd(self, text: str) -> float:
        text_price, audio_price = self._prices()
        # Prompt tokens: style prefix + text, ~1 token per 2 chars is a safe
        # over-estimate for Cyrillic; +64 for request framing.
        prompt_chars = len(config.gemini_tts_style_prefix) + len(text)
        prompt_tokens = prompt_chars / 2 + 64
        return (
            prompt_tokens * text_price + self.max_audio_tokens(text) * audio_price
        ) / 1_000_000

    def tag_catalog(self) -> list[str]:
        # Subset of Gemini's tag vocabulary — enough to steer expressiveness
        # without overwhelming the directive. The 3.8 generation switched to
        # <angle> vocal tags (style is turn-level, set via the prefix);
        # older models read [bracket] tags.
        if _gemini_uses_angle_tags(config.gemini_tts_model):
            return [
                "<chuckle>",
                "<laugh>",
                "<sigh>",
                "<breath>",
                "<whispers>",
                "<short pause>",
                "<long pause>",
                "<phew>",
            ]
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

    async def _request(self, text: str) -> tuple[bytes, float]:
        max_tokens = self.max_audio_tokens(text)
        prefix = config.gemini_tts_style_prefix.strip()
        prompt = f"{prefix} {text}" if prefix else text
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.gemini_tts_model}:generateContent"
        )
        speech_config: dict = {
            "voiceConfig": {
                "prebuiltVoiceConfig": {
                    "voiceName": config.gemini_tts_voice,
                }
            },
        }
        # Empty = let Gemini auto-detect the language from the text itself
        # (the right default for a public install; pin via GEMINI_TTS_LANGUAGE
        # when replies are reliably one language).
        if config.gemini_tts_language_code:
            speech_config["languageCode"] = config.gemini_tts_language_code
        response = await _get_client().post(
            url,
            headers={
                "x-goog-api-key": config.gemini_api_key,
                "Content-Type": "application/json",
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "temperature": config.gemini_tts_temperature,
                    "maxOutputTokens": max_tokens,
                    "speechConfig": speech_config,
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        finish = (payload.get("candidates") or [{}])[0].get("finishReason", "?")
        if finish == "MAX_TOKENS":
            logger.warning(
                "Gemini TTS hit the per-request audio cap (%d tokens, %d chars) "
                "— audio is cut short",
                max_tokens,
                len(text),
            )
        try:
            b64 = payload["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Gemini TTS no audio (finish={finish})") from e
        pcm = base64.b64decode(b64)
        return pcm, self._billed_cost(payload, text, pcm, max_tokens)

    def _billed_cost(self, payload: dict, text: str, pcm: bytes, cap: int) -> float:
        """What this call cost — never less than the audio we actually got.

        Takes the larger of usageMetadata's output count and the token count
        implied by the PCM length, and bills thinking tokens at the audio
        rate. Missing usage → the larger of the PCM cost and the up-front
        worst case. Lands in the ledger in place of the reservation, so it
        must not under-count if the API ever ignores maxOutputTokens.
        """
        text_price, audio_price = self._prices()
        pcm_sec = len(pcm) / (GEMINI_PCM_RATE * 2 * GEMINI_PCM_CHANNELS)
        pcm_tokens = pcm_sec * GEMINI_AUDIO_TOKENS_PER_SEC
        usage = payload.get("usageMetadata")
        try:
            if not isinstance(usage, dict):
                raise TypeError("usageMetadata missing")
            prompt = int(usage.get("promptTokenCount", 0))
            reported = int(usage["candidatesTokenCount"])
            thoughts = int(usage.get("thoughtsTokenCount", 0))
        except (KeyError, TypeError, ValueError):
            pcm_cost = pcm_tokens * audio_price / 1_000_000
            logger.warning(
                "Gemini TTS: no usable usageMetadata; billing the larger of "
                "PCM-derived and worst-case cost"
            )
            return max(pcm_cost, self.max_cost_usd(text))
        audio = max(reported, pcm_tokens)
        if audio > cap * 1.05:
            logger.error(
                "Gemini TTS returned %d audio tokens over a %d-token cap — "
                "maxOutputTokens was NOT honored; billing the real amount",
                int(audio),
                cap,
            )
        cost = (prompt * text_price + (audio + thoughts) * audio_price) / 1_000_000
        logger.info(
            "Gemini TTS usage: model=%s prompt=%d audio=%d thoughts=%d tokens "
            "(≈%.1fs) ≈ $%.5f",
            config.gemini_tts_model,
            prompt,
            int(audio),
            thoughts,
            pcm_sec,
            cost,
        )
        return cost

    async def synthesize(self, text: str) -> SynthResult:
        """Synthesize via Gemini.

        No retry on empty-audio responses: every Gemini call is billed
        regardless of whether ``inlineData`` was returned, so retrying
        a "finishReason=OTHER" doubles the cost in the failure path.
        Caller (``synthesize_speech`` → ``_process_content_task``)
        already has a clean text fallback, which is the right answer
        when Gemini decides not to emit audio for this prompt.
        """
        pcm, cost = await self._request(text)
        return SynthResult(await _pcm_to_ogg_opus(pcm), cost)


class ElevenLabsProvider(Provider):
    name = "elevenlabs"

    def available(self) -> bool:
        return bool(config.elevenlabs_api_key and config.elevenlabs_voice_id)

    def max_cost_usd(self, text: str) -> float:
        return len(text) * _ELEVENLABS_USD_PER_CHAR

    async def synthesize(self, text: str) -> SynthResult:
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
        return SynthResult(response.content, None)


class OpenAIProvider(Provider):
    name = "openai"

    def available(self) -> bool:
        return bool(config.openai_api_key)

    def max_cost_usd(self, text: str) -> float:
        return len(text) * _OPENAI_USD_PER_CHAR

    async def synthesize(self, text: str) -> SynthResult:
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
        return SynthResult(response.content, None)


def _gemini_uses_angle_tags(model: str) -> bool:
    """3.8+ TTS models take <angle> vocal tags; 2.x / 3.1 read [brackets]."""
    return not model.startswith(("gemini-2.", "gemini-3.0", "gemini-3.1"))


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


async def synthesize_speech(text: str, ledger: SpendLedger) -> bytes:
    """Convert text to OGG/Opus audio, every attempt paid through ``ledger``.

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

    Each provider attempt reserves its worst-case cost first and settles
    after (see voice/safety.py) — all under ``_tts_lock``.

    Raises:
        SpendDenied: the ledger refused an attempt (budget / breaker). Not
            retried on the next provider — a refusal is about money, not
            about the provider.
        ValueError: no provider is available under the current policy.
        Exception: from the last attempt if every configured provider fails.
    """
    if len(text) > TTS_MAX_INPUT_LENGTH:
        text = text[: TTS_MAX_INPUT_LENGTH - 20] + "... (truncated)"

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
    async with _tts_lock:
        for provider in configured:
            # Raises SpendDenied before any network traffic.
            reservation = ledger.tts_reserve(provider.max_cost_usd(text), len(text))
            logger.debug(
                "TTS attempt via %s (reserved $%.5f)",
                provider.name,
                reservation.cost_usd,
            )
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    provider.synthesize(text),
                    timeout=PROVIDER_BUDGET_SEC,
                )
            except (TimeoutError, Exception) as e:
                ledger.tts_settle(reservation, None, ok=False)
                last_err = e
                logger.warning(
                    "TTS provider %s failed (%s); %s",
                    provider.name,
                    type(e).__name__,
                    "trying next provider"
                    if len(configured) > 1
                    else "no fallback (TTS_PROVIDER pinned)",
                )
                continue
            ledger.tts_settle(reservation, result.cost_usd, ok=True)
            logger.info(
                "TTS synthesized via %s in %.2fs (%d bytes)",
                provider.name,
                time.monotonic() - started,
                len(result.audio),
            )
            return result.audio

    assert last_err is not None
    raise last_err
