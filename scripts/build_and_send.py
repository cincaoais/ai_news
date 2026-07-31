"""Step 3 entry point: validate the model's selection.json against
candidates.json, build the HTML digest, and send it to Telegram (or print
it under --dry-run).

Usage: python scripts/build_and_send.py [--dry-run] [--force]

Design laws this enforces (docs/02-PLAN.md):
- The model never types a URL -- selection.json references candidates by
  INDEX only; this script attaches the real URL from candidates.json.
- Same-day guard: aborts a second real send for the same profile on the
  same MYT calendar day unless --force is passed.
- On a real send, writes work/sent.json ({message_ids, sent_at}) as the
  signal update_state.py requires before it will touch state/.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib import dates, state, telegram

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
STATE_DIR = REPO_ROOT / "state"

MAX_SUMMARY_CHARS = 300
URL_SUBSTRINGS = ("http://", "https://", "www.")

# Preferred display order + short labels for known market_snapshot symbols
# (see config/profiles/finance_metals.yaml); anything else falls back to
# its raw symbol string, appended after the known ones.
_MARKET_ORDER = ["^GSPC", "^IXIC", "GC=F", "USDMYR=X"]
_MARKET_LABELS = {"^GSPC": "S&P", "^IXIC": "Nasdaq", "GC=F": "Gold", "USDMYR=X": "USD/MYR"}
_MARKET_PCT_SYMBOLS = {"^GSPC", "^IXIC", "GC=F"}  # USDMYR shown as a plain rate, no %


class ValidationError(ValueError):
    """Raised when selection.json fails validation against candidates.json."""


class SameDayGuardError(RuntimeError):
    """Raised when a digest for this profile was already sent today (MYT)."""


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _contains_url(text: str) -> bool:
    lowered = text.lower()
    return any(sub in lowered for sub in URL_SUBSTRINGS)


def validate_selection(selection: dict, candidates: dict) -> list[dict]:
    """Validate selection.json against candidates.json; return the
    validated items (with the REAL title/url/source attached from
    candidates -- the model itself never supplies a URL).

    Raises ValidationError naming the exact problem on any failure:
    unknown index, empty/oversized summary, or an http(s)/www substring
    smuggled into a model-authored text field (summary or category).
    """
    items = selection.get("items") if isinstance(selection, dict) else None
    if not isinstance(items, list) or not items:
        raise ValidationError("selection.json has no non-empty 'items' list")

    candidate_profile = selection.get("profile") if isinstance(selection, dict) else None
    if candidate_profile and candidate_profile != candidates.get("profile"):
        raise ValidationError(
            f"selection.json profile {candidate_profile!r} does not match "
            f"candidates.json profile {candidates.get('profile')!r}"
        )

    candidates_by_index = {c["index"]: c for c in candidates.get("items", [])}

    validated: list[dict] = []
    for entry in items:
        index = entry.get("index") if isinstance(entry, dict) else None
        if index not in candidates_by_index:
            raise ValidationError(f"selection references unknown candidate index {index!r}")

        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValidationError(f"item at index {index} has an empty summary")
        summary = summary.strip()
        if len(summary) > MAX_SUMMARY_CHARS:
            raise ValidationError(
                f"item at index {index} summary is {len(summary)} chars (max {MAX_SUMMARY_CHARS})"
            )
        if _contains_url(summary):
            raise ValidationError(
                f"item at index {index} summary contains a URL -- the model must never author URLs"
            )

        category = entry.get("category")
        if category is not None:
            if not isinstance(category, str):
                raise ValidationError(f"item at index {index} category must be a string")
            if _contains_url(category):
                raise ValidationError(
                    f"item at index {index} category contains a URL -- the model must never author URLs"
                )

        candidate = candidates_by_index[index]
        validated.append({
            "index": index,
            "title": candidate["title"],
            "url": candidate["url"],
            "source": candidate.get("source"),
            "summary": summary,
            "category": category,
        })

    return validated


# ---------------------------------------------------------------------------
# Digest formatting
# ---------------------------------------------------------------------------

def format_market_line(market: Optional[dict]) -> Optional[str]:
    """Build the optional market snapshot line, e.g.
    'S&P 7437.63 +1.66% · Nasdaq 25122.18 +2.78% · Gold 4133.40 −0.65% · USD/MYR 4.089'.
    Returns None if there is no market block (profile doesn't enable one,
    or every symbol failed to fetch)."""
    if not market:
        return None

    symbols = [s for s in _MARKET_ORDER if s in market]
    symbols += [s for s in market if s not in _MARKET_ORDER]

    parts = []
    for symbol in symbols:
        quote = market[symbol]
        price = quote.get("price")
        if price is None:
            continue
        label = _MARKET_LABELS.get(symbol, symbol)

        if symbol == "USDMYR=X":
            parts.append(f"{label} {price:.3f}")
            continue

        price_str = f"{price:.2f}"
        change_pct = quote.get("change_pct")
        if symbol in _MARKET_PCT_SYMBOLS and change_pct is not None:
            sign = "+" if change_pct >= 0 else "-"  # plain ASCII hyphen (Windows-console-safe)
            parts.append(f"{label} {price_str} {sign}{abs(change_pct):.2f}%")
        else:
            parts.append(f"{label} {price_str}")

    return " · ".join(parts) if parts else None


def build_header_block(candidates: dict, today: str) -> str:
    profile = candidates.get("profile", "unknown")
    lines = [f"<b>{telegram.escape_html(today)} · {telegram.escape_html(profile)}</b>"]

    market_line = format_market_line(candidates.get("market"))
    if market_line:
        lines.append(telegram.escape_html(market_line))

    if candidates.get("thin_news"):
        lines.append("<i>Quiet news day.</i>")

    return "\n".join(lines)


def build_item_block(item: dict) -> str:
    title = telegram.escape_html(item["title"])
    url = telegram.escape_html(item["url"])
    summary = telegram.escape_html(item["summary"])
    return f'<b><a href="{url}">{title}</a></b>\n{summary}'


def build_digest_blocks(candidates: dict, validated_items: list[dict], today: str) -> list[str]:
    blocks = [build_header_block(candidates, today)]
    blocks.extend(build_item_block(item) for item in validated_items)
    return blocks


# ---------------------------------------------------------------------------
# Same-day guard
# ---------------------------------------------------------------------------

def check_same_day_guard(last_digest_path: Path, profile: str, today: str, force: bool) -> None:
    """Abort a real send if state/last_digest.md shows this profile already
    sent a digest today (MYT), unless --force. Prevents duplicate digests
    from overlapping schedulers or a manual re-run."""
    if force:
        return

    meta = state.read_last_digest_front_matter(last_digest_path)
    if meta and str(meta.get("date")) == today and meta.get("profile") == profile:
        raise SameDayGuardError(
            f"a digest for profile={profile!r} was already sent today ({today} MYT); "
            f"pass --force to send again"
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    *,
    dry_run: bool = False,
    force: bool = False,
    work_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    work_dir = work_dir or WORK_DIR
    state_dir = state_dir or STATE_DIR

    candidates = load_json(work_dir / "candidates.json")
    selection = load_json(work_dir / "selection.json")

    validated_items = validate_selection(selection, candidates)

    today = dates.today_myt(now)
    profile = candidates.get("profile", "unknown")

    if not dry_run:
        check_same_day_guard(state_dir / "last_digest.md", profile, today, force)

    blocks = build_digest_blocks(candidates, validated_items, today)
    messages = telegram.split_into_messages(blocks)

    if dry_run:
        return {"messages": messages, "dry_run": True}

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the environment "
            "for a real send (use --dry-run to preview without them)"
        )

    message_ids = []
    for message_text in messages:
        result = telegram.send_message(token, chat_id, message_text)
        message_ids.append(result.get("message_id"))

    sent_payload = {
        "message_ids": message_ids,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    sent_path = work_dir / "sent.json"
    with sent_path.open("w", encoding="utf-8") as f:
        json.dump(sent_payload, f, indent=2)
        f.write("\n")

    return {"messages": messages, "message_ids": message_ids, "dry_run": False}


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Windows consoles default stdout to the system codepage (e.g. cp1252),
    # which cannot encode plenty of characters real news headlines/snippets
    # routinely contain. --dry-run prints digest text straight to stdout,
    # so force UTF-8 there explicitly (matches this project's "UTF-8
    # explicit" style) rather than let an unrelated character crash the CLI.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Validate selection.json, build the HTML digest, and send it to Telegram."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the HTML messages instead of sending them")
    parser.add_argument(
        "--force", action="store_true",
        help="Send even if a digest for this profile was already sent today (MYT)",
    )
    args = parser.parse_args(argv)

    try:
        result = run(dry_run=args.dry_run, force=args.force)
    except (ValidationError, SameDayGuardError, telegram.TelegramError, RuntimeError, OSError) as exc:
        print(f"build_and_send: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        total = len(result["messages"])
        for i, message in enumerate(result["messages"], start=1):
            print(f"--- message {i}/{total} ---")
            print(message)
            print()
    else:
        logger.info("sent %d message(s), message_ids=%s", len(result["message_ids"]), result["message_ids"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
