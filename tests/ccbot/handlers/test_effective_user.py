"""Tests for handlers.effective_user — alias-id canonicalization.

The helper is the single seam turning an aliased sender (second account,
@GroupAnonymousBot) into the canonical allowed user BEFORE ``user.id`` is
used as a per-user state key anywhere in the handlers.
"""

import datetime

from telegram import Chat, Message, Update, User

from ccbot import handlers
from ccbot.config import config


def _update(uid: int) -> Update:
    user = User(id=uid, first_name="X", is_bot=False, username="x")
    msg = Message(
        message_id=1,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=-100123, type="supergroup"),
        from_user=user,
    )
    return Update(update_id=1, message=msg)


def test_identity_without_aliases(monkeypatch):
    monkeypatch.setattr(config, "user_aliases", {})
    upd = _update(12345)
    assert handlers.effective_user(upd) is upd.effective_user


def test_alias_rewritten_to_canonical(monkeypatch):
    monkeypatch.setattr(config, "user_aliases", {111: 12345})
    user = handlers.effective_user(_update(111))
    assert user is not None
    assert user.id == 12345
    assert user.username == "x"  # everything but the id is preserved


def test_non_alias_id_untouched(monkeypatch):
    monkeypatch.setattr(config, "user_aliases", {111: 12345})
    user = handlers.effective_user(_update(999))
    assert user is not None
    assert user.id == 999


def test_update_without_user_is_none():
    assert handlers.effective_user(Update(update_id=1)) is None
