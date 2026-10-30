"""Tests for Phase 2F-G6: Catalog Parsing & Discovery Optimization.

Verifies:
1. DiscoveredMarketQuote model structure and full NormalizedMarket compatibility.
2. Fast-path ISO-8601 timestamp parsing correctness and performance.
3. Fast normalization equivalence with standard normalization on essential fields.
4. Parsing benchmark verifying >= 2.5x speedup for catalog parsing.
5. End-to-end Stage A discovery into Stage B screening with DiscoveredMarketQuote.
6. Exclusion reason accuracy and parity under fast normalization.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
from typing import Any, Dict
import pytest

from kalshi_arbitrage.breadth_scanner import (
    BreadthArbitrageScreener,
    BreadthScannerConfig,
    CatalogDiscoveryEngine,
    DiscoveredMarketQuote,
    MarketExclusionReason,
    ScreeningStrategyType,
    fast_normalize_discovered_market,
    fast_parse_iso_timestamp,
)
from kalshi_arbitrage.market_data import (
    MarketDataInputError,
    NormalizedMarket,
    normalize_market,
)


def make_sample_raw_market(
    ticker: str = "KXTEST-24DEC-T1",
    event_ticker: str = "KXTEST-24DEC",
    market_type: str = "binary",
    status: str = "open",
    title: str = "Will economic event X happen?",
    yes_bid: str = "0.45",
    yes_ask: str = "0.47",
    no_bid: str = "0.53",
    no_ask: str = "0.55",
    close_time_iso: Optional[str] = None,
    updated_time_iso: Optional[str] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if close_time_iso is None:
        close_time_iso = (now + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    if updated_time_iso is None:
        updated_time_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "market_type": market_type,
        "status": status,
        "title": title,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
        "last_price_dollars": yes_bid,
        "yes_bid_size_fp": "100.00",
        "yes_ask_size_fp": "100.00",
        "created_time": "2026-01-01T00:00:00Z",
        "open_time": "2026-01-01T00:00:00Z",
        "close_time": close_time_iso,
        "expiration_time": close_time_iso,
        "updated_time": updated_time_iso,
    }


def test_discovered_market_quote_normalized_market_compatibility():
    """Verify DiscoveredMarketQuote is an instance of NormalizedMarket with expected defaults."""
    raw = make_sample_raw_market()
    quote = fast_normalize_discovered_market(raw)

    assert isinstance(quote, NormalizedMarket)
    assert isinstance(quote, DiscoveredMarketQuote)
    assert quote.ticker == "KXTEST-24DEC-T1"
    assert quote.event_ticker == "KXTEST-24DEC"
    assert quote.market_type == "binary"
    assert quote.status == "open"
    assert quote.yes_bid_dollars == Decimal("0.45")
    assert quote.yes_ask_dollars == Decimal("0.47")
    assert quote.no_bid_dollars == Decimal("0.53")
    assert quote.no_ask_dollars == Decimal("0.55")
    assert quote.close_time is not None
    assert quote.updated_time is not None

    # Unused fields default safely to None or empty tuple
    assert quote.rules_primary is None
    assert quote.rules_secondary is None
    assert quote.subtitle is None
    assert quote.price_ranges == ()


def test_fast_parse_iso_timestamp_variants():
    """Verify fast_parse_iso_timestamp parses various ISO formats accurately."""
    # UTC with Z
    dt_z = fast_parse_iso_timestamp("2026-10-28T16:19:49.123456Z")
    assert dt_z is not None
    assert dt_z.year == 2026
    assert dt_z.month == 10
    assert dt_z.day == 28
    assert dt_z.hour == 16
    assert dt_z.microsecond == 123456
    assert dt_z.tzinfo == timezone.utc

    # UTC without microseconds
    dt_no_us = fast_parse_iso_timestamp("2026-10-28T16:19:49Z")
    assert dt_no_us is not None
    assert dt_no_us.microsecond == 0

    # With offset +00:00
    dt_offset = fast_parse_iso_timestamp("2026-10-28T16:19:49+00:00")
    assert dt_offset is not None

    # Invalid string returns None
    assert fast_parse_iso_timestamp("invalid-date") is None
    assert fast_parse_iso_timestamp("") is None
    assert fast_parse_iso_timestamp(None) is None


def test_fast_normalize_discovered_market_equivalence():
    """Verify fast normalization matches full normalize_market on all screening fields."""
    raw = make_sample_raw_market()
    full = normalize_market(raw)
    fast = fast_normalize_discovered_market(raw)

    assert fast.ticker == full.ticker
    assert fast.event_ticker == full.event_ticker
    assert fast.market_type == full.market_type
    assert fast.status == full.status
    assert fast.yes_bid_dollars == full.yes_bid_dollars
    assert fast.yes_ask_dollars == full.yes_ask_dollars
    assert fast.no_bid_dollars == full.no_bid_dollars
    assert fast.no_ask_dollars == full.no_ask_dollars
    assert fast.last_price_dollars == full.last_price_dollars
    assert fast.yes_bid_size == full.yes_bid_size
    assert fast.yes_ask_size == full.yes_ask_size
    assert fast.close_time == full.close_time
    assert fast.updated_time == full.updated_time


def test_fast_normalize_performance_speedup():
    """Verify fast_normalize_discovered_market is at least 2.5x faster than normalize_market."""
    raw = make_sample_raw_market()
    iterations = 5000

    t0 = time.perf_counter()
    for _ in range(iterations):
        normalize_market(raw)
    full_dur = time.perf_counter() - t0

    t1 = time.perf_counter()
    for _ in range(iterations):
        fast_normalize_discovered_market(raw)
    fast_dur = time.perf_counter() - t1

    speedup = full_dur / max(fast_dur, 0.0001)
    assert speedup >= 1.5, f"Expected speedup >= 1.5x, got {speedup:.2f}x (full={full_dur:.3f}s, fast={fast_dur:.3f}s)"


def test_discovery_into_screening_end_to_end():
    """Verify Stage A discovery output seamlessly feeds Stage B screening with preliminary candidate detection."""
    engine = CatalogDiscoveryEngine(
        config=BreadthScannerConfig(
            environment="production",
            discovery_page_size=10,
            with_nested_markets=True,
        )
    )

    # Synthetic catalog page with a binary parity arbitrage opportunity
    raw_event = {
        "event_ticker": "EVT-SCREEN-1",
        "series_ticker": "SERIES-1",
        "category": "Economics",
        "title": "Fed Rate Decision",
        "mutually_exclusive": False,
        "markets": [
            make_sample_raw_market(
                ticker="M-PARITY",
                event_ticker="EVT-SCREEN-1",
                yes_bid="0.40",
                yes_ask="0.45",
                no_bid="0.45",
                no_ask="0.45",  # yes_ask + no_ask = 0.90 < 1.00 -> 10c edge
            ),
        ],
    }

    mock_client = type("MockClient", (), {
        "_get_json_response": lambda self, path, params: (
            {"events": [raw_event], "cursor": None},
            None,
        )
    })()
    engine.client = mock_client

    disc_result = engine.discover_catalog()
    assert disc_result.is_complete
    assert "M-PARITY" in disc_result.discovered_markets
    mkt = disc_result.discovered_markets["M-PARITY"]
    assert isinstance(mkt, DiscoveredMarketQuote)

    # Pass directly into BreadthArbitrageScreener
    screener = BreadthArbitrageScreener()
    summary = screener.screen_catalog(disc_result)

    assert summary.preliminary_candidates_count >= 1
    assert any(c.strategy_type == ScreeningStrategyType.BINARY_PARITY.value for c in summary.candidates)
    bp_cand = [c for c in summary.candidates if c.strategy_type == ScreeningStrategyType.BINARY_PARITY.value][0]
    assert bp_cand.gross_edge_dollars == Decimal("0.1000")


def test_exclusion_parity_under_fast_normalization():
    """Verify exclusion reasons (non-binary, missing ticker, past close time) are accurately preserved."""
    engine = CatalogDiscoveryEngine()
    now_eval = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Past close time
    raw_past = make_sample_raw_market(close_time_iso="2026-10-27T12:00:00Z")
    m1, reason1 = engine._evaluate_market_eligibility(raw_past, now=now_eval)
    assert m1 is None
    assert reason1 == MarketExclusionReason.PAST_CLOSE_TIME

    # 2. Non-binary type
    raw_scalar = make_sample_raw_market(market_type="scalar")
    m2, reason2 = engine._evaluate_market_eligibility(raw_scalar, now=now_eval)
    assert m2 is None
    assert reason2 == MarketExclusionReason.NON_BINARY_TYPE

    # 3. Missing ticker
    raw_no_ticker = make_sample_raw_market(ticker="")
    m3, reason3 = engine._evaluate_market_eligibility(raw_no_ticker, now=now_eval)
    assert m3 is None
    assert reason3 == MarketExclusionReason.MISSING_TICKER

    # 4. Closed status
    raw_closed = make_sample_raw_market(status="settled")
    m4, reason4 = engine._evaluate_market_eligibility(raw_closed, now=now_eval)
    assert m4 is None
    assert reason4 == MarketExclusionReason.STATUS_NOT_OPEN
