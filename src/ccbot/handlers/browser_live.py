"""browser_live poller — single shared dashboard for every docker agent.

Every active docker agent exposes two IPC files (bind-mounted from its
container)::

    <ipc_dir>/browser-live.json   — state, page_title, last_update_ts
    <ipc_dir>/current.png         — latest screenshot

Each agent gets exactly one Telegram photo message in the shared
``LIVE_DASHBOARD_THREAD_ID`` topic (within ``NOTIFICATIONS_CHAT_ID``).
The message is created on the first active frame and edited in place
forever — no new sends on idle/wake transitions, no per-binding
duplicates. Posting all live views into one dedicated topic keeps each
message in a stable position; agents' own topics carry only chat traffic.

State machine per agent:

  active=True  + no msg_id    → bot.send_photo, persist msg_id.
  active=True  + have msg_id  → bot.edit_message_media (skipped when ts
                                hasn't advanced since last edit).
  active=False + have msg_id  → bot.edit_message_caption (idle marker;
                                photo is frozen, message_id retained
                                so the next active frame edits in place).

Message_ids persist via ``SessionManager.{get,set,clear}_dashboard_message_id``
so a bot restart resumes editing the existing message instead of leaving
a duplicate behind. The daemon's ``browser-live.json`` contract is
untouched — its ``message_id`` field (if present) is ignored.

Disabled when ``config.live_dashboard_target()`` returns None (either
``NOTIFICATIONS_CHAT_ID`` or ``LIVE_DASHBOARD_THREAD_ID`` unset). The
loop logs a single warning and idles.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from telegram import Bot, InputMediaPhoto
from telegram.error import BadRequest

from ..config import DockerAgentConfig, config
from ..rate_limiter import background_context
from ..session import session_manager

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.5
TITLE_MAX_LEN = 150


@dataclass
class _AgentState:
    """In-memory bookkeeping per agent. message_id lives in SessionManager."""

    last_edit_ts: float = 0.0  # last browser-live.json ts we rendered


_state: dict[str, _AgentState] = {}


def _fmt_hhmm(ts: float) -> str:
    # Uses localtime (the host's local timezone). Using
    # localtime keeps the format aligned with the rest of ccbot's user-facing
    # timestamps without a hard-coded zoneinfo dependency.
    if ts <= 0:
        ts = time.time()
    return time.strftime("%H:%M", time.localtime(ts))


def _truncate(text: str, limit: int = TITLE_MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# Suffixes the daemon's underlying browser appends to every page title.
# Stripped first thing so they don't waste display real-estate.
_TITLE_NOISE_SUFFIXES = (" — Camoufox", " - Camoufox", " | Camoufox")

# Separators commonly used by sites to glue "<headline> | <site name>" or
# "<headline> - <category> - <site name>" into the <title> tag. We split
# at the *first* occurrence and keep the head, since the head is usually
# the page-specific signal (search term, article title) and the tail is
# SEO/site-branding noise.
_TITLE_SPLIT_SEPS = (" | ", " — ", " - ", " · ")


def _smart_title(title: str, limit: int = 60) -> str:
    """Aggressively trim a browser <title> down to the part the user cares about.

    Strips the trailing browser-product suffix (" — Camoufox" etc.) and
    cuts at the first separator (`|`, `—`, ` - `, `·`) when the head is
    substantial, dropping the SEO/site-name tail. Final hard cap applies
    a `…` ellipsis. Empty input returns empty string.
    """
    t = (title or "").strip()
    if not t:
        return ""
    low = t.lower()
    for suffix in _TITLE_NOISE_SUFFIXES:
        if low.endswith(suffix.lower()):
            t = t[: -len(suffix)].rstrip()
            break
    for sep in _TITLE_SPLIT_SEPS:
        idx = t.find(sep)
        # Only split if the head is meaningful (>= 10 chars) — avoids
        # eating useful titles that happen to start with a short prefix
        # like "Re: <subject>".
        if idx >= 10:
            t = t[:idx].rstrip()
            break
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def _domain(url: str) -> str:
    """Extract a clean hostname from a URL for the dashboard caption.

    Returns empty string on parse failure or when ``url`` is empty
    (daemon may write `current_url=""` while idle / pre-browser-start).
    `www.` is stripped because it's pure visual noise — same site.
    """
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _read_live_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _aux_links(agent: DockerAgentConfig) -> str:
    """HTML controls block (Tailscale + VNC tappable labels), when configured.

    Both URLs are HTTPS shorts (via a redirector you configure) that
    302-redirect to bare ``vnc://`` / ``tailscale://``. HTTPS passes
    Telegram's link-validator so the entity survives; the iOS app picks up
    the deep-link and just opens to its address book.

    Returned with a leading blank-line separator so the controls sit
    visually apart from the body rows.

    Tailscale is rendered first because the VNC IP only resolves while
    the VPN is up — matches the tap order the user takes.
    """
    parts: list[str] = []
    ts_url = config.live_dashboard_tailscale_url
    if ts_url:
        parts.append(f'<a href="{html.escape(ts_url, quote=True)}">🔗 Tailscale</a>')
    if agent.vnc_url:
        parts.append(f'<a href="{html.escape(agent.vnc_url, quote=True)}">📺 VNC</a>')
    return "\n\n" + " · ".join(parts) if parts else ""


def _build_caption(
    agent: DockerAgentConfig,
    *,
    active: bool,
    title: str,
    url: str,
    note: str,
    ts: float,
) -> str:
    """Compose the dashboard caption for one agent.

    Layout (HTML, parse_mode="HTML"):

        🤖 <name> · ⚡ active · HH:MM        (idle uses "💤 idle since HH:MM")
        <domain>                              (omitted if URL unknown)
        <title>                               (or note if title empty)

        🔗 Tailscale · 📺 VNC                 (omitted if both URLs unconfigured)

    Status word + emoji is explicit because the icon alone is ambiguous
    at a glance. Time on row 1 is the daemon's `last_update_ts`: for
    active state it's "screenshot freshness", for idle it's the moment
    activity stopped. Absolute "since HH:MM" beats a relative "idle 5m"
    here — relative would go stale because the idle sentinel only allows
    one caption-edit per active→idle transition.

    Domain is shown standalone (small, plain text) as a context anchor:
    "yes the bot is on shopee.co.id". Title gets aggressive smart-trim
    (`_smart_title`) — page titles are mostly SEO chaff after the first
    "headline" segment. Note is the daemon's free-text status (e.g.
    "container started, no browser activity yet") and is rendered only
    when the title is empty, so it never duplicates page content.
    """
    name = html.escape(agent.name)
    body_text = _smart_title(title) or note.strip()
    domain = _domain(url)
    when = _fmt_hhmm(ts) if ts > 0 else _fmt_hhmm(time.time())
    if active:
        header = f"🤖 {name} · ⚡ active · {when}"
    else:
        header = f"🤖 {name} · 💤 idle since {when}"
    rows = [header]
    if domain:
        rows.append(html.escape(domain))
    if body_text:
        rows.append(html.escape(_truncate(body_text)))
    return "\n".join(rows) + _aux_links(agent)


async def _send_fresh(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    agent: DockerAgentConfig,
    png_path: Path,
    caption: str,
) -> int | None:
    """Send a new live-view photo into the dashboard topic; return message_id."""
    try:
        png_bytes = png_path.read_bytes()
        msg = await bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(png_bytes),
            caption=caption,
            parse_mode="HTML",
            message_thread_id=thread_id,
        )
        logger.info(
            "browser_live: created dashboard message %d for agent=%s",
            msg.message_id,
            agent.name,
        )
        return msg.message_id
    except Exception as e:
        logger.warning("browser_live send_photo failed (agent=%s): %s", agent.name, e)
        return None


async def _edit_active(
    bot: Bot,
    chat_id: int,
    agent: DockerAgentConfig,
    message_id: int,
    png_path: Path,
    caption: str,
) -> bool:
    """Swap photo + caption on the dashboard message. False means the message
    is gone and the caller should clear persisted state."""
    try:
        png_bytes = png_path.read_bytes()
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=InputMediaPhoto(
                media=io.BytesIO(png_bytes), caption=caption, parse_mode="HTML"
            ),
        )
        return True
    except BadRequest as e:
        text = str(e).lower()
        if "not modified" in text:
            return True
        if "message to edit not found" in text or "message can't be edited" in text:
            # User deleted the message, or it's older than Telegram's edit
            # window. Drop persisted msg_id; next active tick re-sends.
            return False
        logger.warning(
            "browser_live edit_media BadRequest (agent=%s): %s", agent.name, e
        )
        return True
    except Exception as e:
        logger.warning("browser_live edit_media error (agent=%s): %s", agent.name, e)
        return True


async def _mark_idle(
    bot: Bot,
    chat_id: int,
    agent: DockerAgentConfig,
    message_id: int,
    caption: str,
) -> bool:
    """Edit the caption to the [idle] marker; photo stays frozen.
    Returns False if the message is gone."""
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            parse_mode="HTML",
        )
        return True
    except BadRequest as e:
        text = str(e).lower()
        if "not modified" in text:
            return True
        if "message to edit not found" in text or "message can't be edited" in text:
            return False
        logger.debug("browser_live idle-edit BadRequest (agent=%s): %s", agent.name, e)
        return True
    except Exception as e:
        logger.debug("browser_live idle-edit error (agent=%s): %s", agent.name, e)
        return True


async def _tick_agent(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    agent: DockerAgentConfig,
) -> None:
    live_json = agent.ipc_dir / "browser-live.json"
    data = _read_live_json(live_json)
    if data is None:
        return  # daemon hasn't written yet, or agent down

    active = bool(data.get("active"))
    title = str(data.get("page_title") or "")
    url = str(data.get("current_url") or "")
    note = str(data.get("note") or "")
    ts = float(data.get("last_update_ts") or 0.0)
    # Pin the screenshot path rather than trusting the JSON's current_png
    # field. A compromised daemon could otherwise point at e.g.
    # ``../workspace/.credentials.json`` and we'd upload host bytes to
    # Telegram. Daemon contract is a single well-known filename.
    png_path = agent.ipc_dir / "current.png"

    agent_state = _state.setdefault(agent.name, _AgentState())
    message_id = session_manager.get_dashboard_message_id(agent.name)
    caption = _build_caption(
        agent, active=active, title=title, url=url, note=note, ts=ts
    )

    if active:
        if not png_path.exists():
            return  # daemon flipped active=true but PNG isn't written yet
        if message_id is None:
            new_id = await _send_fresh(
                bot, chat_id, thread_id, agent, png_path, caption
            )
            if new_id is not None:
                session_manager.set_dashboard_message_id(agent.name, new_id)
                agent_state.last_edit_ts = ts
            return
        if ts <= agent_state.last_edit_ts:
            return  # daemon hasn't pushed a new frame since our last edit
        alive = await _edit_active(bot, chat_id, agent, message_id, png_path, caption)
        if alive:
            agent_state.last_edit_ts = ts
        else:
            session_manager.clear_dashboard_message_id(agent.name)
            agent_state.last_edit_ts = 0.0
    else:
        if message_id is None:
            return  # never had a message; nothing to mark idle
        if agent_state.last_edit_ts < 0:
            return  # already finalized (sentinel below)
        alive = await _mark_idle(bot, chat_id, agent, message_id, caption)
        if not alive:
            session_manager.clear_dashboard_message_id(agent.name)
            agent_state.last_edit_ts = 0.0
            return
        # Sentinel: we've stamped [idle] for this agent's current message.
        # Keep message_id (the daemon may flip back to active and we want
        # to edit in place), but stop spamming caption edits every poll.
        agent_state.last_edit_ts = -1.0


async def browser_live_loop(bot: Bot) -> None:
    """Background task: keep the live-dashboard topic in sync.

    Idle when the dashboard is unconfigured or no docker agents are active.
    """
    target = config.live_dashboard_target()
    if target is None:
        logger.warning(
            "browser_live: dashboard disabled "
            "(set NOTIFICATIONS_CHAT_ID and LIVE_DASHBOARD_THREAD_ID to enable)"
        )
        return
    chat_id, thread_id = target

    logger.info(
        "browser_live loop started (chat=%d, thread=%d, interval=%ss)",
        chat_id,
        thread_id,
        POLL_INTERVAL_S,
    )
    while True:
        try:
            for agent in config.active_docker_agents():
                try:
                    # background_context: photo edits every ~1.5s are the
                    # textbook bursty-background class — keep them off the
                    # interactive bypass lane (a 429 there would arm the
                    # global RetryAfter pause for every send).
                    with background_context():
                        await _tick_agent(bot, chat_id, thread_id, agent)
                except Exception as e:
                    logger.warning(
                        "browser_live tick error (agent=%s): %s", agent.name, e
                    )
        except Exception as e:
            logger.exception("browser_live loop error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_S)
