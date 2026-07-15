"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Backstop probe of topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins → Topic_id_invalid for a deleted topic →
    purge_deleted_topic). One topic per TOPIC_CHECK_INTERVAL, round-robin, in
    background_context() — that call is a chat-management endpoint Telegram
    rate-limits, and a 429 on *any* request makes AIORateLimiter's RetryAfter
    pause every other send, so it must never burst. Active topics' deletions
    are caught immediately by the queue worker (a real send bounces with
    Topic_id_invalid → same purge_deleted_topic); this probe only covers idle
    topics with no traffic at all.
  - Janitor for orphan tmux windows: any non-__main__ window with no thread
    binding is killed after ORPHAN_WINDOW_GRACE. Closes the gap when topics
    were deleted while ccbot was offline (Topic_id_invalid probe never fires)
    or when bind_thread overwrote a previous binding without killing the old
    window.

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - ORPHAN_WINDOW_GRACE: Delay before killing an unbound window (90 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import BadRequest

from ..i18n import tr
from ..rate_limiter import background_context
from ..runtimes import get_runtime
from ..session import session_manager
from ..terminal_parser import (
    detect_model_switch,
    is_interactive_ui,
)
from ..tmux_manager import tmux_manager
from .reaction_emit import maybe_fire as fire_reaction_ack
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .cleanup import clear_topic_state, purge_deleted_topic
from .message_queue import get_message_queue
from .message_sender import is_topic_gone_error, safe_send

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe: one bound topic gets probed every this many seconds,
# round-robin (so with N topics each is probed every N*TOPIC_CHECK_INTERVAL s).
# Only a backstop for idle topics — an active topic's deletion is caught
# immediately when a send to it bounces with Topic_id_invalid (queue worker).
TOPIC_CHECK_INTERVAL = 180.0  # seconds
# Worktree topics are few and their deletion should reclaim the worktree fast —
# probe ALL of them on this short interval (not round-robin), so a hard-deleted
# worktree agent is cleaned up in ~seconds instead of N*TOPIC_CHECK_INTERVAL.
WT_TOPIC_CHECK_INTERVAL = 10.0  # seconds

# Agent health check interval
AGENT_HEALTH_CHECK_INTERVAL = 30.0  # seconds

# Track agent down state: window_id -> monotonic time when first detected dead
_agent_down_since: dict[str, float] = {}

# Grace period before killing dead window (allows for /restart)
DEAD_WINDOW_GRACE = 30.0  # seconds

# Track orphan window state: window_id -> monotonic time when first seen unbound.
# Long enough to cover the gap between create_window and bind_thread inside the
# directory-browser flow, so a freshly-created window isn't reaped before its
# topic gets bound.
_orphan_since: dict[str, float] = {}
ORPHAN_WINDOW_GRACE = 90.0  # seconds

# Zero-bindings guard episode: monotonic time when "agent windows alive but no
# thread bindings" was first seen (None = condition absent), plus whether the
# episode's single warning already fired. The condition is normal for a moment
# during the very first bind (window exists before bind_thread lands), so the
# warning waits out ORPHAN_WINDOW_GRACE and fires once per episode — not every
# poll tick.
_zero_bindings_since: float | None = None
_zero_bindings_warned = False

# Typing heartbeat throttle: re-send "typing" at most this often per topic.
# Telegram's typing status lasts ~5s and Telegram explicitly says not to send
# it more often than every 5s; refresh just under that. (key = (user_id,
# thread_id or 0) → monotonic time of last send.)
TYPING_HEARTBEAT_INTERVAL = 4.0  # seconds
_typing_last_sent: dict[tuple[int, int], float] = {}

# Rising-edge dedup for the safeguard model-switch notice: True while the notice
# is on this window's pane (so the 1s poll notifies once, not every tick),
# re-armed when it scrolls off so a later switch is announced again. Keyed by
# binding value (window_id / "docker:<agent>").
_model_switch_seen: dict[str, bool] = {}


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    # Typing heartbeat — driven by the generation flag, independent of tmux
    # pane state so docker bindings (which don't have a host tmux window) get
    # the same indicator. Set on user input, refreshed by every inbound JSONL
    # event, cleared on final assistant text / interactive UI / stale-after.
    # Re-sent at most every TYPING_HEARTBEAT_INTERVAL per topic (the status
    # lasts ~5s; sending it every 1s poll would just waste rate budget).
    if session_manager.is_generating(window_id):
        tkey = (user_id, thread_id or 0)
        now = time.monotonic()
        if now - _typing_last_sent.get(tkey, 0.0) >= TYPING_HEARTBEAT_INTERVAL:
            _typing_last_sent[tkey] = now
            try:
                chat_id = session_manager.resolve_chat_id(user_id, thread_id)
                await bot.send_chat_action(
                    chat_id=chat_id,
                    action="typing",
                    message_thread_id=thread_id,
                )
            except Exception:
                pass

    # Capture pane via session_manager so the same UI-detection logic
    # works for both tmux ("@<id>") and docker ("docker:<agent>") bindings.
    # Without this, AskUserQuestion / ExitPlanMode rendered inside a
    # docker agent never produced a Telegram notification — the JSONL only
    # records the tool_use after the user answers, and host-side
    # tmux_manager has no window for a docker binding.
    pane_text = await session_manager.capture_pane(window_id)
    if not pane_text:
        # Tmux window gone, docker container down, or transient capture
        # failure — nothing to inspect this tick.
        return

    # Reaction-ack: fire the pending 👀 once this window's input queue drains —
    # i.e. a message armed on delivery is now taken into the agent's context, not
    # left buffered behind a running turn. No-op unless something is armed.
    await fire_reaction_ack(
        bot,
        window_id,
        has_queue=session_manager.agent_has_queued_input(window_id, pane_text),
    )

    # Safeguard model-switch notice — Fable 5 (etc.) silently downgrades to a
    # fallback model when its safeguards flag a message, printing a notice into
    # the transcript only (never the JSONL, so the monitor can't surface it).
    # Catch it off the live pane and tell the user at once. Rising-edge dedup
    # per window: notify once while the notice is on screen, re-arm when it
    # scrolls off. Runs before the interactive-UI early returns so it fires for
    # both tmux and docker bindings.
    switched_to = detect_model_switch(pane_text)
    if switched_to is not None:
        if not _model_switch_seen.get(window_id):
            _model_switch_seen[window_id] = True
            try:
                chat_id = session_manager.resolve_chat_id(user_id, thread_id)
                await safe_send(
                    bot,
                    chat_id,
                    tr(
                        "spoll.model_switched",
                        model=switched_to or tr("spoll.model_switched_fallback"),
                    ),
                    message_thread_id=thread_id,
                )
            except Exception as e:
                logger.debug("Model-switch notice send failed for %s: %s", window_id, e)
    else:
        _model_switch_seen.pop(window_id, None)

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — Claude is waiting for user, not
            # working. Clear the generation flag so typing drops immediately.
            session_manager.mark_idle(window_id)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        # Prompt waiting for user → not working. Stop typing.
        session_manager.mark_idle(window_id)
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Normal status line check — skip if queue is non-empty, or if this
    # is a docker binding (their UX is the agent's own message stream +
    # browser_live dashboard; no Claude-Code bottom-bar status in chat).
    if skip_status or session_manager._is_docker_binding(window_id):
        return

    # Status spinner has been removed: parsing parse_status_line(pane_text)
    # and publishing "✻ Cooked for 12s · Bash(...)" into the topic put a
    # visible message in the chat for every tool the agent ran. The user
    # didn't want tool plumbing in the chat at all — typing… indicator
    # and /screenshot cover "is the agent alive" without inviting tool
    # names into history. Cleanup paths above (None enqueues for pane-
    # capture-failed and interactive-UI transitions) still run so any
    # lingering status message from a previous deploy gets torn down.


async def _poll_one_binding(bot: Bot, user_id: int, thread_id: int, wid: str) -> None:
    """Run one binding's status-poll step. Exceptions are logged, not raised.

    Extracted so the main loop can fan out across all bindings via
    asyncio.gather — serialising these calls meant a single slow
    capture_pane stalled every other topic's status refresh.
    """
    try:
        # Stale-binding cleanup is tmux-only — docker bindings have no
        # host tmux window to look up, and their liveness is the
        # container's liveness (handled elsewhere). For docker we still
        # want to fall through to update_status_message so interactive
        # UI prompts (AskUserQuestion / ExitPlanMode) inside the
        # container's tmux pane get detected and surfaced as a Telegram
        # photo — without this poll the JSONL records nothing until the
        # user has already answered.
        if not session_manager._is_docker_binding(wid):
            w = await tmux_manager.find_window_by_id(wid)
            if not w:
                session_manager.unbind_thread(user_id, thread_id)
                await clear_topic_state(user_id, thread_id, bot)
                logger.info(
                    "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                    user_id,
                    thread_id,
                    wid,
                )
                return

        # UI detection happens unconditionally in update_status_message.
        # Status enqueue is skipped inside update_status_message when
        # interactive UI is detected (returns early) or when this
        # topic's queue is non-empty. With per-topic sharding we look
        # up the queue for THIS topic only — other topics' pending
        # tasks no longer suppress this topic's status poll.
        queue = get_message_queue(user_id, thread_id)
        skip_status = queue is not None and not queue.empty()

        await update_status_message(
            bot,
            user_id,
            wid,
            thread_id=thread_id,
            skip_status=skip_status,
        )
    except Exception as e:
        logger.debug(
            "Status update error for user %d thread %d: %s", user_id, thread_id, e
        )


async def _probe_topic_alive(bot: Bot, user_id: int, thread_id: int, wid: str) -> None:
    """Backstop topic-existence probe for one bound topic.

    Uses ``reopen_forum_topic`` because it is the only side-effect-free call
    that actually distinguishes a deleted topic: empirically
    ``unpin_all_forum_topic_messages`` and ``send_chat_action`` return OK even
    for a hard-deleted topic (so the old unpin probe never detected deletions —
    an idle deleted worktree was never reclaimed). ``reopen_forum_topic`` on a
    live *open* topic is a no-op that raises ``Topic_not_modified`` (not a gone
    marker → ignored); on a deleted topic it raises ``Topic_id_invalid`` →
    ``purge_deleted_topic``. Every bound topic is open (closing one unbinds it),
    so reopen never has a visible effect here.

    This is a *backstop* for idle topics: an active topic's deletion is caught
    immediately when a real send bounces (queue worker → same purge). Called in
    ``background_context()`` — a chat-management call Telegram rate-limits, and a
    429 on *any* request pauses every other send during the RetryAfter window,
    so it must never burst.
    """
    try:
        with background_context():
            await bot.reopen_forum_topic(
                chat_id=session_manager.resolve_chat_id(user_id, thread_id),
                message_thread_id=thread_id,
            )
    except BadRequest as e:
        # Shared gone-detector (Topic_id_invalid / message thread not found,
        # case-insensitive). Topic_not_modified (live open topic) is NOT a gone
        # marker → ignored.
        if is_topic_gone_error(e):
            await purge_deleted_topic(bot, user_id, thread_id, wid)
        else:
            logger.debug("Topic probe non-gone result for %s: %s", wid, e)
    except Exception as e:
        logger.debug("Topic probe error for %s: %s", wid, e)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    last_wt_check = 0.0
    topic_probe_pos = 0
    while True:
        try:
            # Periodic topic-existence probe — ONE topic per cycle, round-robin
            # (see _probe_topic_alive for why we don't fan these out).
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                bindings = list(session_manager.iter_thread_bindings())
                if bindings:
                    topic_probe_pos %= len(bindings)
                    await _probe_topic_alive(bot, *bindings[topic_probe_pos])
                    topic_probe_pos += 1

            # Worktree topics: probe ALL of them on a short interval so a
            # hard-deleted worktree agent is reclaimed in seconds (they're few;
            # the probe is a no-op unpin in the background lane).
            if now - last_wt_check >= WT_TOPIC_CHECK_INTERVAL:
                last_wt_check = now
                for uid, tid, w in list(session_manager.iter_thread_bindings()):
                    if session_manager.get_worktree_meta(uid, tid) is not None:
                        await _probe_topic_alive(bot, uid, tid, w)

            # Fan out status updates across all bound topics in parallel.
            # Each _poll_one_binding does its own tmux capture (I/O-bound,
            # offloaded to the default thread pool via to_thread inside
            # tmux_manager); gathering them means N topics no longer serialise
            # behind each other's capture+edit latency, so the poll loop
            # stays close to its 1s cadence even with many open topics.
            await asyncio.gather(
                *(
                    _poll_one_binding(bot, user_id, thread_id, wid)
                    for user_id, thread_id, wid in list(
                        session_manager.iter_thread_bindings()
                    )
                ),
                return_exceptions=True,
            )
        except Exception as e:
            logger.exception(f"Status poll loop error: {e}")

        # Agent health check — detect dead windows, kill after grace period
        try:
            all_windows = await tmux_manager.list_windows()
            live_ids = {w.window_id for w in all_windows}
            for w in all_windows:
                if w.window_name == "__main__":
                    continue
                # Runtime-aware liveness: each runtime declares the foreground
                # commands that mean "still running" (Claude → claude/node,
                # Codex → codex). Keying on a hardcoded claude set reaped every
                # codex window 30 s after launch (sign-in menu included).
                runtime = get_runtime(session_manager.window_runtime(w.window_id))
                is_alive = w.pane_current_command in runtime.pane_alive_commands
                if is_alive:
                    # Agent is running — clear any down timer
                    _agent_down_since.pop(w.window_id, None)
                    continue

                # Agent is dead (bash or other process)
                if w.window_id not in _agent_down_since:
                    # First detection — start timer and notify user
                    _agent_down_since[w.window_id] = time.monotonic()
                    for uid, tid, wid in list(session_manager.iter_thread_bindings()):
                        if wid == w.window_id:
                            chat_id = session_manager.resolve_chat_id(uid, tid)
                            await safe_send(
                                bot,
                                chat_id,
                                tr(
                                    "spoll.agent_stopped",
                                    name=w.window_name,
                                    grace=int(DEAD_WINDOW_GRACE),
                                ),
                                message_thread_id=tid,
                            )
                            break
                else:
                    # Already tracking — check if grace period expired
                    elapsed = time.monotonic() - _agent_down_since[w.window_id]
                    if elapsed >= DEAD_WINDOW_GRACE:
                        display = w.window_name
                        await tmux_manager.kill_window(w.window_id)
                        _agent_down_since.pop(w.window_id, None)
                        logger.info(
                            "Auto-killed dead window %s (%s) after %.0fs",
                            w.window_id,
                            display,
                            elapsed,
                        )
                        # Unbind and notify
                        for uid, tid, wid in list(
                            session_manager.iter_thread_bindings()
                        ):
                            if wid == w.window_id:
                                session_manager.unbind_thread(uid, tid)
                                await clear_topic_state(uid, tid, bot)
                                chat_id = session_manager.resolve_chat_id(uid, tid)
                                await safe_send(
                                    bot,
                                    chat_id,
                                    tr(
                                        "spoll.window_closed",
                                        name=display,
                                        secs=int(elapsed),
                                    ),
                                    message_thread_id=tid,
                                )
                                break

            # Clean up tracking for windows that no longer exist
            stale = [wid for wid in _agent_down_since if wid not in live_ids]
            for wid in stale:
                _agent_down_since.pop(wid, None)
        except Exception as e:
            logger.debug("Agent health check error: %s", e)

        # Orphan window janitor — any non-__main__ tmux window with no thread
        # binding is stale (topic deleted while bot was offline, leftover from
        # a re-bind, or a window created in tmux outside the bot). Kill it
        # after ORPHAN_WINDOW_GRACE so a single iteration in the brief window
        # between create_window and bind_thread cannot trigger a kill.
        try:
            all_windows = await tmux_manager.list_windows()
            live_ids = {w.window_id for w in all_windows}
            bound_wids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
            now = time.monotonic()
            agent_windows = [w for w in all_windows if w.window_name != "__main__"]
            if agent_windows and not bound_wids:
                # Zero bindings while agent windows are alive smells like a
                # lost/corrupt state.json, not N simultaneously orphaned
                # topics — reaping on that signal would kill every live
                # agent 90s after a single bad state write. It is also the
                # normal transient during the very first bind (create_window
                # precedes bind_thread), so only warn once the episode
                # outlives the grace — and only once, not every poll tick.
                global _zero_bindings_since, _zero_bindings_warned
                if _zero_bindings_since is None:
                    _zero_bindings_since = now
                if (
                    not _zero_bindings_warned
                    and now - _zero_bindings_since >= ORPHAN_WINDOW_GRACE
                ):
                    _zero_bindings_warned = True
                    logger.warning(
                        "Orphan janitor: no thread bindings but %d agent "
                        "window(s) alive — skipping reap (possible state loss)",
                        len(agent_windows),
                    )
                agent_windows = []
            else:
                _zero_bindings_since = None
                _zero_bindings_warned = False
            for w in agent_windows:
                if w.window_id in bound_wids:
                    _orphan_since.pop(w.window_id, None)
                    continue
                first_seen = _orphan_since.setdefault(w.window_id, now)
                elapsed = now - first_seen
                if elapsed >= ORPHAN_WINDOW_GRACE:
                    display = w.window_name
                    # If this is a worktree window, flag its meta orphaned
                    # before reaping — never destroy the worktree/branch here
                    # (headless path, no guard). The phase-3 GC reclaims it.
                    ws = session_manager.get_window_state(w.window_id)
                    if ws and ws.cwd:
                        session_manager.mark_worktree_orphaned_by_path(ws.cwd)
                    await tmux_manager.kill_window(w.window_id)
                    _orphan_since.pop(w.window_id, None)
                    logger.info(
                        "Auto-killed orphan window %s (%s) after %.0fs without binding",
                        w.window_id,
                        display,
                        elapsed,
                    )

            stale = [wid for wid in _orphan_since if wid not in live_ids]
            for wid in stale:
                _orphan_since.pop(wid, None)
        except Exception as e:
            logger.debug("Orphan window janitor error: %s", e)

        await asyncio.sleep(STATUS_POLL_INTERVAL)
