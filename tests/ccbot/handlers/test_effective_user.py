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


def test_not_authorized_text_generic():
    text = handlers.not_authorized_text(555)
    assert "555" in text
    assert "ALLOWED_USERS" in text


def test_not_authorized_text_none_shows_placeholder():
    assert "?" in handlers.not_authorized_text(None)


def test_not_authorized_text_anonymous_admin():
    text = handlers.not_authorized_text(handlers.ANONYMOUS_ADMIN_ID)
    assert "CCBOT_USER_ALIASES" in text
    assert "GroupAnonymousBot" in text


class TestTopicOwner:
    """One topic, one agent: an allowed user with no binding of their own in a
    topic another allowed user's agent serves acts as that owner — otherwise
    they launch a second window on the same session and every reply arrives
    twice (once per binding)."""

    OWNER, OTHER, STRANGER = 1001, 2002, 3003
    CHAT, THREAD = -100123, 42

    def _topic_update(self, uid: int, chat_id: int = CHAT) -> Update:
        user = User(id=uid, first_name="X", is_bot=False)
        msg = Message(
            message_id=1,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=chat_id, type="supergroup", is_forum=True),
            from_user=user,
            message_thread_id=self.THREAD,
            is_topic_message=True,
        )
        return Update(update_id=1, message=msg)

    def _setup(self, monkeypatch):
        from ccbot.session import session_manager

        monkeypatch.setattr(config, "user_aliases", {})
        monkeypatch.setattr(config, "allowed_users", {self.OWNER, self.OTHER})
        monkeypatch.setattr(
            session_manager, "thread_bindings", {self.OWNER: {self.THREAD: "@4"}}
        )
        monkeypatch.setattr(
            session_manager,
            "group_chat_ids",
            {f"{self.OWNER}:{self.THREAD}": self.CHAT},
        )
        return session_manager

    def test_second_user_acts_as_owner(self, monkeypatch):
        self._setup(monkeypatch)
        user = handlers.effective_user(self._topic_update(self.OTHER))
        assert user is not None and user.id == self.OWNER

    def test_owner_is_untouched(self, monkeypatch):
        self._setup(monkeypatch)
        upd = self._topic_update(self.OWNER)
        assert handlers.effective_user(upd) is upd.effective_user

    def test_own_binding_wins(self, monkeypatch):
        sm = self._setup(monkeypatch)
        sm.thread_bindings[self.OTHER] = {self.THREAD: "@5"}
        user = handlers.effective_user(self._topic_update(self.OTHER))
        assert user is not None and user.id == self.OTHER

    def test_same_thread_id_in_another_chat_is_not_the_topic(self, monkeypatch):
        self._setup(monkeypatch)
        user = handlers.effective_user(self._topic_update(self.OTHER, chat_id=-999))
        assert user is not None and user.id == self.OTHER

    def test_never_a_way_past_the_allowlist(self, monkeypatch):
        self._setup(monkeypatch)
        user = handlers.effective_user(self._topic_update(self.STRANGER))
        assert user is not None and user.id == self.STRANGER
