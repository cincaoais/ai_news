"""Local/CI wrapper: runs the whole daily pipeline in one process --
fetch_news -> the Claude editorial step (`claude -p`, or a `--mock-claude`
fixture) -> build_and_send (validate + send/print) -> update_state.

Usage: python scripts/run_digest.py [--profile NAME] [--dry-run] [--force]
       [--mock-claude PATH] [--skip-state]

This is NOT the path the Claude Code Routine takes (it follows RUNBOOK.md
directly, with Claude itself as the orchestrator). This wrapper exists for:
- the dev machine (this file's own live-gate acceptance run)
- the GitHub Actions fallback workflow, where `claude -p` must be invoked as
  a subprocess by something other than Claude itself.

Every stage's failure is routed through notify_failure.py, stage-tagged
("fetch:", "editorial:", "send:", "state:"), before this exits nonzero. Per
docs/01-RND.md Theme B, a `claude -p` exit code of 0 does NOT mean success --
usage-limit hits surface as plain text in the JSON `result` field with no
distinct exit code, and the model sometimes wraps its JSON in prose. This
wrapper never trusts the subprocess's exit code alone: success is verified by
work/selection.json existing, parsing, and passing validate_selection().
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import build_and_send
import fetch_news
import notify_failure
import update_state
from lib.env import load_dotenv

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
WORK_DIR = REPO_ROOT / "work"
STATE_DIR = REPO_ROOT / "state"

CLAUDE_TIMEOUT_SECONDS = 900

STAGE_FETCH = "fetch"
STAGE_EDITORIAL = "editorial"
STAGE_SEND = "send"
STAGE_STATE = "state"


class EditorialError(RuntimeError):
    """Raised when the claude editorial step cannot produce a valid
    work/selection.json, even after one retry."""


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

def notify(stage: str, message: str) -> None:
    """Route a failure through notify_failure.py, stage-tagged. Calls its
    main() in-process (this module already imports it) rather than
    shelling out -- notify_failure.main() already swallows its own
    failures and always returns 0, so this can never mask the caller's
    own nonzero exit."""
    notify_failure.main([f"{stage}: {message}"])


# ---------------------------------------------------------------------------
# Claude editorial step
# ---------------------------------------------------------------------------

def build_claude_prompt(profile: str, extra_note: Optional[str] = None) -> str:
    prompt = (
        "Execute the daily digest editorial step. Follow CLAUDE.md in this repository "
        f"exactly. Read work/candidates.json (profile={profile}) and "
        "state/last_digest.md, then write work/selection.json per the schema and hard "
        "rules in CLAUDE.md. Do nothing else -- no sending, no state writes, no git."
    )
    if extra_note:
        prompt += (
            "\n\nYour previous attempt's work/selection.json failed validation with "
            f"this error: {extra_note}\nFix the problem and write work/selection.json again."
        )
    return prompt


def _delete_stale_selection(selection_path: Path) -> None:
    """A leftover selection.json from a previous run must never pass as
    today's output -- remove it before every claude invocation (first
    attempt and retry alike), so any file found afterwards is guaranteed
    fresh from that specific invocation."""
    if selection_path.exists():
        selection_path.unlink()


def _run_claude_subprocess(prompt: str, repo_root: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--model", "sonnet",
            "--max-turns", "20",
            "--permission-mode", "dontAsk",
            "--allowedTools", "Read,Write",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )


def _check_usage_limit(stdout: str) -> Optional[str]:
    """docs/01-RND.md Theme B: a usage-limit hit surfaces as plain text in
    the CLI's JSON `result` field ("You've hit your ... limit ..."), with
    no distinct exit code. Returns the offending result text if found,
    else None."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    result_text = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result_text, str):
        return None
    lowered = result_text.lower()
    if "hit your" in lowered and "limit" in lowered:
        return result_text
    return None


def _load_selection_prose_tolerant(path: Path) -> dict:
    """Load selection.json; if it isn't pure JSON (the model wrapped it in
    prose or a fenced code block), strip to the first '{' through the
    last '}' and retry once before declaring it invalid."""
    # utf-8-sig: tolerates (and strips) a leading UTF-8 BOM, same rationale
    # as build_and_send.load_json.
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in work/selection.json")
    return json.loads(text[start : end + 1])


def run_editorial_step(
    *,
    profile: str,
    work_dir: Path,
    repo_root: Path,
    mock_claude: Optional[Path] = None,
    timeout: int = CLAUDE_TIMEOUT_SECONDS,
) -> dict:
    """Run the claude editorial step (or copy the --mock-claude fixture),
    then validate the resulting work/selection.json against
    work/candidates.json. One retry with the validation error appended to
    the prompt; a second failure raises EditorialError.

    Returns the validated selection dict on success.
    """
    selection_path = work_dir / "selection.json"
    candidates = build_and_send.load_json(work_dir / "candidates.json")

    extra_note: Optional[str] = None
    last_error = "unknown error"

    for attempt in (1, 2):
        _delete_stale_selection(selection_path)

        prompt = build_claude_prompt(profile, extra_note)

        if mock_claude is not None:
            selection_path.parent.mkdir(parents=True, exist_ok=True)
            selection_path.write_bytes(Path(mock_claude).read_bytes())
        else:
            try:
                result = _run_claude_subprocess(prompt, repo_root, timeout)
            except subprocess.TimeoutExpired:
                raise EditorialError(
                    f"claude subprocess timed out after {timeout}s (attempt {attempt})"
                )

            limit_text = _check_usage_limit(result.stdout)
            if limit_text:
                raise EditorialError(f"Claude usage limit hit: {limit_text.strip()}")

        if not selection_path.exists():
            last_error = "work/selection.json was not written"
        else:
            try:
                selection = _load_selection_prose_tolerant(selection_path)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = f"work/selection.json is not valid JSON: {exc}"
                selection = None

            if selection is not None:
                try:
                    build_and_send.validate_selection(selection, candidates)
                    return selection
                except build_and_send.ValidationError as exc:
                    last_error = str(exc)

        if attempt == 1:
            logger.warning(
                "editorial attempt 1 failed (%s); retrying once with the error appended", last_error
            )
            extra_note = last_error

    raise EditorialError(f"work/selection.json still invalid after one retry: {last_error}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(
    *,
    profile: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    mock_claude: Optional[Path] = None,
    skip_state: bool = False,
    repo_root: Optional[Path] = None,
    config_path: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    now=None,
) -> int:
    repo_root = repo_root or REPO_ROOT
    config_path = config_path or CONFIG_PATH
    work_dir = work_dir or WORK_DIR
    state_dir = state_dir or STATE_DIR

    # Stage 1: fetch
    try:
        payload = fetch_news.run(
            profile_name=profile, config_path=config_path, state_dir=state_dir, work_dir=work_dir
        )
    except Exception as exc:  # noqa: BLE001 -- every failure must reach the notifier
        notify(STAGE_FETCH, str(exc))
        return 1

    active_profile = payload["profile"]

    # Stage 2: editorial (claude)
    try:
        run_editorial_step(
            profile=active_profile, work_dir=work_dir, repo_root=repo_root, mock_claude=mock_claude,
        )
    except Exception as exc:  # noqa: BLE001
        notify(STAGE_EDITORIAL, str(exc))
        return 1

    # Stage 3: validate + send (or print, under --dry-run)
    try:
        send_result = build_and_send.run(
            dry_run=dry_run, force=force, work_dir=work_dir, state_dir=state_dir,
            repo_root=repo_root, now=now,
        )
    except Exception as exc:  # noqa: BLE001
        notify(STAGE_SEND, str(exc))
        return 1

    if dry_run:
        # Mirrors build_and_send.main()'s own --dry-run printing, since this
        # wrapper calls build_and_send.run() directly rather than shelling
        # out to build_and_send.py -- without this, --dry-run would validate
        # silently and print nothing.
        messages = send_result["messages"]
        for i, message in enumerate(messages, start=1):
            print(f"--- message {i}/{len(messages)} ---")
            print(message)
            print()

    if dry_run or skip_state:
        return 0

    # Stage 4: update state (append seen, rewrite last_digest.md, commit+push)
    try:
        update_state.run(work_dir=work_dir, state_dir=state_dir, repo_root=repo_root, now=now)
    except Exception as exc:  # noqa: BLE001
        notify(STAGE_STATE, str(exc))
        return 1

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Run the full daily digest pipeline: fetch -> claude editorial step -> validate+send -> update_state."
    )
    parser.add_argument("--profile", help="Override active_profile from config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Pass through to build_and_send.py (print instead of send)")
    parser.add_argument("--force", action="store_true", help="Pass through to build_and_send.py (bypass the same-day guard)")
    parser.add_argument(
        "--mock-claude", type=Path, default=None,
        help="Skip the claude subprocess entirely; copy this fixture file to work/selection.json instead",
    )
    parser.add_argument(
        "--skip-state", action="store_true",
        help="Skip update_state.py (no state append, no git commit/push) even after a real send",
    )
    args = parser.parse_args(argv)

    return run(
        profile=args.profile,
        dry_run=args.dry_run,
        force=args.force,
        mock_claude=args.mock_claude,
        skip_state=args.skip_state,
    )


if __name__ == "__main__":
    sys.exit(main())
