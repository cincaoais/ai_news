"""Malaysia-time (Asia/Kuala_Lumpur, UTC+8) "today" helper.

Every "today" in this project (candidates.json's date field, the digest
header, the same-day guard, and state entries) must use MYT, never the run
host's naive UTC date.

Rationale (from docs/03-EXECUTION-LOG.md's Slice 1 follow-up): the GitHub
Actions fallback cron fires at 23:47 UTC, which is 07:47 MYT the *next*
day (UTC+8). If "today" were computed from a naive UTC date, that fallback
run would tag its digest with yesterday's date (UTC) while the user
experiences it as a brand-new day (MYT) -- silently breaking the same-day
duplicate-send guard and putting an off-by-one date into state/seen.jsonl
and state/last_digest.md.

Note: on Windows there is no OS-provided IANA timezone database, so
zoneinfo needs the `tzdata` pip package (declared in requirements.txt) to
resolve "Asia/Kuala_Lumpur". Most Linux CI runners ship a system tzdata
already, so the pip package is a harmless no-op there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

MYT = ZoneInfo("Asia/Kuala_Lumpur")


def today_myt(now: Optional[datetime] = None) -> str:
    """Return today's date (YYYY-MM-DD) in Asia/Kuala_Lumpur (MYT, UTC+8).

    `now` defaults to the real current instant (UTC). A naive `now` (no
    tzinfo) is assumed to already be UTC, matching every other datetime in
    this codebase (lib/feeds.py, lib/market.py always produce tz-aware UTC).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(MYT).date().isoformat()
