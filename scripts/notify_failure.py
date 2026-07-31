"""Failure notifier: sends a PLAIN TEXT (no parse_mode) Telegram alert so
the user is told a pipeline stage failed, rather than the digest silently
not arriving.

Usage: python scripts/notify_failure.py "stage: message"

Deliberately plain text, not HTML: this is the last-resort alert path, and
must stay immune to whatever malformed content caused the ORIGINAL failure
(e.g. if the digest formatter itself is what broke, the notifier cannot
also depend on well-formed HTML). It swallows its own failures (logged to
stderr) and always exits 0 -- a notifier hiccup must never mask or shadow
the original failure's exit code in whatever caller invoked it.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

import requests

from lib.env import load_dotenv
from lib.telegram import redact_token

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 15


def send_plain_text(token: str, chat_id: str, text: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> None:
    """POST one plain-text sendMessage call. No parse_mode, no retry policy
    -- this is the simplest possible path, on purpose. Raises on any
    failure; main() is responsible for swallowing it."""
    url = f"{API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {data.get('description', data)}")


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    argv = sys.argv[1:] if argv is None else argv

    if not argv:
        print('notify_failure: usage: python scripts/notify_failure.py "stage: message"', file=sys.stderr)
        return 0  # never mask the caller's own already-failed exit code

    message = argv[0]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("notify_failure: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- cannot deliver alert", file=sys.stderr)
        print(f"notify_failure: original message was: {message}", file=sys.stderr)
        return 0

    try:
        send_plain_text(token, chat_id, f"[ai-news alert] {message}")
    except Exception as exc:  # noqa: BLE001 -- intentional: never let a
        # notifier failure mask the original failure that triggered it.
        # redact_token: `exc` may be a requests exception whose str() embeds
        # the request URL (".../bot<token>/sendMessage") -- this is the
        # last-resort failure path, so it must never leak the token to
        # stderr/CI logs even here.
        safe_exc = redact_token(str(exc), token)
        print(f"notify_failure: failed to deliver alert ({safe_exc}); original message was: {message}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
