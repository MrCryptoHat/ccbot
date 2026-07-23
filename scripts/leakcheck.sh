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

# 1. Real home path.
printf '/home/%s\n' "$(id -un)" >>"$patterns"

# 2. Deployment agent/project names (word-ish, >=4 chars to avoid noise),
#    minus this repo's own name and names the public tree already uses.
self="$(basename "$(git rev-parse --show-toplevel)")"
for d in "$HOME"/agents/*/ "$HOME"/projects/*/; do
    [ -d "$d" ] || continue
    n="$(basename "$d")"
    case "$n" in "$self" | _* | mnt | node_modules) continue ;; esac
    [ "${#n}" -ge 4 ] || continue
    # Already in the public tree at base → evidently not treated as private.
    git grep -qiF "$n" "$base" -- 2>/dev/null && continue
    printf '%s\n' "$n" >>"$patterns"
done

# 3. Secret/ID values from local .env files (never echoed anywhere).
for env in ./.env "$HOME/.ccbot/.env"; do
    [ -f "$env" ] || continue
    sed -n 's/^[A-Za-z_]*\(TOKEN\|KEY\|SECRET\|USERS\|CHAT_ID\|_ID\)[[:space:]]*=[[:space:]]*//p' "$env" |
        tr ',' '\n' | sed 's/^["'\'' ]*//;s/["'\'' ]*$//' |
        awk 'length($0)>=6' >>"$patterns"
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
