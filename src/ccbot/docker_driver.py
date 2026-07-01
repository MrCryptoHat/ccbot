"""Docker-agent driver — mirrors TmuxManager primitives over `docker exec`.

Used when a topic binding is ``docker:<agent>``. Leaves tmux_manager
untouched; callers (session.send_to_window and friends) branch on
binding type and pick the right transport.

Container convention: each docker agent runs a tmux session named
``claude`` (configurable per-driver via ``tmux_session``) holding one
long-lived Claude Code process. ccbot drives it via

    docker exec -e TERM=xterm-256color <container> \
        tmux send-keys / capture-pane -t claude ...

Pacing (200-char chunks, 0.5-1.5s post-text delay, 1s gap after ``!``)
mirrors tmux_manager so Claude Code's TUI treats the paste and the
Enter as separate events — same rule that makes tmux_manager work.

Key class: DockerDriver (singleton instantiated as ``docker_driver``).
"""

from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# tmux session name inside the container. Fixed by convention so ccbot can
# target it without per-agent config. Container entrypoints must create a
# tmux session with this exact name.
CONTAINER_TMUX_SESSION = "claude"

# Valid Claude session id shape — UUID-ish, bounded. We interpolate this into
# the ``claude --resume <id>`` argv handed to ``tmux new-session``, which
# runs its argv through the container's shell; a permissive value would be a
# container-internal command-injection vector (attacker writes crafted
# session_id into the hook's session_map, we feed it to /bin/sh -c).
_RESUME_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

# docker exec adds ~50–100ms per call. The 200-char chunk size matches
# tmux_manager; tmux's send-keys itself is fine with far larger payloads,
# but keeping the chunking identical avoids behavior drift while we're
# still learning how Claude Code reacts inside a container.
_CHUNK_SIZE = 200

# Hard cap per `docker exec` / `docker inspect` — without it a hung Docker
# daemon would pin the caller (e.g. a webhook handler) for the OS
# default subprocess timeout, which is *minutes*. The bridge would then
# retry-storm the webhook. 10 s is generous: every command we issue is
# either `tmux ...` (returns instantly once the in-container tmux server
# accepts it) or `docker inspect` (microseconds). Anything taking longer
# is a daemon problem, not a workload problem — fail fast and let the
# caller decide what to do.
_DEFAULT_TIMEOUT_SEC = 10.0


class DockerDriver:
    """Sends tmux commands into a running docker container."""

    def __init__(self, tmux_session: str = CONTAINER_TMUX_SESSION) -> None:
        self.tmux_session = tmux_session

    @staticmethod
    async def _run(
        argv: list[str], *, timeout: float = _DEFAULT_TIMEOUT_SEC
    ) -> tuple[int, bytes, bytes]:
        """Run a subprocess and capture stdout/stderr. Never raises.

        Returns ``(124, b"", b"timeout after Ns")`` on timeout — same
        convention as GNU ``timeout(1)`` so callers that only check
        ``rc != 0`` keep working.
        """
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, stdout, stderr
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # Reap so we don't leak a zombie. A second wait_for
                # bounds this in the (extremely unlikely) case the
                # kernel itself is stuck delivering SIGKILL.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
            logger.error(
                "docker driver subprocess timed out after %.1fs (argv=%s)",
                timeout,
                argv,
            )
            return 124, b"", f"timeout after {timeout:.0f}s".encode()
        except (OSError, ValueError) as e:
            logger.error("docker driver subprocess failed (argv=%s): %s", argv, e)
            return 1, b"", str(e).encode()

    def _exec_prefix(self, container: str) -> list[str]:
        """Base argv for ``docker exec`` with the TERM env var preset.

        xterm-256color prevents Claude Code's TUI from falling back to a
        monochrome / 16-color degraded mode when it inspects $TERM.
        """
        return [
            "docker",
            "exec",
            "-e",
            "TERM=xterm-256color",
            # Pin cwd to the image's own filesystem, not /workspace (which is
            # a bind-mount from the rclone FUSE mount). Any rclone remount
            # invalidates /workspace's inode and every `docker exec` without
            # an explicit -w fails with "cwd outside of container mount
            # namespace root", silently breaking Telegram → agent routing.
            "-w",
            "/home/ubuntu",
            container,
        ]

    async def is_container_alive(self, container: str) -> bool:
        """Return True iff `docker ps` shows the container running."""
        rc, out, _ = await self._run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                container,
            ]
        )
        if rc != 0:
            return False
        return out.strip() == b"true"

    async def _send_literal(self, container: str, chars: str) -> bool:
        """Send literal text (no key interpretation) to the tmux pane."""
        argv = self._exec_prefix(container) + [
            "tmux",
            "send-keys",
            "-t",
            self.tmux_session,
            "-l",
            # `--` so text starting with "-" isn't eaten as tmux flags.
            "--",
            chars,
        ]
        rc, _, stderr = await self._run(argv)
        if rc != 0:
            logger.error(
                "docker send-keys literal failed (container=%s): %s",
                container,
                stderr.decode(errors="replace").strip(),
            )
            return False
        return True

    async def _send_key(self, container: str, key: str) -> bool:
        """Send a named key (Enter, Escape, Up, …) to the tmux pane."""
        argv = self._exec_prefix(container) + [
            "tmux",
            "send-keys",
            "-t",
            self.tmux_session,
            key,
        ]
        rc, _, stderr = await self._run(argv)
        if rc != 0:
            logger.error(
                "docker send-keys %s failed (container=%s): %s",
                key,
                container,
                stderr.decode(errors="replace").strip(),
            )
            return False
        return True

    async def send_keys(
        self,
        container: str,
        text: str,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        """Send text to the agent's tmux pane inside ``container``.

        Behavior mirrors tmux_manager.send_keys for the common case
        (``literal=True`` + ``enter=True``): chunk text, sleep, send Enter.
        Claude Code's TUI treats a same-batch Enter as a newline rather
        than submit; the delay before Enter is the fix.
        """
        if literal and enter:
            if text.startswith("!"):
                if not await self._send_literal(container, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    if not await self._send_chunked_literal(container, rest):
                        return False
            else:
                if not await self._send_chunked_literal(container, text):
                    return False
            delay = 1.5 if len(text) > _CHUNK_SIZE else 0.5
            await asyncio.sleep(delay)
            return await self._send_key(container, "Enter")

        # Special-key or no-Enter paths.
        if literal:
            return await self._send_literal(container, text)
        return await self._send_key(container, text)

    async def _send_chunked_literal(self, container: str, text: str) -> bool:
        for i in range(0, len(text), _CHUNK_SIZE):
            chunk = text[i : i + _CHUNK_SIZE]
            if not await self._send_literal(container, chunk):
                return False
            if i + _CHUNK_SIZE < len(text):
                await asyncio.sleep(0.1)
        return True

    async def has_session(self, container: str) -> bool:
        """Return True iff ``tmux has-session -t claude`` succeeds inside."""
        argv = self._exec_prefix(container) + [
            "tmux",
            "has-session",
            "-t",
            self.tmux_session,
        ]
        rc, _, _ = await self._run(argv)
        return rc == 0

    async def kill_session(self, container: str) -> bool:
        """Kill the agent's tmux session. Container itself stays up."""
        argv = self._exec_prefix(container) + [
            "tmux",
            "kill-session",
            "-t",
            self.tmux_session,
        ]
        rc, _, stderr = await self._run(argv)
        if rc != 0:
            err = stderr.decode(errors="replace").strip()
            # "can't find session" is not a failure — it's already dead.
            if "can't find session" in err.lower():
                return True
            logger.error(
                "docker kill-session failed (container=%s): %s", container, err
            )
            return False
        return True

    async def start_session(
        self,
        container: str,
        cwd: str = "/workspace",
        claude_cmd: str = "claude --dangerously-skip-permissions",
        resume_session_id: str | None = None,
    ) -> bool:
        """(Re)create the agent's tmux session with Claude Code running inside.

        Matches the invocation container entrypoints use on boot so restart
        behavior stays consistent. ``resume_session_id`` appends
        ``--resume <id>`` so the Claude process picks up the same JSONL.
        """
        cmd = claude_cmd
        if resume_session_id:
            if not _RESUME_SESSION_ID_RE.match(resume_session_id):
                logger.error(
                    "docker start_session: refusing to resume malformed session_id "
                    "(container=%s, id=%r) — starting fresh session instead",
                    container,
                    resume_session_id,
                )
            else:
                cmd = f"{cmd} --resume {resume_session_id}"
        argv = self._exec_prefix(container) + [
            "tmux",
            "new-session",
            "-d",
            "-s",
            self.tmux_session,
            "-c",
            cwd,
            cmd,
        ]
        rc, _, stderr = await self._run(argv)
        if rc != 0:
            logger.error(
                "docker new-session failed (container=%s): %s",
                container,
                stderr.decode(errors="replace").strip(),
            )
            return False
        # Restore the screenshot-friendly pane size — the new session would
        # otherwise inherit the in-container tmux default (80x24) and wrap
        # Claude Code's footer.
        await self.ensure_pane_size(container, cols=100, rows=50)
        return True

    async def ensure_pane_size(self, container: str, cols: int, rows: int) -> None:
        """Pin the in-container tmux pane size for screenshots.

        Same rationale as TmuxManager.ensure_session_pane_size: with no
        client attached, tmux defaults the pane to 80x24 and capture-pane
        returns wrapped text. Sets ``default-size`` globally inside the
        container's tmux server, then resizes the live ``claude`` window.
        Best-effort; failures are logged at DEBUG.
        """
        # default-size for any new tmux session inside the container.
        await self._run(
            self._exec_prefix(container)
            + [
                "tmux",
                "set-option",
                "-g",
                "default-size",
                f"{cols}x{rows}",
            ]
        )
        # Resize the live session if it exists.
        rc, _, stderr = await self._run(
            self._exec_prefix(container)
            + [
                "tmux",
                "resize-window",
                "-t",
                self.tmux_session,
                "-x",
                str(cols),
                "-y",
                str(rows),
            ]
        )
        if rc != 0:
            logger.debug(
                "docker resize-window failed (container=%s): %s",
                container,
                stderr.decode(errors="replace").strip(),
            )

    async def capture_pane(
        self,
        container: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str | None:
        """Return text of the agent's tmux pane, or None on error.

        scrollback_lines >0 includes that many rows of history above the
        visible area (passes `-S -<N>` through to in-container tmux) — used
        when output may have scrolled past the viewport, e.g. /context."""
        argv = self._exec_prefix(container) + [
            "tmux",
            "capture-pane",
            "-p",
            "-t",
            self.tmux_session,
        ]
        if with_ansi:
            argv.insert(-2, "-e")
        if scrollback_lines > 0:
            argv.extend(["-S", f"-{scrollback_lines}"])
        rc, stdout, stderr = await self._run(argv)
        if rc != 0:
            logger.error(
                "docker capture-pane failed (container=%s): %s",
                container,
                stderr.decode(errors="replace").strip(),
            )
            return None
        return stdout.decode("utf-8", errors="replace")


docker_driver = DockerDriver()
