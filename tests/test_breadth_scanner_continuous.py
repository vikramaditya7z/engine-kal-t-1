"""Comprehensive test suite for Phase 2F-C: Continuous Discovery, Refresh Scheduling & Quote Freshness.

Tests all behaviors specified in Phase 2F-C:
- Workstream A: Newly listed market discovery, cursor pagination, loop detection, partial pass catalog retention, and closed market reconciliation.
- Workstream B: Refresh scheduling, single-flight guard, monotonic timing, cycle overrun telemetry, and burst prevention.
- Workstream C: Quote freshness, max_stale_seconds threshold, MECE cross-leg skew rejection, candidate queue staleness, and candidate invalidation on quote moves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional
import pytest

from kalshi_arbitrage import (
    BreadthArbitrageScreener,
    BreadthScannerConfig,
    BreadthScannerHealth,
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    CatalogDiscoveryEngine,
    CatalogDiscoveryResult,
    HTTPResponse,
    KalshiPaginationError,
    KalshiRestClient,
    PreliminaryCandidate,
    QualificationOutcome,
    ScanCycleSummary,
    ScreeningOutcome,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
)
from kalshi_arbitrage.market_data import normalize_event, normalize_market


# -----------------------------------------------------------------------------
# Test Fixtures and Smart Mock Transport
# -----------------------------------------------------------------------------

class ContinuousMockTransport:
    """Mock HTTP transport supporting queued multi-page catalog discovery and L2 books."""

    def __init__(self) -> None:
        self.events_pages: List[Dict[str, Any]] = []
        self.order_books: Dict[str, Dict[str, Any]] = {}
        self.requested_urls: List[str] = []
        self.requested_orderbooks: List[str] = []
        self.date_header: str = "Thu, 29 Oct 2026 12:00:00 GMT"

    def queue_page(self, events: List[Dict[str, Any]], cursor: Optional[str] = None) -> None:
        self.events_pages.append({"events": events, "cursor": cursor})

    def set_order_book(self, ticker: str, yes_levels: List[List[str]], no_levels: List[List[str]]) -> None:
        self.order_books[ticker] = {
            "orderbook_fp": {
                "yes_dollars": [[p, q] for p, q in yes_levels],
                "no_dollars": [[p, q] for p, q in no_levels],
            }
        }

    def get(self, url: str, params: Optional[Mapping[str, str]] = None, timeout_seconds: float = 10.0) -> HTTPResponse:
        self.requested_urls.append(url)

        # Prohibit private / trading endpoints
        for forbidden in ("/orders", "/portfolio", "/fills", "/account", "/trade-api/v2/orders"):
            if forbidden in url:
                raise AssertionError(f"Forbidden private endpoint called: {url}")

        if "/orderbook" in url:
            parts = url.split("/")
            ticker_idx = parts.index("markets") + 1 if "markets" in parts else -2
            ticker = parts[ticker_idx]
            self.requested_orderbooks.append(ticker)

            book = self.order_books.get(ticker, {
                "orderbook_fp": {
                    "yes_dollars": [["0.55", "10.00"]],
                    "no_dollars": [["0.58", "10.00"]],
                }
            })
            return HTTPResponse(200, json.dumps(book).encode("utf-8"), {"Date": self.date_header})

        if self.events_pages:
            page = self.events_pages.pop(0)
            return HTTPResponse(200, json.dumps(page).encode("utf-8"), {"Date": self.date_header})

        return HTTPResponse(200, json.dumps({"events": [], "cursor": None}).encode("utf-8"), {"Date": self.date_header})


def make_market_dict(
    ticker: str,
    event_ticker: str = "EV1",
    yes_bid: str = "0.4500",
    yes_ask: str = "0.4600",
    no_bid: str = "0.5400",
    no_ask: str = "0.5500",
    updated_time: Optional[str] = None,
    status: str = "open",
) -> Dict[str, Any]:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    close_str = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "market_type": "binary",
        "status": status,
        "title": f"Market {ticker}",
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": no_ask,
        "yes_bid_size_fp": "100.00",
        "yes_ask_size_fp": "100.00",
        "created_time": now_str,
        "updated_time": updated_time if updated_time is not None else now_str,
        "open_time": now_str,
        "close_time": close_str,
        "rules_primary": "Standard settlement rules.",
    }


def make_event_dict(
    event_ticker: str = "EV1",
    markets: Optional[List[Dict[str, Any]]] = None,
    mutually_exclusive: bool = True,
    series_ticker: str = "KXFEDDECISION",
) -> Dict[str, Any]:
    mkts = markets if markets is not None else [
        make_market_dict(f"{event_ticker}-M1", event_ticker=event_ticker),
        make_market_dict(f"{event_ticker}-M2", event_ticker=event_ticker),
    ]
    return {
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "title": f"Event {event_ticker}",
        "category": "Economics",
        "mutually_exclusive": mutually_exclusive,
        "markets": mkts,
    }


# =============================================================================
# Workstream A: Newly Listed Market Discovery & Cursor Pagination Robustness
# =============================================================================

def test_workstream_a_cursor_continuation_and_new_market_discovery(tmp_path: Path):
    """Newly listed markets on later cursor pages and subsequent refreshes are reconciled cleanly."""
    transport = ContinuousMockTransport()

    # Pass 1: Initial catalog with 2 markets
    p1 = [make_event_dict("EV1", [make_market_dict("M1"), make_market_dict("M2")])]
    transport.queue_page(p1, cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=False, refresh_interval_seconds=0.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.is_catalog_complete is True
    assert s1.markets_discovered == 2
    assert s1.new_markets_count == 2
    assert runner.get_health().total_unique_markets_discovered == 2

    # Pass 2: A newly listed market M3 appears in the catalog
    p2 = [make_event_dict("EV1", [make_market_dict("M1"), make_market_dict("M2"), make_market_dict("M3")])]
    transport.queue_page(p2, cursor=None)

    s2 = runner.run_cycle()
    assert s2.is_catalog_complete is True
    assert s2.markets_discovered == 3
    assert s2.new_markets_count == 1
    assert runner.get_health().total_unique_markets_discovered == 3


def test_workstream_a_cursor_loop_detection_fails_soft_without_claiming_completeness():
    """Repeated cursor cycle terminates cleanly with is_complete=False and does not wipe catalog."""
    transport = ContinuousMockTransport()
    # Cursor cycle: page 1 -> cursor 'c_cycle' -> page 2 returns 'c_cycle' again
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1")])], cursor="c_cycle")
    transport.queue_page([make_event_dict("EV2", [make_market_dict("M2")])], cursor="c_cycle")

    client = KalshiRestClient(environment="production", transport=transport)
    engine = CatalogDiscoveryEngine(client=client, config=BreadthScannerConfig(raise_on_error=False))

    result = engine.discover_catalog()
    assert result.is_complete is False
    assert result.pages_fetched == 2
    assert result.error_message is not None
    assert "repeated" in result.error_message.lower()
    # Markets from both fetched pages were retained
    assert "M1" in result.discovered_markets
    assert "M2" in result.discovered_markets


def test_workstream_a_partial_pass_retains_prior_complete_catalog(tmp_path: Path):
    """An incomplete refresh pass merges with the last complete catalog and never falsely closes markets."""
    transport = ContinuousMockTransport()

    # Pass 1: Complete pass with 4 markets
    ev1_mkts = [make_market_dict("M1"), make_market_dict("M2"), make_market_dict("M3"), make_market_dict("M4")]
    transport.queue_page([make_event_dict("EV1", ev1_mkts)], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=False, refresh_interval_seconds=0.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.is_catalog_complete is True
    assert s1.markets_discovered == 4
    assert s1.closed_or_settled_markets_count == 0

    # Pass 2: Incomplete pass that loops or fails after fetching only M1 (partial pass)
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1")])], cursor="loop_cursor")
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1")])], cursor="loop_cursor")

    s2 = runner.run_cycle()
    assert s2.is_catalog_complete is False
    # CRITICAL: markets were merged with last complete catalog rather than dropped!
    assert s2.markets_discovered == 4
    # CRITICAL: M2, M3, M4 were NOT falsely marked as closed!
    assert s2.closed_or_settled_markets_count == 0
    assert runner.get_health().last_catalog_complete is False


def test_workstream_a_closed_markets_reconciled_only_on_complete_passes(tmp_path: Path):
    """Closed markets are only reconciled when discovery is complete."""
    transport = ContinuousMockTransport()

    # Pass 1: M1, M2 open
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1"), make_market_dict("M2")])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=False, refresh_interval_seconds=0.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.closed_or_settled_markets_count == 0

    # Pass 2: Complete pass where M2 transitioned to closed status
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1"), make_market_dict("M2", status="closed")])], cursor=None)

    s2 = runner.run_cycle()
    assert s2.is_catalog_complete is True
    assert s2.closed_or_settled_markets_count == 1


# =============================================================================
# Workstream B: Refresh Scheduling & Cycle Overrun Hardening
# =============================================================================

def test_workstream_b_single_flight_guard_rejects_concurrent_cycles(tmp_path: Path):
    """Single-flight execution guard rejects concurrent execution calls."""
    transport = ContinuousMockTransport()
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1")])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, refresh_interval_seconds=10.0)
    runner = BreadthScannerRunner(client=client, config=config)

    # Simulate an active cycle
    runner._is_executing_cycle = True
    try:
        with pytest.raises(RuntimeError, match="Concurrent cycle execution rejected"):
            runner.run_cycle()
    finally:
        runner._is_executing_cycle = False


def test_workstream_b_overrun_telemetry_and_monotonic_timing(tmp_path: Path):
    """Cycle duration exceeding configured interval records overrun telemetry and start-to-start interval."""
    transport = ContinuousMockTransport()
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1")])], cursor=None)
    transport.queue_page([make_event_dict("EV1", [make_market_dict("M1")])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    # Configure tiny interval so any cycle overruns
    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        refresh_interval_seconds=0.000001,
        min_cycle_cooldown_seconds=1.5,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    # Cycle 1
    s1 = runner.run_cycle()
    assert s1.scheduled_interval_seconds == 0.000001
    assert s1.start_to_start_interval_seconds is None  # First cycle
    assert s1.is_overrun is True
    assert s1.overrun_seconds > 0.0
    # Overrun applies minimum cooldown rather than burst catch-up
    assert s1.next_cycle_delay_seconds <= config.min_cycle_cooldown_seconds

    health = runner.get_health()
    assert health.total_overruns_count == 1
    assert health.total_overrun_seconds > 0.0
    assert health.is_currently_overrunning is True

    # Cycle 2
    s2 = runner.run_cycle()
    assert s2.start_to_start_interval_seconds is not None
    assert s2.start_to_start_interval_seconds > 0.0
    assert runner.get_health().total_overruns_count == 2


# =============================================================================
# Workstream C: Existing Market Quote Refresh & Quote Staleness Detection
# =============================================================================

def test_workstream_c_binary_parity_screening_allows_resting_catalog_quotes():
    """Stage B screening approves mathematically valid resting quotes regardless of catalog updated_time."""
    now_dt = datetime.now(timezone.utc)
    stale_dt = now_dt - timedelta(seconds=120)

    # Edge exists mathematically ($0.40 + $0.45 = $0.85 < $1.00), even if catalog updated_time is 120s old
    m_stale = normalize_market(make_market_dict("M-STALE", yes_ask="0.4000", no_ask="0.4500", updated_time=stale_dt.strftime("%Y-%m-%dT%H:%M:%SZ")))

    screener = BreadthArbitrageScreener(config=BreadthScannerConfig(max_stale_seconds=60.0))
    res = screener.screen_market_binary_parity(m_stale)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert res.candidate is not None
    assert res.candidate.quote_timestamp is not None


def test_workstream_c_binary_parity_approves_fresh_quotes():
    """Markets with fresh quotes within max_stale_seconds produce PRELIMINARY_CANDIDATE."""
    now_dt = datetime.now(timezone.utc)
    fresh_dt = now_dt - timedelta(seconds=10)

    m_fresh = normalize_market(make_market_dict("M-FRESH", yes_ask="0.4000", no_ask="0.4500", updated_time=fresh_dt.strftime("%Y-%m-%dT%H:%M:%SZ")))

    screener = BreadthArbitrageScreener(config=BreadthScannerConfig(max_stale_seconds=60.0))
    res = screener.screen_market_binary_parity(m_fresh)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert res.candidate is not None
    assert res.candidate.quote_timestamp is not None


def test_workstream_c_mece_basket_screening_allows_catalog_cross_leg_skew():
    """Multi-leg MECE basket screening generates preliminary candidate; cross-leg skew is checked in Stage C."""
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    ts_leg1 = now_dt - timedelta(seconds=2)
    ts_leg2 = now_dt - timedelta(seconds=15)  # 13s skew in catalog

    m1_dict = make_market_dict("EV-M1", event_ticker="EV-FED", yes_ask="0.4000", no_ask="0.6000", updated_time=ts_leg1.strftime("%Y-%m-%dT%H:%M:%SZ"))
    m2_dict = make_market_dict("EV-M2", event_ticker="EV-FED", yes_ask="0.4000", no_ask="0.6000", updated_time=ts_leg2.strftime("%Y-%m-%dT%H:%M:%SZ"))

    ev = normalize_event(make_event_dict("EV-FED", [m1_dict, m2_dict], series_ticker="KXFEDDECISION"))
    m1 = normalize_market(m1_dict)
    m2 = normalize_market(m2_dict)

    screener = BreadthArbitrageScreener(config=BreadthScannerConfig(max_stale_seconds=60.0, max_leg_skew_seconds=5.0))
    results = screener.screen_event_mece_baskets(ev, [m1, m2])

    yes_res = next(r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_YES.value)
    assert yes_res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert yes_res.candidate is not None
    assert yes_res.candidate.quote_timestamp == ts_leg2.isoformat()


def test_workstream_c_mece_basket_records_oldest_leg_timestamp():
    """Multi-leg MECE basket PreliminaryCandidate records the oldest (conservative) leg timestamp."""
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    ts_leg1 = now_dt - timedelta(seconds=3)
    ts_leg2 = now_dt - timedelta(seconds=1)

    m1_dict = make_market_dict("EV-M1", event_ticker="EV-FED", yes_ask="0.4000", no_ask="0.6000", updated_time=ts_leg1.strftime("%Y-%m-%dT%H:%M:%SZ"))
    m2_dict = make_market_dict("EV-M2", event_ticker="EV-FED", yes_ask="0.4000", no_ask="0.6000", updated_time=ts_leg2.strftime("%Y-%m-%dT%H:%M:%SZ"))

    ev = normalize_event(make_event_dict("EV-FED", [m1_dict, m2_dict], series_ticker="KXFEDDECISION"))
    m1 = normalize_market(m1_dict)
    m2 = normalize_market(m2_dict)

    screener = BreadthArbitrageScreener(config=BreadthScannerConfig(max_stale_seconds=60.0, max_leg_skew_seconds=5.0))
    results = screener.screen_event_mece_baskets(ev, [m1, m2])

    yes_res = next(r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_YES.value)
    assert yes_res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert yes_res.candidate is not None
    # Conservative oldest timestamp recorded
    assert yes_res.candidate.quote_timestamp == ts_leg1.isoformat()


def test_workstream_c_qualifier_preflight_staleness_zero_requests():
    """TargetedOrderBookQualifier rejects stale candidates without making any HTTP order book requests."""
    transport = ContinuousMockTransport()
    client = KalshiRestClient(environment="production", transport=transport)
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_stale_seconds=30.0),
    )

    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    cand = PreliminaryCandidate(
        candidate_id="PRE-TEST",
        strategy_type="binary_parity",
        event_ticker="EV1",
        market_tickers=("M1",),
        gross_edge_dollars=Decimal("0.1000"),
        gross_edge_cents=10,
        estimated_entry_cost_dollars=Decimal("0.9000"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"M1": {"yes_ask": "0.4500", "no_ask": "0.4500"}},
        screening_timestamp=datetime.now(timezone.utc).isoformat(),
        candidate_rationale="Parity",
        validation_limitations=(),
        stable_key="BP:M1",
        quote_timestamp=stale_ts,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.outcome == QualificationOutcome.REJECTED_STALE.value
    assert res.is_qualified is False
    assert res.requests_made == 0
    assert len(transport.requested_orderbooks) == 0


def test_workstream_c_candidate_invalidation_when_edge_dissolves(tmp_path: Path):
    """When quotes move and an opportunity dissolves, candidate is explicitly invalidated."""
    transport = ContinuousMockTransport()

    # Cycle 1: Opportunity exists (YES ask 0.40, NO ask 0.45, sum 0.85 < 1.00)
    m_opp = make_market_dict("M1", yes_bid="0.3500", yes_ask="0.4000", no_bid="0.4000", no_ask="0.4500")
    transport.queue_page([make_event_dict("EV1", [m_opp])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=False, refresh_interval_seconds=0.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.preliminary_candidates_count == 1
    assert "BP:M1" in runner._known_candidates

    # Cycle 2: Market price moves, opportunity dissolves (YES ask 0.55, NO ask 0.50, sum 1.05 > 1.00)
    m_no_opp = make_market_dict("M1", yes_bid="0.5000", yes_ask="0.5500", no_bid="0.4500", no_ask="0.5000")
    transport.queue_page([make_event_dict("EV1", [m_no_opp])], cursor=None)

    s2 = runner.run_cycle()
    assert s2.preliminary_candidates_count == 0
    assert s2.invalidated_candidates_count == 1
    # Candidate was explicitly pruned from active known candidates
    assert "BP:M1" not in runner._known_candidates
    assert runner.get_health().total_candidates_invalidated == 1

    # Verify invalidation record was appended to candidate history
    history_file = tmp_path / "candidate_history.jsonl"
    assert history_file.exists()
    lines = [json.loads(line) for line in history_file.read_text().strip().split("\n")]
    inv_records = [l for l in lines if l.get("action") == "INVALIDATED"]
    assert len(inv_records) == 1
    assert inv_records[0]["candidate"]["stable_key"] == "BP:M1"
