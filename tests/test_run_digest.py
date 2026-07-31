"""Tests for scripts/run_digest.py: the local/CI wrapper that chains
fetch_news -> the claude editorial step -> build_and_send -> update_state,
with every stage's failure routed through notify_failure.py (stage-tagged)
before a nonzero exit.

Subprocess is mocked completely -- no real `claude` invocation happens in
this suite (see docs/03-EXECUTION-LOG.md's Slice 3 live-gate section for the
one real run, done manually outside pytest)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import build_and_send
import run_digest as rd


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


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


def _valid_selection(profile="finance_metals"):
    return {
        "profile": profile, "date": "2026-07-31",
        "items": [{"index": 0, "summary": "Fine, grounded summary."}],
        "skipped_as_repeat": [],
    }


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _notifier_stub(monkeypatch):
    """Swap notify_failure.main for a capturing stub in every test, so no
    test needs real Telegram env vars, and every test can assert on
    exactly what the notifier was told."""
    calls = []
    monkeypatch.setattr(rd.notify_failure, "main", lambda argv: calls.append(argv[0]) or 0)
    return calls


# ---------------------------------------------------------------------------
# Wrapper sequence: happy path, fetch-fail, --skip-state, dry-run auto-skip
# ---------------------------------------------------------------------------

def test_happy_path_runs_all_four_stages_in_order(tmp_path, monkeypatch, _notifier_stub):
    work_dir = tmp_path / "work"
    state_dir = tmp_path / "state"
    calls = []

    def fake_fetch_run(*, profile_name=None, config_path=None, state_dir=None, work_dir=None):
        calls.append("fetch")
        return {"profile": "finance_metals"}

    def fake_editorial(*, profile, work_dir, repo_root, mock_claude=None, timeout=900):
        calls.append("editorial")
        return _valid_selection()

    def fake_send_run(*, dry_run=False, force=False, work_dir=None, state_dir=None, now=None):
        calls.append("send")
        return {"messages": ["m"], "dry_run": dry_run}

    def fake_state_run(*, work_dir=None, state_dir=None, repo_root=None, now=None):
        calls.append("state")
        return {}

    monkeypatch.setattr(rd.fetch_news, "run", fake_fetch_run)
    monkeypatch.setattr(rd, "run_editorial_step", fake_editorial)
    monkeypatch.setattr(rd.build_and_send, "run", fake_send_run)
    monkeypatch.setattr(rd.update_state, "run", fake_state_run)

    exit_code = rd.run(work_dir=work_dir, state_dir=state_dir, repo_root=tmp_path)

    assert exit_code == 0
    assert calls == ["fetch", "editorial", "send", "state"]
    assert _notifier_stub == []  # no failure -> notifier never invoked


def test_fetch_failure_notifies_and_never_invokes_editorial(tmp_path, monkeypatch, _notifier_stub):
    def fake_fetch_run(**kwargs):
        raise rd.fetch_news.NoCandidatesError("0 candidates after filtering -- all feeds may be down")

    def should_not_be_called(**kwargs):
        raise AssertionError("editorial step must not run when fetch fails")

    monkeypatch.setattr(rd.fetch_news, "run", fake_fetch_run)
    monkeypatch.setattr(rd, "run_editorial_step", should_not_be_called)

    exit_code = rd.run(work_dir=tmp_path / "work", state_dir=tmp_path / "state", repo_root=tmp_path)

    assert exit_code == 1
    assert len(_notifier_stub) == 1
    assert _notifier_stub[0].startswith("fetch: ")


def test_skip_state_flag_skips_stage_four(tmp_path, monkeypatch, _notifier_stub):
    monkeypatch.setattr(rd.fetch_news, "run", lambda **kwargs: {"profile": "finance_metals"})
    monkeypatch.setattr(rd, "run_editorial_step", lambda **kwargs: _valid_selection())
    monkeypatch.setattr(rd.build_and_send, "run", lambda **kwargs: {"messages": [], "dry_run": False})
    monkeypatch.setattr(
        rd.update_state, "run",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("update_state must not run with --skip-state")),
    )

    exit_code = rd.run(
        work_dir=tmp_path / "work", state_dir=tmp_path / "state", repo_root=tmp_path, skip_state=True,
    )

    assert exit_code == 0
    assert _notifier_stub == []


def test_dry_run_also_skips_stage_four_even_without_skip_state(tmp_path, monkeypatch, _notifier_stub):
    monkeypatch.setattr(rd.fetch_news, "run", lambda **kwargs: {"profile": "finance_metals"})
    monkeypatch.setattr(rd, "run_editorial_step", lambda **kwargs: _valid_selection())
    monkeypatch.setattr(rd.build_and_send, "run", lambda **kwargs: {"messages": [], "dry_run": True})
    monkeypatch.setattr(
        rd.update_state, "run",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("update_state must not run under --dry-run")),
    )

    exit_code = rd.run(
        work_dir=tmp_path / "work", state_dir=tmp_path / "state", repo_root=tmp_path,
        dry_run=True, skip_state=False,
    )

    assert exit_code == 0


def test_dry_run_prints_digest_messages_to_stdout(tmp_path, monkeypatch, capsys, _notifier_stub):
    monkeypatch.setattr(rd.fetch_news, "run", lambda **kwargs: {"profile": "finance_metals"})
    monkeypatch.setattr(rd, "run_editorial_step", lambda **kwargs: _valid_selection())
    monkeypatch.setattr(
        rd.build_and_send, "run",
        lambda **kwargs: {"messages": ["<b>Header</b>\nfirst message body"], "dry_run": True},
    )
    monkeypatch.setattr(
        rd.update_state, "run",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("update_state must not run under --dry-run")),
    )

    exit_code = rd.run(work_dir=tmp_path / "work", state_dir=tmp_path / "state", repo_root=tmp_path, dry_run=True)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "--- message 1/1 ---" in out
    assert "first message body" in out


def test_send_stage_failure_notifies_send_tag(tmp_path, monkeypatch, _notifier_stub):
    monkeypatch.setattr(rd.fetch_news, "run", lambda **kwargs: {"profile": "finance_metals"})
    monkeypatch.setattr(rd, "run_editorial_step", lambda **kwargs: _valid_selection())

    def fake_send_run(**kwargs):
        raise build_and_send.ValidationError("selection references unknown candidate index 99")

    monkeypatch.setattr(rd.build_and_send, "run", fake_send_run)

    exit_code = rd.run(work_dir=tmp_path / "work", state_dir=tmp_path / "state", repo_root=tmp_path)

    assert exit_code == 1
    assert _notifier_stub[0].startswith("send: ")


def test_state_stage_failure_notifies_state_tag(tmp_path, monkeypatch, _notifier_stub):
    monkeypatch.setattr(rd.fetch_news, "run", lambda **kwargs: {"profile": "finance_metals"})
    monkeypatch.setattr(rd, "run_editorial_step", lambda **kwargs: _valid_selection())
    monkeypatch.setattr(rd.build_and_send, "run", lambda **kwargs: {"messages": [], "dry_run": False})

    def fake_state_run(**kwargs):
        raise rd.update_state.UpdateStateError("git push failed even after pull --rebase retry")

    monkeypatch.setattr(rd.update_state, "run", fake_state_run)

    exit_code = rd.run(work_dir=tmp_path / "work", state_dir=tmp_path / "state", repo_root=tmp_path)

    assert exit_code == 1
    assert _notifier_stub[0].startswith("state: ")


# ---------------------------------------------------------------------------
# run_editorial_step: retry, prose-tolerant parsing, limit-hit, timeout,
# --mock-claude, stale-file deletion
# ---------------------------------------------------------------------------

def test_editorial_step_missing_selection_retries_once_then_notifies(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        return _FakeCompletedProcess(stdout=json.dumps({"result": "done, but forgot to write the file"}))

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    with pytest.raises(rd.EditorialError, match="was not written"):
        rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert call_count["n"] == 2  # first attempt + exactly one retry


def test_editorial_step_prose_wrapped_json_parses_and_validates(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    prose_wrapped = (
        "Sure, here is my selection:\n\n```json\n"
        + json.dumps(_valid_selection())
        + "\n```\n\nLet me know if you need anything else!"
    )

    def fake_run(cmd, **kwargs):
        (work_dir / "selection.json").write_text(prose_wrapped, encoding="utf-8")
        return _FakeCompletedProcess(stdout=json.dumps({"result": "ok"}))

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    selection = rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert selection["items"][0]["index"] == 0


def test_editorial_step_usage_limit_hit_raises_distinct_message_no_retry(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        return _FakeCompletedProcess(
            stdout=json.dumps({"result": "You've hit your Max plan limit · resets 4pm"})
        )

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    with pytest.raises(rd.EditorialError, match="(?i)usage limit"):
        rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert call_count["n"] == 1  # no point retrying an exhausted quota


def test_editorial_step_timeout_raises_and_does_not_retry(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=900)

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    with pytest.raises(rd.EditorialError, match="timed out"):
        rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert call_count["n"] == 1


def test_mock_claude_bypasses_subprocess_entirely(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    fixture_path = tmp_path / "fixture_selection.json"
    fixture_path.write_text(json.dumps(_valid_selection()), encoding="utf-8")

    def should_not_be_called(cmd, **kwargs):
        raise AssertionError("subprocess.run must not be called under --mock-claude")

    monkeypatch.setattr(rd.subprocess, "run", should_not_be_called)

    selection = rd.run_editorial_step(
        profile="finance_metals", work_dir=work_dir, repo_root=tmp_path, mock_claude=fixture_path,
    )

    assert selection["items"][0]["index"] == 0
    assert (work_dir / "selection.json").read_text(encoding="utf-8") == fixture_path.read_text(encoding="utf-8")


def test_stale_selection_json_deleted_before_every_claude_invocation(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())
    selection_path = work_dir / "selection.json"
    selection_path.write_text('{"stale": "leftover from yesterday, not valid selection.json"}', encoding="utf-8")

    missing_at_call_time = []

    def fake_run(cmd, **kwargs):
        missing_at_call_time.append(not selection_path.exists())
        return _FakeCompletedProcess(stdout=json.dumps({"result": "did not write anything"}))

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    with pytest.raises(rd.EditorialError):
        rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert missing_at_call_time == [True, True]  # deleted before attempt 1 AND before the retry


def test_editorial_step_validation_error_appends_to_retry_prompt(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    prompts_seen = []

    def fake_run(cmd, **kwargs):
        prompt = cmd[cmd.index("-p") + 1]
        prompts_seen.append(prompt)
        if len(prompts_seen) == 1:
            # first attempt: references an unknown index -> validation error
            bad_selection = {"items": [{"index": 99, "summary": "bad"}]}
            (work_dir / "selection.json").write_text(json.dumps(bad_selection), encoding="utf-8")
        else:
            (work_dir / "selection.json").write_text(json.dumps(_valid_selection()), encoding="utf-8")
        return _FakeCompletedProcess(stdout=json.dumps({"result": "ok"}))

    monkeypatch.setattr(rd.subprocess, "run", fake_run)

    selection = rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert selection["items"][0]["index"] == 0
    assert len(prompts_seen) == 2
    assert "99" in prompts_seen[1]  # the retry prompt names the validation error


# ---------------------------------------------------------------------------
# _load_selection_prose_tolerant
# ---------------------------------------------------------------------------

def test_load_selection_prose_tolerant_pure_json(tmp_path):
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(_valid_selection()), encoding="utf-8")
    assert rd._load_selection_prose_tolerant(path)["items"][0]["index"] == 0


def test_load_selection_prose_tolerant_strips_surrounding_prose(tmp_path):
    path = tmp_path / "selection.json"
    path.write_text(
        "Here you go:\n```json\n" + json.dumps(_valid_selection()) + "\n```\nDone.",
        encoding="utf-8",
    )
    assert rd._load_selection_prose_tolerant(path)["items"][0]["index"] == 0


def test_load_selection_prose_tolerant_no_json_object_raises(tmp_path):
    path = tmp_path / "selection.json"
    path.write_text("I could not find any relevant news today.", encoding="utf-8")
    with pytest.raises(ValueError):
        rd._load_selection_prose_tolerant(path)


# ---------------------------------------------------------------------------
# CLI arg parsing / pass-through
# ---------------------------------------------------------------------------

def test_main_parses_and_passes_through_all_flags(monkeypatch, tmp_path):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(rd, "run", fake_run)

    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}", encoding="utf-8")

    exit_code = rd.main([
        "--profile", "sports",
        "--dry-run",
        "--force",
        "--mock-claude", str(fixture),
        "--skip-state",
    ])

    assert exit_code == 0
    assert captured["profile"] == "sports"
    assert captured["dry_run"] is True
    assert captured["force"] is True
    assert captured["mock_claude"] == fixture
    assert captured["skip_state"] is True


def test_main_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(rd, "run", lambda **kwargs: captured.update(kwargs) or 0)

    rd.main([])

    assert captured["profile"] is None
    assert captured["dry_run"] is False
    assert captured["force"] is False
    assert captured["mock_claude"] is None
    assert captured["skip_state"] is False
