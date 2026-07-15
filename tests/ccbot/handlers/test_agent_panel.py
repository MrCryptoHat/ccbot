"""Tests for the runtime-capability-gated agent panel.

The panel shows only the buttons the bound agent's runtime supports — Claude
Code's full set vs Codex's subset — plus the 🌳 worktree button only when the
topic can actually fork a git repo. Gating lives behind
runtimes.AgentRuntime.panel_actions + session_manager wrappers, so the keyboard
builder has no `if codex:` branch and a third agent needs no builder change.
"""

from __future__ import annotations

import pytest

from ccbot.handlers.commands import _build_commands_keyboard
from ccbot.runtimes import CLAUDE, CODEX
from ccbot.session import WindowState, session_manager
from ccbot.worktrees import is_git_repo


# ── runtime capability declarations ───────────────────────────────────────


class TestPanelActions:
    def test_claude_has_full_set(self):
        for a in (
            "mode",
            "effort",
            "compact",
            "clear",
            "model",
            "context",
            "mcp",
            "background",
            "worktree",
        ):
            assert CLAUDE.supports_panel_action(a), a

    def test_codex_subset(self):
        # context → /status, mode → Shift+Tab (both wired to codex equivalents).
        for a in ("compact", "clear", "model", "mcp", "context", "mode"):
            assert CODEX.supports_panel_action(a), a
        # Still no codex equivalent for these.
        for a in ("effort", "background", "worktree"):
            assert not CODEX.supports_panel_action(a), a

    def test_context_slash_differs_by_runtime(self):
        # Codex has no /context — its Context button runs /status.
        assert CLAUDE.panel_slash("context") == "/context"
        assert CODEX.panel_slash("context") == "/status"
        # Shared commands resolve the same for both.
        assert CODEX.panel_slash("clear") == CLAUDE.panel_slash("clear") == "/clear"


class TestIsGitRepo:
    def test_repo_with_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert is_git_repo(tmp_path) is True

    def test_repo_with_git_file(self, tmp_path):
        # A worktree checkout carries a .git FILE (gitdir pointer), not a dir.
        (tmp_path / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
        assert is_git_repo(tmp_path) is True

    def test_plain_dir_is_not_repo(self, tmp_path):
        assert is_git_repo(tmp_path) is False

    def test_none_is_not_repo(self):
        assert is_git_repo(None) is False


# ── session_manager wrappers ──────────────────────────────────────────────


@pytest.fixture
def panel_window():
    """Register windows in the global session_manager; clean up after."""
    created: list[str] = []

    def _make(wid: str, runtime: str, *, cwd=None) -> str:
        ws = WindowState(runtime=runtime)
        ws.cwd = str(cwd) if cwd else ""
        session_manager.window_states[wid] = ws
        created.append(wid)
        return wid

    yield _make
    for wid in created:
        session_manager.window_states.pop(wid, None)


class TestWrappers:
    def test_agent_supports_dispatches(self, panel_window):
        c = panel_window("@1", "claude")
        x = panel_window("@2", "codex")
        assert session_manager.agent_supports(c, "effort") is True
        assert session_manager.agent_supports(x, "effort") is False
        assert session_manager.agent_supports(x, "compact") is True
        # codex DOES support context (→ /status) and mode (→ Shift+Tab).
        assert session_manager.agent_supports(x, "context") is True

    def test_can_offer_worktree_needs_repo_and_runtime(self, panel_window, tmp_path):
        (tmp_path / ".git").mkdir()
        repo = panel_window("@3", "claude", cwd=tmp_path)
        plain = panel_window("@4", "claude", cwd=tmp_path.parent)  # no .git
        codex_repo = panel_window("@5", "codex", cwd=tmp_path)
        assert session_manager.can_offer_worktree(repo) is True
        assert session_manager.can_offer_worktree(plain) is False
        # Even in a repo, codex worktrees aren't built → hidden.
        assert session_manager.can_offer_worktree(codex_repo) is False


# ── the keyboard builder itself ───────────────────────────────────────────


def _prefixes(window_id: str) -> set[str]:
    """Every callback_data across the panel's three tabs."""
    out: set[str] = set()
    for tab in ("nav", "act", "ses"):
        kb = _build_commands_keyboard(window_id, tab=tab)
        for row in kb.inline_keyboard:
            for btn in row:
                if isinstance(btn.callback_data, str):
                    out.add(btn.callback_data)
    return out


def _has(prefixes: set[str], sub: str) -> bool:
    return any(p.startswith(sub) for p in prefixes)


class TestKeyboardGating:
    def test_claude_full_panel_in_git_repo(self, panel_window, tmp_path):
        (tmp_path / ".git").mkdir()
        wid = panel_window("@10", "claude", cwd=tmp_path)
        p = _prefixes(wid)
        for sub in (
            "cm:model:",
            "cm:ctx:",
            "cm:mcp:",
            "cm:effort:",
            "cm:mcyc:",
            "cm:compact:",
            "cm:clear:",
            "kb:cb:",
            "cm:resume:",
            "cm:restart:",
            "cm:kill:",
            "wt:new:",
        ):
            assert _has(p, sub), f"claude panel missing {sub}"

    def test_claude_worktree_hidden_outside_repo(self, panel_window, tmp_path):
        # No .git → 🌳 must not show (regression: it used to always appear).
        wid = panel_window("@11", "claude", cwd=tmp_path)
        p = _prefixes(wid)
        assert not _has(p, "wt:new:")
        # Claude-specific buttons still present.
        assert _has(p, "cm:ctx:") and _has(p, "kb:cb:")

    def test_codex_panel_hides_unsupported(self, panel_window, tmp_path):
        (tmp_path / ".git").mkdir()
        wid = panel_window("@12", "codex", cwd=tmp_path)
        p = _prefixes(wid)
        # Supported by codex — incl. Context (→ /status) and Mode (→ Shift+Tab).
        for sub in (
            "cm:model:",
            "cm:mcp:",
            "cm:compact:",
            "cm:clear:",
            "cm:ctx:",
            "cm:mcyc:",
            "cm:resume:",
            "cm:restart:",
            "cm:kill:",
        ):
            assert _has(p, sub), f"codex panel missing {sub}"
        # No codex equivalent → hidden.
        for sub in ("cm:effort:", "kb:cb:", "wt:new:"):
            assert not _has(p, sub), f"codex panel should not have {sub}"
        # Universal keys survive.
        assert _has(p, "kb:esc:") and _has(p, "kb:ent:")

    def test_codex_act_tab_packs_without_gaps(self, panel_window):
        # Effort gone (folded into /model), Mode/Compact/Clear stay → 3 buttons
        # pack into 2 rows [Mode, Compact] / [Clear], no empty row.
        wid = panel_window("@13", "codex")
        kb = _build_commands_keyboard(wid, tab="act")
        # tab row + 2 body rows + refresh row = 4 rows.
        assert len(kb.inline_keyboard) == 4
        assert len(kb.inline_keyboard[1]) == 2  # Mode + Compact
        assert len(kb.inline_keyboard[2]) == 1  # Clear
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert any(str(d).startswith("cm:mcyc:") for d in flat)  # Mode present
        assert not any(str(d).startswith("cm:effort:") for d in flat)  # Effort gone
