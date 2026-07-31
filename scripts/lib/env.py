"""Minimal .env loader (stdlib only -- no python-dotenv dependency).

Loads KEY=VALUE pairs from the repo-root .env file into os.environ, but
NEVER overrides a variable that is already set in the real environment:
real env vars (setx, CI secrets, Routine environment variables) always
win over .env. The .env file is gitignored; example.env is the committed
template.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


def load_dotenv(path: Optional[Path] = None) -> int:
    """Load KEY=VALUE lines from `path` (default: repo-root .env) into
    os.environ via setdefault. Returns the number of variables actually
    set (i.e. not already present in the environment).

    Parsing rules: blank lines and lines starting with '#' are ignored;
    everything before the first '=' is the key, everything after is the
    value; surrounding whitespace and one layer of matching quotes are
    stripped from the value. Lines without '=' are ignored with a warning.
    A missing file is a silent no-op (normal on CI, where secrets arrive
    as real env vars).
    """
    path = path or DEFAULT_ENV_PATH
    if not path.exists():
        return 0

    loaded = 0
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning(".env line %d has no '=', ignored: %r", lineno, raw)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            logger.warning(".env line %d has empty key, ignored: %r", lineno, raw)
            continue
        if value.startswith("PASTE_") and value.endswith("_HERE"):
            # Unfilled example.env placeholder -- treat as not set, so a
            # half-filled .env degrades to the same "missing credential"
            # behavior as no .env at all.
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
