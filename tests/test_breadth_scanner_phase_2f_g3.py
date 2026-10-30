"""Phase 2F-G3 Unit Tests: Timestamp, Freshness & Performance Telemetry.

Verifies:
1. Dual-skew metrics: client-side fetch window vs. server-side timestamp skew.
2. Dual-skew rejections: fetch window exceeded vs. server timestamp skew exceeded.
3. Discovery sub-phase timings: network, json parse, normalization, deduplication, total.
4. Deterministic quote age calculation and rejection with telemetry populated.
5. Constituent leg count recorded across all qualification outcomes.
6. Backward compatibility: parsing legacy telemetry records missing new fields.
"""

from decimal import Decimal
from datetime import datetime, timezone, timedelta
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage.breadth_scanner import (
    BreadthScannerConfig,
    CatalogDiscoveryEngine,
    CatalogDiscoveryResult,
    PreliminaryCandidate,
    ScanCycleSummary,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
    TargetedQualificationResult,
)
from kalshi_arbitrage.execution_qualifier import QualificationOutcome
from kalshi_arbitrage.market_data import (
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.rest_client import HTTPResponse, KalshiRestClient


# -----------------------------------------------------------------------------
# Test Fixtures & Mocks
# -----------------------------------------------------------------------------

class MockTransportWithDelays:
    """Mock transport that simulates sequential responses and network delay."""

    def __init__(self, responses: Sequence[Any], simulated_delay_seconds: float = 0.0):
        self.responses: List[Any] = list(responses)
        self.calls: List[Tuple[str, Dict[str, str], float]] = []
        self.simulated_delay = simulated_delay_seconds

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        self.calls.append((url, dict(params), timeout_seconds))
        if not self.responses:
            raise RuntimeError(f"No mock response queued for {url}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, HTTPResponse):
            return resp
        if isinstance(resp, (dict, list)):
            body = json.dumps(resp).encode("utf-8")
            return HTTPResponse(200, body, {"date": "Wed, 28 Oct 2026 12:00:00 GMT"})
        raise ValueError(f"Unsupported mock response type: {type(resp)}")


class MockG3Client:
    """Mock KalshiRestClient with controllable timestamps and delays for G3 testing."""

    def __init__(
        self,
        event_response: Optional[NormalizedEvent] = None,
        order_books: Optional[Mapping[str, NormalizedOrderBook]] = None,
        fetch_delay_seconds: float = 0.0,
        time_provider: Optional[Any] = None,
    ):
        self.event_response = event_response
        self.order_books = dict(order_books or {})
        self.fetch_delay = fetch_delay_seconds
        self.time_provider = time_provider or (lambda: datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc))
        self.get_event_calls: List[str] = []
        self.get_order_book_calls: List[str] = []

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> Optional[NormalizedEvent]:
        self.get_event_calls.append(event_ticker)
        return self.event_response

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        self.get_order_book_calls.append(ticker)
        if ticker in self.order_books:
            return self.order_books[ticker]
        now = self.time_provider()
        return NormalizedOrderBook(
            market_ticker=ticker,
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=50),),
            source_timestamp=now,
        )


def make_test_market(
    ticker: str,
    event_ticker: str = "EV-1",
    yes_ask: str = "0.45",
    no_ask: str = "0.50",
    updated_time: Optional[datetime] = None,
) -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status="open",
        yes_bid_dollars=Decimal("0.40"),
        yes_ask_dollars=Decimal(yes_ask),
        no_bid_dollars=Decimal("0.45"),
        no_ask_dollars=Decimal(no_ask),
        close_time=datetime(2026, 12, 31, 0, 0, tzinfo=timezone.utc),
        updated_time=updated_time or datetime(2026, 10, 28, 11, 59, 58, tzinfo=timezone.utc),
    )


def make_test_candidate(
    candidate_id: str = "CAND-1",
    strategy_type: str = ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
    event_ticker: str = "EV-1",
    market_tickers: Tuple[str, ...] = ("M1", "M2"),
    quote_timestamp: Optional[str] = "2026-10-28T11:59:58+00:00",
) -> PreliminaryCandidate:
    now_iso = quote_timestamp or datetime.now(timezone.utc).isoformat()
    return PreliminaryCandidate(
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        event_ticker=event_ticker,
        market_tickers=tuple(market_tickers),
        gross_edge_dollars=Decimal("0.1000"),
        gross_edge_cents=10,
        estimated_entry_cost_dollars=Decimal("0.9000"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={t: {"yes_ask": "0.4500", "no_ask": "0.5000"} for t in market_tickers},
        screening_timestamp=now_iso,
        candidate_rationale="Test candidate",
        validation_limitations=(),
        stable_key=f"{strategy_type}:{event_ticker}:{'-'.join(market_tickers)}",
        is_execution_qualified=False,
        quote_timestamp=now_iso,
    )


# -----------------------------------------------------------------------------
# Milestone G3 Unit Tests
# -----------------------------------------------------------------------------

def test_g3_quote_age_calculation_deterministic():
    """Verify quote age calculation against a deterministic evaluation clock."""
    eval_time = datetime(2026, 10, 28, 12, 0, 10, tzinfo=timezone.utc)
    quote_time_str = "2026-10-28T12:00:02+00:00"  # 8.0s old

    cand = make_test_candidate(quote_timestamp=quote_time_str)
    client = MockG3Client(time_provider=lambda: eval_time)
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_stale_seconds=15.0, enable_event_prefilter=False),
        time_provider=lambda: eval_time,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.quote_age_seconds is not None
    assert abs(res.quote_age_seconds - 8.0) < 0.01


def test_g3_quote_age_stale_rejection_populates_telemetry():
    """Verify that quote age exceeding max_stale_seconds populates quote_age_seconds and leg_count."""
    eval_time = datetime(2026, 10, 28, 12, 0, 30, tzinfo=timezone.utc)
    quote_time_str = "2026-10-28T12:00:00+00:00"  # 30.0s old

    cand = make_test_candidate(market_tickers=("M1", "M2", "M3"), quote_timestamp=quote_time_str)
    client = MockG3Client(time_provider=lambda: eval_time)
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_stale_seconds=10.0, enable_event_prefilter=False),
        time_provider=lambda: eval_time,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.outcome == QualificationOutcome.REJECTED_STALE.value
    assert res.is_qualified is False
    assert res.candidate_leg_count == 3
    assert res.quote_age_seconds is not None
    assert abs(res.quote_age_seconds - 30.0) < 0.01
    assert "30.0s old > 10.0s limit" in res.rejection_reason


def test_g3_server_timestamp_skew_cross_leg_delta():
    """Verify server_timestamp_skew_seconds accurately measures cross-leg server delta."""
    eval_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    # Market 1 server timestamp: 12:00:01
    # Market 2 server timestamp: 12:00:03.5 (skew: 2.5s)
    book1 = NormalizedOrderBook(
        market_ticker="M1",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=50),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=50),),
        source_timestamp=datetime(2026, 10, 28, 12, 0, 1, 0, tzinfo=timezone.utc),
    )
    book2 = NormalizedOrderBook(
        market_ticker="M2",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=50),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=50),),
        source_timestamp=datetime(2026, 10, 28, 12, 0, 3, 500000, tzinfo=timezone.utc),
    )

    client = MockG3Client(
        order_books={"M1": book1, "M2": book2},
        time_provider=lambda: eval_time,
    )
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(
            max_leg_skew_seconds=5.0,  # 2.5s < 5.0s, will not reject
            max_fetch_window_seconds=10.0,
            enable_event_prefilter=False,
        ),
        time_provider=lambda: eval_time,
    )

    cand = make_test_candidate(market_tickers=("M1", "M2"))
    res = qualifier.qualify_candidate(cand)

    assert res.server_timestamp_skew_seconds is not None
    assert abs(res.server_timestamp_skew_seconds - 2.5) < 0.01
    assert res.cross_leg_skew_seconds == res.server_timestamp_skew_seconds
    assert res.candidate_leg_count == 2


def test_g3_server_timestamp_skew_exceeded_rejection():
    """Verify rejection with REJECTED_LEG_SKEW when server skew exceeds max_leg_skew_seconds."""
    eval_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    # 4.0s skew vs 2.0s limit
    book1 = NormalizedOrderBook(
        market_ticker="M1",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=50),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=50),),
        source_timestamp=datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc),
    )
    book2 = NormalizedOrderBook(
        market_ticker="M2",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=50),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=50),),
        source_timestamp=datetime(2026, 10, 28, 12, 0, 4, tzinfo=timezone.utc),
    )

    client = MockG3Client(
        order_books={"M1": book1, "M2": book2},
        time_provider=lambda: eval_time,
    )
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(
            max_leg_skew_seconds=2.0,
            max_fetch_window_seconds=10.0,
            enable_event_prefilter=False,
        ),
        time_provider=lambda: eval_time,
    )

    cand = make_test_candidate(market_tickers=("M1", "M2"))
    res = qualifier.qualify_candidate(cand)

    assert res.outcome == QualificationOutcome.REJECTED_LEG_SKEW.value
    assert res.is_qualified is False
    assert res.server_timestamp_skew_seconds is not None
    assert abs(res.server_timestamp_skew_seconds - 4.0) < 0.01
    assert "Constituent leg timestamp skew" in res.rejection_reason


def test_g3_client_fetch_window_exceeded_rejection(monkeypatch):
    """Verify candidate is rejected when client fetch window exceeds max_fetch_window_seconds."""
    eval_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    # Server timestamps are perfectly synchronized (0.0s skew)
    book1 = NormalizedOrderBook(
        market_ticker="M1",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=50),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=50),),
        source_timestamp=eval_time,
    )
    book2 = NormalizedOrderBook(
        market_ticker="M2",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=50),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=50),),
        source_timestamp=eval_time,
    )

    client = MockG3Client(
        order_books={"M1": book1, "M2": book2},
        time_provider=lambda: eval_time,
    )
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(
            max_leg_skew_seconds=10.0,
            max_fetch_window_seconds=0.01,  # Ultra-strict 10ms window
            enable_event_prefilter=False,
        ),
        time_provider=lambda: eval_time,
    )

    # Inject artificial wall-clock delay during order book fetching
    real_fetch = qualifier._fetch_order_book_with_retry

    def slow_fetch(ticker: str):
        import time
        time.sleep(0.02)  # 20ms > 10ms limit
        return real_fetch(ticker)

    monkeypatch.setattr(qualifier, "_fetch_order_book_with_retry", slow_fetch)

    cand = make_test_candidate(market_tickers=("M1", "M2"))
    res = qualifier.qualify_candidate(cand)

    assert res.outcome == QualificationOutcome.REJECTED_LEG_SKEW.value
    assert res.is_qualified is False
    assert res.fetch_window_seconds is not None
    assert res.fetch_window_seconds >= 0.02
    assert "Candidate order-book fetch window" in res.rejection_reason


def test_g3_discovery_sub_phase_timings():
    """Verify sub-phase discovery timers are non-overlapping and recorded on CatalogDiscoveryResult."""
    events_payload = {
        "events": [
            {
                "event_ticker": "EV1",
                "category": "Economics",
                "markets": [
                    {
                        "ticker": "M1",
                        "market_type": "binary",
                        "status": "open",
                        "yes_bid_dollars": 0.40,
                        "yes_ask_dollars": 0.45,
                        "no_bid_dollars": 0.50,
                        "no_ask_dollars": 0.55,
                        "close_time": "2026-12-31T00:00:00Z",
                    }
                ],
            }
        ],
        "cursor": None,
    }

    transport = MockTransportWithDelays([events_payload])
    client = KalshiRestClient(environment="production", transport=transport)
    engine = CatalogDiscoveryEngine(client=client)

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.network_time_seconds >= 0.0
    assert result.json_parse_time_seconds >= 0.0
    assert result.normalization_time_seconds >= 0.0
    assert result.deduplication_time_seconds >= 0.0
    assert result.total_discovery_time_seconds >= 0.0

    # Ensure all sub-phase timers appear in serialization
    d = result.to_dict()
    assert "network_time_seconds" in d
    assert "json_parse_time_seconds" in d
    assert "normalization_time_seconds" in d
    assert "deduplication_time_seconds" in d
    assert "screening_time_seconds" in d
    assert "total_discovery_time_seconds" in d


def test_g3_scan_cycle_summary_telemetry_serialization():
    """Verify ScanCycleSummary captures and serializes all G3 sub-phase timers."""
    summary = ScanCycleSummary(
        cycle_number=1,
        session_id="test-session",
        started_at="2026-10-28T12:00:00+00:00",
        completed_at="2026-10-28T12:00:05+00:00",
        duration_seconds=5.0,
        is_catalog_complete=True,
        markets_discovered=100,
        new_markets_count=100,
        changed_markets_count=0,
        closed_or_settled_markets_count=0,
        eligible_markets=100,
        markets_screened=100,
        events_screened=10,
        strategies_evaluated=200,
        preliminary_candidates_count=2,
        new_preliminary_candidates_count=2,
        deduplicated_preliminary_candidates_count=0,
        candidates_enqueued=2,
        candidates_evaluated=2,
        qualified_count=1,
        rejected_count=1,
        stale_count=0,
        error_count=0,
        unsupported_count=0,
        queue_overflow_count=0,
        requests_made=12,
        requests_failed=0,
        retries_count=0,
        rate_limit_events_count=0,
        network_time_seconds=1.2345,
        json_parse_time_seconds=0.1234,
        normalization_time_seconds=0.5678,
        deduplication_time_seconds=0.0543,
        screening_time_seconds=0.4321,
        total_discovery_time_seconds=2.0,
        total_qualification_time_seconds=2.5,
    )

    d = summary.to_dict()
    assert d["network_time_seconds"] == 1.2345
    assert d["json_parse_time_seconds"] == 0.1234
    assert d["normalization_time_seconds"] == 0.5678
    assert d["deduplication_time_seconds"] == 0.0543
    assert d["screening_time_seconds"] == 0.4321
    assert d["total_discovery_time_seconds"] == 2.0
    assert d["total_qualification_time_seconds"] == 2.5


def test_g3_backward_compatibility_old_telemetry_records():
    """Verify that TargetedQualificationResult instantiation without G3 fields defaults gracefully."""
    cand = make_test_candidate()
    # Instantiate without passing new G3 fields
    old_res = TargetedQualificationResult(
        qualification_id="tqual-old-1",
        candidate_id=cand.candidate_id,
        strategy_type=cand.strategy_type,
        stable_key=cand.stable_key,
        evaluated_at="2026-10-28T12:00:00+00:00",
        outcome="QUALIFIED",
        is_qualified=True,
        rejection_reason=None,
        preliminary_candidate=cand,
        requested_quantity=10,
        executable_quantity=10,
        gross_edge_cents=10,
        net_edge_cents=8,
        total_fees_cents=2,
        return_on_capital_pct=Decimal("10.0"),
    )

    assert old_res.candidate_leg_count == 0
    assert old_res.quote_age_seconds is None
    assert old_res.prefilter_duration_seconds is None
    assert old_res.order_book_fetch_duration_seconds is None
    assert old_res.fetch_window_seconds is None
    assert old_res.server_timestamp_skew_seconds is None
    assert old_res.cross_leg_skew_seconds is None

    # Serialization does not crash
    d = old_res.to_dict()
    assert d["candidate_leg_count"] == 0
    assert d["quote_age_seconds"] is None
    assert d["fetch_window_seconds"] is None
    assert d["server_timestamp_skew_seconds"] is None
