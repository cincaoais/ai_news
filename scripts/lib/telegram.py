"""Telegram Bot API client: HTML escaping, block-based message splitting,
and sendMessage with the plan's retry policy (docs/01-RND.md Theme E).

HTML parse_mode needs only 3 characters escaped (`< > &`) vs 20+ for
MarkdownV2, which is why the plan chose it for LLM-adjacent text. This
module never authors URLs or headlines itself -- callers (build_and_send.py)
own escaping every dynamic string before handing it a block.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 3500  # plan-mandated margin under Telegram's 4096 UTF-16 limit
REQUEST_TIMEOUT_SECONDS = 15


class TelegramError(RuntimeError):
    """Raised for any Telegram sendMessage failure that should abort the run."""


def escape_html(text: Optional[str]) -> str:
    """Escape the characters HTML parse_mode requires, PLUS '"' (double
    quote) -- Telegram's HTML parser supports exactly the named entities
    lt/gt/amp/quot, so escaping all four is safe everywhere this function is
    used (titles, summaries, URLs, header), and closes an attribute-breakout
    path: an unescaped '"' inside a URL or title could otherwise terminate
    the `href="..."` attribute early and let attacker-controlled text inject
    new HTML (e.g. a fake `<a href="...">` link) into the rendered message.

    Order matters: '&' MUST be escaped first. Escaping '<'/'>'/'"' first
    would turn them into "&lt;"/"&gt;"/"&quot;", and THEN escaping '&' would
    double-escape the '&' those replacements just introduced.
    """
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def redact_token(text: str, token: str) -> str:
    """Replace every occurrence of `token` in `text` with '***TOKEN***'.

    No-op for an empty/falsy token (nothing to redact). Used to keep the bot
    token out of any raised/printed/logged message that interpolates a
    requests exception or a Telegram API response -- both can echo back the
    request URL, which embeds the token as `.../bot<token>/sendMessage`.
    """
    if not token:
        return text
    return text.replace(token, "***TOKEN***")


def _utf16_len(s: str) -> int:
    """Telegram counts message length in UTF-16 code units, not Python
    codepoints -- characters outside the BMP are 2 code units there."""
    return len(s.encode("utf-16-le")) // 2


def split_into_messages(blocks: list[str], max_chars: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Greedily pack self-contained HTML blocks into messages <= max_chars
    (UTF-16 code units), joined by a blank line, NEVER splitting inside a
    block (which would risk cutting an HTML tag in half).

    Each element of `blocks` must already be one complete, independently
    valid chunk of HTML (e.g. one news item's own <b><a>...</a></b> +
    summary line). If a single block itself exceeds max_chars, it is still
    emitted whole (never split) as its own message -- the "never split
    mid-block/mid-tag" rule outranks the soft 3500 target; the hard
    4096-char Telegram limit is expected never to be hit in practice since
    validate_selection() caps summaries at 300 chars.
    """
    if not blocks:
        return []

    messages: list[str] = []
    current: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = _utf16_len(block)
        added_len = block_len if not current else block_len + 2  # +2 for the "\n\n" join
        if current and current_len + added_len > max_chars:
            messages.append("\n\n".join(current))
            current = [block]
            current_len = block_len
        else:
            current.append(block)
            current_len += added_len

    if current:
        messages.append("\n\n".join(current))

    return messages


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    max_attempts_5xx: int = 2,
    session=None,
) -> dict:
    """POST one sendMessage call (HTML parse_mode, link previews disabled)
    with the plan's retry policy:

    - 429 (rate limited): sleep `retry_after + 1` seconds, retry ONCE.
    - 5xx / network error: exponential backoff, up to `max_attempts_5xx`
      attempts total (default 2 -- i.e. one retry).
    - any other 4xx: raise immediately with the API's `description` --
      malformed HTML or a blocked bot won't fix themselves by retrying.

    Returns the parsed `result` object from a successful response.
    """
    url = f"{API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    http = session or requests

    attempt = 0
    retried_429 = False
    while True:
        attempt += 1
        try:
            resp = http.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            # A requests exception's own str() often embeds the request URL
            # (which contains the bot token, e.g. ".../bot<token>/sendMessage")
            # -- redact before this ever reaches a log line or a raised
            # message that a caller might print/relay (e.g. notify_failure.py).
            safe_exc = redact_token(str(exc), token)
            if attempt < max_attempts_5xx:
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    "network error sending to Telegram (attempt %d/%d): %s; retrying in %ds",
                    attempt, max_attempts_5xx, safe_exc, backoff,
                )
                time.sleep(backoff)
                continue
            raise TelegramError(
                f"network error sending to Telegram after {attempt} attempt(s): {safe_exc}"
            ) from exc

        try:
            data = resp.json()
        except ValueError:
            data = {}

        if resp.status_code == 200 and data.get("ok"):
            return data.get("result", {})

        # redact here too -- resp.text (the fallback when the API gives no
        # `description`) could in principle echo request details back.
        description = redact_token(str(data.get("description", resp.text)), token)

        if resp.status_code == 429 and not retried_429:
            retry_after = (data.get("parameters") or {}).get("retry_after", 1)
            sleep_for = retry_after + 1
            logger.warning("Telegram rate-limited (429); sleeping %ds then retrying once", sleep_for)
            time.sleep(sleep_for)
            retried_429 = True
            continue

        if resp.status_code >= 500 and attempt < max_attempts_5xx:
            backoff = 2 ** (attempt - 1)
            logger.warning(
                "Telegram 5xx (attempt %d/%d): %s; retrying in %ds",
                attempt, max_attempts_5xx, description, backoff,
            )
            time.sleep(backoff)
            continue

        raise TelegramError(f"Telegram API error {resp.status_code}: {description}")
