"""Tests for lib/telegram.py: HTML escaping, block-based splitting (never
mid-block/mid-tag), and sendMessage's retry policy (mocked HTTP, no
network)."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
import requests

import lib.telegram as telegram_mod
from lib import telegram
from conftest import FakeResponse


# ---------------------------------------------------------------------------
# escape_html
# ---------------------------------------------------------------------------

def test_escape_html_basic_chars():
    assert telegram.escape_html("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_escape_html_empty_and_none():
    assert telegram.escape_html("") == ""
    assert telegram.escape_html(None) == ""


def test_escape_html_ampersand_escaped_first_no_double_escape():
    # '&' must be escaped BEFORE '<'/'>'. If it were escaped after, the
    # '&' introduced by escaping '<' into '&lt;' would get re-escaped into
    # '&amp;lt;' -- this asserts that does NOT happen.
    assert telegram.escape_html("<script>") == "&lt;script&gt;"


def test_escape_html_literal_ampersand_in_already_entity_like_text():
    # Adversarial input: text that merely LOOKS like an HTML entity but is
    # just literal characters (no real '<' or '>'). Only the literal '&'
    # gets escaped; nothing about '<'/'>' handling applies since none exist.
    text = "&lt;already escaped&gt;"
    assert telegram.escape_html(text) == "&amp;lt;already escaped&amp;gt;"


def test_escape_html_nested_tag_attempt_neutralized():
    text = 'click <a href="evil">here</a> & <b>bold</b>'
    escaped = telegram.escape_html(text)
    assert "<a" not in escaped
    assert "<b>" not in escaped
    assert "</a>" not in escaped
    assert escaped.count("&amp;") == 1  # only the one literal '&' in the input


# ---------------------------------------------------------------------------
# escape_html: F1 -- quote escaping (attribute-breakout prevention)
# ---------------------------------------------------------------------------

def test_escape_html_quotes_escaped():
    assert telegram.escape_html('a "quoted" string') == "a &quot;quoted&quot; string"


def test_escape_html_no_raw_quote_remains():
    assert '"' not in telegram.escape_html('a "quoted" string')


def test_escape_html_ampersand_still_escaped_first_with_quotes_present():
    # '&' must still be escaped before '"' is introduced, same reasoning as
    # the existing '<'/'>' ordering test -- otherwise the '&' from
    # '&quot;' would get double-escaped into '&amp;quot;'.
    assert telegram.escape_html('&"') == "&amp;&quot;"


def test_escape_html_url_attribute_breakout_attempt_fully_inert():
    # A URL crafted to close an href="..." attribute early and inject a
    # brand-new <a> tag (a classic HTML-in-attribute injection). After
    # escaping, no raw '"' or '<'/'>' may survive at all.
    breakout = 'https://evil.example/"><a href="https://phish.example">x</a>'
    escaped = telegram.escape_html(breakout)
    assert '"' not in escaped
    assert "<" not in escaped
    assert ">" not in escaped
    assert "&quot;" in escaped


def test_build_item_block_neutralizes_quote_breakout_in_url_and_summary():
    # End-to-end through build_and_send.py's build_item_block: the only two
    # raw '"' characters allowed in the rendered block are the href="..."
    # attribute delimiters build_item_block itself writes -- none may come
    # from attacker-controlled title/url/summary text.
    import build_and_send as bas

    item = {
        "index": 0,
        "title": 'Normal Title with "quotes"',
        "url": 'https://evil.example/"><a href="https://phish.example">x</a>',
        "source": "Src",
        "summary": 'Summary with "quotes" inside it',
        "category": None,
    }
    block = bas.build_item_block(item)
    # Exactly the two href="..." delimiters build_item_block itself writes
    # -- none surviving from the attacker-controlled title/url/summary.
    assert block.count('"') == 2
    assert block.count("<a href=") == 1  # only the one legitimate tag
    assert '<a href="https://phish.example">' not in block  # the injected tag never materializes


# ---------------------------------------------------------------------------
# redact_token (F2)
# ---------------------------------------------------------------------------

def test_redact_token_replaces_all_occurrences():
    text = "error at bot12345:ABC/sendMessage and again bot12345:ABC/x"
    result = telegram.redact_token(text, "12345:ABC")
    assert "12345:ABC" not in result
    assert result.count("***TOKEN***") == 2


def test_redact_token_empty_token_is_noop():
    assert telegram.redact_token("some text", "") == "some text"
    assert telegram.redact_token("some text", None) == "some text"


def test_redact_token_no_occurrence_is_unchanged():
    assert telegram.redact_token("nothing to see here", "SECRET") == "nothing to see here"


# ---------------------------------------------------------------------------
# split_into_messages
# ---------------------------------------------------------------------------

class _TagBalanceChecker(HTMLParser):
    """Proves a chunk's HTML tags are all properly opened+closed within it
    -- i.e. splitting never cut a block (or a tag) in half."""

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack, f"unexpected closing tag </{tag}> with nothing open"
        assert self.stack[-1] == tag, f"mismatched closing tag: expected </{self.stack[-1]}>, got </{tag}>"
        self.stack.pop()

    def assert_balanced(self):
        assert self.stack == [], f"unclosed tag(s) at chunk end: {self.stack}"


def _assert_valid_html_chunk(chunk: str) -> None:
    checker = _TagBalanceChecker()
    checker.feed(chunk)
    checker.close()
    checker.assert_balanced()


def _item_block(i: int, summary_len: int = 60) -> str:
    return f'<b><a href="https://example.com/{i}">Headline number {i}</a></b>\n{"x" * summary_len}'


def test_split_empty_list_returns_empty():
    assert telegram.split_into_messages([]) == []


def test_split_single_item_returns_one_message_unchanged():
    blocks = [_item_block(0)]
    messages = telegram.split_into_messages(blocks)
    assert messages == [blocks[0]]
    _assert_valid_html_chunk(messages[0])


def test_split_twelve_items_packs_greedily_without_mid_block_split():
    blocks = [_item_block(i, summary_len=300) for i in range(12)]
    messages = telegram.split_into_messages(blocks)

    assert len(messages) > 1  # 12 * ~350 chars is well over one 3500-char message
    for message in messages:
        assert telegram._utf16_len(message) <= telegram.MAX_MESSAGE_CHARS
        _assert_valid_html_chunk(message)

    # every block appears whole and unmodified somewhere in the output
    rejoined = "\n\n".join(messages)
    for block in blocks:
        assert block in rejoined


def test_split_giant_block_gets_isolated_in_its_own_message():
    # Big relative to the others, but still comfortably under the hard
    # 4096-char Telegram limit -- forces the greedy packer to isolate it
    # rather than try to merge it with neighbours.
    giant = _item_block(0, summary_len=3400)
    normal = [_item_block(i, summary_len=60) for i in range(1, 4)]
    blocks = [giant] + normal

    messages = telegram.split_into_messages(blocks)

    giant_messages = [m for m in messages if giant in m]
    assert len(giant_messages) == 1
    assert giant_messages[0] == giant  # nothing else packed alongside it

    for message in messages:
        assert telegram._utf16_len(message) <= telegram.MAX_MESSAGE_CHARS
        _assert_valid_html_chunk(message)


def test_split_never_splits_mid_tag_across_many_boundary_shapes():
    # Deliberately awkward sizes so blank-line packing boundaries land at
    # different offsets relative to each block's own tags.
    blocks = [_item_block(i, summary_len=length) for i, length in enumerate([3400, 50, 3450, 3400, 10])]
    messages = telegram.split_into_messages(blocks)
    for message in messages:
        _assert_valid_html_chunk(message)


# ---------------------------------------------------------------------------
# send_message retry policy
# ---------------------------------------------------------------------------

class _FakeSession:
    """Replays a canned sequence of responses/exceptions for requests.post."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok_response(message_id=123):
    return FakeResponse(json_data={"ok": True, "result": {"message_id": message_id}}, status_code=200)


def _error_response(status_code, description, retry_after=None):
    data = {"ok": False, "description": description}
    if retry_after is not None:
        data["parameters"] = {"retry_after": retry_after}
    return FakeResponse(json_data=data, status_code=status_code)


def test_send_message_success_first_try(monkeypatch):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    session = _FakeSession([_ok_response(message_id=42)])
    result = telegram.send_message("TOKEN", "CHAT", "hello", session=session)
    assert result == {"message_id": 42}
    assert session.calls == 1


def test_send_message_429_retries_once_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: sleeps.append(s))
    session = _FakeSession([_error_response(429, "Too Many Requests", retry_after=3), _ok_response()])

    result = telegram.send_message("TOKEN", "CHAT", "hello", session=session)

    assert result == {"message_id": 123}
    assert session.calls == 2
    assert sleeps == [4]  # retry_after(3) + 1


def test_send_message_429_twice_raises_after_exactly_one_retry(monkeypatch):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: None)
    session = _FakeSession([
        _error_response(429, "Too Many Requests", retry_after=1),
        _error_response(429, "Too Many Requests", retry_after=1),
    ])

    with pytest.raises(telegram.TelegramError):
        telegram.send_message("TOKEN", "CHAT", "hello", session=session)
    assert session.calls == 2  # exactly one retry for 429, never a second


def test_send_message_5xx_backoff_then_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: sleeps.append(s))
    session = _FakeSession([_error_response(503, "Service Unavailable"), _ok_response()])

    result = telegram.send_message("TOKEN", "CHAT", "hello", session=session)

    assert result == {"message_id": 123}
    assert session.calls == 2
    assert sleeps == [1]


def test_send_message_5xx_exhausts_two_attempts_then_raises(monkeypatch):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: None)
    session = _FakeSession([
        _error_response(500, "Internal Server Error"),
        _error_response(500, "Internal Server Error"),
    ])

    with pytest.raises(telegram.TelegramError, match="Internal Server Error"):
        telegram.send_message("TOKEN", "CHAT", "hello", session=session)
    assert session.calls == 2  # default max_attempts_5xx=2 -> no third attempt


def test_send_message_network_error_backoff_then_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: sleeps.append(s))
    session = _FakeSession([requests.ConnectionError("boom"), _ok_response()])

    result = telegram.send_message("TOKEN", "CHAT", "hello", session=session)

    assert result == {"message_id": 123}
    assert session.calls == 2
    assert sleeps == [1]


def test_send_message_network_error_exhausts_attempts_then_raises(monkeypatch):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: None)
    session = _FakeSession([requests.ConnectionError("boom"), requests.ConnectionError("boom again")])

    with pytest.raises(telegram.TelegramError, match="boom again"):
        telegram.send_message("TOKEN", "CHAT", "hello", session=session)
    assert session.calls == 2


def test_send_message_other_4xx_raises_immediately_with_api_description(monkeypatch):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    session = _FakeSession([_error_response(400, "Bad Request: can't parse entities")])

    with pytest.raises(telegram.TelegramError, match="can't parse entities"):
        telegram.send_message("TOKEN", "CHAT", "hello", session=session)
    assert session.calls == 1  # no retry at all for a non-429/5xx 4xx


def test_send_message_network_error_redacts_token_in_raised_message(monkeypatch):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: None)
    token = "123456:SECRETTOKEN"
    exc = requests.ConnectionError(
        f"Failed to establish a new connection: HTTPSConnectionPool(host='api.telegram.org') "
        f"bot{token}/sendMessage"
    )
    session = _FakeSession([exc, exc])  # exhaust both attempts

    with pytest.raises(telegram.TelegramError) as excinfo:
        telegram.send_message(token, "CHAT", "hello", session=session)

    assert token not in str(excinfo.value)
    assert "***TOKEN***" in str(excinfo.value)


def test_send_message_network_error_redacts_token_in_retry_warning_log(monkeypatch, caplog):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: None)
    token = "123456:SECRETTOKEN"
    exc = requests.ConnectionError(f"connection refused bot{token}/sendMessage")
    session = _FakeSession([exc, _ok_response()])

    with caplog.at_level("WARNING"):
        telegram.send_message(token, "CHAT", "hello", session=session)

    assert token not in caplog.text
    assert "***TOKEN***" in caplog.text


def test_send_message_api_error_redacts_token_in_description(monkeypatch):
    monkeypatch.setattr(telegram_mod.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")))
    token = "123456:SECRETTOKEN"
    session = _FakeSession([_error_response(400, f"Bad Request: bot{token}/sendMessage not found")])

    with pytest.raises(telegram.TelegramError) as excinfo:
        telegram.send_message(token, "CHAT", "hello", session=session)

    assert token not in str(excinfo.value)
    assert "***TOKEN***" in str(excinfo.value)


def test_send_message_sends_html_parse_mode_and_disabled_previews():
    captured = {}

    class _CapturingSession:
        def post(self, url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _ok_response()

    telegram.send_message("TOKEN", "CHAT123", "hi", session=_CapturingSession())

    assert captured["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert captured["json"]["chat_id"] == "CHAT123"
    assert captured["json"]["parse_mode"] == "HTML"
    assert captured["json"]["link_preview_options"] == {"is_disabled": True}
