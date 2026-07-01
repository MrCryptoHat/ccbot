"""Tests for handlers.live_board — preview/app-host scan, render, edit loop."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import live_board


# ── _scan_app_hosts ──────────────────────────────────────────────────────


class TestScanAppHosts:
    def _caddy(self, tmp_path: Path, name: str, body: str) -> Path:
        (tmp_path / name).write_text(body)
        return tmp_path / name

    def test_parses_host_and_backend(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(live_board, "CADDY_APPS_DIR", tmp_path)
        self._caddy(
            tmp_path,
            "app.caddy",
            "http://app.example.com:8080 {\n\treverse_proxy 127.0.0.1:3000\n}\n",
        )
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: p == 3000)
        rows = live_board._scan_app_hosts()
        assert rows == [(True, "https://app.example.com · :3000")]

    def test_unhealthy_when_backend_down(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(live_board, "CADDY_APPS_DIR", tmp_path)
        self._caddy(
            tmp_path,
            "app.caddy",
            "http://x.example.com:8080 {\n reverse_proxy 127.0.0.1:9999\n}",
        )
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: False)
        assert live_board._scan_app_hosts() == [
            (False, "https://x.example.com · :9999")
        ]

    def test_multiple_files_sorted(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(live_board, "CADDY_APPS_DIR", tmp_path)
        self._caddy(
            tmp_path, "b.caddy", "http://b.example.com:8080 {\n reverse_proxy :2\n}"
        )
        self._caddy(
            tmp_path, "a.caddy", "http://a.example.com:8080 {\n reverse_proxy :1\n}"
        )
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: True)
        rows = live_board._scan_app_hosts()
        assert [r[1] for r in rows] == [
            "https://a.example.com · :1",
            "https://b.example.com · :2",
        ]

    def test_no_dir_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(live_board, "CADDY_APPS_DIR", tmp_path / "nope")
        assert live_board._scan_app_hosts() == []

    def test_file_without_host_block_skipped(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(live_board, "CADDY_APPS_DIR", tmp_path)
        self._caddy(tmp_path, "junk.caddy", "# just a comment\n")
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: True)
        assert live_board._scan_app_hosts() == []

    def test_host_escaped(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(live_board, "CADDY_APPS_DIR", tmp_path)
        self._caddy(tmp_path, "x.caddy", "http://a<b>.x:8080 {\n reverse_proxy :1\n}")
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: True)
        rows = live_board._scan_app_hosts()
        assert "a&lt;b&gt;.x" in rows[0][1]


# ── _scan_previews ───────────────────────────────────────────────────────


class TestScanPreviews:
    def _write_registry(self, path: Path, data: dict):
        path.write_text(json.dumps(data))

    @pytest.fixture(autouse=True)
    def _no_tmux(self, monkeypatch):
        # Pretend the preview-<slug> tmux session is alive; health then
        # hinges on the port check, which tests control.
        import subprocess as _sp

        class _R:
            returncode = 0

        monkeypatch.setattr(live_board.subprocess, "run", lambda *a, **k: _R())
        # keep a ref so the import isn't flagged unused by linters
        assert _sp is not None

    def test_healthy_preview(self, tmp_path: Path, monkeypatch):
        reg = tmp_path / "registry.json"
        self._write_registry(
            reg, {"foo": {"port": 5001, "ttl": "2h", "started": "2026-05-11T10:00:00Z"}}
        )
        monkeypatch.setattr(live_board.preview, "REGISTRY_PATH", str(reg))
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: True)
        monkeypatch.setattr(live_board.preview, "ttl_remaining", lambda *a: "1h23m")
        monkeypatch.setattr(live_board.config, "preview_domain", "example.dev")
        rows = live_board._scan_previews()
        assert rows == [(True, "foo · https://preview-foo.example.dev · 1h23m")]

    def test_unhealthy_when_port_dead(self, tmp_path: Path, monkeypatch):
        reg = tmp_path / "registry.json"
        self._write_registry(reg, {"bar": {"port": 5002, "ttl": "1h", "started": ""}})
        monkeypatch.setattr(live_board.preview, "REGISTRY_PATH", str(reg))
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: False)
        monkeypatch.setattr(live_board.preview, "ttl_remaining", lambda *a: "45m")
        monkeypatch.setattr(live_board.config, "preview_domain", "example.dev")
        assert live_board._scan_previews() == [
            (False, "bar · https://preview-bar.example.dev · 45m")
        ]

    def test_missing_registry_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            live_board.preview, "REGISTRY_PATH", str(tmp_path / "gone.json")
        )
        assert live_board._scan_previews() == []

    def test_sorted_by_slug(self, tmp_path: Path, monkeypatch):
        reg = tmp_path / "registry.json"
        self._write_registry(
            reg,
            {
                "zeta": {"port": 1, "ttl": "1h", "started": ""},
                "alpha": {"port": 2, "ttl": "1h", "started": ""},
            },
        )
        monkeypatch.setattr(live_board.preview, "REGISTRY_PATH", str(reg))
        monkeypatch.setattr(live_board.preview, "port_listening", lambda p: True)
        monkeypatch.setattr(live_board.preview, "ttl_remaining", lambda *a: "x")
        rows = live_board._scan_previews()
        assert [r[1].split(" ")[0] for r in rows] == ["alpha", "zeta"]


# ── _render_body / _compose ──────────────────────────────────────────────


class TestRenderBody:
    def test_both_sections(self, monkeypatch):
        monkeypatch.setattr(
            live_board,
            "_scan_previews",
            lambda: [(True, "foo · https://preview-foo.example.com · 1h")],
        )
        monkeypatch.setattr(
            live_board, "_scan_app_hosts", lambda: [(False, "https://app.x · :3000")]
        )
        body = live_board._render_body()
        assert "🌐 Preview-серверы" in body
        assert "🟢 foo · https://preview-foo.example.com · 1h" in body
        assert "🔗 Постоянные app-хосты" in body
        assert "🔴 https://app.x · :3000" in body

    def test_only_one_section_other_hidden(self, monkeypatch):
        monkeypatch.setattr(live_board, "_scan_previews", lambda: [])
        monkeypatch.setattr(
            live_board, "_scan_app_hosts", lambda: [(True, "https://app.x · :3000")]
        )
        body = live_board._render_body()
        assert "Preview-серверы" not in body
        assert "Постоянные app-хосты" in body

    def test_nothing_up(self, monkeypatch):
        monkeypatch.setattr(live_board, "_scan_previews", lambda: [])
        monkeypatch.setattr(live_board, "_scan_app_hosts", lambda: [])
        assert live_board._render_body() == "Сейчас ничего не поднято."

    def test_compose_has_header(self):
        text = live_board._compose("body here", stamped_at=0.0)
        assert text.startswith("📡 <b>Поднято сейчас</b> · обновлено ")
        assert text.endswith("body here")


# ── live_board_loop ──────────────────────────────────────────────────────


class TestLiveBoardLoop:
    @pytest.mark.asyncio
    async def test_disabled_when_no_target(self, monkeypatch):
        monkeypatch.setattr(live_board.config, "live_dashboard_target", lambda: None)
        bot = AsyncMock()
        await live_board.live_board_loop(bot)  # must return immediately
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_tick_sends_then_persists_id(self, monkeypatch):
        monkeypatch.setattr(
            live_board.config, "live_dashboard_target", lambda: (-100, 5)
        )
        monkeypatch.setattr(live_board, "_render_body", lambda: "X")
        monkeypatch.setattr(live_board, "REFRESH_INTERVAL", 0.01)
        # Fresh, isolated message-id store.
        monkeypatch.setattr(
            live_board.session_manager, "live_dashboard_message_ids", {}
        )
        set_id = []
        monkeypatch.setattr(
            live_board.session_manager,
            "set_dashboard_message_id",
            lambda k, v: set_id.append((k, v)),
        )
        bot = AsyncMock()
        bot.send_message.return_value = type("M", (), {"message_id": 4242})()

        import asyncio as _a

        task = _a.create_task(live_board.live_board_loop(bot))
        try:
            await _a.sleep(0.05)
            bot.send_message.assert_awaited()
            kwargs = bot.send_message.await_args.kwargs
            assert kwargs["chat_id"] == -100
            assert kwargs["message_thread_id"] == 5
            assert kwargs["parse_mode"] == "HTML"
            assert set_id and set_id[0] == ("__live_board__", 4242)
        finally:
            task.cancel()
            try:
                await task
            except _a.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_subsequent_tick_edits(self, monkeypatch):
        monkeypatch.setattr(
            live_board.config, "live_dashboard_target", lambda: (-100, 5)
        )
        monkeypatch.setattr(live_board, "_render_body", lambda: "X")
        monkeypatch.setattr(live_board, "REFRESH_INTERVAL", 0.01)
        monkeypatch.setattr(
            live_board.session_manager,
            "live_dashboard_message_ids",
            {"__live_board__": 999},
        )
        bot = AsyncMock()

        import asyncio as _a

        task = _a.create_task(live_board.live_board_loop(bot))
        try:
            await _a.sleep(0.05)
            bot.send_message.assert_not_called()
            bot.edit_message_text.assert_awaited()
            assert bot.edit_message_text.await_args.kwargs["message_id"] == 999
        finally:
            task.cancel()
            try:
                await task
            except _a.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_message_gone_clears_id(self, monkeypatch):
        from telegram.error import BadRequest

        monkeypatch.setattr(
            live_board.config, "live_dashboard_target", lambda: (-100, 5)
        )
        monkeypatch.setattr(live_board, "_render_body", lambda: "X")
        monkeypatch.setattr(live_board, "REFRESH_INTERVAL", 0.01)
        monkeypatch.setattr(
            live_board.session_manager,
            "live_dashboard_message_ids",
            {"__live_board__": 7},
        )
        cleared = []
        monkeypatch.setattr(
            live_board.session_manager,
            "clear_dashboard_message_id",
            lambda k: cleared.append(k),
        )
        bot = AsyncMock()
        bot.edit_message_text.side_effect = BadRequest("Message to edit not found")

        import asyncio as _a

        task = _a.create_task(live_board.live_board_loop(bot))
        try:
            await _a.sleep(0.05)
            assert "__live_board__" in cleared
        finally:
            task.cancel()
            try:
                await task
            except _a.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_not_modified_is_swallowed(self, monkeypatch):
        from telegram.error import BadRequest

        monkeypatch.setattr(
            live_board.config, "live_dashboard_target", lambda: (-100, 5)
        )
        monkeypatch.setattr(live_board, "_render_body", lambda: "X")
        monkeypatch.setattr(live_board, "REFRESH_INTERVAL", 0.01)
        monkeypatch.setattr(
            live_board.session_manager,
            "live_dashboard_message_ids",
            {"__live_board__": 7},
        )
        cleared = []
        monkeypatch.setattr(
            live_board.session_manager,
            "clear_dashboard_message_id",
            lambda k: cleared.append(k),
        )
        bot = AsyncMock()
        bot.edit_message_text.side_effect = BadRequest("Message is not modified")

        import asyncio as _a

        task = _a.create_task(live_board.live_board_loop(bot))
        try:
            await _a.sleep(0.05)
            # Not cleared — the message is fine, just identical.
            assert cleared == []
        finally:
            task.cancel()
            try:
                await task
            except _a.CancelledError:
                pass
