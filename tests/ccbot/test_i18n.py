"""Unit tests for the UI-string catalog."""

from ccbot import i18n


class TestTr:
    def test_known_key(self):
        assert i18n.tr("menu.server") == "🖥️ Server"

    def test_unknown_key_returns_key(self):
        assert i18n.tr("no.such.key") == "no.such.key"

    def test_format_substitution(self):
        assert (
            i18n.tr("ctx.alert", k=312, pct=31)
            == "📈 Context: 312k tokens (~31% of 1M)"
        )

    def test_bad_format_returns_unformatted(self):
        # Missing placeholder must not raise — a UI string is never worth a crash.
        assert i18n.tr("ctx.alert") == i18n.STRINGS["ctx.alert"]


class TestRegister:
    def test_plugin_entries_merge(self, monkeypatch):
        monkeypatch.setitem(i18n.STRINGS, "test.plugin", "placeholder")
        i18n.register({"test.plugin": "from plugin"})
        assert i18n.tr("test.plugin") == "from plugin"


class TestCatalogShape:
    def test_every_entry_is_a_plain_string(self):
        # A dict value would be a leftover of the old bilingual catalog and
        # would render as its repr in the UI.
        bad = [k for k, v in i18n.STRINGS.items() if not isinstance(v, str)]
        assert bad == []
