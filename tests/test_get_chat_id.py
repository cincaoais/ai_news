"""Tests for scripts/get_chat_id.py: chat extraction from a fixture
getUpdates payload, and the CLI's env-var / no-chats-yet paths."""

from __future__ import annotations

import get_chat_id


def _fixture_updates() -> list[dict]:
    return [
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 111, "type": "private", "first_name": "Jo", "last_name": "Han"},
                "text": "/start",
            },
        },
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "chat": {"id": 111, "type": "private", "first_name": "Jo", "last_name": "Han"},
                "text": "hi again",
            },
        },
        {
            "update_id": 3,
            "channel_post": {
                "message_id": 1,
                "chat": {"id": -1009, "type": "channel", "title": "News Channel"},
            },
        },
    ]


def test_get_distinct_chats_dedupes_repeats_and_names_them():
    chats = get_chat_id.get_distinct_chats(_fixture_updates())
    assert chats == {111: "Jo Han", -1009: "News Channel"}


def test_get_distinct_chats_empty_updates_returns_empty():
    assert get_chat_id.get_distinct_chats([]) == {}


def test_get_distinct_chats_username_fallback_when_no_name():
    updates = [{"message": {"chat": {"id": 5, "type": "private", "username": "someone"}}}]
    assert get_chat_id.get_distinct_chats(updates) == {5: "@someone"}


def test_get_distinct_chats_unnamed_fallback_when_nothing_available():
    updates = [{"message": {"chat": {"id": 5, "type": "group"}}}]
    assert get_chat_id.get_distinct_chats(updates) == {5: "(unnamed chat)"}


def test_get_distinct_chats_ignores_updates_without_a_chat_bearing_key():
    updates = [{"update_id": 1, "poll": {"id": "abc"}}]
    assert get_chat_id.get_distinct_chats(updates) == {}


def test_main_prints_distinct_chat_ids(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(get_chat_id, "fetch_updates", lambda token, timeout=15: _fixture_updates())

    exit_code = get_chat_id.main([])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "111" in out
    assert "Jo Han" in out
    assert "-1009" in out
    assert "News Channel" in out


def test_main_no_chats_yet_prints_helpful_hint(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(get_chat_id, "fetch_updates", lambda token, timeout=15: [])

    exit_code = get_chat_id.main([])

    assert exit_code == 0
    assert "/start" in capsys.readouterr().out


def test_main_missing_token_errors_without_calling_the_api(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def _should_not_be_called(token, timeout=15):
        raise AssertionError("fetch_updates should not be called without a token")

    monkeypatch.setattr(get_chat_id, "fetch_updates", _should_not_be_called)

    exit_code = get_chat_id.main([])

    assert exit_code == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
