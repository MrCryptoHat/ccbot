"""Fence: paid TTS endpoints and keys are touched ONLY by voice/providers.py.

The spend guard (voice/safety.py) bounds TTS cost only because every request
goes through ``synthesize_speech(text, ledger)``. A runtime, transport, plugin
seam or handler that calls a TTS endpoint itself would bypass the budget, the
rate breaker and the per-request audio cap — silently. This test makes that
bypass fail loudly instead.

Transcription (OpenAI / Deepgram speech-to-text in transcribe.py) is a
different, user-initiated product and is allowed its key; it is NOT allowed a
speech-synthesis endpoint.
"""

import re
from pathlib import Path

import ccbot

SRC = Path(ccbot.__file__).parent

# Speech-SYNTHESIS endpoints and the TTS-only keys.
_TTS_MARKERS = re.compile(
    r"generativelanguage\.googleapis\.com"
    r"|api\.elevenlabs\.io"
    r"|/audio/speech"
    r"|gemini_api_key"
    r"|elevenlabs_api_key"
    r"|GEMINI_API_KEY"
    r"|ELEVENLABS_API_KEY"
)

# Files that may mention them: the one provider module, and config, which
# only reads the env (and lists the keys to scrub from child processes).
_ALLOWED = {
    SRC / "voice" / "providers.py",
    SRC / "config.py",
}


def test_tts_endpoints_and_keys_only_in_providers() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path in _ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _TTS_MARKERS.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Paid TTS must go through voice.synthesize_speech(text, ledger) — "
        "found direct use:\n" + "\n".join(offenders)
    )


def test_synthesize_speech_requires_a_ledger() -> None:
    """No default ledger: a caller can't forget to pass one."""
    import inspect

    from ccbot.voice.providers import synthesize_speech

    param = inspect.signature(synthesize_speech).parameters["ledger"]
    assert param.default is inspect.Parameter.empty


def test_tts_keys_are_scrubbed_from_child_processes() -> None:
    """Agents ccbot spawns (any runtime, any transport) must not inherit
    a key they could spend directly."""
    from ccbot.config import SENSITIVE_ENV_VARS

    for key in (
        "GEMINI_API_KEY",
        "ELEVENLABS_API_KEY",
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
    ):
        assert key in SENSITIVE_ENV_VARS
