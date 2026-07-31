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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib import dates, state, telegram
from lib.env import load_dotenv

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
STATE_DIR = REPO_ROOT / "state"

MAX_SUMMARY_CHARS = 300
URL_SUBSTRINGS = ("http://", "https://", "www.")

# Fallback bounds when candidates.json predates fetch_news.py's "limits"
# field (F4) or omits it -- matches config.yaml's own defaults.
DEFAULT_MIN_ITEMS = 5
DEFAULT_MAX_ITEMS = 8

GIT_SUBPROCESS_TIMEOUT_SECONDS = 30

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
    # utf-8-sig: tolerates (and strips) a leading UTF-8 BOM -- some editors
    # / tools write one, and a stray BOM before "{" would otherwise make
    # json.load() raise on an otherwise-perfectly-valid file.
    with path.open("r", encoding="utf-8-sig") as f:
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

    # F4: enforce the item-count bounds fetch_news.py embeds in
    # candidates.json's `limits` field (config.yaml's min_items/max_items --
    # max_items was dead until this wired it in). Falls back to this
    # module's own defaults for a candidates.json that predates/omits the
    # field.
    limits = candidates.get("limits") if isinstance(candidates, dict) else None
    limits = limits if isinstance(limits, dict) else {}
    min_items = limits.get("min_items", DEFAULT_MIN_ITEMS)
    max_items = limits.get("max_items", DEFAULT_MAX_ITEMS)

    count = len(validated)
    if count > max_items:
        raise ValidationError(
            f"selection.json has {count} items, more than the allowed max of {max_items}"
        )

    total_candidates = len(candidates.get("items", []))
    thin_news = bool(candidates.get("thin_news"))
    # CLAUDE.md's carve-out: fewer than min_items is fine when the day is
    # genuinely thin_news, OR when candidates.json itself never had enough
    # candidates to reach min_items in the first place -- neither case is
    # the model skimping on its selection.
    if count < min_items and not thin_news and total_candidates >= min_items:
        raise ValidationError(
            f"selection.json has {count} items, fewer than the allowed min of {min_items} "
            f"(candidates.json has {total_candidates} candidates available and is not thin_news)"
        )

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


def build_feed_warnings_footer(candidates: dict) -> Optional[str]:
    """Optional one-line digest footer noting how many source feeds were
    skipped (stale or dead) during this run's fetch step (Slice 6). Reads
    candidates.json's `feed_warnings` list (see lib/feeds.py's
    `warnings_out` and fetch_news.py's gather_raw_items); returns None when
    every feed was healthy (the common case), so nothing is appended on a
    normal day. Deliberately never echoes the individual warning strings
    (which carry a feed label, not model text) into the footer itself --
    just a count -- so this line is trivially valid, single-line HTML."""
    feed_warnings = candidates.get("feed_warnings") or []
    if not feed_warnings:
        return None
    return f"<i>note: {len(feed_warnings)} source feeds skipped (details in repo logs)</i>"


def build_digest_blocks(candidates: dict, validated_items: list[dict], today: str) -> list[str]:
    blocks = [build_header_block(candidates, today)]
    blocks.extend(build_item_block(item) for item in validated_items)
    footer = build_feed_warnings_footer(candidates)
    if footer:
        blocks.append(footer)
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


def _has_git_remote_origin(repo_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "remote"], cwd=repo_root, capture_output=True, text=True,
            timeout=GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not list git remotes (%s); skipping pre-send origin re-check", exc)
        return False
    return "origin" in result.stdout.split()


def check_origin_same_day_guard(repo_root: Path, profile: str, today: str, force: bool) -> None:
    """Best-effort, last-moment re-check of origin/main's OWN
    state/last_digest.md, run immediately before the first real send of
    this invocation -- on top of (not instead of) check_same_day_guard
    above, which only ever sees this clone's/session's own local copy of
    state/, however stale that is.

    `git fetch origin` then `git show origin/main:state/last_digest.md`
    shrinks that staleness window down to the few seconds this takes, for
    the specific case of two schedulers racing each other. It remains a
    mitigation, not a substitute for the documented rule of running exactly
    one scheduler at a time (see README's "Scheduler operations") -- a
    scheduler that has been running unattended for hours could still race
    past this in principle.

    Never raises for anything other than a genuine hit: no `origin` remote
    configured, `git fetch` failing (offline, auth, etc.), `git show`
    failing (e.g. state/last_digest.md doesn't exist yet on origin/main),
    or any other subprocess weirdness all log a warning and let the send
    proceed -- this check must never be the reason a send that would
    otherwise succeed gets crashed instead.
    """
    if force:
        return

    try:
        if not _has_git_remote_origin(repo_root):
            return

        fetch = subprocess.run(
            ["git", "fetch", "origin"], cwd=repo_root, capture_output=True, text=True,
            timeout=GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
        if fetch.returncode != 0:
            logger.warning(
                "pre-send origin re-check: git fetch origin failed (%s); proceeding without it",
                fetch.stderr.strip(),
            )
            return

        show = subprocess.run(
            ["git", "show", "origin/main:state/last_digest.md"], cwd=repo_root,
            capture_output=True, text=True, timeout=GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
        if show.returncode != 0:
            logger.warning(
                "pre-send origin re-check: could not read origin/main:state/last_digest.md "
                "(%s); proceeding without it", show.stderr.strip(),
            )
            return
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("pre-send origin re-check failed unexpectedly (%s); proceeding without it", exc)
        return

    meta = state.parse_front_matter(show.stdout, source_label="origin/main:state/last_digest.md")
    if meta and str(meta.get("date")) == today and meta.get("profile") == profile:
        raise SameDayGuardError(
            f"origin/main's state/last_digest.md already shows profile={profile!r} sent "
            f"today ({today} MYT); pass --force to send again"
        )


# ---------------------------------------------------------------------------
# Send-progress resume (F5): survives a mid-loop failure (e.g. Telegram
# rejects message 3 of 4) without re-sending messages already delivered.
# ---------------------------------------------------------------------------

def _default_send_progress(today: str, profile: str) -> dict:
    return {"date": today, "profile": profile, "sent_count": 0, "message_ids": []}


def load_send_progress(path: Path, *, today: str, profile: str) -> dict:
    """Load work/sent_progress.json (a checkpoint from a prior, partially
    successful send), if present AND it matches today's {date, profile} --
    otherwise start fresh. A progress file from a different day or profile
    (leftover, or a stale clone) must never be resumed from."""
    if not path.exists():
        return _default_send_progress(today, profile)

    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read %s (%s); starting this send from scratch", path, exc)
        return _default_send_progress(today, profile)

    if not isinstance(data, dict) or data.get("date") != today or data.get("profile") != profile:
        return _default_send_progress(today, profile)

    return {
        "date": today,
        "profile": profile,
        "sent_count": int(data.get("sent_count") or 0),
        "message_ids": list(data.get("message_ids") or []),
    }


def _write_send_progress(path: Path, *, today: str, profile: str, message_ids: list) -> None:
    payload = {"date": today, "profile": profile, "sent_count": len(message_ids), "message_ids": message_ids}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    *,
    dry_run: bool = False,
    force: bool = False,
    work_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    work_dir = work_dir or WORK_DIR
    state_dir = state_dir or STATE_DIR
    repo_root = repo_root or REPO_ROOT

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

    # F6: last-moment re-check against origin/main's own state/, immediately
    # before the first real send of this invocation (see
    # check_origin_same_day_guard's own docstring for what this does and
    # doesn't guarantee).
    check_origin_same_day_guard(repo_root, profile, today, force)

    # F5: resume support. NOTE on why this can never be wrongly blocked by
    # either guard above: update_state.py only ever rewrites
    # state/last_digest.md AFTER a fully successful send -- its own
    # precondition is work/sent.json existing (see
    # update_state.load_sent_items), which this function only writes at the
    # very end, once every message has sent. So a send that fails partway
    # through (an exception raised somewhere in the loop below, before
    # sent.json is written) can never have produced a last_digest.md
    # rewrite for today -- meaning a resume retry's calls to
    # check_same_day_guard/check_origin_same_day_guard above always still
    # see "not sent yet" for this date+profile, exactly as they should.
    progress_path = work_dir / "sent_progress.json"
    progress = load_send_progress(progress_path, today=today, profile=profile)
    resume_from = min(progress["sent_count"], len(messages))
    message_ids: list = list(progress["message_ids"])
    if resume_from:
        logger.info(
            "resuming send: %d of %d message(s) already sent in a prior attempt",
            resume_from, len(messages),
        )

    for message_text in messages[resume_from:]:
        result = telegram.send_message(token, chat_id, message_text)
        message_ids.append(result.get("message_id"))
        # Persist after EVERY successful send, not just at the end -- this
        # is the whole point: if the NEXT message raises, this file already
        # reflects everything sent so far, so a retry resumes instead of
        # re-sending (and duplicating) messages already delivered.
        _write_send_progress(progress_path, today=today, profile=profile, message_ids=message_ids)

    sent_payload = {
        "message_ids": message_ids,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    sent_path = work_dir / "sent.json"
    with sent_path.open("w", encoding="utf-8") as f:
        json.dump(sent_payload, f, indent=2)
        f.write("\n")

    if progress_path.exists():
        progress_path.unlink()

    return {"messages": messages, "message_ids": message_ids, "dry_run": False}


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
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
