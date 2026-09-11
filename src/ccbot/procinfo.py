"""Process-table reads that work on Linux (/proc) and macOS (ps) alike.

ccbot inspects processes it didn't start — the shells tmux spawned for agent
panes, the ancestors of a hook invocation — and the platforms expose them
differently: Linux through ``/proc/<pid>/stat``, macOS/BSD only through
``ps``. A reader that knows only /proc works on the server and silently
returns nothing on a Mac, so both legs live here, picked by
``proc_root.is_dir()``.

  - read_proc_stat: comm plus the remaining fields of ``/proc/<pid>/stat``.
  - foreground_process_groups: pid → foreground process group of its
    controlling terminal (``tpgid``), for many pids in one pass.

Stdlib only — hook.py imports this and must stay free of config.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

PROC_ROOT = Path("/proc")


def read_proc_stat(proc_root: Path, pid: int) -> tuple[str, list[bytes]] | None:
    """``(comm, fields after comm)`` from ``/proc/<pid>/stat`` — Linux.

    ``fields[i]`` is proc(5) field ``i + 3``: ``fields[0]`` state,
    ``fields[1]`` ppid, ``fields[5]`` tpgid.
    """
    try:
        stat = (proc_root / str(pid) / "stat").read_bytes()
    except OSError:
        return None
    # "<pid> (<comm>) <state> <ppid> …" — comm can contain ')' and spaces,
    # so anchor on the LAST ')'.
    lparen = stat.find(b"(")
    rparen = stat.rfind(b")")
    if lparen < 0 or rparen < lparen:
        return None
    comm = stat[lparen + 1 : rparen].decode("utf-8", "replace")
    return comm, stat[rparen + 1 :].split()


def foreground_process_groups(
    pids: Iterable[int], proc_root: Path = PROC_ROOT
) -> dict[int, int]:
    """``pid → tpgid``: which process group holds each pid's terminal.

    A pid that can't be read (exited, unparsable) is simply absent, and a
    process without a controlling terminal reports ``-1`` or ``0`` — callers
    must treat both as "unknown", never as an answer.
    """
    wanted = sorted(set(pids))
    if not wanted:
        return {}
    if proc_root.is_dir():
        return _proc_tpgids(proc_root, wanted)
    return _ps_tpgids(wanted)


def _proc_tpgids(proc_root: Path, pids: list[int]) -> dict[int, int]:
    """``tpgid`` per pid from ``/proc/<pid>/stat`` — Linux."""
    found: dict[int, int] = {}
    for pid in pids:
        entry = read_proc_stat(proc_root, pid)
        if entry is None or len(entry[1]) < 6:
            continue
        try:
            found[pid] = int(entry[1][5])
        except ValueError:
            continue
    return found


def _ps_tpgids(pids: list[int]) -> dict[int, int]:
    """``tpgid`` per pid from ONE ``ps`` call — macOS/BSD, which have no /proc.

    The exit status is ignored on purpose: ``ps -p`` exits 1 when none of the
    pids exists, and a pid that exited since the caller listed it is just left
    out — parsing whatever came back keeps every answer that is still valid.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,tpgid=", "-p", ",".join(map(str, pids))],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            found[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return found
