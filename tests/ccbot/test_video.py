"""Tests for ccbot.video and the video hand-off text in handlers.media."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ccbot import video
from ccbot.handlers import media

needs_ffmpeg = pytest.mark.skipif(not video.available(), reason="no ffmpeg")


def _make_clip(path: Path, seconds: int, audio: bool) -> Path:
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i"]
    args += [f"testsrc=duration={seconds}:size=320x240:rate=10"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(args, check=True)
    return path


def test_frame_times_spread_and_capped() -> None:
    assert video.frame_times(0) == [0.0]
    assert video.frame_times(1.0) == [0.5]
    times = video.frame_times(300)
    assert len(times) == video.MAX_FRAMES
    assert times[0] > 0 and times[-1] < 300
    assert len(video.frame_times(6)) == 4  # one per MIN_FRAME_GAP_SEC


def test_fmt_time() -> None:
    assert video.fmt_time(0) == "0:00"
    assert video.fmt_time(75.9) == "1:15"


@needs_ffmpeg
async def test_probe_and_frames(tmp_path: Path) -> None:
    clip = _make_clip(tmp_path / "c.mp4", 6, audio=True)
    duration, has_audio = await video.probe(clip)
    assert 5.5 < duration < 6.5 and has_audio
    frames = await video.extract_frames(clip, duration)
    assert len(frames) == 4
    assert all(f.exists() and f.name.startswith("c_f") for _, f in frames)
    assert await video.extract_audio(clip)


@needs_ffmpeg
async def test_silent_clip_has_no_audio(tmp_path: Path) -> None:
    clip = _make_clip(tmp_path / "s.mp4", 2, audio=False)
    _, has_audio = await video.probe(clip)
    assert not has_audio


async def test_probe_unreadable_file(tmp_path: Path) -> None:
    junk = tmp_path / "j.mp4"
    junk.write_bytes(b"not a video")
    assert await video.probe(junk) == (0.0, False)


@needs_ffmpeg
async def test_attach_text_uses_agent_side_paths(tmp_path: Path, monkeypatch) -> None:
    clip = _make_clip(tmp_path / "v.mp4", 3, audio=True)
    monkeypatch.setattr(media.config, "deepgram_api_key", "k")
    monkeypatch.setattr(media, "transcribe_voice", AsyncMock(return_value="hello"))
    text, frames = await media._video_attach(clip, "/workspace/.inbox/v.mp4")
    lines = text.splitlines()
    assert lines[0].startswith("(video attached: /workspace/.inbox/v.mp4 — 0:03")
    assert frames == 2
    assert lines[1].startswith("(video frame at 0:00: /workspace/.inbox/v_f01.jpg")
    assert lines[-1] == '(said in the video: "hello")'


@needs_ffmpeg
async def test_attach_text_no_speech(tmp_path: Path, monkeypatch) -> None:
    clip = _make_clip(tmp_path / "v.mp4", 2, audio=True)
    monkeypatch.setattr(media.config, "deepgram_api_key", "k")
    monkeypatch.setattr(
        media, "transcribe_voice", AsyncMock(side_effect=ValueError("empty"))
    )
    text, _ = await media._video_attach(clip, "/x/v.mp4")
    assert text.splitlines()[-1] == "(no speech in the video)"


async def test_attach_without_ffmpeg(monkeypatch) -> None:
    monkeypatch.setattr(video, "available", lambda: False)
    text, frames = await media._video_attach(Path("/x/v.mp4"), "/x/v.mp4")
    assert frames == 0 and "couldn't cut it" in text
