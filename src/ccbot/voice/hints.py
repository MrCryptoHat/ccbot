"""Voice-mode directives Claude sees at state changes + tag stripper.

The ON directive is built at call time from the active TTS provider's
tag catalog, so switching providers doesn't require changing this text.
Providers that are plain-text readers (ElevenLabs, OpenAI) get a
minimal hint without tag vocabulary — mentioning tags they don't
support would just waste context.

Sent ONCE per session (first user message after voice goes on, or after
/clear while still on), not per user message. See SessionManager.
consume_voice_directive for the anchoring logic.

Voice/chat split protocol: in voice mode Claude can wrap parts of the
reply in [chat]...[/chat]. Those parts are delivered as regular text
messages (with markdown); everything outside is synthesized to speech.
split_voice_segments() is the parser; _process_content_task walks its
output to pick send_voice vs send_with_fallback per segment.
"""

from __future__ import annotations

import re
from typing import Literal

from .providers import get_active_provider

SegmentKind = Literal["voice", "chat"]

_TAG_STRIP_RE = re.compile(
    r"\[(?:warmly|laughing|sighs?|excited|angry|sad|bored|sleepy|nervous|"
    r"whispering|shouting|thoughtfully|sarcastic|curious|concerned|"
    r"long pause|pause|quickly|slowly)\][ \t]*",
    re.IGNORECASE,
)

_CHAT_TAG_STRIP_RE = re.compile(r"\[/?chat\][ \t]*", re.IGNORECASE)

_STYLE_PREFIX_STRIP_RE = re.compile(
    r"^Say\s+[a-zA-Z\s]{1,40}:\s*",
    re.MULTILINE | re.IGNORECASE,
)

_CHAT_OPEN_RE = re.compile(r"\[chat\]", re.IGNORECASE)
_CHAT_CLOSE_RE = re.compile(r"\[/chat\]", re.IGNORECASE)

# Agent-directed protocol text is deliberately NOT in the i18n catalog (see
# i18n.py scope note) — it is an instruction to the agent, not chrome the user
# reads. It is written in English because a directive's language nudges the
# agent to answer the user in that language (observed in practice).

_OFF_DIRECTIVE = (
    "[SYSTEM: VOICE MODE OFF]\n"
    "Voice mode is off. Reply in regular markdown text from now on. Do "
    "NOT use audio tags ([warmly], [pause] etc.), [chat]...[/chat] "
    "markers or style prefixes (Say cheerfully: etc.) — they would land "
    "in the chat verbatim."
)

_CHAT_SPLIT_RULES = (
    "\n\nVoice/chat split. Everything you write is spoken aloud EXCEPT "
    "parts wrapped in [chat]...[/chat] — those go to the chat as markdown "
    "text and are not read out. Wrap in [chat]: links, paths, "
    "code/commands, long IDs/numbers/hashes, tables, lists, log excerpts. "
    "Use voice for explanation and context, [chat] for the exact content "
    "that is awkward to listen to. You can alternate: voice → "
    "[chat]...[/chat] → voice → [chat]...[/chat]. Example: “The report is "
    "ready, here's the link. [chat]https://example.com/report.pdf[/chat] "
    "Have a look, it's all itemized.” If the whole reply is a link/code, "
    "wrap it all in [chat] — no voice message gets sent, that's fine."
)

_ON_BASE = (
    "[SYSTEM: VOICE MODE ON]\n"
    "Your reply will be synthesized and sent as a voice message. Speak "
    "like a real person in casual conversation: first person, natural "
    "pauses, fillers (hm, well, you know), light self-corrections. No "
    "markdown, tables, code, lists, emoji or headings — plain speech "
    "only. Keep it short: 2-3 sentences unless the question is complex."
)

_ON_TAGS = (
    "\nYou may insert audio tags for expressiveness: {tags} — 1-2 per "
    "reply, no more. Delivery style (tone, pace) is set by ccbot; you "
    "just write the content."
)

_ON_NO_TAGS = (
    "\nThe voice provider does not support audio tags — don't use "
    "[warmly], [pause] etc., they would be read out literally."
)

_ON_TAIL = "\nThe mode stays active until '[SYSTEM: VOICE MODE OFF]'."


def off_directive() -> str:
    """The 'voice mode OFF' directive sent to the agent."""
    return _OFF_DIRECTIVE


def build_on_directive() -> str:
    """Compose the 'voice mode ON' directive for the active provider."""
    provider = get_active_provider()
    if provider is None:
        return _ON_BASE + _CHAT_SPLIT_RULES + "\n" + _ON_TAIL
    tags = provider.tag_catalog()
    tag_line = _ON_TAGS.format(tags=" ".join(tags)) if tags else _ON_NO_TAGS
    return _ON_BASE + tag_line + _CHAT_SPLIT_RULES + _ON_TAIL


def split_voice_segments(text: str) -> list[tuple[SegmentKind, str]]:
    """Split text into voice and chat segments by [chat]...[/chat] markers.

    Segments are returned in source order. Consecutive voice-only text
    (no markers) yields a single ("voice", text) entry. Whitespace-only
    segments are dropped so we don't send empty messages or silence.

    Nesting is balanced like brackets: inside an open [chat] block, a
    further [chat] bumps the depth and is held as literal content until
    its matching [/chat] closes it. The outer block only closes when
    depth returns to zero. This keeps user content that happens to
    contain the literal `[chat]...[/chat]` (e.g. pasted commit messages
    or docs about the protocol itself) intact — otherwise the first
    inner [/chat] would prematurely close the outer block and bleed
    tail content into the next voice segment.

    Rules:
      - Case-insensitive [chat] / [/chat].
      - Unclosed [chat] → everything after it is chat until end of text.
      - [/chat] without a matching open in voice mode is treated as
        literal content (stripped by downstream cleanup if sent as text).
    """
    segments: list[tuple[SegmentKind, str]] = []
    i = 0
    mode: SegmentKind = "voice"
    buf: list[str] = []
    depth = 0  # nesting depth when mode == "chat"

    def flush() -> None:
        if not buf:
            return
        chunk = "".join(buf).strip()
        buf.clear()
        if chunk:
            segments.append((mode, chunk))

    while i < len(text):
        if mode == "voice":
            m = _CHAT_OPEN_RE.search(text, i)
            if m is None:
                buf.append(text[i:])
                i = len(text)
                break
            buf.append(text[i : m.start()])
            flush()
            mode = "chat"
            depth = 1
            i = m.end()
        else:  # chat — match [chat] and [/chat] in order, balanced
            open_m = _CHAT_OPEN_RE.search(text, i)
            close_m = _CHAT_CLOSE_RE.search(text, i)
            if close_m is None:
                # No closing tag left — consume to end (defensive).
                buf.append(text[i:])
                i = len(text)
                break
            if open_m is not None and open_m.start() < close_m.start():
                # Nested open: keep literal, bump depth.
                buf.append(text[i : open_m.end()])
                depth += 1
                i = open_m.end()
                continue
            # Closing tag
            depth -= 1
            if depth > 0:
                # Inner close — keep literal, stay in chat.
                buf.append(text[i : close_m.end()])
                i = close_m.end()
                continue
            # Outermost close — emit the chat segment.
            buf.append(text[i : close_m.start()])
            flush()
            mode = "voice"
            i = close_m.end()

    flush()
    return segments


def strip_output_tags(text: str) -> str:
    """Remove TTS audio tags / style prefixes from text-mode output.

    Defensive: used when voice is off (Claude may still emit tags from
    prior turn inertia) or when TTS failed and we fell back to text.
    Also strips [chat]/[/chat] markers — they're a voice-mode protocol
    and have no meaning in plain text topics.

    Must stay a no-op on tag-free text: every text reply passes through
    here, and anything broader than the specific voice tokens (e.g. a
    whitespace collapse) mangles code blocks and aligned output.
    """
    text = _TAG_STRIP_RE.sub("", text)
    text = _CHAT_TAG_STRIP_RE.sub("", text)
    text = _STYLE_PREFIX_STRIP_RE.sub("", text)
    return text
