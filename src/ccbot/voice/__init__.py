"""Voice-mode subsystem.

Public surface:
  synthesize_speech(text) -> bytes
  close_client()                       (shutdown hook)
  build_on_directive() -> str          (Claude system message: voice ON)
  OFF_DIRECTIVE: str                   (Claude system message: voice OFF)
  strip_output_tags(text) -> str       (defensive strip for text-mode output)
  split_voice_segments(text) -> list   (voice/chat split by [chat] markers)
  check_runtime_dependencies()         (startup sanity check)
"""

from .hints import (
    OFF_DIRECTIVE,
    build_on_directive,
    split_voice_segments,
    strip_output_tags,
)
from .providers import close_client, synthesize_speech
from .safety import (
    VOICE_FRESH_WINDOW_SEC,
    BudgetEvent,
    VoiceBudget,
    is_fresh_for_voice,
    parse_iso_to_epoch,
)
from .startup import check_runtime_dependencies

__all__ = [
    "BudgetEvent",
    "OFF_DIRECTIVE",
    "VOICE_FRESH_WINDOW_SEC",
    "VoiceBudget",
    "build_on_directive",
    "check_runtime_dependencies",
    "close_client",
    "is_fresh_for_voice",
    "parse_iso_to_epoch",
    "split_voice_segments",
    "strip_output_tags",
    "synthesize_speech",
]
