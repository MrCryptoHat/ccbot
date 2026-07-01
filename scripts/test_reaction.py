#!/usr/bin/env python
"""Manual one-shot test of a BOT-set reaction (for the reaction-ack feature).

The reaction-ack idea ("bot puts 👀 on your message = 'seen, agent is on it'")
only works if a reaction the BOT places on YOUR message does NOT raise a
notification banner on your phone. This lets you verify that live, before any
feature code is written.

Usage:
    uv run python scripts/test_reaction.py <telegram-message-link> [emoji]

Example:
    uv run python scripts/test_reaction.py "https://t.me/c/1234567890/8/1517" 👀

Steps:
    1. Send a message in any ccbot topic from YOUR account.
    2. Long-press it → "Copy Link" → paste as the first argument.
    3. Run this. The bot reacts with the emoji (default 👀).
    4. Watch your phone: did the reaction produce a banner / unread bump?
         no banner → reaction-ack is viable, we build it.
         banner    → it dies like the rejected status spinner; we skip it.

    Pass an empty emoji to CLEAR the reaction again:
        uv run python scripts/test_reaction.py "<link>" ""

Note: allowed reaction emoji are a fixed Telegram set — 👀 👍 👌 🫡 🔥 💯 work,
✅ does NOT (raises REACTION_INVALID). The bot must be an admin of the chat
(ccbot already is). This is a plain API call; it does not touch the running bot.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot, ReactionTypeEmoji


def _load_token() -> str:
    """Read TELEGRAM_BOT_TOKEN the same way config.py does (local .env wins)."""
    for env_path in (Path.cwd() / ".env", Path.home() / ".ccbot" / ".env"):
        if env_path.exists():
            load_dotenv(env_path)
    token = os.getenv("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN not found in .env or ~/.ccbot/.env")
    return token


def _parse_link(link: str) -> tuple[int, int]:
    """Telegram private/forum link → (chat_id, message_id).

    https://t.me/c/<internal>/<thread>/<msg>  (forum topic)
    https://t.me/c/<internal>/<msg>           (no topic)
    """
    tail = link.strip().split("/c/")[-1]
    nums = re.findall(r"\d+", tail)
    if len(nums) < 2:
        sys.exit(f"can't parse a /c/<chat>/.../<msg> link from: {link!r}")
    chat_id = int(f"-100{nums[0]}")
    message_id = int(nums[-1])
    return chat_id, message_id


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    chat_id, message_id = _parse_link(sys.argv[1])
    emoji = sys.argv[2] if len(sys.argv) > 2 else "👀"
    bot = Bot(_load_token())
    async with bot:
        reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else None
        await bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id, reaction=reaction
        )
    print(f"reacted {emoji or '(cleared)'} on chat={chat_id} msg={message_id}")


if __name__ == "__main__":
    asyncio.run(main())
