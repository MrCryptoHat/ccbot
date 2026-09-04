"""Sibling agents — a second agent beside this one, no worktree, no repo.

The ➕ counterpart to 🌳: where a worktree agent forks a git repo into its own
branch+directory, a sibling agent shares the parent's working files verbatim
and only gets its own session and topic. Two shapes, one flow:

  - **Docker parent** → another Claude Code process in the SAME container
    (own in-container tmux session ``claude-<slug>``, same /workspace, same
    claude-home). Binding ``docker:<agent>/<slug>``, topic ``<agent>-<slug>``.
  - **Tmux parent** → another tmux window on the same cwd. Plain ``@<id>``
    binding, topic ``<parent>-<slug>`` — no new machinery at all.

Entry points: ``_handle_sib_new`` (➕ button) → ``consume_sibling_name`` (the
typed name) → ``provision_sibling_agent`` (transactional create; rolls the
topic back on any later failure). ``teardown_sibling`` kills a docker
sub-agent's session when its topic closes or is deleted — a tmux sibling is
just a normal topic and dies through the existing window teardown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.ext import ContextTypes

from ..i18n import tr
from ..session import session_manager
from ..worktrees import dedup_slug, slugify
from . import effective_user, get_thread_id
from .callback_data import CALLBACK_WID_MAX, CB_SIB_CANCEL, CB_SIB_NEW
from .message_sender import safe_send
from .provisioning import claim_topic

logger = logging.getLogger(__name__)

# Topic-name marker, so a fleet of siblings is findable in the topic list at a
# glance (🌳 does the same for worktree agents).
SIBLING_TOPIC_MARK = "➕"

# The "name your agent" step is stored PER TOPIC (``user_data["_sib_pending"]``
# = {thread_id: (parent_binding, chat_id, deadline)}), not in the single
# shared ``STATE_KEY`` slot the directory browser and pickers use. Two ➕ taps
# in two topics are then independent, and neither disarms a picker running in
# some third topic — a shared slot silently dropped whichever was armed first
# and forwarded the typed name to the agent instead.
PENDING_KEY = "_sib_pending"
# An abandoned ➕ tap must not eat a much later message as a name.
SIB_NAMING_TTL_SEC = 300.0

# How long to wait for the container's SessionStart hook to name the new
# sibling in its session_map. Claude Code boots in ~2-5 s inside a container;
# 12 s leaves room on a loaded host without making a refusal feel like a hang.
_HOOK_PROBE_TIMEOUT_SEC = 12.0
_HOOK_PROBE_INTERVAL_SEC = 0.5


def _pending(ud: dict | None) -> dict[int, tuple[str, int, float]]:
    """The per-topic naming states for this user (created on demand)."""
    if ud is None:
        return {}
    slot = ud.get(PENDING_KEY)
    if not isinstance(slot, dict):
        slot = {}
        ud[PENDING_KEY] = slot
    return slot


def _clear_sib_naming(ud: dict | None, thread_id: int | None = None) -> None:
    """Drop the naming state for one topic (or all of them when None)."""
    slot = _pending(ud)
    if thread_id is None:
        slot.clear()
    else:
        slot.pop(thread_id, None)


def cancel_pending_naming(ud: dict | None, thread_id: int | None) -> None:
    """Public seam: this topic did something else, so the ➕ step is off.

    Called from the non-text inbound paths (voice, photos, documents): they
    reach the agent directly, so a user who taps ➕ and then sends a voice note
    has clearly moved on — leaving the step armed would turn their next typed
    message into an agent instead of a prompt.
    """
    if thread_id is not None and _pending(ud).pop(thread_id, None) is not None:
        logger.debug(
            "sibling naming cancelled by non-text message (thread=%s)", thread_id
        )


def _cancel_keyboard(thread_id: int | None) -> InlineKeyboardMarkup:
    """Cancel button carrying its topic, so it clears ITS state, not another's."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("wt.cancel"), callback_data=f"{CB_SIB_CANCEL}{thread_id or 0}"
                )
            ]
        ]
    )


def _max_slug_len(agent_name: str) -> int:
    """Slug budget so ``docker:<agent>/<slug>`` still fits a callback payload.

    A binding longer than ``CALLBACK_WID_MAX`` is truncated into the panel's
    buttons, and the stale-panel guard then rejects every tap — the agent would
    be unreachable through its own panel. 4 chars minimum: a pathological agent
    name costs the slug its readability, never its existence.
    """
    return max(4, CALLBACK_WID_MAX - len("docker:") - len(agent_name) - 1)


# --- provisioning -----------------------------------------------------------


async def _rollback_topic(bot, chat_id: int, thread_id: int) -> None:
    """Delete a topic we created during a provision that then failed."""
    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except Exception as e:  # noqa: BLE001 — best-effort rollback
        logger.debug("sibling rollback delete_forum_topic failed: %s", e)


async def _hook_names_the_sibling(
    map_path: Path, binding: str, parent_binding: str, session_id: str
) -> bool | None:
    """Did the container's hook write the SIBLING's key for this session?

    Returns True (sibling-aware hook), False (it wrote the parent's key with
    the sibling's session — that hook hardcodes its agent name), or None
    (nothing conclusive within the timeout; the sibling still works off its
    pinned id).

    Why it matters enough to check: a hardcoded-name hook reports every future
    session of the sibling — the next `/clear` above all — under the PARENT's
    key. ccbot's one-session-one-binding guard blocks the first such claim
    (the id is pinned to the sibling), but a *fresh* id it has never seen has
    nothing to be checked against, and the parent's topic would silently start
    streaming its child's transcript.
    """

    def _sid(data: object, key: str) -> object:
        """session_id under `key`, tolerating any shape the file may hold."""
        if not isinstance(data, dict):
            return None
        entry = data.get(key)
        return entry.get("session_id") if isinstance(entry, dict) else None

    deadline = time.monotonic() + _HOOK_PROBE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            data = json.loads(await asyncio.to_thread(map_path.read_text))
        except Exception:  # noqa: BLE001 — unreadable/odd file is just "not yet"
            data = {}
        if _sid(data, binding) == session_id:
            return True
        if _sid(data, parent_binding) == session_id:
            return False
        await asyncio.sleep(_HOOK_PROBE_INTERVAL_SEC)
    return None


async def _provision_docker_sibling(
    bot,
    user_id: int,
    chat_id: int,
    parent_wid: str,
    title: str,
) -> tuple[bool, str]:
    """Start a second Claude Code inside the parent's container."""
    target = session_manager.resolve_docker_target(parent_wid)
    if target is None:
        return False, tr("sib.no_parent")
    agent_name = target.agent.name
    taken = await session_manager.taken_sub_slugs(agent_name)
    slug = dedup_slug(slugify(title)[: _max_slug_len(agent_name)].strip("-"), taken)
    binding = f"docker:{agent_name}/{slug}"
    display = f"{agent_name}-{slug}"
    topic_name = f"{SIBLING_TOPIC_MARK} {display}"

    try:
        ft = await bot.create_forum_topic(chat_id=chat_id, name=topic_name[:128])
    except Exception as e:  # noqa: BLE001
        return False, tr("wt.err_topic_not_created", error=e)
    new_thread = ft.message_thread_id

    with claim_topic(user_id, new_thread):
        # The parent's cwd is the container path Claude reported (/workspace);
        # a sibling starts in the same place — that's the whole point.
        parent_cwd = session_manager.get_window_state(parent_wid).cwd or "/workspace"
        session_id = str(uuid.uuid4())
        started = await session_manager.start_docker_agent(
            binding, new_session_id=session_id, cwd=parent_cwd
        )
        if not started:
            await _rollback_topic(bot, chat_id, new_thread)
            return False, tr("sib.err_start")

        named = await _hook_names_the_sibling(
            target.agent.session_map_path, binding, f"docker:{agent_name}", session_id
        )
        if named is False:
            # This container's hook can't tell its agents apart, so the sibling's
            # next session would be reported as the PARENT's. Undo everything
            # rather than leave a trap that fires on the sibling's first /clear.
            await session_manager.kill_agent(binding)
            session_manager.forget_binding(binding)
            await _rollback_topic(bot, chat_id, new_thread)
            logger.warning(
                "Sibling refused: %s's hook wrote the parent key for session %s "
                "(it must key off AGENT_NAME)",
                agent_name,
                session_id,
            )
            return False, tr("sib.err_hook", name=agent_name)
        if named is None:
            logger.warning(
                "Sibling %s: the container's hook never reported it; tracking "
                "relies on the pinned session id alone",
                binding,
            )

        session_manager.bind_thread(user_id, new_thread, binding, window_name=display)
        session_manager.set_group_chat_id(user_id, new_thread, chat_id)
        await safe_send(
            bot,
            chat_id,
            tr("sib.welcome_docker", parent=agent_name),
            message_thread_id=new_thread,
        )
        logger.info(
            "Provisioned sibling docker agent %s (thread=%d, container=%s, session=%s)",
            binding,
            new_thread,
            target.agent.container,
            target.tmux_session,
        )
        return True, tr("sib.provision_ok", name=display)


async def _provision_tmux_sibling(
    bot,
    user_id: int,
    chat_id: int,
    parent_wid: str,
    title: str,
) -> tuple[bool, str]:
    """Open a topic for a second agent on the parent's directory.

    Stops short of starting it: the topic remembers the DIRECTORY and its
    first message opens the session picker, so the sibling can run a different
    CLI (and resume a different session) than its parent. Inheriting the
    parent's runtime removed the one choice a parallel agent exists for
    (operator request 2026-09-04).
    """
    ws = session_manager.get_window_state(parent_wid)
    cwd = ws.cwd
    if not cwd or not Path(cwd).is_dir():
        return False, tr("sib.no_parent")
    parent_display = session_manager.get_display_name(parent_wid)
    display = f"{parent_display}-{slugify(title)}"
    topic_name = f"{SIBLING_TOPIC_MARK} {display}"

    try:
        ft = await bot.create_forum_topic(chat_id=chat_id, name=topic_name[:128])
    except Exception as e:  # noqa: BLE001
        return False, tr("wt.err_topic_not_created", error=e)
    new_thread = ft.message_thread_id

    with claim_topic(user_id, new_thread):
        session_manager.set_group_chat_id(user_id, new_thread, chat_id)
        # Directory only — no runtime: recording one would pre-answer the picker.
        session_manager.record_thread_directory(user_id, new_thread, cwd)
        # Flags this topic as an EXTRA agent, which is what makes the panel offer
        # 🗑 here (a main topic has no delete button — see can_delete_agent).
        session_manager.mark_sub_agent_topic(user_id, new_thread)
        await safe_send(
            bot,
            chat_id,
            tr("sib.welcome_tmux", path=cwd),
            message_thread_id=new_thread,
        )
        logger.info(
            "Provisioned sibling topic on %s (thread=%d) — awaiting agent pick",
            cwd,
            new_thread,
        )
        return True, tr("sib.provision_ok", name=display)


async def provision_sibling_agent(
    bot,
    user_id: int,
    chat_id: int,
    parent_wid: str,
    title: str,
) -> tuple[bool, str]:
    """Create an agent beside ``parent_wid``. Returns (ok, user-facing message).

    Transactional in the same sense as the worktree flow: a failure after
    ``create_forum_topic`` deletes the topic again, so a half-made agent never
    leaves an orphan topic behind.
    """
    if session_manager._is_docker_binding(parent_wid):
        return await _provision_docker_sibling(bot, user_id, chat_id, parent_wid, title)
    return await _provision_tmux_sibling(bot, user_id, chat_id, parent_wid, title)


# --- create flow (➕ button → name capture) ---------------------------------


async def _handle_sib_new(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """➕ One more agent — ask for the new agent's name."""
    thread_id = get_thread_id(update)
    # The topic's CURRENT binding, not the payload: callback data holds only
    # the first CALLBACK_WID_MAX chars of it, and a truncated value would mint
    # window_states nothing owns. The stale-panel guard already checked they
    # agree on that prefix.
    wid = (
        session_manager.resolve_window_for_thread(user.id, thread_id)
        or data[len(CB_SIB_NEW) :]
    )
    if not session_manager.can_offer_sibling(wid):
        await query.answer(tr("sib.no_parent"), show_alert=True)
        return
    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    if thread_id is not None:
        _pending(context.user_data)[thread_id] = (
            wid,
            chat_id,
            time.monotonic() + SIB_NAMING_TTL_SEC,
        )
    await query.answer()
    await safe_send(
        context.bot,
        chat_id,
        tr("sib.name_prompt", name=session_manager.get_display_name(wid)),
        message_thread_id=thread_id,
        reply_markup=_cancel_keyboard(thread_id),
    )


async def _handle_sib_cancel(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """↩ Cancel on the naming prompt — clears THIS topic's step only."""
    raw = data[len(CB_SIB_CANCEL) :]
    thread_id = int(raw) if raw.lstrip("-").isdigit() else get_thread_id(update)
    _clear_sib_naming(context.user_data, thread_id)
    await query.answer(tr("cb.cancelled"))
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:  # noqa: BLE001
        logger.debug("sibling cancel keyboard clear failed: %s", e)


async def consume_sibling_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """If we're awaiting a sibling's name in this topic, create it. True = handled.

    Called from ``text_handler`` before normal routing (same seam as the
    worktree naming step). Keyed by topic, so a ➕ armed in another topic
    neither eats this message nor gets dropped by it.
    """
    ud = context.user_data
    thread_id = get_thread_id(update)
    if not ud or thread_id is None:
        return False
    state = _pending(ud).get(thread_id)
    if state is None:
        return False
    parent_wid, chat_id, deadline = state
    if time.monotonic() > deadline:
        # Stale tap — let the message reach the agent instead of becoming a name.
        _clear_sib_naming(ud, thread_id)
        return False
    user = effective_user(update)
    if user is None or update.message is None:
        return False

    _clear_sib_naming(ud, thread_id)
    title = (update.message.text or "").strip()
    if not title:
        await safe_send(
            context.bot, chat_id, tr("wt.empty_name"), message_thread_id=thread_id
        )
        return True

    await safe_send(
        context.bot, chat_id, tr("wt.creating"), message_thread_id=thread_id
    )
    ok, info = await provision_sibling_agent(
        context.bot, user.id, chat_id, parent_wid, title
    )
    await safe_send(
        context.bot,
        chat_id,
        f"✅ {info}" if ok else tr("wt.provision_failed", info=info),
        message_thread_id=thread_id,
    )
    return True


# --- teardown ---------------------------------------------------------------


async def teardown_sibling(user_id: int, thread_id: int, wid: str) -> bool:
    """Kill a docker sub-agent when its topic goes away. True iff it was one.

    A sub-agent is ccbot-created and ccbot-owned, so unlike a main docker
    binding (whose lifecycle is the container's) it must not outlive its topic:
    nothing else would ever reach that in-container tmux session again. The
    caller still does the usual unbind/state cleanup.
    """
    if not session_manager.is_docker_sub_agent(wid):
        return False
    killed = await session_manager.kill_agent(wid)
    session_manager.forget_binding(wid)
    logger.info(
        "Sibling agent %s torn down with its topic (thread=%d, killed=%s)",
        wid,
        thread_id,
        killed,
    )
    return True
