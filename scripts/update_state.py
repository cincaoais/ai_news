"""Step 4 entry point: after a successful real send, append the sent items
to state/seen.jsonl, prune entries older than 30 days, rewrite
state/last_digest.md, and commit + push state/ with rebase-retry resilience.

Usage: python scripts/update_state.py

Must run only after build_and_send.py has written work/sent.json (proof a
real send happened) -- otherwise state would record items that were never
actually delivered. On any unrecoverable failure this exits nonzero;
notify_failure.py (a separate step) is the caller's job, not this script's.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path
from typing import Optional

import build_and_send
from lib import dates, state

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = REPO_ROOT / "work"
STATE_DIR = REPO_ROOT / "state"

PRUNE_DAYS = 30

# F7: by the time git_commit_and_push runs, build_and_send.py has ALREADY
# delivered the digest (that's this whole script's precondition -- see
# load_sent_items/work/sent.json below). So any failure past that point is
# never "the digest didn't go out" -- it's "the digest went out fine, but
# tomorrow's dedup bookkeeping is at risk." Every UpdateStateError this
# module raises for a git_commit_and_push failure says so explicitly,
# because run_digest.py routes this straight into notify_failure.py's
# "state:"-tagged Telegram alert -- the user reading that alert needs to
# know immediately that nothing was actually lost, only state.
STATE_PUSH_FAILURE_NOTE = (
    "digest WAS already sent; only the dedup state failed to push -- "
    "tomorrow's digest may repeat today's stories"
)


class UpdateStateError(RuntimeError):
    """Raised for any unrecoverable failure in this step (missing
    work/sent.json, or a git push that fails even after rebase-retry)."""


# The branch state commits land on. The Routine's scheduled cloud env can
# check the repo out at a pinned SHA (detached HEAD, seen live 2026-08-01),
# where a bare `git push` / `git pull --rebase` both die with "You are not
# currently on a branch" -- so every push/pull below names the remote and
# branch explicitly instead of relying on a current branch or upstream.
STATE_BRANCH = "main"


# ---------------------------------------------------------------------------
# Deriving the sent items
# ---------------------------------------------------------------------------

def load_sent_items(work_dir: Path) -> tuple[list[dict], str]:
    """Re-derive the exact sent items (title/url/summary) by re-validating
    selection.json against candidates.json -- guarded by the presence of
    work/sent.json (build_and_send.py's proof a real send actually happened).

    Returns (validated_items, profile).
    """
    sent_path = work_dir / "sent.json"
    if not sent_path.exists():
        raise UpdateStateError(
            f"{sent_path} not found -- run build_and_send.py (without --dry-run) first"
        )

    candidates = build_and_send.load_json(work_dir / "candidates.json")
    selection = build_and_send.load_json(work_dir / "selection.json")
    validated = build_and_send.validate_selection(selection, candidates)
    return validated, candidates.get("profile", "unknown")


# ---------------------------------------------------------------------------
# seen.jsonl append + prune
# ---------------------------------------------------------------------------

def append_seen(seen_path: Path, items: list[dict], *, today: str) -> None:
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    with seen_path.open("a", encoding="utf-8") as f:
        for item in items:
            record = {
                "url": state.canonicalize_url(item["url"]),
                "title": item["title"],
                "date": today,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def prune_seen(seen_path: Path, *, today: str, max_age_days: int = PRUNE_DAYS) -> int:
    """Rewrite seen.jsonl keeping only rows within max_age_days of `today`
    (MYT). Rows with a missing/unparseable date are kept -- fail open, so a
    parse hiccup never silently loses dedup state. Returns rows pruned."""
    if not seen_path.exists():
        return 0

    today_date = date_cls.fromisoformat(today)
    cutoff = today_date - timedelta(days=max_age_days)

    kept: list[str] = []
    pruned = 0
    with seen_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                row_date = date_cls.fromisoformat(record["date"])
            except (json.JSONDecodeError, KeyError, ValueError):
                kept.append(line)
                continue
            if row_date >= cutoff:
                kept.append(line)
            else:
                pruned += 1

    with seen_path.open("w", encoding="utf-8") as f:
        for line in kept:
            f.write(line + "\n")

    return pruned


# ---------------------------------------------------------------------------
# git commit + push (rebase-retry resilience)
# ---------------------------------------------------------------------------

def _has_remote(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "remote"], cwd=repo_root, capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def git_commit_and_push(repo_root: Path, message: str) -> None:
    """Stage state/, commit (no-op if nothing changed), and push HEAD to
    origin's state branch with an explicit refspec (safe from detached HEAD).

    On a rejected push: `git pull --rebase origin <branch>`, then retry the
    push once. If
    there is no remote configured at all (dev machine, before Checkpoint B),
    skip the push cleanly (log only) -- this is not an error.

    Raises UpdateStateError on any unrecoverable git failure.
    """
    add = subprocess.run(["git", "add", "state/"], cwd=repo_root, capture_output=True, text=True)
    if add.returncode != 0:
        raise UpdateStateError(f"git add state/ failed: {add.stderr.strip()}. {STATE_PUSH_FAILURE_NOTE}")

    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
    if diff.returncode == 0:
        logger.info("no state/ changes to commit")
        return

    commit = subprocess.run(
        ["git", "commit", "-m", message], cwd=repo_root, capture_output=True, text=True,
    )
    if commit.returncode != 0:
        raise UpdateStateError(f"git commit failed: {commit.stderr.strip()}. {STATE_PUSH_FAILURE_NOTE}")

    if not _has_remote(repo_root):
        logger.info("no git remote configured -- skipping push (dev machine; remote comes at Checkpoint B)")
        return

    push_args = ["git", "push", "origin", f"HEAD:{STATE_BRANCH}"]
    push = subprocess.run(push_args, cwd=repo_root, capture_output=True, text=True)
    if push.returncode == 0:
        return

    logger.warning("git push rejected, attempting pull --rebase then one retry: %s", push.stderr.strip())
    rebase = subprocess.run(
        ["git", "pull", "--rebase", "origin", STATE_BRANCH], cwd=repo_root, capture_output=True, text=True,
    )
    if rebase.returncode != 0:
        raise UpdateStateError(
            f"git pull --rebase failed after a rejected push: {rebase.stderr.strip()}. {STATE_PUSH_FAILURE_NOTE}"
        )

    retry_push = subprocess.run(push_args, cwd=repo_root, capture_output=True, text=True)
    if retry_push.returncode != 0:
        raise UpdateStateError(
            f"git push failed even after pull --rebase retry: {retry_push.stderr.strip()}. {STATE_PUSH_FAILURE_NOTE}"
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    *,
    work_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    now=None,
) -> dict:
    work_dir = work_dir or WORK_DIR
    state_dir = state_dir or STATE_DIR
    repo_root = repo_root or REPO_ROOT

    today = dates.today_myt(now)

    items, profile = load_sent_items(work_dir)

    seen_path = state_dir / "seen.jsonl"
    append_seen(seen_path, items, today=today)
    pruned = prune_seen(seen_path, today=today)

    headlines = [item["title"] for item in items]
    state.write_last_digest(state_dir / "last_digest.md", date=today, profile=profile, headlines=headlines)

    # [skip ci] guard: the Actions fallback workflow (Slice 4) triggers on
    # workflow_dispatch/cron, not on pushes, but GitHub still evaluates
    # `push`-triggered workflows against any commit -- this marker keeps a
    # state-only commit from ever re-triggering CI on its own.
    git_commit_and_push(repo_root, f"state: digest {today} [skip ci]")

    return {"appended": len(items), "pruned": pruned, "date": today, "profile": profile}


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Record the sent digest in state/ (seen.jsonl + last_digest.md) and commit+push."
    )
    parser.parse_args(argv)

    try:
        result = run()
    except UpdateStateError as exc:
        print(f"update_state: {exc}", file=sys.stderr)
        return 1

    logger.info(
        "appended %d item(s) (pruned %d stale), rewrote last_digest.md for %s (%s)",
        result["appended"], result["pruned"], result["date"], result["profile"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
