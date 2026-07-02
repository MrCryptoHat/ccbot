"""Application entry point — CLI dispatcher and bot bootstrap.

Handles two execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. Default — configures logging (stderr + rotating file in
     ~/.ccbot/logs/), initializes tmux session, and starts the Telegram
     bot polling loop via bot.create_bot().
"""

import logging
import logging.handlers
import os
import sys


_USAGE = """\
Usage:
  ccbot                     start the Telegram bot
  ccbot hook [--install]    Claude Code SessionStart hook (--install: add it
                            to Claude Code's settings.json)
  ccbot --help              show this message
  ccbot --version           show version

Configuration lives in .env (repo root or $CCBOT_DIR, default ~/.ccbot/).
See SETUP.md for the first-run walkthrough."""


def main() -> None:
    """Main entry point."""
    # Manual argv routing (hook.py parses its own sys.argv[2:], so a full
    # argparse with subparsers buys nothing here). Anything unrecognised must
    # NOT fall through to the bot: a configured user running `ccbot --help`
    # would otherwise start a SECOND polling instance and fight the running
    # one over getUpdates (Telegram 409s, replies flapping).
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "hook":
            from .hook import hook_main

            hook_main()
            return
        if arg in ("-h", "--help"):
            print(_USAGE)
            return
        if arg in ("-V", "--version"):
            from . import __version__

            print(f"ccbot {__version__}")
            return
        print(f"Unknown command: {arg}\n\n{_USAGE}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(
            "Create a .env file (in the repo root, or at "
            f"{env_path}) with the following content:\n"
        )
        # Angle-bracket placeholders on purpose: the .env.example literal
        # ("your_bot_token_here") is exactly the value the placeholder check
        # just rejected — echoing it back invites pasting it a second time.
        print("  TELEGRAM_BOT_TOKEN=<token from @BotFather>")
        print("  ALLOWED_USERS=<your numeric Telegram user id>")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        print("See SETUP.md for the full first-run walkthrough.")
        sys.exit(1)

    # Rotating file log next to the config dir: the process runs inside a
    # tmux pane, so without a file the only log storage is pane scrollback
    # — gone after any crash, tmux restart or reboot, making post-mortems
    # impossible.
    from .utils import ccbot_dir

    try:
        log_dir = ccbot_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "ccbot.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logging.getLogger().addHandler(file_handler)
    except OSError as e:
        print(f"Warning: file logging unavailable: {e}", file=sys.stderr)

    # CCBOT_LOG_LEVEL overrides the default (INFO). DEBUG is the firehose —
    # per-tap TIMING lines, message previews — turn it on only on demand.
    level_name = os.environ.get("CCBOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        print(f"Warning: bad CCBOT_LOG_LEVEL={level_name!r}, using INFO")
        level = logging.INFO
    logging.getLogger("ccbot").setLevel(level)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    # First-run trap: without the SessionStart hook, session_map.json is never
    # written, so agent replies silently never reach the chat. Warn loudly at
    # boot; the bind-time in-topic warning (bot.hook_missing) is the backstop.
    from .hook import hook_installed_in_settings

    if not hook_installed_in_settings():
        logger.warning(
            "Claude Code SessionStart hook is NOT installed — agent replies "
            "will not be delivered to Telegram. Run: ccbot hook --install "
            "(or `uv run ccbot hook --install`)."
        )

    # A `.env`-only CCBOT_DIR is exported into the bot's own tmux session
    # (so hooks in agent panes see it), but any Claude session started
    # outside that tmux session would still write session_map.json to the
    # default ~/.ccbot — a split-brain that looks like "replies stopped".
    from . import config as config_module

    if os.environ.get("CCBOT_DIR") and not config_module.ccbot_dir_from_shell:
        logger.warning(
            "CCBOT_DIR=%s is set only in .env, not exported in the shell. "
            "Agent windows created by the bot will see it, but a `claude` "
            "started outside the bot's tmux session will not — export it in "
            "your shell profile to be safe.",
            config.config_dir,
        )

    # Preflight: missing binaries fail in confusing places later (libtmux
    # raises deep inside get_or_create_session; a missing `claude` makes every
    # agent window die silently 30s after creation) — check them up front.
    import shutil

    if shutil.which("tmux") is None:
        print("Error: `tmux` not found on PATH — ccbot drives Claude Code")
        print("inside tmux windows and cannot run without it.")
        print("Install it first (e.g. `apt install tmux` / `brew install tmux`).")
        sys.exit(1)
    claude_bin = (config.claude_command.split() or ["claude"])[0]
    if shutil.which(os.path.expanduser(claude_bin)) is None:
        logger.warning(
            "`%s` (from CLAUDE_COMMAND) not found on PATH — new agent windows "
            "will fail to start until it is installed. "
            "Install Claude Code: https://claude.com/claude-code",
            claude_bin,
        )

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot()
    allowed_updates = ["message", "callback_query"]
    if config.reaction_confirm_enabled:
        # Telegram only delivers message_reaction updates when explicitly
        # listed here (and the bot is a chat admin). See reaction_confirm.py.
        allowed_updates.append("message_reaction")
    # A mistyped/truncated token is the most likely first-run error after a
    # missing .env; PTB raises InvalidToken out of run_polling AFTER its own
    # ERROR-with-traceback log — without this catch the user gets ~120 lines
    # of stack with the actual cause buried, looking like a crash.
    from telegram.error import InvalidToken

    try:
        application.run_polling(allowed_updates=allowed_updates)
    except InvalidToken:
        print()
        print("Error: Telegram rejected TELEGRAM_BOT_TOKEN.")
        print("Check the token in your .env — it usually means a typo or a")
        print("partial copy-paste. Get the exact value from @BotFather")
        print("(/mybots -> your bot -> API Token).")
        sys.exit(1)


if __name__ == "__main__":
    main()
