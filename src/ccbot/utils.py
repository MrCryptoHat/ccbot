"""Shared utility functions used across multiple CCBot modules.

Provides:
  - ccbot_dir(): resolve config directory from CCBOT_DIR env var.
  - atomic_write_json(): crash-safe JSON file writes via temp+rename.
  - schedule_async_json_write(): fire-and-forget variant that runs the
    write (and its fsync) in a dedicated background thread so high-frequency
    state persistence does not stall the asyncio event loop.
  - shutdown_async_writer(): drain the background writer on shutdown.
  - read_cwd_from_jsonl(): extract the cwd field from the first JSONL entry.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CCBOT_DIR_ENV = "CCBOT_DIR"

# Single-thread executor dedicated to background JSON writes. Single-threaded
# on purpose: writes submitted later land on disk later, so a newer snapshot
# never loses a race to an older one (atomic_write_json's os.replace is atomic
# per call, but parallel workers could still reorder the final rename).
_async_write_executor: concurrent.futures.ThreadPoolExecutor | None = None


def ccbot_dir() -> Path:
    """Resolve config directory from CCBOT_DIR env var or default ~/.ccbot."""
    raw = os.environ.get(CCBOT_DIR_ENV, "")
    return Path(raw) if raw else Path.home() / ".ccbot"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory, then renames it
    to the target path. This prevents data corruption if the process
    is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    # Write to temp file in same directory (same filesystem for atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _get_async_write_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _async_write_executor
    if _async_write_executor is None:
        _async_write_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ccbot-json-writer"
        )
    return _async_write_executor


def _log_write_exception(future: concurrent.futures.Future) -> None:
    exc = future.exception()
    if exc is not None:
        logger.error("Background JSON write failed: %s", exc)


def schedule_async_json_write(path: Path, data: Any, indent: int = 2) -> None:
    """Fire-and-forget JSON write that offloads the fsync.

    Serialises submission order through a single-thread executor so a later
    snapshot cannot be overwritten by an earlier one that happened to finish
    second. Intended for high-frequency state persistence (e.g. per-message
    window offset updates) that would otherwise stall the event loop.

    Errors are logged but not raised — this is a best-effort persistence
    path. Falls back to a synchronous write when there is no running event
    loop (e.g. during test setup or early init).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        atomic_write_json(path, data, indent=indent)
        return
    future = _get_async_write_executor().submit(atomic_write_json, path, data, indent)
    future.add_done_callback(_log_write_exception)


def shutdown_async_writer() -> None:
    """Drain and shut down the background writer.

    Call this during graceful shutdown so pending state writes finish
    before the process exits. No-op if the writer was never started.
    """
    global _async_write_executor
    if _async_write_executor is not None:
        _async_write_executor.shutdown(wait=True)
        _async_write_executor = None


def read_cwd_from_jsonl(file_path: str | Path) -> str:
    """Read the cwd field from the first JSONL entry that has one.

    Shared by session.py and session_monitor.py.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cwd = data.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""
