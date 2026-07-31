"""Seen-URL state: canonicalization + load/append of state/seen.jsonl."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

logger = logging.getLogger(__name__)


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
