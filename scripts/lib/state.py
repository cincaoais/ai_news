"""Seen-URL state: canonicalization + load/append of state/seen.jsonl, plus
state/last_digest.md front-matter read/write (soft-dedup input for Claude
and the same-day guard's date/profile source)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import yaml

logger = logging.getLogger(__name__)

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def canonicalize_url(url: str) -> str:
    """Normalize a URL for dedup/seen-comparison purposes.

    - lowercase the host
    - drop the fragment
    - strip utm_* query params
    - drop a single trailing slash from the path
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()

    kept_params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_")]
    query = urlencode(kept_params)

    path = parts.path
    if path.endswith("/") and path != "/":
        path = path[:-1]

    return urlunsplit((parts.scheme, host, path, query, ""))


def load_seen(seen_path: Path) -> set[str]:
    """Load the set of canonical URLs already seen (sent) previously.

    Bootstraps an empty set if the file is missing.
    """
    if not seen_path.exists():
        logger.info("seen file %s does not exist; starting with empty seen-set", seen_path)
        return set()

    seen: set[str] = set()
    with seen_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed line %d in %s", line_no, seen_path)
                continue
            url = record.get("url")
            if url:
                seen.add(canonicalize_url(url))
    return seen


def bootstrap_state_dir(state_dir: Path) -> None:
    """Ensure the state directory (and an empty seen.jsonl) exist."""
    state_dir.mkdir(parents=True, exist_ok=True)
    seen_path = state_dir / "seen.jsonl"
    if not seen_path.exists():
        seen_path.touch()


def read_last_digest_front_matter(path: Path) -> Optional[dict]:
    """Parse state/last_digest.md's YAML front-matter, returning
    {"date": ..., "profile": ...} (or None if the file doesn't exist yet --
    first run -- or has no parseable front-matter)."""
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return None

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        logger.warning("could not parse front-matter of %s; treating as absent", path)
        return None

    return meta if isinstance(meta, dict) else None


def write_last_digest(path: Path, *, date: str, profile: str, headlines: list[str]) -> None:
    """Rewrite state/last_digest.md: YAML front-matter (date + profile) the
    same-day guard reads, followed by the sent headlines list -- this body
    is what Claude reads for soft (semantic, not just URL) dedup."""
    # Quote the date explicitly -- unquoted "date: 2026-07-31" is valid
    # YAML for an implicit date/timestamp scalar, so yaml.safe_load would
    # hand back a datetime.date object instead of the str every caller
    # (the same-day guard, tests) expects.
    lines = [
        "---",
        f'date: "{date}"',
        f"profile: {profile}",
        "---",
        "",
        f"# Digest {date} ({profile})",
        "",
    ]
    lines.extend(f"- {headline}" for headline in headlines)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
