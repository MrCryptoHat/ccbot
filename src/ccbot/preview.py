"""Preview-server registry helpers — shared by the /status command and
the Live-topic dashboard.

Preview servers are ephemeral per-agent HTTP dev servers managed by an
external ``preview`` CLI; it records each one in ``REGISTRY_PATH`` and
runs it inside a tmux session named ``preview-<slug>``. Both the
``/status`` command's "Preview" section and ``handlers/live_board``'s
"🌐 Preview-серверы" section read that registry, check the port, and
render remaining TTL — this module is the single home for those two
checks and the registry path so neither consumer has to reach into the
other module's internals.
"""

import socket
from datetime import datetime, timedelta, timezone

from .config import config

# Where the `preview` CLI persists its registry of active servers
# (``CCBOT_PREVIEW_REGISTRY``; defaults to this server's XDG state path).
REGISTRY_PATH = config.preview_registry_path


def port_listening(port: int) -> bool:
    """True iff something accepts a TCP connection on 127.0.0.1:<port>.

    Short timeout (0.3 s) — we're polling many ports on the status path;
    a hung connect must not stall the caller.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except Exception:
        return False


def ttl_remaining(started_iso: str, ttl: str) -> str:
    """Human "time left" for a preview given its start time and TTL string.

    ``ttl`` is the CLI's form: ``"never"`` (→ ``"∞"``) or ``"<N><unit>"``
    where unit is ``m``/``h``/``d``. Returns ``"expired"`` once the
    deadline has passed, ``"?"`` if the inputs don't parse (the registry
    is third-party JSON — never crash on it).
    """
    if ttl == "never":
        return "∞"
    try:
        started = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
        amount = int(ttl[:-1])
        unit = ttl[-1]
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }[unit]
        left = (started + delta) - datetime.now(timezone.utc)
        if left.total_seconds() <= 0:
            return "expired"
        mins = int(left.total_seconds() // 60)
        return f"{mins // 60}h{mins % 60}m" if mins >= 60 else f"{mins}m"
    except Exception:
        return "?"
