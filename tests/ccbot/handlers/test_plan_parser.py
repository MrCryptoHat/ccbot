"""Tests for plan_parser — extracting the plan-file name from an ExitPlanMode pane.

Claude Code writes the proposed plan to `<claude-home>/plans/<slug>.md` before
the approval widget is answered and renders the path in the widget footer; the
JSONL copy is held until the answer, so pre-approval that file is the only
full-text source. Only the BASENAME may ever be extracted — the pane is
agent-controlled text, and the caller resolves the name against the binding's
own plans dir. The fixture is a real v2.1.3x capture (trimmed).
"""

from ccbot.handlers.plan_parser import extract_plan_file_name

# Real capture: the footer path sits right-aligned on its own line under the
# "ctrl+g to edit in Vim" hint.
PLAN_WIDGET_PANE = """\
  ──────────────────────────────────────────────────────────────────────────────
   Ready to code?

   Here is Claude's plan:
  ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
   Plan: add markdown rendering in a new render.py module

   Context                                                                    ↓
  ──────────────────────────────────────────────────────────────────────────────
   Claude has written up a plan and is ready to execute. Would you like to
   proceed?

   ❯ 1. Yes, and use auto mode
     2. Yes, manually approve edits
     3. No, refine with Ultraplan on Claude Code on the web
     4. Tell Claude what to change
        shift+tab to approve with this feedback

   ctrl+g to edit in Vim  ·
                         ~/.claude/plans/plan-a-tiny-refactor-greedy-treasure.md
"""


class TestExtractPlanFileName:
    def test_real_widget_footer(self):
        assert (
            extract_plan_file_name(PLAN_WIDGET_PANE)
            == "plan-a-tiny-refactor-greedy-treasure.md"
        )

    def test_last_match_wins(self):
        """Scrollback may still show an earlier plan widget above the live one."""
        pane = (
            "  ~/.claude/plans/old-plan.md\n"
            "  ... user answered, agent replanned ...\n"
            "  ~/.claude/plans/new-plan.md\n"
        )
        assert extract_plan_file_name(pane) == "new-plan.md"

    def test_container_home_path(self):
        """Docker agents render their container home — still just the basename."""
        pane = "   /root/.claude/plans/fix-auth-quiet-otter.md\n"
        assert extract_plan_file_name(pane) == "fix-auth-quiet-otter.md"

    def test_no_match_returns_none(self):
        assert extract_plan_file_name("just a shell prompt\n$ ls\n") is None
        assert extract_plan_file_name("") is None

    def test_traversal_cannot_be_expressed(self):
        """A hostile pane can't smuggle a path: the basename charset has no `/`
        and must start alphanumeric, so `..` or nested segments never match."""
        assert extract_plan_file_name("~/.claude/plans/../../etc/passwd.md") is None
        assert extract_plan_file_name("~/.claude/plans/.hidden.md") is None
        # A nested dir after /plans/ yields nothing (not the tail segment).
        assert extract_plan_file_name("~/.claude/plans/sub/inner.md") is None

    def test_wrapped_or_truncated_path_fails_open(self):
        """A mid-path ellipsis (pane truncation) must miss, not mis-extract."""
        assert extract_plan_file_name("~/.claude/plans/plan-…-treasure") is None
