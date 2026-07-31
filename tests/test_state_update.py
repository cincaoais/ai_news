"""Tests for the same-day guard (build_and_send.py), state append + 30-day
prune, last_digest.md front-matter rewriting, and update_state.py's git
commit/push resilience (rebase-retry on a rejected push; clean skip with
no remote at all) -- against real throwaway git repos under tmp_path, no
network involved."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import build_and_send as bas
import update_state as us
from lib import state

NOW = datetime(2026, 7, 31, 4, 0, 0, tzinfo=timezone.utc)  # 12:00 MYT


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Same-day guard (build_and_send.check_same_day_guard / .run)
# ---------------------------------------------------------------------------

def test_same_day_guard_blocks_matching_date_and_profile(tmp_path):
    last_digest = tmp_path / "last_digest.md"
    state.write_last_digest(last_digest, date="2026-07-31", profile="finance_metals", headlines=["A"])

    with pytest.raises(bas.SameDayGuardError, match="finance_metals"):
        bas.check_same_day_guard(last_digest, "finance_metals", "2026-07-31", force=False)


def test_same_day_guard_allows_different_profile_same_date(tmp_path):
    last_digest = tmp_path / "last_digest.md"
    state.write_last_digest(last_digest, date="2026-07-31", profile="sports", headlines=["A"])

    bas.check_same_day_guard(last_digest, "finance_metals", "2026-07-31", force=False)  # no raise


def test_same_day_guard_allows_different_date_same_profile(tmp_path):
    last_digest = tmp_path / "last_digest.md"
    state.write_last_digest(last_digest, date="2026-07-30", profile="finance_metals", headlines=["A"])

    bas.check_same_day_guard(last_digest, "finance_metals", "2026-07-31", force=False)  # no raise


def test_same_day_guard_missing_file_allows_first_ever_send(tmp_path):
    bas.check_same_day_guard(tmp_path / "nope.md", "finance_metals", "2026-07-31", force=False)  # no raise


def test_same_day_guard_force_bypasses_even_exact_match(tmp_path):
    last_digest = tmp_path / "last_digest.md"
    state.write_last_digest(last_digest, date="2026-07-31", profile="finance_metals", headlines=["A"])

    bas.check_same_day_guard(last_digest, "finance_metals", "2026-07-31", force=True)  # no raise


def test_build_and_send_run_blocks_second_send_then_force_overrides(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    candidates = {
        "profile": "finance_metals", "date": "2026-07-31", "thin_news": False, "market": None,
        "items": [{
            "index": 0, "title": "Headline A", "source": "Src", "url": "https://example.com/a",
            "published": "2026-07-31T00:00:00+00:00", "snippet": "s",
        }],
    }
    selection = {
        "profile": "finance_metals", "date": "2026-07-31",
        "items": [{"index": 0, "summary": "Fine summary."}], "skipped_as_repeat": [],
    }
    _write_json(work_dir / "candidates.json", candidates)
    _write_json(work_dir / "selection.json", selection)

    state.write_last_digest(state_dir / "last_digest.md", date="2026-07-31", profile="finance_metals", headlines=["Something"])

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")

    sent_texts = []

    def fake_send_message(token, chat_id, text, **kwargs):
        sent_texts.append(text)
        return {"message_id": len(sent_texts)}

    monkeypatch.setattr(bas.telegram, "send_message", fake_send_message)

    with pytest.raises(bas.SameDayGuardError):
        bas.run(dry_run=False, force=False, work_dir=work_dir, state_dir=state_dir, now=NOW)
    assert sent_texts == []  # blocked before any send was attempted

    result = bas.run(dry_run=False, force=True, work_dir=work_dir, state_dir=state_dir, now=NOW)

    assert result["message_ids"] == [1]
    assert len(sent_texts) == 1
    assert (work_dir / "sent.json").exists()
    sent_payload = json.loads((work_dir / "sent.json").read_text(encoding="utf-8"))
    assert sent_payload["message_ids"] == [1]
    assert "sent_at" in sent_payload


# ---------------------------------------------------------------------------
# seen.jsonl append + prune (update_state.py)
# ---------------------------------------------------------------------------

def test_append_seen_writes_canonicalized_rows(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    items = [
        {"index": 0, "title": "Headline A", "url": "https://example.com/a?utm_source=x", "source": "Src", "summary": "s", "category": None},
        {"index": 1, "title": "Headline B", "url": "https://EXAMPLE.com/b/", "source": "Src", "summary": "s", "category": None},
    ]

    us.append_seen(seen_path, items, today="2026-07-31")

    rows = [json.loads(line) for line in seen_path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {"url": "https://example.com/a", "title": "Headline A", "date": "2026-07-31"},
        {"url": "https://example.com/b", "title": "Headline B", "date": "2026-07-31"},
    ]


def test_prune_seen_removes_entries_older_than_30_days(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    rows = [
        {"url": "https://example.com/old", "title": "Old", "date": "2026-06-01"},      # 60 days before -> pruned
        {"url": "https://example.com/recent", "title": "Recent", "date": "2026-07-15"},  # within 30 days -> kept
        {"url": "https://example.com/today", "title": "Today", "date": "2026-07-31"},   # today -> kept
    ]
    with seen_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    pruned = us.prune_seen(seen_path, today="2026-07-31")

    remaining_urls = [json.loads(line)["url"] for line in seen_path.read_text(encoding="utf-8").splitlines()]
    assert pruned == 1
    assert remaining_urls == ["https://example.com/recent", "https://example.com/today"]


def test_prune_seen_keeps_rows_with_missing_or_unparseable_date_fail_open(tmp_path):
    seen_path = tmp_path / "seen.jsonl"
    seen_path.write_text(
        '{"url": "https://example.com/a", "title": "A"}\n'
        '{"url": "https://example.com/b", "title": "B", "date": "not-a-date"}\n',
        encoding="utf-8",
    )

    pruned = us.prune_seen(seen_path, today="2026-07-31")

    text = seen_path.read_text(encoding="utf-8")
    assert pruned == 0
    assert "https://example.com/a" in text
    assert "https://example.com/b" in text


def test_prune_seen_missing_file_is_a_noop(tmp_path):
    assert us.prune_seen(tmp_path / "nope.jsonl", today="2026-07-31") == 0


# ---------------------------------------------------------------------------
# last_digest.md format (lib/state.py)
# ---------------------------------------------------------------------------

def test_write_last_digest_front_matter_and_body(tmp_path):
    path = tmp_path / "last_digest.md"
    state.write_last_digest(path, date="2026-07-31", profile="finance_metals", headlines=["Headline A", "Headline B"])

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")

    meta = state.read_last_digest_front_matter(path)
    assert meta == {"date": "2026-07-31", "profile": "finance_metals"}
    assert "Headline A" in text
    assert "Headline B" in text


def test_read_last_digest_front_matter_missing_file_returns_none(tmp_path):
    assert state.read_last_digest_front_matter(tmp_path / "nope.md") is None


def test_read_last_digest_front_matter_no_front_matter_returns_none(tmp_path):
    path = tmp_path / "last_digest.md"
    path.write_text("just a plain markdown file, no front-matter\n", encoding="utf-8")
    assert state.read_last_digest_front_matter(path) is None


# ---------------------------------------------------------------------------
# update_state.load_sent_items (guarded by work/sent.json)
# ---------------------------------------------------------------------------

def test_load_sent_items_requires_sent_json(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with pytest.raises(us.UpdateStateError, match="sent.json"):
        us.load_sent_items(work_dir)


def test_load_sent_items_reconstructs_from_candidates_and_selection(tmp_path):
    work_dir = tmp_path / "work"
    candidates = {
        "profile": "finance_metals",
        "items": [{
            "index": 0, "title": "Headline A", "source": "Src",
            "url": "https://example.com/a", "published": "2026-07-31T00:00:00+00:00", "snippet": "s",
        }],
    }
    selection = {"items": [{"index": 0, "summary": "Fine summary."}]}
    _write_json(work_dir / "candidates.json", candidates)
    _write_json(work_dir / "selection.json", selection)
    _write_json(work_dir / "sent.json", {"message_ids": [1], "sent_at": "2026-07-31T04:00:00+00:00"})

    items, profile = us.load_sent_items(work_dir)

    assert profile == "finance_metals"
    assert items == [{
        "index": 0, "title": "Headline A", "url": "https://example.com/a",
        "source": "Src", "summary": "Fine summary.", "category": None,
    }]


# ---------------------------------------------------------------------------
# git commit + push resilience (real throwaway repos under tmp_path)
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed (cwd={cwd}):\n{result.stdout}\n{result.stderr}"
    return result


def _init_local_repo(path: Path, *, email: str, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", email, cwd=path)
    _git("config", "user.name", name, cwd=path)


def test_git_commit_and_push_skips_cleanly_with_no_remote(tmp_path):
    repo = tmp_path / "solo_repo"
    _init_local_repo(repo, email="dev@example.com", name="Dev")
    (repo / "state").mkdir()
    (repo / "state" / "seen.jsonl").write_text('{"url": "https://example.com/x"}\n', encoding="utf-8")

    us.git_commit_and_push(repo, "state: digest 2026-07-31")  # must not raise

    log = _git("log", "--oneline", cwd=repo)
    assert "state: digest 2026-07-31" in log.stdout


def test_git_commit_and_push_noop_when_nothing_changed(tmp_path):
    repo = tmp_path / "empty_repo"
    _init_local_repo(repo, email="dev@example.com", name="Dev")
    (repo / "state").mkdir()
    (repo / "state" / "seen.jsonl").write_text("", encoding="utf-8")
    _git("add", "state/", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)

    us.git_commit_and_push(repo, "state: digest 2026-07-31")  # nothing staged -> no-op, no raise

    log = _git("log", "--oneline", cwd=repo)
    assert log.stdout.strip().count("\n") == 0  # still just the one "initial" commit


def test_git_commit_and_push_retries_after_rejected_push(tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), cwd=tmp_path)

    work = tmp_path / "work_repo"
    _init_local_repo(work, email="dev@example.com", name="Dev")
    (work / "state").mkdir()
    (work / "state" / "seen.jsonl").write_text("", encoding="utf-8")
    _git("add", "state/", cwd=work)
    _git("commit", "-m", "initial", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)

    # A second clone pushes a commit to a DIFFERENT file first, so `work`'s
    # eventual push will be rejected (non-fast-forward) but its later
    # rebase is guaranteed conflict-free.
    other = tmp_path / "other_clone"
    _git("clone", "--branch", "main", str(remote), str(other), cwd=tmp_path)
    _git("config", "user.email", "other@example.com", cwd=other)
    _git("config", "user.name", "Other", cwd=other)
    (other / "state" / "other_marker.md").write_text("from the other clone\n", encoding="utf-8")
    _git("add", "state/", cwd=other)
    _git("commit", "-m", "other push", cwd=other)
    _git("push", cwd=other)

    # `work` is now behind origin by one commit. Make our own state change
    # (a different file) and attempt to commit+push -- this must be
    # rejected once, then recover via pull --rebase + retry.
    (work / "state" / "seen.jsonl").write_text('{"url": "https://example.com/mine"}\n', encoding="utf-8")

    us.git_commit_and_push(work, "state: digest 2026-08-01")  # must not raise

    log = _git("log", "--oneline", cwd=work)
    assert "other push" in log.stdout
    assert "state: digest 2026-08-01" in log.stdout
    assert (work / "state" / "other_marker.md").exists()  # rebase pulled in the other commit's file too
