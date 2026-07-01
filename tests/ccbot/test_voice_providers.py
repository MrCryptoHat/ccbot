"""Tests for voice.providers chain resolution and TTS_PROVIDER pinning."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.voice import providers


class _StubProvider:
    def __init__(self, name: str, is_available: bool = True, audio: bytes = b"ogg"):
        self.name = name
        self._available = is_available
        self._audio = audio
        self.synthesize = AsyncMock(return_value=audio)

    def available(self) -> bool:
        return self._available

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
            audio = await providers.synthesize_speech("hello")
        assert audio == b"gem-ogg"
        gem.synthesize.assert_awaited_once()
        el.synthesize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_falls_back_on_failure(self, stub_providers):
        gem, el, _ = stub_providers
        gem.synthesize.side_effect = RuntimeError("gemini down")
        with patch.object(providers.config, "tts_provider", "auto"):
            audio = await providers.synthesize_speech("hello")
        assert audio == b"el-ogg"
        gem.synthesize.assert_awaited_once()
        el.synthesize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pinned_skips_fallback(self, stub_providers):
        gem, el, oa = stub_providers
        gem.synthesize.side_effect = RuntimeError("gemini down")
        with patch.object(providers.config, "tts_provider", "gemini"):
            with pytest.raises(RuntimeError, match="gemini down"):
                await providers.synthesize_speech("hello")
        # Other providers must NOT be called in pinned mode.
        el.synthesize.assert_not_awaited()
        oa.synthesize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pinned_unavailable_raises_value_error(self, stub_providers):
        gem, _, _ = stub_providers
        gem._available = False
        with patch.object(providers.config, "tts_provider", "gemini"):
            with pytest.raises(ValueError, match="TTS_PROVIDER"):
                await providers.synthesize_speech("hello")

    @pytest.mark.asyncio
    async def test_auto_all_unavailable_raises_value_error(self, stub_providers):
        for p in stub_providers:
            p._available = False
        with patch.object(providers.config, "tts_provider", "auto"):
            with pytest.raises(ValueError, match="No TTS API configured"):
                await providers.synthesize_speech("hello")


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
