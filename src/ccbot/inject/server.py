"""aiohttp listener for the /inject endpoint (fire-and-forget task injection).

Transport is a **unix socket** (default ``~/.ccbot/run/inject.sock``,
mode ``0660`` under a ``0700`` parent dir), so it's unreachable from docker
containers and other uids — only a process running as the same local user
can POST to it.

Single route: ``POST /inject`` with header ``X-Inject-Token: <secret>`` and
body ``{"agent": "<name>", "text": "<task>"}``. The handler:
  1. checks the token (constant-time),
  2. gates the agent against the allowlist,
  3. resolves the name to a binding — ``docker:<name>`` for a docker agent,
     ``@<id>`` for a live host tmux window of that name (so it can drive
     both docker and host-tmux agents),
  4. sanitizes the text (the leading-``!``/``/`` shield, see ``core``),
  5. refuses if the agent is mid-turn / showing an interactive prompt,
  6. otherwise types the task into the agent's pane as a prompt.

The agent's reply reaches the user in Telegram as usual — injection is
one-way and doesn't return the result to the caller (by design).

Status codes:
  - 401 bad/missing token (constant-time compare, opaque body)
  - 400 malformed JSON / non-string or empty-after-sanitize text
  - 403 ``forbidden_agent`` — agent not in the allowlist
  - 503 ``not_running`` — allowlisted but no docker agent and no live tmux
    window of that name (the agent simply isn't up; distinct from 403)
  - 409 ``busy`` — a turn or interactive prompt is in progress
  - 503 ``unavailable`` — binding resolved but the pane is uncapturable
    (container down / window died) or the send failed
  - 200 ``{"ok": true}`` — task injected

Lifecycle: ``start_server(cfg)`` returns an
``AppRunner`` that ``bot.post_init`` stashes; ``post_shutdown`` calls
``runner.cleanup()`` and unlinks the socket. Runs on PTB's event loop.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .. import terminal_parser
from ..session import session_manager
from .core import sanitize_inject_text

if TYPE_CHECKING:
    from ..config import InjectConfig

logger = logging.getLogger(__name__)

_CFG_KEY: web.AppKey["InjectConfig"] = web.AppKey("inject_cfg")
_TOKEN_HEADER = "X-Inject-Token"


async def _handle_inject(request: web.Request) -> web.Response:
    cfg = request.app[_CFG_KEY]

    supplied = request.headers.get(_TOKEN_HEADER, "")
    if not secrets.compare_digest(supplied, cfg.token):
        # Don't echo anything useful — log locally, stay opaque on the wire.
        logger.warning("inject: unauthorized request (bad/missing token)")
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    agent = str(body.get("agent") or "").strip()
    raw_text = body.get("text")
    if not isinstance(raw_text, str):
        return web.json_response({"ok": False, "error": "bad_text"}, status=400)

    # Allowlist gate first, then resolve the name to a transport binding.
    if agent not in cfg.allowed_agents:
        logger.warning("inject: agent %r not in allowlist", agent)
        return web.json_response({"ok": False, "error": "forbidden_agent"}, status=403)

    # docker:<name> for docker agents; @<id> for a live host tmux window;
    # None when neither exists. None ≠ forbidden: the name IS allowlisted
    # but the agent isn't up (a host tmux agent runs only when started), so
    # the caller can show "not started" rather than conflate it with "not allowed".
    binding = await session_manager.resolve_agent_binding(agent)
    if binding is None:
        logger.warning("inject: agent %r allowlisted but not running", agent)
        return web.json_response({"ok": False, "error": "not_running"}, status=503)

    text = sanitize_inject_text(raw_text)
    if not text.strip():
        return web.json_response({"ok": False, "error": "empty_text"}, status=400)

    # Reject-if-busy: this is one-way fire-and-forget — far better to bounce
    # ("busy, retry") than barge into a running turn or an open prompt and
    # corrupt it. A None pane means the container is down / unreadable.
    pane = await session_manager.capture_pane(binding)
    if pane is None:
        logger.warning("inject: pane uncapturable for %s (agent down?)", binding)
        return web.json_response({"ok": False, "error": "unavailable"}, status=503)
    if terminal_parser.is_interactive_ui(pane) or terminal_parser.is_claude_working(
        pane
    ):
        return web.json_response({"ok": False, "error": "busy"}, status=409)

    ok, msg = await session_manager.send_to_window(binding, text)
    if not ok:
        logger.warning("inject: send to %s failed: %s", binding, msg)
        return web.json_response(
            {"ok": False, "error": "unavailable", "detail": msg}, status=503
        )

    logger.info("inject: delivered task to %s (len=%d)", binding, len(text))
    return web.json_response({"ok": True}, status=200)


def build_app(cfg: "InjectConfig") -> web.Application:
    """Construct the aiohttp application. Exposed for tests."""
    app = web.Application()
    app[_CFG_KEY] = cfg
    app.router.add_post("/inject", _handle_inject)
    return app


async def start_server(cfg: "InjectConfig") -> web.AppRunner:
    """Bind the unix-socket listener; return the runner for cleanup.

    Creates the parent dir ``0700`` (so even before the socket is chmod'd
    nothing else can reach into it), removes any stale socket from a
    previous run (else ``UnixSite`` fails to bind), then chmods the live
    socket ``0660``. The process runs as the same local user so owner/group are
    already correct — chmod only narrows the mode bits.
    """
    sock: Path = cfg.socket_path
    sock.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(sock.parent, 0o700)
    try:
        sock.unlink()
    except FileNotFoundError:
        pass

    app = build_app(cfg)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.UnixSite(runner, str(sock))
    await site.start()
    os.chmod(sock, 0o660)
    logger.info(
        "inject server listening on unix socket %s (agents=%s)",
        sock,
        sorted(cfg.allowed_agents),
    )
    return runner
