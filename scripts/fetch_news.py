"""Step 1 entry point: fetch profile feeds + gnews fallback + market snapshot,
drop already-seen URLs, dedupe, apply the thin-news policy, and write
work/candidates.json for the active profile.

Usage: python scripts/fetch_news.py [--profile NAME] [--config PATH]

Exit codes: 0 on success (candidates.json written); 1 if there are 0
candidates after filtering (all feeds down/stale) -- notify_failure.py
(a later slice) is expected to react to this.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from lib import dates, feeds, market, state
from lib.feeds import FeedItem

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
PROFILES_DIR = REPO_ROOT / "config" / "profiles"
STATE_DIR = REPO_ROOT / "state"
WORK_DIR = REPO_ROOT / "work"
CANDIDATES_PATH = WORK_DIR / "candidates.json"

MAX_CANDIDATES = 40
THIN_NEWS_THRESHOLD = 3  # strictly fewer than this -> thin_news = true


class NoCandidatesError(RuntimeError):
    """Raised when 0 candidates remain after filtering (feeds down/stale)."""


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_profile(profile_name: str, profiles_dir: Path = PROFILES_DIR) -> dict:
    profile_path = profiles_dir / f"{profile_name}.yaml"
    with profile_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def gnews_locale_kwargs(profile: dict) -> dict:
    """Per-profile Google News RSS locale override (Slice 1 follow-up,
    delivered in Slice 6). A profile may set an optional `gnews_locale:
    {hl, gl, ceid}` field (only malaysia_politics.yaml does); any subset of
    those three keys is honored. Absent (or no matching keys) -> an empty
    dict, so google_news_rss_url() falls back to its own en-US/US/US:en
    defaults untouched."""
    locale = profile.get("gnews_locale") or {}
    return {k: v for k, v in locale.items() if k in ("hl", "gl", "ceid")}


def gather_raw_items(
    profile: dict, *, now: Optional[datetime] = None
) -> tuple[list[FeedItem], list[str]]:
    """Fetch every feed and gnews_fallback query for a profile.

    Returns (items, feed_warnings): the union of items across all sources,
    freshness-checked per feed but NOT yet lookback-filtered, plus a
    "<label>: dead"/"<label>: stale" string for every feed skipped along the
    way (see lib/feeds.py's `warnings_out`). Dead/stale individual feeds are
    skipped (logged) -- this function itself never raises.
    """
    now = now or datetime.now(timezone.utc)
    items: list[FeedItem] = []
    feed_warnings: list[str] = []

    for feed in profile.get("feeds", []) or []:
        label = feed.get("label") or feed.get("url", "unknown feed")
        url = feed["url"]
        items.extend(feeds.fetch_feed_items(label, url, now=now, warnings_out=feed_warnings))

    locale_kwargs = gnews_locale_kwargs(profile)
    for query in profile.get("gnews_fallback", []) or []:
        url = feeds.google_news_rss_url(query, **locale_kwargs)
        items.extend(
            feeds.fetch_feed_items(
                f"Google News: {query}", url, now=now, warnings_out=feed_warnings
            )
        )

    return items, feed_warnings


def canonicalize_and_dedupe(items: list[FeedItem], seen: set[str]) -> list[FeedItem]:
    """Canonicalize each item's URL, drop already-seen ones, and dedupe
    within this run by canonical URL (first occurrence wins)."""
    result: list[FeedItem] = []
    seen_this_run: set[str] = set()
    for item in items:
        canonical = state.canonicalize_url(item.url)
        if canonical in seen or canonical in seen_this_run:
            continue
        seen_this_run.add(canonical)
        result.append(
            FeedItem(
                title=item.title,
                source=item.source,
                url=canonical,
                published=item.published,
                snippet=item.snippet,
            )
        )
    return result


def apply_thin_news_policy(
    items: list[FeedItem],
    lookback_hours: float,
    min_items: int,
    *,
    now: Optional[datetime] = None,
) -> tuple[list[FeedItem], bool]:
    """Lookback-filter; widen the window once (x2) if under min_items; flag
    thin_news if still under THIN_NEWS_THRESHOLD after that.

    `items` must already be canonicalized/deduped but NOT lookback-filtered,
    so widening is a cheap local re-filter, never a refetch.
    """
    now = now or datetime.now(timezone.utc)
    candidates = feeds.filter_by_lookback(items, lookback_hours, now=now)

    if len(candidates) < min_items:
        widened_hours = lookback_hours * 2
        logger.info(
            "%d candidates (< min_items=%d); widening lookback %sh -> %sh and re-filtering",
            len(candidates), min_items, lookback_hours, widened_hours,
        )
        candidates = feeds.filter_by_lookback(items, widened_hours, now=now)

    thin_news = len(candidates) < THIN_NEWS_THRESHOLD
    if thin_news:
        logger.warning("thin news day: only %d candidates remain after widening", len(candidates))

    return candidates, thin_news


def build_candidates_payload(
    profile_name: str,
    profile: dict,
    config: dict,
    seen: set[str],
    *,
    now: Optional[datetime] = None,
    fetch_market: bool = True,
) -> dict:
    now = now or datetime.now(timezone.utc)

    raw_items, feed_warnings = gather_raw_items(profile, now=now)
    deduped = canonicalize_and_dedupe(raw_items, seen)

    lookback_hours = config["lookback_hours"]
    min_items = config["min_items"]
    candidates, thin_news = apply_thin_news_policy(deduped, lookback_hours, min_items, now=now)

    if not candidates:
        raise NoCandidatesError("0 candidates after filtering -- all feeds may be down or stale")

    candidates.sort(key=lambda item: item.published, reverse=True)
    candidates = candidates[:MAX_CANDIDATES]

    market_block = None
    market_cfg = profile.get("market_snapshot") or {}
    if fetch_market and market_cfg.get("enabled"):
        market_block = market.fetch_market_snapshot(market_cfg.get("symbols", []))

    return {
        "profile": profile_name,
        # MYT (Asia/Kuala_Lumpur), not the host's UTC date -- the GH Actions
        # fallback cron fires 23:47 UTC = 07:47 MYT the *next* day, so a
        # naive UTC date here would silently break the same-day guard
        # (see lib/dates.py and docs/03-EXECUTION-LOG.md's Slice 1 follow-up).
        "date": dates.today_myt(now),
        "thin_news": thin_news,
        "feed_warnings": feed_warnings,
        "market": market_block,
        "items": [
            {
                "index": i,
                "title": item.title,
                "source": item.source,
                "url": item.url,
                "published": item.published.isoformat(),
                "snippet": item.snippet,
            }
            for i, item in enumerate(candidates)
        ],
    }


def write_candidates(payload: dict, path: Optional[Path] = None) -> None:
    if path is None:
        path = WORK_DIR / "candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def run(
    profile_name: Optional[str] = None,
    config_path: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    work_dir: Optional[Path] = None,
) -> dict:
    config_path = config_path or CONFIG_PATH
    state_dir = state_dir or STATE_DIR
    work_dir = work_dir or WORK_DIR
    profiles_dir = config_path.parent / "profiles"

    state.bootstrap_state_dir(state_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(config_path)
    active_profile = profile_name or config["active_profile"]
    profile = load_profile(active_profile, profiles_dir=profiles_dir)

    seen = state.load_seen(state_dir / "seen.jsonl")

    payload = build_candidates_payload(active_profile, profile, config, seen)
    write_candidates(payload, path=work_dir / "candidates.json")
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Fetch news + market candidates for the active profile.")
    parser.add_argument("--profile", help="Override active_profile from config.yaml")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Path to config.yaml")
    args = parser.parse_args(argv)

    try:
        payload = run(profile_name=args.profile, config_path=args.config)
    except NoCandidatesError as exc:
        print(f"fetch_news: {exc}", file=sys.stderr)
        return 1

    logger.info(
        "wrote %s: profile=%s items=%d thin_news=%s market=%s",
        CANDIDATES_PATH, payload["profile"], len(payload["items"]), payload["thin_news"],
        "present" if payload["market"] else "null",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
