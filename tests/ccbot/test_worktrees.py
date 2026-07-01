"""Tests for ccbot.worktrees pure core: slugify, dedup, dirty-guard, discovery."""

import re
from pathlib import Path

from ccbot import worktrees as wt

# preview.sh valid_slug: ^[a-z0-9][a-z0-9-]{0,29}$
VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")


class TestSlugify:
    def test_cyrillic_transliterates(self):
        assert wt.slugify("редизайн шапки") == "redizain-shapki"

    def test_latin_passthrough(self):
        assert wt.slugify("Fix Login") == "fix-login"

    def test_output_always_valid_for_preview(self):
        for name in [
            "редизайн шапки",
            "🌳 шапка!!!",
            "Привет, мир",
            "a",
            "ЖЗЧШЩ",
            "...",
            "",
        ]:
            assert VALID_SLUG.match(wt.slugify(name)), name

    def test_degenerate_falls_back(self):
        assert wt.slugify("🌳🌳🌳") == wt.slugify("...") == "task"

    def test_collapses_and_trims_dashes(self):
        assert wt.slugify("  a // b  ") == "a-b"

    def test_caps_length(self):
        assert len(wt.slugify("x" * 100)) == wt.SLUG_MAX

    def test_emoji_and_punctuation_stripped(self):
        assert wt.slugify("hero 🌳 v2!") == "hero-v2"


class TestDedupSlug:
    def test_no_collision_returns_as_is(self):
        assert wt.dedup_slug("hero", set()) == "hero"

    def test_appends_counter(self):
        assert wt.dedup_slug("hero", {"hero"}) == "hero-2"

    def test_skips_taken_counters(self):
        assert wt.dedup_slug("hero", {"hero", "hero-2", "hero-3"}) == "hero-4"

    def test_dedup_respects_max_length(self):
        base = "x" * wt.SLUG_MAX
        out = wt.dedup_slug(base, {base})
        assert len(out) <= wt.SLUG_MAX and out.endswith("-2")


class TestBranchName:
    def test_prefix(self):
        assert wt.branch_name("hero") == "wt/hero"


class TestLayout:
    def test_worktree_path_shape(self, monkeypatch):
        monkeypatch.setenv("CCBOT_DIR", "/tmp/cc")
        assert wt.worktree_path("ccbot", "hero") == Path("/tmp/cc/worktrees/ccbot/hero")


class TestParsePorcelain:
    def test_clean(self):
        assert wt.parse_porcelain("") == (False, 0)

    def test_real_changes_count(self):
        out = " M app.py\n?? new.py\n"
        assert wt.parse_porcelain(out) == (True, 2)

    def test_seed_artifacts_filtered(self):
        # symlinked .env + installed node_modules/.venv must not read as dirty
        out = "?? .env\n?? node_modules/\n?? .venv/\n"
        assert wt.parse_porcelain(out) == (False, 0)

    def test_seed_and_real_mixed(self):
        out = "?? .env\n?? node_modules/\n M src/feature.py\n"
        assert wt.parse_porcelain(out) == (True, 1)

    def test_rename_takes_new_path(self):
        out = "R  old.py -> src/new.py\n"
        assert wt.parse_porcelain(out) == (True, 1)


class TestDecideDeleteSafety:
    def _status(self, *, dirty=False, ahead=0):
        return wt.WorktreeStatus(
            dirty=dirty, dirty_files=0, ahead=ahead, base_branch="main", exists=True
        )

    def test_clean(self):
        assert wt.decide_delete_safety(self._status()) == "clean"

    def test_unmerged(self):
        assert wt.decide_delete_safety(self._status(ahead=2)) == "unmerged"

    def test_dirty_wins_over_unmerged(self):
        assert wt.decide_delete_safety(self._status(dirty=True, ahead=2)) == "dirty"

    def test_indeterminate_ahead_fails_closed_to_unmerged(self):
        # ahead=None means count_unmerged could not determine merge status
        # (no candidate base ref resolved). Must NOT be treated as clean, else
        # a headless teardown would `git branch -D` committed work. (audit HIGH#2)
        assert wt.decide_delete_safety(self._status(ahead=None)) == "unmerged"


class TestSeedCommands:
    def test_uv_for_python(self, tmp_path: Path):
        (tmp_path / "uv.lock").write_text("")
        labels = [c[0] for c in wt._seed_commands(tmp_path)]
        assert labels == ["uv sync"]

    def test_pnpm_vs_npm_by_lockfile(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pnpm-lock.yaml").write_text("")
        assert wt._seed_commands(tmp_path)[0][1][0] == "pnpm"

    def test_npm_without_pnpm_lock(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{}")
        assert wt._seed_commands(tmp_path)[0][1][0] == "npm"

    def test_none_for_empty(self, tmp_path: Path):
        assert wt._seed_commands(tmp_path) == []


class TestListGitRepos:
    def test_finds_repos_skips_infra(self, tmp_path: Path):
        (tmp_path / "proj" / ".git").mkdir(parents=True)
        (tmp_path / "_infra" / ".git").mkdir(parents=True)
        (tmp_path / ".hidden" / ".git").mkdir(parents=True)
        (tmp_path / "notrepo").mkdir()
        found = [p.name for p in wt.list_git_repos([tmp_path])]
        assert found == ["proj"]

    def test_exclude_path(self, tmp_path: Path):
        (tmp_path / "a" / ".git").mkdir(parents=True)
        (tmp_path / "b" / ".git").mkdir(parents=True)
        found = [
            p.name for p in wt.list_git_repos([tmp_path], exclude=[tmp_path / "b"])
        ]
        assert found == ["a"]


class TestPreviewSlugsUnder:
    def test_matches_cwd_under_worktree(self, tmp_path: Path):
        wt_dir = tmp_path / "wt" / "hero"
        wt_dir.mkdir(parents=True)
        registry = {
            "hero": {"cwd": str(wt_dir)},
            "sub": {"cwd": str(wt_dir / "frontend")},
            "other": {"cwd": str(tmp_path / "elsewhere")},
            "bad": "notadict",
        }
        assert sorted(wt.preview_slugs_under(registry, wt_dir)) == ["hero", "sub"]
