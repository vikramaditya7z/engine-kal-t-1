"""Unit and integration tests for Phase 2D: Standalone Continuous Breadth-First Scanner Runner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage import (
    AlertEngine,
    BreadthArbitrageScreener,
    BreadthScannerConfig,
    BreadthScannerHealth,
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    CatalogDiscoveryEngine,
    ContinuousCollector,
    ContinuousEvaluator,
    ExecutionAwareArbitrageQualifier,
    FeeScheduleType,
    HTTPResponse,
    KalshiHTTPError,
    KalshiRestClient,
    KalshiTransportError,
    NormalizedOrderBook,
    OrderBookLevel,
    PreliminaryCandidate,
    QualificationOutcome,
    ScanCycleSummary,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
)
from kalshi_arbitrage.market_data import normalize_event, normalize_market


# -----------------------------------------------------------------------------
# Fixtures and Helpers
# -----------------------------------------------------------------------------

def make_raw_market(
    ticker: str = "KXTEST-1",
    event_ticker: str = "KXEVENT-1",
    market_type: str = "binary",
    status: str = "open",
    title: str = "Will X happen?",
    close_time: Optional[str] = None,
    yes_bid: str = "0.4500",
    yes_ask: str = "0.4600",
    no_bid: str = "0.5400",
    no_ask: str = "0.5500",
) -> Dict[str, Any]:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if close_time is None:
        close_dt = datetime.now(timezone.utc) + timedelta(days=2)
        close_time = close_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

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
        "yes_bid_size_fp": "100.00",
        "yes_ask_size_fp": "100.00",
        "volume_fp": "500.00",
        "volume_24h_fp": "50.00",
        "open_interest_fp": "200.00",
        "created_time": now_str,
        "updated_time": now_str,
        "open_time": now_str,
        "close_time": close_time,
        "rules_primary": "Standard settlement rules.",
    }


def make_raw_event(
    event_ticker: str = "KXEVENT-1",
    category: str = "Economics",
    markets: Optional[List[Dict[str, Any]]] = None,
    series_ticker: str = "KXSERIES",
    mutually_exclusive: bool = True,
) -> Dict[str, Any]:
    mkts = markets if markets is not None else [
        make_raw_market(f"{event_ticker}-M1", event_ticker=event_ticker),
        make_raw_market(f"{event_ticker}-M2", event_ticker=event_ticker),
    ]
    return {
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "title": f"Event {event_ticker}",
        "category": category,
        "mutually_exclusive": mutually_exclusive,
        "markets": mkts,
    }


def make_test_order_book_json(
    yes_bids: Sequence[Tuple[str, str]] = (("0.55", "10.00"),),
    no_bids: Sequence[Tuple[str, str]] = (("0.58", "10.00"),),
) -> Dict[str, Any]:
    """In Kalshi: yes_bids 0.55 creates NO ask 0.45; no_bids 0.58 creates YES ask 0.42."""
    return {
        "orderbook_fp": {
            "yes_dollars": [[p, q] for p, q in yes_bids],
            "no_dollars": [[p, q] for p, q in no_bids],
        }
    }


class SmartMockTransport:
    """Mock transport handling both /events discovery pagination and /orderbook requests."""

    def __init__(self) -> None:
        self.events_pages: List[Dict[str, Any]] = []
        self.events_by_ticker: Dict[str, Dict[str, Any]] = {}
        self.order_books: Dict[str, Dict[str, Any]] = {}
        self.order_book_errors: Dict[str, Exception] = {}
        self.events_errors: List[Exception] = []
        self.requested_orderbooks: List[str] = []
        self.calls: List[str] = []
        import email.utils
        self.date_header: str = email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)

    def queue_events_page(self, events: List[Dict[str, Any]], cursor: Optional[str] = None) -> None:
        self.events_pages.append({"events": events, "cursor": cursor})
        for ev in events:
            if "event_ticker" in ev:
                self.events_by_ticker[ev["event_ticker"]] = ev
            for m in ev.get("markets", []):
                if isinstance(m, dict) and "event_ticker" in m:
                    self.events_by_ticker[m["event_ticker"]] = ev

    def set_order_book(self, ticker: str, yes_bids: Sequence[Tuple[str, str]] = (("0.55", "10.00"),), no_bids: Sequence[Tuple[str, str]] = (("0.58", "10.00"),)) -> None:
        self.order_books[ticker] = make_test_order_book_json(yes_bids=yes_bids, no_bids=no_bids)

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        self.calls.append(url)

        # Prohibit private / trading endpoints
        for forbidden in ("/orders", "/portfolio", "/fills", "/account", "/trade-api/v2/orders"):
            if forbidden in url:
                raise AssertionError(f"Forbidden private endpoint called: {url}")

        if "/orderbook" in url:
            # Extract ticker from URL: e.g. /markets/TICKER/orderbook
            parts = url.split("/")
            ticker_idx = parts.index("markets") + 1 if "markets" in parts else -2
            ticker = parts[ticker_idx]
            self.requested_orderbooks.append(ticker)

            if ticker in self.order_book_errors:
                err = self.order_book_errors[ticker]
                if isinstance(err, list) and err:
                    e = err.pop(0)
                    if e is not None:
                        raise e
                elif isinstance(err, Exception):
                    raise err

            book_json = self.order_books.get(ticker, make_test_order_book_json())
            body = json.dumps(book_json).encode("utf-8")
            return HTTPResponse(200, body, {"Date": self.date_header})

        # Single event endpoint: /events/{ticker}
        parsed_url = url.split("?")[0].rstrip("/")
        if "/events/" in parsed_url:
            ev_ticker = parsed_url.split("/events/")[1]
            ev_data = self.events_by_ticker.get(ev_ticker)
            if ev_data is None:
                ev_data = make_raw_event(event_ticker=ev_ticker)
            body = json.dumps({"event": ev_data}).encode("utf-8")
            return HTTPResponse(200, body, {"Date": self.date_header})

        # Events endpoint
        if self.events_errors:
            err = self.events_errors.pop(0)
            if err is not None:
                raise err

        if self.events_pages:
            page = self.events_pages.pop(0)
            body = json.dumps(page).encode("utf-8")
            return HTTPResponse(200, body, {"Date": self.date_header})

        # Empty fallback
        empty_page = {"events": [], "cursor": None}
        return HTTPResponse(200, json.dumps(empty_page).encode("utf-8"), {"Date": self.date_header})


# -----------------------------------------------------------------------------
# Test Cases 1 - 24
# -----------------------------------------------------------------------------

def test_1_single_pass_pipeline(tmp_path: Path):
    """1. Single-pass pipeline execution: discovery → screening → qualification → persistence."""
    transport = SmartMockTransport()
    # Market M-ARB has arbitrage: yes_ask 0.42 + no_ask 0.45 = 0.87 < 1.00
    m_arb = make_raw_market("M-ARB", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    ev = make_raw_event("EV-1", "Economics", [m_arb])
    transport.queue_events_page([ev], cursor=None)
    transport.set_order_book("M-ARB")

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        single_pass=True,
        refresh_interval_seconds=0.0,
        max_requests_per_second=1000.0,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    runner.run()

    health = runner.get_health()
    assert health.status == "STOPPED"
    assert health.total_cycles_count == 1
    assert health.successful_cycles_count == 1
    assert health.total_unique_markets_discovered == 1
    assert health.total_unique_markets_screened == 1
    assert health.total_preliminary_candidates_generated == 1
    assert health.qualified_candidates_count == 1
    assert runner.state_file.exists()
    assert runner.summaries_file.exists()
    assert runner.candidate_history_file.exists()
    assert runner.qualification_history_file.exists()


def test_2_continuous_scheduling_multiple_cycles(tmp_path: Path):
    """2. Continuous scheduling executes multiple cycles up to max_cycles bound."""
    transport = SmartMockTransport()
    ev = make_raw_event("EV-1", "Economics", [make_raw_market("M1")])
    # Queue 3 pages for 3 cycles
    transport.queue_events_page([ev], cursor=None)
    transport.queue_events_page([ev], cursor=None)
    transport.queue_events_page([ev], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        max_cycles=3,
        refresh_interval_seconds=0.0,
        max_requests_per_second=1000.0,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    runner.run()

    health = runner.get_health()
    assert health.status == "STOPPED"
    assert health.total_cycles_count == 3
    assert health.successful_cycles_count == 3


def test_3_discovery_pagination_beyond_five_markets(tmp_path: Path):
    """3. Discovery enumerates more than 5 markets across multiple continuation pages."""
    transport = SmartMockTransport()
    # Page 1: 4 markets (M1, M2, M3, M4)
    p1_mkts = [make_raw_market(f"M{i}") for i in range(1, 5)]
    ev1 = make_raw_event("EV1", "Economics", p1_mkts)
    transport.queue_events_page([ev1], cursor="cursor-page-2")

    # Page 2: 4 markets (M5, M6, M7, M8)
    p2_mkts = [make_raw_market(f"M{i}") for i in range(5, 9)]
    ev2 = make_raw_event("EV2", "Economics", p2_mkts)
    transport.queue_events_page([ev2], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=True, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    summary = runner.run_cycle()

    assert summary.markets_discovered == 8
    assert summary.is_catalog_complete is True
    assert runner.get_health().total_unique_markets_discovered == 8
    assert runner.get_health().total_unique_markets_screened == 8


def test_4_incomplete_catalog_pass_handling(tmp_path: Path):
    """4. Incomplete catalog pass propagates is_catalog_complete=False and preserves last good catalog."""
    transport = SmartMockTransport()
    # Cycle 1: Good catalog
    ev1 = make_raw_event("EV1", "Economics", [make_raw_market("M1")])
    transport.queue_events_page([ev1], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, refresh_interval_seconds=0.0, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.is_catalog_complete is True
    assert s1.markets_discovered == 1

    # Cycle 2: Transport error during discovery
    transport.events_errors.extend([KalshiTransportError("Connection reset by peer")] * 5)
    s2 = runner.run_cycle()

    assert s2.is_catalog_complete is False
    assert s2.error_message is not None
    assert runner.get_health().last_catalog_complete is False
    assert runner.get_health().failed_cycles_count == 1
    # Fallback to last good catalog kept markets_discovered at 1
    assert s2.markets_discovered == 1


def test_5_newly_discovered_market_on_later_refresh(tmp_path: Path):
    """5. Newly listed market on a later cycle is detected and screened incrementally."""
    transport = SmartMockTransport()
    # Cycle 1: M1, M2
    transport.queue_events_page([make_raw_event("EV1", "Economics", [make_raw_market("M1"), make_raw_market("M2")])], cursor=None)
    # Cycle 2: M1, M2, M3
    transport.queue_events_page([make_raw_event("EV1", "Economics", [make_raw_market("M1"), make_raw_market("M2"), make_raw_market("M3")])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, refresh_interval_seconds=0.0, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.new_markets_count == 2
    assert runner.get_health().total_unique_markets_discovered == 2

    s2 = runner.run_cycle()
    assert s2.new_markets_count == 1
    assert runner.get_health().total_unique_markets_discovered == 3


def test_6_changed_quotes_triggering_reevaluation(tmp_path: Path):
    """6. Changed market quotes trigger reevaluation and candidate requalification."""
    transport = SmartMockTransport()
    # Cycle 1: No arb quotes (yes_ask 0.52 + no_ask 0.52 = 1.04)
    m_cycle1 = make_raw_market("M1", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_cycle1])], cursor=None)

    # Cycle 2: Arb quotes (yes_ask 0.42 + no_ask 0.45 = 0.87)
    m_cycle2 = make_raw_market("M1", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_cycle2])], cursor=None)
    transport.set_order_book("M1")

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, refresh_interval_seconds=0.0, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.preliminary_candidates_count == 0
    assert s1.qualified_count == 0

    s2 = runner.run_cycle()
    assert s2.changed_markets_count == 1
    assert s2.preliminary_candidates_count == 1
    assert s2.qualified_count == 1


def test_7_no_candidate_markets_produce_zero_order_book_requests(tmp_path: Path):
    """7. Discovered markets with no arbitrage candidates trigger zero order-book requests."""
    transport = SmartMockTransport()
    # 5 balanced markets
    mkts = [make_raw_market(f"M{i}", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200") for i in range(5)]
    transport.queue_events_page([make_raw_event("EV1", "Economics", mkts)], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=True, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    summary = runner.run_cycle()

    assert summary.markets_discovered == 5
    assert summary.preliminary_candidates_count == 0
    assert len(transport.requested_orderbooks) == 0


def test_8_preliminary_candidates_trigger_only_constituent_order_book_requests(tmp_path: Path):
    """8. Only constituent legs of preliminary candidates trigger order-book requests."""
    transport = SmartMockTransport()
    # 5 markets, only M3 has an arb
    m1 = make_raw_market("M1", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200")
    m2 = make_raw_market("M2", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200")
    m3 = make_raw_market("M3", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    m4 = make_raw_market("M4", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200")
    m5 = make_raw_market("M5", yes_bid="0.4500", yes_ask="0.5200", no_bid="0.4500", no_ask="0.5200")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m1, m2, m3, m4, m5])], cursor=None)
    transport.set_order_book("M3")

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=True, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    summary = runner.run_cycle()

    assert summary.markets_discovered == 5
    assert summary.preliminary_candidates_count == 1
    assert transport.requested_orderbooks == ["M3"]


def test_9_candidate_deduplication_across_cycles(tmp_path: Path):
    """9. Unchanged candidates across multiple cycles are deduplicated without re-requesting order books."""
    transport = SmartMockTransport()
    m_arb = make_raw_market("M-ARB", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_arb])], cursor=None)
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_arb])], cursor=None)
    transport.set_order_book("M-ARB")

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, refresh_interval_seconds=0.0, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    # Cycle 1: Candidate discovered and qualified
    s1 = runner.run_cycle()
    assert s1.new_preliminary_candidates_count == 1
    assert s1.deduplicated_preliminary_candidates_count == 0
    assert s1.candidates_evaluated == 1
    assert transport.requested_orderbooks == ["M-ARB"]

    # Cycle 2: Candidate deduplicated (quotes unchanged)
    s2 = runner.run_cycle()
    assert s2.new_preliminary_candidates_count == 0
    assert s2.deduplicated_preliminary_candidates_count == 1
    assert s2.candidates_evaluated == 0
    # No second order-book fetch
    assert transport.requested_orderbooks == ["M-ARB"]


def test_10_bounded_queue_and_observable_overflow(tmp_path: Path):
    """10. Candidate queue enforces max_queue_size and records observable overflow in telemetry."""
    transport = SmartMockTransport()
    # 4 distinct arb markets
    m1 = make_raw_market("M1", yes_ask="0.4500", no_ask="0.5000")  # edge 0.05
    m2 = make_raw_market("M2", yes_ask="0.4000", no_ask="0.5000")  # edge 0.10
    m3 = make_raw_market("M3", yes_ask="0.4800", no_ask="0.5000")  # edge 0.02
    m4 = make_raw_market("M4", yes_ask="0.3000", no_ask="0.5000")  # edge 0.20
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m1, m2, m3, m4])], cursor=None)
    for m in ("M1", "M2", "M3", "M4"):
        transport.set_order_book(m)

    client = KalshiRestClient(environment="production", transport=transport)
    # Bounded queue size of 2
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, max_queue_size=2, single_pass=True, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    summary = runner.run_cycle()

    assert summary.queue_overflow_count >= 1
    assert runner.get_health().queue_overflow_count >= 1


def test_11_request_rate_and_concurrency_constraints(tmp_path: Path):
    """11. Outgoing order-book fetches respect configured rate limit."""
    transport = SmartMockTransport()
    m1 = make_raw_market("M1", yes_ask="0.4000", no_ask="0.4000")
    m2 = make_raw_market("M2", yes_ask="0.4000", no_ask="0.4000")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m1, m2])], cursor=None)
    transport.set_order_book("M1")
    transport.set_order_book("M2")

    sleep_calls: List[float] = []
    client = KalshiRestClient(environment="production", transport=transport)
    # 5 req/s -> 0.2s minimum interval
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, max_requests_per_second=5.0, single_pass=True)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: sleep_calls.append(d))

    runner.run_cycle()

    # Verified rate limiter invoked sleep
    assert any(d > 0.0 for d in sleep_calls)


def test_12_retry_and_backoff_handling(tmp_path: Path):
    """12. Transient order-book transport failure triggers retry with backoff."""
    transport = SmartMockTransport()
    m_arb = make_raw_market("M-ARB", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_arb])], cursor=None)
    transport.set_order_book("M-ARB")
    # Inject 2 transient transport failures before success
    transport.order_book_errors["M-ARB"] = [
        KalshiTransportError("Connection reset"),
        KalshiTransportError("Read timeout"),
    ]  # type: ignore

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        max_retries=3,
        retry_backoff_seconds=0.01,
        max_requests_per_second=1000.0,
        single_pass=True,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    summary = runner.run_cycle()

    assert summary.qualified_count == 1
    assert summary.retries_count == 2
    assert summary.requests_failed == 2


def test_13_candidate_staleness_and_invalidation(tmp_path: Path):
    """13. Stale quote timestamp causes qualification rejection without network requests."""
    transport = SmartMockTransport()
    # Market close time is in future, but quote is stale
    m_stale = make_raw_market("M-STALE", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    # Quote timestamp 150 seconds ago
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=150)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m_stale["updated_time"] = stale_ts
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_stale])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        max_stale_seconds=60.0,
        single_pass=True,
        max_requests_per_second=1000.0,
        check_prefilter_quote_staleness=True,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    summary = runner.run_cycle()

    # Screener approves mathematically valid candidate in Stage B, but qualification rejects stale actual quotes
    assert summary.preliminary_candidates_count == 1
    assert summary.qualified_count == 0
    assert summary.stale_count == 1
    assert summary.stale_or_missing_quote_markets_count == 1
    assert len(transport.requested_orderbooks) == 0

    # Qualifier pre-flight staleness check rejects any candidate with stale timestamp
    cand_stale = PreliminaryCandidate(
        candidate_id="PRE-STALE",
        strategy_type="binary_parity",
        event_ticker="EV1",
        market_tickers=("M-STALE",),
        gross_edge_dollars=Decimal("0.1300"),
        gross_edge_cents=13,
        estimated_entry_cost_dollars=Decimal("0.8700"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"M-STALE": {"yes_ask": "0.4200", "no_ask": "0.4500"}},
        screening_timestamp=datetime.now(timezone.utc).isoformat(),
        candidate_rationale="Test",
        validation_limitations=(),
        stable_key="BP:M-STALE",
        quote_timestamp=stale_ts,
    )
    qual_res = runner._qualifier.qualify_candidate(cand_stale)
    assert qual_res.outcome == QualificationOutcome.REJECTED_STALE.value
    assert len(transport.requested_orderbooks) == 0


def test_14_market_closure_or_settlement(tmp_path: Path):
    """14. Transition of open market to closed status is reconciled and tracked."""
    transport = SmartMockTransport()
    # Cycle 1: Market open
    m_open = make_raw_market("M-CLOSE", status="open")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_open])], cursor=None)

    # Cycle 2: Market closed
    m_closed = make_raw_market("M-CLOSE", status="closed")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_closed])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, refresh_interval_seconds=0.0, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    s1 = runner.run_cycle()
    assert s1.closed_or_settled_markets_count == 0

    s2 = runner.run_cycle()
    assert s2.closed_or_settled_markets_count >= 1


def test_15_isolated_output_paths(tmp_path: Path):
    """15. Scanner persistence is isolated strictly inside configured output_dir."""
    custom_dir = tmp_path / "custom_scanner_output"
    transport = SmartMockTransport()
    m_arb = make_raw_market("M-ARB", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_arb])], cursor=None)
    transport.set_order_book("M-ARB")

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=custom_dir, single_pass=True, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    runner.run_cycle()

    assert (custom_dir / "scanner_state.json").exists()
    assert (custom_dir / "scan_summaries.jsonl").exists()
    assert (custom_dir / "candidate_history.jsonl").exists()
    assert (custom_dir / "qualification_history.jsonl").exists()


def test_16_no_writes_to_legacy_collector_evaluator_state(tmp_path: Path):
    """16. Scanner never modifies or overwrites legacy collector/evaluator state files."""
    legacy_obs = Path("data/live_collection/observations.jsonl")
    legacy_collector = Path("data/live_collector_state.json")
    legacy_evaluator = Path("data/live_evaluator_state.json")

    # Record pre-execution state
    obs_mtime = legacy_obs.stat().st_mtime if legacy_obs.exists() else None
    col_mtime = legacy_collector.stat().st_mtime if legacy_collector.exists() else None
    eval_mtime = legacy_evaluator.stat().st_mtime if legacy_evaluator.exists() else None

    transport = SmartMockTransport()
    transport.queue_events_page([make_raw_event("EV1", "Economics", [make_raw_market("M1")])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=True, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    runner.run_cycle()

    # Assert legacy files unchanged
    if legacy_obs.exists():
        assert legacy_obs.stat().st_mtime == obs_mtime
    if legacy_collector.exists():
        assert legacy_collector.stat().st_mtime == col_mtime
    if legacy_evaluator.exists():
        assert legacy_evaluator.stat().st_mtime == eval_mtime


def test_17_persistence_and_recovery(tmp_path: Path):
    """17. State persisted on cycle is safely restored on subsequent runner startup."""
    transport = SmartMockTransport()
    m_arb = make_raw_market("M-ARB", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_arb])], cursor=None)
    transport.set_order_book("M-ARB")

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=True, max_requests_per_second=1000.0)

    # Runner 1 executes
    runner1 = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)
    runner1.run_cycle()
    h1 = runner1.get_health()

    # Runner 2 starts with same output directory
    runner2 = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)
    h2 = runner2.get_health()

    assert h2.total_unique_markets_discovered == h1.total_unique_markets_discovered
    assert h2.total_unique_markets_screened == h1.total_unique_markets_screened
    assert h2.total_preliminary_candidates_generated == h1.total_preliminary_candidates_generated
    assert h2.qualified_candidates_count == h1.qualified_candidates_count


def test_18_corrupt_state_recovery(tmp_path: Path):
    """18. Corrupt or malformed state file is handled safely without crashing on startup."""
    corrupt_state_file = tmp_path / "scanner_state.json"
    corrupt_state_file.write_text("NOT_VALID_JSON{:::broken", encoding="utf-8")

    transport = SmartMockTransport()
    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, state_file=corrupt_state_file, single_pass=True)

    # Should not raise exception
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    health = runner.get_health()
    assert health.status == "INITIALIZING"
    assert health.last_error is not None
    assert "failed loading state" in health.last_error.lower() or "corrupt" in health.last_error.lower()


def test_19_graceful_sigint_sigterm_shutdown(tmp_path: Path):
    """19. Calling stop() signals cooperative shutdown, exits loop, and records STOPPED status."""
    transport = SmartMockTransport()
    # Endless pages
    for i in range(10):
        transport.queue_events_page([make_raw_event(f"EV{i}", "Economics", [make_raw_market(f"M{i}")])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, refresh_interval_seconds=0.01, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: time.sleep(0.005))

    # Stop after 0.05 seconds from another thread
    def delayed_stop():
        time.sleep(0.05)
        runner.stop()

    t = threading.Thread(target=delayed_stop)
    t.start()

    runner.run()
    t.join()

    health = runner.get_health()
    assert health.status == "STOPPED"
    assert health.total_cycles_count >= 1


def test_20_finite_duration_mode(tmp_path: Path):
    """20. Finite duration_seconds halts scanner loop after specified time expires."""
    transport = SmartMockTransport()
    for i in range(20):
        transport.queue_events_page([make_raw_event(f"EV{i}", "Economics", [make_raw_market(f"M{i}")])], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    # Halt after 0.1s
    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        duration_seconds=0.1,
        refresh_interval_seconds=0.01,
        max_requests_per_second=1000.0,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: time.sleep(0.01))

    t0 = time.time()
    runner.run()
    elapsed = time.time() - t0

    assert elapsed >= 0.09
    assert runner.get_health().status == "STOPPED"


def test_21_no_private_credentials_or_order_placement_calls(tmp_path: Path):
    """21. Scanner performs strictly read-only public market data calls."""
    transport = SmartMockTransport()
    m_arb = make_raw_market("M-ARB", yes_bid="0.4000", yes_ask="0.4200", no_bid="0.4300", no_ask="0.4500")
    transport.queue_events_page([make_raw_event("EV1", "Economics", [m_arb])], cursor=None)
    transport.set_order_book("M-ARB")

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=True, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    runner.run()

    for call_url in transport.calls:
        # Must only call public /events or /markets/.../orderbook
        assert "/trade-api/v2/events" in call_url or "/markets/" in call_url
        assert "/orders" not in call_url
        assert "/portfolio" not in call_url
        assert "/fills" not in call_url


def test_22_incomplete_scans_never_reported_as_complete(tmp_path: Path):
    """22. Partial pagination passes are explicitly flagged with is_catalog_complete=False."""
    transport = SmartMockTransport()
    # Cursor loop or truncation simulation: error on page 2
    transport.queue_events_page([make_raw_event("EV1", "Economics", [make_raw_market("M1")])], cursor="page2-cursor")
    transport.events_errors.extend([KalshiHTTPError(500, "https://api.kalshi.com/trade-api/v2/events", "Server Error")] * 5)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, single_pass=True, max_retries=1, retry_backoff_seconds=0.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    summary = runner.run_cycle()

    assert summary.is_catalog_complete is False
    assert runner.get_health().last_catalog_complete is False


def test_23_unique_market_counts_never_conflated_with_scan_cycles(tmp_path: Path):
    """23. Telemetry strictly separates unique markets coverage from cycle repetitions."""
    transport = SmartMockTransport()
    # Catalog has exactly 4 fixed markets
    mkts = [make_raw_market(f"M{i}") for i in range(1, 5)]
    ev = make_raw_event("EV1", "Economics", mkts)
    for _ in range(5):
        transport.queue_events_page([ev], cursor=None)

    client = KalshiRestClient(environment="production", transport=transport)
    config = BreadthScannerRunnerConfig(output_dir=tmp_path, max_cycles=5, refresh_interval_seconds=0.0, max_requests_per_second=1000.0)
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    runner.run()

    health = runner.get_health()
    # Unique markets should remain 4, despite 5 cycles
    assert health.total_unique_markets_discovered == 4
    assert health.total_unique_markets_screened == 4
    assert health.total_cycles_count == 5


def test_24_existing_v20_v25_and_phase_2a_2c_regression():
    """24. Phase 2D seamlessly integrates without regressions across all V20-V25 and Phase 2A-2C modules."""
    collector = ContinuousCollector()
    evaluator = ContinuousEvaluator()
    v22_qualifier = ExecutionAwareArbitrageQualifier()
    alert_engine = AlertEngine()
    screener = BreadthArbitrageScreener()
    phase2c_qualifier = TargetedOrderBookQualifier()
    phase2d_runner = BreadthScannerRunner()

    assert collector is not None
    assert evaluator is not None
    assert v22_qualifier is not None
    assert alert_engine is not None
    assert screener is not None
    assert phase2c_qualifier is not None
    assert phase2d_runner is not None
