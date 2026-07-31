"""Yahoo Finance chart API market snapshot fetcher (keyless, browser UA required)."""

from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 10

PRIMARY_HOST = "query1.finance.yahoo.com"
FALLBACK_HOST = "query2.finance.yahoo.com"
CHART_URL_TEMPLATE = "https://{host}/v8/finance/chart/{symbol}?range=1d&interval=1d"


def fetch_quote(symbol: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> Optional[dict]:
    """Fetch one symbol's snapshot quote from Yahoo's chart API.

    Tries query1 first, falls back to query2 on any failure. Returns None
    (never raises) if both hosts fail or the response is unparseable --
    callers treat that as a graceful per-symbol skip.
    """
    headers = {"User-Agent": USER_AGENT}
    for host in (PRIMARY_HOST, FALLBACK_HOST):
        url = CHART_URL_TEMPLATE.format(host=host, symbol=symbol)
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("market fetch failed for %s via %s: %s", symbol, host, exc)
            continue

        try:
            result = data["chart"]["result"][0]
            meta = result["meta"]
            price = meta["regularMarketPrice"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("market response for %s via %s missing expected fields: %s", symbol, host, exc)
            continue

        previous_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        quote: dict = {"symbol": symbol, "price": price}
        if previous_close:
            change = price - previous_close
            quote["previous_close"] = previous_close
            quote["change"] = change
            quote["change_pct"] = (change / previous_close) * 100
        return quote

    logger.warning("market snapshot unavailable for %s (all hosts failed)", symbol)
    return None


def fetch_market_snapshot(symbols: list[str], *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> Optional[dict]:
    """Fetch quotes for each symbol. Failed symbols are omitted.

    Returns None if every symbol failed (graceful whole-block skip);
    otherwise a dict of symbol -> quote for the ones that succeeded.
    """
    snapshot: dict = {}
    for symbol in symbols:
        quote = fetch_quote(symbol, timeout=timeout)
        if quote is not None:
            snapshot[symbol] = quote
    return snapshot or None
