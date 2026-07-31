"""Tests for scripts/lib/env.py (.env loader)."""

from __future__ import annotations

import os

from lib.env import load_dotenv


def _clean(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_loads_basic_pairs(tmp_path, monkeypatch):
    _clean(monkeypatch, "EV_A", "EV_B")
    env_file = tmp_path / ".env"
    env_file.write_text("EV_A=hello\nEV_B = world \n", encoding="utf-8")
    assert load_dotenv(env_file) == 2
    assert os.environ["EV_A"] == "hello"
    assert os.environ["EV_B"] == "world"
    _clean(monkeypatch, "EV_A", "EV_B")


def test_strips_matching_quotes(tmp_path, monkeypatch):
    _clean(monkeypatch, "EV_Q1", "EV_Q2")
    env_file = tmp_path / ".env"
    env_file.write_text("EV_Q1=\"quoted value\"\nEV_Q2='single'\n", encoding="utf-8")
    load_dotenv(env_file)
    assert os.environ["EV_Q1"] == "quoted value"
    assert os.environ["EV_Q2"] == "single"
    _clean(monkeypatch, "EV_Q1", "EV_Q2")


def test_ignores_comments_blanks_and_junk(tmp_path, monkeypatch):
    _clean(monkeypatch, "EV_C")
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nnot a pair line\nEV_C=1\n", encoding="utf-8")
    assert load_dotenv(env_file) == 1
    assert os.environ["EV_C"] == "1"
    _clean(monkeypatch, "EV_C")


def test_never_overrides_real_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EV_REAL", "from-real-env")
    env_file = tmp_path / ".env"
    env_file.write_text("EV_REAL=from-dotenv\n", encoding="utf-8")
    assert load_dotenv(env_file) == 0
    assert os.environ["EV_REAL"] == "from-real-env"


def test_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "does-not-exist.env") == 0


def test_unfilled_placeholders_are_skipped(tmp_path, monkeypatch):
    _clean(monkeypatch, "TELEGRAM_BOT_TOKEN_TESTVAL", "EV_FILLED")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN_TESTVAL=PASTE_YOUR_BOT_TOKEN_HERE\nEV_FILLED=real\n",
        encoding="utf-8",
    )
    assert load_dotenv(env_file) == 1
    assert "TELEGRAM_BOT_TOKEN_TESTVAL" not in os.environ
    assert os.environ["EV_FILLED"] == "real"
    _clean(monkeypatch, "EV_FILLED")
