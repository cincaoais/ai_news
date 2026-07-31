"""Live smoke test -- real network, real feeds, real Yahoo chart API.

Excluded by default (pytest.ini sets `addopts = -m "not live"`). Run
explicitly with:

    pytest -m live
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.live
def test_finance_metals_live_fetch_produces_enough_candidates():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fetch_news.py"), "--profile", "finance_metals"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"fetch_news.py failed:\nstdout={result.stdout}\nstderr={result.stderr}"

    candidates_path = REPO_ROOT / "work" / "candidates.json"
    assert candidates_path.exists()

    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    assert payload["profile"] == "finance_metals"
    assert len(payload["items"]) >= 10, (
        f"expected >=10 candidates on a normal news day, got {len(payload['items'])}"
    )
    assert payload["market"] is not None, "expected a non-null market snapshot for finance_metals"
