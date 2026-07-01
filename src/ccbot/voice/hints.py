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

OFF_DIRECTIVE = (
    "[SYSTEM: VOICE MODE OFF]\n"
    "Голосовой режим выключен. Дальше отвечай обычным текстом с markdown "
    "как раньше. НЕ используй аудио-теги ([warmly], [pause] и т.п.), "
    "маркеры [chat]...[/chat] и стилевые префиксы (Say cheerfully: и т.п.) "
    "— они попадут в чат как есть."
)

_CHAT_SPLIT_RULES = (
    "\n\nРазделение голос/чат. Всё что ты пишешь озвучивается, КРОМЕ того что "
    "обёрнуто в [chat]...[/chat] — такие куски уходят в чат текстом с "
    "markdown и не читаются вслух. Оборачивай в [chat]: ссылки, пути, "
    "код/команды, длинные ID/числа/хэши, таблицы, списки, куски логов. "
    "Голосом давай объяснение и контекст, в [chat] — саму суть которую "
    "неудобно слушать. Можно чередовать: голос → [chat]...[/chat] → "
    "голос → [chat]...[/chat]. Пример: «Отчёт готов, вот ссылка. "
    "[chat]https://example.com/report.pdf[/chat] Посмотри, там всё по "
    "пунктам.» Если весь ответ — это ссылка/код, оберни всё в [chat] — "
    "голосом ничего не отправится, это норм."
)


def build_on_directive() -> str:
    """Compose the 'voice mode ON' directive for the active provider."""
    provider = get_active_provider()
    base = (
        "[SYSTEM: VOICE MODE ON]\n"
        "Твой ответ будет озвучен и отправлен как голосовое сообщение. "
        "Говори как живой человек в непринуждённой беседе: от первого лица, с паузами, "
        "междометиями (хм, ну, эм, короче, слушай), лёгкими самоперебивами. "
        "Без markdown, таблиц, кода, списков, эмодзи, заголовков — только обычная речь. "
        "Коротко: 2-3 предложения, кроме сложных вопросов."
    )
    if provider is None:
        return (
            base
            + _CHAT_SPLIT_RULES
            + "\n\nРежим остаётся активным до '[SYSTEM: VOICE MODE OFF]'."
        )

    tags = provider.tag_catalog()
    if tags:
        tag_line = (
            "\nМожно вставлять аудио-теги для выразительности: "
            + " ".join(tags)
            + " — 1-2 на ответ, не чаще. Стиль подачи "
            "(тон, темп) ccbot задаёт сам, ты просто пиши содержание."
        )
    else:
        tag_line = (
            "\nПровайдер голоса не поддерживает аудио-теги — не используй "
            "[warmly], [pause] и т.п., они прочитаются буквально."
        )
    tail = "\nРежим остаётся активным до '[SYSTEM: VOICE MODE OFF]'."
    return base + tag_line + _CHAT_SPLIT_RULES + tail


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
