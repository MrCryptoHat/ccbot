"""Live-topic dashboard — one self-updating "what's up right now" message.

A single Telegram message in the Live-topic (the same
``LIVE_DASHBOARD_THREAD_ID`` topic ``browser_live`` posts agent
screenshots into) that lists everything currently reachable, so the user
can glance at it and tap through:

  🌐 Preview-серверы — ephemeral per-agent dev servers from
     ``~/.local/state/preview/registry.json``. For each: slug,
     ``https://preview-<slug>.<domain>``, remaining TTL, health
     (tmux session ``preview-<slug>`` alive **and** the port listening).
     Same source the ``/status`` command's Preview section uses.

  🔗 Постоянные app-хосты — permanent Caddy-fronted apps from
     ``/etc/caddy/apps.d/*.caddy`` (fixed shape: ``http://<host>:8080 {
     reverse_proxy 127.0.0.1:<port> }``). For each: ``https://<host>``,
     backend ``:<port>``, health (the backend port listening). Caddy's
     ``preview.d/`` and ``redirects.d/`` are *not* parsed — previews are
     covered above, redirects aren't "services".

The message is created once and edited in place forever; its id is
persisted in ``state.json`` (reserved key in
``SessionManager.live_dashboard_message_ids`` — ``browser_live`` only
iterates real agent names, so it never touches it). The header carries
the time of the **last content change**, not "now": the loop caches the
rendered body and skips the edit when nothing moved, so the timestamp
reads "this state has held since HH:MM" and we don't churn one edit per
poll. Refresh is every ``REFRESH_INTERVAL`` seconds (no push hook from
``preview up/down`` — that's a separate CLI — so a freshly-started
preview shows up within one poll).

Disabled when ``config.live_dashboard_target()`` returns None (either
``NOTIFICATIONS_CHAT_ID`` or ``LIVE_DASHBOARD_THREAD_ID`` unset) — the
loop logs one warning and idles.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import subprocess
import time

from telegram import Bot
from telegram.error import BadRequest

from .. import preview
from ..config import config
from ..i18n import tr
from ..rate_limiter import background_context
from ..session import session_manager

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = 45.0
# Caddy app-host config dir (CCBOT_CADDY_APPS_DIR; defaults to this server's
# /etc/caddy/apps.d). Missing dir → _scan_app_hosts returns [] (graceful).
CADDY_APPS_DIR = config.caddy_apps_dir
# Reserved key in SessionManager.live_dashboard_message_ids — distinct from
# any docker-agent name (those come from ~/agents/<dir> names, never start
# with an underscore), so browser_live's per-agent iteration ignores it.
_BOARD_MSG_KEY = "__live_board__"

# Caddy app-host blocks look like:  http://app.example.com:8080 { reverse_proxy 127.0.0.1:3000 }
_CADDY_HOST_RE = re.compile(r"http://(\S+):8080\s*\{")
_CADDY_BACKEND_RE = re.compile(r"reverse_proxy\s+\S*?:(\d+)")


def _fmt_hhmm(ts: float | None = None) -> str:
    # Uses localtime (the host's local timezone).
    return time.strftime("%H:%M", time.localtime(ts if ts is not None else time.time()))


def _scan_previews() -> list[tuple[bool, str]]:
    """Return ``[(healthy, "slug · https://preview-slug.<domain> · TTL"), …]``."""
    try:
        with open(preview.REGISTRY_PATH) as f:
            registry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(registry, dict):
        return []
    rows: list[tuple[bool, str]] = []
    for slug, entry in sorted(registry.items()):
        if not isinstance(entry, dict):
            continue
        port = entry.get("port", 0)
        ttl = str(entry.get("ttl", "?"))
        started = str(entry.get("started", ""))
        try:
            tmux_alive = (
                subprocess.run(
                    ["tmux", "has-session", "-t", f"preview-{slug}"],
                    capture_output=True,
                    timeout=2,
                ).returncode
                == 0
            )
        except Exception:
            tmux_alive = False
        healthy = tmux_alive and preview.port_listening(port)
        remaining = preview.ttl_remaining(started, ttl)
        safe_slug = html.escape(str(slug))
        rows.append(
            (
                healthy,
                f"{safe_slug} · https://{config.preview_host(safe_slug)} · {remaining}",
            )
        )
    return rows


def _scan_app_hosts() -> list[tuple[bool, str]]:
    """Return ``[(healthy, "https://host · :port"), …]`` from caddy apps.d."""
    if not CADDY_APPS_DIR.is_dir():
        return []
    rows: list[tuple[bool, str]] = []
    for caddyfile in sorted(CADDY_APPS_DIR.glob("*.caddy")):
        try:
            text = caddyfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        host_m = _CADDY_HOST_RE.search(text)
        port_m = _CADDY_BACKEND_RE.search(text)
        if not host_m:
            continue
        host = host_m.group(1)
        port = int(port_m.group(1)) if port_m else 0
        healthy = bool(port) and preview.port_listening(port)
        safe_host = html.escape(host)
        backend = f":{port}" if port else "?"
        rows.append((healthy, f"https://{safe_host} · {backend}"))
    return rows


def _render_body() -> str:
    """Build the dashboard body (without the timestamp header line)."""
    previews = _scan_previews()
    apps = _scan_app_hosts()

    def _section(title: str, rows: list[tuple[bool, str]]) -> list[str]:
        if not rows:
            return []
        out = [f"<b>{title}</b>"]
        for healthy, line in rows:
            out.append(f"  {'🟢' if healthy else '🔴'} {line}")
        return out

    blocks: list[list[str]] = []
    pv = _section(tr("lboard.section_previews"), previews)
    ap = _section(tr("lboard.section_apps"), apps)
    if pv:
        blocks.append(pv)
    if ap:
        blocks.append(ap)
    if not blocks:
        return tr("lboard.nothing_up")
    return "\n\n".join("\n".join(b) for b in blocks)


def _compose(body: str, *, stamped_at: float) -> str:
    return tr("lboard.header", time=_fmt_hhmm(stamped_at), body=body)


async def live_board_loop(bot: Bot) -> None:
    """Background task: keep one Live-topic message in sync with reality.

    Runs forever until cancelled. Exits immediately with a warning if the
    Live-topic isn't configured.
    """
    target = config.live_dashboard_target()
    if target is None:
        logger.warning(
            "live_board: NOTIFICATIONS_CHAT_ID / LIVE_DASHBOARD_THREAD_ID unset "
            "— live dashboard disabled"
        )
        return
    chat_id, thread_id = target
    logger.info(
        "live_board: dashboard active (chat=%d, thread=%d, interval=%.0fs)",
        chat_id,
        thread_id,
        REFRESH_INTERVAL,
    )

    last_body: str | None = None
    stamped_at = time.time()

    while True:
        try:
            # background_context: dashboard refreshes are not user-waiting
            # and must not ride the interactive bypass lane — a 429 there
            # would arm the global RetryAfter pause for every send.
            with background_context():
                body = await asyncio.to_thread(_render_body)
                if body != last_body:
                    stamped_at = time.time()
                    last_body = body
                text = _compose(body, stamped_at=stamped_at)

                msg_id = session_manager.live_dashboard_message_ids.get(_BOARD_MSG_KEY)
                if msg_id is None:
                    msg = await bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    session_manager.set_dashboard_message_id(
                        _BOARD_MSG_KEY, msg.message_id
                    )
                else:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except BadRequest as e:
                        detail = str(e).lower()
                        if "not modified" in detail:
                            pass  # someone/we already set this exact text
                        elif "not found" in detail or "can't be edited" in detail:
                            # Message vanished (topic cleared, too old) — drop the
                            # id so the next tick sends a fresh one.
                            session_manager.clear_dashboard_message_id(_BOARD_MSG_KEY)
                            logger.info(
                                "live_board: message gone (%s) — will resend", e
                            )
                        else:
                            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("live_board: loop iteration failed: %s", e)

        await asyncio.sleep(REFRESH_INTERVAL)
