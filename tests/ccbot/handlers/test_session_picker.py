"""Tests for the runtime-tabbed session picker (directory_browser).

The picker shows one tab per runtime (Claude Code / Codex / …) on top and the
active runtime's resumable sessions below, with a "➕ New session" button that
starts a fresh window on the active runtime. Tabs come from the runtime registry
so a new runtime appears automatically.
"""

import pytest

from ccbot.agent_session import AgentSession
from ccbot.handlers.callback_data import (
    CB_RUNTIME_SELECT,
    CB_RUNTIME_TAB,
    CB_SESSION_SELECT,
)
from ccbot.handlers.directory_browser import build_session_picker
from ccbot.runtimes import AgentRuntime


@pytest.fixture(autouse=True)
def _all_runtimes_available(monkeypatch):
    # pickable_runtimes() gates tabs on an installed CLI (shutil.which); pin
    # every runtime "installed" so tab assertions don't depend on the host.
    monkeypatch.setattr(AgentRuntime, "is_available", lambda self: True)


def _flat(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def _cd(button) -> str:
    return button.callback_data or ""


_SESSIONS = [
    AgentSession("id-a", "First task", 5, "/tmp/a.jsonl"),
    AgentSession("id-b", "Second task", 42, "/tmp/b.jsonl"),
]


class TestRuntimeTabs:
    def test_tab_row_has_one_button_per_runtime(self):
        _, kb = build_session_picker(_SESSIONS, "/home/user/project", "claude")
        tab_data = [
            b.callback_data for b in _flat(kb) if _cd(b).startswith(CB_RUNTIME_TAB)
        ]
        assert f"{CB_RUNTIME_TAB}claude" in tab_data
        assert f"{CB_RUNTIME_TAB}codex" in tab_data

    def test_active_tab_marked_with_pointer(self):
        _, kb = build_session_picker(_SESSIONS, "/d", "claude")
        labels = {b.callback_data: b.text for b in _flat(kb)}
        assert labels[f"{CB_RUNTIME_TAB}claude"].startswith("▸")
        assert "Claude Code" in labels[f"{CB_RUNTIME_TAB}claude"]
        # Inactive tab keeps its icon (🟠), no pointer.
        assert not labels[f"{CB_RUNTIME_TAB}codex"].startswith("▸")
        assert "Codex" in labels[f"{CB_RUNTIME_TAB}codex"]

    def test_codex_tab_active_switches_pointer_and_new_button(self):
        _, kb = build_session_picker([], "/d", "codex")
        labels = {b.callback_data: b.text for b in _flat(kb)}
        assert labels[f"{CB_RUNTIME_TAB}codex"].startswith("▸")
        assert not labels[f"{CB_RUNTIME_TAB}claude"].startswith("▸")
        # ➕ New session starts a codex window.
        new = [b for b in _flat(kb) if _cd(b) == f"{CB_RUNTIME_SELECT}codex"]
        assert len(new) == 1


class TestSessionButtons:
    def test_resume_buttons_indexed_in_order(self):
        _, kb = build_session_picker(_SESSIONS, "/d", "claude")
        resume = [b for b in _flat(kb) if _cd(b).startswith(CB_SESSION_SELECT)]
        assert [_cd(b) for b in resume] == [
            f"{CB_SESSION_SELECT}0",
            f"{CB_SESSION_SELECT}1",
        ]
        assert "First task" in resume[0].text

    def test_new_session_targets_active_runtime(self):
        _, kb = build_session_picker(_SESSIONS, "/d", "claude")
        new = [b for b in _flat(kb) if _cd(b) == f"{CB_RUNTIME_SELECT}claude"]
        assert len(new) == 1

    def test_empty_sessions_still_shows_tabs_and_new(self):
        text, kb = build_session_picker([], "/d", "claude")
        # No resume buttons…
        assert not any(_cd(b).startswith(CB_SESSION_SELECT) for b in _flat(kb))
        # …but tabs and a New button are present.
        assert any(_cd(b).startswith(CB_RUNTIME_TAB) for b in _flat(kb))
        assert any(_cd(b) == f"{CB_RUNTIME_SELECT}claude" for b in _flat(kb))

    def test_session_list_rendered_in_text(self):
        text, _ = build_session_picker(_SESSIONS, "/home/user/project", "claude")
        assert "First task" in text
        assert "Second task" in text
