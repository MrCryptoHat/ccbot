"""Tests for topic-name → tmux directory auto-bind helper.

Covers ``_find_matching_dir_for_topic``: pure function consulted by
``topic_created_handler`` to decide whether a fresh topic should skip
the directory browser; and ``_topic_name_from_root``: recovers a
pre-existing topic's name from the root service message Telegram
attaches as ``reply_to_message``.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot import _topic_name_from_root
from ccbot.handlers import commands as cmd
from ccbot.handlers.commands import _find_matching_dir_for_topic


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand up a fake ``$HOME`` with ``projects/`` and ``agents/`` subtrees."""
    (tmp_path / "projects" / "ccbot").mkdir(parents=True)
    (tmp_path / "projects" / "flashcards-bot").mkdir(parents=True)
    (tmp_path / "agents" / "admin").mkdir(parents=True)
    (tmp_path / "agents" / "_tools").mkdir(parents=True)
    (tmp_path / "agents" / "_plans").mkdir(parents=True)
    (tmp_path / "agents" / "dup").mkdir(parents=True)
    (tmp_path / "projects" / "dup").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_match_project(fake_home: Path) -> None:
    assert _find_matching_dir_for_topic("ccbot") == fake_home / "projects" / "ccbot"


def test_match_agent(fake_home: Path) -> None:
    assert _find_matching_dir_for_topic("admin") == fake_home / "agents" / "admin"


def test_projects_wins_over_agents(fake_home: Path) -> None:
    # Same name in both: projects/ is checked first (project repos common case).
    assert _find_matching_dir_for_topic("dup") == fake_home / "projects" / "dup"


def test_match_case_insensitive(fake_home: Path) -> None:
    # Topic "VPN" (capitalized) must bind to the lowercase folder "ccbot"-style
    # dir. Folders are lowercase; topic names often aren't.
    assert _find_matching_dir_for_topic("CCBOT") == fake_home / "projects" / "ccbot"
    assert _find_matching_dir_for_topic("Admin") == fake_home / "agents" / "admin"


def test_case_insensitive_skips_infra(fake_home: Path) -> None:
    # Case-insensitive scan must still skip underscore-prefixed infra dirs.
    assert _find_matching_dir_for_topic("Tools") is None  # no ~/agents/tools


def test_no_match_returns_none(fake_home: Path) -> None:
    assert _find_matching_dir_for_topic("does-not-exist") is None


def test_empty_name_rejected(fake_home: Path) -> None:
    assert _find_matching_dir_for_topic("") is None


def test_underscore_prefix_rejected(fake_home: Path) -> None:
    # Server infra dirs (_tools, _plans, _docker, ...) must not auto-bind even
    # when a user names a topic after them.
    assert _find_matching_dir_for_topic("_tools") is None
    assert _find_matching_dir_for_topic("_plans") is None


def test_dotfile_rejected(fake_home: Path) -> None:
    assert _find_matching_dir_for_topic(".cache") is None


def test_path_traversal_rejected(fake_home: Path) -> None:
    assert _find_matching_dir_for_topic("../etc") is None
    assert _find_matching_dir_for_topic("foo/bar") is None
    assert _find_matching_dir_for_topic("foo\\bar") is None
    assert _find_matching_dir_for_topic("..") is None


# --- _topic_name_from_root: recover topic name from reply_to_message root ---


def _msg(reply_to: object) -> SimpleNamespace:
    return SimpleNamespace(reply_to_message=reply_to)


def test_topic_name_from_root_present() -> None:
    # A bare in-topic message: Telegram sets reply_to_message to the topic's
    # root forum_topic_created service message, which carries the name.
    root = SimpleNamespace(forum_topic_created=SimpleNamespace(name="VPN"))
    assert _topic_name_from_root(_msg(root)) == "VPN"


def test_topic_name_from_root_stripped() -> None:
    root = SimpleNamespace(forum_topic_created=SimpleNamespace(name="  ccbot  "))
    assert _topic_name_from_root(_msg(root)) == "ccbot"


def test_topic_name_no_reply() -> None:
    # First message in General or a non-topic message — nothing to recover.
    assert _topic_name_from_root(_msg(None)) is None


def test_topic_name_reply_to_plain_message() -> None:
    # User explicitly replied to another message in the topic: reply_to_message
    # is that message (no forum_topic_created) → fail open.
    plain = SimpleNamespace(forum_topic_created=None)
    assert _topic_name_from_root(_msg(plain)) is None


def test_topic_name_empty_rejected() -> None:
    root = SimpleNamespace(forum_topic_created=SimpleNamespace(name=""))
    assert _topic_name_from_root(_msg(root)) is None
    root_none = SimpleNamespace(forum_topic_created=SimpleNamespace(name=None))
    assert _topic_name_from_root(_msg(root_none)) is None


# --- _auto_bind_to_directory: transparent resume (CCBOT_AUTO_RESUME_AGENTS) ---


def _autobind_mocks(sessions):
    """Patch the module deps _auto_bind_to_directory touches. Returns (sm, tm)."""
    sm = MagicMock()
    sm.record_thread_directory = MagicMock()
    sm.list_sessions_for_directory = AsyncMock(return_value=sessions)
    sm.wait_for_session_map_entry = AsyncMock(return_value=True)
    sm.bind_thread = MagicMock()
    sm.get_window_state = MagicMock(
        return_value=SimpleNamespace(session_id="", cwd="", window_name="")
    )
    sm._save_state = MagicMock()
    tm = MagicMock()
    tm.create_window = AsyncMock(return_value=(True, "ok", "editor", "@7"))
    return sm, tm


NEWEST = "11111111-2222-3333-4444-555555555555"
OLDER = "00000000-1111-2222-3333-444444444444"


@pytest.mark.asyncio
async def test_auto_resume_agents_resumes_newest_without_picker(tmp_path: Path):
    """Flag ON + existing sessions → resume the newest, no picker (admin fix).

    list_sessions_for_directory is newest-first, so sessions[0] is resumed and
    window_state is pinned to it (the --resume transcript-tracking override).
    """
    d = tmp_path / "editor"
    d.mkdir()
    sessions = [SimpleNamespace(session_id=NEWEST), SimpleNamespace(session_id=OLDER)]
    sm, tm = _autobind_mocks(sessions)
    ws = sm.get_window_state.return_value
    ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace())
    with (
        patch.object(cmd, "session_manager", sm),
        patch.object(cmd, "tmux_manager", tm),
        patch.object(cmd.config, "auto_resume_agents", True),
        patch.object(cmd, "safe_reply", new=AsyncMock()),
    ):
        result = await cmd._auto_bind_to_directory(1, 42, d, SimpleNamespace(), ctx)

    assert result is True
    tm.create_window.assert_awaited_once()
    assert tm.create_window.call_args.kwargs["resume_session_id"] == NEWEST
    sm.bind_thread.assert_called_once()
    assert ws.session_id == NEWEST  # window_state pinned to the resumed id
    # No picker state was armed.
    assert ctx.user_data.get(cmd.STATE_KEY) != cmd.STATE_SELECTING_SESSION


@pytest.mark.asyncio
async def test_auto_resume_pins_state_when_hook_times_out(tmp_path: Path):
    """The in-container hook this flag targets is flaky, so a session-map
    timeout is the expected path — window_state must still be fully pinned
    (session_id + cwd + window_name) so the monitor tracks the resumed JSONL."""
    d = tmp_path / "editor"
    d.mkdir()
    sessions = [SimpleNamespace(session_id=NEWEST)]
    sm, tm = _autobind_mocks(sessions)
    sm.wait_for_session_map_entry = AsyncMock(return_value=False)  # hook timed out
    ws = sm.get_window_state.return_value
    ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace())
    with (
        patch.object(cmd, "session_manager", sm),
        patch.object(cmd, "tmux_manager", tm),
        patch.object(cmd.config, "auto_resume_agents", True),
        patch.object(cmd, "safe_reply", new=AsyncMock()),
    ):
        await cmd._auto_bind_to_directory(1, 42, d, SimpleNamespace(), ctx)

    assert ws.session_id == NEWEST
    assert ws.cwd == str(d)
    assert ws.window_name == "editor"
    sm._save_state.assert_called()


@pytest.mark.asyncio
async def test_auto_resume_off_shows_picker(tmp_path: Path):
    """Flag OFF (default) → the interactive session picker, no window created."""
    d = tmp_path / "editor"
    d.mkdir()
    sessions = [SimpleNamespace(session_id=NEWEST)]
    sm, tm = _autobind_mocks(sessions)
    ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace())
    with (
        patch.object(cmd, "session_manager", sm),
        patch.object(cmd, "tmux_manager", tm),
        patch.object(cmd.config, "auto_resume_agents", False),
        patch.object(cmd, "safe_reply", new=AsyncMock()),
        patch.object(cmd, "build_session_picker", return_value=("pick", None)),
    ):
        result = await cmd._auto_bind_to_directory(1, 42, d, SimpleNamespace(), ctx)

    assert result is True
    tm.create_window.assert_not_awaited()  # picker path — no window created
    assert ctx.user_data[cmd.STATE_KEY] == cmd.STATE_SELECTING_SESSION
