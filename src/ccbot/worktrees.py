"""Git-worktree provisioning and teardown for parallel agents on one project.

Pure, unit-tested core (no Telegram, no tmux) plus async git helpers:
  - slugify (ru→latin transliteration) / branch_name / dedup_slug — naming
  - worktree_root / worktree_path — on-disk layout (~/.ccbot/worktrees/<repo>/<slug>)
  - SEED_* tables + seed_worktree — wire .env / deps into a fresh worktree
  - WorktreeStatus + parse_porcelain + count_unmerged + decide_delete_safety — the teardown guard
  - add_worktree / remove_worktree / delete_branch / prune_worktrees — lifecycle
  - detect_base_branch / branch_exists / list_git_repos — discovery

A worktree agent is just a tmux topic whose cwd is a worktree dir; this module
owns only the git/disk plumbing. See _plans/2026-06-14-ccbot-worktree-agents.md.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .utils import ccbot_dir

# --- naming -----------------------------------------------------------------

# Minimal ru→latin transliteration; the codebase has no translit dependency and
# the input is a human task name typed in Russian. Output is forced to the
# preview CLI's slug charset (^[a-z0-9][a-z0-9-]{0,29}$) by slugify() below.
_RU_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

SLUG_MAX = 30
BRANCH_PREFIX = "wt/"
_SLUG_FALLBACK = "task"


def slugify(name: str) -> str:
    """Turn a human task name into a safe slug ``[a-z0-9][a-z0-9-]{0,29}``.

    Transliterates Cyrillic, lowercases, replaces any other run of non
    ``[a-z0-9]`` with a single dash, trims, caps at 30 chars, and falls back to
    ``task`` when the result is degenerate. The output always satisfies the
    filesystem-safe shape above (leading alnum, 1-30 chars).
    """
    out: list[str] = []
    for ch in name.lower():
        if ch in _RU_LATIN:
            out.append(_RU_LATIN[ch])
        elif ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch)
        else:
            out.append("-")
    slug = "".join(out)
    # collapse repeated dashes, strip leading/trailing
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")[:SLUG_MAX].strip("-")
    # leading char must be alnum (it already is after strip, but guard empty)
    if not slug:
        return _SLUG_FALLBACK
    return slug


def branch_name(slug: str) -> str:
    """The git branch a worktree slug maps to."""
    return f"{BRANCH_PREFIX}{slug}"


def dedup_slug(slug: str, taken: set[str]) -> str:
    """Return ``slug`` or ``slug-2``/``slug-3``… so it does not collide.

    ``taken`` is the set of slugs already in use for the repo (existing
    worktree dirs + branches, both reduced to their slug form). Keeps the
    result within ``SLUG_MAX``.
    """
    if slug not in taken:
        return slug
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = slug[: SLUG_MAX - len(suffix)].strip("-") + suffix
        if candidate not in taken:
            return candidate
        n += 1


# --- on-disk layout ---------------------------------------------------------


def worktree_root() -> Path:
    """Root dir for all worktrees: ``~/.ccbot/worktrees``.

    Deliberately under CCBOT_DIR (outside the scanned ``~/projects`` /
    ``~/agents`` roots) so name-based auto-bind never mistakes a worktree for a
    bindable project.
    """
    return ccbot_dir() / "worktrees"


def worktree_path(repo_name: str, slug: str) -> Path:
    """Path for a single worktree: ``~/.ccbot/worktrees/<repo>/<slug>``."""
    return worktree_root() / repo_name / slug


# --- persisted metadata -----------------------------------------------------


@dataclass
class WorktreeMeta:
    """What a worktree-backed topic needs that git can't recover on its own.

    Keyed in ``state.json`` by ``thread_id`` (same key as every other per-topic
    structure), so teardown — which receives ``(user, thread)`` — looks it up
    directly. ``status`` is ``active`` normally, ``orphaned`` once the topic is
    gone but the worktree survives on disk (headless purge / reaped window).
    """

    repo: str  # absolute base-repo path
    repo_name: str  # display/grouping name
    branch: str  # wt/<slug>
    base_branch: str  # branch it forked from
    path: str  # absolute worktree path
    task_title: str  # the human name the user typed
    status: str = "active"  # active | orphaned

    def to_dict(self) -> dict[str, str]:
        return {
            "repo": self.repo,
            "repo_name": self.repo_name,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "path": self.path,
            "task_title": self.task_title,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "WorktreeMeta":
        return cls(
            repo=d.get("repo", ""),
            repo_name=d.get("repo_name", ""),
            branch=d.get("branch", ""),
            base_branch=d.get("base_branch", ""),
            path=d.get("path", ""),
            task_title=d.get("task_title", ""),
            status=d.get("status", "active"),
        )


# --- dependency seeding -----------------------------------------------------

# Files symlinked from the base repo into a fresh worktree (shared secrets).
SEED_SYMLINK = [".env"]
# Paths that never count as "dirty" and are never seeded (resync output, caches).
SEED_SKIP = [".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"]
# The union that the dirty-guard must subtract from `git status` before deciding.
SEED_IGNORE = sorted(set(SEED_SYMLINK) | set(SEED_SKIP))


def _seed_commands(wt_path: Path) -> list[tuple[str, list[str]]]:
    """Decide the resync commands for a worktree by the lockfiles present.

    Returns a list of ``(label, argv)``. uv for Python (fast hardlink resync),
    pnpm/npm for Node selected by lockfile. Pure: it only inspects which files
    exist, it does not run anything.
    """
    cmds: list[tuple[str, list[str]]] = []
    if (wt_path / "uv.lock").is_file() or (wt_path / "pyproject.toml").is_file():
        cmds.append(("uv sync", ["uv", "sync", "-q"]))
    if (wt_path / "package.json").is_file():
        if (wt_path / "pnpm-lock.yaml").is_file():
            cmds.append(("pnpm install", ["pnpm", "install", "--frozen-lockfile"]))
        else:
            cmds.append(("npm ci", ["npm", "ci"]))
    return cmds


# --- teardown guard ---------------------------------------------------------


@dataclass(frozen=True)
class WorktreeStatus:
    """Snapshot of a worktree's git state, used to gate teardown."""

    dirty: bool  # real uncommitted work after subtracting seeded artifacts
    dirty_files: int
    ahead: int | None  # unmerged commits; None = indeterminate (fail-closed)
    base_branch: str
    exists: bool  # the worktree dir + .git link are still present


DeleteSafety = Literal["clean", "unmerged", "dirty"]


def parse_porcelain(
    porcelain: str, ignore: list[str] = SEED_IGNORE
) -> tuple[bool, int]:
    """Count real dirt in ``git status --porcelain`` output.

    Subtracts seeded artifacts (``ignore``) — a symlinked ``.env`` or an
    installed ``node_modules`` shows up as untracked unless the project happens
    to gitignore it, and must not register as the user's work. Returns
    ``(dirty, n_files)``.
    """
    n = 0
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        # porcelain v1: "XY <path>" (rename: "R  old -> new" — take the new path)
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        top = path.split("/", 1)[0]
        if top in ignore or path in ignore:
            continue
        n += 1
    return (n > 0, n)


def decide_delete_safety(status: WorktreeStatus) -> DeleteSafety:
    """Classify a worktree for the teardown traffic-light.

    ``dirty`` (🟢 uncommitted work) → ``unmerged`` (🟡 committed, not in base) →
    ``clean`` (⚪ nothing to lose). Pure; the UI maps these to confirm dialogs.
    """
    if status.dirty:
        return "dirty"
    # ahead is None when merge status is indeterminate (no candidate base ref
    # resolved AND the rev-list fallback failed). Fail CLOSED: treat unknown as
    # unmerged, so a worktree we cannot vouch for is never auto-torn-down.
    if status.ahead is None or status.ahead > 0:
        return "unmerged"
    return "clean"


# --- async git helpers ------------------------------------------------------


async def _git(
    *args: str, cwd: Path | str | None = None, timeout: float = 30.0
) -> tuple[int, str, str]:
    """Run ``git <args>`` and return ``(returncode, stdout, stderr)`` (text)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def branch_exists(repo: Path, branch: str) -> bool:
    """True if a local branch ``branch`` exists in ``repo``."""
    rc, out, _ = await _git("-C", str(repo), "branch", "--list", branch)
    return rc == 0 and out.strip() != ""


async def detect_base_branch(repo: Path) -> str | None:
    """Resolve the branch a new worktree should fork from.

    Uses the repo's current branch (``symbolic-ref --short HEAD``), then
    verifies it resolves. Returns ``None`` for a detached HEAD / unresolvable
    ref so the caller can refuse rather than emit a confusing ``worktree add``
    error. Deliberately does NOT hard-fallback to the literal ``main`` (many
    repos default to ``master`` and lack ``origin/HEAD``).
    """
    rc, out, _ = await _git("-C", str(repo), "symbolic-ref", "--short", "HEAD")
    ref = out.strip()
    if rc != 0 or not ref:
        return None
    rc2, _, _ = await _git("-C", str(repo), "rev-parse", "--verify", "--quiet", ref)
    return ref if rc2 == 0 else None


async def taken_slugs(repo: Path, repo_name: str) -> set[str]:
    """All slugs already in use for a repo (worktree dirs + ``wt/`` branches).

    Feed to ``dedup_slug`` so a new task never collides with a leftover branch
    (e.g. a worktree removed without ``branch -D``) or a sibling worktree.
    """
    slugs = existing_slugs(repo_name)
    rc, out, _ = await _git(
        "-C", str(repo), "branch", "--list", "wt/*", "--format=%(refname:short)"
    )
    if rc == 0:
        for line in out.splitlines():
            ref = line.strip()
            if ref.startswith(BRANCH_PREFIX):
                slugs.add(ref[len(BRANCH_PREFIX) :])
    return slugs


async def add_worktree(
    repo: Path, wt_path: Path, branch: str, base_branch: str
) -> tuple[bool, str]:
    """``git worktree add -b <branch> <wt_path> <base_branch>``. Returns (ok, msg)."""
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = await _git(
        "-C", str(repo), "worktree", "add", "-b", branch, str(wt_path), base_branch
    )
    return (rc == 0, err.strip())


async def remove_worktree(
    repo: Path, wt_path: Path, *, force: bool
) -> tuple[bool, str]:
    """``git worktree remove [--force] <wt_path>``. ``force`` only post-guard."""
    args = ["-C", str(repo), "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(wt_path))
    rc, _, err = await _git(*args)
    return (rc == 0, err.strip())


async def delete_branch(repo: Path, branch: str, *, force: bool) -> tuple[bool, str]:
    """``git branch -d/-D <branch>`` (run after the worktree is removed)."""
    flag = "-D" if force else "-d"
    rc, _, err = await _git("-C", str(repo), "branch", flag, branch)
    return (rc == 0, err.strip())


async def prune_worktrees(repo: Path) -> None:
    """``git worktree prune`` — drop registrations for dirs deleted out-of-band."""
    await _git("-C", str(repo), "worktree", "prune")


async def _ref_exists(repo: Path, ref: str) -> bool:
    """True if ``ref`` resolves in ``repo`` (a branch, tag, or remote-tracking ref)."""
    rc, _, _ = await _git("-C", str(repo), "rev-parse", "--verify", "--quiet", ref)
    return rc == 0


async def count_unmerged(repo: Path, base_branch: str, branch: str) -> int | None:
    """Count commits on ``branch`` not integrated into ANY candidate base ref.

    A plain ``rev-list --count <base>..<branch>`` against the *local* base raises
    a false "unmerged" alarm in two real cases:
      * **Direct push to the base on the remote** — work landed via
        ``git push origin <branch>:<base>`` (fast-forward), so the commits live
        in ``origin/<base>`` while the local ``<base>`` ref stays behind.
      * **Squash / rebase / cherry-pick merge** — the diff is integrated under a
        new sha, so reachability (``base..branch``) still counts it as ahead.

    Both are handled by checking ``git cherry`` (matches by *patch-id*, so an
    integrated diff counts as merged even under a different sha) against every
    resolvable candidate base — the local base, ``origin/<base>``, and the
    configured upstreams of both base and branch — and taking the MINIMUM
    ``+``-count: integrated anywhere ⇒ integrated. Falls back to a plain
    ``rev-list --count`` against the local base when no candidate resolves, and
    to ``None`` when even that fails — the caller must read ``None`` as "cannot
    determine, treat as unmerged" (fail-closed), never as merged.
    """
    seen: set[str] = set()
    counts: list[int] = []
    for ref in (
        base_branch,
        f"origin/{base_branch}",
        f"{base_branch}@{{upstream}}",
        f"{branch}@{{upstream}}",
    ):
        if ref in seen or not await _ref_exists(repo, ref):
            continue
        seen.add(ref)
        # `git cherry <upstream> <head>`: '+' = no patch-equivalent in upstream
        # (unmerged), '-' = already integrated. Count only the '+' lines.
        rc, out, _ = await _git("-C", str(repo), "cherry", ref, branch)
        if rc == 0:
            counts.append(sum(1 for ln in out.splitlines() if ln.startswith("+")))
    if counts:
        return min(counts)
    # No candidate base ref resolved. Try the local rev-list as a last resort;
    # if even that fails (e.g. the base branch no longer exists), we genuinely
    # cannot tell whether the branch is merged — return None so callers fail
    # CLOSED (treat as unmerged) instead of reporting a false 0 that would let a
    # headless teardown `git branch -D` committed work. (audit HIGH#2)
    rc, out, _ = await _git(
        "-C", str(repo), "rev-list", "--count", f"{base_branch}..{branch}"
    )
    if rc == 0 and out.strip().isdigit():
        return int(out.strip())
    return None


async def worktree_status(
    repo: Path, wt_path: Path, base_branch: str, branch: str
) -> WorktreeStatus:
    """Compute the teardown-guard snapshot for a worktree."""
    if not (wt_path / ".git").exists():
        return WorktreeStatus(
            dirty=False, dirty_files=0, ahead=0, base_branch=base_branch, exists=False
        )
    _, porcelain, _ = await _git("-C", str(wt_path), "status", "--porcelain")
    dirty, n = parse_porcelain(porcelain)
    ahead = await count_unmerged(repo, base_branch, branch)
    return WorktreeStatus(
        dirty=dirty, dirty_files=n, ahead=ahead, base_branch=base_branch, exists=True
    )


async def seed_worktree(repo: Path, wt_path: Path) -> list[str]:
    """Wire ``.env`` + dependencies into a fresh worktree. Returns status lines.

    Symlinks SEED_SYMLINK files from the base repo (shared secrets), then runs
    the resync commands chosen by lockfile. Best-effort: a failed resync is
    reported but does not abort provisioning (the agent can fix it in-topic).
    """
    notes: list[str] = []
    for name in SEED_SYMLINK:
        src = repo / name
        dst = wt_path / name
        if src.exists() and not dst.exists():
            try:
                dst.symlink_to(src)
                notes.append(f"🔗 {name}")
            except OSError as e:
                notes.append(f"⚠️ {name}: {e}")
    for label, argv in _seed_commands(wt_path):
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(wt_path),
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode == 0:
                notes.append(f"✅ {label}")
            else:
                notes.append(f"⚠️ {label}: {err.decode(errors='replace').strip()[:80]}")
        except (OSError, asyncio.TimeoutError) as e:
            notes.append(f"⚠️ {label}: {e}")
    return notes


# --- discovery --------------------------------------------------------------


def list_git_repos(
    roots: list[Path], *, exclude: list[Path] | None = None
) -> list[Path]:
    """Scan ``roots`` for top-level git repos (for the global project picker).

    Skips dotfile / underscore-prefixed dirs (server infra) and anything under
    ``exclude`` (e.g. the rclone mount or the worktree root). A dir is a repo if
    it contains ``.git``.
    """
    excl = [e.resolve() for e in (exclude or [])]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            name = entry.name
            if name.startswith(".") or name.startswith("_") or not entry.is_dir():
                continue
            rp = entry.resolve()
            if any(rp == e or rp.is_relative_to(e) for e in excl):
                continue
            if (entry / ".git").exists():
                found.append(entry)
    return found


def existing_slugs(repo_name: str) -> set[str]:
    """Slugs already used on disk under this repo's worktree dir."""
    base = worktree_root() / repo_name
    if not base.is_dir():
        return set()
    return {p.name for p in base.iterdir() if p.is_dir()}


def preview_slugs_under(registry: dict[str, object], wt_path: Path) -> list[str]:
    """Preview slugs whose ``cwd`` is inside ``wt_path`` (for `preview down`).

    The preview registry stores ``cwd`` per slug; teardown scans it to stop any
    dev server running from the worktree before the dir is removed.
    """
    wt = str(wt_path.resolve())
    slugs: list[str] = []
    for slug, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        cwd = entry.get("cwd")
        if not isinstance(cwd, str):
            continue
        try:
            rp = str(Path(cwd).resolve())
        except OSError:
            continue
        if rp == wt or rp.startswith(wt + os.sep):
            slugs.append(slug)
    return slugs
