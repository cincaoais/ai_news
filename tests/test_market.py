"""Tests for scripts/lib/market.py: Yahoo chart API, query2 fallback, graceful None."""

from __future__ import annotations

import requests

from conftest import FakeResponse
from lib import market

GOOD_CHART_JSON = {
    "chart": {
        "result": [
            {
                "meta": {
                    "symbol": "^GSPC",
                    "regularMarketPrice": 5555.5,
                    "previousClose": 5500.0,
                }
            }
        ],
        "error": None,
    }
}


def test_fetch_quote_uses_query1_and_sends_ua(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(json_data=GOOD_CHART_JSON)

    monkeypatch.setattr(market.requests, "get", fake_get)

    quote = market.fetch_quote("^GSPC")

    assert quote is not None
    assert quote["symbol"] == "^GSPC"
    assert quote["price"] == 5555.5
    assert quote["previous_close"] == 5500.0
    assert round(quote["change"], 2) == 55.5
    assert len(calls) == 1
    assert "query1.finance.yahoo.com" in calls[0]["url"]
    assert calls[0]["headers"]["User-Agent"] == market.USER_AGENT
    assert calls[0]["timeout"] == market.REQUEST_TIMEOUT_SECONDS


def test_fetch_quote_falls_back_to_query2_on_query1_failure(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "query1" in url:
            raise requests.ConnectionError("query1 down")
        return FakeResponse(json_data=GOOD_CHART_JSON)

    monkeypatch.setattr(market.requests, "get", fake_get)

    quote = market.fetch_quote("^GSPC")

    assert quote is not None
    assert quote["price"] == 5555.5
    assert len(calls) == 2
    assert "query1" in calls[0]
    assert "query2" in calls[1]


def test_fetch_quote_returns_none_when_both_hosts_fail(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(market.requests, "get", fake_get)

    quote = market.fetch_quote("^GSPC")
    assert quote is None


def test_fetch_quote_returns_none_on_unparseable_json(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return FakeResponse(json_data={"chart": {"result": []}})

    monkeypatch.setattr(market.requests, "get", fake_get)

    quote = market.fetch_quote("^GSPC")
    assert quote is None


def test_fetch_market_snapshot_partial_failure_keeps_successes(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if "IXIC" in url:
            raise requests.ConnectionError("down")
        return FakeResponse(json_data=GOOD_CHART_JSON)

    monkeypatch.setattr(market.requests, "get", fake_get)

    snapshot = market.fetch_market_snapshot(["^GSPC", "^IXIC", "GC=F"])

    assert snapshot is not None
    assert set(snapshot.keys()) == {"^GSPC", "GC=F"}
    assert "^IXIC" not in snapshot


def test_fetch_market_snapshot_returns_none_when_all_fail(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(market.requests, "get", fake_get)

    snapshot = market.fetch_market_snapshot(["^GSPC", "^IXIC"])
    assert snapshot is None
