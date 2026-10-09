import json
from pathlib import Path

import pytest

from kalshi_arbitrage.market_data import normalize_market


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "kalshi_market_response.json"


def test_saved_real_market_fixture_normalizes_offline():
    if not FIXTURE_PATH.exists():
        pytest.skip("Run scripts/smoke_test_markets.py to capture the real fixture")

    with FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
        payload = json.load(fixture_file)

    market = normalize_market(payload)
    assert market.ticker
    assert market.event_ticker
    assert market.market_type == "binary"
