"""Unit tests for the i18n catalog and language switch."""

import pytest

from ccbot import i18n


@pytest.fixture(autouse=True)
def _restore_language():
    """Each test runs against a known language and restores it after."""
    prev = i18n.current_language()
    i18n.set_language("ru")
    yield
    i18n.set_language(prev)


class TestSetLanguage:
    def test_set_and_read(self):
        i18n.set_language("en")
        assert i18n.current_language() == "en"

    def test_unknown_falls_back_to_default(self):
        i18n.set_language("de")
        assert i18n.current_language() == i18n.DEFAULT_LANGUAGE


class TestTr:
    def test_active_language(self):
        i18n.set_language("en")
        assert i18n.tr("menu.server") == "🖥️ Server"
        i18n.set_language("ru")
        assert i18n.tr("menu.server") == "🖥️ Сервер"

    def test_unknown_key_returns_key(self):
        assert i18n.tr("no.such.key") == "no.such.key"

    def test_format_substitution(self):
        i18n.set_language("en")
        assert (
            i18n.tr("ctx.alert", k=312, pct=31)
            == "📈 Context: 312k tokens (~31% of 1M)"
        )

    def test_bad_format_returns_unformatted(self):
        # Missing placeholder must not raise — UI string is never worth a crash.
        out = i18n.tr("ctx.alert")  # no k/pct provided
        assert "Контекст" in out  # ru default, unformatted

    def test_falls_back_to_ru_when_language_missing_entry(self, monkeypatch):
        # A key present only in ru still renders under en (ru fallback).
        monkeypatch.setitem(i18n.STRINGS, "test.only_ru", {"ru": "только ру"})
        i18n.set_language("en")
        assert i18n.tr("test.only_ru") == "только ру"


class TestAllVariants:
    def test_returns_every_language(self):
        variants = i18n.all_variants("menu.agent")
        assert "👾 Агент" in variants
        assert "👾 Agent" in variants

    def test_unknown_key_empty(self):
        assert i18n.all_variants("no.such.key") == []
