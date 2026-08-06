"""Local/CI wrapper: runs the whole daily pipeline in one process --
fetch_news -> the LLM editorial step (DeepSeek API call, or a
`--mock-claude` fixture) -> build_and_send (validate + send/print) ->
update_state.

Usage: python scripts/run_digest.py [--profile NAME] [--dry-run] [--force]
       [--mock-claude PATH] [--skip-state]

This is the ONLY path the pipeline takes: the GitHub Actions cron (or a
manual workflow_dispatch) invokes this wrapper, which calls the DeepSeek
chat-completions API for the editorial step. (The Claude Code Routine that
was previously the primary scheduler is retired -- the user dropped the
Claude subscription; see README.md for the current scheduler story.)

Every stage's failure is routed through notify_failure.py, stage-tagged
("fetch:", "editorial:", "send:", "state:"), before this exits nonzero. The
wrapper never trusts the API call alone: success is verified by
work/selection.json existing, parsing, and passing validate_selection().
The model sometimes wraps its JSON in prose, so parsing is prose-tolerant.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import requests
import yaml

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

LLM_TIMEOUT_SECONDS = 900

# DeepSeek chat-completions API (OpenAI-compatible). deepseek-chat is the
# V3-series chat model -- the strong, cheap choice for this JSON task
# (deepseek-reasoner would burn thinking tokens for no benefit here).
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MAX_TOKENS = 4096

STAGE_FETCH = "fetch"
STAGE_EDITORIAL = "editorial"
STAGE_SEND = "send"
STAGE_STATE = "state"


class EditorialError(RuntimeError):
    """Raised when the LLM editorial step cannot produce a valid
    work/selection.json, even after one retry (or fails outright in a
    non-retryable way: missing API key, network/HTTP error, empty reply)."""


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
# LLM editorial step
# ---------------------------------------------------------------------------

# Compact copy of CLAUDE.md's editorial rules, used only when the repo's
# CLAUDE.md can't be read (e.g. tests with a bare tmp repo). In production
# the live CLAUDE.md is embedded verbatim -- it is the single source of
# truth, and this fallback exists purely so the wrapper still works (and
# tests still exercise a realistic prompt) without it.
_FALLBACK_RULES = """Selection schema (authoritative):
{"profile": "<active profile name from candidates.json>",
 "date": "<today's MYT date from candidates.json>",
 "items": [{"index": <int>, "summary": "<why it matters, 1-2 sentences, <=200 chars, plain text>",
            "category": "<optional short label, e.g. markets/macro/gold>"}],
 "skipped_as_repeat": [<indices of stories that are pure repeats of yesterday's digest>]}

Hard rules:
1. Select ONLY by index from the candidates list. Never select or reference a story not in it.
2. NEVER write a URL, domain, or the string "www" anywhere in your output -- not in
   summary, not in category, not anywhere. The sending script attaches the real URLs.
3. NEVER invent facts. Only use what a candidate's title and snippet say. Do not add
   numbers, quotes, or claims the source text doesn't contain.
4. Summaries are <=200 characters, 1-2 sentences, plain text. No HTML, no Markdown.
5. If a story is a pure repeat of something already covered in last_digest.md, skip it
   and put its index in skipped_as_repeat instead of items. A genuine new development
   may be included, but the summary MUST state what is new.

Editorial guidance: prefer market-moving / high-impact stories over merely interesting
ones; if several candidates cover the same underlying story, pick the single best one
and leave the rest out silently (do not list them in skipped_as_repeat); use the active
profile's editorial_focus as your lens. 5-8 items; fewer is acceptable ONLY if
thin_news is true or fewer than 5 candidates exist. Never invent items to reach 5.
On weekends (Saturday/Sunday MYT), use a lighter framing."""


def _load_rules_text(repo_root: Path) -> str:
    """The editorial rules for the prompt come from the repo's CLAUDE.md --
    one source of truth, so rule changes never require touching the wrapper.
    Falls back to _FALLBACK_RULES if the file is missing/unreadable."""
    try:
        text = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_RULES
    return text if text.strip() else _FALLBACK_RULES


def _load_editorial_focus(profile: str, repo_root: Path) -> str:
    """Read the active profile's editorial_focus (the editorial lens) from
    config/profiles/<profile>.yaml; empty string if unavailable."""
    path = repo_root / "config" / "profiles" / f"{profile}.yaml"
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(data, dict):
        return ""
    focus = data.get("editorial_focus")
    return str(focus).strip() if focus else ""


def build_editorial_prompt(
    *,
    profile: str,
    candidates: dict,
    last_digest: str,
    rules_text: str,
    editorial_focus: str = "",
    extra_note: Optional[str] = None,
) -> str:
    """Build the self-contained editorial prompt. Unlike the retired Claude
    Code path (where the model could read files itself), the DeepSeek API
    gets everything inline: the rules, the candidates, yesterday's digest
    (soft dedup), and the profile's editorial focus. The model returns the
    selection JSON; the wrapper writes work/selection.json from its reply."""
    prompt = (
        "You are the editorial step of a daily news digest pipeline for a busy reader "
        "in Malaysia (MYT, UTC+8). Produce today's selection. Follow the rules below "
        "exactly.\n\n"
        "NOTE: you cannot read files in this invocation. work/candidates.json and "
        "state/last_digest.md are provided inline below. Do not attempt file access.\n\n"
        f"--- editorial rules ---\n{rules_text}\n\n"
        f"--- active profile's editorial focus (lens for what matters) ---\n"
        f"{editorial_focus or '(none configured)'}\n\n"
        f"--- work/candidates.json (profile={profile}) ---\n"
        f"{json.dumps(candidates, indent=2, ensure_ascii=False)}\n\n"
        "--- state/last_digest.md (yesterday's digest, for soft dedup) ---\n"
        f"{last_digest.strip() if last_digest.strip() else '(no previous digest yet -- first run)'}\n\n"
        "Return ONLY the selection JSON object -- the exact schema from the rules above. "
        "No markdown code fences, no commentary, no prose around it. The output must be "
        "a single valid JSON object."
    )
    if extra_note:
        prompt += (
            "\n\nYour previous attempt's JSON failed validation with this error: "
            f"{extra_note}\nFix the problem and return a valid selection JSON object again."
        )
    return prompt


def _delete_stale_selection(selection_path: Path) -> None:
    """A leftover selection.json from a previous run must never pass as
    today's output -- remove it before every LLM call (first attempt and
    retry alike), so any file found afterwards is guaranteed fresh from
    that specific invocation."""
    if selection_path.exists():
        selection_path.unlink()


def _call_llm(prompt: str, timeout: int) -> str:
    """Call the DeepSeek chat-completions API and return the model's reply
    text. Raises EditorialError on any failure -- missing key, network
    error, non-200 HTTP status, malformed/empty response. These are all
    treated as non-retryable (no point retrying a dead key, an exhausted
    balance, or a platform outage); only validation failures retry (see
    run_editorial_step)."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise EditorialError("DEEPSEEK_API_KEY environment variable is not set")

    try:
        response = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": "You are a news editor producing a JSON selection."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": DEEPSEEK_MAX_TOKENS,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise EditorialError(f"LLM API request failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        raise EditorialError(f"LLM API returned non-JSON response (HTTP {response.status_code})")

    if response.status_code != 200:
        message = "unknown error"
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("message"):
            message = error["message"]
        raise EditorialError(f"LLM API error {response.status_code}: {message}")

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise EditorialError("LLM API response missing choices[0].message.content")
    if not isinstance(content, str) or not content.strip():
        raise EditorialError("LLM API returned an empty response")
    return content


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
    state_dir: Optional[Path] = None,
    mock_claude: Optional[Path] = None,
    timeout: int = LLM_TIMEOUT_SECONDS,
) -> dict:
    """Run the LLM editorial step (or copy the --mock-claude fixture), then
    validate the resulting work/selection.json against work/candidates.json.
    One retry with the validation error appended to the prompt; a second
    failure raises EditorialError.

    Returns the validated selection dict on success.
    """
    selection_path = work_dir / "selection.json"
    candidates = build_and_send.load_json(work_dir / "candidates.json")

    state_dir = state_dir or repo_root / "state"
    last_digest_path = state_dir / "last_digest.md"
    last_digest = last_digest_path.read_text(encoding="utf-8") if last_digest_path.exists() else ""
    rules_text = _load_rules_text(repo_root)
    editorial_focus = _load_editorial_focus(profile, repo_root)

    extra_note: Optional[str] = None
    last_error = "unknown error"

    for attempt in (1, 2):
        _delete_stale_selection(selection_path)

        prompt = build_editorial_prompt(
            profile=profile,
            candidates=candidates,
            last_digest=last_digest,
            rules_text=rules_text,
            editorial_focus=editorial_focus,
            extra_note=extra_note,
        )

        if mock_claude is not None:
            selection_path.parent.mkdir(parents=True, exist_ok=True)
            selection_path.write_bytes(Path(mock_claude).read_bytes())
        else:
            # _call_llm raises EditorialError on API/network failures and
            # those propagate immediately -- only validation failures are
            # worth retrying with the error appended to the prompt.
            model_text = _call_llm(prompt, timeout)
            selection_path.write_text(model_text, encoding="utf-8")

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

    # Stage 2: editorial (LLM)
    try:
        run_editorial_step(
            profile=active_profile, work_dir=work_dir, repo_root=repo_root, state_dir=state_dir,
            mock_claude=mock_claude,
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
        description="Run the full daily digest pipeline: fetch -> LLM editorial step (DeepSeek) -> validate+send -> update_state."
    )
    parser.add_argument("--profile", help="Override active_profile from config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Pass through to build_and_send.py (print instead of send)")
    parser.add_argument("--force", action="store_true", help="Pass through to build_and_send.py (bypass the same-day guard)")
    parser.add_argument(
        "--mock-claude", type=Path, default=None,
        help="Skip the LLM call entirely; copy this fixture file to work/selection.json instead",
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
