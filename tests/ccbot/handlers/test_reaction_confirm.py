"""Tests for handlers.reaction_confirm — 👍-to-confirm logic."""

import pytest
from telegram import ReactionTypeCustomEmoji, ReactionTypeEmoji

from ccbot.handlers import reaction_confirm as rc


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset module-global state so tests don't bleed into each other."""
    rc._msg_index.clear()
    rc._pending.clear()
    yield
    rc._msg_index.clear()
    rc._pending.clear()


# ── decide_confirm_action ────────────────────────────────────────────────


@pytest.mark.parametrize("pane", [None, "", "   \n  "])
def test_decide_skip_on_empty(pane):
    assert rc.decide_confirm_action(pane) == ("skip", ())


def test_decide_enter_on_exit_plan(sample_pane_exit_plan):
    assert rc.decide_confirm_action(sample_pane_exit_plan) == ("confirm", ("Enter",))


def test_decide_enter_on_permission(sample_pane_permission):
    assert rc.decide_confirm_action(sample_pane_permission) == ("confirm", ("Enter",))


def test_decide_type_yes_when_idle(sample_pane_no_ui):
    # No interactive UI, not working → idle agent waiting for input.
    assert rc.decide_confirm_action(sample_pane_no_ui) == ("type_yes", ())


def test_decide_skip_when_working():
    pane = (
        "doing things\n"
        "✶ Orbiting… (3m 13s · ↓ 13.9k tokens · esc to interrupt)\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  [Opus 4.7] Context: 41%\n"
    )
    assert rc.decide_confirm_action(pane) == ("skip", ())


def test_decide_skip_when_codex_working():
    # A busy codex pane has no ─ chrome — the DEFAULT (claude) detector reads it
    # as idle ("type_yes"), which is the bug. Passing runtime="codex" makes the
    # busy-state visible so a 👍 is correctly skipped, not typed into a live turn.
    pane = (
        "◦ Working (4s • esc to interrupt)\n"
        "› Summarize recent commits\n"
        "  gpt-5.5 medium · /home/user/project\n"
    )
    assert rc.decide_confirm_action(pane, "codex") == ("skip", ())
    # Same pane misjudged as idle under the wrong runtime — documents WHY the
    # caller must pass the bound window's runtime.
    assert rc.decide_confirm_action(pane, "claude") == ("type_yes", ())


def test_decide_enter_on_codex_menu():
    # A codex approval / choice menu is a runtime-agnostic interactive UI →
    # a 👍 presses Enter, same as Claude's AskUserQuestion.
    pane = (
        "  Would you like to run the following command?\n"
        "  $ ls -la\n"
        "› 1. Yes, proceed (y)\n"
        "  2. No, and tell Codex what to do differently (esc)\n"
        "  Press enter to confirm or esc to cancel\n"
    )
    assert rc.decide_confirm_action(pane, "codex") == ("confirm", ("Enter",))


# ── _has_confirm_emoji ───────────────────────────────────────────────────


def test_has_confirm_emoji_match():
    assert rc._has_confirm_emoji([ReactionTypeEmoji(emoji="👍")]) is True


def test_has_confirm_emoji_other_emoji():
    assert rc._has_confirm_emoji([ReactionTypeEmoji(emoji="❤")]) is False


def test_has_confirm_emoji_empty():
    assert rc._has_confirm_emoji([]) is False
    assert rc._has_confirm_emoji(()) is False


def test_has_confirm_emoji_ignores_custom_emoji():
    assert (
        rc._has_confirm_emoji([ReactionTypeCustomEmoji(custom_emoji_id="123")]) is False
    )


# ── note_topic_message / LRU index ───────────────────────────────────────


def test_note_records_and_resolves():
    rc.note_topic_message(chat_id=-100, message_id=42, user_id=7, thread_id=5)
    assert rc._msg_index[(-100, 42)] == (7, 5)


def test_note_thread_none_stored_as_zero():
    rc.note_topic_message(chat_id=-100, message_id=1, user_id=7, thread_id=None)
    assert rc._msg_index[(-100, 1)] == (7, 0)


def test_note_lru_eviction():
    for i in range(rc._MSG_INDEX_MAX + 5):
        rc.note_topic_message(chat_id=-100, message_id=i, user_id=1, thread_id=2)
    assert len(rc._msg_index) == rc._MSG_INDEX_MAX
    # The five oldest were evicted; the newest survive.
    assert (-100, 0) not in rc._msg_index
    assert (-100, 4) not in rc._msg_index
    assert (-100, 5) in rc._msg_index
    assert (-100, rc._MSG_INDEX_MAX + 4) in rc._msg_index


def test_note_reinsert_refreshes_recency():
    for i in range(rc._MSG_INDEX_MAX):
        rc.note_topic_message(chat_id=-100, message_id=i, user_id=1, thread_id=2)
    # Touch the oldest entry — it should now survive the next eviction.
    rc.note_topic_message(chat_id=-100, message_id=0, user_id=1, thread_id=2)
    rc.note_topic_message(chat_id=-100, message_id=10_000, user_id=1, thread_id=2)
    assert (-100, 0) in rc._msg_index
    assert (-100, 1) not in rc._msg_index  # the new oldest got evicted instead


def test_note_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(rc.config, "reaction_confirm_enabled", False)
    rc.note_topic_message(chat_id=-100, message_id=42, user_id=7, thread_id=5)
    assert (-100, 42) not in rc._msg_index


def test_grok_approval_confirms_yes_once_not_always_approve():
    """👍 on grok's approval prompt must NOT accept the preselected «Yes, and
    don't ask again» (permanent always-approve) — the pattern's confirm_keys
    route it Down (→ «Yes, proceed») then Enter."""
    pane = (
        "  ┃  Remove test directory\n"
        "  ┃  rm -rf ./subdir\n"
        "  ┃\n"
        "  ┃  1 (●) Yes, and don't ask again for anything (always-approve mode)\n"
        "  ┃  2 (○) Yes, proceed\n"
        "  ┃  3 (○) No, reject (type to add feedback)\n"
        "\n"
        "  1/3:select  │  Ctrl+o:always-approve  │  Ctrl+c:cancel\n"
    )
    assert rc.decide_confirm_action(pane, "grok") == ("confirm", ("Down", "Enter"))
