"""Tests for status_command — /status output composition."""

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    return context


def _run_stub(systemd_stdout: str, systemd_rc: int = 0):
    """Return a subprocess.run stub that serves realistic output per tool."""

    def _run(args, *a, **kw):
        tool = args[0]
        result = MagicMock()
        result.returncode = 0
        if tool == "docker":
            result.stdout = ""
        elif tool == "systemctl":
            result.stdout = systemd_stdout
            result.returncode = systemd_rc
        elif tool == "df":
            result.stdout = (
                "Filesystem  Size  Used  Avail Use%  Mounted on\n"
                "/dev/sda1   40G   10G   30G  25%   /\n"
            )
        elif tool == "free":
            result.stdout = (
                "              total        used        free\nMem:     8G   2G   6G\n"
            )
        else:
            result.stdout = ""
        return result

    return _run


class TestStatusCommandUserServices:
    @pytest.mark.asyncio
    async def test_user_services_block_lists_whitelisted_unit(self):
        """systemctl reports a whitelisted unit → "⚙️ Фоновые" block lists it,
        non-whitelisted units and the .service suffix dropped."""
        update = _make_update()
        context = _make_context()
        captured: dict[str, str] = {}

        async def _capture(msg, text, **kwargs):
            captured["text"] = text

        # demo-bot is in STATUS_USER_SERVICES_WHITELIST; dbus is not.
        systemd_stdout = (
            "demo-bot.service loaded active running Demo Telegram Bot\n"
            "dbus.service         loaded active running D-Bus User Message Bus\n"
        )

        with (
            patch(
                "ccbot.handlers.commands.STATUS_USER_SERVICES_WHITELIST",
                {"demo-bot"},
            ),
            patch("ccbot.handlers.commands.is_user_allowed", return_value=True),
            patch("ccbot.handlers.commands.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.commands.subprocess") as mock_sp,
            patch(
                "ccbot.handlers.commands.safe_reply",
                new=AsyncMock(side_effect=_capture),
            ),
            patch("ccbot.handlers.commands.os.path.ismount", return_value=True),
            patch("ccbot.handlers.commands.os.listdir", return_value=["x"]),
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[])
            mock_sp.run.side_effect = _run_stub(systemd_stdout)

            from ccbot.handlers.commands import status_command

            await status_command(update, context)

        text = captured["text"]
        assert "⚙️ Фоновые" in text
        assert "demo-bot" in text
        assert "dbus" not in text
        assert ".service" not in text
        # The "Фоновые" block renders before the resource block. We don't
        # anchor on the Docker section because the stub returns no containers
        # and that section is conditionally omitted.
        assert text.index("⚙️ Фоновые") < text.index("💾 Ресурсы")

    @pytest.mark.asyncio
    async def test_system_only_output_drops_header(self):
        """systemctl returns only non-whitelisted services → no block at all."""
        update = _make_update()
        context = _make_context()
        captured: dict[str, str] = {}

        async def _capture(msg, text, **kwargs):
            captured["text"] = text

        systemd_stdout = "dbus.service loaded active running D-Bus User Message Bus\n"

        with (
            patch("ccbot.handlers.commands.is_user_allowed", return_value=True),
            patch("ccbot.handlers.commands.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.commands.subprocess") as mock_sp,
            patch(
                "ccbot.handlers.commands.safe_reply",
                new=AsyncMock(side_effect=_capture),
            ),
            patch("ccbot.handlers.commands.os.path.ismount", return_value=True),
            patch("ccbot.handlers.commands.os.listdir", return_value=["x"]),
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[])
            mock_sp.run.side_effect = _run_stub(systemd_stdout)

            from ccbot.handlers.commands import status_command

            await status_command(update, context)

        assert "*Фоновые программы*" not in captured["text"]
        assert "dbus" not in captured["text"]

    @pytest.mark.asyncio
    async def test_no_header_when_empty(self):
        """Empty systemctl output → no User services header at all."""
        update = _make_update()
        context = _make_context()
        captured: dict[str, str] = {}

        async def _capture(msg, text, **kwargs):
            captured["text"] = text

        with (
            patch("ccbot.handlers.commands.is_user_allowed", return_value=True),
            patch("ccbot.handlers.commands.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.commands.subprocess") as mock_sp,
            patch(
                "ccbot.handlers.commands.safe_reply",
                new=AsyncMock(side_effect=_capture),
            ),
            patch("ccbot.handlers.commands.os.path.ismount", return_value=True),
            patch("ccbot.handlers.commands.os.listdir", return_value=["x"]),
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[])
            mock_sp.run.side_effect = _run_stub("")

            from ccbot.handlers.commands import status_command

            await status_command(update, context)

        assert "*Фоновые программы*" not in captured["text"]

    @pytest.mark.asyncio
    async def test_no_header_when_systemctl_fails(self):
        """systemctl raising (e.g. not installed) → no header, no crash."""
        update = _make_update()
        context = _make_context()
        captured: dict[str, str] = {}

        async def _capture(msg, text, **kwargs):
            captured["text"] = text

        def _run(args, *a, **kw):
            if args[0] == "systemctl":
                raise subprocess.TimeoutExpired(cmd=args, timeout=5)
            return _run_stub("")(args, *a, **kw)

        with (
            patch("ccbot.handlers.commands.is_user_allowed", return_value=True),
            patch("ccbot.handlers.commands.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.commands.subprocess") as mock_sp,
            patch(
                "ccbot.handlers.commands.safe_reply",
                new=AsyncMock(side_effect=_capture),
            ),
            patch("ccbot.handlers.commands.os.path.ismount", return_value=True),
            patch("ccbot.handlers.commands.os.listdir", return_value=["x"]),
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[])
            mock_sp.TimeoutExpired = subprocess.TimeoutExpired
            mock_sp.run.side_effect = _run

            from ccbot.handlers.commands import status_command

            await status_command(update, context)

        assert "*Фоновые программы*" not in captured["text"]
