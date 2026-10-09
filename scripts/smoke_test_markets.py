#!/usr/bin/env python3
"""Make one unauthenticated Kalshi market-data request and save a fixture."""

import json
from pathlib import Path
import sys

from kalshi_arbitrage.market_data import MarketDataInputError, normalize_market
from kalshi_arbitrage.rest_client import (
    DEFAULT_TIMEOUT_SECONDS,
    PRODUCTION_BASE_URL,
    KalshiClientError,
    UrllibGetTransport,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "kalshi_market_response.json"


def main() -> int:
    url = f"{PRODUCTION_BASE_URL}/markets"
    print(f"GET {url}?limit=1")
    try:
        response = UrllibGetTransport().get(
            url,
            {"limit": "1"},
            DEFAULT_TIMEOUT_SECONDS,
        )
    except KalshiClientError as exc:
        print(f"Network/DNS error: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP status: {response.status_code}")
    if response.status_code < 200 or response.status_code >= 300:
        print("HTTP request failed; no fixture was saved.", file=sys.stderr)
        return 1

    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"JSON parsing error: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print("Response-shape error: top-level JSON must be an object.", file=sys.stderr)
        return 1
    markets = payload.get("markets")
    if not isinstance(markets, list):
        print("Response-shape error: 'markets' must be an array.", file=sys.stderr)
        return 1
    print(f"JSON keys: {sorted(payload.keys())}")
    print(f"Market count: {len(markets)}")
    print(f"Cursor present: {'cursor' in payload}")
    if not markets or not isinstance(markets[0], dict):
        print("Response-shape error: no market object was returned.", file=sys.stderr)
        return 1

    try:
        normalized = normalize_market(markets[0])
    except (MarketDataInputError, TypeError, ValueError) as exc:
        print(f"Normalization error: {exc}", file=sys.stderr)
        return 1

    print(
        "Normalized market: "
        f"ticker={normalized.ticker!r}, "
        f"event_ticker={normalized.event_ticker!r}, "
        f"status={normalized.status!r}"
    )
    fixture_payload = {"market": markets[0]}
    try:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(
            json.dumps(fixture_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"Fixture write error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved fixture: {FIXTURE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
