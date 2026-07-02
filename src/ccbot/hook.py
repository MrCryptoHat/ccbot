"""Hook subcommand for Claude Code session tracking.

Called by Claude Code's SessionStart hook to maintain a window↔session
mapping in <CCBOT_DIR>/session_map.json. Also provides `--install` to
auto-configure the hook in Claude Code's settings.json (honours
CLAUDE_CONFIG_DIR, default ~/.claude).

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.ccbot_dir() (shared with config.py).

Key functions: hook_main() (CLI entry), _install_hook().
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# The hook command suffix for legacy (unquoted) entry detection
_HOOK_COMMAND_SUFFIX = "ccbot hook"


def _claude_settings_file() -> Path:
    """Resolve Claude Code's settings.json, honouring CLAUDE_CONFIG_DIR.

    Claude Code relocates its whole config dir (settings.json included) when
    CLAUDE_CONFIG_DIR is set; installing/checking the hook against a
    hardcoded ~/.claude would silently target a file that Claude never reads
    — and suppress every "hook missing" warning while replies fail to arrive.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / "settings.json"


def _count_claude_ancestors(
    proc_root: Path = Path("/proc"), start_pid: int | None = None
) -> int | None:
    """Number of ``claude`` processes in a process's ancestor chain (incl. itself).

    Returns ``None`` on platforms without ``/proc`` (the check is then skipped).

    A SessionStart hook fired by the tmux pane's *own* interactive Claude has
    exactly one such ancestor: ``ccbot hook → [sh -c] → claude → pane-shell →
    tmux``. A hook fired by a **nested** ``claude`` — a ``claude -p`` shelled
    out from a Bash tool call, a subagent that runs ``claude``, ``! claude …``
    inside an interactive session — has two or more. Those must not overwrite
    the window↔session map: the moment the nested invocation exits, the map
    still points at its (now dead) session, so the pane's real Claude session
    goes unmonitored and the bound Telegram topic falls silent. (Relies on
    Claude Code setting its process title to ``claude``; if that ever changes,
    the count comes back 0 and we simply fall back to the old behaviour.)
    """
    if not proc_root.is_dir():
        return None
    count = 0
    pid = os.getpid() if start_pid is None else start_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            stat = (proc_root / str(pid) / "stat").read_bytes()
        except OSError:
            break
        # "<pid> (<comm>) <state> <ppid> …" — comm can contain ')' and spaces,
        # so anchor on the LAST ')'.
        lparen = stat.find(b"(")
        rparen = stat.rfind(b")")
        if lparen < 0 or rparen < lparen:
            break
        if stat[lparen + 1 : rparen] == b"claude":
            count += 1
        fields = stat[rparen + 1 :].split()
        if len(fields) < 2:
            break
        try:
            pid = int(fields[1])  # ppid
        except ValueError:
            break
    return count


def _find_ccbot_path() -> str:
    """Find the full path to the ccbot executable.

    Priority:
    1. shutil.which("ccbot") - if ccbot is in PATH
    2. Same directory as the Python interpreter (for venv installs)
    """
    # Try PATH first
    ccbot_path = shutil.which("ccbot")
    if ccbot_path:
        return ccbot_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where ccbot is installed in a venv
    python_dir = Path(sys.executable).parent
    ccbot_in_venv = python_dir / "ccbot"
    if ccbot_in_venv.exists():
        return str(ccbot_in_venv)

    # Last resort: assume it will be in PATH
    return "ccbot"


def _is_ccbot_hook_command(cmd: str) -> bool:
    """Is this settings.json command string a ccbot SessionStart hook?

    Primary form is shell-quoted `<path-to-ccbot> hook` (what --install
    writes). The legacy suffix match keeps recognising old unquoted entries —
    including ones whose path contains a space (those split into 3+ tokens
    under shlex but still end with "/ccbot hook"), so --install can repair
    them instead of stacking a second entry.
    """
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if len(parts) == 2 and parts[1] == "hook" and Path(parts[0]).name == "ccbot":
        return True
    return cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX)


def _find_hook_entry(settings: dict) -> dict | None:
    """Return the ccbot SessionStart hook dict from settings, or None.

    Matches quoted and legacy-unquoted command forms (see
    _is_ccbot_hook_command). Returned dict is the live object inside
    ``settings`` so callers can repair its ``command`` in place.
    """
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            if _is_ccbot_hook_command(h.get("command", "")):
                return h
    return None


def _is_hook_installed(settings: dict) -> bool:
    """Check if ccbot hook is already installed in the settings."""
    return _find_hook_entry(settings) is not None


def _hook_command_runnable(command: str) -> bool:
    """Can the installed hook command actually execute?

    The command is ``<executable> hook`` (shell-quoted by --install). An
    absolute executable must exist on disk (the recorded path goes stale when
    the repo/venv is moved or renamed — the exact failure this check exists
    to catch); a bare name must resolve via PATH. A legacy *unquoted* path
    containing a space splits into 3+ tokens here and correctly reports
    not-runnable — the shell would split it the same way at execution time.
    A dead hook means session_map is never written and replies stop reaching
    chat, so err on the side of False.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) != 2 or parts[1] != "hook":
        return False
    executable = parts[0]
    if os.path.isabs(executable):
        return Path(executable).exists()
    return shutil.which(executable) is not None


def hook_installed_in_settings() -> bool:
    """True if a *runnable* ccbot SessionStart hook is in Claude's settings.json.

    Used by the bot as a first-run check: without the hook, session_map.json
    is never written and agent replies silently never reach the chat.
    Unreadable/absent settings count as "not installed", and so does an
    entry whose recorded executable no longer exists (repo/venv moved or
    renamed) — that hook is just as dead as a missing one.
    ``ccbot hook --install`` repairs a stale path in place.
    """
    try:
        settings = json.loads(_claude_settings_file().read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(settings, dict):
        return False
    entry = _find_hook_entry(settings)
    if entry is None:
        return False
    return _hook_command_runnable(entry.get("command", ""))


def _install_hook() -> int:
    """Install the ccbot hook into Claude's settings.json.

    Returns 0 on success, 1 on error.
    """
    settings_file = _claude_settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings
    settings: dict = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading %s: %s", settings_file, e)
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            return 1

    # Find the full path to ccbot. shlex.quote so a path containing a space
    # (common on macOS: "My Projects", iCloud dirs) survives the shell that
    # Claude Code runs hook commands through — unquoted it would split at the
    # space, the hook would silently never fire, and replies would never
    # reach the chat. Paths without special characters are left untouched.
    ccbot_path = _find_ccbot_path()
    hook_command = f"{shlex.quote(ccbot_path)} hook"

    # Already installed? Healthy → done; stale executable path (repo/venv
    # moved or renamed since install) → repair the command in place instead
    # of reporting success on a dead hook.
    existing = _find_hook_entry(settings)
    if existing is not None:
        if _hook_command_runnable(existing.get("command", "")):
            logger.info("Hook already installed in %s", settings_file)
            print(f"Hook already installed in {settings_file}")
            return 0
        old_command = existing.get("command", "")
        existing["command"] = hook_command
        logger.info("Repairing stale hook command: %r -> %r", old_command, hook_command)
        print(f"Repairing stale hook path: {old_command} -> {hook_command}")
    else:
        hook_config = {"type": "command", "command": hook_command, "timeout": 5}
        logger.info("Installing hook command: %s", hook_command)

        # Install the hook
        if "hooks" not in settings:
            settings["hooks"] = {}
        if "SessionStart" not in settings["hooks"]:
            settings["hooks"]["SessionStart"] = []

        settings["hooks"]["SessionStart"].append({"hooks": [hook_config]})

    # Write back
    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        logger.error("Error writing %s: %s", settings_file, e)
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Hook installed successfully in %s", settings_file)
    print(f"Hook installed successfully in {settings_file}")
    return 0


def _uninstall_hook() -> int:
    """Remove the ccbot hook from Claude's settings.json.

    Without this, deleting a tried-out ccbot checkout leaves a global
    SessionStart hook pointing at a dead binary — every future Claude Code
    session on the machine (ccbot-related or not) runs a failing command.

    Returns 0 on success (including "nothing to remove"), 1 on error.
    """
    settings_file = _claude_settings_file()
    try:
        settings = json.loads(settings_file.read_text())
    except FileNotFoundError:
        print("Hook is not installed (no settings file) — nothing to remove.")
        return 0
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading {settings_file}: {e}", file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print(f"Error: {settings_file} is not a JSON object", file=sys.stderr)
        return 1

    session_start = settings.get("hooks", {}).get("SessionStart", [])
    removed = 0
    kept_entries = []
    for entry in session_start:
        if not isinstance(entry, dict):
            kept_entries.append(entry)
            continue
        inner = entry.get("hooks", [])
        kept_hooks = [
            h
            for h in inner
            if not (
                isinstance(h, dict) and _is_ccbot_hook_command(h.get("command", ""))
            )
        ]
        removed += len(inner) - len(kept_hooks)
        if kept_hooks or set(entry.keys()) - {"hooks"}:
            kept_entries.append({**entry, "hooks": kept_hooks})

    if not removed:
        print("Hook is not installed — nothing to remove.")
        return 0

    settings["hooks"]["SessionStart"] = kept_entries
    if not kept_entries:
        del settings["hooks"]["SessionStart"]
        if not settings["hooks"]:
            del settings["hooks"]

    try:
        settings_file.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as e:
        print(f"Error writing {settings_file}: {e}", file=sys.stderr)
        return 1
    print(f"Removed ccbot hook from {settings_file}")
    return 0


def _preload_dotenv_for_install() -> None:
    """Best-effort .env preload for the --install CLI path.

    Mirrors config._preload_dotenv (local .env first, then $CCBOT_DIR/.env,
    no override of real env vars) without importing config.py, which this
    module must not do.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a hard dep of the bot
        return
    from .utils import ccbot_dir

    local_env = Path(".env")
    if local_env.is_file():
        load_dotenv(local_env)
    global_env = ccbot_dir() / ".env"
    if global_env.is_file():
        load_dotenv(global_env)


def hook_main() -> None:
    """Process a Claude Code hook event from stdin, or (un)install the hook."""
    # Configure logging for the hook subprocess (main.py logging doesn't apply
    # here). Default WARNING, not DEBUG: once installed the hook fires on
    # EVERY Claude session on the machine, and per-event debug chatter would
    # pollute Claude Code's hook stderr. CCBOT_LOG_LEVEL opts into more.
    level_name = os.environ.get("CCBOT_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.WARNING
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=level,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="ccbot hook",
        description="Claude Code session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install the hook into Claude Code's settings.json",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the hook from Claude Code's settings.json",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    if args.install or args.uninstall:
        # `ccbot hook --(un)install` dispatches before config.py's dotenv
        # preload ever runs, so honour a CLAUDE_CONFIG_DIR (or CCBOT_DIR)
        # that lives only in .env. dotenv+utils only — this module must not
        # import config.py (see module docstring).
        _preload_dotenv_for_install()
    if args.install and args.uninstall:
        print("Pick one: --install or --uninstall", file=sys.stderr)
        sys.exit(2)
    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook())
    if args.uninstall:
        logger.info("Hook uninstall requested")
        sys.exit(_uninstall_hook())

    # Normal hook processing: read JSON from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    # Validate session_id format
    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

    # A nested `claude` (e.g. `claude -p` shelled out from a Bash tool call,
    # a subagent, `! claude …`) inherits TMUX_PANE and would otherwise clobber
    # the pane's session_map entry — leaving the real session unmonitored once
    # the nested one exits. Only the pane's top-level Claude owns the slot.
    claude_ancestors = _count_claude_ancestors()
    if claude_ancestors is not None and claude_ancestors > 1:
        logger.info(
            "Nested claude invocation (%d claude ancestors); not updating "
            "session_map for session %s",
            claude_ancestors,
            session_id,
        )
        return

    # Get tmux session:window key for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.warning("TMUX_PANE not set, cannot determine window")
        return

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    # Expected format: "session_name:@id:window_name"
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return
    tmux_session_name, window_id, window_name = parts
    # Key uses window_id for uniqueness
    session_window_key = f"{tmux_session_name}:{window_id}"

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    # Read-modify-write with file locking to prevent concurrent hook races
    from .utils import ccbot_dir

    map_file = ccbot_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": window_name,
                }

                # Clean up old-format key ("session:window_name") if it exists.
                # Previous versions keyed by window_name instead of window_id.
                old_key = f"{tmux_session_name}:{window_name}"
                if old_key != session_window_key and old_key in session_map:
                    del session_map[old_key]
                    logger.info("Removed old-format session_map key: %s", old_key)

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)
