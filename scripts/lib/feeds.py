"""RSS/Atom feed fetching, freshness checks, and lookback filtering.

Used both for a profile's regular `feeds:` list and its `gnews_fallback:`
Google News RSS queries -- they go through the exact same fetch path since
both are plain RSS.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from time import mktime
from typing import Optional
from urllib.parse import urlencode

import feedparser
import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 10
STALE_FEED_HOURS = 48

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class FeedItem:
    title: str
    source: str
    url: str
    published: datetime  # tz-aware, UTC
    snippet: str


def strip_html(text: str) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace."""
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    unescaped = unescape(no_tags)
    return _WS_RE.sub(" ", unescaped).strip()


def make_snippet(text: str, max_len: int = 300) -> str:
    """Strip HTML from a feed summary and truncate to at most max_len chars."""
    cleaned = strip_html(text)
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def google_news_rss_url(query: str, hl: str = "en-US", gl: str = "US", ceid: str = "US:en") -> str:
    """Build a Google News RSS pseudo-search URL for a gnews_fallback query."""
    params = urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid})
    return f"https://news.google.com/rss/search?{params}"


def _entry_datetime(entry) -> Optional[datetime]:
    # feedparser entries are dict-like (FeedParserDict)
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                return datetime.fromtimestamp(mktime(struct), tz=timezone.utc)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def _entry_source_label(entry, default_label: str) -> str:
    """Prefer the entry's own <source> (e.g. Google News's originating
    publisher) over the feed-level human label, when present."""
    src = entry.get("source")
    if src:
        title = src.get("title") if isinstance(src, dict) else getattr(src, "title", None)
        if title:
            return title
    return default_label


def fetch_feed_text(url: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> Optional[str]:
    """Fetch raw feed content with a browser User-Agent.

    Returns None (never raises) on any network/HTTP error -- callers treat
    that as a dead feed to be skipped.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("dead feed (network error), skipping %s: %s", url, exc)
        return None


def fetch_feed_items(
    label: str,
    url: str,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    stale_hours: int = STALE_FEED_HOURS,
    now: Optional[datetime] = None,
    warnings_out: Optional[list[str]] = None,
) -> list[FeedItem]:
    """Fetch and parse one feed, applying the per-feed freshness check.

    Returns an UNFILTERED (by lookback) list of FeedItems -- lookback
    filtering is a separate, cheap, re-runnable step (see filter_by_lookback)
    so the orchestrator can widen the lookback window without refetching.

    Returns an empty list -- logging a warning, never raising -- for: dead
    feeds (network/parse error), feeds with no usable entries, and stale
    feeds (newest entry older than stale_hours).

    `warnings_out`, if given, gets one "<label>: dead" or "<label>: stale"
    string appended whenever this feed is skipped. fetch_news.py collects
    these across every feed + gnews_fallback query into candidates.json's
    `feed_warnings` list, which build_and_send.py surfaces as a one-line
    digest footer (Slice 6). A healthy feed appends nothing.
    """
    now = now or datetime.now(timezone.utc)

    text = fetch_feed_text(url, timeout=timeout)
    if text is None:
        if warnings_out is not None:
            warnings_out.append(f"{label}: dead")
        return []

    parsed = feedparser.parse(text)
    entries = parsed.entries

    if not entries:
        reason = getattr(parsed, "bozo_exception", None)
        logger.warning("dead feed (no usable entries), skipping %s (%s): %s", label, url, reason)
        if warnings_out is not None:
            warnings_out.append(f"{label}: dead")
        return []

    newest_dt: Optional[datetime] = None
    for entry in entries:
        dt = _entry_datetime(entry)
        if dt and (newest_dt is None or dt > newest_dt):
            newest_dt = dt

    if newest_dt is None:
        logger.warning("feed %s (%s) has no parseable entry dates; skipping", label, url)
        if warnings_out is not None:
            warnings_out.append(f"{label}: dead")
        return []

    age = now - newest_dt
    if age > timedelta(hours=stale_hours):
        logger.warning(
            "STALE feed skipped: %s (%s) newest entry is %.1fh old (> %dh)",
            label, url, age.total_seconds() / 3600, stale_hours,
        )
        if warnings_out is not None:
            warnings_out.append(f"{label}: stale")
        return []

    items: list[FeedItem] = []
    for entry in entries:
        dt = _entry_datetime(entry)
        if dt is None:
            continue
        title = strip_html(entry.get("title", ""))
        link = entry.get("link", "")
        summary = entry.get("summary") or entry.get("description") or ""
        if not title or not link:
            continue
        items.append(
            FeedItem(
                title=title,
                source=_entry_source_label(entry, label),
                url=link,
                published=dt,
                snippet=make_snippet(summary),
            )
        )
    return items


def filter_by_lookback(
    items: list[FeedItem], lookback_hours: float, *, now: Optional[datetime] = None
) -> list[FeedItem]:
    """Keep only items published within the last lookback_hours."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    return [item for item in items if item.published >= cutoff]
