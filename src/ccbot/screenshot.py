"""Terminal text → PNG screenshot renderer.

Converts captured tmux pane text (with optional ANSI color codes) into a
dark-background PNG image. Supports full ANSI color parsing (16/256/RGB)
and a three-tier font fallback chain:
  1. JetBrains Mono — Latin, symbols, box-drawing
  2. Noto Sans Mono CJK SC — CJK characters
  3. Symbola — remaining special symbols

Key function: text_to_image(text, font_size, with_ansi) → PNG bytes.
"""

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_FONTS_DIR = Path(__file__).parent / "fonts"

# Font fallback chain (highest priority first):
#   1. JetBrains Mono (OFL-1.1) — Latin, symbols, box-drawing, blocks
#   2. Noto Sans Mono CJK SC (OFL-1.1) — CJK, additional symbols
#   3. Symbola (free license) — remaining miscellaneous symbols, dingbats
_FONT_PATHS: list[Path] = [
    _FONTS_DIR / "JetBrainsMono-Regular.ttf",
    _FONTS_DIR / "NotoSansMonoCJKsc-Regular.otf",
    _FONTS_DIR / "Symbola.ttf",
]

# Pre-computed codepoint sets for characters NOT in JetBrains Mono.
# Tier 2: present in Noto Sans Mono CJK SC (CJK ideographs, fullwidth punctuation, etc.)
_NOTO_CODEPOINTS: set[int] = {
    0x23BF,  # ⎿ DENTISTRY SYMBOL LIGHT VERTICAL AND BOTTOM RIGHT
}
# Tier 3: only in Symbola (misc symbols not in either JB or Noto)
_SYMBOLA_CODEPOINTS: set[int] = {
    0x23F5,  # ⏵ BLACK MEDIUM RIGHT-POINTING TRIANGLE
    0x2714,  # ✔ HEAVY CHECK MARK
    0x274C,  # ❌ CROSS MARK
}

# ANSI color mapping (basic 16 colors)
_ANSI_COLORS: dict[int, tuple[int, int, int]] = {
    # Standard colors (30-37, 40-47)
    0: (0, 0, 0),  # Black
    1: (205, 49, 49),  # Red
    2: (13, 188, 121),  # Green
    3: (229, 229, 16),  # Yellow
    4: (36, 114, 200),  # Blue
    5: (188, 63, 188),  # Magenta
    6: (17, 168, 205),  # Cyan
    7: (229, 229, 229),  # White
    # Bright colors (90-97, 100-107)
    8: (102, 102, 102),  # Bright Black
    9: (241, 76, 76),  # Bright Red
    10: (35, 209, 139),  # Bright Green
    11: (245, 245, 67),  # Bright Yellow
    12: (59, 142, 234),  # Bright Blue
    13: (214, 112, 214),  # Bright Magenta
    14: (41, 184, 219),  # Bright Cyan
    15: (255, 255, 255),  # Bright White
}

# Default colors for terminals
_DEFAULT_FG = (212, 212, 212)  # Light gray
_DEFAULT_BG = (30, 30, 30)  # Dark gray

# Long-side cap. Telegram's chat photo viewer on iPhone is ~340 pt =
# ~1020 px on retina, so anything bigger just gets downsampled at view
# time. 1024 matches the actual display res 1:1 and keeps the JPEG
# Telegram serves under ~200 KB. Aspect follows content (no forced
# square): tried 1024×1024 fit-and-pad and it left visible side bg on
# tall portrait panes — user flagged the side margins as worse than
# variable aspect.
_CANVAS_SIZE = 1024
# Max long-side (px) for non-square table images — bounds PNG size while
# keeping columns legible; Telegram downscales for display, user can zoom.
_TABLE_MAX_SIDE = 1600

# Adaptive palette size for the PNG quantize step. 256 keeps the full
# AA-halo gradient around every glyph stroke smooth — Telegram's JPEG
# re-encode q≈87 reads a smooth gradient as a continuous edge and
# preserves crispness; a coarser 64-colour palette quantizes the halo
# into 4-5 stepped bands that JPEG then mistakes for hard transitions
# and renders rough. The ~10–15 % PNG-size win from going to 64 isn't
# worth the visible reduction in font sharpness on iOS.
_PALETTE_COLORS = 256

# Maximum allowed width:height ratio. When pane content is sparse
# (e.g. a status spinner + a few output lines after trim) but lines
# are wide (~100 cols), the rendered image becomes a thin ribbon —
# Telegram fits chat photos to chat-width with proportional height,
# so a 1024×400 source displays at ~340 pt × 135 pt on iPhone and the
# text inside ends up unreadable. When aspect exceeds this cap, pad
# the bottom with bg so the photo lands as a more readable rectangle
# in chat. Conservative threshold — normal panes (portrait / near-
# square) skip the pad entirely; only the genuine ribbon case pays
# the dead-space cost, and that cost is necessary to keep the text
# legible at iPhone viewing scale.
_MAX_ASPECT_RATIO = 1.6


@dataclass
class TextStyle:
    """Text styling information from ANSI codes."""

    fg_color: tuple[int, int, int] = _DEFAULT_FG
    bg_color: tuple[int, int, int] | None = None


@dataclass
class StyledSegment:
    """A text segment with its styling."""

    text: str
    style: TextStyle
    font_tier: int


def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a TrueType/OpenType font, falling back to Pillow default."""
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        logger.warning("Failed to load font %s, using Pillow default", path)
        return ImageFont.load_default()


def _font_tier(ch: str) -> int:
    """Return 0 (JetBrains), 1 (Noto CJK), or 2 (Symbola) for a character."""
    cp = ord(ch)
    if cp in _SYMBOLA_CODEPOINTS:
        return 2
    # CJK Unified Ideographs + CJK compat + fullwidth forms + Hangul + known Noto-only codepoints
    if (
        cp in _NOTO_CODEPOINTS
        or cp >= 0x1100
        and (
            cp <= 0x11FF  # Hangul Jamo
            or 0x2E80 <= cp <= 0x9FFF  # CJK radicals, kangxi, ideographs
            or 0xAC00 <= cp <= 0xD7AF  # Hangul Syllables
            or 0xF900 <= cp <= 0xFAFF  # CJK compat ideographs
            or 0xFE30 <= cp <= 0xFE4F  # CJK compat forms
            or 0xFF00 <= cp <= 0xFFEF  # fullwidth forms
            or 0x20000 <= cp <= 0x2FA1F  # CJK extension B+
        )
    ):
        return 1
    return 0


def _parse_ansi_line(line: str) -> list[StyledSegment]:
    """Parse a line with ANSI escape codes into styled segments."""
    # ANSI escape sequence pattern
    ansi_pattern = re.compile(r"\x1b\[([0-9;]*)m")

    segments: list[StyledSegment] = []
    current_style = TextStyle()
    pos = 0

    for match in ansi_pattern.finditer(line):
        # Add text before this escape code
        text_before = line[pos : match.start()]
        if text_before:
            # Split by font tier
            for seg_text, tier in _split_line_segments_plain(text_before):
                if seg_text:
                    segments.append(StyledSegment(seg_text, current_style, tier))

        # Parse escape code
        codes = match.group(1)
        if codes:
            current_style = _apply_ansi_codes(current_style, codes)
        else:
            # Empty code means reset
            current_style = TextStyle()

        pos = match.end()

    # Add remaining text after last escape code
    text_after = line[pos:]
    if text_after:
        for seg_text, tier in _split_line_segments_plain(text_after):
            if seg_text:
                segments.append(StyledSegment(seg_text, current_style, tier))

    return segments if segments else [StyledSegment("", TextStyle(), 0)]


def _apply_ansi_codes(style: TextStyle, codes: str) -> TextStyle:
    """Apply ANSI color codes to a text style."""
    # Create a new style (copy current)
    new_style = TextStyle(
        fg_color=style.fg_color,
        bg_color=style.bg_color,
    )

    parts = [int(c) for c in codes.split(";") if c]
    i = 0
    while i < len(parts):
        code = parts[i]

        if code == 0:  # Reset
            new_style = TextStyle()
        elif 30 <= code <= 37:  # Foreground color
            new_style.fg_color = _ANSI_COLORS[code - 30]
        elif code == 38:  # Extended foreground color
            if i + 1 < len(parts) and parts[i + 1] == 5:  # 256 color
                if i + 2 < len(parts):
                    color_idx = parts[i + 2] % 256
                    if color_idx < 16:
                        new_style.fg_color = _ANSI_COLORS[color_idx]
                    else:
                        # Approximate 256 colors (simplified)
                        new_style.fg_color = _approximate_256_color(color_idx)
                    i += 2
            elif i + 1 < len(parts) and parts[i + 1] == 2:  # RGB color
                if i + 4 < len(parts):
                    new_style.fg_color = (parts[i + 2], parts[i + 3], parts[i + 4])
                    i += 4
        elif code == 39:  # Default foreground
            new_style.fg_color = _DEFAULT_FG
        elif 40 <= code <= 47:  # Background color
            new_style.bg_color = _ANSI_COLORS[code - 40]
        elif code == 48:  # Extended background color
            if i + 1 < len(parts) and parts[i + 1] == 5:  # 256 color
                if i + 2 < len(parts):
                    color_idx = parts[i + 2] % 256
                    if color_idx < 16:
                        new_style.bg_color = _ANSI_COLORS[color_idx]
                    else:
                        new_style.bg_color = _approximate_256_color(color_idx)
                    i += 2
            elif i + 1 < len(parts) and parts[i + 1] == 2:  # RGB color
                if i + 4 < len(parts):
                    new_style.bg_color = (parts[i + 2], parts[i + 3], parts[i + 4])
                    i += 4
        elif code == 49:  # Default background
            new_style.bg_color = None
        elif 90 <= code <= 97:  # Bright foreground color
            new_style.fg_color = _ANSI_COLORS[code - 90 + 8]
        elif 100 <= code <= 107:  # Bright background color
            new_style.bg_color = _ANSI_COLORS[code - 100 + 8]

        i += 1

    return new_style


def _approximate_256_color(idx: int) -> tuple[int, int, int]:
    """Approximate a 256-color palette index to RGB."""
    if idx < 16:
        return _ANSI_COLORS[idx]
    elif idx < 232:
        # 216 color cube: 16 + 36*r + 6*g + b
        idx -= 16
        r = (idx // 36) * 51
        g = ((idx % 36) // 6) * 51
        b = (idx % 6) * 51
        return (r, g, b)
    else:
        # Grayscale: 232-255
        gray = 8 + (idx - 232) * 10
        return (gray, gray, gray)


def _split_line_segments_plain(line: str) -> list[tuple[str, int]]:
    """Split a line into (text, font_tier) segments.

    Consecutive characters sharing the same tier are merged.
    """
    if not line:
        return [("", 0)]
    segments: list[tuple[str, int]] = []
    cur_tier = _font_tier(line[0])
    start = 0
    for i in range(1, len(line)):
        tier = _font_tier(line[i])
        if tier != cur_tier:
            segments.append((line[start:i], cur_tier))
            cur_tier = tier
            start = i
    segments.append((line[start:], cur_tier))
    return segments


async def text_to_image(
    text: str, font_size: int = 28, with_ansi: bool = True, square: bool = True
) -> bytes:
    """Render monospace text onto a dark-background image and return PNG bytes.

    Args:
        text: The text to render (may contain ANSI color codes)
        font_size: Font size in pixels
        with_ansi: If True, parse and render ANSI color codes
        square: If True (default, /screenshot pane), force a _CANVAS_SIZE
            square — top-crops tall panes, bottom-pads short ones. If False
            (table images), aspect follows content with a tight crop, just
            bounding the long side so a big table stays a sane PNG.

    Returns:
        PNG image bytes
    """

    def _render_image() -> bytes:
        fonts = [_load_font(p, font_size) for p in _FONT_PATHS]

        # Strip OSC 8 hyperlinks etc. before rendering — the SGR parser
        # below ignores them and the URL bytes would otherwise show up as
        # visible text and ribbon out the image width.
        from .terminal_parser import strip_osc

        lines = strip_osc(text).split("\n")
        # tmux capture-pane returns the full geometry (e.g. 50 rows) even
        # when only the top 30 carry content. Without trimming, we'd render
        # the whole canvas with a huge bottom margin of empty rows — wasted
        # bytes, ugly in chat. Strip trailing blank lines so the rendered
        # height matches the actual content.
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            lines = [""]
        # 8 px padding (was 16). Telegram's auto-thumbnail crops a bit
        # anyway, so 8 is enough breathing room around the text without
        # the dead-space look the user flagged.
        padding = 8

        # Parse lines into styled segments
        if with_ansi:
            line_segments = [_parse_ansi_line(line) for line in lines]
        else:
            # Legacy plain text mode
            line_segments_plain = [_split_line_segments_plain(line) for line in lines]
            line_segments = [
                [
                    StyledSegment(seg_text, TextStyle(), tier)
                    for seg_text, tier in segments
                ]
                for segments in line_segments_plain
            ]

        # Measure text size
        dummy = Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(dummy)
        line_height = int(font_size * 1.4)
        max_width = 0
        for segments in line_segments:
            w = 0
            for seg in segments:
                bbox = draw.textbbox((0, 0), seg.text, font=fonts[seg.font_tier])
                w += bbox[2] - bbox[0]
            max_width = max(max_width, w)

        img_width = int(max_width) + padding * 2
        img_height = line_height * len(lines) + padding * 2

        img = Image.new("RGB", (img_width, img_height), _DEFAULT_BG)
        draw = ImageDraw.Draw(img)

        y = padding
        for segments in line_segments:
            x = padding
            for seg in segments:
                f = fonts[seg.font_tier]

                # Draw background if specified
                if seg.style.bg_color:
                    bbox = draw.textbbox((x, y), seg.text, font=f)
                    draw.rectangle(
                        [bbox[0], y, bbox[2], y + line_height], fill=seg.style.bg_color
                    )

                # Draw text with foreground color
                draw.text((x, y), seg.text, fill=seg.style.fg_color, font=f)

                bbox = draw.textbbox((0, 0), seg.text, font=f)
                x += bbox[2] - bbox[0]
            y += line_height

        # Auto-crop to actual content, then re-apply uniform padding.
        # Without this, lines with short visible content leave dead bg
        # space on the right of the canvas (canvas width = longest line,
        # but most lines are shorter). The diff-vs-bg bbox catches every
        # rendered glyph stroke; we then re-pad with the same `padding`
        # value so margins stay tight and uniform on all four sides.
        diff = ImageChops.difference(img, Image.new("RGB", img.size, _DEFAULT_BG))
        bbox = diff.getbbox()
        if bbox is not None:
            cropped = img.crop(bbox)
            img = Image.new(
                "RGB",
                (cropped.size[0] + padding * 2, cropped.size[1] + padding * 2),
                _DEFAULT_BG,
            )
            img.paste(cropped, (padding, padding))

        # Scale so width = _CANVAS_SIZE, then crop or pad height to make
        # the final image a perfect _CANVAS_SIZE × _CANVAS_SIZE square.
        # Two guarantees: (a) every screenshot lands in chat as the same
        # square block; (b) no side bg padding ever — width is always
        # filled with content. The cost: tall panes get top-cropped, so
        # the oldest rows fall off (terminal stream semantics — bottom
        # is most-recent activity, which is what the user is reading).
        # Short panes get bottom-padded with bg.
        # NEAREST resampling: terminal glyphs are hard-edged ANSI;
        # LANCZOS' AA halos defeat the palette quantize and *grow* the
        # PNG on heavy panes (verified: 167→226 KB).
        if square:
            scale = _CANVAS_SIZE / img.size[0]
            new_w = _CANVAS_SIZE
            new_h = max(1, int(img.size[1] * scale))
            if (new_w, new_h) != img.size:
                img = img.resize((new_w, new_h), Image.Resampling.NEAREST)
            if new_h > _CANVAS_SIZE:
                # Tall content: crop top (keep bottom — most recent output).
                img = img.crop((0, new_h - _CANVAS_SIZE, _CANVAS_SIZE, new_h))
            elif new_h < _CANVAS_SIZE:
                # Short content: pad bottom with bg. No side padding ever
                # since width already equals canvas width.
                canvas = Image.new("RGB", (_CANVAS_SIZE, _CANVAS_SIZE), _DEFAULT_BG)
                canvas.paste(img, (0, 0))
                img = canvas
        else:
            # Table image: aspect follows content (already tight-cropped to
            # the glyph bbox above), so there's no dead space. Only bound the
            # long side so a big table stays a reasonable PNG — Telegram
            # downscales for display and the user can pinch-zoom. LANCZOS
            # here (not NEAREST): downscaling rendered text reads far better
            # with AA, and a bordered table is light on colours so the halo
            # doesn't bloat the PNG the way a dense ANSI pane would.
            long_side = max(img.size)
            if long_side > _TABLE_MAX_SIDE:
                s = _TABLE_MAX_SIDE / long_side
                img = img.resize(
                    (max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))),
                    Image.Resampling.LANCZOS,
                )

        # Adaptive palette quantize. 64 colours covers the full ANSI table
        # (16 base + 16 bright) plus the AA halo shades the font hinting
        # produces, with room to spare. Smaller palette table + better
        # Deflate runs on solid bg = ~10–15 % off the PNG vs colors=256.
        img_p = img.convert("P", palette=Image.Palette.ADAPTIVE, colors=_PALETTE_COLORS)
        buf = io.BytesIO()
        img_p.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    # Run CPU-intensive image rendering in thread pool
    return await asyncio.to_thread(_render_image)
