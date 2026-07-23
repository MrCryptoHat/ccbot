"""Tests for the session picker with the agent-switcher row (directory_browser).

Redesigned 2026-07-23 (design review with the operator): full-width resume
buttons carrying title + short age, «➕ Новая сессия — <agent>», and a single
«🤖 Агент: … ▾» switcher row that opens the runtime menu — replacing the old
wrapping tab row, which didn't scale past a couple of runtimes and squeezed
session titles into 14-char mush.
"""

import pytest

from ccbot.agent_session import AgentSession
from ccbot.handlers.callback_data import (
    CB_RUNTIME_MENU,
    CB_RUNTIME_SELECT,
    CB_RUNTIME_TAB,
    CB_SESSION_BROWSE,
    CB_SESSION_SELECT,
)
from ccbot.handlers.directory_browser import (
    PICKER_SESSION_ROWS,
    build_runtime_menu,
    build_session_picker,
)
from ccbot.runtimes import AgentRuntime


@pytest.fixture(autouse=True)
def _all_runtimes_available(monkeypatch):
    # pickable_runtimes() gates on an installed CLI; pin every runtime
    # "installed" so layout assertions don't depend on the host.
    monkeypatch.setattr(AgentRuntime, "is_available", lambda self: True)


def _flat(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def _cd(button) -> str:
    return button.callback_data or ""


_SESSIONS = [
    AgentSession("id-a", "First task", 5, "/tmp/a.jsonl"),
    AgentSession("id-b", "Second task", 42, "/tmp/b.jsonl"),
]


class TestSessionRows:
    def test_full_width_rows_indexed_in_order(self):
        _, kb = build_session_picker(_SESSIONS, "/home/user/project", "claude")
        resume_rows = [
            row
            for row in kb.inline_keyboard
            if any(_cd(b).startswith(CB_SESSION_SELECT) for b in row)
        ]
        # One session per row (full width), indices in list order.
        assert all(len(row) == 1 for row in resume_rows)
        assert [_cd(row[0]) for row in resume_rows] == [
            f"{CB_SESSION_SELECT}0",
            f"{CB_SESSION_SELECT}1",
        ]
        assert "First task" in resume_rows[0][0].text

    def test_rows_capped(self):
        many = [
            AgentSession(f"id-{i}", f"Task {i}", 1, "/tmp/x.jsonl") for i in range(9)
        ]
        _, kb = build_session_picker(many, "/d", "claude")
        resume = [b for b in _flat(kb) if _cd(b).startswith(CB_SESSION_SELECT)]
        assert len(resume) == PICKER_SESSION_ROWS
        # The cap keeps the prefix — indices still address the cached list.
        assert _cd(resume[0]) == f"{CB_SESSION_SELECT}0"
        assert _cd(resume[-1]) == f"{CB_SESSION_SELECT}{PICKER_SESSION_ROWS - 1}"

    def test_no_numbered_list_in_text(self):
        # Session info lives on the buttons now; the text stays header+folder.
        text, _ = build_session_picker(_SESSIONS, "/home/user/project", "claude")
        assert "First task" not in text
        assert "Second task" not in text


class TestNewSessionAndSwitcher:
    def test_new_session_names_active_agent(self):
        _, kb = build_session_picker(_SESSIONS, "/d", "claude")
        new = [b for b in _flat(kb) if _cd(b) == f"{CB_RUNTIME_SELECT}claude"]
        assert len(new) == 1
        assert "Claude Code" in new[0].text

    def test_switcher_row_names_active_agent(self):
        _, kb = build_session_picker(_SESSIONS, "/d", "codex")
        switcher = [b for b in _flat(kb) if _cd(b) == CB_RUNTIME_MENU]
        assert len(switcher) == 1
        assert "Codex" in switcher[0].text

    def test_single_runtime_hides_switcher(self, monkeypatch):
        # With one CLI installed there's nothing to switch to.
        monkeypatch.setattr(
            AgentRuntime, "is_available", lambda self: self.name == "claude"
        )
        _, kb = build_session_picker(_SESSIONS, "/d", "claude")
        assert not any(_cd(b) == CB_RUNTIME_MENU for b in _flat(kb))

    def test_no_tab_row(self):
        # The old wrapping tab row is gone — runtime switching goes through
        # the menu, so no CB_RUNTIME_TAB buttons in the picker itself.
        _, kb = build_session_picker(_SESSIONS, "/d", "claude")
        assert not any(_cd(b).startswith(CB_RUNTIME_TAB) for b in _flat(kb))

    def test_empty_sessions_still_offers_new_and_switcher(self):
        text, kb = build_session_picker([], "/d", "claude")
        assert not any(_cd(b).startswith(CB_SESSION_SELECT) for b in _flat(kb))
        assert any(_cd(b) == f"{CB_RUNTIME_SELECT}claude" for b in _flat(kb))
        assert any(_cd(b) == CB_RUNTIME_MENU for b in _flat(kb))

    def test_folder_and_cancel_share_a_row(self):
        _, kb = build_session_picker(_SESSIONS, "/d", "claude")
        last = kb.inline_keyboard[-1]
        assert len(last) == 2
        assert _cd(last[0]) == CB_SESSION_BROWSE


class TestRuntimeMenu:
    def test_one_row_per_runtime_via_tab_callback(self):
        _, kb = build_runtime_menu("/d", "claude", 2)
        tab_rows = [
            row
            for row in kb.inline_keyboard
            if any(_cd(b).startswith(CB_RUNTIME_TAB) for b in row)
        ]
        assert all(len(row) == 1 for row in tab_rows)
        data = [_cd(row[0]) for row in tab_rows]
        assert f"{CB_RUNTIME_TAB}claude" in data
        assert f"{CB_RUNTIME_TAB}codex" in data
        assert f"{CB_RUNTIME_TAB}grok" in data

    def test_active_marked_with_count(self):
        _, kb = build_runtime_menu("/d", "claude", 2)
        # Skip the last row: «← Назад» shares the active runtime's callback
        # (that's the design — back IS "re-open the active tab") and would
        # overwrite its label in the dict.
        labels = {_cd(b): b.text for row in kb.inline_keyboard[:-1] for b in row}
        assert labels[f"{CB_RUNTIME_TAB}claude"].startswith("●")
        assert "2" in labels[f"{CB_RUNTIME_TAB}claude"]
        # Inactive rows keep their icon, no count.
        assert labels[f"{CB_RUNTIME_TAB}codex"].startswith("🔵")

    def test_back_returns_to_active_runtime_view(self):
        # «← Назад» is just CB_RUNTIME_TAB for the active runtime — the tab
        # handler re-enumerates sessions and re-renders the picker.
        _, kb = build_runtime_menu("/d", "codex", 0)
        back_row = kb.inline_keyboard[-1]
        assert len(back_row) == 1
        assert _cd(back_row[0]) == f"{CB_RUNTIME_TAB}codex"
