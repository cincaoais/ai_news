"""Make scripts/ (and its lib/ package) importable from tests, matching how
`python scripts/fetch_news.py` runs it directly (script dir on sys.path[0]).

Also provides small shared test helpers: a fake `requests.get` response and
the fixtures directory path.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

for p in (str(SCRIPTS_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class FakeResponse:
    """Stand-in for requests.Response used to mock requests.get in tests."""

    def __init__(self, *, text: str = "", json_data=None, status_code: int = 200):
        self.text = text
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        if self._json_data is None:
            raise ValueError("FakeResponse has no json body")
        return self._json_data


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
