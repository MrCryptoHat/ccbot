"""Turn an inbound video into what an agent can actually look at.

Agent CLIs read images and text, not video, and the agent's own sandbox
(a docker agent's container) usually has no ffmpeg to cut it up. So the bot
does it on its side, where ffmpeg already is (Gemini TTS needs it):
- frames: :data:`MAX_FRAMES` stills spread evenly over the clip (one per
  :data:`MIN_FRAME_GAP_SEC` at most), each saved next to the video;
- speech: the audio track as OGG/Opus, for the voice transcriber.

Everything here is best-effort and bounded (``_run`` timeout): a missing
ffmpeg, a broken file or a silent clip just yields fewer parts, and the
caller still hands the agent the video file itself.

Key functions: ``probe(path)``, ``extract_frames(path, duration)``,
``extract_audio(path)``, ``frame_times(duration)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FRAMES = 12
MIN_FRAME_GAP_SEC = 1.5
# Frames go to a model, not a screen: 1280 px on the long side keeps UI text
# readable at a fraction of a full-size frame's bytes.
FRAME_MAX_SIDE = 1280
_TIMEOUT_SEC = 60.0


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


async def _run(*args: str) -> tuple[int, bytes]:
    """Run a command, return (exit code, stdout); -1 on timeout or OSError."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as e:
        logger.warning("video: %s failed to start: %s", args[0], e)
        return -1, b""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("video: %s timed out", args[0])
        return -1, b""
    return proc.returncode or 0, out


async def probe(path: Path) -> tuple[float, bool]:
    """(duration in seconds, has an audio track); (0.0, False) if unreadable."""
    code, out = await _run(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        str(path),
    )
    if code != 0:
        return 0.0, False
    try:
        data = json.loads(out)
        duration = float(data.get("format", {}).get("duration") or 0.0)
        streams = data.get("streams") or []
    except (ValueError, TypeError, AttributeError):
        return 0.0, False
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return max(duration, 0.0), has_audio


def frame_times(duration: float) -> list[float]:
    """Evenly spread capture points, each in the middle of its slice — never
    the very first/last frame, which are often black or mid-transition."""
    if duration <= 0:
        return [0.0]
    n = max(1, min(MAX_FRAMES, int(duration / MIN_FRAME_GAP_SEC)))
    step = duration / n
    return [round((i + 0.5) * step, 2) for i in range(n)]


async def extract_frames(path: Path, duration: float) -> list[tuple[float, Path]]:
    """Save stills of ``path`` as ``<stem>_fNN.jpg`` beside it.

    Returns (timestamp, file) for every frame that came out.
    """
    scale = (
        f"scale='if(gt(iw,ih),min({FRAME_MAX_SIDE},iw),-2)'"
        f":'if(gt(iw,ih),-2,min({FRAME_MAX_SIDE},ih))'"
    )
    frames: list[tuple[float, Path]] = []
    for i, t in enumerate(frame_times(duration), start=1):
        out = path.with_name(f"{path.stem}_f{i:02d}.jpg")
        code, _ = await _run(
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{t}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            scale,
            "-q:v",
            "3",
            str(out),
        )
        if code == 0 and out.exists() and out.stat().st_size > 0:
            frames.append((t, out))
    return frames


async def extract_audio(path: Path) -> bytes | None:
    """The audio track as OGG/Opus bytes (what the transcriber takes)."""
    code, out = await _run(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-f",
        "ogg",
        "pipe:1",
    )
    return out if code == 0 and out else None


def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"
