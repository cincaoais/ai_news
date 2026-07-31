"""Tests for scripts/build_and_send.py's digest-formatting helpers, focused
on the Slice 6 feed-health footer: a one-line note appended to the digest
when fetch_news.py recorded any skipped (stale/dead) feeds, absent
otherwise, and always safe, single-line HTML regardless of what the
underlying feed labels contain."""

from __future__ import annotations

import build_and_send as bas


def _candidates(feed_warnings=None, thin_news=False, market=None):
    return {
        "profile": "finance_metals",
        "date": "2026-07-31",
        "thin_news": thin_news,
        "market": market,
        "feed_warnings": feed_warnings if feed_warnings is not None else [],
        "items": [],
    }


# ---------------------------------------------------------------------------
# build_feed_warnings_footer
# ---------------------------------------------------------------------------

def test_feed_warnings_footer_absent_when_healthy():
    assert bas.build_feed_warnings_footer(_candidates(feed_warnings=[])) is None


def test_feed_warnings_footer_absent_when_key_missing_entirely():
    candidates = _candidates()
    del candidates["feed_warnings"]
    assert bas.build_feed_warnings_footer(candidates) is None


def test_feed_warnings_footer_present_and_counts_correctly():
    footer = bas.build_feed_warnings_footer(
        _candidates(feed_warnings=["CNBC Top News: stale", "Bernama: dead"])
    )
    assert footer == "<i>note: 2 source feeds skipped (details in repo logs)</i>"


def test_feed_warnings_footer_single_feed_count():
    footer = bas.build_feed_warnings_footer(_candidates(feed_warnings=["Feed A: dead"]))
    assert footer == "<i>note: 1 source feeds skipped (details in repo logs)</i>"


def test_feed_warnings_footer_is_one_line_and_balanced_html():
    footer = bas.build_feed_warnings_footer(_candidates(feed_warnings=["Feed A: dead"]))
    assert "\n" not in footer
    assert footer.startswith("<i>") and footer.endswith("</i>")
    assert footer.count("<i>") == 1
    assert footer.count("</i>") == 1


def test_feed_warnings_footer_never_echoes_raw_label_content():
    # The footer must only ever surface a COUNT, never the warning strings
    # themselves -- those carry a feed label (config data, not model text),
    # but nothing in this codebase guarantees a feed label is HTML-safe, so
    # the footer's safety must not depend on that.
    warnings = ["<script>bad</script>: dead", "Feed & Co: stale"]
    footer = bas.build_feed_warnings_footer(_candidates(feed_warnings=warnings))
    assert footer == "<i>note: 2 source feeds skipped (details in repo logs)</i>"
    assert "<script>" not in footer
    assert "&" not in footer.replace("Feed & Co", "")  # sanity: no leaked label text at all


# ---------------------------------------------------------------------------
# build_digest_blocks: footer wiring
# ---------------------------------------------------------------------------

def test_build_digest_blocks_appends_footer_as_last_block_when_present():
    candidates = _candidates(feed_warnings=["Feed A: dead"])
    blocks = bas.build_digest_blocks(candidates, validated_items=[], today="2026-07-31")
    assert blocks[-1] == "<i>note: 1 source feeds skipped (details in repo logs)</i>"


def test_build_digest_blocks_no_footer_block_when_all_feeds_healthy():
    candidates = _candidates(feed_warnings=[])
    blocks = bas.build_digest_blocks(candidates, validated_items=[], today="2026-07-31")
    assert not any("source feeds skipped" in b for b in blocks)


def test_build_digest_blocks_footer_appears_after_item_blocks():
    candidates = _candidates(feed_warnings=["Feed A: dead"])
    item = {
        "index": 0, "title": "Headline", "url": "https://example.com/a",
        "source": "Src", "summary": "Summary text.", "category": None,
    }
    blocks = bas.build_digest_blocks(candidates, validated_items=[item], today="2026-07-31")
    assert len(blocks) == 3  # header, item, footer
    assert "Headline" in blocks[1]
    assert blocks[2] == "<i>note: 1 source feeds skipped (details in repo logs)</i>"
