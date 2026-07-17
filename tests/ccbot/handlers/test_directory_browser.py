"""Tests for directory_browser — the CCBOT_BROWSE_ROOT sandbox.

Unset (default): legacy behavior — browsing starts at $HOME and «..» goes all
the way to /. Set: browsing starts at the root, the up button disappears AT
the root, and a path OUTSIDE the root gets no up button either (a remembered
dir from an older config must not become an escape hatch). The «..» handler is
clamped independently of the button, so a stale/crafted callback can't escape.
"""

from pathlib import Path

import pytest

from ccbot.config import config
from ccbot.handlers.callback_data import CB_DIR_UP
from ccbot.handlers.directory_browser import (
    browse_start_path,
    build_directory_browser,
    clamp_parent_path,
)


@pytest.fixture
def browse_root(monkeypatch, tmp_path):
    """A sandbox root with one subdir, applied to the config singleton."""
    root = tmp_path / "projects"
    (root / "demo").mkdir(parents=True)
    monkeypatch.setattr(config, "browse_root", root.resolve())
    return root.resolve()


def _has_up_button(keyboard) -> bool:
    return any(
        btn.callback_data == CB_DIR_UP
        for row in keyboard.inline_keyboard
        for btn in row
    )


class TestBrowseRootUnset:
    def test_start_is_home(self, monkeypatch):
        monkeypatch.setattr(config, "browse_root", None)
        assert browse_start_path() == str(Path.home())

    def test_up_allowed_from_home(self, monkeypatch):
        monkeypatch.setattr(config, "browse_root", None)
        _, keyboard, _ = build_directory_browser(str(Path.home()))
        assert _has_up_button(keyboard)

    def test_clamp_is_plain_parent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "browse_root", None)
        sub = tmp_path / "a"
        sub.mkdir()
        assert clamp_parent_path(str(sub)) == str(tmp_path)


class TestBrowseRootSet:
    def test_start_is_root(self, browse_root):
        assert browse_start_path() == str(browse_root)

    def test_no_up_button_at_root(self, browse_root):
        _, keyboard, _ = build_directory_browser(str(browse_root))
        assert not _has_up_button(keyboard)

    def test_up_button_inside_root(self, browse_root):
        _, keyboard, _ = build_directory_browser(str(browse_root / "demo"))
        assert _has_up_button(keyboard)

    def test_no_up_button_outside_root(self, browse_root, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        _, keyboard, _ = build_directory_browser(str(outside))
        assert not _has_up_button(keyboard)

    def test_clamp_inside_root_goes_to_parent(self, browse_root):
        assert clamp_parent_path(str(browse_root / "demo")) == str(browse_root)

    def test_clamp_at_root_stays(self, browse_root):
        assert clamp_parent_path(str(browse_root)) == str(browse_root)

    def test_clamp_outside_root_snaps_back(self, browse_root, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        assert clamp_parent_path(str(outside)) == str(browse_root)

    def test_missing_path_falls_back_to_root(self, browse_root):
        """A dead remembered path re-roots at the sandbox, not cwd."""
        text, _, _ = build_directory_browser(str(browse_root / "gone"))
        assert "projects" in text
