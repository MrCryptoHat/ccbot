"""Bot orchestrator — lifecycle, handler registration, and message routing.

Thin orchestrator that registers handlers from submodules and manages
the bot lifecycle. Delegates command/callback/media handling to:
  - handlers/commands.py: Command and topic lifecycle handlers
  - handlers/callbacks.py: Callback query dispatch
  - handlers/media.py: Photo, document, voice handlers

Keeps text_handler, handle_new_message, and _create_and_bind_window here
because they depend on multiple handler modules and serve as integration points.

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import logging
import re
from pathlib import Path

from telegram import (
    Bot,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from . import i18n
from . import plugins
from .config import config
from .hook import hook_installed_in_settings
from .rate_limiter import CcbotRateLimiter
from .handlers import get_thread_id, is_user_allowed
from .handlers.coalesce import coalesce_text
from .handlers.delivery import deliver_user_text
from .handlers.callbacks import callback_handler
from .handlers.commands import (
    _auto_bind_to_directory,
    _try_auto_bind_topic,
    commands_command,
    diff_command,
    esc_command,
    forward_command_handler,
    help_command,
    kill_command,
    lang_command,
    menu_button_dispatcher,
    bind_command,
    menu_command,
    react_command,
    tables_command,
    restart_command,
    screenshot_command,
    start_command,
    status_command,
    topic_closed_handler,
    topic_created_handler,
    pin_command,
    topic_edited_handler,
    unsupported_content_handler,
    voice_command,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    browse_start_path,
    build_directory_browser,
    build_window_picker,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    consume_pending_ask_tool_use,
    consume_pending_plan_text,
    consume_pending_prose_upgrade,
    get_interactive_msg_id,
    get_interactive_window,
    handle_interactive_ui,
    set_interactive_mode,
)
from .handlers.media import (
    document_handler,
    photo_handler,
    voice_handler,
)
from .rich_message import flatten_rich_message, is_rich_safe
from .handlers.message_queue import (
    enqueue_content_message,
    get_message_queue,
    shutdown_workers,
)
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    RICH_MESSAGE_MAX_CHARS,
    safe_edit,
    safe_reply,
    safe_send,
    send_with_fallback,
)
from .handlers.diff_view import capture_and_send as capture_and_send_diffs
from .runtimes import get_runtime
from .handlers.reaction_confirm import handle_message_reaction
from .handlers.task_pin import (
    pin_task_message,
    pinned_service_message_handler,
    should_pin_task,
)
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import status_poll_loop
from .markdown_v2 import convert_markdown
from .session import session_manager
from .session_monitor import NewMessage, SessionMonitor
from .terminal_parser import extract_bash_output, is_interactive_ui
from .tmux_manager import tmux_manager
from .transcribe import close_client as close_transcribe_client
from .voice import (
    build_on_directive,
    off_directive,
    check_runtime_dependencies as check_voice_dependencies,
    close_client as close_tts_client,
)
from aiohttp.web import AppRunner as _AiohttpAppRunner

from .inject.server import start_server as start_inject_server

logger = logging.getLogger(__name__)

# Tmux pane size pinned for screenshots. Wide enough that Claude Code's
# bottom status line (context %, subscription limits) doesn't wrap when
# capture-pane runs against a detached session (default 80x24).
PANE_COLS = 100
PANE_ROWS = 50

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

# /inject unix-socket server runner (None when CCBOT_INJECT_TOKEN is unset).
_inject_runner: _AiohttpAppRunner | None = None


# --- Text handler ---


def _topic_name_from_root(message: object) -> str | None:
    """Recover a forum topic's name from a message sent inside it.

    The Bot API has no ``getForumTopic``, and a plain in-topic message carries
    no name — but Telegram attaches the topic's root ``forum_topic_created``
    service message as ``reply_to_message`` (unless the user explicitly replied
    to some other message in the topic, in which case there's nothing to
    recover and we fail open). This is the *creation-time* name: a topic
    renamed afterwards still reports the original here — same semantics as the
    create-time auto-bind in ``topic_created_handler``.
    """
    rtm = getattr(message, "reply_to_message", None)
    if rtm is None:
        return None
    ftc = getattr(rtm, "forum_topic_created", None)
    if ftc is None:
        return None
    name = (getattr(ftc, "name", None) or "").strip()
    return name or None


async def _forward_pending_after_autobind(
    wid: str, user_id: int, thread_id: int, text: str
) -> None:
    """Send ``text`` to a freshly auto-bound window, applying any voice directive."""
    directive = session_manager.consume_voice_directive(user_id, thread_id)
    if directive == "on":
        text = f"{build_on_directive()}\n\n---\n{text}"
    elif directive == "off":
        text = f"{off_directive()}\n\n---\n{text}"
    ok, msg = await session_manager.send_to_window(wid, text)
    if not ok:
        logger.warning("Auto-bind: failed to forward pending text: %s", msg)


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text_override: str | None = None,
) -> None:
    """Route an inbound text message. ``text_override`` substitutes the message
    text for updates whose content isn't in ``message.text`` (rich messages
    flattened by rich_message_handler) — everything else (binding, states,
    coalescing, delivery) runs identically."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(
                update.message,
                i18n.tr("common.not_authorized", uid=user.id if user else "?"),
            )
        return

    if not update.message or not (text_override or update.message.text):
        return

    thread_id = get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Awaiting a worktree task name? Consume it and provision (returns True
    # when handled, before any normal routing).
    from .handlers.worktrees import consume_worktree_name

    if await consume_worktree_name(update, context):
        return

    text = text_override or update.message.text
    assert text is not None  # narrowed by the guard above

    # Ignore text in picker/browser modes (only for the same thread)
    _STATE_CHECKS: list[tuple[str, str, object]] = [
        (
            STATE_SELECTING_WINDOW,
            i18n.tr("bot.use_picker_above"),
            clear_window_picker_state,
        ),
        (
            STATE_BROWSING_DIRECTORY,
            i18n.tr("bot.use_picker_above"),
            clear_browse_state,
        ),
        (
            STATE_SELECTING_SESSION,
            i18n.tr("bot.use_picker_above"),
            clear_session_picker_state,
        ),
    ]
    for state_val, prompt_msg, clear_fn in _STATE_CHECKS:
        if context.user_data and context.user_data.get(STATE_KEY) == state_val:
            pending_tid = context.user_data.get("_pending_thread_id")
            if pending_tid == thread_id:
                await safe_reply(update.message, prompt_msg)
                return
            # Stale state from a different thread — clear it
            clear_fn(context.user_data)  # type: ignore[operator]
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
            if state_val == STATE_SELECTING_SESSION:
                context.user_data.pop("_selected_path", None)

    # Must be in a named topic
    if thread_id is None:
        # «Create a topic» is impossible advice in a DM or a non-forum group —
        # point at the actual next step instead.
        if chat and chat.type == "private":
            await safe_reply(update.message, i18n.tr("bot.private_start"))
        elif chat and not getattr(chat, "is_forum", False):
            await safe_reply(update.message, i18n.tr("bot.enable_topics_hint"))
        else:
            await safe_reply(update.message, i18n.tr("bot.use_named_topic"))
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        # 1. Learned memory (most reliable, name-independent): this topic was
        #    bound to a directory before — its window since died / tmux
        #    restarted / the topic was renamed. Rebind to the same folder by
        #    its permanent thread_id, no matter what the topic is called now.
        remembered = session_manager.get_remembered_directory(user.id, thread_id)
        if remembered and Path(remembered).is_dir():
            if context.user_data is not None:
                context.user_data["_pending_thread_text"] = text
            await _auto_bind_to_directory(
                user.id, thread_id, Path(remembered), update.message, context
            )
            new_wid = session_manager.get_window_for_thread(user.id, thread_id)
            if new_wid is not None:
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_text", None)
                await _forward_pending_after_autobind(new_wid, user.id, thread_id, text)
            logger.info(
                "Rebound pre-existing topic by memory → %s (user=%d, thread=%d)",
                remembered,
                user.id,
                thread_id,
            )
            return

        # 2. Name-based auto-bind: recover the topic name from the root service
        #    message Telegram attaches as reply_to_message (_topic_name_from_root)
        #    and match it to a folder/agent. Convenience for topics never bound
        #    before whose name happens to equal a folder/agent name.
        topic_name = _topic_name_from_root(update.message)
        if topic_name:
            # Stash the pending text so the session-picker branch of
            # _try_auto_bind_topic (deferred bind) forwards it after the user
            # picks; for an immediate bind we forward it ourselves below.
            if context.user_data is not None:
                context.user_data["_pending_thread_text"] = text
            bound = await _try_auto_bind_topic(
                user.id, thread_id, topic_name, update.message, context
            )
            if bound:
                new_wid = session_manager.get_window_for_thread(user.id, thread_id)
                if new_wid is not None:
                    # Immediate bind (docker agent or fresh tmux window) —
                    # forward now; the session picker path leaves wid None and
                    # forwards on selection instead.
                    if context.user_data is not None:
                        context.user_data.pop("_pending_thread_text", None)
                    await _forward_pending_after_autobind(
                        new_wid, user.id, thread_id, text
                    )
                logger.info(
                    "Auto-bound pre-existing topic %r by name (user=%d, thread=%d)",
                    topic_name,
                    user.id,
                    thread_id,
                )
                return
            # Name didn't match a dir/agent — drop the stash and fall through.
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_text", None)

        # Unbound topic — check for unbound windows first
        all_windows = await tmux_manager.list_windows()
        bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
        unbound = [
            (w.window_id, w.window_name, w.cwd)
            for w in all_windows
            if w.window_id not in bound_ids
        ]
        logger.debug(
            "Window picker check: all=%s, bound=%s, unbound=%s",
            [w.window_name for w in all_windows],
            bound_ids,
            [name for _, name, _ in unbound],
        )

        if unbound:
            # Show window picker
            logger.info(
                "Unbound topic: showing window picker (%d unbound windows, user=%d, thread=%d)",
                len(unbound),
                user.id,
                thread_id,
            )
            msg_text, keyboard, win_ids = build_window_picker(unbound)
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
                context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
                context.user_data["_pending_thread_id"] = thread_id
                context.user_data["_pending_thread_text"] = text
            await safe_reply(update.message, msg_text, reply_markup=keyboard)
            return

        # No unbound windows — show directory browser to create a new session
        logger.info(
            "Unbound topic: showing directory browser (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        start_path = browse_start_path()
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data["_pending_thread_id"] = thread_id
            context.user_data["_pending_thread_text"] = text
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    # Coalesce a Telegram-split long paste back into one prompt. A message over
    # 4096 chars is split by the *client* into several Updates; delivered one by
    # one the agent takes the first as a turn and the rest queue as separate
    # prompts (so it sees only a slice). Near-ceiling fragments are buffered and
    # the reassembled text is forwarded once; ordinary messages pass straight
    # through with no delay. See handlers/coalesce.py.
    await coalesce_text(
        user.id,
        thread_id,
        update.message.message_id,
        text,
        lambda full_text: _forward_text_to_agent(
            update, context, user.id, thread_id, wid, full_text
        ),
    )


async def _forward_text_to_agent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    thread_id: int,
    wid: str,
    text: str,
) -> None:
    """Forward one (possibly reassembled) user message to the bound agent.

    The bound-topic tail of text_handler: stale-binding check, pending
    interactive-UI detection, the shared deliver_user_text pre-send pipeline,
    then post-send bash-capture / interactive-refresh. Invoked by coalesce_text
    — immediately for an ordinary message, or once per reassembled split paste.
    """
    if update.message is None:
        return

    # Bound topic — stale-check then forward. Branch by binding type:
    # docker bindings have no tmux window to look up; calling
    # find_window_by_id("docker:<agent>") always returns None and would
    # spuriously unbind a perfectly healthy container binding.
    if session_manager._is_docker_binding(wid):
        from .docker_driver import docker_driver

        agent_name = wid[len("docker:") :]
        agent = config.get_docker_agent(agent_name)
        container_alive = bool(
            agent and await docker_driver.is_container_alive(agent.container)
        )
        if not container_alive:
            logger.info(
                "Stale binding: docker agent %s not running, unbinding "
                "(user=%d, thread=%d)",
                agent_name,
                user_id,
                thread_id,
            )
            session_manager.unbind_thread(user_id, thread_id)
            await safe_reply(
                update.message,
                i18n.tr("bot.docker_agent_down", name=agent_name),
            )
            return
    else:
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            display = session_manager.get_display_name(wid)
            logger.info(
                "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
                display,
                user_id,
                thread_id,
            )
            session_manager.unbind_thread(user_id, thread_id)
            await safe_reply(
                update.message,
                i18n.tr("bot.window_gone", name=display),
            )
            return

    await update.message.chat.send_action(ChatAction.TYPING)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user_id, thread_id)

    # Pending interactive-UI detection — works for both tmux and docker
    # bindings via session_manager.capture_pane (it branches by binding
    # type internally). Docker matters because Claude Code's JSONL only
    # records AskUserQuestion as a tool_use *after* the user answers it,
    # so without this poll the photo never reaches Telegram and the user
    # types blind into a TUI.
    pane_text = await session_manager.capture_pane(wid)

    # Task-pin (/pin): decide on the PRE-send pane — after the send the agent
    # is working on this very message and the idle check would always fail.
    pin_candidate = await should_pin_task(
        user_id, thread_id, wid, text, pane_text=pane_text
    )

    if pane_text and is_interactive_ui(pane_text):
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, thread=%s)",
            user_id,
            thread_id,
        )
        await handle_interactive_ui(context.bot, user_id, wid, thread_id)
        await asyncio.sleep(0.3)

    # Shared pre-send pipeline: AskUserQuestion auto-route, guard against
    # typing into other interactive widgets (Enter would silently confirm
    # the highlighted option — e.g. grant a permission), voice directive.
    status, detail = await deliver_user_text(
        user_id,
        thread_id,
        wid,
        text,
        ack_chat_id=update.message.chat.id,
        ack_message_id=update.message.message_id,
    )
    if status == "routed":
        try:
            await update.message.chat.send_action(
                action=ChatAction.TYPING, message_thread_id=thread_id
            )
        except Exception:
            pass
        return
    if status == "blocked_no_text_option":
        await safe_reply(
            update.message,
            i18n.tr("bot.no_free_text_option"),
        )
        return
    if status == "blocked_widget":
        await safe_reply(
            update.message,
            i18n.tr("bot.agent_waiting_in_dialog"),
        )
        return
    if status == "error":
        await safe_reply(update.message, f"❌ {detail}")
        return

    if pin_candidate:
        await pin_task_message(
            context.bot, update.message.chat.id, update.message.message_id
        )

    # Send typing indicator
    try:
        await update.message.chat.send_action(
            action=ChatAction.TYPING, message_thread_id=thread_id
        )
    except Exception:
        pass

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user_id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user_id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user_id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user_id, wid, thread_id)


# --- Bash capture ---

_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane."""
    try:
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


# --- Window creation helper ---


async def rich_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Bot API 10.1 rich messages (tables / headings / lists composed in the
    Telegram client). PTB ≤ Bot API 10.0 doesn't parse ``Message.rich_message``
    — the payload surfaces raw in ``api_kwargs`` and ``message.text`` is None,
    so without this handler such messages fell through to
    unsupported_content_handler («stickers can't be forwarded»). Flatten the
    block tree to markdown and run the normal text path.
    """
    if not update.message:
        return
    rich = update.message.api_kwargs.get("rich_message")
    text = flatten_rich_message(rich)
    if not text:
        # Nothing textual salvaged (e.g. a pure media collage) — the honest
        # answer is the regular unsupported-content reply.
        await unsupported_content_handler(update, context)
        return
    logger.info("Rich message flattened to %d chars of markdown", len(text))
    await text_handler(update, context, text_override=text)


class _RichMessageFilter(filters.MessageFilter):
    """Matches messages carrying an unparsed Bot API 10.1 rich_message."""

    def filter(self, message: object) -> bool:
        api_kwargs = getattr(message, "api_kwargs", None) or {}
        return bool(api_kwargs.get("rich_message"))


rich_message_filter = _RichMessageFilter(name="ccbot.rich_message")


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None = None,
    runtime: str = "claude",
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by the session picker's resume tap (CB_SESSION_SELECT → resume the
    active runtime's session) and its "➕ New session" button (CB_RUNTIME_SELECT
    → fresh window on the chosen runtime). ``runtime`` selects the agent CLI
    ("claude" default / "codex"); it is tagged onto the window state and drives
    launch + tracking.
    """
    from telegram import CallbackQuery, User

    from .runtimes import get_runtime

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    rt = get_runtime(runtime)

    # A runtime without a session_map resolves its transcript by cwd ("newest
    # wins") — a second live window on the same directory would cross-talk
    # with the first topic. Refuse it up front with an explanation.
    if not rt.uses_session_map and await session_manager.has_live_agent_on_cwd(
        rt.name, str(selected_path)
    ):
        await safe_edit(
            query,
            i18n.tr("bot.same_dir_conflict", agent=rt.display_name, dir=selected_path),
        )
        return

    success, message, created_wname, created_wid = await tmux_manager.create_window(
        selected_path, resume_session_id=resume_session_id, runtime=rt.name
    )
    if success:
        # Stamp runtime (+cwd for hookless runtimes) before anything can poll.
        session_manager.tag_window_runtime(created_wid, rt.name, str(selected_path))

        logger.info(
            "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s, runtime=%s)",
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
            resume_session_id,
            rt.name,
        )

        if not rt.uses_session_map:
            # No SessionStart hook for this runtime (Codex: the monitor tracks
            # it by cwd→rollout matching instead): nothing to wait on, and the
            # "hook missing" alarm below must not fire.
            hook_ok = False
        else:
            hook_timeout = 15.0 if resume_session_id else 5.0
            hook_ok = await session_manager.wait_for_session_map_entry(
                created_wid, timeout=hook_timeout
            )

            if resume_session_id:
                ws = session_manager.get_window_state(created_wid)
                if not hook_ok:
                    logger.warning(
                        "Hook timed out for resume window %s, "
                        "manually setting session_id=%s cwd=%s",
                        created_wid,
                        resume_session_id,
                        selected_path,
                    )
                    ws.session_id = resume_session_id
                    ws.cwd = str(selected_path)
                    ws.window_name = created_wname
                    session_manager._save_state()
                elif ws.session_id != resume_session_id:
                    logger.info(
                        "Resume override: window %s session_id %s -> %s",
                        created_wid,
                        ws.session_id,
                        resume_session_id,
                    )
                    ws.session_id = resume_session_id
                    session_manager._save_state()

        if pending_thread_id is not None:
            session_manager.bind_thread(
                user.id, pending_thread_id, created_wid, window_name=created_wname
            )
            # Remember directory + runtime so this topic auto-rebinds to the
            # SAME agent CLI next time (after its window dies / tmux restarts)
            # without the dir browser.
            session_manager.record_thread_directory(
                user.id, pending_thread_id, str(selected_path), runtime=rt.name
            )

            # The topic name is the USER's label — never renamed to the window
            # name (dedup like `demo-api-2` stays internal; a custom topic name
            # like `codex-play` must survive binding). Routing never depends on
            # the topic name: rebinds go through thread_directory_memory.
            resolved_chat = session_manager.resolve_chat_id(user.id, pending_thread_id)

            shown_dir = str(selected_path)
            home_prefix = str(Path.home())
            if shown_dir.startswith(home_prefix):
                shown_dir = "~" + shown_dir[len(home_prefix) :]
            ready_key = rt.ready_message_key(resumed=bool(resume_session_id))
            await safe_edit(query, i18n.tr(ready_key, dir=shown_dir))

            # First-run trap: without the SessionStart hook the monitor never
            # learns this window's session_id, so the agent's replies silently
            # never reach the chat. Warn in-topic only when the hook is
            # definitively absent from settings.json (a merely-slow hook that
            # missed the wait window above must not raise a false alarm).
            # Hookless runtimes (Codex) are tracked by cwd — skip.
            if rt.uses_session_map and not hook_ok and not hook_installed_in_settings():
                await safe_send(
                    context.bot,
                    resolved_chat,
                    i18n.tr("bot.hook_missing"),
                    message_thread_id=pending_thread_id,
                )

            pending_text = (
                context.user_data.get("_pending_thread_text")
                if context.user_data
                else None
            )
            # Auto-forward the pending first message only when the runtime
            # declares it safe (auto_forward_first_message): a CLI that can
            # open on an interactive startup screen (Codex's sign-in menu)
            # must not get blind typing + Enter — that would take a step the
            # user didn't choose. The pending keys are just cleared below.
            if pending_text and rt.auto_forward_first_message:
                logger.debug(
                    "Forwarding pending text to window %s (len=%d)",
                    created_wname,
                    len(pending_text),
                )
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_text", None)
                    context.user_data.pop("_pending_thread_id", None)
                directive = session_manager.consume_voice_directive(
                    user.id, pending_thread_id
                )
                if directive == "on":
                    pending_text = f"{build_on_directive()}\n\n---\n{pending_text}"
                elif directive == "off":
                    pending_text = f"{off_directive()}\n\n---\n{pending_text}"
                send_ok, send_msg = await session_manager.send_to_window(
                    created_wid,
                    pending_text,
                )
                if not send_ok:
                    logger.warning("Failed to forward pending text: %s", send_msg)
                    await safe_send(
                        context.bot,
                        resolved_chat,
                        i18n.tr("bot.pending_send_failed", err=send_msg),
                        message_thread_id=pending_thread_id,
                    )
            elif context.user_data is not None:
                # No forward (no pending text, or a runtime we must not
                # auto-type into) — drop both pending keys so they don't leak
                # into a later flow.
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
        else:
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
    await query.answer(i18n.tr("cb.done") if success else i18n.tr("cb.failed"))


# callback_handler is imported from handlers.callbacks


# --- Streaming response / notifications ---


async def _bump_read_offset_to_eof(user_id: int, wid: str) -> None:
    """Mark this binding's JSONL as delivered up to its current end for ``user_id``.

    Used after we intentionally *don't* forward a JSONL message (the interactive
    photo carries it, or it's a de-duplicated AskUserQuestion copy) so the
    history pager (``handlers/history.py``) doesn't show it again.
    """
    file_path = session_manager.resolve_session_file_for_window(wid)
    if file_path is not None:
        try:
            file_size = file_path.stat().st_size
            session_manager.update_user_window_offset(user_id, wid, file_size)
        except OSError:
            pass


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session
    active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    for user_id, wid, thread_id in active_users:
        # Context-fill alert (synthetic, emitted by the monitor's token-
        # threshold check — not real agent output). Deliver as a ringing
        # text notice and skip all the assistant-output handling below.
        if msg.content_type == "context_alert":
            await enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_id=wid,
                parts=[msg.text],
                content_type="context_alert",
                thread_id=thread_id,
            )
            continue

        # Any inbound event means Claude is mid-turn — refresh the generation
        # flag so the typing heartbeat stays alive during long tool phases.
        session_manager.mark_generating(wid)

        # Handle interactive tools specially - capture terminal and send UI
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            # AskUserQuestion's text (prose + question) was already surfaced from
            # the pane before the user answered (interactive_ui). The tool_use
            # only reaches JSONL *after* the answer, so re-sending it here would
            # just duplicate that surfaced text — skip it (the widget is gone,
            # so there's no photo to render either).
            if msg.tool_name == "AskUserQuestion" and consume_pending_ask_tool_use(
                msg.session_id
            ):
                await _bump_read_offset_to_eof(user_id, wid)
                continue
            # ExitPlanMode tool_use reaching JSONL = the widget was answered.
            # Its plan text (if any) rode the preceding is_plan_text entry and
            # was consumed there; this pop only covers the empty-plan edge so
            # the bookkeeping can't linger.
            if msg.tool_name == "ExitPlanMode":
                consume_pending_plan_text(msg.session_id)
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Flush pending messages (e.g. plan content) in THIS topic's
            # queue before sending the interactive UI. Other topics' queues
            # aren't relevant — the user is seeing this one. Bounded: this
            # runs on the monitor loop, and an unbounded join() during a
            # long flood ban would freeze delivery for every session.
            queue = get_message_queue(user_id, thread_id)
            if queue:
                try:
                    await asyncio.wait_for(queue.join(), timeout=10)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Queue flush before interactive UI timed out "
                        "(user=%d thread=%s)",
                        user_id,
                        thread_id,
                    )
            # Wait briefly for Claude Code to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid, thread_id)
            if handled:
                # Interactive UI on screen → Claude is waiting for user, not
                # working. Stop typing until user taps a nav key (send_to_window
                # will re-arm the flag).
                session_manager.mark_idle(wid)
                await _bump_read_offset_to_eof(user_id, wid)
                continue  # Don't send the normal tool_use message
            else:
                # UI not rendered — clear the early-set mode
                clear_interactive_mode(user_id, thread_id)

        # A user message means the interaction was resolved by typing
        # (instead of tapping the keyboard) — tear down the UI message.
        # Assistant text must NOT clear the UI: e.g. ExitPlanMode writes
        # the plan body into the JSONL as an assistant message BEFORE the
        # approval prompt stabilises in the pane; clearing on that wipes
        # the still-pending photo. When the prompt is truly resolved the
        # pane loses the interactive markers and status_polling clears
        # the message on its next tick (≤1s).
        if get_interactive_msg_id(user_id, thread_id) and msg.role == "user":
            await clear_interactive_msg(user_id, bot, thread_id)

        # Assistant prose that immediately precedes an AskUserQuestion call:
        # interactive_ui already surfaced it from the pane (before the user
        # answered, since JSONL holds the whole turn until then). Now that the
        # clean markdown version has landed in JSONL, upgrade that message in
        # place — links and formatting flattened by the pane render come back —
        # instead of sending a duplicate.
        if (
            msg.content_type == "text"
            and msg.role == "assistant"
            and msg.precedes_interactive_prompt
            and await consume_pending_prose_upgrade(
                bot, msg.session_id, user_id, thread_id, msg.text
            )
        ):
            await _bump_read_offset_to_eof(user_id, wid)
            continue

        # The ExitPlanMode plan text landing in JSONL after the user answered:
        # interactive_ui already surfaced the full plan from the plan FILE
        # while the approval widget was up (the turn is held out of JSONL
        # until the answer), so this copy would be a duplicate. Consume-miss
        # (surfacing failed / bot restarted) → delivered normally, as before.
        if (
            msg.content_type == "text"
            and msg.role == "assistant"
            and msg.is_plan_text
            and consume_pending_plan_text(msg.session_id)
        ):
            logger.info(
                "Plan JSONL copy skipped (surfaced pre-approval): session=%s",
                msg.session_id[:8],
            )
            await _bump_read_offset_to_eof(user_id, wid)
            continue

        # Filter notifications based on config. (The "typing…" indicator is
        # kept alive by the 1 s status poll's heartbeat — `mark_generating`
        # above re-arms it on every inbound event — so we don't fire
        # `send_chat_action` per message here: Telegram's own guidance is
        # "no more often than every 5 s", and a tool-heavy agent would blow
        # far past that, eating into the chat's rate budget for nothing.)
        # Tool plumbing (tool_use, tool_result, thinking) is always
        # suppressed from the chat — the user gets only the agent's text
        # replies. The legacy "show as status spinner" fallback for the
        # show_tool_calls/show_tool_results=false configs has been
        # removed: it put "🔧 Bash(...)" into the chat anyway, which is
        # exactly what we now reject. /screenshot covers tool history;
        # the typing… indicator covers liveness.
        if msg.content_type in ("tool_use", "tool_result"):
            # Opt-in /diff: an edit-tool call → screenshot the native diff
            # block(s) Claude Code drew in the pane (dedup'd, so re-scanning
            # is harmless). Triggered on tool_use; the ~2s monitor lag means
            # the diff has rendered by now, and a final-text re-scan (below)
            # backstops the last edit of a turn.
            if (
                msg.content_type == "tool_use"
                and session_manager.is_diff_mode(user_id, thread_id)
                and get_runtime(session_manager.window_runtime(wid)).is_edit_tool(
                    msg.tool_name
                )
            ):
                await capture_and_send_diffs(bot, user_id, wid, thread_id)
            # Tool plumbing (the "🔧 Bash(...)" text) stays out of the chat,
            # but images the agent is *looking at* — screenshots it Read,
            # browser captures, Bash-generated plots — are substantive and
            # surfaced so the user sees what the agent sees. They ride on a
            # tool_result entry as image_data; the queue's _process_content_task
            # short-circuits tool_result to images-only (no text). Without this
            # branch the blanket skip below would drop them, which is what
            # silently broke "images go through" (no image ever reached chat).
            if msg.image_data:
                await enqueue_content_message(
                    bot=bot,
                    user_id=user_id,
                    window_id=wid,
                    parts=[],
                    tool_use_id=msg.tool_use_id,
                    content_type=msg.content_type,
                    thread_id=thread_id,
                    image_data=msg.image_data,
                    entry_ts_iso=msg.entry_ts_iso,
                )
                await _bump_read_offset_to_eof(user_id, wid)
            continue
        if not config.show_thinking and msg.content_type == "thinking":
            continue

        # Detect (send file: /path) pattern in assistant text and send as document
        if msg.content_type == "text" and msg.role == "assistant":
            file_matches = re.findall(r"\(send file: ([^)]+)\)", msg.text)
            if file_matches:
                from .handlers.message_sender import send_document

                chat_id = session_manager.resolve_chat_id(user_id, thread_id)
                send_kwargs = {}
                if thread_id is not None:
                    send_kwargs["message_thread_id"] = thread_id
                for fpath in file_matches:
                    raw = fpath.strip()
                    resolved = session_manager.resolve_agent_file_path(wid, raw)
                    if resolved is None:
                        logger.warning(
                            "send-file rejected (binding=%s, path=%r) — "
                            "outside /workspace whitelist or unknown agent",
                            wid,
                            raw,
                        )
                        continue
                    if resolved.is_file():
                        await send_document(bot, chat_id, str(resolved), **send_kwargs)
                msg.text = re.sub(r"\(send file: [^)]+\)\s*", "", msg.text).strip()
                if not msg.text:
                    continue

        parts, table_texts, code_files = build_response_parts(
            msg.text,
            msg.is_complete,
            msg.content_type,
            msg.role,
        )

        # Rich-first (Bot API 10.2): exactly where the legacy pipeline
        # degrades — a table/box-art became a PNG (table_texts), long code
        # became a file attachment (code_files), or the reply split into
        # [i/N] pages — carry the ORIGINAL markdown alongside; the queue
        # worker tries one native sendRichMessage and falls back to the
        # legacy parts on any failure. Plain short replies keep the proven
        # MarkdownV2 path untouched, as do texts is_rich_safe rejects
        # (Telegram accepts but MANGLES those — no error to fall back on).
        rich_markdown = ""
        if (
            config.rich_messages_enabled
            and msg.content_type == "text"
            and msg.role == "assistant"
            and len(msg.text) <= RICH_MESSAGE_MAX_CHARS
            and (table_texts or code_files or len(parts) > 1)
            and is_rich_safe(msg.text)
            # /tables=image: extracted tables/box-art must arrive as PNGs, so
            # a message carrying any skips whole-message rich (which would
            # render them natively) and takes the legacy parts.
            and not (table_texts and session_manager.table_style == "image")
            # Drawn blocks (box-art / dir trees, rich_markdown="") must arrive
            # as PNGs regardless of /tables style: a rich code block wraps on
            # the phone — the very soup the PNG path exists to avoid.
            and not any(not getattr(t, "rich_markdown", "") for t in table_texts)
        ):
            rich_markdown = msg.text

        if msg.is_complete:
            # Send typing indicator for text content
            if msg.content_type == "text":
                chat_id = session_manager.resolve_chat_id(user_id, thread_id)
                typing_kwargs = {}
                if thread_id is not None:
                    typing_kwargs["message_thread_id"] = thread_id
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id,
                        action=ChatAction.TYPING,
                        **typing_kwargs,
                    )
                except Exception:
                    pass

            await enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_id=wid,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                content_type=msg.content_type,
                thread_id=thread_id,
                image_data=msg.image_data,
                entry_ts_iso=msg.entry_ts_iso,
                table_texts=table_texts,
                code_files=code_files,
                rich_markdown=rich_markdown,
            )

            # Final assistant text → turn done. Typing heartbeat stops; the
            # reply message itself clears the indicator on the client side.
            # If Claude follows up with more tools/text, the next event's
            # mark_generating refresh (top of loop) brings typing back.
            if msg.content_type == "text" and msg.role == "assistant":
                session_manager.mark_idle(wid)
                # Opt-in /diff: backstop scan on the turn's final text — the
                # last edit's block has definitely rendered by now. Dedup
                # means already-sent blocks aren't re-sent.
                if session_manager.is_diff_mode(user_id, thread_id):
                    await capture_and_send_diffs(bot, user_id, wid, thread_id)

            await _bump_read_offset_to_eof(user_id, wid)


# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task

    from telegram import (
        BotCommandScopeAllGroupChats,
        BotCommandScopeDefault,
        MenuButtonCommands,
    )

    from .handlers.commands import apply_bot_commands

    # Clear old commands from all scopes
    await application.bot.delete_my_commands(scope=BotCommandScopeDefault())
    await application.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())

    # Publish the /command menu in the active UI language (loaded from state
    # by SessionManager). /lang re-publishes it on switch via the same helper.
    # Order is curated inside build_bot_commands — most-used at the top.
    await apply_bot_commands(application.bot)
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    await session_manager.resolve_stale_ids()
    # Sweep worktree_meta rows whose worktree is gone and thread is now unbound
    # (leftovers from manual teardown / lost state) — after stale-id resolution.
    session_manager.reconcile_worktree_meta()

    # Pin tmux pane size so /screenshot doesn't wrap Claude Code's footer.
    # Detached tmux sessions otherwise default to 80x24, which clips the
    # context/limits status bar.
    await tmux_manager.ensure_session_pane_size(cols=PANE_COLS, rows=PANE_ROWS)
    from .docker_driver import docker_driver as _dd

    for agent in config.active_docker_agents():
        if await _dd.is_container_alive(agent.container):
            await _dd.ensure_pane_size(agent.container, cols=PANE_COLS, rows=PANE_ROWS)

    check_voice_dependencies()

    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

    # Optional plugins declared in CCBOT_PLUGINS (mail bus, gateways, live
    # dashboards, …). Each starts its own servers/tasks; absent ones skipped.
    await plugins.on_startup(application)

    # /inject unix-socket listener — only started when CCBOT_INJECT_TOKEN is
    # set. Empty token disables the endpoint, so this is a no-op on hosts
    # without the inject endpoint wired up.
    global _inject_runner
    if config.inject.is_enabled():
        try:
            _inject_runner = await start_inject_server(config.inject)
        except OSError as e:
            # Socket bind failure (stale path, perms) — log and continue
            # without the endpoint rather than crashing the whole bot.
            logger.error("inject server failed to start: %s", e)
            _inject_runner = None
    else:
        logger.info("inject endpoint disabled (CCBOT_INJECT_TOKEN unset)")


async def post_shutdown(application: Application) -> None:
    global _status_poll_task

    await plugins.on_shutdown()

    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    global _inject_runner
    if _inject_runner is not None:
        await _inject_runner.cleanup()
        _inject_runner = None
        # aiohttp doesn't unlink the unix socket on cleanup; remove it so
        # the next boot binds cleanly (start_server also unlinks defensively).
        try:
            config.inject.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("inject: failed to unlink socket on shutdown: %s", e)
        logger.info("inject server stopped")

    await shutdown_workers()

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")

    # Drain any pending async state writes submitted during runtime.
    # Without this, the process can exit while the
    # last snapshot is still sitting in the writer queue and the on-disk
    # state.json lags one edit behind reality.
    from .utils import shutdown_async_writer

    shutdown_async_writer()

    await close_transcribe_client()
    await close_tts_client()


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Application-wide error handler.

    Without one, PTB logs every escaped handler exception under a generic
    "No error handlers are registered" banner with zero update context.
    Transient network errors (httpx timeouts on a high-latency link)
    are routine — one WARNING line; everything else gets the traceback
    plus enough context to find the topic.
    """
    err = context.error
    if isinstance(err, NetworkError):
        # Covers TimedOut too. The poller retries by itself; sends have
        # their own fallbacks. A traceback per blip is just noise.
        logger.warning("Telegram network error: %s", err)
        return
    desc = ""
    if isinstance(update, Update):
        chat = update.effective_chat
        desc = f" (chat={chat.id if chat else '?'}, thread={get_thread_id(update)})"
    logger.error("Unhandled error in handler%s", desc, exc_info=err)


def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        # Dispatch updates concurrently so a long-running handler in one
        # topic doesn't stall handlers for unrelated topics. Safe here
        # because per-(user, thread) state is guarded by its own locks,
        # and the single-user model rules out cross-user interference.
        .concurrent_updates(True)
        # group_max_rate=15/60s = Telegram's documented ~20 msg/min-per-group
        # with headroom. Stream traffic (agent output + status edits) all keys
        # on the supergroup's chat_id (CcbotRateLimiter doesn't split per topic
        # — topics share Telegram's per-chat budget), so the bot self-throttles
        # below the real limit instead of getting 429'd and freezing.
        .rate_limiter(
            CcbotRateLimiter(max_retries=5, group_max_rate=15, group_time_period=60)
        )
        # PTB's defaults (read=write=5s) are fragile on a high-latency link:
        # an interactive-UI photo send-photo over a freshly-restarted process
        # was timing out client-side at 5s while Telegram still committed the
        # upload server-side, producing a duplicate when status_polling's
        # next tick retried. Bumping read/write to 30s and media_write to 60s
        # absorbs slow uploads without changing fast-path latency.
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .media_write_timeout(60)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_error_handler(_on_error)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("kill", kill_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("commands", commands_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(CommandHandler("react", react_command))
    application.add_handler(CommandHandler("tables", tables_command))
    application.add_handler(CommandHandler("diff", diff_command))
    application.add_handler(CommandHandler("pin", pin_command))
    application.add_handler(CommandHandler("lang", lang_command))
    application.add_handler(CommandHandler("bind", bind_command))
    application.add_handler(CommandHandler("menu", menu_command))
    plugins.register_handlers(application)
    application.add_handler(CallbackQueryHandler(callback_handler))
    if config.reaction_confirm_enabled:
        application.add_handler(MessageReactionHandler(handle_message_reaction))
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED,
            topic_created_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED,
            topic_edited_handler,
        )
    )
    # Clean up the «pinned a message» service line for the bot's own /pin
    # task pins (a user's manual pins keep theirs).
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.PINNED_MESSAGE,
            pinned_service_message_handler,
        )
    )
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    # Persistent ReplyKeyboard buttons — taps arrive as plain text labels.
    # Match EVERY language's label (built once from the full catalog): the
    # regex is fixed at registration, but a /lang switch must keep routing
    # whichever label the client's still-shown keyboard carries.
    _menu_labels = i18n.all_variants("menu.server") + i18n.all_variants("menu.agent")
    _menu_regex = "^(" + "|".join(re.escape(lbl) for lbl in _menu_labels) + ")$"
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(_menu_regex),
            menu_button_dispatcher,
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Bot API 10.1 rich messages (client-composed tables/headings): PTB doesn't
    # parse them, message.text is None — MUST precede the unsupported catch-all
    # or they bounce with «stickers can't be forwarded».
    application.add_handler(
        MessageHandler(rich_message_filter & ~filters.COMMAND, rich_message_handler)
    )
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
