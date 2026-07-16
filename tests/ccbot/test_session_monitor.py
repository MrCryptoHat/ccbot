"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import SessionMonitor


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_complete_unparseable_line_is_skipped(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """A complete-but-unparseable line is skipped, not stalled forever.

        Guards audit MEDIUM: a corrupt/rejected line ending in a newline must
        advance the offset so downstream content still reaches the user, rather
        than blocking every later line for the session's lifetime.
        """
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="before")
        entry2 = make_jsonl_entry(msg_type="assistant", content="after")
        jsonl_file.write_text(
            json.dumps(entry1)
            + "\n"
            + "@@@ not json @@@\n"  # complete garbage line (has newline)
            + json.dumps(entry2)
            + "\n",
            encoding="utf-8",
        )
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Both good lines delivered ('after' proves it did not stall), and the
        # offset advanced fully past everything.
        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_partial_line_without_newline_retries(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """A trailing line with no newline is an in-flight write — retry it."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="done")
        good = json.dumps(entry1) + "\n"
        jsonl_file.write_text(good + '{"partial": tru', encoding="utf-8")  # no \n
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Only the complete line delivered; offset stops before the partial so
        # the next cycle re-reads it once the write finishes.
        assert len(result) == 1
        assert session.last_byte_offset == len(good.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestDockerMultiRoot:
    """Docker agents park their JSONL under a bind-mounted claude-home, and
    their SessionStart hook writes a per-agent session_map on host. The
    monitor has to walk every root and merge every map or the Telegram
    side never sees the container's replies.
    """

    def _agent(self, tmp_path, name="assistant"):

        from ccbot.config import DockerAgentConfig

        return DockerAgentConfig(
            name=name,
            container=f"{name}-ctn",
            workspace_host_path=tmp_path / name / "workspace",
            claude_home_host_path=tmp_path / name / "claude-home",
            ipc_dir=tmp_path / name / "ipc",
            session_map_path=tmp_path / name / "session-map.json",
        )

    @pytest.fixture
    def monitor_with_agent(self, tmp_path, monkeypatch):
        """Monitor with main root + one active docker agent, plus on-disk
        directories for each. Returns (monitor, agent, main_root,
        agent_projects_root).
        """
        from ccbot import session_monitor as sm_mod

        main_root = tmp_path / "main"
        main_root.mkdir()
        agent = self._agent(tmp_path)
        agent_projects = agent.claude_home_host_path / "projects"
        agent_projects.mkdir(parents=True)
        monkeypatch.setattr(sm_mod.config, "active_docker_agents", lambda: [agent])
        monitor = SessionMonitor(
            projects_path=main_root,
            state_file=tmp_path / "monitor_state.json",
        )
        return monitor, agent, main_root, agent_projects

    async def test_project_roots_includes_agent(self, monitor_with_agent) -> None:
        monitor, _agent, main_root, agent_projects = monitor_with_agent
        roots = monitor._project_roots()
        assert main_root in roots
        assert agent_projects in roots

    async def test_project_roots_skips_missing_agent_dir(
        self, tmp_path, monkeypatch
    ) -> None:
        """A container not yet started has no claude-home/projects on host;
        the monitor must silently skip it, not crash."""
        from ccbot import session_monitor as sm_mod

        main_root = tmp_path / "main"
        main_root.mkdir()
        agent = self._agent(tmp_path)  # dirs not created
        monkeypatch.setattr(sm_mod.config, "active_docker_agents", lambda: [agent])
        monitor = SessionMonitor(
            projects_path=main_root,
            state_file=tmp_path / "monitor_state.json",
        )
        # Only the main root survives the existence check.
        assert monitor._project_roots() == [main_root]

    async def test_scan_projects_finds_agent_jsonl(self, monitor_with_agent) -> None:
        """A JSONL dropped under the agent projects root is picked up by
        scan_projects — this is the wire that carries Claude's replies
        from inside the container back to ccbot."""
        monitor, _agent, main_root, agent_projects = monitor_with_agent
        # Main root has its own session
        (main_root / "-host-proj").mkdir()
        (main_root / "-host-proj" / "host-sid.jsonl").write_text(
            '{"type":"user","message":{"content":"hi"}}\n'
        )
        # Agent root has an in-container session
        (agent_projects / "-workspace").mkdir()
        (agent_projects / "-workspace" / "agent-sid.jsonl").write_text(
            '{"type":"user","message":{"content":"hi from container"}}\n'
        )
        sessions = await monitor.scan_projects()
        ids = {s.session_id for s in sessions}
        assert "host-sid" in ids
        assert "agent-sid" in ids

    async def test_load_session_map_merges_main_and_agent(
        self, monitor_with_agent, tmp_path, monkeypatch
    ) -> None:
        monitor, agent, _main_root, _agent_projects = monitor_with_agent
        from ccbot import session_monitor as sm_mod

        # Main session_map (tmux prefix format)
        main_map = tmp_path / "main_map.json"
        main_map.write_text(
            json.dumps(
                {
                    "ccbot:@12": {"session_id": "tmux-sid", "cwd": "/foo"},
                    "otherbot:@5": {"session_id": "skip-me", "cwd": "/bar"},
                }
            )
        )
        monkeypatch.setattr(sm_mod.config, "session_map_file", main_map)
        # Per-agent session_map (binding-value key, no prefix)
        agent.session_map_path.parent.mkdir(parents=True, exist_ok=True)
        agent.session_map_path.write_text(
            json.dumps(
                {
                    "docker:assistant": {
                        "session_id": "docker-sid",
                        "cwd": "/workspace",
                    }
                }
            )
        )
        merged = await monitor._load_current_session_map()
        assert merged["@12"] == "tmux-sid"
        assert merged["docker:assistant"] == "docker-sid"
        # Entries from other tmux sessions must not leak through
        assert "otherbot:@5" not in merged and "@5" not in merged


class TestUnicodeDecodeRobustness:
    """The read path must survive UTF-8 split mid-write: Claude appends
    multibyte (cyrillic) text while the monitor reads, so a char cut at
    EOF is a matter of time. Regression: it used to escape the except
    (only OSError was caught) and abort the whole check_for_updates tick
    for every session."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_partial_multibyte_at_eof_keeps_parsed_lines(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="привет")
        good_line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        # First byte of a two-byte cyrillic char — a write caught mid-flush.
        partial_char = "д".encode("utf-8")[:1]
        jsonl_file.write_bytes(good_line + partial_char)

        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        # Must not raise; the complete line is parsed and delivered.
        result = await monitor._read_new_lines(session, jsonl_file)
        assert len(result) == 1

        # Offset committed at the end of the good line — the next cycle
        # re-reads only the partial tail (no duplicates of the good line).
        assert session.last_byte_offset == len(good_line)


class TestContextTokens:
    """Tests for context-fill token extraction from raw JSONL entries."""

    def test_sums_input_cache_tokens(self):
        from ccbot.runtimes import _entry_context_tokens

        entry = {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 9736,
                    "cache_read_input_tokens": 62468,
                    "output_tokens": 555,  # excluded — next turn's input
                }
            },
        }
        assert _entry_context_tokens(entry) == 2 + 9736 + 62468

    def test_non_assistant_returns_none(self):
        from ccbot.runtimes import _entry_context_tokens

        assert _entry_context_tokens({"type": "user", "message": {}}) is None

    def test_missing_usage_returns_none(self):
        from ccbot.runtimes import _entry_context_tokens

        assert _entry_context_tokens({"type": "assistant", "message": {}}) is None

    def test_partial_usage_treats_missing_as_zero(self):
        from ccbot.runtimes import _entry_context_tokens

        entry = {"type": "assistant", "message": {"usage": {"input_tokens": 400_000}}}
        assert _entry_context_tokens(entry) == 400_000


class TestContextThresholds:
    """Tests for per-session context-threshold crossing detection."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def test_no_alert_below_first_threshold(self, monitor):
        assert monitor._check_context_thresholds("s", 250_000) is None

    def test_fires_on_first_crossing(self, monitor):
        alert = monitor._check_context_thresholds("s", 312_000)
        assert alert is not None
        assert "312k" in alert
        assert "31%" in alert

    def test_does_not_refire_same_band(self, monitor):
        assert monitor._check_context_thresholds("s", 312_000) is not None
        # Still above 300k but below 500k — no new threshold crossed.
        assert monitor._check_context_thresholds("s", 350_000) is None
        assert monitor._check_context_thresholds("s", 480_000) is None

    def test_fires_again_on_next_band(self, monitor):
        assert monitor._check_context_thresholds("s", 320_000) is not None
        alert = monitor._check_context_thresholds("s", 510_000)
        assert alert is not None
        assert "51%" in alert

    def test_multiple_bands_crossed_at_once_emits_one(self, monitor):
        # First measurement already at 600k (e.g. after a restart): both
        # 300k and 500k are crossed, but only one alert is emitted.
        alert = monitor._check_context_thresholds("s", 600_000)
        assert alert is not None
        assert "600k" in alert
        # Subsequent same-band measurement is silent.
        assert monitor._check_context_thresholds("s", 650_000) is None

    def test_rearm_after_drop(self, monitor):
        assert monitor._check_context_thresholds("s", 510_000) is not None
        # /compact drops fill below every threshold — re-arms them.
        assert monitor._check_context_thresholds("s", 120_000) is None
        # Genuine re-cross alerts again.
        assert monitor._check_context_thresholds("s", 305_000) is not None

    def test_sessions_are_independent(self, monitor):
        assert monitor._check_context_thresholds("a", 305_000) is not None
        # Different session starts fresh — its own first crossing fires.
        assert monitor._check_context_thresholds("b", 305_000) is not None


class TestDuplicateSessionIdAcrossDirs:
    """`claude --resume <id>` from a DIFFERENT cwd keeps the session id but
    writes a new JSONL under the new cwd's project dir — the same id then
    exists in two files. The monitor must follow the LIVE one (newest mtime)
    and re-track at EOF when the chosen path switches; the old "first found
    wins" arbitrarily silenced the topic (notes → notes-writer incident,
    2026-07-16)."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def _two_copies(self, tmp_path, sid="dup-sid"):
        import os

        root = tmp_path / "projects"
        old_dir = root / "-home-user-agents-notes"
        new_dir = root / "-home-user-projects-notes-writer"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        old = old_dir / f"{sid}.jsonl"
        new = new_dir / f"{sid}.jsonl"
        old.write_text('{"type":"user","message":{"content":"old"}}\n')
        new.write_text('{"type":"user","message":{"content":"old"}}\n' * 2)
        now = 1_752_650_000
        os.utime(old, (now - 3600, now - 3600))
        os.utime(new, (now, now))
        return old, new

    async def test_scan_prefers_newest_mtime(self, monitor, tmp_path) -> None:
        old, new = self._two_copies(tmp_path)
        sessions = await monitor.scan_projects()
        by_id = {s.session_id: s.file_path for s in sessions}
        assert by_id["dup-sid"] == new

    async def test_scan_prefers_newest_regardless_of_dir_order(
        self, monitor, tmp_path
    ) -> None:
        # Same layout but the NEWER file sorts first alphabetically — the
        # winner must still be chosen by mtime, not directory iteration order.
        import os

        root = tmp_path / "projects"
        a = root / "-a-first"
        b = root / "-b-second"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        first = a / "dup2.jsonl"
        second = b / "dup2.jsonl"
        first.write_text("{}\n")
        second.write_text("{}\n")
        now = 1_752_650_000
        os.utime(first, (now, now))
        os.utime(second, (now - 60, now - 60))
        sessions = await monitor.scan_projects()
        by_id = {s.session_id: s.file_path for s in sessions}
        assert by_id["dup2"] == first

    async def test_path_switch_retracks_at_eof(self, monitor, tmp_path) -> None:
        from ccbot.runtimes import CLAUDE

        old, new = self._two_copies(tmp_path)
        # Track the OLD file first (as the pre-fix state would have).
        msgs = await monitor._process_transcript(CLAUDE, "dup-sid", old)
        assert msgs == []  # first track = EOF start
        tracked = monitor.state.get_session("dup-sid")
        assert tracked is not None and tracked.file_path == str(old)

        # The scan now points at the NEW file → re-track at its EOF, no
        # reflood of the copied history, offset valid for the new file.
        msgs = await monitor._process_transcript(CLAUDE, "dup-sid", new)
        assert msgs == []
        tracked = monitor.state.get_session("dup-sid")
        assert tracked is not None
        assert tracked.file_path == str(new)
        assert tracked.last_byte_offset == new.stat().st_size

        # An append to the new file after the switch IS delivered.
        with open(new, "a") as f:
            f.write(
                '{"type":"assistant","message":{"content":'
                '[{"type":"text","text":"reply after switch"}]}}\n'
            )
        msgs = await monitor._process_transcript(CLAUDE, "dup-sid", new)
        assert any("reply after switch" in m.text for m in msgs)
