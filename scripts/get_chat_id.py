"""Checkpoint A helper: reads TELEGRAM_BOT_TOKEN, calls getUpdates, and
prints every distinct chat id + a human-readable name seen so far.

Usage: python scripts/get_chat_id.py

The user runs this once, after creating the bot via @BotFather and sending
it /start (or any message), to discover the chat_id to store as
TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import requests

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 15

# Update payload keys that carry a "chat" object, per the Bot API's Update type.
_CHAT_BEARING_KEYS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "my_chat_member",
)


def _chat_display_name(chat: dict) -> str:
    if chat.get("title"):
        return chat["title"]
    name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
    if name:
        return name
    if chat.get("username"):
        return f"@{chat['username']}"
    return "(unnamed chat)"


def get_distinct_chats(updates: list[dict]) -> dict[int, str]:
    """Extract {chat_id: display_name} for every distinct chat seen across
    a getUpdates payload. Later updates for an already-seen chat_id
    overwrite the name (harmless -- chat identity, not the name, is what
    matters; names rarely change mid-history anyway)."""
    chats: dict[int, str] = {}
    for update in updates:
        for key in _CHAT_BEARING_KEYS:
            container = update.get(key)
            if not container:
                continue
            chat = container.get("chat")
            if chat and "id" in chat:
                chats[chat["id"]] = _chat_display_name(chat)
    return chats


def fetch_updates(token: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> list[dict]:
    url = f"{API_BASE}/bot{token}/getUpdates"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {data.get('description', data)}")
    return data.get("result", [])


def main(argv: Optional[list[str]] = None) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("get_chat_id: TELEGRAM_BOT_TOKEN environment variable is not set", file=sys.stderr)
        return 1

    try:
        updates = fetch_updates(token)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"get_chat_id: failed to call getUpdates: {exc}", file=sys.stderr)
        return 1

    chats = get_distinct_chats(updates)
    if not chats:
        print("No chats found yet. Send /start (or any message) to your bot, then re-run this script.")
        return 0

    print("Distinct chat ids seen:")
    for chat_id, name in chats.items():
        print(f"  {chat_id}\t{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
