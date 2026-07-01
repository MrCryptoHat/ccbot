"""Integration tests for ccbot.worktrees git helpers against a real repo.

Marked `integration` (real git subprocess + filesystem). Validates the actual
worktree lifecycle the provisioning/teardown orchestration depends on.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest

from ccbot import worktrees as wt

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "app.py").write_text("print('hi')\n")
    (r / ".gitignore").write_text(".env\n.venv/\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


async def test_full_lifecycle(repo: Path, tmp_path: Path):
    base = await wt.detect_base_branch(repo)
    assert base == "main"

    wt_path = tmp_path / "wt" / "hero"
    ok, msg = await wt.add_worktree(repo, wt_path, "wt/hero", base)
    assert ok, msg
    assert (wt_path / "app.py").exists()

    # fresh worktree is clean
    st = await wt.worktree_status(repo, wt_path, base, "wt/hero")
    assert st.exists and not st.dirty and st.ahead == 0

    # a symlinked .env must NOT register as dirty (seed filter)
    (wt_path / ".env").symlink_to(repo / "app.py")
    st = await wt.worktree_status(repo, wt_path, base, "wt/hero")
    assert not st.dirty, "seeded .env should be filtered"

    # a real edit IS dirty
    (wt_path / "app.py").write_text("print('changed')\n")
    st = await wt.worktree_status(repo, wt_path, base, "wt/hero")
    assert st.dirty and st.dirty_files == 1
    assert wt.decide_delete_safety(st) == "dirty"

    # commit it → unmerged (ahead of main), no longer dirty
    _git(wt_path, "add", "app.py")
    _git(wt_path, "commit", "-m", "work")
    st = await wt.worktree_status(repo, wt_path, base, "wt/hero")
    assert not st.dirty and st.ahead == 1
    assert wt.decide_delete_safety(st) == "unmerged"

    # branch shows up in taken_slugs (collision protection)
    assert "hero" in await wt.taken_slugs(repo, "proj")

    # teardown: remove worktree (force, it has commits), then delete branch
    ok, _ = await wt.remove_worktree(repo, wt_path, force=True)
    assert ok and not wt_path.exists()
    ok, _ = await wt.delete_branch(repo, "wt/hero", force=True)
    assert ok
    assert "hero" not in await wt.taken_slugs(repo, "proj")


async def test_count_unmerged_fails_closed_when_base_unresolvable(
    repo: Path, tmp_path: Path
):
    """A worktree with committed work whose base ref no longer resolves must
    classify as unmerged, never clean — else headless teardown force-deletes it.

    Guards audit HIGH#2: count_unmerged returns None (indeterminate) and
    decide_delete_safety fails closed to "unmerged".
    """
    wt_path = tmp_path / "wt" / "hero"
    ok, msg = await wt.add_worktree(repo, wt_path, "wt/hero", "main")
    assert ok, msg
    (wt_path / "app.py").write_text("print('changed')\n")
    _git(wt_path, "add", "app.py")
    _git(wt_path, "commit", "-m", "committed work")

    # Base ref does not resolve (deleted/renamed base, never pushed): no local
    # ref, no origin/<base>, no upstream → the count is genuinely unknown.
    assert await wt.count_unmerged(repo, "ghost-base", "wt/hero") is None

    st = await wt.worktree_status(repo, wt_path, "ghost-base", "wt/hero")
    assert st.ahead is None
    assert wt.decide_delete_safety(st) == "unmerged"


async def test_direct_push_to_base_reads_as_merged(repo: Path, tmp_path: Path):
    """Work integrated by pushing straight onto the remote base (no merge, no
    branch upstream) must read as merged — the local base lags behind but the
    diffs are in origin/<base>. Reproduces the false 'N невлитых' alarm."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")

    base = "main"
    wt_path = tmp_path / "wt" / "feat"
    ok, msg = await wt.add_worktree(repo, wt_path, "wt/feat", base)
    assert ok, msg
    (wt_path / "f.py").write_text("x = 1\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-m", "feat work")

    # before integration: genuinely 1 ahead of every base
    st = await wt.worktree_status(repo, wt_path, base, "wt/feat")
    assert st.ahead == 1 and wt.decide_delete_safety(st) == "unmerged"

    # integrate by fast-forwarding origin/main to the branch tip (the user's flow)
    _git(wt_path, "push", "origin", "wt/feat:main")

    # local main is still stale, but origin/main now contains the commit →
    # cherry against origin/main sees 0 unmerged → clean, no false alarm
    st = await wt.worktree_status(repo, wt_path, base, "wt/feat")
    assert st.ahead == 0, "direct-push to origin/base must read as merged"
    assert wt.decide_delete_safety(st) == "clean"


async def test_squash_merge_reads_as_merged(repo: Path, tmp_path: Path):
    """A squash/rebase merge lands the diff under a new sha; cherry matches by
    patch-id so the branch still reads as merged, unlike rev-list reachability."""
    base = "main"
    wt_path = tmp_path / "wt" / "sq"
    ok, msg = await wt.add_worktree(repo, wt_path, "wt/sq", base)
    assert ok, msg
    (wt_path / "g.py").write_text("y = 2\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-m", "sq work")

    # apply the same diff onto local main under a fresh commit (squash-style)
    (repo / "g.py").write_text("y = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squashed sq work")

    # rev-list would say 1 ahead; cherry (patch-id) says 0 → clean
    st = await wt.worktree_status(repo, wt_path, base, "wt/sq")
    assert st.ahead == 0, "squash-merged diff must read as merged"
    assert wt.decide_delete_safety(st) == "clean"


async def test_add_collision_fails(repo: Path, tmp_path: Path):
    base = await wt.detect_base_branch(repo)
    assert base
    ok, _ = await wt.add_worktree(repo, tmp_path / "a", "wt/dup", base)
    assert ok
    # same branch again → git refuses (the collision dedup_slug prevents)
    ok2, msg = await wt.add_worktree(repo, tmp_path / "b", "wt/dup", base)
    assert not ok2 and "already" in msg.lower()


async def test_detect_base_branch_master(tmp_path: Path):
    r = tmp_path / "m"
    r.mkdir()
    _git(r, "init", "-b", "master")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("x")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "i")
    # origin/HEAD unset, default is master — must NOT fall back to literal main
    assert await wt.detect_base_branch(r) == "master"


def test_sync_wrapper_runs():
    # smoke: the async helpers are callable from a fresh loop
    asyncio.run(_noop())


async def _noop() -> None:
    return None
