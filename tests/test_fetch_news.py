"""Tests for scripts/fetch_news.py orchestration: dedup, thin-news policy,
all-feeds-down exit, end-to-end candidates.json shape."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests
import yaml

import fetch_news
from lib.feeds import FeedItem

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


def _item(title, url, hours_ago, source="Test Source", snippet="a snippet"):
    return FeedItem(
        title=title,
        source=source,
        url=url,
        published=NOW - timedelta(hours=hours_ago),
        snippet=snippet,
    )


# ---------------------------------------------------------------------------
# canonicalize_and_dedupe
# ---------------------------------------------------------------------------

def test_dedupe_drops_seen_urls():
    items = [_item("A", "https://example.com/a", 1), _item("B", "https://example.com/b", 1)]
    seen = {"https://example.com/a"}
    result = fetch_news.canonicalize_and_dedupe(items, seen)
    assert [i.title for i in result] == ["B"]


def test_dedupe_within_run_keeps_first_occurrence():
    items = [
        _item("A original", "https://example.com/a?utm_source=x", 1),
        _item("A duplicate via different tracking params", "https://example.com/a?utm_source=y", 2),
    ]
    result = fetch_news.canonicalize_and_dedupe(items, seen=set())
    assert len(result) == 1
    assert result[0].title == "A original"
    assert result[0].url == "https://example.com/a"  # canonical, utm stripped


# ---------------------------------------------------------------------------
# thin-news policy
# ---------------------------------------------------------------------------

def test_thin_news_policy_widens_lookback_when_under_min_items():
    # 2 items within 24h, 3 more items between 24h-48h old
    items = [
        _item("Fresh 1", "https://example.com/1", 2),
        _item("Fresh 2", "https://example.com/2", 5),
        _item("Older 1", "https://example.com/3", 30),
        _item("Older 2", "https://example.com/4", 35),
        _item("Older 3", "https://example.com/5", 40),
    ]
    candidates, thin_news = fetch_news.apply_thin_news_policy(
        items, lookback_hours=24, min_items=5, now=NOW
    )
    assert len(candidates) == 5  # widened to 48h, picks up all 5
    assert thin_news is False


def test_thin_news_policy_flags_thin_when_still_under_threshold_after_widening():
    # Only 2 items total, both outside even the widened window is irrelevant --
    # still only 2 candidates after widening -> thin_news True
    items = [
        _item("Only 1", "https://example.com/1", 2),
        _item("Only 2", "https://example.com/2", 5),
    ]
    candidates, thin_news = fetch_news.apply_thin_news_policy(
        items, lookback_hours=24, min_items=5, now=NOW
    )
    assert len(candidates) == 2
    assert thin_news is True


def test_thin_news_policy_no_widen_when_min_items_met():
    items = [_item(f"Item {i}", f"https://example.com/{i}", 1) for i in range(6)]
    candidates, thin_news = fetch_news.apply_thin_news_policy(
        items, lookback_hours=24, min_items=5, now=NOW
    )
    assert len(candidates) == 6
    assert thin_news is False


# ---------------------------------------------------------------------------
# build_candidates_payload
# ---------------------------------------------------------------------------

def _fake_profile(market_enabled=True):
    return {
        "name": "finance_metals",
        "feeds": [],
        "gnews_fallback": [],
        "market_snapshot": {"enabled": market_enabled, "symbols": ["^GSPC", "^IXIC", "GC=F", "USDMYR=X"]},
    }


def test_build_candidates_payload_shape_and_ordering(monkeypatch):
    items = [
        _item("Oldest", "https://example.com/old", 5, source="Src A"),
        _item("Newest", "https://example.com/new", 1, source="Src B"),
        _item("Middle", "https://example.com/mid", 3, source="Src C"),
    ]
    monkeypatch.setattr(fetch_news, "gather_raw_items", lambda profile, now=None: items)

    config = {"lookback_hours": 24, "min_items": 2, "max_items": 8}
    payload = fetch_news.build_candidates_payload(
        "finance_metals", _fake_profile(market_enabled=False), config, seen=set(), now=NOW, fetch_market=False
    )

    assert payload["profile"] == "finance_metals"
    assert payload["date"] == "2026-07-31"
    assert payload["thin_news"] is False
    assert payload["market"] is None

    titles_in_order = [item["title"] for item in payload["items"]]
    assert titles_in_order == ["Newest", "Middle", "Oldest"]  # newest first

    indices = [item["index"] for item in payload["items"]]
    assert indices == [0, 1, 2]

    first = payload["items"][0]
    assert set(first.keys()) == {"index", "title", "source", "url", "published", "snippet"}
    assert first["source"] == "Src B"


def test_build_candidates_payload_date_uses_myt_not_host_utc(monkeypatch):
    # 23:47 UTC on 2026-07-30 is 07:47 MYT on 2026-07-31 (UTC+8) -- exactly
    # the GH Actions fallback-cron scenario lib/dates.py's MYT helper
    # exists for. A naive UTC date here would wrongly read "2026-07-30" and
    # silently break the same-day duplicate-send guard.
    near_midnight_utc = datetime(2026, 7, 30, 23, 47, 0, tzinfo=timezone.utc)
    items = [_item("A", "https://example.com/a", 1)]
    monkeypatch.setattr(fetch_news, "gather_raw_items", lambda profile, now=None: items)

    config = {"lookback_hours": 24, "min_items": 1, "max_items": 8}
    payload = fetch_news.build_candidates_payload(
        "finance_metals", _fake_profile(market_enabled=False), config, seen=set(),
        now=near_midnight_utc, fetch_market=False,
    )
    assert payload["date"] == "2026-07-31"


def test_build_candidates_payload_caps_at_40(monkeypatch):
    items = [_item(f"Item {i}", f"https://example.com/{i}", i * 0.1) for i in range(60)]
    monkeypatch.setattr(fetch_news, "gather_raw_items", lambda profile, now=None: items)

    config = {"lookback_hours": 24, "min_items": 2, "max_items": 8}
    payload = fetch_news.build_candidates_payload(
        "finance_metals", _fake_profile(market_enabled=False), config, seen=set(), now=NOW, fetch_market=False
    )
    assert len(payload["items"]) == 40


def test_build_candidates_payload_includes_market_when_enabled(monkeypatch):
    items = [_item("A", "https://example.com/a", 1)]
    monkeypatch.setattr(fetch_news, "gather_raw_items", lambda profile, now=None: items)
    fake_snapshot = {"^GSPC": {"symbol": "^GSPC", "price": 5000.0}}
    import lib.market as market_mod
    monkeypatch.setattr(market_mod, "fetch_market_snapshot", lambda symbols, timeout=10: fake_snapshot)

    config = {"lookback_hours": 24, "min_items": 1, "max_items": 8}
    payload = fetch_news.build_candidates_payload(
        "finance_metals", _fake_profile(market_enabled=True), config, seen=set(), now=NOW, fetch_market=True
    )
    assert payload["market"] == fake_snapshot


def test_build_candidates_payload_raises_on_zero_candidates(monkeypatch):
    monkeypatch.setattr(fetch_news, "gather_raw_items", lambda profile, now=None: [])

    config = {"lookback_hours": 24, "min_items": 5, "max_items": 8}
    with pytest.raises(fetch_news.NoCandidatesError):
        fetch_news.build_candidates_payload(
            "finance_metals", _fake_profile(market_enabled=False), config, seen=set(), now=NOW, fetch_market=False
        )


# ---------------------------------------------------------------------------
# End-to-end main(): all-feeds-down -> nonzero exit; happy path -> file written
# ---------------------------------------------------------------------------

def _write_tmp_config(tmp_path, profile_yaml: dict, config_yaml: dict, profile_name="testprofile"):
    config_dir = tmp_path / "config"
    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir(parents=True)

    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config_yaml), encoding="utf-8")

    profile_path = profiles_dir / f"{profile_name}.yaml"
    profile_path.write_text(yaml.safe_dump(profile_yaml), encoding="utf-8")
    return config_path


def test_main_all_feeds_down_exits_nonzero(tmp_path, monkeypatch, capsys):
    profile_yaml = {
        "name": "testprofile",
        "feeds": [{"label": "Dead Feed", "url": "https://dead.example.com/rss"}],
        "gnews_fallback": [],
        "market_snapshot": {"enabled": False, "symbols": []},
    }
    config_yaml = {"active_profile": "testprofile", "max_items": 8, "min_items": 5, "lookback_hours": 24}
    config_path = _write_tmp_config(tmp_path, profile_yaml, config_yaml)

    # simulate every feed being down (network error) without hitting the network
    import lib.feeds as feeds_mod

    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("simulated down")

    monkeypatch.setattr(feeds_mod.requests, "get", fake_get)
    monkeypatch.setattr(fetch_news, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(fetch_news, "WORK_DIR", tmp_path / "work")

    exit_code = fetch_news.main(["--config", str(config_path)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "fetch_news:" in captured.err
    assert not (tmp_path / "work" / "candidates.json").exists()


def test_main_happy_path_writes_candidates_json(tmp_path, monkeypatch):
    profile_yaml = {
        "name": "testprofile",
        "feeds": [{"label": "Good Feed", "url": "https://good.example.com/rss"}],
        "gnews_fallback": [],
        "market_snapshot": {"enabled": False, "symbols": []},
    }
    config_yaml = {"active_profile": "testprofile", "max_items": 8, "min_items": 1, "lookback_hours": 24}
    config_path = _write_tmp_config(tmp_path, profile_yaml, config_yaml)

    # Build feed content with a pubDate relative to REAL wall-clock time (not
    # a fixed authored date) so this test stays valid regardless of when the
    # suite is actually run -- main()/run() use real datetime.now() internally.
    from email.utils import format_datetime

    pub_date_str = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))
    rss_text = f"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0">
<channel>
  <title>Dynamic Fixture</title>
  <item>
    <title>Dynamic Fresh Item</title>
    <link>https://good.example.com/articles/dynamic-item</link>
    <description>A dynamically-dated fresh item for the happy-path test.</description>
    <pubDate>{pub_date_str}</pubDate>
  </item>
</channel>
</rss>
"""

    import lib.feeds as feeds_mod
    from conftest import FakeResponse

    def fake_get(url, headers=None, timeout=None):
        return FakeResponse(text=rss_text)

    monkeypatch.setattr(feeds_mod.requests, "get", fake_get)
    monkeypatch.setattr(fetch_news, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(fetch_news, "WORK_DIR", tmp_path / "work")

    exit_code = fetch_news.main(["--config", str(config_path)])

    assert exit_code == 0
    candidates_path = tmp_path / "work" / "candidates.json"
    assert candidates_path.exists()
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    assert payload["profile"] == "testprofile"
    assert payload["market"] is None
    assert len(payload["items"]) >= 1
    for i, item in enumerate(payload["items"]):
        assert item["index"] == i
        assert set(item.keys()) == {"index", "title", "source", "url", "published", "snippet"}
