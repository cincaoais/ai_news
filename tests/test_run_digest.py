"""Tests for scripts/run_digest.py: the local/CI wrapper that chains
fetch_news -> the LLM editorial step (DeepSeek API call) -> build_and_send ->
update_state, with every stage's failure routed through notify_failure.py
(stage-tagged) before a nonzero exit.

The LLM call (_call_llm) is mocked completely -- no real DeepSeek API
invocation happens in this suite. _call_llm's own HTTP layer is tested
separately below with a fake requests.post response."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

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


class _FakeHTTPResponse:
    """Stand-in for requests.Response in _call_llm tests."""

    def __init__(self, status_code: int = 200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


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

    def fake_editorial(*, profile, work_dir, repo_root, state_dir=None, mock_claude=None, timeout=900):
        calls.append("editorial")
        return _valid_selection()

    def fake_send_run(*, dry_run=False, force=False, work_dir=None, state_dir=None, repo_root=None, now=None):
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


def test_send_stage_receives_repo_root(tmp_path, monkeypatch, _notifier_stub):
    """build_and_send.py's F6 pre-send origin re-check needs the actual
    repo_root run_digest.py was given (e.g. a test fixture repo, not
    whatever the CWD happens to be) -- verify run_digest.py forwards it."""
    monkeypatch.setattr(rd.fetch_news, "run", lambda **kwargs: {"profile": "finance_metals"})
    monkeypatch.setattr(rd, "run_editorial_step", lambda **kwargs: _valid_selection())
    monkeypatch.setattr(rd.update_state, "run", lambda **kwargs: {})

    captured = {}

    def fake_send_run(**kwargs):
        captured.update(kwargs)
        return {"messages": [], "dry_run": False}

    monkeypatch.setattr(rd.build_and_send, "run", fake_send_run)

    rd.run(work_dir=tmp_path / "work", state_dir=tmp_path / "state", repo_root=tmp_path)

    assert captured["repo_root"] == tmp_path


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
# run_editorial_step: retry, prose-tolerant parsing, API-error, timeout,
# --mock-claude, stale-file deletion, prompt contents
# ---------------------------------------------------------------------------

def test_editorial_step_invalid_output_retries_once_then_notifies(tmp_path, monkeypatch):
    """The model's reply is not valid JSON on both attempts -> one retry
    (with the error appended) then EditorialError, never a send."""
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    call_count = {"n": 0}

    def fake_call_llm(prompt, timeout):
        call_count["n"] += 1
        return "I could not find any relevant news today."

    monkeypatch.setattr(rd, "_call_llm", fake_call_llm)

    with pytest.raises(rd.EditorialError, match="still invalid after one retry"):
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

    monkeypatch.setattr(rd, "_call_llm", lambda prompt, timeout: prose_wrapped)

    selection = rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert selection["items"][0]["index"] == 0


def test_editorial_step_api_error_raises_immediately_no_retry(tmp_path, monkeypatch):
    """A hard API failure (e.g. insufficient balance) is non-retryable:
    raise immediately, never retry with the error appended to the prompt."""
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    call_count = {"n": 0}

    def fake_call_llm(prompt, timeout):
        call_count["n"] += 1
        raise rd.EditorialError("LLM API error 402: Insufficient Balance")

    monkeypatch.setattr(rd, "_call_llm", fake_call_llm)

    with pytest.raises(rd.EditorialError, match="402"):
        rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert call_count["n"] == 1


def test_editorial_step_timeout_raises_immediately_no_retry(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    call_count = {"n": 0}

    def fake_call_llm(prompt, timeout):
        call_count["n"] += 1
        raise rd.EditorialError("LLM API request failed: timed out after 900s")

    monkeypatch.setattr(rd, "_call_llm", fake_call_llm)

    with pytest.raises(rd.EditorialError, match="timed out"):
        rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert call_count["n"] == 1


def test_mock_claude_bypasses_llm_call_entirely(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    fixture_path = tmp_path / "fixture_selection.json"
    fixture_path.write_text(json.dumps(_valid_selection()), encoding="utf-8")

    def should_not_be_called(prompt, timeout):
        raise AssertionError("_call_llm must not be called under --mock-claude")

    monkeypatch.setattr(rd, "_call_llm", should_not_be_called)

    selection = rd.run_editorial_step(
        profile="finance_metals", work_dir=work_dir, repo_root=tmp_path, mock_claude=fixture_path,
    )

    assert selection["items"][0]["index"] == 0
    assert (work_dir / "selection.json").read_text(encoding="utf-8") == fixture_path.read_text(encoding="utf-8")


def test_stale_selection_json_deleted_before_every_llm_call(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())
    selection_path = work_dir / "selection.json"
    selection_path.write_text('{"stale": "leftover from yesterday, not valid selection.json"}', encoding="utf-8")

    missing_at_call_time = []

    def fake_call_llm(prompt, timeout):
        missing_at_call_time.append(not selection_path.exists())
        return "not valid json at all"

    monkeypatch.setattr(rd, "_call_llm", fake_call_llm)

    with pytest.raises(rd.EditorialError):
        rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert missing_at_call_time == [True, True]  # deleted before attempt 1 AND before the retry


def test_editorial_step_validation_error_appends_to_retry_prompt(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    prompts_seen = []

    def fake_call_llm(prompt, timeout):
        prompts_seen.append(prompt)
        if len(prompts_seen) == 1:
            # first attempt: references an unknown index -> validation error
            bad_selection = {"items": [{"index": 99, "summary": "bad"}]}
            return json.dumps(bad_selection)
        return json.dumps(_valid_selection())

    monkeypatch.setattr(rd, "_call_llm", fake_call_llm)

    selection = rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert selection["items"][0]["index"] == 0
    assert len(prompts_seen) == 2
    assert "99" in prompts_seen[1]  # the retry prompt names the validation error


def test_editorial_step_prompt_embeds_candidates_last_digest_and_focus(tmp_path, monkeypatch):
    """The API model cannot read files, so the prompt must carry everything
    inline: candidates.json, last_digest.md, and the profile's
    editorial_focus from config/profiles/<profile>.yaml."""
    work_dir = tmp_path / "work"
    state_dir = tmp_path / "state"
    _write_json(work_dir / "candidates.json", _candidates())

    last_digest_path = state_dir / "last_digest.md"
    last_digest_path.parent.mkdir(parents=True, exist_ok=True)
    last_digest_path.write_text(
        "---\ndate: \"2026-07-30\"\nprofile: finance_metals\n---\n\n# Digest 2026-07-30 (finance_metals)\n\n- Old headline A\n",
        encoding="utf-8",
    )

    profile_path = tmp_path / "config" / "profiles" / "finance_metals.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("editorial_focus: >\n  Gold, US stocks, and macro moves matter most.\n", encoding="utf-8")

    captured = {}

    def fake_call_llm(prompt, timeout):
        captured["prompt"] = prompt
        return json.dumps(_valid_selection())

    monkeypatch.setattr(rd, "_call_llm", fake_call_llm)

    rd.run_editorial_step(
        profile="finance_metals", work_dir=work_dir, repo_root=tmp_path, state_dir=state_dir,
    )

    prompt = captured["prompt"]
    assert "Headline A" in prompt          # candidates embedded
    assert "Headline B" in prompt
    assert "# Digest 2026-07-30" in prompt  # yesterday's digest embedded (soft dedup)
    assert "Old headline A" in prompt
    assert "Gold, US stocks, and macro moves matter most." in prompt  # editorial focus embedded
    assert "work/candidates.json (profile=finance_metals)" in prompt


def test_editorial_step_prompt_handles_missing_last_digest(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    _write_json(work_dir / "candidates.json", _candidates())

    captured = {}

    def fake_call_llm(prompt, timeout):
        captured["prompt"] = prompt
        return json.dumps(_valid_selection())

    monkeypatch.setattr(rd, "_call_llm", fake_call_llm)

    rd.run_editorial_step(profile="finance_metals", work_dir=work_dir, repo_root=tmp_path)

    assert "(no previous digest yet -- first run)" in captured["prompt"]


# ---------------------------------------------------------------------------
# _call_llm: the DeepSeek HTTP layer
# ---------------------------------------------------------------------------

def test_call_llm_success(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _FakeHTTPResponse(json_data={"choices": [{"message": {"content": '{"items": []}'}}]})

    monkeypatch.setattr(rd.requests, "post", fake_post)

    assert rd._call_llm("prompt text", timeout=30) == '{"items": []}'

    assert captured["url"] == rd.DEEPSEEK_API_URL
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "deepseek-chat"
    assert captured["timeout"] == 30
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_call_llm_missing_key_raises_without_network(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("requests.post must not be called without an API key")

    monkeypatch.setattr(rd.requests, "post", should_not_be_called)

    with pytest.raises(rd.EditorialError, match="DEEPSEEK_API_KEY"):
        rd._call_llm("prompt text", timeout=30)


def test_call_llm_http_error_includes_api_message(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        rd.requests, "post",
        lambda *args, **kwargs: _FakeHTTPResponse(
            status_code=402, json_data={"error": {"message": "Insufficient Balance"}}
        ),
    )

    with pytest.raises(rd.EditorialError, match="Insufficient Balance"):
        rd._call_llm("prompt text", timeout=30)


def test_call_llm_request_exception_raises(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def fake_post(*args, **kwargs):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(rd.requests, "post", fake_post)

    with pytest.raises(rd.EditorialError, match="request failed"):
        rd._call_llm("prompt text", timeout=30)


def test_call_llm_missing_content_field_raises(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        rd.requests, "post",
        lambda *args, **kwargs: _FakeHTTPResponse(json_data={"choices": [{"message": {}}]}),
    )

    with pytest.raises(rd.EditorialError, match="missing choices"):
        rd._call_llm("prompt text", timeout=30)


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


def test_load_selection_prose_tolerant_tolerates_utf8_bom(tmp_path):
    path = tmp_path / "selection.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_valid_selection()).encode("utf-8"))
    assert rd._load_selection_prose_tolerant(path)["items"][0]["index"] == 0


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
