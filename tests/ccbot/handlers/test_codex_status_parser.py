"""Tests for codex_status_parser — the panel Context button's codex renderer.

Codex has no /context; the Context button runs /status, whose box is parsed
here into the session-status fields. Fixture is synthetic (public repo — no real
account / paths); it mirrors the live /status layout.
"""

from ccbot.handlers.codex_status_parser import (
    parse_status_output,
    format_status_message,
)

_STATUS_PANE = """\
╭─────────────────────────────────────────────────────────────────╮
│  >_ OpenAI Codex (v0.144.4)                                     │
│                                                                 │
│ Visit https://chatgpt.com/codex/settings/usage for up-to-date   │
│                                                                 │
│  Model:                gpt-5.5 (reasoning medium, summaries auto) │
│  Directory:            /home/user/project                       │
│  Permissions:          Custom (workspace with network access, never) │
│  Agents.md:            <none>                                   │
│  Account:              user@example.com (Plus)                  │
│  Collaboration mode:   Default                                  │
│  Session:              0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee      │
│  Weekly limit:         [████████████████████] 99% left (resets 15:18 on 22 Jul) │
╰─────────────────────────────────────────────────────────────────╯
› Explain this codebase
  gpt-5.5 medium · /home/user/project
"""


class TestParse:
    def test_extracts_fields(self):
        d = parse_status_output(_STATUS_PANE)
        assert d is not None
        assert d["model"] == "gpt-5.5 (reasoning medium, summaries auto)"
        assert d["account"] == "user@example.com (Plus)"
        assert d["permissions"] == "Custom (workspace with network access, never)"
        assert d["collaboration_mode"] == "Default"

    def test_strips_progress_bar_from_weekly_limit(self):
        d = parse_status_output(_STATUS_PANE)
        assert d is not None
        # The "[████…]" bar is dropped, the human part kept.
        assert d["weekly_limit"] == "99% left (resets 15:18 on 22 Jul)"
        assert "█" not in d["weekly_limit"]

    def test_takes_freshest_of_repeated_status(self):
        # Two /status invocations stacked → last one's values win.
        second = _STATUS_PANE.replace("gpt-5.5", "gpt-6-preview")
        d = parse_status_output(_STATUS_PANE + second)
        assert d is not None
        assert d["model"].startswith("gpt-6-preview")

    def test_parse_miss_returns_none(self):
        assert parse_status_output("just a reply\n› prompt\n") is None
        assert parse_status_output("") is None


class TestFormat:
    def test_renders_narrow_tree(self):
        body = format_status_message(parse_status_output(_STATUS_PANE))
        assert "📊" in body.splitlines()[0]
        assert "gpt-5.5 (reasoning medium, summaries auto)" in body
        # Tree rows for the surfaced fields.
        assert "user@example.com (Plus)" in body
        assert "99% left" in body
        assert "└" in body  # last row gets the closing tree glyph

    def test_missing_fields_omitted(self):
        body = format_status_message(
            {
                "model": "gpt-5.5",
                "account": None,
                "permissions": None,
                "collaboration_mode": None,
                "weekly_limit": None,
            }
        )
        # Header + model, no dangling tree rows.
        assert "gpt-5.5" in body
        assert "├" not in body and "└" not in body
