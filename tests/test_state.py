"""Tests for scripts/lib/state.py: URL canonicalization + seen-set load/bootstrap."""

from __future__ import annotations

import json

import pytest

from lib import state


@pytest.mark.parametrize(
    "raw, expected",
    [
        # utm_* params stripped
        (
            "https://example.com/a/b?utm_source=twitter&utm_medium=social&id=42",
            "https://example.com/a/b?id=42",
        ),
        # all-utm query string collapses to no query string at all
        (
            "https://example.com/a/b?utm_source=twitter&utm_campaign=x",
            "https://example.com/a/b",
        ),
        # fragment stripped
        ("https://example.com/a/b#section-2", "https://example.com/a/b"),
        # host lowercased
        ("https://EXAMPLE.com/a/b", "https://example.com/a/b"),
        # trailing slash dropped
        ("https://example.com/a/b/", "https://example.com/a/b"),
        # root path trailing slash kept as "/"  (never collapsed to empty)
        ("https://example.com/", "https://example.com/"),
        # combination: case + utm + fragment + trailing slash all together
        (
            "https://EXAMPLE.com/a/b/?utm_source=x&utm_medium=y#frag",
            "https://example.com/a/b",
        ),
        # non-utm query params are preserved as-is
        ("https://example.com/a?x=1&y=2", "https://example.com/a?x=1&y=2"),
    ],
)
def test_canonicalize_url_table(raw, expected):
    assert state.canonicalize_url(raw) == expected


def test_canonicalize_url_is_idempotent():
    raw = "https://EXAMPLE.com/a/b/?utm_source=x#frag"
    once = state.canonicalize_url(raw)
    twice = state.canonicalize_url(once)
    assert once == twice


def test_load_seen_missing_file_bootstraps_empty(tmp_path):
    missing = tmp_path / "seen.jsonl"
    assert not missing.exists()
    seen = state.load_seen(missing)
    assert seen == set()


def test_load_seen_reads_and_canonicalizes_urls(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    rows = [
        {"url": "https://example.com/a?utm_source=x", "title": "A"},
        {"url": "https://EXAMPLE.com/b/", "title": "B"},
    ]
    with seen_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    seen = state.load_seen(seen_path)
    assert seen == {"https://example.com/a", "https://example.com/b"}


def test_load_seen_skips_malformed_lines(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    with seen_path.open("w", encoding="utf-8") as f:
        f.write('{"url": "https://example.com/ok"}\n')
        f.write("not json at all\n")
        f.write("\n")  # blank line

    seen = state.load_seen(seen_path)
    assert seen == {"https://example.com/ok"}


def test_bootstrap_state_dir_creates_dir_and_empty_seen_file(tmp_path):
    state_dir = tmp_path / "state"
    assert not state_dir.exists()

    state.bootstrap_state_dir(state_dir)

    assert state_dir.is_dir()
    seen_path = state_dir / "seen.jsonl"
    assert seen_path.exists()
    assert seen_path.read_text(encoding="utf-8") == ""


def test_parse_front_matter_valid_text():
    text = '---\ndate: "2026-07-31"\nprofile: finance_metals\n---\n\n# Digest\n'
    assert state.parse_front_matter(text) == {"date": "2026-07-31", "profile": "finance_metals"}


def test_parse_front_matter_no_front_matter_returns_none():
    assert state.parse_front_matter("just plain text, no front-matter\n") is None


def test_parse_front_matter_malformed_yaml_returns_none():
    text = "---\n: not: valid: yaml: at: all:\n---\n"
    assert state.parse_front_matter(text) is None


def test_bootstrap_state_dir_exist_ok_does_not_clobber_existing_seen(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seen_path = state_dir / "seen.jsonl"
    seen_path.write_text('{"url": "https://example.com/keep"}\n', encoding="utf-8")

    state.bootstrap_state_dir(state_dir)

    assert seen_path.read_text(encoding="utf-8") == '{"url": "https://example.com/keep"}\n'
