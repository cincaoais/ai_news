"""Tests for build_and_send.validate_selection(): the hard-gate validation
that runs before anything else in the digest pipeline -- unknown indices,
empty/oversized summaries, and (critically) URLs smuggled into any
model-authored text field, since the model must never author a URL."""

from __future__ import annotations

import json

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


# ---------------------------------------------------------------------------
# F4: item-count bounds (min_items/max_items from candidates.json's `limits`)
# ---------------------------------------------------------------------------

def _candidates_many(n, *, profile="finance_metals", thin_news=False, limits=None):
    items = [
        {
            "index": i, "title": f"Headline {i}", "source": "Src",
            "url": f"https://example.com/{i}", "published": "2026-07-31T00:00:00+00:00",
            "snippet": "s",
        }
        for i in range(n)
    ]
    payload = {
        "profile": profile, "date": "2026-07-31", "thin_news": thin_news, "market": None,
        "items": items,
    }
    if limits is not None:
        payload["limits"] = limits
    return payload


def _selection_of(n):
    return {"items": [{"index": i, "summary": f"summary {i}"} for i in range(n)]}


def test_validate_selection_rejects_nine_items_over_default_max_of_eight():
    candidates = _candidates_many(20)
    with pytest.raises(bas.ValidationError, match="(?i)max"):
        bas.validate_selection(_selection_of(9), candidates)


def test_validate_selection_rejects_twenty_items_over_max():
    candidates = _candidates_many(25)
    with pytest.raises(bas.ValidationError, match="(?i)max"):
        bas.validate_selection(_selection_of(20), candidates)


def test_validate_selection_rejects_below_min_when_enough_candidates_and_not_thin():
    candidates = _candidates_many(40)  # plenty of candidates, thin_news defaults False
    with pytest.raises(bas.ValidationError, match="(?i)min"):
        bas.validate_selection(_selection_of(2), candidates)


def test_validate_selection_below_min_accepted_when_thin_news_true():
    candidates = _candidates_many(40, thin_news=True)
    result = bas.validate_selection(_selection_of(2), candidates)
    assert len(result) == 2


def test_validate_selection_below_min_accepted_when_only_three_candidates_exist():
    candidates = _candidates_many(3)
    result = bas.validate_selection(_selection_of(2), candidates)
    assert len(result) == 2


def test_validate_selection_respects_custom_limits_from_candidates_json():
    candidates = _candidates_many(10, limits={"min_items": 2, "max_items": 3})
    # 4 items exceeds the custom max of 3, even though it's under the module
    # default max of 8.
    with pytest.raises(bas.ValidationError, match="(?i)max"):
        bas.validate_selection(_selection_of(4), candidates)


def test_validate_selection_exactly_at_max_is_allowed():
    candidates = _candidates_many(20, limits={"min_items": 1, "max_items": 8})
    result = bas.validate_selection(_selection_of(8), candidates)
    assert len(result) == 8


def test_validate_selection_error_names_actual_and_allowed_counts():
    candidates = _candidates_many(20)
    with pytest.raises(bas.ValidationError, match="9") as excinfo:
        bas.validate_selection(_selection_of(9), candidates)
    assert "8" in str(excinfo.value)  # the allowed max is named too


# ---------------------------------------------------------------------------
# F8: load_json tolerates a UTF-8 BOM
# ---------------------------------------------------------------------------

def test_load_json_tolerates_utf8_bom(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode("utf-8"))
    assert bas.load_json(path) == {"a": 1}


def test_load_json_still_works_without_a_bom(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert bas.load_json(path) == {"a": 1}
