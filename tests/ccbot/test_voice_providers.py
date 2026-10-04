"""Tests for voice.providers: chain resolution, TTS_PROVIDER pinning, and
that every provider attempt is paid through the spend ledger."""

import base64
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.voice import providers
from ccbot.voice.safety import (
    MAX_CALLS_PER_MINUTE,
    Reservation,
    SpendDenied,
    VoiceBudget,
)


class _Ledger:
    """SpendLedger over a real VoiceBudget, recording the calls."""

    def __init__(self, limit: float = 100.0) -> None:
        self.budget = VoiceBudget(daily_limit_usd=limit, date=VoiceBudget._today())
        self.settled: list[tuple[float, float | None, bool]] = []

    def tts_reserve(self, cost_usd: float, chars: int) -> Reservation:
        return self.budget.reserve(cost_usd, chars, time.time())

    def tts_settle(self, reservation, actual_usd, ok) -> None:
        self.settled.append((reservation.cost_usd, actual_usd, ok))
        self.budget.settle(reservation, actual_usd, ok)


class _StubProvider:
    def __init__(self, name: str, is_available: bool = True, audio: bytes = b"ogg"):
        self.name = name
        self._available = is_available
        self._audio = audio
        self.synthesize = AsyncMock(
            return_value=providers.SynthResult(audio=audio, cost_usd=None)
        )

    def available(self) -> bool:
        return self._available

    def max_cost_usd(self, text: str) -> float:
        return 0.05

    def tag_catalog(self):
        return None


@pytest.fixture
def stub_providers():
    gem = _StubProvider("gemini", audio=b"gem-ogg")
    el = _StubProvider("elevenlabs", audio=b"el-ogg")
    oa = _StubProvider("openai", audio=b"oa-ogg")
    with patch.object(providers, "_PROVIDERS", (gem, el, oa)):
        yield gem, el, oa


@pytest.mark.usefixtures("stub_providers")
class TestResolveChain:
    def test_auto_returns_all_available_in_priority(self):
        with patch.object(providers.config, "tts_provider", "auto"):
            chain = providers._resolve_chain()
        assert [p.name for p in chain] == ["gemini", "elevenlabs", "openai"]

    def test_auto_skips_unavailable(self, stub_providers):
        stub_providers[0]._available = False
        with patch.object(providers.config, "tts_provider", "auto"):
            chain = providers._resolve_chain()
        assert [p.name for p in chain] == ["elevenlabs", "openai"]

    def test_pinned_returns_single_provider(self):
        with patch.object(providers.config, "tts_provider", "elevenlabs"):
            chain = providers._resolve_chain()
        assert [p.name for p in chain] == ["elevenlabs"]

    def test_pinned_empty_when_unavailable(self, stub_providers):
        gem, _, _ = stub_providers
        gem._available = False
        with patch.object(providers.config, "tts_provider", "gemini"):
            chain = providers._resolve_chain()
        assert chain == []

    def test_pinned_empty_for_unknown_name(self):
        with patch.object(providers.config, "tts_provider", "bogus"):
            chain = providers._resolve_chain()
        assert chain == []


class TestSynthesizeSpeech:
    @pytest.mark.asyncio
    async def test_auto_succeeds_with_first_provider(self, stub_providers):
        gem, el, _ = stub_providers
        with patch.object(providers.config, "tts_provider", "auto"):
            audio = await providers.synthesize_speech("hello", _Ledger())
        assert audio == b"gem-ogg"
        gem.synthesize.assert_awaited_once()
        el.synthesize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_falls_back_on_failure(self, stub_providers):
        gem, el, _ = stub_providers
        gem.synthesize.side_effect = RuntimeError("gemini down")
        with patch.object(providers.config, "tts_provider", "auto"):
            audio = await providers.synthesize_speech("hello", _Ledger())
        assert audio == b"el-ogg"
        gem.synthesize.assert_awaited_once()
        el.synthesize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pinned_skips_fallback(self, stub_providers):
        gem, el, oa = stub_providers
        gem.synthesize.side_effect = RuntimeError("gemini down")
        with patch.object(providers.config, "tts_provider", "gemini"):
            with pytest.raises(RuntimeError, match="gemini down"):
                await providers.synthesize_speech("hello", _Ledger())
        # Other providers must NOT be called in pinned mode.
        el.synthesize.assert_not_awaited()
        oa.synthesize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pinned_unavailable_raises_value_error(self, stub_providers):
        gem, _, _ = stub_providers
        gem._available = False
        with patch.object(providers.config, "tts_provider", "gemini"):
            with pytest.raises(ValueError, match="TTS_PROVIDER"):
                await providers.synthesize_speech("hello", _Ledger())

    @pytest.mark.asyncio
    async def test_auto_all_unavailable_raises_value_error(self, stub_providers):
        for p in stub_providers:
            p._available = False
        with patch.object(providers.config, "tts_provider", "auto"):
            with pytest.raises(ValueError, match="No TTS API configured"):
                await providers.synthesize_speech("hello", _Ledger())


class TestLedgerWiring:
    @pytest.mark.asyncio
    async def test_success_reserves_then_settles(self, stub_providers):
        ledger = _Ledger()
        with patch.object(providers.config, "tts_provider", "gemini"):
            await providers.synthesize_speech("hello", ledger)
        assert ledger.budget.calls == 1
        assert ledger.settled == [(0.05, None, True)]

    @pytest.mark.asyncio
    async def test_denied_never_calls_provider(self, stub_providers):
        gem, el, _ = stub_providers
        ledger = _Ledger(limit=0.01)  # below the stub's 0.05 worst case
        with patch.object(providers.config, "tts_provider", "auto"):
            with pytest.raises(SpendDenied):
                await providers.synthesize_speech("hello", ledger)
        gem.synthesize.assert_not_awaited()
        el.synthesize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_chain_pays_per_attempt(self, stub_providers):
        gem, _, _ = stub_providers
        gem.synthesize.side_effect = RuntimeError("gemini down")
        ledger = _Ledger()
        with patch.object(providers.config, "tts_provider", "auto"):
            await providers.synthesize_speech("hello", ledger)
        assert ledger.budget.calls == 2
        assert ledger.budget.spent_usd == pytest.approx(0.10)
        assert [ok for _, _, ok in ledger.settled] == [False, True]

    @pytest.mark.asyncio
    async def test_reported_cost_replaces_worst_case(self, stub_providers):
        gem, _, _ = stub_providers
        gem.synthesize.return_value = providers.SynthResult(b"a", cost_usd=0.002)
        ledger = _Ledger()
        with patch.object(providers.config, "tts_provider", "gemini"):
            await providers.synthesize_speech("hello", ledger)
        assert ledger.budget.spent_usd == pytest.approx(0.002)

    @pytest.mark.asyncio
    async def test_runaway_caller_is_cut_off(self, stub_providers):
        """A caller looping on synthesize_speech reaches the provider only
        MAX_CALLS_PER_MINUTE times — then every call is refused."""
        gem, _, _ = stub_providers
        ledger = _Ledger()
        refused = 0
        with patch.object(providers.config, "tts_provider", "gemini"):
            for _ in range(200):
                try:
                    await providers.synthesize_speech("hello", ledger)
                except SpendDenied:
                    refused += 1
        assert gem.synthesize.await_count == MAX_CALLS_PER_MINUTE
        assert refused == 200 - MAX_CALLS_PER_MINUTE


class TestGeminiProvider:
    """Request shape and cost math — the HTTP client is mocked, no API call."""

    def _response(self, usage: dict | None, finish: str = "STOP") -> MagicMock:
        payload: dict = {
            "candidates": [
                {
                    "finishReason": finish,
                    "content": {
                        "parts": [
                            {"inlineData": {"data": base64.b64encode(b"pcm").decode()}}
                        ]
                    },
                }
            ]
        }
        if usage is not None:
            payload["usageMetadata"] = usage
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        return resp

    @pytest.mark.asyncio
    async def test_request_caps_audio_tokens(self):
        client = MagicMock()
        client.post = AsyncMock(return_value=self._response(None))
        gem = providers.GeminiProvider()
        text = "привет " * 20
        with (
            patch.object(providers, "_get_client", return_value=client),
            patch.object(providers.config, "gemini_tts_model", "gemini-3.8-flash-tts"),
        ):
            await gem._request(text)
        url = client.post.await_args.args[0]
        body = client.post.await_args.kwargs["json"]
        assert "gemini-3.8-flash-tts:generateContent" in url
        cap = body["generationConfig"]["maxOutputTokens"]
        assert cap == gem.max_audio_tokens(text)
        assert 0 < cap <= providers.GEMINI_MAX_OUTPUT_TOKENS

    def test_audio_cap_scales_with_text_and_is_bounded(self):
        short = providers.GeminiProvider.max_audio_tokens("да")
        longer = providers.GeminiProvider.max_audio_tokens("а" * 700)
        huge = providers.GeminiProvider.max_audio_tokens("а" * 100_000)
        assert short < longer
        assert huge == providers.GEMINI_MAX_OUTPUT_TOKENS
        # ≥2x slack over brisk real speech (~14 chars/s) for a normal reply.
        assert longer >= 2 * (700 / 14) * providers.GEMINI_AUDIO_TOKENS_PER_SEC

    def test_worst_case_covers_reported_cost_at_the_cap(self):
        gem = providers.GeminiProvider()
        text = "а" * 500
        with patch.object(providers.config, "gemini_tts_model", "gemini-3.8-flash-tts"):
            worst = gem.max_cost_usd(text)
            at_cap = gem._billed_cost(
                {
                    "usageMetadata": {
                        "promptTokenCount": 300,
                        "candidatesTokenCount": gem.max_audio_tokens(text),
                    }
                },
                text,
                b"",
                gem.max_audio_tokens(text),
            )
        assert at_cap <= worst

    def test_unknown_model_priced_above_known_ones(self):
        gem = providers.GeminiProvider()
        with patch.object(providers.config, "gemini_tts_model", "gemini-9-tts"):
            unknown = gem.max_cost_usd("hello")
        with patch.object(
            providers.config, "gemini_tts_model", "gemini-3.1-flash-tts-preview"
        ):
            dearest_known = gem.max_cost_usd("hello")
        assert unknown > dearest_known

    @pytest.mark.asyncio
    async def test_usage_metadata_becomes_cost(self):
        client = MagicMock()
        client.post = AsyncMock(
            return_value=self._response(
                {"promptTokenCount": 100, "candidatesTokenCount": 1000}
            )
        )
        with (
            patch.object(providers, "_get_client", return_value=client),
            patch.object(providers.config, "gemini_tts_model", "gemini-3.8-flash-tts"),
        ):
            _, cost = await providers.GeminiProvider()._request("hi")
        assert cost == pytest.approx((100 * 1.0 + 1000 * 18.0) / 1_000_000)

    @pytest.mark.asyncio
    async def test_missing_usage_bills_worst_case(self):
        client = MagicMock()
        client.post = AsyncMock(return_value=self._response(None))
        gem = providers.GeminiProvider()
        with patch.object(providers, "_get_client", return_value=client):
            _, cost = await gem._request("hi")
        assert cost == pytest.approx(gem.max_cost_usd("hi"))

    def test_pcm_length_beats_underreported_usage(self):
        """If the API ignored maxOutputTokens and under-reported, the audio
        we actually received is what gets billed."""
        gem = providers.GeminiProvider()
        ten_minutes = b"\0" * (providers.GEMINI_PCM_RATE * 2 * 600)
        with patch.object(providers.config, "gemini_tts_model", "gemini-3.8-flash-tts"):
            cost = gem._billed_cost(
                {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}},
                "hi",
                ten_minutes,
                gem.max_audio_tokens("hi"),
            )
        assert cost == pytest.approx((10 * 1.0 + 600 * 25 * 18.0) / 1_000_000)
        assert cost > gem.max_cost_usd("hi")

    def test_thought_tokens_are_billed(self):
        gem = providers.GeminiProvider()
        usage = {"promptTokenCount": 0, "candidatesTokenCount": 100}
        with patch.object(providers.config, "gemini_tts_model", "gemini-3.8-flash-tts"):
            base = gem._billed_cost({"usageMetadata": usage}, "hi", b"", 1000)
            more = gem._billed_cost(
                {"usageMetadata": {**usage, "thoughtsTokenCount": 1000}},
                "hi",
                b"",
                1000,
            )
        assert more - base == pytest.approx(1000 * 18.0 / 1_000_000)

    def test_tag_syntax_follows_model_generation(self):
        gem = providers.GeminiProvider()
        with patch.object(providers.config, "gemini_tts_model", "gemini-3.8-flash-tts"):
            assert all(t.startswith("<") for t in gem.tag_catalog())
        with patch.object(
            providers.config, "gemini_tts_model", "gemini-3.1-flash-tts-preview"
        ):
            assert all(t.startswith("[") for t in gem.tag_catalog())


@pytest.mark.usefixtures("stub_providers")
class TestGetActiveProvider:
    def test_auto_returns_top_available(self):
        with patch.object(providers.config, "tts_provider", "auto"):
            active = providers.get_active_provider()
        assert active is not None
        assert active.name == "gemini"

    def test_pinned_returns_pinned_provider(self):
        with patch.object(providers.config, "tts_provider", "openai"):
            active = providers.get_active_provider()
        assert active is not None
        assert active.name == "openai"

    def test_pinned_unavailable_returns_none(self, stub_providers):
        gem, _, _ = stub_providers
        gem._available = False
        with patch.object(providers.config, "tts_provider", "gemini"):
            assert providers.get_active_provider() is None
