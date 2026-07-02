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


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hook import hook_main

        hook_main()
        return

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
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
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
    application.run_polling(allowed_updates=allowed_updates)


if __name__ == "__main__":
    main()
