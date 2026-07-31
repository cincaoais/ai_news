"""Tests for build_and_send.validate_selection(): the hard-gate validation
that runs before anything else in the digest pipeline -- unknown indices,
empty/oversized summaries, and (critically) URLs smuggled into any
model-authored text field, since the model must never author a URL."""

from __future__ import annotations

import pytest

import build_and_send as bas


def _candidates(profile="finance_metals"):
    return {
        "profile": profile,
        "date": "2026-07-31",
        "thin_news": False,
        "market": None,
        "items": [
            {
                "index": 0, "title": "Headline A", "source": "Src A",
                "url": "https://example.com/a", "published": "2026-07-31T00:00:00+00:00",
                "snippet": "s",
            },
            {
                "index": 1, "title": "Headline B", "source": "Src B",
                "url": "https://example.com/b", "published": "2026-07-31T00:00:00+00:00",
                "snippet": "s",
            },
        ],
    }


def test_validate_selection_happy_path_attaches_real_url_and_title():
    candidates = _candidates()
    selection = {
        "profile": "finance_metals",
        "date": "2026-07-31",
        "items": [{"index": 0, "summary": "A short, factual summary.", "category": "markets"}],
        "skipped_as_repeat": [],
    }

    result = bas.validate_selection(selection, candidates)

    assert result == [{
        "index": 0,
        "title": "Headline A",
        "url": "https://example.com/a",
        "source": "Src A",
        "summary": "A short, factual summary.",
        "category": "markets",
    }]


def test_validate_selection_preserves_selection_order_not_candidate_order():
    candidates = _candidates()
    selection = {"items": [
        {"index": 1, "summary": "second candidate, picked first"},
        {"index": 0, "summary": "first candidate, picked second"},
    ]}
    result = bas.validate_selection(selection, candidates)
    assert [r["index"] for r in result] == [1, 0]


def test_validate_selection_no_items_key_rejected():
    with pytest.raises(bas.ValidationError):
        bas.validate_selection({}, _candidates())


def test_validate_selection_empty_items_list_rejected():
    with pytest.raises(bas.ValidationError):
        bas.validate_selection({"items": []}, _candidates())


def test_validate_selection_unknown_index_names_it_in_the_error():
    selection = {"items": [{"index": 99, "summary": "ok"}]}
    with pytest.raises(bas.ValidationError, match="99"):
        bas.validate_selection(selection, _candidates())


def test_validate_selection_empty_summary_rejected():
    selection = {"items": [{"index": 0, "summary": "   "}]}
    with pytest.raises(bas.ValidationError, match="(?i)summary"):
        bas.validate_selection(selection, _candidates())


def test_validate_selection_missing_summary_key_rejected():
    selection = {"items": [{"index": 0}]}
    with pytest.raises(bas.ValidationError, match="(?i)summary"):
        bas.validate_selection(selection, _candidates())


def test_validate_selection_summary_over_300_chars_rejected():
    selection = {"items": [{"index": 0, "summary": "x" * 301}]}
    with pytest.raises(bas.ValidationError, match="300"):
        bas.validate_selection(selection, _candidates())


def test_validate_selection_summary_exactly_300_chars_is_allowed():
    selection = {"items": [{"index": 0, "summary": "x" * 300}]}
    result = bas.validate_selection(selection, _candidates())
    assert len(result[0]["summary"]) == 300


@pytest.mark.parametrize("bad_summary", [
    "Read more at https://evil.example.com for details",
    "Read more at http://evil.example.com for details",
    "See www.example.com for the full story",
])
def test_validate_selection_url_smuggled_in_summary_rejected(bad_summary):
    selection = {"items": [{"index": 0, "summary": bad_summary}]}
    with pytest.raises(bas.ValidationError, match="(?i)url"):
        bas.validate_selection(selection, _candidates())


def test_validate_selection_url_smuggled_in_category_rejected():
    selection = {"items": [{"index": 0, "summary": "fine summary", "category": "see https://evil.example.com"}]}
    with pytest.raises(bas.ValidationError, match="(?i)url"):
        bas.validate_selection(selection, _candidates())


def test_validate_selection_category_optional_and_none_is_fine():
    selection = {"items": [{"index": 0, "summary": "fine summary"}]}
    result = bas.validate_selection(selection, _candidates())
    assert result[0]["category"] is None


def test_validate_selection_category_must_be_a_string():
    selection = {"items": [{"index": 0, "summary": "fine summary", "category": 123}]}
    with pytest.raises(bas.ValidationError):
        bas.validate_selection(selection, _candidates())


def test_validate_selection_profile_mismatch_rejected():
    selection = {"profile": "sports", "items": [{"index": 0, "summary": "fine summary"}]}
    with pytest.raises(bas.ValidationError, match="profile"):
        bas.validate_selection(selection, _candidates(profile="finance_metals"))
