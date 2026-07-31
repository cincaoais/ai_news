"""Tests for lib/dates.py: the MYT (Asia/Kuala_Lumpur) 'today' helper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib import dates


def test_today_myt_regular_daytime_utc_is_same_calendar_day():
    # Noon UTC == 20:00 MYT (UTC+8), same calendar day.
    now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
    assert dates.today_myt(now) == "2026-07-31"


def test_today_myt_2347_utc_rolls_to_next_myt_day():
    # This is the exact scenario the helper exists for: the GitHub Actions
    # fallback cron fires 23:47 UTC == 07:47 MYT the *next* day.
    now = datetime(2026, 7, 30, 23, 47, 0, tzinfo=timezone.utc)
    assert dates.today_myt(now) == "2026-07-31"


def test_today_myt_just_before_midnight_utc_still_next_myt_day():
    now = datetime(2026, 7, 30, 23, 59, 59, tzinfo=timezone.utc)
    assert dates.today_myt(now) == "2026-07-31"


def test_today_myt_naive_datetime_assumed_utc():
    # No tzinfo at all -- every other datetime in this codebase (lib/feeds.py,
    # lib/market.py) always produces tz-aware UTC, but guard the assumption.
    now = datetime(2026, 7, 30, 23, 47, 0)
    assert dates.today_myt(now) == "2026-07-31"


def test_today_myt_non_utc_tz_aware_input_converts_correctly():
    # 2026-07-30 20:00 in UTC+5 == 2026-07-30 15:00 UTC == 2026-07-30 23:00 MYT.
    plus5 = timezone(timedelta(hours=5))
    now = datetime(2026, 7, 30, 20, 0, 0, tzinfo=plus5)
    assert dates.today_myt(now) == "2026-07-30"
