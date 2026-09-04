"""Tests for status_command — /status output composition."""

import io
import re
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers import commands
from ccbot.handlers.commands import _format_cron_groups


class TestCronGroups:
    """The cron block is part of /status — one of the two persistent menu
    buttons — so its labels are the most visible chrome the bot renders."""

    ENTRIES = [
        ("*/5 * * * *", "/x/health.sh", "healthcheck"),
        ("30 3 * * 0", "/x/backup.sh", "weekly backup"),
        ("15 */4 * * *", "/x/sync.sh", None),
        ("0 * * * *", "/x/hourly.sh", "hourly"),
        ("@reboot", "/x/boot.sh", "boot"),
    ]

    def test_output_has_no_cyrillic(self):
        out = "\n".join(_format_cron_groups(self.ENTRIES))
        assert not re.search(r"[а-яА-Я]", out), out

    def test_weekday_and_interval_labels(self):
        out = "\n".join(_format_cron_groups(self.ENTRIES))
        assert "(Sun)" in out and "5m" in out and "every 4h" in out


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
        """systemctl reports a whitelisted unit → the background block lists
        it, non-whitelisted units and the .service suffix dropped."""
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
        assert "⚙️ Background" in text
        assert "demo-bot" in text
        assert "dbus" not in text
        assert ".service" not in text
        # The background block renders before the resource block. We don't
        # anchor on the Docker section because the stub returns no containers
        # and that section is conditionally omitted.
        assert text.index("⚙️ Background") < text.index("💾 Resources")

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


def _fake_open(mapping: dict[str, str]):
    """Stub for the module's `open`: serve mapped paths, ENOENT otherwise."""

    def _open(path, *args, **kwargs):
        if str(path) in mapping:
            return io.StringIO(mapping[str(path)])
        raise FileNotFoundError(path)

    return _open


class TestHostMetricsPortability:
    """/status read host metrics from Linux-only sources. On macOS the RAM row
    and uptime silently vanished, cron showed a permanent false 🔴, and the
    disk bar came from `df /` — the SEALED system volume, a flat ~1% however
    full the Mac was. CI is Linux-only, so none of it was ever visible."""

    MEMINFO = (
        "MemTotal:       65536000 kB\n"
        "MemFree:            1000 kB\n"
        "MemAvailable:   16384000 kB\n"
    )
    VM_STAT = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                        40515.\n"
        "Pages active:                    1000000.\n"
        "Pages inactive:                  1345315.\n"
        "Pages wired down:                 500000.\n"
        "Pages occupied by compressor:     100000.\n"
    )

    def test_linux_memory_counts_available_not_free(self):
        # "used" is total - AVAILABLE: page cache is reclaimable, and counting
        # it as used reports ~95% RAM on every healthy box.
        with patch(
            "ccbot.handlers.commands.open",
            _fake_open({"/proc/meminfo": self.MEMINFO}),
        ):
            used, total = commands._read_memory_bytes()
        assert total == 65536000 * 1024
        assert used == (65536000 - 16384000) * 1024

    def test_macos_memory_falls_back_to_sysctl_and_vm_stat(self):
        # No /proc/meminfo and no `free` → the RAM row used to just disappear.
        total_bytes = 64 * 1024**3

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[0] == "sysctl":
                result.stdout = f"{total_bytes}\n"
            elif cmd[0] == "vm_stat":
                result.stdout = self.VM_STAT
            else:
                raise FileNotFoundError(cmd[0])
            return result

        with (
            patch("ccbot.handlers.commands.open", _fake_open({})),
            patch("ccbot.handlers.commands.subprocess.run", side_effect=fake_run),
        ):
            used, total = commands._read_memory_bytes()
        assert total == total_bytes
        # active + wired + compressed, at the 16 KiB page size vm_stat reports
        assert used == (1000000 + 500000 + 100000) * 16384

    def test_memory_is_none_when_no_source_works(self):
        with (
            patch("ccbot.handlers.commands.open", _fake_open({})),
            patch(
                "ccbot.handlers.commands.subprocess.run", side_effect=FileNotFoundError
            ),
        ):
            assert commands._read_memory_bytes() is None

    def test_macos_uptime_from_boottime(self):
        boot = 1784096081
        result = MagicMock()
        result.stdout = f"{{ sec = {boot}, usec = 702035 }} Wed Jul 15 14:14:41 2026\n"
        with (
            patch("ccbot.handlers.commands.open", _fake_open({})),
            patch("ccbot.handlers.commands.subprocess.run", return_value=result),
            patch("ccbot.handlers.commands.time.time", return_value=boot + 86400),
        ):
            assert commands._read_uptime_seconds() == pytest.approx(86400)

    def test_linux_uptime_still_preferred(self):
        with patch(
            "ccbot.handlers.commands.open",
            _fake_open({"/proc/uptime": "1234.5 99.9\n"}),
        ):
            assert commands._read_uptime_seconds() == pytest.approx(1234.5)

    def test_cron_unknown_is_not_reported_as_stopped(self, monkeypatch):
        # macOS runs cron as an on-demand launchd job, so systemctl is absent.
        # Unknown must not render as stopped — a false alarm on every /status
        # is worse than a missed one.
        monkeypatch.setattr(commands.shutil, "which", lambda name: None)
        assert commands._cron_daemon_active() is None

    def test_cron_stopped_is_still_detected(self, monkeypatch):
        monkeypatch.setattr(commands.shutil, "which", lambda name: "/usr/bin/systemctl")
        result = MagicMock()
        result.stdout = "inactive\n"
        with patch("ccbot.handlers.commands.subprocess.run", return_value=result):
            assert commands._cron_daemon_active() is False

    def test_human_bytes_shape(self):
        assert commands._human_bytes(0) == "0B"
        assert commands._human_bytes(530 * 1024**3) == "530.0G"
        assert commands._human_bytes(4 * 1024**4) == "4.0T"
