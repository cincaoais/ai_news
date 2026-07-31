"""Tests for scripts/lib/feeds.py: fetch, freshness, lookback, snippets, UA header."""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from conftest import FakeResponse
from lib import feeds

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


def _mock_get(monkeypatch, response=None, exc=None):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(feeds.requests, "get", fake_get)
    return calls


def test_fresh_feed_parses_all_entries_and_sends_ua(monkeypatch, fixtures_dir):
    text = (fixtures_dir / "fresh_feed.xml").read_text(encoding="utf-8")
    calls = _mock_get(monkeypatch, response=FakeResponse(text=text))

    items = feeds.fetch_feed_items("Fixture Feed", "https://example.com/rss", now=NOW)

    # feed-level freshness check passes (newest entry is 2h old); lookback not yet applied
    assert len(items) == 3
    titles = {item.title for item in items}
    assert "Fresh Item A" in titles
    assert "Old Item C (outside lookback)" in titles

    # browser UA + timeout asserted on the outgoing request
    assert len(calls) == 1
    assert calls[0]["headers"]["User-Agent"] == feeds.USER_AGENT
    assert "Mozilla" in calls[0]["headers"]["User-Agent"]
    assert calls[0]["timeout"] == feeds.REQUEST_TIMEOUT_SECONDS


def test_lookback_filter_excludes_old_item(monkeypatch, fixtures_dir):
    text = (fixtures_dir / "fresh_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    items = feeds.fetch_feed_items("Fixture Feed", "https://example.com/rss", now=NOW)
    filtered = feeds.filter_by_lookback(items, lookback_hours=24, now=NOW)

    titles = {item.title for item in filtered}
    assert titles == {"Fresh Item A", "Fresh Item B"}


def test_lookback_widened_includes_previously_excluded_item(monkeypatch, fixtures_dir):
    text = (fixtures_dir / "fresh_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    items = feeds.fetch_feed_items("Fixture Feed", "https://example.com/rss", now=NOW)
    widened = feeds.filter_by_lookback(items, lookback_hours=96, now=NOW)

    titles = {item.title for item in widened}
    assert "Old Item C (outside lookback)" in titles


def test_stale_feed_is_skipped(monkeypatch, fixtures_dir, caplog):
    text = (fixtures_dir / "stale_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    with caplog.at_level("WARNING"):
        items = feeds.fetch_feed_items("Stale Fixture", "https://stale.example.com/rss", now=NOW)

    assert items == []
    assert any("STALE" in record.message for record in caplog.records)


def test_malformed_feed_is_skipped_not_crashed(monkeypatch, fixtures_dir, caplog):
    text = (fixtures_dir / "malformed_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    with caplog.at_level("WARNING"):
        items = feeds.fetch_feed_items("Malformed Fixture", "https://bad.example.com/rss", now=NOW)

    assert items == []  # no crash, just skipped + logged


def test_dead_feed_network_error_is_skipped_not_crashed(monkeypatch, caplog):
    _mock_get(monkeypatch, exc=requests.ConnectionError("boom"))

    with caplog.at_level("WARNING"):
        items = feeds.fetch_feed_items("Dead Fixture", "https://dead.example.com/rss", now=NOW)

    assert items == []
    assert any("dead feed" in record.message for record in caplog.records)


def test_dead_feed_http_error_is_skipped_not_crashed(monkeypatch):
    _mock_get(monkeypatch, response=FakeResponse(text="", status_code=404))
    items = feeds.fetch_feed_items("404 Fixture", "https://missing.example.com/rss", now=NOW)
    assert items == []


def test_snippet_strips_html_and_truncates():
    raw = "<p>" + ("word " * 100) + "</p>"
    snippet = feeds.make_snippet(raw, max_len=50)
    assert "<" not in snippet and ">" not in snippet
    assert len(snippet) <= 50
    assert snippet.endswith("...")


def test_snippet_short_text_unstripped_of_content():
    raw = "<b>Short</b> text with &amp; an entity."
    snippet = feeds.make_snippet(raw)
    assert snippet == "Short text with & an entity."
    assert len(snippet) <= 300


def test_google_news_rss_url_encodes_query():
    url = feeds.google_news_rss_url("S&P 500 when:1d")
    assert url.startswith("https://news.google.com/rss/search?")
    assert "hl=en-US" in url
    assert "gl=US" in url
    assert "ceid=US%3Aen" in url


def test_google_news_rss_url_honors_explicit_locale_override():
    url = feeds.google_news_rss_url("Malaysia politics when:1d", hl="en-MY", gl="MY", ceid="MY:en")
    assert "hl=en-MY" in url
    assert "gl=MY" in url
    assert "ceid=MY%3Aen" in url


# ---------------------------------------------------------------------------
# warnings_out (Slice 6 feed-health footer)
# ---------------------------------------------------------------------------

def test_fetch_feed_items_records_dead_warning_on_network_error(monkeypatch):
    _mock_get(monkeypatch, exc=requests.ConnectionError("boom"))

    warnings: list[str] = []
    items = feeds.fetch_feed_items(
        "My Feed", "https://dead.example.com/rss", now=NOW, warnings_out=warnings
    )

    assert items == []
    assert warnings == ["My Feed: dead"]


def test_fetch_feed_items_records_dead_warning_on_no_usable_entries(monkeypatch, fixtures_dir):
    text = (fixtures_dir / "malformed_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    warnings: list[str] = []
    items = feeds.fetch_feed_items(
        "Malformed Fixture", "https://bad.example.com/rss", now=NOW, warnings_out=warnings
    )

    assert items == []
    assert warnings == ["Malformed Fixture: dead"]


def test_fetch_feed_items_records_stale_warning(monkeypatch, fixtures_dir):
    text = (fixtures_dir / "stale_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    warnings: list[str] = []
    items = feeds.fetch_feed_items(
        "Stale Fixture", "https://stale.example.com/rss", now=NOW, warnings_out=warnings
    )

    assert items == []
    assert warnings == ["Stale Fixture: stale"]


def test_fetch_feed_items_no_warning_when_healthy(monkeypatch, fixtures_dir):
    text = (fixtures_dir / "fresh_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    warnings: list[str] = []
    items = feeds.fetch_feed_items(
        "Fixture Feed", "https://example.com/rss", now=NOW, warnings_out=warnings
    )

    assert len(items) == 3
    assert warnings == []


def test_fetch_feed_items_warnings_out_defaults_to_none_safely(monkeypatch, fixtures_dir):
    text = (fixtures_dir / "stale_feed.xml").read_text(encoding="utf-8")
    _mock_get(monkeypatch, response=FakeResponse(text=text))

    # No warnings_out passed at all -- must not crash (every existing caller
    # of fetch_feed_items before Slice 6 omits this argument).
    items = feeds.fetch_feed_items("Stale Fixture", "https://stale.example.com/rss", now=NOW)
    assert items == []
