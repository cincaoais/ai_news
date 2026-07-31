"""Tests for scripts/notify_failure.py: plain-text (no parse_mode) send,
and that the notifier always swallows its own failures rather than raising
or exiting nonzero (which could mask the ORIGINAL failure that triggered
it)."""

from __future__ import annotations

import notify_failure


def test_send_plain_text_has_no_parse_mode_key(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "result": {}}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(notify_failure.requests, "post", fake_post)

    notify_failure.send_plain_text("TOKEN", "CHAT", "hello")

    assert "parse_mode" not in captured["json"]
    assert captured["json"]["text"] == "hello"
    assert captured["url"] == "https://api.telegram.org/botTOKEN/sendMessage"


def test_main_missing_env_vars_swallows_and_exits_zero(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    # main() calls load_dotenv() itself -- on a dev machine with a real
    # (gitignored) .env populated (post Checkpoint A), that would silently
    # re-populate the vars we just deleted. Neutralize it so this test
    # exercises the "no credentials anywhere" path regardless of local .env
    # state.
    monkeypatch.setattr(notify_failure, "load_dotenv", lambda path=None: 0)

    exit_code = notify_failure.main(["fetch_news: all feeds down"])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "fetch_news: all feeds down" in err


def test_main_send_failure_is_swallowed_and_still_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")

    def _raise(*args, **kwargs):
        raise RuntimeError("network is down")

    monkeypatch.setattr(notify_failure, "send_plain_text", _raise)

    exit_code = notify_failure.main(["build_and_send: some stage failed"])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "network is down" in err
    assert "build_and_send: some stage failed" in err


def test_main_success_calls_send_plain_text_with_prefixed_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")

    calls = []
    monkeypatch.setattr(
        notify_failure, "send_plain_text",
        lambda token, chat_id, text: calls.append((token, chat_id, text)),
    )

    exit_code = notify_failure.main(["update_state: push failed"])

    assert exit_code == 0
    assert calls == [("TOKEN", "CHAT", "[ai-news alert] update_state: push failed")]


def test_main_no_args_exits_zero_without_sending(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("send_plain_text should not be called with no message")

    monkeypatch.setattr(notify_failure, "send_plain_text", _should_not_be_called)

    assert notify_failure.main([]) == 0
