"""Tests for links — URL extraction and chat formatting."""

from ccbot.links import MAX_LINKS, extract_urls, format_links_block, shorten_url


class TestExtractUrls:
    def test_empty(self):
        assert extract_urls("") == []
        assert extract_urls("no links here") == []

    def test_bare_url(self):
        assert extract_urls("see https://example.com/path now") == [
            "https://example.com/path"
        ]

    def test_strips_trailing_punctuation(self):
        assert extract_urls("go to https://example.com/x.") == ["https://example.com/x"]
        assert extract_urls("(https://example.com)") == ["https://example.com"]

    def test_markdown_link_url_only(self):
        # The closing paren of [label](url) must not be swallowed into the URL.
        assert extract_urls("[docs](https://example.com/a-b_c)") == [
            "https://example.com/a-b_c"
        ]

    def test_dedupe_preserves_order(self):
        text = "https://b.com then https://a.com then https://b.com"
        assert extract_urls(text) == ["https://b.com", "https://a.com"]

    def test_http_and_https(self):
        assert extract_urls("http://x.io and https://y.io") == [
            "http://x.io",
            "https://y.io",
        ]

    def test_ignores_non_http_schemes(self):
        assert extract_urls("ftp://x.io file:///etc/passwd vnc://host") == []

    def test_multiline(self):
        text = "first https://one.com\nsecond https://two.com"
        assert extract_urls(text) == ["https://one.com", "https://two.com"]


class TestShortenUrl:
    def test_strips_scheme(self):
        assert shorten_url("https://example.com/x") == "example.com/x"

    def test_ellipsizes_long(self):
        url = "https://example.com/" + "a" * 200
        out = shorten_url(url, max_len=20)
        assert len(out) == 20
        assert out.endswith("…")


class TestFormatLinksBlock:
    def test_empty(self):
        assert format_links_block([]) == ""

    def test_single(self):
        block = format_links_block(["https://example.com/x"])
        assert block == "🔗 Ссылки:\n• [example.com/x](https://example.com/x)"

    def test_caps_and_counts_overflow(self):
        urls = [f"https://example.com/{i}" for i in range(MAX_LINKS + 5)]
        block = format_links_block(urls)
        lines = block.split("\n")
        # header + MAX_LINKS bullets + overflow line
        assert len(lines) == 1 + MAX_LINKS + 1
        assert lines[-1] == "… и ещё 5"
