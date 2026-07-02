#!/usr/bin/env bash
# Restart ccbot. Self-heals missing tmux session/window and stops the
# bot by PID (not by pane lookup) so a renamed/closed window doesn't
# leave the bot orphaned.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Tmux session name — must match config's TMUX_SESSION_NAME. Honor the env
# var if exported, else read it from an .env (repo root or $CCBOT_DIR), else
# default "ccbot".
TMUX_SESSION="${TMUX_SESSION_NAME:-}"
if [ -z "$TMUX_SESSION" ]; then
    for _envf in "$PROJECT_DIR/.env" "${CCBOT_DIR:-$HOME/.ccbot}/.env"; do
        [ -f "$_envf" ] || continue
        # `|| true`: under `set -e`+pipefail a no-match grep (var absent from
        # this .env — the documented default case) would otherwise abort the
        # whole script silently before we fall through to the "ccbot" default.
        _v="$(grep -E '^[[:space:]]*TMUX_SESSION_NAME=' "$_envf" | tail -1 || true)"
        _v="${_v#*=}"                     # value after first =
        _v="${_v%%#*}"                    # drop inline comment
        _v="${_v//[[:space:]]/}"          # drop whitespace
        _v="${_v//\"/}"; _v="${_v//\'/}"  # drop quotes
        [ -n "$_v" ] && TMUX_SESSION="$_v" && break
    done
fi
TMUX_SESSION="${TMUX_SESSION:-ccbot}"

TMUX_WINDOW="__main__"
TARGET="${TMUX_SESSION}:${TMUX_WINDOW}"
MAX_WAIT=10  # seconds to wait for graceful SIGINT shutdown
# Match the running bot on its console-script path — covers `uv run`
# (.venv/bin/ccbot), `uv tool`, and pipx/PATH installs. Requires `/bin/`
# so it can't match a process that merely mentions the repo path.
BOT_PATTERN='/bin/ccbot([[:space:]]|$)'

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' not found in PATH — install it: https://docs.astral.sh/uv/" >&2
    exit 1
fi

bot_pids() {
    # Match the bot's console-script path (BOT_PATTERN), then drop any PID
    # running inside a docker container. On a host that ALSO runs a
    # containerized ccbot (e.g. a dockerized deployment on the same box),
    # `pgrep -f` sees the container's process too — its cmdline carries the
    # same `.venv/bin/ccbot` script. SIGINT'ing it kills the in-container tmux
    # session → the container's entrypoint exits → docker restarts the whole
    # container, an unintended bounce of a live service. A containerized
    # process carries `docker-<id>.scope` in its cgroup; the host bot does not.
    #
    # Also drop instances launched from a DIFFERENT checkout's .venv (e.g. a
    # second clone used as a test instance beside the main deployment) —
    # restarting repo A must not SIGINT repo B's bot. Installs that can't be
    # attributed to any repo (pipx / uv tool / PATH) keep the legacy
    # match-everything behavior.
    local pid cmdline
    for pid in $(pgrep -f "$BOT_PATTERN" 2>/dev/null || true); do
        grep -q docker "/proc/$pid/cgroup" 2>/dev/null && continue
        cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
        case "$cmdline" in
            *"$PROJECT_DIR/.venv/bin/ccbot"*) ;;   # this repo's instance
            *"/.venv/bin/ccbot"*) continue ;;      # another checkout's — leave it alone
            *) ;;                                  # unattributable install — legacy behavior
        esac
        printf '%s\n' "$pid"
    done
}

# 1. Stop any running ccbot by PID. Works regardless of tmux state —
#    the bot could be orphaned, in a different window, or the window
#    could have been renamed since it was started.
pids="$(bot_pids)"
if [ -n "$pids" ]; then
    echo "Stopping ccbot (PIDs: $pids)..."
    # SIGINT mirrors Ctrl-C; asyncio/PTB handles it for a clean shutdown.
    # shellcheck disable=SC2086  # word-splitting is intentional here
    kill -INT $pids 2>/dev/null || true

    waited=0
    while [ -n "$(bot_pids)" ] && [ "$waited" -lt "$MAX_WAIT" ]; do
        sleep 1
        waited=$((waited + 1))
        echo "  Waiting... (${waited}s/${MAX_WAIT}s)"
    done

    pids="$(bot_pids)"
    if [ -n "$pids" ]; then
        echo "Did not exit in ${MAX_WAIT}s, sending SIGKILL (PIDs: $pids)..."
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null || true
        sleep 1
    fi
    echo "Stopped."
else
    echo "No ccbot process running."
fi

# 2. Ensure tmux session exists (recover from full tmux restart).
if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "Creating tmux session '$TMUX_SESSION'..."
    tmux new-session -d -s "$TMUX_SESSION" -n "$TMUX_WINDOW" -c "$PROJECT_DIR"
fi

# 3. Ensure the target window exists (recover from Ctrl-C closing the
#    pane shell and taking the window with it).
if ! tmux list-windows -t "$TMUX_SESSION" -F '#{window_name}' | grep -qx "$TMUX_WINDOW"; then
    echo "Creating tmux window '$TMUX_WINDOW'..."
    tmux new-window -t "${TMUX_SESSION}:" -n "$TMUX_WINDOW" -c "$PROJECT_DIR"
fi

# 4. Clear any stale input line in the pane, then launch.
tmux send-keys -t "$TARGET" C-c 2>/dev/null || true
sleep 0.2
echo "Starting ccbot in $TARGET..."
tmux send-keys -t "$TARGET" "cd ${PROJECT_DIR} && uv run ccbot" Enter

# 5. Verify by PID, not by pane contents — faster and reliable.
sleep 3
if [ -n "$(bot_pids)" ]; then
    echo "ccbot restarted successfully. Recent logs:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -20
    echo "----------------------------------------"
else
    echo "Warning: ccbot may not have started. Pane output:"
    echo "----------------------------------------"
    tmux capture-pane -t "$TARGET" -p | tail -30
    echo "----------------------------------------"
    exit 1
fi
