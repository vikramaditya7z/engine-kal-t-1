"""Tests for Phase 2F-L: End-to-End Pipeline Validation and Dashboard Integration.

Validates the complete 12 scenarios:
1. Valid binary parity candidate (passes to Stage C, qualifies).
2. Valid multi-leg MECE basket (passes to Stage C, qualifies).
3. Candidate with old catalog updated_time but mathematically valid prices (passes Stage B, reaches Stage C).
4. Candidate rejected for invalid market status (status != "open").
5. Candidate rejected for invalid structure (incomplete event group / non-binary).
6. Candidate rejected for insufficient depth (available_contracts < requested_qty).
7. Candidate rejected for stale actual order-book data (order_book.source_timestamp > 60s).
8. Candidate rejected for excessive cross-leg client fetch window (fetch_window > 5.0s).
9. Candidate rejected for excessive server timestamp skew (server_skew > 2.0s).
10. Candidate rejected after fees (net edge < 5 cents / unprofitable).
11. Candidate rejected for exceeding maximum legs (legs > 8 -> REJECTED_BASKET_TOO_LARGE).
12. Candidate passing every execution-aware check.

Also verifies:
- Preliminary candidates are distinct and not treated as executable opportunities.
- Dashboard persistence and service (DashboardDataService) accurately represents each stage and rejection reason.
- Offline synthetic data only; no live API calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import email.utils
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage import (
    BreadthArbitrageScreener,
    BreadthScannerConfig,
    BreadthScannerHealth,
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    CatalogDiscoveryEngine,
    ExecutionAwareArbitrageQualifier,
    ExecutionQualifierConfig,
    HTTPResponse,
    KalshiRestClient,
    MarketDataInputError,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PreliminaryCandidate,
    QualificationOutcome,
    ScanCycleSummary,
    ScreeningOutcome,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
)
from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.market_data import normalize_event, normalize_market


# -----------------------------------------------------------------------------
# Test Helpers & Smart Mock Transport
# -----------------------------------------------------------------------------

def make_test_order_book_json(
    yes_bids: Sequence[Tuple[str, str]] = (("0.30", "50.00"),),
    no_bids: Sequence[Tuple[str, str]] = (("0.80", "50.00"),),
) -> Dict[str, Any]:
    """In Kalshi: yes_bids at 0.30 create NO ask at 0.70; no_bids at 0.80 create YES ask at 0.20."""
    return {
        "orderbook_fp": {
            "yes_dollars": [[p, q] for p, q in yes_bids],
            "no_dollars": [[p, q] for p, q in no_bids],
        }
    }


def make_raw_market(
    ticker: str,
    event_ticker: str,
    status: str = "open",
    yes_ask: str = "0.20",
    no_ask: str = "0.70",
    yes_bid: str = "0.18",
    no_bid: str = "0.68",
    updated_time: Optional[str] = None,
    market_type: str = "binary",
) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "market_type": market_type,
        "status": status,
        "title": f"Market for {ticker}",
        "yes_ask_dollars": yes_ask,
        "no_ask_dollars": no_ask,
        "yes_bid_dollars": yes_bid,
        "no_bid_dollars": no_bid,
        "updated_time": updated_time or datetime.now(timezone.utc).isoformat(),
        "close_time": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }


def make_raw_event(
    event_ticker: str,
    markets: Sequence[Dict[str, Any]],
    mutually_exclusive: bool = True,
    category: str = "Economics",
    series_ticker: str = "SERIES-1",
) -> Dict[str, Any]:
    return {
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "category": category,
        "title": f"Event {event_ticker}",
        "mutually_exclusive": mutually_exclusive,
        "markets": list(markets),
    }


class ScenarioMockTransport:
    """Mock transport supporting configurable HTTP responses, delays, and per-market headers."""

    def __init__(self) -> None:
        self.events_pages: List[Dict[str, Any]] = []
        self.order_books: Dict[str, Dict[str, Any]] = {}
        self.order_book_headers: Dict[str, Dict[str, str]] = {}
        self.order_book_delays: Dict[str, float] = {}
        self.events_by_ticker: Dict[str, Dict[str, Any]] = {}
        self.markets_by_event: Dict[str, List[Dict[str, Any]]] = {}
        self.default_date: str = email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)

    def register_event(self, event_data: Dict[str, Any]) -> None:
        ev_ticker = event_data["event_ticker"]
        self.events_by_ticker[ev_ticker] = event_data
        for m in event_data.get("markets", []):
            if isinstance(m, dict) and "ticker" in m:
                self.register_market(m)

    def register_market(self, market_data: Dict[str, Any]) -> None:
        ev_ticker = market_data.get("event_ticker", "DEFAULT-EV")
        if ev_ticker not in self.markets_by_event:
            self.markets_by_event[ev_ticker] = []
        existing = [m for m in self.markets_by_event[ev_ticker] if m.get("ticker") == market_data.get("ticker")]
        if not existing:
            self.markets_by_event[ev_ticker].append(market_data)

    def queue_events_page(self, events: List[Dict[str, Any]], cursor: Optional[str] = None) -> None:
        self.events_pages.append({"events": events, "cursor": cursor})
        for ev in events:
            self.register_event(ev)

    def set_order_book(
        self,
        ticker: str,
        yes_bids: Sequence[Tuple[str, str]] = (("0.30", "50.00"),),
        no_bids: Sequence[Tuple[str, str]] = (("0.80", "50.00"),),
        date_header: Optional[str] = None,
        fetch_delay_s: float = 0.0,
    ) -> None:
        self.order_books[ticker] = make_test_order_book_json(yes_bids=yes_bids, no_bids=no_bids)
        if date_header:
            self.order_book_headers[ticker] = {"Date": date_header}
        if fetch_delay_s > 0:
            self.order_book_delays[ticker] = fetch_delay_s

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        # Prohibit trading endpoints
        for forbidden in ("/orders", "/portfolio", "/fills", "/account", "/trade-api/v2/orders"):
            if forbidden in url:
                raise AssertionError(f"Forbidden endpoint called: {url}")

        if "/orderbook" in url:
            parts = url.split("/")
            ticker_idx = parts.index("markets") + 1 if "markets" in parts else -2
            ticker = parts[ticker_idx]

            delay = self.order_book_delays.get(ticker, 0.0)
            if delay > 0:
                time.sleep(delay)

            book = self.order_books.get(ticker, make_test_order_book_json())
            headers = self.order_book_headers.get(ticker, {"Date": self.default_date})
            return HTTPResponse(200, json.dumps(book).encode("utf-8"), headers)

        if "/events/" in url:
            ev_ticker = url.split("?")[0].rstrip("/").split("/events/")[1]
            ev_data = self.events_by_ticker.get(ev_ticker)
            if ev_data is None:
                markets = self.markets_by_event.get(ev_ticker, [])
                ev_data = make_raw_event(ev_ticker, markets)
            return HTTPResponse(200, json.dumps({"event": ev_data}).encode("utf-8"), {"Date": self.default_date})

        # Events list pagination
        cursor = params.get("cursor")
        if self.events_pages:
            page = self.events_pages.pop(0)
            return HTTPResponse(200, json.dumps(page).encode("utf-8"), {"Date": self.default_date})

        return HTTPResponse(200, json.dumps({"events": [], "cursor": None}).encode("utf-8"), {"Date": self.default_date})


# -----------------------------------------------------------------------------
# 12 Individual Scenario Tests
# -----------------------------------------------------------------------------

def test_scenario_01_valid_binary_parity():
    """Scenario 1: Valid binary parity candidate passes Stage B and qualifies in Stage C."""
    raw_m = make_raw_market("KX-BP-1", "EV-BP-1", status="open", yes_ask="0.20", no_ask="0.70")
    raw_ev = make_raw_event("EV-BP-1", [raw_m])

    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    # yes_bid 0.30 creates NO ask 0.70; no_bid 0.80 creates YES ask 0.20 (50 contracts)
    transport.set_order_book("KX-BP-1", yes_bids=[("0.30", "50.00")], no_bids=[("0.80", "50.00")])
    client = KalshiRestClient(transport=transport)

    screener = BreadthArbitrageScreener()
    norm_m = normalize_market(raw_m)
    res = screener.screen_market_binary_parity(norm_m)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    cand = res.candidate
    assert cand is not None
    assert cand.strategy_type == ScreeningStrategyType.BINARY_PARITY.value
    assert cand.gross_edge_cents == 10

    qualifier = TargetedOrderBookQualifier(client=client, config=TargetedOrderBookQualifierConfig(target_quantity=10))
    qual = qualifier.qualify_candidate(cand, norm_m)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED.value
    assert qual.requested_quantity == 10
    assert qual.executable_quantity == 10
    assert qual.net_edge_cents is not None and qual.net_edge_cents > 0


def test_scenario_02_valid_multi_leg_mece_basket():
    """Scenario 2: Valid multi-leg MECE basket passes Stage B and qualifies in Stage C."""
    raw_m1 = make_raw_market("KX-M1", "EV-MECE-1", yes_ask="0.25", no_ask="0.75")
    raw_m2 = make_raw_market("KX-M2", "EV-MECE-1", yes_ask="0.30", no_ask="0.70")
    # End with -OTHER to provide collective exhaustiveness
    raw_m3 = make_raw_market("KX-M3-OTHER", "EV-MECE-1", yes_ask="0.35", no_ask="0.65")
    raw_ev = make_raw_event("EV-MECE-1", [raw_m1, raw_m2, raw_m3])

    norm_m1 = normalize_market(raw_m1)
    norm_m2 = normalize_market(raw_m2)
    norm_m3 = normalize_market(raw_m3)
    norm_ev = normalize_event(raw_ev, (norm_m1, norm_m2, norm_m3))

    screener = BreadthArbitrageScreener()
    res = screener.screen_event_mece_baskets(norm_ev, (norm_m1, norm_m2, norm_m3))

    # sum of yes asks = 0.25 + 0.30 + 0.35 = 0.90 -> 10c gross edge for Long YES
    cands = [r.candidate for r in res if r.candidate is not None]
    yes_cands = [c for c in cands if "yes" in c.strategy_type.lower()]
    assert len(yes_cands) == 1
    cand = yes_cands[0]
    assert cand.gross_edge_cents == 10

    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    # For Long YES basket, buy YES on all 3 legs (no_bids create YES asks)
    transport.set_order_book("KX-M1", no_bids=[("0.75", "50.00")])
    transport.set_order_book("KX-M2", no_bids=[("0.70", "50.00")])
    transport.set_order_book("KX-M3-OTHER", no_bids=[("0.65", "50.00")])
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(client=client, config=TargetedOrderBookQualifierConfig(target_quantity=10))
    qual = qualifier.qualify_candidate(cand)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED.value
    assert qual.candidate_leg_count == 3
    assert qual.executable_quantity == 10


def test_scenario_03_candidate_with_old_catalog_updated_time():
    """Scenario 3: Candidate with old catalog updated_time passes Stage B and qualifies at Stage C."""
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    raw_m = make_raw_market("KX-OLD-1", "EV-OLD-1", status="open", yes_ask="0.20", no_ask="0.70", updated_time=two_hours_ago)
    raw_ev = make_raw_event("EV-OLD-1", [raw_m])

    screener = BreadthArbitrageScreener()
    norm_m = normalize_market(raw_m)
    res = screener.screen_market_binary_parity(norm_m)

    # In Phase 2F-K, old catalog updated_time does NOT reject at Stage B
    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    cand = res.candidate
    assert cand is not None
    assert cand.quote_timestamp == two_hours_ago
    assert cand.observation_timestamp is not None  # stamped at discovery

    # At Stage C, fresh order book arrives from exchange
    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    transport.set_order_book("KX-OLD-1", yes_bids=[("0.30", "50.00")], no_bids=[("0.80", "50.00")])
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(target_quantity=10, enable_event_prefilter=False),
    )
    qual = qualifier.qualify_candidate(cand, norm_m)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED.value


def test_scenario_04_candidate_rejected_for_invalid_market_status():
    """Scenario 4: Candidate rejected during Stage B preliminary screening for status != open."""
    raw_m = make_raw_market("KX-CLOSED-1", "EV-CLOSED-1", status="closed", yes_ask="0.20", no_ask="0.70")
    screener = BreadthArbitrageScreener()
    norm_m = normalize_market(raw_m)
    res = screener.screen_market_binary_parity(norm_m)

    assert res.outcome == ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET
    assert res.candidate is None
    assert "closed" in (res.reason or "").lower()


def test_scenario_05_candidate_rejected_for_invalid_structure():
    """Scenario 5: Candidate rejected for invalid structure (incomplete event group / non-binary)."""
    # 5a: Incomplete event group (declared 3 markets, only 2 provided)
    raw_m1 = make_raw_market("KX-S5-1", "EV-S5", yes_ask="0.25", no_ask="0.75")
    raw_m2 = make_raw_market("KX-S5-2", "EV-S5", yes_ask="0.30", no_ask="0.70")
    raw_ev = {
        "event_ticker": "EV-S5",
        "series_ticker": "SERIES-1",
        "category": "Economics",
        "title": "Incomplete Event",
        "mutually_exclusive": True,
        "markets": [
            raw_m1,
            raw_m2,
            {"ticker": "KX-S5-3", "event_ticker": "EV-S5", "market_type": "binary", "status": "open", "title": "Missing Market 3", "yes_ask_dollars": "0.35", "no_ask_dollars": "0.65", "yes_bid_dollars": "0.34", "no_bid_dollars": "0.64", "close_time": raw_m1["close_time"]},
        ],
    }

    norm_m1 = normalize_market(raw_m1)
    norm_m2 = normalize_market(raw_m2)
    norm_ev = normalize_event(raw_ev, (norm_m1, norm_m2))

    screener = BreadthArbitrageScreener()
    # Pass only 2 markets where event has 3 tickers
    res = screener.screen_event_mece_baskets(norm_ev, (norm_m1, norm_m2))
    assert any(r.outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_EVENT_GROUP for r in res)
    assert not any(r.candidate is not None for r in res)

    # 5b: Non-binary market rejected during normalization
    raw_scalar = make_raw_market("KX-SCALAR", "EV-SCALAR", market_type="scalar")
    with pytest.raises(MarketDataInputError, match=r"unsupported market_type"):
        normalize_market(raw_scalar)


def test_scenario_06_candidate_rejected_for_insufficient_depth():
    """Scenario 6: Candidate passes Stage B but is rejected at Stage C for insufficient depth."""
    raw_m = make_raw_market("KX-THIN-1", "EV-THIN-1", status="open", yes_ask="0.20", no_ask="0.70")
    raw_ev = make_raw_event("EV-THIN-1", [raw_m])

    screener = BreadthArbitrageScreener()
    norm_m = normalize_market(raw_m)
    res = screener.screen_market_binary_parity(norm_m)
    cand = res.candidate
    assert cand is not None

    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    # Only 2 contracts available when requested quantity is 10
    transport.set_order_book("KX-THIN-1", yes_bids=[("0.30", "2.00")], no_bids=[("0.80", "2.00")])
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(target_quantity=10, enable_event_prefilter=False, require_full_quantity=True),
    )
    qual = qualifier.qualify_candidate(cand, norm_m)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH.value
    assert "Insufficient depth" in (qual.rejection_reason or "")
    assert qual.executable_quantity == 2


def test_scenario_07_candidate_rejected_for_stale_actual_order_book():
    """Scenario 7: Candidate rejected at Stage C for stale actual order-book timestamp (> 60s)."""
    raw_m = make_raw_market("KX-STALE-1", "EV-STALE-1", status="open", yes_ask="0.20", no_ask="0.70")
    raw_ev = make_raw_event("EV-STALE-1", [raw_m])

    screener = BreadthArbitrageScreener()
    norm_m = normalize_market(raw_m)
    res = screener.screen_market_binary_parity(norm_m)
    cand = res.candidate
    assert cand is not None

    # Stale HTTP Date on order book: 120 seconds old
    stale_date = email.utils.format_datetime(datetime.now(timezone.utc) - timedelta(seconds=120), usegmt=True)
    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    transport.set_order_book("KX-STALE-1", yes_bids=[("0.30", "50.00")], no_bids=[("0.80", "50.00")], date_header=stale_date)
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(target_quantity=10, max_stale_seconds=60.0, enable_event_prefilter=False),
    )
    qual = qualifier.qualify_candidate(cand, norm_m)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_STALE.value
    assert "exceeds limit" in (qual.rejection_reason or "")


def test_scenario_08_candidate_rejected_for_excessive_cross_leg_fetch_window():
    """Scenario 8: Candidate rejected at Stage C when order-book fetch window exceeds limit."""
    raw_m1 = make_raw_market("KX-FW-1", "EV-FW", yes_ask="0.40", no_ask="0.60")
    raw_m2 = make_raw_market("KX-FW-OTHER", "EV-FW", yes_ask="0.40", no_ask="0.60")
    raw_ev = make_raw_event("EV-FW", [raw_m1, raw_m2])

    norm_m1 = normalize_market(raw_m1)
    norm_m2 = normalize_market(raw_m2)
    norm_ev = normalize_event(raw_ev, (norm_m1, norm_m2))

    screener = BreadthArbitrageScreener()
    res = screener.screen_event_mece_baskets(norm_ev, (norm_m1, norm_m2))
    cands = [r.candidate for r in res if r.candidate is not None]
    cand = [c for c in cands if "yes" in c.strategy_type.lower()][0]

    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    # Add small delay of 0.03s, with max_fetch_window_seconds configured to 0.01s
    transport.set_order_book("KX-FW-1", no_bids=[("0.60", "50.00")], fetch_delay_s=0.03)
    transport.set_order_book("KX-FW-OTHER", no_bids=[("0.60", "50.00")], fetch_delay_s=0.03)
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(target_quantity=10, max_fetch_window_seconds=0.01, enable_event_prefilter=False),
    )
    qual = qualifier.qualify_candidate(cand)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_LEG_SKEW.value
    assert "fetch window" in (qual.rejection_reason or "")


def test_scenario_09_candidate_rejected_for_server_timestamp_skew():
    """Scenario 9: Candidate rejected at Stage C when server timestamp skew between legs exceeds limit."""
    raw_m1 = make_raw_market("KX-SS-1", "EV-SS", yes_ask="0.40", no_ask="0.60")
    raw_m2 = make_raw_market("KX-SS-OTHER", "EV-SS", yes_ask="0.40", no_ask="0.60")
    raw_ev = make_raw_event("EV-SS", [raw_m1, raw_m2])

    norm_m1 = normalize_market(raw_m1)
    norm_m2 = normalize_market(raw_m2)
    norm_ev = normalize_event(raw_ev, (norm_m1, norm_m2))

    screener = BreadthArbitrageScreener()
    res = screener.screen_event_mece_baskets(norm_ev, (norm_m1, norm_m2))
    cands = [r.candidate for r in res if r.candidate is not None]
    cand = [c for c in cands if "yes" in c.strategy_type.lower()][0]

    now = datetime.now(timezone.utc)
    t1_date = email.utils.format_datetime(now - timedelta(seconds=10), usegmt=True)
    t2_date = email.utils.format_datetime(now, usegmt=True)  # 10s skew > 2s limit

    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    transport.set_order_book("KX-SS-1", no_bids=[("0.60", "50.00")], date_header=t1_date)
    transport.set_order_book("KX-SS-OTHER", no_bids=[("0.60", "50.00")], date_header=t2_date)
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(target_quantity=10, max_leg_skew_seconds=2.0, enable_event_prefilter=False),
    )
    qual = qualifier.qualify_candidate(cand)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_LEG_SKEW.value
    assert "timestamp skew" in (qual.rejection_reason or "").lower()


def test_scenario_10_candidate_rejected_after_fees():
    """Scenario 10: Candidate passes Stage B with small gross edge but is rejected after taker fees."""
    # YES ask = 0.49, NO ask = 0.50 -> gross edge = 1 cent/contract (10 cents for 10 contracts)
    raw_m = make_raw_market("KX-TIGHT-1", "EV-TIGHT-1", status="open", yes_ask="0.49", no_ask="0.50")
    raw_ev = make_raw_event("EV-TIGHT-1", [raw_m])

    screener = BreadthArbitrageScreener()
    norm_m = normalize_market(raw_m)
    res = screener.screen_market_binary_parity(norm_m)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    cand = res.candidate
    assert cand is not None
    assert cand.gross_edge_cents == 1

    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    # yes_bid 0.50 creates NO ask 0.50; no_bid 0.51 creates YES ask 0.49
    transport.set_order_book("KX-TIGHT-1", yes_bids=[("0.50", "50.00")], no_bids=[("0.51", "50.00")])
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(target_quantity=10, min_net_profit_cents=5, enable_event_prefilter=False),
    )
    qual = qualifier.qualify_candidate(cand, norm_m)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_UNPROFITABLE_FEES.value
    assert "Unprofitable after fees" in (qual.rejection_reason or "")


def test_scenario_11_candidate_rejected_for_exceeding_maximum_legs():
    """Scenario 11: Candidate rejected at Stage C pre-qualification leg gating (legs > 8)."""
    # 9-leg candidate
    tickers = [f"KX-LEG-{i}" for i in range(1, 10)]
    quotes = {t: "0.10" for t in tickers}
    cand = PreliminaryCandidate(
        candidate_id="cand-9legs",
        strategy_type=ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
        market_tickers=tuple(tickers),
        event_ticker="EV-9LEGS",
        gross_edge_dollars=Decimal("0.10"),
        gross_edge_cents=10,
        estimated_entry_cost_dollars=Decimal("0.90"),
        guaranteed_payout_dollars=Decimal("1.00"),
        screening_timestamp=datetime.now(timezone.utc).isoformat(),
        candidate_rationale="9-leg candidate exceeds size limit",
        validation_limitations=(),
        stable_key="MECE-YES:EV-9LEGS",
        constituent_quotes=quotes,
        quote_timestamp=datetime.now(timezone.utc).isoformat(),
        observation_timestamp=datetime.now(timezone.utc).isoformat(),
    )

    transport = ScenarioMockTransport()
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_candidate_legs=8),
    )
    qual = qualifier.qualify_candidate(cand)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert qual.candidate_leg_count == 9
    assert "exceeds configured maximum allowable limit (8)" in (qual.rejection_reason or "")
    # Ensured zero network requests made
    assert qual.requests_made == 0


def test_scenario_12_candidate_passing_every_execution_aware_check():
    """Scenario 12: Candidate passes every execution-aware check with full depth, freshness, fees, and ROC."""
    raw_m1 = make_raw_market("KX-PERF-1", "EV-PERF", yes_ask="0.20", no_ask="0.80")
    raw_m2 = make_raw_market("KX-PERF-2", "EV-PERF", yes_ask="0.30", no_ask="0.70")
    raw_m3 = make_raw_market("KX-PERF-OTHER", "EV-PERF", yes_ask="0.30", no_ask="0.70")
    raw_ev = make_raw_event("EV-PERF", [raw_m1, raw_m2, raw_m3])

    norm_m1 = normalize_market(raw_m1)
    norm_m2 = normalize_market(raw_m2)
    norm_m3 = normalize_market(raw_m3)
    norm_ev = normalize_event(raw_ev, (norm_m1, norm_m2, norm_m3))

    screener = BreadthArbitrageScreener()
    res = screener.screen_event_mece_baskets(norm_ev, (norm_m1, norm_m2, norm_m3))
    cands = [r.candidate for r in res if r.candidate is not None]
    cand = [c for c in cands if "yes" in c.strategy_type.lower()][0]
    # sum = 0.20 + 0.30 + 0.30 = 0.80 -> 20c gross edge

    now_date = email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)
    transport = ScenarioMockTransport()
    transport.register_event(raw_ev)
    transport.set_order_book("KX-PERF-1", no_bids=[("0.80", "100.00")], date_header=now_date)
    transport.set_order_book("KX-PERF-2", no_bids=[("0.70", "100.00")], date_header=now_date)
    transport.set_order_book("KX-PERF-OTHER", no_bids=[("0.70", "100.00")], date_header=now_date)
    client = KalshiRestClient(transport=transport)

    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(target_quantity=10, max_candidate_legs=8, min_net_profit_cents=5, max_stale_seconds=60.0, enable_event_prefilter=False),
    )
    qual = qualifier.qualify_candidate(cand)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED.value
    assert qual.executable_quantity == 10
    assert qual.candidate_leg_count == 3
    assert qual.gross_edge_cents == 200  # 20c * 10 contracts
    assert qual.net_edge_cents is not None and qual.net_edge_cents > 50
    assert qual.total_fees_cents is not None and qual.total_fees_cents > 0
    assert qual.return_on_capital_pct is not None and qual.return_on_capital_pct > Decimal("0")
    assert qual.rejection_reason is None


# -----------------------------------------------------------------------------
# End-to-End Pipeline & Dashboard Compatibility Integration Test
# -----------------------------------------------------------------------------

def test_end_to_end_pipeline_and_dashboard_verification(tmp_path: Path):
    """Integrated run of the Breadth-First Scanner through discovery, screening, qualification, and dashboard loading.

    Verifies:
    - Preliminary candidates are distinct and not treated as executable opportunities.
    - DashboardDataService accurately represents each stage, count, and rejection reason.
    - Persistence files (state, summaries, candidates, qualifications) conform to schema.
    """
    scanner_dir = tmp_path / "breadth_scanner_test_l"
    scanner_dir.mkdir(parents=True, exist_ok=True)

    transport = ScenarioMockTransport()
    now_dt = datetime.now(timezone.utc)
    now_date = email.utils.format_datetime(now_dt, usegmt=True)
    old_iso = (now_dt - timedelta(hours=2)).isoformat()

    # Define markets representing our scenarios:
    # 1. Valid binary parity
    m1 = make_raw_market("M-BP-VAL", "EV-BP-VAL", status="open", yes_ask="0.20", no_ask="0.70", yes_bid="0.18", no_bid="0.68")
    transport.set_order_book("M-BP-VAL", yes_bids=[("0.30", "50.00")], no_bids=[("0.80", "50.00")], date_header=now_date)

    # 2. Old catalog updated_time (Phase 2F-K unblocked)
    m2 = make_raw_market("M-BP-OLD", "EV-BP-OLD", status="open", yes_ask="0.20", no_ask="0.70", yes_bid="0.18", no_bid="0.68", updated_time=old_iso)
    transport.set_order_book("M-BP-OLD", yes_bids=[("0.30", "50.00")], no_bids=[("0.80", "50.00")], date_header=now_date)

    # 3. Insufficient depth
    m3 = make_raw_market("M-BP-SHALLOW", "EV-BP-SHALLOW", status="open", yes_ask="0.20", no_ask="0.70", yes_bid="0.18", no_bid="0.68")
    transport.set_order_book("M-BP-SHALLOW", yes_bids=[("0.30", "2.00")], no_bids=[("0.80", "2.00")], date_header=now_date)

    # 4. Stale actual order book (120s old)
    stale_date = email.utils.format_datetime(now_dt - timedelta(seconds=120), usegmt=True)
    m4 = make_raw_market("M-BP-STALE", "EV-BP-STALE", status="open", yes_ask="0.20", no_ask="0.70", yes_bid="0.18", no_bid="0.68")
    transport.set_order_book("M-BP-STALE", yes_bids=[("0.30", "50.00")], no_bids=[("0.80", "50.00")], date_header=stale_date)

    # 5. Unprofitable fees (tight 1c edge, uncrossed bids: yes_bid=0.48, no_bid=0.48)
    m5 = make_raw_market("M-BP-TIGHT", "EV-BP-TIGHT", status="open", yes_ask="0.49", no_ask="0.50", yes_bid="0.48", no_bid="0.48")
    transport.set_order_book("M-BP-TIGHT", yes_bids=[("0.50", "50.00")], no_bids=[("0.51", "50.00")], date_header=now_date)

    # 6. Invalid status (closed)
    m6 = make_raw_market("M-BP-CLOSED", "EV-BP-CLOSED", status="closed", yes_ask="0.20", no_ask="0.70", yes_bid="0.18", no_bid="0.68")

    events = [
        make_raw_event("EV-BP-VAL", [m1]),
        make_raw_event("EV-BP-OLD", [m2]),
        make_raw_event("EV-BP-SHALLOW", [m3]),
        make_raw_event("EV-BP-STALE", [m4]),
        make_raw_event("EV-BP-TIGHT", [m5]),
        make_raw_event("EV-BP-CLOSED", [m6]),
    ]
    transport.queue_events_page(events)

    client = KalshiRestClient(transport=transport)
    runner_config = BreadthScannerRunnerConfig(
        output_dir=scanner_dir,
        refresh_interval_seconds=60.0,
        single_pass=True,
        max_cycles=1,
        target_quantity=10,
        min_net_profit_cents=5,
        enable_event_prefilter=False,
        require_full_quantity=True,
    )
    runner = BreadthScannerRunner(client=client, config=runner_config)

    # Run cycle 1
    cycle_summary = runner.run_cycle()

    assert cycle_summary.cycle_number == 1
    assert cycle_summary.is_catalog_complete is True
    # 5 eligible open markets discovered (closed market excluded at discovery eligibility)
    assert cycle_summary.markets_discovered == 5
    # 5 open markets generated preliminary candidates
    assert cycle_summary.preliminary_candidates_count == 5
    assert cycle_summary.candidates_enqueued == 5
    assert cycle_summary.candidates_evaluated == 5

    # Check breakdown of qualification outcomes
    # 2 qualified: M-BP-VAL, M-BP-OLD
    assert cycle_summary.qualified_count == 2
    # 3 rejected: shallow depth, stale order book, unprofitable fees
    assert cycle_summary.rejected_count == 3
    assert cycle_summary.error_count == 0

    # Verify persistence files created
    state_file = scanner_dir / "scanner_state.json"
    summaries_file = scanner_dir / "scan_summaries.jsonl"
    candidates_file = scanner_dir / "candidate_history.jsonl"
    qualifications_file = scanner_dir / "qualification_history.jsonl"

    assert state_file.exists()
    assert summaries_file.exists()
    assert candidates_file.exists()
    assert qualifications_file.exists()

    # -------------------------------------------------------------------------
    # Dashboard Data Service Verification
    # -------------------------------------------------------------------------
    service = DashboardDataService()

    # 1. Status
    status = service.get_breadth_scanner_status(scanner_dir)
    assert status["session_id"] is not None
    assert status["total_cycles_count"] == 1
    assert status["successful_cycles_count"] == 1
    assert status["total_preliminary_candidates_generated"] == 5
    assert status["qualified_candidates_count"] == 2
    assert "status" in status

    # 2. Summaries
    summaries = service.get_breadth_scanner_summaries(scanner_dir)
    assert len(summaries) == 1
    s0 = summaries[0]
    assert s0["cycle_number"] == 1
    assert s0["qualified_count"] == 2
    assert s0["rejected_count"] == 3
    assert s0["preliminary_candidates_count"] == 5

    # 3. Preliminary Candidates
    candidates = service.get_breadth_scanner_candidates(scanner_dir)
    assert len(candidates) == 5
    # Verify preliminary candidates are NOT marked qualified or executable
    for c in candidates:
        assert "is_qualified" not in c or c.get("is_qualified") is not True
        assert "observation_timestamp" in c
        assert "gross_edge_cents" in c

    # 4. Qualifications
    quals = service.get_breadth_scanner_qualifications(scanner_dir)
    assert len(quals) == 5

    # Filter by outcome
    qual_only = service.get_breadth_scanner_qualifications(scanner_dir, outcome="QUALIFIED")
    assert len(qual_only) == 2
    for q in qual_only:
        assert q["is_qualified"] is True
        assert q["outcome"] == "QUALIFIED"
        assert q["executable_quantity"] == 10

    rej_depth = service.get_breadth_scanner_qualifications(scanner_dir, outcome="REJECTED_INSUFFICIENT_DEPTH")
    assert len(rej_depth) == 1
    assert rej_depth[0]["is_qualified"] is False
    assert "Insufficient depth" in rej_depth[0]["rejection_reason"]

    rej_stale = service.get_breadth_scanner_qualifications(scanner_dir, outcome="REJECTED_STALE")
    assert len(rej_stale) == 1
    assert rej_stale[0]["is_qualified"] is False
    assert "exceeds limit" in rej_stale[0]["rejection_reason"]

    rej_fees = service.get_breadth_scanner_qualifications(scanner_dir, outcome="REJECTED_UNPROFITABLE_FEES")
    assert len(rej_fees) == 1
    assert rej_fees[0]["is_qualified"] is False
    assert "Unprofitable after fees" in rej_fees[0]["rejection_reason"]

    # Filter by is_qualified boolean
    qualified_items = service.get_breadth_scanner_qualifications(scanner_dir, is_qualified=True)
    assert len(qualified_items) == 2

    rejected_items = service.get_breadth_scanner_qualifications(scanner_dir, is_qualified=False)
    assert len(rejected_items) == 3

    # 5. Strategy Breakdown
    breakdown = service.get_breadth_scanner_strategy_breakdown(scanner_dir)
    assert breakdown["has_candidate_records"] is True
    assert breakdown["has_qualification_records"] is True
    assert breakdown["preliminary_by_strategy"]["binary_parity"] == 5
    bp_qual = breakdown["qualification_by_strategy"]["binary_parity"]
    assert bp_qual["qualified"] == 2
    assert bp_qual["rejected"] == 3
    assert bp_qual["outcomes"]["QUALIFIED"] == 2
    assert bp_qual["outcomes"]["REJECTED_INSUFFICIENT_DEPTH"] == 1
    assert bp_qual["outcomes"]["REJECTED_STALE"] == 1
    assert bp_qual["outcomes"]["REJECTED_UNPROFITABLE_FEES"] == 1
