#!/bin/sh
# Leak check for a PUBLIC repo: scan outgoing commits (content AND commit
# messages) for deployment-identifying strings before they reach the remote.
#
# The script itself is generic and committable — every private marker is
# derived at RUNTIME from the deployment, never written here:
#   1. the real home path (/home/<user> — public fixtures use /home/user);
#   2. names of the operator's agents/projects (~/agents/*, ~/projects/*),
#      EXCEPT names already present in the public tree (an example name like
#      "assistant" that the docs legitimately use auto-allowlists itself);
#   3. secret/ID VALUES from local .env files (tokens, allowed users, chat
#      ids) — matched by value, so new kinds of secrets are caught without
#      updating this script.
#
# Usage: scripts/leakcheck.sh [<base-ref>]     (default: origin/main)
# Exit: 0 clean, 1 findings (printed truncated), 2 usage/setup error.
# Install as a push gate:  ln -s ../../scripts/leakcheck.sh .git/hooks/pre-push
set -eu

# As a pre-push hook git passes (remote-name, url) — accept a remote name by
# resolving it to its main branch; a plain ref works too; default origin/main.
base="${1:-origin/main}"
if ! git rev-parse --verify -q "$base" >/dev/null; then
    if git rev-parse --verify -q "$base/main" >/dev/null; then
        base="$base/main"
    else
        echo "leakcheck: base ref '$base' not found" >&2
        exit 2
    fi
fi
range="$base..HEAD"

patterns="$(mktemp)"
trap 'rm -f "$patterns"' EXIT

# 1. Real home path. Both spellings: $HOME is the truth (macOS homes are
#    /Users/<user>, so the /home/<user> guess matched nothing there and this
#    leg silently checked for a string that cannot occur), and the literal
#    /home/<user> still covers a Linux path pasted in from elsewhere.
printf '%s\n' "$HOME" >>"$patterns"
printf '/home/%s\n' "$(id -un)" >>"$patterns"

# 2. Deployment agent/project names (word-ish, >=4 chars to avoid noise),
#    minus this repo's own name and names the public tree already uses.
#    Roots follow CCBOT_TOPIC_DIR_ROOTS — a deployment that keeps its clones
#    in ~/dev is exactly as identifying as one using the default ~/projects.
self="$(basename "$(git rev-parse --show-toplevel)")"
roots="projects agents"
for env in ./.env "$HOME/.ccbot/.env"; do
    [ -f "$env" ] || continue
    extra="$(sed -n 's/^CCBOT_TOPIC_DIR_ROOTS[[:space:]]*=[[:space:]]*//p' "$env" |
        tr ',' ' ' | tr -d '"'\''')"
    [ -n "$extra" ] && roots="$roots $extra"
done
for root in $roots; do
    for d in "$HOME/$root"/*/; do
        [ -d "$d" ] || continue
        n="$(basename "$d")"
        case "$n" in "$self" | _* | mnt | node_modules) continue ;; esac
        [ "${#n}" -ge 4 ] || continue
        # Already in the public tree at base → evidently not treated as private.
        git grep -qiF "$n" "$base" -- 2>/dev/null && continue
        printf '%s\n' "$n" >>"$patterns"
    done
done

# 2b. Docker-agent names from local .env. Their workspaces can live outside
#     ~/agents (a mount, a custom DOCKER_AGENT_<N>_WORKSPACE), so the directory
#     sweep above misses them — and an agent name is exactly as identifying as
#     a project name. Same >=4 chars + already-public allowlist rules.
for env in ./.env "$HOME/.ccbot/.env"; do
    [ -f "$env" ] || continue
    for n in $(sed -n 's/^DOCKER_AGENTS[[:space:]]*=[[:space:]]*//p' "$env" | tr ',' '\n' |
        sed 's/^["'\'' ]*//;s/["'\'' ]*$//'); do
        [ "${#n}" -ge 4 ] || continue
        git grep -qiF "$n" "$base" -- 2>/dev/null && continue
        printf '%s\n' "$n" >>"$patterns"
    done
done

# 3. Secret/ID values from local .env files (never echoed anywhere).
#    awk, not `sed -n 's/^[A-Za-z_]*\(TOKEN\|...\)=//p'`: that BRE needs the
#    prefix star to give characters BACK so the alternation can match, which
#    GNU sed does and BSD sed does not. On macOS it matched nothing, so THE
#    SECRET LEG OF THIS GATE SILENTLY CHECKED NOTHING while still exiting 0.
for env in ./.env "$HOME/.ccbot/.env"; do
    [ -f "$env" ] || continue
    awk -F= '
        /^[A-Za-z_]+[[:space:]]*=/ {
            key = $1
            sub(/[[:space:]]+$/, "", key)
            if (key !~ /(TOKEN|KEY|SECRET|USERS|CHAT_ID|_ID)$/) next
            value = substr($0, index($0, "=") + 1)
            n = split(value, parts, ",")
            for (i = 1; i <= n; i++) {
                gsub(/^[["'\''[:space:]]+|[]"'\''[:space:]]+$/, "", parts[i])
                if (length(parts[i]) >= 6) print parts[i]
            }
        }
    ' "$env" >>"$patterns"
done

[ -s "$patterns" ] || exit 0

hits="$(git log -p "$range" 2>/dev/null | grep -inF -f "$patterns" | cut -c1-100 | head -20 || true)"
if [ -n "$hits" ]; then
    echo "leakcheck: deployment-identifying strings in outgoing commits ($range):" >&2
    echo "$hits" >&2
    echo "leakcheck: scrub them (content AND commit messages) before pushing." >&2
    exit 1
fi
exit 0
