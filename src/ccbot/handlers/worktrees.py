"""Telegram-facing orchestration for worktree agents (create / teardown / guard).

Wraps the pure git/disk core in ``..worktrees`` with the bot lifecycle:
  - provision_worktree_agent: transactional create (topic → worktree → seed →
    window → bind → meta → welcome), rolls back the topic on any later failure.
  - _handle_wt_new + consume_worktree_name: the ➕ "новый агент" flow (resolve
    project from the current topic → ask for a task name → provision).
  - handle_worktree_topic_close: the close-topic guard — ⚪ auto-teardown,
    🟢/🟡 keep the agent alive and offer [🧨 Удалить] / [↩ Вернуть топик].
  - teardown_worktree: preview down → kill window → unbind → worktree remove →
    branch -D → delete topic → drop meta. Destructive; interactive paths only.
  - handle_deleted_worktree_topic: headless cleanup for a hard-deleted topic —
    clean worktree → full teardown, dirty/unmerged → flag orphaned (preserve).

A worktree agent is a normal tmux topic whose cwd is a worktree dir; this layer
only owns the git lifecycle. See _plans/2026-06-14-ccbot-worktree-agents.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from telegram import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.constants import KeyboardButtonStyle
from telegram.ext import ContextTypes

from .. import worktrees as wtc
from ..config import config
from ..i18n import tr
from ..session import session_manager
from ..tmux_manager import tmux_manager
from ..worktrees import WorktreeMeta
from . import get_thread_id
from .callback_data import (
    CB_WT_DEL,
    CB_WT_DELNO,
    CB_WT_DELOK,
    CB_WT_DROP,
    CB_WT_KEEP,
    CB_WT_NEW,
)
from .cleanup import clear_topic_state
from .directory_browser import STATE_KEY
from .message_sender import safe_send

logger = logging.getLogger(__name__)

# user_data state value + keys for the "name your task" step.
STATE_WT_NAMING = "wt_naming"
# External `preview` CLI + its registry (paths overridable via CCBOT_PREVIEW_*;
# default to this server's XDG layout). Absent on a plain host → the registry
# read below fails soft and teardown just skips the preview-down step.
_PREVIEW_BIN = config.preview_bin
_PREVIEW_REGISTRY = config.preview_registry_path


# --- preview cleanup --------------------------------------------------------


async def _preview_down_under(wt_path: Path) -> None:
    """Best-effort ``preview down`` for any preview server cwd'd in the worktree.

    The preview registry stores ``cwd`` per slug; we stop those before removing
    the dir out from under a live dev server. Failures never block teardown.
    """
    try:
        reg = json.loads(_PREVIEW_REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(reg, dict):
        return
    for slug in wtc.preview_slugs_under(reg, wt_path):
        try:
            proc = await asyncio.create_subprocess_exec(
                str(_PREVIEW_BIN),
                "down",
                slug,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
        except (OSError, asyncio.TimeoutError):
            pass


# --- provisioning -----------------------------------------------------------


def _copy_btn(label: str, value: str) -> InlineKeyboardButton | None:
    """A 📋 copy-to-clipboard button, or None if the value can't be copied.

    Bot API caps ``copy_text.text`` at 256 chars; over that we drop the button
    rather than send a truncated (useless) path. Worktree paths/branches are
    always well under, so this is just defensive.
    """
    if not value or len(value) > 256:
        return None
    return InlineKeyboardButton(label, copy_text=CopyTextButton(text=value))


def _welcome_keyboard(wt_path: str, branch: str) -> InlineKeyboardMarkup | None:
    """One-tap copy buttons for the worktree path / branch on the welcome note."""
    row = [
        b
        for b in (
            _copy_btn(tr("wt.copy_path"), wt_path),
            _copy_btn(tr("wt.copy_branch"), branch),
        )
        if b is not None
    ]
    return InlineKeyboardMarkup([row]) if row else None


def _welcome_text(repo_name: str, branch: str) -> str:
    """User-facing note posted into the fresh topic (plain text by design)."""
    return tr("wt.welcome", repo=repo_name, branch=branch)


async def _rollback_topic(bot, chat_id: int, thread_id: int) -> None:
    """Delete a topic we created during a provision that then failed."""
    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except Exception as e:  # noqa: BLE001 — best-effort rollback
        logger.debug("rollback delete_forum_topic failed: %s", e)


async def provision_worktree_agent(
    bot,
    user_id: int,
    chat_id: int,
    base_repo: Path,
    repo_name: str,
    task_title: str,
) -> tuple[bool, str]:
    """Create a worktree-backed agent end to end. Returns (ok, message).

    Transactional: anything that fails after ``create_forum_topic`` rolls the
    topic (and any half-made worktree/branch) back so no orphan is left.
    """
    base = await wtc.detect_base_branch(base_repo)
    if not base:
        return False, tr("wt.err_no_base_branch")

    taken = await wtc.taken_slugs(base_repo, repo_name)
    slug = wtc.dedup_slug(wtc.slugify(task_title), taken)
    branch = wtc.branch_name(slug)
    wt_path = wtc.worktree_path(repo_name, slug)

    topic_name = f"🌳 {repo_name} · {task_title}"[:128]
    try:
        ft = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
    except Exception as e:  # noqa: BLE001
        return False, tr("wt.err_topic_not_created", error=e)
    new_thread = ft.message_thread_id

    ok, msg = await wtc.add_worktree(base_repo, wt_path, branch, base)
    if not ok:
        await _rollback_topic(bot, chat_id, new_thread)
        return False, f"worktree add: {msg[:120]}"

    await wtc.seed_worktree(base_repo, wt_path)

    success, message, wname, wid = await tmux_manager.create_window(
        str(wt_path), window_name=slug
    )
    if not success:
        await wtc.remove_worktree(base_repo, wt_path, force=True)
        await wtc.delete_branch(base_repo, branch, force=True)
        await _rollback_topic(bot, chat_id, new_thread)
        return False, tr("wt.err_window", error=message[:120])

    await session_manager.wait_for_session_map_entry(wid, timeout=5.0)
    session_manager.bind_thread(user_id, new_thread, wid, window_name=wname)
    session_manager.set_group_chat_id(user_id, new_thread, chat_id)
    session_manager.record_thread_directory(user_id, new_thread, str(wt_path))
    session_manager.set_worktree_meta(
        user_id,
        new_thread,
        WorktreeMeta(
            repo=str(base_repo),
            repo_name=repo_name,
            branch=branch,
            base_branch=base,
            path=str(wt_path),
            task_title=task_title,
        ),
    )
    await safe_send(
        bot,
        chat_id,
        _welcome_text(repo_name, branch),
        message_thread_id=new_thread,
        reply_markup=_welcome_keyboard(str(wt_path), branch),
    )
    logger.info(
        "Provisioned worktree agent: %s on %s (thread=%d, wid=%s)",
        branch,
        repo_name,
        new_thread,
        wid,
    )
    return True, tr("wt.provision_ok", repo=repo_name, title=task_title, branch=branch)


# --- create flow (➕ button → name capture) ---------------------------------


def _resolve_base_repo(user_id: int, thread_id: int | None, wid: str) -> Path | None:
    """The base repo a "new sibling agent" should fork from, for this topic.

    For a worktree topic that's the recorded base repo; otherwise the bound
    window's cwd (a plain project topic). Returns None if unresolvable.
    """
    if thread_id is not None:
        meta = session_manager.get_worktree_meta(user_id, thread_id)
        if meta is not None:
            return Path(meta.repo)
    ws = session_manager.get_window_state(wid)
    if ws and ws.cwd:
        return Path(ws.cwd)
    return None


async def _handle_wt_new(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """➕ Новый агент в проекте — resolve project, then prompt for a task name."""
    wid = data[len(CB_WT_NEW) :]
    thread_id = get_thread_id(update)
    base_repo = _resolve_base_repo(user.id, thread_id, wid)
    if base_repo is None:
        await query.answer(tr("wt.no_project"), show_alert=True)
        return
    repo_name = base_repo.name
    if not (base_repo / ".git").exists():
        await query.answer(tr("wt.not_git_repo", repo=repo_name), show_alert=True)
        return

    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_WT_NAMING
        context.user_data["_wt_repo"] = str(base_repo)
        context.user_data["_wt_chat_id"] = chat_id
        context.user_data["_wt_source_thread"] = thread_id
    await query.answer()
    await safe_send(
        context.bot,
        chat_id,
        tr("wt.new_agent_prompt", repo=repo_name),
        message_thread_id=thread_id,
    )


async def consume_worktree_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """If we're awaiting a task name in this topic, provision and return True.

    Called from ``text_handler`` before normal routing. Returns False when the
    message is not a worktree-naming reply (let normal handling proceed).
    """
    ud = context.user_data
    if not ud or ud.get(STATE_KEY) != STATE_WT_NAMING:
        return False
    thread_id = get_thread_id(update)
    if ud.get("_wt_source_thread") != thread_id:
        return False  # state belongs to a different topic — don't consume
    user = update.effective_user
    if user is None or update.message is None:
        return False

    base_repo = Path(str(ud.pop("_wt_repo", "")))
    chat_id = int(ud.pop("_wt_chat_id", 0))
    ud.pop("_wt_source_thread", None)
    ud.pop(STATE_KEY, None)
    task_title = (update.message.text or "").strip()

    if not task_title:
        await safe_send(
            context.bot, chat_id, tr("wt.empty_name"), message_thread_id=thread_id
        )
        return True

    await safe_send(
        context.bot, chat_id, tr("wt.creating"), message_thread_id=thread_id
    )
    ok, info = await provision_worktree_agent(
        context.bot, user.id, chat_id, base_repo, base_repo.name, task_title
    )
    await safe_send(
        context.bot,
        chat_id,
        f"✅ {info}" if ok else tr("wt.provision_failed", info=info),
        message_thread_id=thread_id,
    )
    return True


# --- teardown ---------------------------------------------------------------


async def teardown_worktree(
    bot,
    chat_id: int,
    user_id: int,
    thread_id: int,
    meta: WorktreeMeta,
    *,
    force: bool,
) -> None:
    """Destructive teardown (interactive paths only — guard already passed).

    Order: preview down → kill window → unbind/clear → worktree remove →
    branch -D → delete topic → drop meta.
    """
    repo = Path(meta.repo)
    wt_path = Path(meta.path)
    await _preview_down_under(wt_path)

    wid = session_manager.get_window_for_thread(user_id, thread_id)
    if wid and not session_manager._is_docker_binding(wid):
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
    session_manager.unbind_thread(user_id, thread_id)
    await clear_topic_state(user_id, thread_id, bot)

    ok, msg = await wtc.remove_worktree(repo, wt_path, force=force)
    if ok:
        # Always `-D`: reaching teardown means either the user explicitly
        # consented (interactive 🧨/🗑) or decide_delete_safety classified the
        # branch clean via count_unmerged's patch-id + multi-base check — which
        # already recognises squash/rebase/direct-push merges. git's own `-d`
        # judges merge by commit-ANCESTRY, a different (stricter) predicate that
        # would false-refuse exactly those integrated branches and leave them
        # orphaned, without ever catching a wrong "clean". The real guard
        # against destroying unmerged work is count_unmerged failing CLOSED to
        # None → "unmerged" → this path is never taken. (audit HIGH#2)
        deleted, derr = await wtc.delete_branch(repo, meta.branch, force=True)
        if not deleted:
            logger.warning(
                "branch %s not deleted after teardown: %s", meta.branch, derr
            )
    else:
        logger.warning("worktree remove failed for %s: %s", wt_path, msg)
    await wtc.prune_worktrees(repo)

    try:
        await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("delete_forum_topic on teardown failed: %s", e)
    session_manager.clear_worktree_meta(user_id, thread_id)
    logger.info("Tore down worktree agent %s (thread=%d)", meta.branch, thread_id)


async def handle_worktree_topic_close(
    bot,
    user_id: int,
    thread_id: int,
    meta: WorktreeMeta,
    chat_id: int,
) -> None:
    """Close-topic guard for a worktree topic (replaces the default teardown).

    ⚪ clean+merged → full auto-teardown. 🟢/🟡 → keep the agent alive and post
    a [🧨 Всё равно удалить] / [↩ Вернуть топик] choice (never silently destroy).
    """
    status = await wtc.worktree_status(
        Path(meta.repo), Path(meta.path), meta.base_branch, meta.branch
    )
    safety = wtc.decide_delete_safety(status)
    if safety == "clean":
        await teardown_worktree(bot, chat_id, user_id, thread_id, meta, force=False)
        return

    # Dirty / unmerged: keep the live agent (don't kill/unbind). Reopen the
    # topic right away so it's in a normal OPEN state — both because the user
    # needs to see/act on the choice, and so the existence probe's reopen stays
    # a no-op (a left-closed topic would otherwise get auto-reopened in ~10s,
    # the same effect but confusingly out of the user's hands).
    try:
        await bot.reopen_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("worktree close-guard reopen failed: %s", e)

    bullets: list[str] = []
    if status.dirty:
        bullets.append(tr("wt.bullet_dirty", count=status.dirty_files))
    if status.ahead is None or status.ahead > 0:
        bullets.append(
            tr(
                "wt.bullet_ahead",
                count="?" if status.ahead is None else status.ahead,
                base=meta.base_branch,
            )
        )
    text = (
        tr("wt.close_guard_header", title=meta.task_title)
        + "\n".join(bullets)
        + tr("wt.close_guard_footer")
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("wt.delete_anyway"),
                    callback_data=f"{CB_WT_DROP}{thread_id}"[:64],
                    style=KeyboardButtonStyle.DANGER,
                )
            ],
            [
                InlineKeyboardButton(
                    tr("wt.keep_agent"), callback_data=f"{CB_WT_KEEP}{thread_id}"[:64]
                )
            ],
        ]
    )
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            reply_markup=keyboard,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("worktree close-guard message failed: %s", e)


async def _handle_wt_drop(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """🧨 confirm — force-teardown a dirty/unmerged worktree agent."""
    thread_id = int(data[len(CB_WT_DROP) :])
    meta = session_manager.get_worktree_meta(user.id, thread_id)
    if meta is None:
        await query.answer(tr("wt.agent_gone"), show_alert=True)
        return
    await query.answer(tr("wt.deleting"))
    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    await teardown_worktree(context.bot, chat_id, user.id, thread_id, meta, force=True)
    try:
        await query.edit_message_text(tr("wt.agent_deleted", title=meta.task_title))
    except Exception:  # noqa: BLE001
        pass


async def _handle_wt_keep(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """↩ Оставить агента — dismiss the choice; the topic is already reopened
    and the agent is still alive and bound."""
    thread_id = int(data[len(CB_WT_KEEP) :])
    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    # Belt-and-braces: ensure the topic is open (no-op if it already is).
    try:
        await context.bot.reopen_forum_topic(
            chat_id=chat_id, message_thread_id=thread_id
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("reopen_forum_topic failed: %s", e)
    await query.answer(tr("wt.kept_toast"))
    try:
        await query.edit_message_text(tr("wt.kept_msg"))
    except Exception:  # noqa: BLE001
        pass


# --- 🗑 panel button (instant, in-app delete) -------------------------------


async def _handle_wt_del(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """🗑 Удалить агента — compute the guard and show a confirm on the panel."""
    wid = data[len(CB_WT_DEL) :]
    thread_id = get_thread_id(update)
    if thread_id is None:
        await query.answer(tr("wt.not_in_topic"), show_alert=True)
        return
    meta = session_manager.get_worktree_meta(user.id, thread_id)
    if meta is None:
        await query.answer(tr("wt.not_worktree_agent"), show_alert=True)
        return
    status = await wtc.worktree_status(
        Path(meta.repo), Path(meta.path), meta.base_branch, meta.branch
    )
    if wtc.decide_delete_safety(status) == "clean":
        caption = tr("wt.del_confirm_clean", title=meta.task_title)
        ok_label = tr("wt.del_ok_clean")
    else:
        bits: list[str] = []
        if status.dirty:
            bits.append(tr("wt.bits_dirty", count=status.dirty_files))
        if status.ahead is None or status.ahead > 0:
            bits.append(
                tr("wt.bits_ahead", count="?" if status.ahead is None else status.ahead)
            )
        caption = tr(
            "wt.del_confirm_dirty", title=meta.task_title, bits=", ".join(bits)
        )
        ok_label = tr("wt.del_ok_dirty")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ok_label,
                    callback_data=f"{CB_WT_DELOK}{thread_id}"[:64],
                    style=KeyboardButtonStyle.DANGER,
                )
            ],
            [
                InlineKeyboardButton(
                    tr("wt.cancel"), callback_data=f"{CB_WT_DELNO}{wid}"[:64]
                )
            ],
        ]
    )
    try:
        await query.edit_message_caption(caption=caption, reply_markup=keyboard)
    except Exception as e:  # noqa: BLE001
        logger.debug("wt delete confirm edit failed: %s", e)
    await query.answer()


async def _handle_wt_delok(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """Confirmed 🗑 — tear the agent down now (deletes the topic too)."""
    thread_id = int(data[len(CB_WT_DELOK) :])
    meta = session_manager.get_worktree_meta(user.id, thread_id)
    if meta is None:
        await query.answer(tr("wt.agent_gone"), show_alert=True)
        return
    await query.answer(tr("wt.deleting"))
    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    await teardown_worktree(context.bot, chat_id, user.id, thread_id, meta, force=True)
    # The topic (and this panel message) is gone now; best-effort note if not.
    try:
        await query.edit_message_caption(
            caption=tr("wt.agent_deleted", title=meta.task_title)
        )
    except Exception:  # noqa: BLE001
        pass


async def _handle_wt_delno(
    query: CallbackQuery,
    data: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
) -> None:
    """↩ Отмена — restore the agent panel caption + keyboard."""
    from telegram.helpers import escape_markdown

    from .commands import _build_commands_keyboard

    wid = data[len(CB_WT_DELNO) :]
    display = session_manager.get_display_name(wid)
    caption = tr("wt.panel_agent_header", name=escape_markdown(display, version=2))
    try:
        await query.edit_message_caption(
            caption=caption,
            parse_mode="MarkdownV2",
            reply_markup=_build_commands_keyboard(wid, tab="act"),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("wt delete cancel restore failed: %s", e)
    await query.answer(tr("wt.cancelled"))


# --- headless deletion (hard-deleted topic, no guard UI) --------------------


async def handle_deleted_worktree_topic(
    bot,
    user_id: int,
    thread_id: int,
    meta: WorktreeMeta,
) -> bool:
    """Cleanup for a *hard-deleted* worktree topic (no close event, no UI).

    A clean+merged worktree (⚪) has nothing to lose → full teardown, so
    "delete the topic" cleans up just like "close" does. A dirty/unmerged
    worktree (🟢/🟡) is preserved: flag it orphaned and return False so the
    caller does the standard window/binding teardown but leaves the work on
    disk for the phase-3 GC. Returns True iff it fully tore the worktree down.
    """
    status = await wtc.worktree_status(
        Path(meta.repo), Path(meta.path), meta.base_branch, meta.branch
    )
    if wtc.decide_delete_safety(status) == "clean":
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        await teardown_worktree(bot, chat_id, user_id, thread_id, meta, force=False)
        return True
    session_manager.mark_worktree_orphaned(user_id, thread_id)
    return False
