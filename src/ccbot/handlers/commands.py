"""Command handlers — /start, /history, /screenshot, /esc, /unbind, /status, /restart, /voice.

Also handles topic lifecycle events (close, edit, rename) and
forwarding unknown /commands to Claude Code via tmux.
"""

import asyncio
import io
import logging
import os
import re
import subprocess
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, KeyboardButtonStyle
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from typing import Literal

from . import get_thread_id, is_user_allowed, pane_cache
from .callback_data import (
    CB_CMD_CANCEL,
    CB_CMD_CLEAR,
    CB_CMD_COMPACT,
    CB_CMD_CONTEXT,
    CB_CMD_EFFORT,
    CB_CMD_FRESH,
    CB_CMD_KILL,
    CB_CMD_MCP,
    CB_CMD_MODE_CYCLE,
    CB_CMD_MODEL,
    CB_CMD_REFRESH,
    CB_CMD_RESTART,
    CB_CMD_RESUME,
    CB_CMD_TAB,
    CB_CMD_WIPE_INPUT,
    CB_KEYS_PREFIX,
    CB_STATUS_REFRESH,
    CB_WT_DEL,
    CB_WT_NEW,
)
from .cleanup import clear_topic_state
from .directory_browser import (
    SESSIONS_KEY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    build_session_picker,
    clear_browse_state,
)
from .message_sender import PARSE_MODE, safe_reply
from ..screenshot import text_to_image
from ..config import config
from ..docker_driver import docker_driver
from ..hook import hook_installed_in_settings
from .. import i18n, plugins
from ..i18n import tr
from ..session import session_manager
from ..terminal_parser import is_claude_working
from ..tmux_manager import tmux_manager
from ..transcript_parser import TranscriptParser
from ..utils import is_valid_session_id
from ..voice import providers as voice_providers

logger = logging.getLogger(__name__)

# Whitelist for the /status "User services" block: only these systemd --user
# units are shown (everything else — dbus, pipewire, … — is noise). Host-
# specific, so it comes from env (CCBOT_STATUS_SERVICES="a,b,c"); empty = the
# section is omitted.
STATUS_USER_SERVICES_WHITELIST = {
    s.strip() for s in os.getenv("CCBOT_STATUS_SERVICES", "").split(",") if s.strip()
}

# Canonical weekday index for cron dow tokens: Monday=0 … Sunday=6. The
# rendered label comes from the i18n catalog (commands.cron_wd_<i>) at
# format time, so /status weekday groups follow the active UI language.
_CRON_DOW_CANON = {
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "0": 6,
    "7": 6,
}


def _cron_parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("@"):
        parts = line.split(None, 1)
        if len(parts) != 2:
            return None
        return parts[0], parts[1]
    parts = line.split(None, 5)
    if len(parts) != 6:
        return None
    return " ".join(parts[:5]), parts[5]


def _cron_script_name(command: str) -> str:
    cmd = re.sub(r"\s*2>&1", "", command)
    cmd = re.sub(r"\s*>>?\s*\S+", "", cmd)
    # Skip prefixes like "sleep 45 &&" — take the last chained segment
    cmd = cmd.split("&&")[-1].strip()
    m = re.match(r"(\S+?/\S+)(?:\s+(\S+))?", cmd)
    if not m:
        return cmd[:30] or "?"
    script = os.path.basename(m.group(1))
    arg = m.group(2)
    if arg and not arg.startswith(("-", '"', "'", ">")):
        return f"{script} {arg}"
    return script


def _cron_parse_crontab(text: str) -> list[tuple[str, str, str | None]]:
    """Parse crontab text, attaching `# desc: ...` comments to the next entry.

    Returns list of (schedule_raw, command, description_or_None). A blank
    line between a desc comment and an entry clears the pending desc.
    """
    entries: list[tuple[str, str, str | None]] = []
    pending_desc: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            pending_desc = None
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            low = body.lower()
            if low.startswith("desc:"):
                pending_desc = body[5:].strip()
            elif low.startswith("description:"):
                pending_desc = body[12:].strip()
            continue
        parsed = _cron_parse_line(line)
        if parsed:
            schedule, command = parsed
            entries.append((schedule, command, pending_desc))
            pending_desc = None
    return entries


def _tree_list(items: list[str], indent: str = " ") -> list[str]:
    """Render items as ├-prefixed lines (last one └). Returns empty list for empty input."""
    n = len(items)
    return [f"{indent}{'└' if i == n - 1 else '├'} {it}" for i, it in enumerate(items)]


# Cron classification — group/sort_key/time_label/desc/weekday_index.
# Groups: "daily" (fires at fixed time(s) every day), "weekly" (specific
# weekday — grouped further, index Monday=0…Sunday=6), "interval" (every N
# min/hour regardless of date), "boot" (@reboot). Fallback is "interval"
# with the raw expression as the label so we don't silently lose
# unparseable entries.
def _classify_cron(
    schedule_raw: str, command: str, desc: str | None
) -> tuple[str, tuple, str, str, int | None]:
    label_desc = desc if desc else _cron_script_name(command)
    if schedule_raw.startswith("@reboot"):
        return ("boot", (0,), "", label_desc, None)
    parts = schedule_raw.split()
    if len(parts) != 5:
        return ("interval", (10**9,), schedule_raw, label_desc, None)
    m, h, dom, mon, dow = parts
    all_dates_stars = dom == "*" and mon == "*" and dow == "*"

    # Interval — every N minutes within any hour, every day.
    if m.startswith("*/") and h == "*" and all_dates_stars:
        try:
            n = int(m[2:])
        except ValueError:
            return ("interval", (10**9,), schedule_raw, label_desc, None)
        return ("interval", (n,), tr("commands.cron_label_min", n=n), label_desc, None)

    # Interval — specific minute, every hour. Label as "1h (:MM)".
    if m.isdigit() and h == "*" and all_dates_stars:
        return (
            "interval",
            (60, int(m)),
            tr("commands.cron_label_hourly", mm=m.zfill(2)),
            label_desc,
            None,
        )

    # Daily multi-time — specific minute, hour=*/N. Enumerate first two
    # fire-times then "…" plus interval annotation, so users see *when*
    # without scrolling through 12 entries.
    if m.isdigit() and h.startswith("*/") and all_dates_stars:
        try:
            step = int(h[2:])
        except ValueError:
            return ("interval", (10**9,), schedule_raw, label_desc, None)
        if step < 1:
            return ("interval", (10**9,), schedule_raw, label_desc, None)
        mm = int(m)
        times = [f"{hh:02d}:{mm:02d}" for hh in range(0, 24, step)]
        if len(times) == 1:
            label = times[0]
        elif len(times) == 2:
            label = ",".join(times)
        else:
            label = tr("commands.cron_label_every_h", t1=times[0], t2=times[1], n=step)
        first_h = int(times[0].split(":")[0])
        return ("daily", (first_h, mm), label, label_desc, None)

    # Daily single-time.
    if m.isdigit() and h.isdigit() and all_dates_stars:
        return (
            "daily",
            (int(h), int(m)),
            f"{h.zfill(2)}:{m.zfill(2)}",
            label_desc,
            None,
        )

    # Weekly — specific dow.
    if (
        m.isdigit()
        and h.isdigit()
        and dom == "*"
        and mon == "*"
        and dow in _CRON_DOW_CANON
    ):
        return (
            "weekly",
            (int(h), int(m)),
            f"{h.zfill(2)}:{m.zfill(2)}",
            label_desc,
            _CRON_DOW_CANON[dow],
        )

    # Anything else — keep visible but in interval group with raw expr.
    return ("interval", (10**9, schedule_raw), schedule_raw, label_desc, None)


def _format_cron_groups(
    entries: list[tuple[str, str, str | None]],
) -> list[str]:
    """Render cron entries as grouped subsections. Returns lines without the
    parent section header, ready to drop inside the expandable blockquote."""
    classified = [_classify_cron(s, c, d) for (s, c, d) in entries]

    daily: list[tuple[tuple, str, str]] = []
    weekly_by_wd: dict[int, list[tuple[tuple, str, str]]] = {}
    interval: list[tuple[tuple, str, str]] = []
    boot: list[str] = []
    for g, k, lbl, desc, wd in classified:
        if g == "daily":
            daily.append((k, lbl, desc))
        elif g == "weekly" and wd is not None:
            weekly_by_wd.setdefault(wd, []).append((k, lbl, desc))
        elif g == "interval":
            interval.append((k, lbl, desc))
        elif g == "boot":
            boot.append(desc)

    daily.sort(key=lambda t: t[0])
    interval.sort(key=lambda t: t[0])
    for items in weekly_by_wd.values():
        items.sort(key=lambda t: t[0])

    out: list[str] = []
    if daily:
        out.append(tr("commands.cron_daily"))
        out.extend(_tree_list([f"{lbl} — {desc}" for (_, lbl, desc) in daily], "  "))
    for wd in range(7):
        if wd in weekly_by_wd:
            out.append(tr("commands.cron_weekly", wd=tr(f"commands.cron_wd_{wd}")))
            out.extend(
                _tree_list(
                    [f"{lbl} — {desc}" for (_, lbl, desc) in weekly_by_wd[wd]],
                    "  ",
                )
            )
    if interval:
        out.append(tr("commands.cron_interval"))
        out.extend(_tree_list([f"{lbl} — {desc}" for (_, lbl, desc) in interval], "  "))
    if boot:
        out.append(tr("commands.cron_boot"))
        out.extend(_tree_list(boot, "  "))
    return out


def menu_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent menu ReplyKeyboard in the active UI language.

    A function (not a module constant) so it picks up the current language
    on every send — labels switch the moment /lang flips i18n. One row —
    compact, everything under the thumb. «👾 Agent» opens the panel with
    Nav/Actions tabs inside. /voice, /lang etc. stay slash-only (too rare
    for the main keyboard).
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton(tr("menu.server")), KeyboardButton(tr("menu.agent"))]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A real /help — the first thing a new user asks the bot.

    Registered BEFORE the forward-everything-else command handler: without
    it /help would be typed into the agent's terminal (bound topic) or
    answered with a useless "no session" error (unbound/private chat).
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(
                update.message,
                tr("common.not_authorized", uid=user.id if user else "?"),
            )
        return
    if not update.message:
        return
    await safe_reply(update.message, tr("bot.help"))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(
                update.message,
                tr("common.not_authorized", uid=user.id if user else "?"),
            )
        return

    clear_browse_state(context.user_data)

    if update.message:
        # In a DM the "create a topic" welcome is impossible advice — a
        # friend's first tap after BotFather lands here, so walk them
        # through the group setup instead.
        chat = update.effective_chat
        in_private = chat is not None and chat.type == "private"
        text = tr("bot.private_start") if in_private else tr("bot.start_welcome")
        if chat is not None and chat.type in ("group", "supergroup"):
            # /start is the one signal that reaches the bot even without admin
            # rights (privacy mode blocks plain group messages) — so it's the
            # only place the two group-setup traps can be detected and
            # explained instead of greeting into silence.
            try:
                me = await chat.get_member(context.bot.id)
                is_admin = me.status in ("administrator", "creator")
            except Exception:
                is_admin = True  # can't verify — don't nag on a false alarm
            if not is_admin:
                text = tr("bot.make_me_admin")
            elif not getattr(chat, "is_forum", False):
                text = tr("bot.enable_topics_hint")
        await safe_reply(update.message, text, reply_markup=menu_keyboard())
        thread_id = get_thread_id(update)
        if thread_id is not None:
            session_manager.mark_menu_shown(user.id, thread_id)


async def bind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bind the current topic to a docker agent: ``/bind <agent-name>``.

    Tmux agents have the directory-browser flow for binding; docker
    agents don't have a "directory" — they're long-lived containers —
    so this is the deliberate entry point. Adds ``docker:<name>`` to
    thread_bindings and names the topic after the agent.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, tr("common.topics_only"))
        return

    args = (context.args or []) if context else []
    if not args:
        await safe_reply(
            update.message,
            tr("commands.bind_usage"),
        )
        return

    agent_name = args[0].strip()
    if not config.docker_agents_enabled:
        await safe_reply(update.message, tr("commands.docker_disabled"))
        return
    agent = config.get_docker_agent(agent_name)
    if not agent:
        available = ", ".join(a.name for a in config.docker_agents) or tr(
            "commands.none_paren"
        )
        await safe_reply(
            update.message,
            tr("commands.agent_not_configured", name=agent_name, available=available),
        )
        return

    # agent.name, not the user's spelling: get_docker_agent matches
    # case-insensitively ("Assistant" from a phone keyboard), but every
    # session_map key and route is the canonical lowercase binding value —
    # a user-cased binding would silently break inbound delivery.
    session_manager.bind_thread(
        user.id,
        thread_id,
        f"docker:{agent.name}",
        window_name=agent.name,
    )
    # Remember the group chat_id so outbound messages land in this topic.
    if update.effective_chat and update.effective_chat.id != user.id:
        session_manager.set_group_chat_id(user.id, thread_id, update.effective_chat.id)
    await safe_reply(
        update.message,
        tr("commands.bind_done", name=agent.name),
        reply_markup=menu_keyboard(),
    )
    session_manager.mark_menu_shown(user.id, thread_id)


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle voice mode (TTS) for the current topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, tr("common.topics_only"))
        return

    if voice_providers.get_active_provider() is None:
        await safe_reply(
            update.message,
            tr("commands.voice_no_provider"),
        )
        return

    enabled = session_manager.toggle_voice_mode(user.id, thread_id)
    if enabled:
        await safe_reply(update.message, tr("commands.voice_on"))
    else:
        await safe_reply(update.message, tr("commands.voice_off"))


async def react_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle reaction-ack: bot marks an ingested message with 👀 (global)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    enabled = session_manager.toggle_reaction_ack()
    if enabled:
        await safe_reply(
            update.message,
            tr("commands.react_on"),
        )
    else:
        await safe_reply(update.message, tr("commands.react_off"))


def build_bot_commands() -> list[BotCommand]:
    """The /command menu, described in the active UI language.

    Rebuilt (not cached) so /lang can re-publish it in the new language —
    keep this in sync with the CommandHandler registrations in bot.py.
    """
    # /screenshot (alias of /commands) still works but is deliberately NOT
    # published — an alias row is noise in the menu new users read as a
    # feature map.
    return [
        BotCommand("help", tr("cmd.help")),
        BotCommand("start", tr("cmd.start")),
        BotCommand("status", tr("cmd.status")),
        BotCommand("commands", tr("cmd.commands")),
        BotCommand("restart", tr("cmd.restart")),
        BotCommand("esc", tr("cmd.esc")),
        BotCommand("kill", tr("cmd.kill")),
        BotCommand("voice", tr("cmd.voice")),
        BotCommand("react", tr("cmd.react")),
        BotCommand("diff", tr("cmd.diff")),
        BotCommand("pin", tr("cmd.pin")),
        BotCommand("lang", tr("cmd.lang")),
        BotCommand("menu", tr("cmd.menu")),
        *plugins.bot_commands(),
    ]


async def apply_bot_commands(bot: Bot) -> None:
    """Publish the localized /command menu to Telegram (default + group scopes)."""
    plugins.register_i18n()
    cmds = build_bot_commands()
    await bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch ccbot's UI language. `/lang ru` / `/lang en` set explicitly;
    bare `/lang` toggles ru↔en. Re-publishes the localized command menu and
    re-pins the menu keyboard so its labels relabel immediately."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    arg = context.args[0].strip().lower() if context.args else ""
    if arg in i18n.LANGUAGES:
        session_manager.set_ui_language(arg)
    else:
        session_manager.toggle_ui_language()

    await apply_bot_commands(update.get_bot())
    await safe_reply(update.message, tr("lang.changed"), reply_markup=menu_keyboard())


async def diff_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle diff-screenshot mode for the current topic (/diff)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, tr("common.topics_only"))
        return

    from .diff_view import prime, reset

    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    enabled = session_manager.toggle_diff_mode(user.id, thread_id)
    if enabled:
        # Mark diffs already on screen as seen, so turning /diff on doesn't
        # dump the existing backlog — only edits from here on get sent.
        if wid:
            await prime(wid)
        await safe_reply(
            update.message,
            tr("commands.diff_on"),
        )
    else:
        if wid:
            reset(wid)
        await safe_reply(update.message, tr("commands.diff_off"))


async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle task-pin mode for the current topic (/pin).

    When on, a long user message sent to an idle agent (a new task, not a
    mid-turn follow-up) gets pinned in the topic — the pinned list becomes
    the topic's task history. Logic in handlers/task_pin.py.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, tr("common.topics_only"))
        return

    enabled = session_manager.toggle_pin_mode(user.id, thread_id)
    if enabled:
        await safe_reply(
            update.message,
            tr("commands.pin_on", n=config.pin_tasks_min_chars),
        )
    else:
        await safe_reply(update.message, tr("commands.pin_off"))


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Backward-compat alias for /commands — opens the unified agent panel.

    Historically `/screenshot` sent a bare photo with only nav keys
    attached. The new panel folds both flows (nav keys + agent actions)
    into one screen with tabs, so the two slash commands now resolve to
    the same UI.
    """
    await commands_command(update, context)


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape + Ctrl+C to interrupt Claude in any state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, tr("commands.no_session_in_topic"))
        return

    if session_manager._is_docker_binding(wid):
        agent = config.get_docker_agent(wid[len("docker:") :])
        if not agent or not await docker_driver.is_container_alive(agent.container):
            await safe_reply(update.message, tr("commands.container_not_running"))
            return
        await docker_driver.send_keys(
            agent.container, "Escape", enter=False, literal=False
        )
        await asyncio.sleep(0.1)
        await docker_driver.send_keys(
            agent.container, "C-c", enter=False, literal=False
        )
    else:
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            await safe_reply(
                update.message, tr("commands.window_not_exist", name=display)
            )
            return
        # Escape — прерывает генерацию Claude Code
        await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)
        await asyncio.sleep(0.1)
        # Ctrl+C — прерывает Bash-команды
        await tmux_manager.send_keys(w.window_id, "C-c", enter=False, literal=False)

    await safe_reply(update.message, tr("commands.interrupted"))


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill the agent for this topic — tmux window or container's tmux session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, tr("commands.only_in_topic"))
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, tr("commands.no_session_in_topic"))
        return

    display = session_manager.get_display_name(wid)
    if session_manager._is_docker_binding(wid):
        # For docker we kill the tmux session inside the container (Claude
        # dies with it). The container itself stays up — you'd use
        # `docker compose stop` for that. /restart re-spawns the session.
        agent = config.get_docker_agent(wid[len("docker:") :])
        if agent and await docker_driver.is_container_alive(agent.container):
            await docker_driver.kill_session(agent.container)
            logger.info(
                "Kill command: killed tmux session in %s (user=%d, thread=%d)",
                agent.container,
                user.id,
                thread_id,
            )
    else:
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Kill command: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
    session_manager.unbind_thread(user.id, thread_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    await safe_reply(update.message, tr("commands.agent_killed", name=display))


def _progress_bar(pct: float, width: int = 10) -> str:
    """Render a Unicode progress bar of N segments for a 0-100% value."""
    filled = round(min(max(pct, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _threshold_emoji(pct: float) -> str:
    """Traffic-light emoji for a percent value (≤80 ok, ≤95 warn, else crit)."""
    if pct >= 95:
        return "🔴"
    if pct >= 80:
        return "🟡"
    return "🟢"


def _read_uptime_human() -> str | None:
    """Read /proc/uptime and format as '12 дн.' / '5 ч.' / '42 мин.'."""
    try:
        with open("/proc/uptime") as f:
            seconds = float(f.read().split()[0])
    except Exception:
        return None
    days = int(seconds // 86400)
    if days >= 1:
        return tr("commands.uptime_days", n=days)
    hours = int(seconds // 3600)
    if hours >= 1:
        return tr("commands.uptime_hours", n=hours)
    mins = int(seconds // 60)
    return tr("commands.uptime_mins", n=mins)


async def _build_status_text() -> str:
    """Compose the full /status message body. Pure string output, no send.

    The heavy lifting happens in _build_status_text_sync inside
    asyncio.to_thread: the body runs several subprocess calls (and plugin
    sections may probe mounts/filesystems) — doing that on the event loop
    froze every topic for seconds, and a hung filesystem froze the whole
    bot.
    """
    try:
        windows = await tmux_manager.list_windows()
    except Exception:
        windows = []
    return await asyncio.to_thread(_build_status_text_sync, windows)


def _build_status_text_sync(windows: list) -> str:
    """Blocking body of /status — must be called via asyncio.to_thread."""
    warnings: list[str] = []
    sections: list[str] = []

    # --- Docker container snapshot (consumed by Agents + Docker sections) -
    # Isolated docker-agents (config.active_docker_agents()) are
    # rendered in the Агенты section, not in Docker — they're agents
    # conceptually, just sandboxed. Pull `docker ps` once and feed both
    # sections from the same map so we don't hit the docker socket twice.
    docker_status: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                if not line:
                    continue
                name, _, st = line.partition("\t")
                docker_status[name] = st or "?"
    except Exception:
        pass
    docker_agent_containers: set[str] = {
        a.container for a in config.active_docker_agents()
    }

    def _docker_is_up(st: str) -> bool:
        return "Up" in st or "healthy" in st

    # --- Agents -----------------------------------------------------------
    alive_agents: list[str] = []
    dead_agents: list[str] = []
    for w in windows:
        if w.window_name == "__main__":
            continue
        cmd = w.pane_current_command or "?"
        if cmd in ("claude", "node"):
            alive_agents.append(w.window_name)
        else:
            dead_agents.append(w.window_name)

    # Isolated docker-agents — Up/healthy → alive, otherwise (missing from
    # `docker ps` or status not "Up") → dead with a warning. Container
    # name and agent name may differ (DockerAgentConfig.container defaults
    # to .name but can be overridden), so we lookup by container.
    alive_docker_agents: list[str] = []
    dead_docker_agents: list[tuple[str, str]] = []
    for agent in config.active_docker_agents():
        st = docker_status.get(agent.container)
        if st and _docker_is_up(st):
            alive_docker_agents.append(agent.name)
        else:
            dead_docker_agents.append(
                (agent.name, st or tr("commands.status_not_started"))
            )
            warnings.append(f"агент {agent.name}")

    # Sort alive and dead independently — admin's spec is "живые α-сорт,
    # потом мёртвые α-сорт". Both groups mix tmux and docker entries; the
    # ` · 🐳` suffix tells them apart visually.
    alive_combined: list[tuple[str, bool]] = sorted(
        [(n, False) for n in alive_agents] + [(n, True) for n in alive_docker_agents],
        key=lambda p: p[0],
    )
    dead_combined: list[tuple[str, bool, str]] = sorted(
        [(n, False, tr("commands.status_stopped")) for n in dead_agents]
        + [(n, True, st) for (n, st) in dead_docker_agents],
        key=lambda p: p[0],
    )
    total_agents = len(alive_combined) + len(dead_combined)
    if total_agents > 0:
        alive_items = [
            f"{n} · 🐳" if is_docker else n for (n, is_docker) in alive_combined
        ]
        dead_items = [
            f"🔴 {n} · 🐳 — {st}" if is_docker else f"🔴 {n} ({st})"
            for (n, is_docker, st) in dead_combined
        ]
        agent_lines = [
            tr(
                "commands.status_agents",
                alive=len(alive_combined),
                total=total_agents,
            )
        ]
        agent_lines.extend(_tree_list(alive_items + dead_items))
        sections.append("\n".join(agent_lines))

    # --- Docker -----------------------------------------------------------
    # Plain containers only — isolated agents are rendered above.
    docker_ok: list[str] = []
    docker_bad: list[tuple[str, str]] = []
    for name, st in docker_status.items():
        if name in docker_agent_containers:
            continue
        if _docker_is_up(st):
            docker_ok.append(name)
        else:
            docker_bad.append((name, st))
            warnings.append(f"docker {name}")
    total = len(docker_ok) + len(docker_bad)
    if total > 0:
        items = sorted(docker_ok) + [
            f"🔴 {n} — {st}" for (n, st) in sorted(docker_bad, key=lambda p: p[0])
        ]
        lines_d = [f"🐳 Docker · {len(docker_ok)}/{total}"]
        lines_d.extend(_tree_list(items))
        sections.append("\n".join(lines_d))

    # --- Systemd user services (whitelist) -------------------------------
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--state=running",
                "--no-legend",
                "--plain",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        services: list[str] = []
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                name = line.split()[0]
                if name.endswith(".service"):
                    name = name[: -len(".service")]
                if name in STATUS_USER_SERVICES_WHITELIST:
                    services.append(name)
        if services:
            svc_lines = [tr("commands.status_background", n=len(services))]
            svc_lines.extend(_tree_list(services))
            sections.append("\n".join(svc_lines))
    except Exception:
        pass

    # --- Resources (disk + RAM with bars) --------------------------------
    # Label widths chosen to keep the bar column aligned across both rows
    # ("Диск" is 4 chars cyr, "RAM" is 3 chars latin) — padding to 5 in
    # Python str width gets us close enough in Telegram's body font.
    res_lines = [tr("commands.status_resources")]
    try:
        result = subprocess.run(
            ["df", "-h", "/"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        df_line = result.stdout.strip().splitlines()[-1].split()
        total_d, used_d, pct_str = df_line[1], df_line[2], df_line[4]
        pct = float(pct_str.rstrip("%"))
        emoji = _threshold_emoji(pct)
        if pct >= 80:
            warnings.append(f"диск {int(pct)}%")
        res_lines.append(
            f" {emoji} {tr('commands.status_disk'):<6}`{_progress_bar(pct)}`  {int(pct):>3}%   "
            f"{used_d} / {total_d}"
        )
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["free", "-b"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        mem_line = result.stdout.strip().splitlines()[1].split()
        total_b = int(mem_line[1])
        used_b = int(mem_line[2])
        pct = used_b / total_b * 100 if total_b else 0
        emoji = _threshold_emoji(pct)
        if pct >= 80:
            warnings.append(f"память {int(pct)}%")
        used_gb = used_b / (1024**3)
        total_gb = total_b / (1024**3)
        res_lines.append(
            f" {emoji} {'RAM':<6}`{_progress_bar(pct)}`  {int(pct):>3}%   "
            f"{used_gb:.1f}G / {total_gb:.1f}G"
        )
    except Exception:
        pass
    if len(res_lines) > 1:
        sections.append("\n".join(res_lines))

    # --- Mounts -----------------------------------------------------------
    # Vertical list under the header, same shape as agents/docker — user
    # prefers a consistent visual rhythm across sections.

    # --- Plugin sections (mounts, preview fleets, …) ----------------------
    # Host-specific integrations contribute their own blocks + warnings here
    # (see plugins.status_sections). We're already on the worker thread.
    plugin_sections, plugin_warnings = plugins.status_sections()
    sections.extend(plugin_sections)
    warnings.extend(plugin_warnings)

    # --- Cron schedule (expandable blockquote for detail) ----------------
    try:
        crontab = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        entries: list[tuple[str, str, str | None]] = []
        if crontab.returncode == 0:
            entries = _cron_parse_crontab(crontab.stdout)
        if entries:
            try:
                cron_active = (
                    subprocess.run(
                        ["systemctl", "is-active", "cron"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    ).stdout.strip()
                    == "active"
                )
            except Exception:
                cron_active = False
            header_emoji = "🟢" if cron_active else "🔴"
            if not cron_active:
                warnings.append(tr("commands.cron_stopped"))
            grouped = _format_cron_groups(entries)
            grouped_text = "\n".join(grouped)
            # Telegram caps a single message at 4096 chars. MarkdownV2
            # conversion can inflate the body 1.2-1.5× via escapes, so we
            # gate on a conservative 2500-char ceiling for just this block.
            # When it triggers, drop the per-task list and keep just the
            # group counts — much terser, still informative.
            if len(grouped_text) > 2500:
                group_counts: dict[str, int] = {}
                for line in grouped:
                    if line.startswith(" ⏰") or line.startswith(" 🚀"):
                        group_counts[line.strip()] = 0
                    elif line.lstrip().startswith(("├", "└")):
                        last = list(group_counts.keys())[-1] if group_counts else None
                        if last:
                            group_counts[last] += 1
                grouped_text = "\n".join(
                    tr("commands.status_n_tasks", name=name, n=n)
                    for name, n in group_counts.items()
                )
            cron_block = (
                tr("commands.status_schedule", emoji=header_emoji, n=len(entries))
                + TranscriptParser.EXPANDABLE_QUOTE_START
                + grouped_text
                + TranscriptParser.EXPANDABLE_QUOTE_END
            )
            sections.append(cron_block)
    except Exception:
        pass

    # --- Header (summary) -------------------------------------------------
    if warnings:
        summary = tr("commands.status_warnings", n=len(warnings))
    else:
        summary = tr("commands.status_all_ok")
    uptime = _read_uptime_human()
    header = tr("commands.status_header", summary=summary)
    if uptime:
        header += f"\n🕐 uptime {uptime}"
    return header + "\n\n" + "\n\n".join(sections)


def _status_keyboard() -> InlineKeyboardMarkup:
    # Host-specific integrations (e.g. the drive plugin's «Fix Drive») add
    # their buttons via plugins.status_buttons(); the core row is Refresh only.
    row = [
        InlineKeyboardButton(
            tr("commands.status_refresh"), callback_data=CB_STATUS_REFRESH
        )
    ]
    row.extend(plugins.status_buttons())
    return InlineKeyboardMarkup([row])


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show server status: agents, Docker, services, resources, cron
    schedule + plugin sections. Includes the refresh inline keyboard."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    text = await _build_status_text()
    await safe_reply(update.message, text, reply_markup=_status_keyboard())


Tab = Literal["nav", "act", "ses"]


def _action_home_tab(action: str) -> Tab:
    """The tab a panel action's button lives on.

    Post-action repaints and confirm-cancel return here, so the user lands
    back on the buttons they tapped from. Lifecycle + configuration actions
    live on «Сессия»; everyday actions on «Действия».
    """
    return "ses" if action in ("kill", "restart", "fresh") else "act"


# Confirmation copy for destructive / restart panel actions.
# (description, short_button_label) — the description is shown in the panel
# caption above the button by callbacks._show_confirm; the label goes on the
# button. Kept here so the keyboard builder and the confirm handler share one
# source of truth. Descriptions are plain text (no Markdown) — _show_confirm
# escapes them for MarkdownV2 before sending.
#
# A `.get(key, default)`-compatible object (not a plain dict) so the copy is
# resolved through `tr` at access time — /lang then switches it live — while
# callbacks.py keeps consuming it as `CONFIRM_COPY.get(action, ("", ""))`.
class _ConfirmCopy:
    def get(self, key: str, default: tuple[str, str] = ("", "")) -> tuple[str, str]:
        table: dict[str, tuple[str, str]] = {
            "clear": (
                tr("commands.confirm_clear_desc"),
                tr("commands.confirm_clear_btn"),
            ),
            "compact": (
                tr("commands.confirm_compact_desc"),
                tr("commands.confirm_compact_btn"),
            ),
            "kill": (
                tr("commands.confirm_kill_desc"),
                tr("commands.confirm_kill_btn"),
            ),
            # restart/fresh aren't destructive (both keep the old session's
            # JSONL), but the labels alone don't say what they do — the
            # description is where that's explained.
            "restart": (
                tr("commands.confirm_restart_desc"),
                tr("commands.confirm_restart_btn"),
            ),
            "fresh": (
                tr("commands.confirm_fresh_desc"),
                tr("commands.confirm_fresh_btn"),
            ),
        }
        return table.get(key, default)


CONFIRM_COPY = _ConfirmCopy()

# Confirm-button colour grammar (from the design panel):
#   red   = «да, уничтожить» — irreversible loss (clear / kill / delete agent)
#   green = «да, вперёд к свежему состоянию» — restart / new session (recoverable)
#   blue  = «да, продолжить» — a reversible op (compact); the default
# Cancel always stays neutral so the coloured commit button draws the eye.
_DESTRUCTIVE_CONFIRMS = {"clear", "kill"}
_FORWARD_CONFIRMS = {"restart", "fresh"}


def _build_commands_keyboard(
    window_id: str,
    *,
    tab: Tab = "nav",
    confirming: str | None = None,
) -> InlineKeyboardMarkup:
    """Agent panel keyboard with three tabs (Клавиши / Действия / Сессия).

    Nav tab («Клавиши») — raw key presses for driving Claude's TUI:
    arrows, Esc, ^C, ^B, Enter, "/", plus «Стереть ввод» (an input-line
    op, so it lives with the keys). Key presses route through the
    screenshot-keys handler (CB_KEYS_PREFIX:kb), which knows how to send
    a key and refresh the photo while preserving the active tab.

    Act tab («Действия») — the everyday on-the-fly toggles: Mode / Effort /
    Compact / Clear. Kept to two rows so the pane photo — the actual
    content — stays on screen instead of being pushed off by the keyboard.

    Ses tab («Сессия») — session config & diagnostics (Model / Context /
    MCP) and lifecycle (Resume / New / Restart / End / worktree
    fork+delete).

    Colour grammar (see architecture.md): the grid is neutral except two
    accents — blue on the primary tap (Refresh) and red only on 🗑
    delete-agent (worktree topics). Clear and End are neutral in the grid
    by user preference: the loss warning lives on their red confirm step.
    Green appears only on confirm buttons, never in the grid.

    Confirmation layout — shown when ``confirming`` is set; it suspends
    the tab UI entirely and offers a single yes/cancel pair. Destructive
    actions flip here first; confirm/cancel returns to the action's home
    tab (``_action_home_tab``).
    """
    wid_short = window_id[:32]  # keep payload well under 64 bytes

    if confirming:
        # The full explanation of what each action does lives in the panel
        # caption above the button (set by callbacks._show_confirm from
        # CONFIRM_COPY), not in the button label — Telegram clips long
        # labels, so a description-in-button got truncated. The button is
        # just a short «Да, …» confirmation.
        yes_label = CONFIRM_COPY.get(
            confirming, ("", tr("commands.confirm_default_btn"))
        )[1]
        if confirming in _DESTRUCTIVE_CONFIRMS:
            yes_style = KeyboardButtonStyle.DANGER
        elif confirming in _FORWARD_CONFIRMS:
            yes_style = KeyboardButtonStyle.SUCCESS
        else:
            yes_style = KeyboardButtonStyle.PRIMARY
        # Cancel returns to the tab the action was tapped from.
        home_tab = _action_home_tab(confirming)
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        yes_label,
                        callback_data=f"cm:cfm:{confirming}:{wid_short}"[:64],
                        style=yes_style,
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr("commands.cancel"),
                        callback_data=f"{CB_CMD_CANCEL}{home_tab}:{wid_short}"[:64],
                    )
                ],
            ]
        )

    def cmd_btn(
        label: str, prefix: str, *, style: KeyboardButtonStyle | None = None
    ) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label, callback_data=f"{prefix}{wid_short}"[:64], style=style
        )

    def key_btn(label: str, key_id: str) -> InlineKeyboardButton:
        # Reuses the screenshot-keys callback so the nav tab and any
        # legacy screenshot messages still in chat speak the same wire
        # format. _handle_screenshot_keys parses kb:<key_id>:<window>.
        return InlineKeyboardButton(
            label, callback_data=f"{CB_KEYS_PREFIX}{key_id}:{wid_short}"[:64]
        )

    # Tab switcher row — the active tab swaps its icon for a «▸» pointer.
    # A suffix marker (`label ·`) made the longest label («⚙️ Session ·»)
    # overflow the three-per-row button width on phones and Telegram clipped
    # it to «Sessio…»; replacing the wide emoji with a narrow pointer keeps
    # the active label strictly narrower than the idle one. Tapping the
    # active tab is a no-op (same callback re-renders the same keyboard);
    # we still emit the callback so Telegram closes the spinner instead of
    # leaving it spinning.
    def tab_btn(tab_id: Tab, label_key: str) -> InlineKeyboardButton:
        label = tr(label_key)
        if tab == tab_id:
            parts = label.split(" ", 1)
            label = f"▸ {parts[1]}" if len(parts) == 2 else f"▸ {label}"
        return InlineKeyboardButton(
            label, callback_data=f"{CB_CMD_TAB}{tab_id}:{wid_short}"[:64]
        )

    tab_row = [
        tab_btn("nav", "commands.tab_nav"),
        tab_btn("act", "commands.tab_act"),
        tab_btn("ses", "commands.tab_ses"),
    ]

    # Refresh button repeats the active tab so the rendered photo comes back
    # with the same keyboard layout the user was looking at. Full-width on both
    # tabs — «Стереть ввод» now lives in the act-tab body grid.
    refresh_row = [
        InlineKeyboardButton(
            tr("commands.refresh"),
            callback_data=f"{CB_CMD_REFRESH}{tab}:{wid_short}"[:64],
            style=KeyboardButtonStyle.PRIMARY,
        )
    ]

    if tab == "nav":
        # Сверху Ctrl-комбо: ⎋ ^C — «упс, отмена/прервать», ^B — отправить
        # запущенный субагент / долгую bash-команду в фон и продолжить
        # общаться. Снизу рабочий ряд / ← → ↑ ↓ ⏎ — открыть Claude-овское
        # slash-меню, походить по нему (←/→ ходят по табам диалогов,
        # например в permission-промптах) и подтвердить. Стрелки в порядке
        # чтения: лево, право, верх, низ. Раздельно потому, что у этих двух
        # групп противоположное настроение и смешивать их в одну строку (как
        # было одним рядом) — глаз каждый раз ищет нужное. «Стереть ввод» —
        # тоже операция со строкой ввода, поэтому живёт здесь, не в действиях.
        # (Пробовали растащить ⏎ от ↓ по совету дизайн-ревью — юзер вернул:
        # единый ряд навигации удобнее, промахов на практике нет.)
        body = [
            [
                key_btn("⎋ Esc", "esc"),
                key_btn("Ctrl + C", "cc"),
                key_btn("Ctrl + B", "cb"),
            ],
            [
                key_btn("/", "slash"),
                key_btn("←", "lt"),
                key_btn("→", "rt"),
                key_btn("↑", "up"),
                key_btn("↓", "dn"),
                key_btn("⏎", "ent"),
            ],
            [cmd_btn(tr("commands.btn_wipe_input"), CB_CMD_WIPE_INPUT)],
        ]
    elif tab == "act":
        # Только ежедневное — два ряда, чтобы фото панели оставалось на
        # экране. Режим и Усилие — переключатели «на ходу» (пара по духу);
        # Контекст — диагностика состояния сессии, живёт на «Сессии».
        # Всё установочное/жизненный цикл — тоже там.
        body = [
            [
                cmd_btn(tr("commands.btn_mode"), CB_CMD_MODE_CYCLE),
                cmd_btn(tr("commands.btn_effort"), CB_CMD_EFFORT),
            ],
            [
                cmd_btn(tr("commands.btn_compact"), CB_CMD_COMPACT),
                # Neutral in the grid (user's call — the everyday tab shouldn't
                # carry a red button); the loss warning lives in the red
                # confirm step, which stays.
                cmd_btn(tr("commands.btn_clear"), CB_CMD_CLEAR),
            ],
        ]
    else:
        # «Сессия»: конфиг и диагностика сессии + жизненный цикл. Тройка
        # Модель/Контекст/MCP — подписи короткие, на телефоне не ужимаются;
        # остальное строго по двое. 🌳 заводит параллельного
        # агента-worktree в этом же проекте.
        body = [
            [
                cmd_btn(tr("commands.btn_model"), CB_CMD_MODEL),
                cmd_btn(tr("commands.btn_context"), CB_CMD_CONTEXT),
                cmd_btn("🔌 MCP", CB_CMD_MCP),
            ],
            [
                cmd_btn(tr("commands.btn_resume"), CB_CMD_RESUME),
                cmd_btn(tr("commands.btn_new"), CB_CMD_FRESH),
            ],
            [
                cmd_btn(tr("commands.btn_restart"), CB_CMD_RESTART),
                # Neutral like Clear (user's call) — the red confirm step
                # carries the warning.
                cmd_btn(tr("commands.btn_end"), CB_CMD_KILL),
            ],
            [cmd_btn(tr("commands.btn_new_worktree"), CB_WT_NEW)],
        ]
        # Worktree topics get an explicit instant delete (no waiting for the
        # hard-delete probe) — on the SAME row as 🌳, so the worst-case tab
        # stays within the row budget and the pane photo keeps fitting on
        # screen. Plain project topics never show it. The red confirm step
        # guards the create/destroy adjacency.
        if session_manager.is_worktree_window(window_id):
            body[-1].append(
                cmd_btn(
                    tr("commands.btn_delete_agent"),
                    CB_WT_DEL,
                    style=KeyboardButtonStyle.DANGER,
                )
            )

    return InlineKeyboardMarkup([tab_row, *body, refresh_row])


async def commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the agent-control inline menu with a live terminal screenshot.

    Sends a photo of the current tmux pane plus an inline keyboard of
    Claude Code slash commands and ccbot actions. Each button runs
    against the topic's bound agent; the photo refreshes after every
    action so the user sees what the command did.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, tr("commands.no_session_in_topic"))
        return

    display = session_manager.get_display_name(wid)
    pane_text = await session_manager.capture_pane(wid, with_ansi=True)
    if not pane_text:
        await safe_reply(update.message, tr("commands.agent_unavailable", name=display))
        return

    safe_name = escape_markdown(display, version=2)
    caption = tr("commands.agent_caption", name=safe_name)
    keyboard = _build_commands_keyboard(wid, tab="nav")

    # If this exact pane was uploaded before in this bot's lifetime (agent idle
    # since the last screenshot), reuse Telegram's file_id — no render, no
    # upload. On a stale-file_id BadRequest, drop it and render fresh. Same
    # pane_cache the 🔄-refresh path uses, so the next refresh hash-skips too.
    pane_h = pane_cache.pane_hash(pane_text)
    cached_file_id = pane_cache.get_file_id(pane_h)
    sent = None
    if cached_file_id is not None:
        try:
            sent = await update.message.reply_photo(
                photo=cached_file_id,
                caption=caption,
                parse_mode=PARSE_MODE,
                reply_markup=keyboard,
            )
        except BadRequest:
            pane_cache.forget_file_id(pane_h)
            sent = None
    if sent is None:
        png_bytes = await text_to_image(pane_text, with_ansi=True)
        sent = await update.message.reply_photo(
            photo=io.BytesIO(png_bytes),
            caption=caption,
            parse_mode=PARSE_MODE,
            reply_markup=keyboard,
        )
        if sent.photo:
            pane_cache.set_file_id(pane_h, sent.photo[-1].file_id)
    if sent:
        pane_cache.set_hash(sent.message_id, pane_h)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-attach the persistent command keyboard to the current chat."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    await safe_reply(
        update.message,
        tr("menu.pinned"),
        reply_markup=menu_keyboard(),
    )
    thread_id = get_thread_id(update)
    if thread_id is not None:
        session_manager.mark_menu_shown(user.id, thread_id)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart a Claude agent. Topic binding first, then optional /restart <name>.

    Tmux path: send /exit then re-invoke claude in the same window.
    Docker path: kill the container's tmux session and recreate it with
    Claude running in /workspace.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)
    text = update.message.text or ""
    # Only treat as "/restart <name>" if the message actually starts with a
    # slash command. ReplyKeyboard taps arrive as plain labels like
    # "🔄 Restart" — those must fall through to topic-binding resolution.
    parts_cmd = text.strip().split(maxsplit=1) if text.startswith("/") else []

    # Resolution: either the arg (tmux agent by name / docker agent by name)
    # or the topic binding. ``target_wid`` is the binding value (``@<id>`` or
    # ``docker:<name>``); the branches below route accordingly.
    target_wid: str | None = None
    agent_name: str = ""

    if len(parts_cmd) >= 2:
        requested = parts_cmd[1].strip()
        agent_name = requested
        docker_agent = config.get_docker_agent(requested)
        if docker_agent and config.docker_agents_enabled:
            # Canonical name: a user-cased "docker:Assistant" would miss
            # the window_state (no session resume) and the display map.
            target_wid = f"docker:{docker_agent.name}"
            agent_name = docker_agent.name
        else:
            windows = await tmux_manager.list_windows()
            # Accept both literal match and the project-agent "-dev"
            # convention: mail/whoami.sh derives "<project>-dev" but the
            # tmux window is "<project>" (the dir name). Without strip,
            # `/restart ccbot-dev` would fail to find the "ccbot" window.
            target_name = requested.lower()
            stripped = target_name.removesuffix("-dev")
            for w in windows:
                wn = w.window_name.lower()
                if wn == target_name or wn == stripped:
                    target_wid = w.window_id
                    agent_name = w.window_name
                    break
    elif thread_id is not None:
        target_wid = session_manager.resolve_window_for_thread(user.id, thread_id)
        if target_wid:
            agent_name = session_manager.get_display_name(target_wid)

    if not target_wid:
        if agent_name:
            await safe_reply(
                update.message, tr("commands.agent_not_found", name=agent_name)
            )
        else:
            await safe_reply(update.message, tr("commands.no_session_in_topic"))
        return

    ws = session_manager.get_window_state(target_wid)
    session_id = ws.session_id if ws else ""

    if not session_manager._is_docker_binding(target_wid):
        # The tmux restart types /exit + the relaunch command into the
        # pane — on a busy agent both would land in Claude's prompt as
        # text (and the success check would still pass, since the
        # process never exited). Docker restart kills the tmux session
        # outright, so it doesn't need this guard.
        pane = await tmux_manager.capture_pane(target_wid)
        if pane and is_claude_working(pane):
            await safe_reply(
                update.message,
                tr("commands.restart_busy", name=agent_name),
            )
            return

    await safe_reply(update.message, tr("commands.restarting", name=agent_name))

    if session_manager._is_docker_binding(target_wid):
        agent = config.get_docker_agent(target_wid[len("docker:") :])
        if not agent or not await docker_driver.is_container_alive(agent.container):
            await safe_reply(update.message, tr("commands.container_not_running"))
            return
        # send_lock: no other writer may type into the pane mid-restart.
        async with session_manager.send_lock(target_wid):
            await docker_driver.kill_session(agent.container)
            await asyncio.sleep(1)
            started = await docker_driver.start_session(
                agent.container, resume_session_id=session_id or None
            )
        if not started:
            await safe_reply(
                update.message,
                tr("commands.restart_no_tmux", name=agent_name),
            )
            return
        await asyncio.sleep(5)
        if await docker_driver.has_session(agent.container):
            await safe_reply(update.message, tr("commands.restarted", name=agent_name))
        else:
            await safe_reply(
                update.message,
                tr("commands.restart_maybe_failed", name=agent_name),
            )
        return

    # Tmux path — original behavior.
    target_window = await tmux_manager.find_window_by_id(target_wid)
    if not target_window:
        await safe_reply(update.message, tr("commands.window_gone", name=agent_name))
        return

    # send_lock across /exit → relaunch: anything typed into the pane in
    # the 3s gap would land in bash, not Claude.
    async with session_manager.send_lock(target_wid):
        await tmux_manager.send_keys(target_window.window_id, "/exit")
        await asyncio.sleep(3)

        cmd = config.claude_command
        # session_id is typed into the pane's shell after /exit — only append it
        # when it is a well-formed session id, else start fresh. (audit HIGH#1)
        if session_id and is_valid_session_id(session_id):
            cmd = f"{cmd} --resume {session_id}"
        elif session_id:
            logger.warning(
                "Ignoring malformed resume id %r; starting fresh", session_id
            )
        await tmux_manager.send_keys(target_window.window_id, cmd)

    await asyncio.sleep(8)
    w = await tmux_manager.find_window_by_id(target_window.window_id)
    if w and w.pane_current_command in ("claude", "node"):
        await safe_reply(update.message, tr("commands.restarted", name=agent_name))
    else:
        await safe_reply(
            update.message,
            tr("commands.restart_maybe_failed", name=agent_name),
        )


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        return

    # Worktree topics run the git safety guard (⚪ auto-teardown vs 🟢/🟡 keep
    # the agent alive + offer a choice) instead of the unconditional teardown.
    meta = session_manager.get_worktree_meta(user.id, thread_id)
    if meta is not None:
        from .worktrees import handle_worktree_topic_close

        chat_id = session_manager.resolve_chat_id(user.id, thread_id)
        await handle_worktree_topic_close(
            context.bot, user.id, thread_id, meta, chat_id
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid:
        display = session_manager.get_display_name(wid)
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        session_manager.unbind_thread(user.id, thread_id)
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


# Parent directories (under $HOME) scanned by topic-name → tmux directory
# auto-bind. From CCBOT_TOPIC_DIR_ROOTS; defaults to this server's
# ``projects,agents``. Order matters: the first root wins if the same name
# exists in several (project repos are the common case).
_TOPIC_DIR_PARENTS: tuple[str, ...] = config.topic_dir_roots


def _find_matching_dir_for_topic(name: str) -> Path | None:
    """Match a topic name to ``~/projects/<name>`` or ``~/agents/<name>``.

    Skips dotfiles and underscore-prefixed names (the latter are server
    infra dirs like ``_docker``, ``_tools``, ``_plans``). Returns ``None``
    when nothing matches; caller falls back to the normal directory
    browser flow.
    """
    if not name or name.startswith(".") or name.startswith("_"):
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    home = Path.home()
    lname = name.lower()
    for parent in _TOPIC_DIR_PARENTS:
        parent_dir = home / parent
        # Exact match first (fast path, original behavior).
        exact = parent_dir / name
        if exact.is_dir():
            return exact
        # Case-insensitive fallback: topic names are often capitalized
        # ("VPN", "Ccbot") while the folder is lowercase. Still skip infra
        # dirs (dotfiles / underscore-prefixed).
        if not parent_dir.is_dir():
            continue
        for entry in sorted(parent_dir.iterdir()):
            ename = entry.name
            if ename.startswith(".") or ename.startswith("_"):
                continue
            if ename.lower() == lname and entry.is_dir():
                return entry
    return None


async def _try_auto_bind_topic(
    user_id: int,
    thread_id: int,
    name: str,
    msg: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Try to auto-bind ``thread_id`` based on its display name.

    Two flavours of match (checked in order):

    1. **Docker agent** (``DOCKER_AGENTS`` env) — bind to ``docker:<name>``
       immediately, no tmux window involved (the container *is* the workdir).
    2. **Tmux directory** — if ``~/projects/<name>`` or ``~/agents/<name>``
       exists: create a tmux window in that path and bind. When existing
       Claude sessions are present in the dir, surface the session picker
       so the user can resume or start fresh; otherwise create new silently.

    Returns ``True`` when something was attempted (docker bind, tmux window
    created, or session picker shown) so callers can decide whether to fall
    back to other flows. ``msg`` is used only as a target for ``safe_reply``
    confirmations — pass the ``forum_topic_created`` service message.
    """
    assert isinstance(msg, Message)

    # Idempotency: if the thread is already bound (e.g. the bot created the
    # topic itself and bound it explicitly), the self-emitted
    # forum_topic_created must not re-bind or spawn a second window.
    if session_manager.get_window_for_thread(user_id, thread_id):
        return True

    # --- 1. Docker agent match ---
    if config.docker_agents_enabled:
        agent = config.get_docker_agent(name)
        if agent:
            # Bind by the canonical agent name, not the topic's casing.
            agent_name = agent.name
            session_manager.bind_thread(
                user_id, thread_id, f"docker:{agent_name}", window_name=agent_name
            )
            logger.info(
                "Auto-bound topic to docker agent %r (user=%d, thread=%d)",
                agent_name,
                user_id,
                thread_id,
            )
            try:
                await safe_reply(
                    msg,
                    tr("commands.autobind_docker", name=agent_name),
                    reply_markup=menu_keyboard(),
                )
                session_manager.mark_menu_shown(user_id, thread_id)
            except Exception as e:
                logger.debug("auto-bind reply failed: %s", e)
            return True

    # --- 2. Tmux directory match (~/projects/<name>, ~/agents/<name>) ---
    matching_dir = _find_matching_dir_for_topic(name)
    if not matching_dir:
        return False

    return await _auto_bind_to_directory(
        user_id, thread_id, matching_dir, msg, context, topic_name=name
    )


def _display_home_path(path: Path) -> str:
    """Render ``path`` with ``$HOME`` collapsed to ``~`` for user-facing text."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


async def _auto_bind_to_directory(
    user_id: int,
    thread_id: int,
    matching_dir: Path,
    msg: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    topic_name: str | None = None,
) -> bool:
    """Bind ``thread_id`` to ``matching_dir`` (session picker, or fresh window).

    The directory-resolution half shared by the name-based auto-bind and the
    learned ``thread_directory_memory`` rebind. ``topic_name`` is the topic's
    display name when known (name path) — used only to decide whether a
    dedup-renamed window should rename the topic; the memory path passes
    ``None`` (no rename). Records the directory in memory up front so the topic
    re-resolves here next time regardless of its current name.
    """
    session_manager.record_thread_directory(user_id, thread_id, str(matching_dir))
    shown = _display_home_path(matching_dir)
    sessions = await session_manager.list_sessions_for_directory(str(matching_dir))

    resume_id: str | None = None
    if sessions and config.auto_resume_agents:
        # Transparent resume (no picker): a non-technical user in an agent topic
        # won't tap a session picker, so on rebind — e.g. after a container/tmux
        # restart dropped the window — silently continue the most recent session
        # for this folder. Opt-in via CCBOT_AUTO_RESUME_AGENTS (default off). The
        # session_id is validated at list_sessions_for_directory (JSONL stem) and
        # again in create_window before it can reach the shell.
        resume_id = sessions[0].session_id
        logger.info(
            "Auto-bind: resuming newest session %s for %s (user=%d, thread=%d)",
            resume_id,
            shown,
            user_id,
            thread_id,
        )
    elif sessions:
        # Existing Claude history in this folder — let the user pick.
        # State below is the same shape _handle_session_{select,new,cancel}
        # already consume, so the picker callbacks Just Work without changes.
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
            context.user_data[SESSIONS_KEY] = sessions
            context.user_data["_selected_path"] = str(matching_dir)
            context.user_data["_pending_thread_id"] = thread_id
        text, keyboard = build_session_picker(sessions, str(matching_dir))
        try:
            await safe_reply(msg, text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("auto-bind session picker reply failed: %s", e)
        logger.info(
            "Auto-bind: showed session picker for %s (%d sessions, user=%d, thread=%d)",
            shown,
            len(sessions),
            user_id,
            thread_id,
        )
        return True

    # No sessions (fresh window) or auto-resume enabled (continue the newest
    # session) — create the window and bind right away.
    success, message, created_wname, created_wid = await tmux_manager.create_window(
        str(matching_dir), resume_session_id=resume_id
    )
    if not success:
        logger.warning(
            "Auto-bind create_window failed for %s: %s", matching_dir, message
        )
        try:
            escaped = message.replace("\\", "\\\\").replace("`", "\\`")
            await safe_reply(
                msg,
                tr("commands.autobind_window_failed", dir=matching_dir, err=escaped),
            )
        except Exception:
            pass
        return True

    hook_ok = await session_manager.wait_for_session_map_entry(
        created_wid, timeout=15.0 if resume_id else 5.0
    )
    session_manager.bind_thread(
        user_id, thread_id, created_wid, window_name=created_wname
    )
    if resume_id:
        # `--resume` makes the SessionStart hook report a NEW session_id while
        # messages keep writing to the ORIGINAL JSONL — pin window_state to the
        # resumed id so the monitor tracks the right transcript. Mirrors the
        # resume override in bot._create_and_bind_window, INCLUDING the hook-
        # timeout branch: the in-container SessionStart hook is exactly what's
        # flaky in the deployment this flag targets, so a timeout here is the
        # expected path, not an edge case.
        ws = session_manager.get_window_state(created_wid)
        if not hook_ok:
            logger.warning(
                "Hook timed out for resume window %s — pinning session_id=%s cwd=%s",
                created_wid,
                resume_id,
                matching_dir,
            )
            ws.session_id = resume_id
            ws.cwd = str(matching_dir)
            ws.window_name = created_wname
            session_manager._save_state()
        elif ws.session_id != resume_id:
            ws.session_id = resume_id
            session_manager._save_state()
    logger.info(
        "Auto-bound thread %d to tmux window %s at %s (user=%d)",
        thread_id,
        created_wid,
        matching_dir,
        user_id,
    )

    # If create_window had to deduplicate (existing `ccbot` window → `ccbot-2`),
    # rename the topic to match — keeps Telegram in sync with tmux. A pure
    # case difference ("VPN" topic → "vpn" folder) is not a dedup: leave the
    # user's capitalization alone. Only the name path (topic_name set) renames.
    if topic_name is not None and created_wname.lower() != topic_name.lower():
        try:
            resolved_chat = session_manager.resolve_chat_id(user_id, thread_id)
            await context.bot.edit_forum_topic(
                chat_id=resolved_chat,
                message_thread_id=thread_id,
                name=created_wname,
            )
        except Exception as e:
            logger.debug("auto-bind topic rename failed: %s", e)

    try:
        await safe_reply(
            msg,
            tr(
                "commands.autobind_resumed"
                if resume_id
                else "commands.autobind_new_session",
                dir=_display_home_path(matching_dir),
            ),
            reply_markup=menu_keyboard(),
        )
        session_manager.mark_menu_shown(user_id, thread_id)
    except Exception as e:
        logger.debug("auto-bind reply failed: %s", e)

    # First-run trap: hook definitively absent → agent replies will never be
    # delivered for this fresh session. Warn in-topic (see bot.hook_missing).
    if not hook_ok and not resume_id and not hook_installed_in_settings():
        try:
            await safe_reply(msg, tr("bot.hook_missing"))
        except Exception as e:
            logger.debug("hook-missing warning failed: %s", e)
    return True


async def topic_created_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Auto-bind a freshly created topic when its name matches a known target.

    Delegates to ``_try_auto_bind_topic`` which handles both docker-agent
    and tmux-directory matches.

    Without this the user goes through window picker → directory browser →
    session picker on every new topic.

    For topics created while ccbot was offline the ``forum_topic_created``
    service message was already consumed by Telegram — those need to be
    renamed (handled by ``topic_edited_handler``) or fall back to the
    existing dir-browser flow on first message.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_created:
        return

    name = (msg.forum_topic_created.name or "").strip()
    if not name:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        return

    # Capture group chat_id so outbound messages reach this topic.
    # Both auto-bind branches need it; do it once up front.
    chat = update.effective_chat
    if chat and chat.id != user.id:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    await _try_auto_bind_topic(user.id, thread_id, name, msg, context)


async def topic_edited_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and internal state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        logger.debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    old_name = session_manager.get_display_name(wid)
    # Docker bindings don't have a tmux window on the host — only update
    # the display-name map. Tmux bindings get the tmux window rename too.
    if not session_manager._is_docker_binding(wid):
        await tmux_manager.rename_window(wid, new_name)
    session_manager.update_display_name(wid, new_name)
    logger.info(
        "Topic renamed: '%s' -> '%s' (window=%s, user=%d, thread=%d)",
        old_name,
        new_name,
        wid,
        user.id,
        thread_id,
    )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Claude Code session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = get_thread_id(update)

    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    cc_slash = cmd_text.split("@")[0]
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, tr("fwd.no_session"))
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, tr("fwd.sent", name=display, cmd=cc_slash))
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)
    else:
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(update.message, tr("media.unsupported"))


async def menu_button_dispatcher(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route taps on the persistent ReplyKeyboard buttons to slash commands.

    Buttons send their visible label as plain text (e.g. "🖥️ Сервер").
    Target commands must be resilient to plain text — restart_command
    ignores text that doesn't start with a slash, the rest don't parse it.
    """
    if not update.message or not update.message.text:
        return
    text = update.message.text
    # Match against every language's label, not just the active one — the
    # persistent keyboard a client still shows may carry the previous
    # language's label after a /lang switch.
    if text in i18n.all_variants("menu.server"):
        await status_command(update, context)
    elif text in i18n.all_variants("menu.agent"):
        await commands_command(update, context)
