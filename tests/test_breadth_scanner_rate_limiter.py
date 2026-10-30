"""Offline tests verifying global rate limiting across catalog discovery, retries, and qualification.

Validates that a single shared TokenBucketRateLimiter instance is reused across:
1. Catalog discovery pagination.
2. Catalog discovery retries.
3. Event-level top-of-book pre-filtering and retries.
4. Targeted L2 order-book fetching and retries.
5. Concurrent worker threads during qualification.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import unittest.mock

import pytest

from kalshi_arbitrage import (
    BreadthScannerConfig,
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    CatalogDiscoveryEngine,
    FeeScheduleType,
    HTTPResponse,
    KalshiHTTPError,
    KalshiRestClient,
    KalshiTransportError,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PreliminaryCandidate,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
    TokenBucketRateLimiter,
)


class MockTransport:
    """Mock transport that returns sequential pre-queued responses."""

    def __init__(self, responses: Optional[Sequence[Any]] = None):
        self.responses: List[Any] = list(responses or [])
        self.calls: List[Tuple[str, Dict[str, str], float]] = []

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        self.calls.append((url, dict(params), timeout_seconds))
        if not self.responses:
            raise KalshiTransportError(f"No mock response queued for {url}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, HTTPResponse):
            return resp
        if isinstance(resp, (dict, list)):
            body = json.dumps(resp).encode("utf-8")
            return HTTPResponse(200, body, {"date": "Wed, 28 Oct 2026 12:00:00 GMT"})
        raise ValueError(f"Unsupported mock response type: {type(resp)}")


def make_raw_market(ticker: str, event_ticker: str = "EV1") -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "market_type": "binary",
        "status": "open",
        "title": f"Title for {ticker}",
        "yes_bid_dollars": "0.45",
        "yes_ask_dollars": "0.48",
        "no_bid_dollars": "0.52",
        "no_ask_dollars": "0.55",
        "last_price_dollars": "0.47",
        "close_time": "2026-12-31T23:59:59Z",
        "updated_time": "2026-10-28T12:00:00Z",
    }


def make_raw_event(event_ticker: str, markets: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "event_ticker": event_ticker,
        "category": "Economics",
        "series_ticker": "KXSERIES",
        "mutually_exclusive": True,
        "title": f"Event {event_ticker}",
        "markets": markets,
    }


class SpyRateLimiter(TokenBucketRateLimiter):
    """Subclass of TokenBucketRateLimiter that records every call to acquire()."""

    def __init__(self, rate_per_second: float = 5.0, sleep_func: Optional[Any] = None):
        super().__init__(rate_per_second=rate_per_second, sleep_func=sleep_func or (lambda s: None))
        self.acquire_calls: List[float] = []
        self._spy_lock = threading.Lock()

    def acquire(self) -> float:
        with self._spy_lock:
            self.acquire_calls.append(time.monotonic())
        return super().acquire()


# -----------------------------------------------------------------------------
# Test Cases
# -----------------------------------------------------------------------------

def test_catalog_pagination_acquires_token_per_page():
    """1. Verify catalog pagination acquires exactly 1 token before each page dispatch."""
    ev1 = make_raw_event("EV1", [make_raw_market("M1"), make_raw_market("M2")])
    ev2 = make_raw_event("EV2", [make_raw_market("M3"), make_raw_market("M4")])
    ev3 = make_raw_event("EV3", [make_raw_market("M5"), make_raw_market("M6")])

    page1 = {"events": [ev1], "cursor": "cur_1"}
    page2 = {"events": [ev2], "cursor": "cur_2"}
    page3 = {"events": [ev3], "cursor": None}

    transport = MockTransport([page1, page2, page3])
    client = KalshiRestClient(environment="production", transport=transport)

    limiter = SpyRateLimiter(rate_per_second=5.0)
    cfg = BreadthScannerConfig(discovery_page_size=10, retry_backoff_seconds=0.0)
    engine = CatalogDiscoveryEngine(
        client=client,
        config=cfg,
        rate_limiter=limiter,
        sleep_func=lambda s: None,
    )

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.pages_fetched == 3
    assert result.request_attempts == 3
    assert result.retries_count == 0
    # Exactly one acquire per page
    assert len(limiter.acquire_calls) == 3


def test_catalog_retries_acquire_token_per_attempt():
    """2. Verify that retry attempts during catalog pagination acquire a new token per attempt."""
    ev1 = make_raw_event("EV1", [make_raw_market("M1")])
    ev2 = make_raw_event("EV2", [make_raw_market("M2")])

    page1 = {"events": [ev1], "cursor": "cur_1"}
    # Page 2 fails on attempt 1, then succeeds on attempt 2 (retry 1)
    err500 = KalshiHTTPError(500, "Internal Server Error")
    page2 = {"events": [ev2], "cursor": None}

    transport = MockTransport([page1, err500, page2])
    client = KalshiRestClient(environment="production", transport=transport)

    limiter = SpyRateLimiter(rate_per_second=5.0)
    cfg = BreadthScannerConfig(discovery_page_size=10, max_retries=2, retry_backoff_seconds=0.0)
    engine = CatalogDiscoveryEngine(
        client=client,
        config=cfg,
        rate_limiter=limiter,
        sleep_func=lambda s: None,
    )

    result = engine.discover_catalog()

    assert result.is_complete is True
    assert result.pages_fetched == 2
    assert result.request_attempts == 3  # Page 1, Page 2 (attempt 1), Page 2 (attempt 2)
    assert result.retries_count == 1
    # Limiter must have been acquired for all 3 attempts
    assert len(limiter.acquire_calls) == 3


def test_runner_shares_single_limiter_instance():
    """3. Verify BreadthScannerRunner shares the exact same TokenBucketRateLimiter across all engines."""
    config = BreadthScannerRunnerConfig(
        max_requests_per_second=5.0,
        output_dir="/tmp/test_runner_share",
    )
    runner = BreadthScannerRunner(config=config, sleep_func=lambda s: None)

    # Runner, Discovery Engine, and Qualifier must share identical limiter reference
    assert runner.rate_limiter is not None
    assert runner._discovery_engine.rate_limiter is runner.rate_limiter
    assert runner._qualifier.rate_limiter is runner.rate_limiter
    assert runner.rate_limiter.rate_per_second == 5.0


def test_custom_rate_limiter_injected_into_runner():
    """4. Verify an externally injected rate limiter is shared across all runner engines."""
    custom_limiter = TokenBucketRateLimiter(rate_per_second=8.0)
    config = BreadthScannerRunnerConfig(
        output_dir="/tmp/test_runner_custom",
    )
    runner = BreadthScannerRunner(
        config=config,
        rate_limiter=custom_limiter,
        sleep_func=lambda s: None,
    )

    assert runner.rate_limiter is custom_limiter
    assert runner._discovery_engine.rate_limiter is custom_limiter
    assert runner._qualifier.rate_limiter is custom_limiter
    assert runner.rate_limiter.rate_per_second == 8.0


def make_test_candidate(
    candidate_id: str = "TEST-CAND",
    strategy_type: str = ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
    event_ticker: str = "EV1",
    market_tickers: Sequence[str] = ("M1", "M2"),
    gross_edge_dollars: str = "0.20",
) -> PreliminaryCandidate:
    now_iso = "2026-10-28T12:00:00Z"
    return PreliminaryCandidate(
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        event_ticker=event_ticker,
        market_tickers=tuple(market_tickers),
        gross_edge_dollars=Decimal(gross_edge_dollars),
        gross_edge_cents=int(Decimal(gross_edge_dollars) * 100),
        estimated_entry_cost_dollars=Decimal("0.80"),
        guaranteed_payout_dollars=Decimal("1.00"),
        constituent_quotes={t: {"yes_ask": "0.4000", "no_ask": "0.5500"} for t in market_tickers},
        screening_timestamp=now_iso,
        candidate_rationale="Test candidate",
        validation_limitations=(),
        stable_key=f"{strategy_type}:{':'.join(market_tickers)}",
        is_execution_qualified=False,
        quote_timestamp=now_iso,
    )


def test_all_request_paths_consume_from_shared_limiter():
    """5. Verify discovery, event pre-filter, and order-book requests all consume tickets from shared limiter."""
    shared_limiter = SpyRateLimiter(rate_per_second=5.0)

    # 1. Setup mock client
    client = unittest.mock.MagicMock()
    client._last_network_duration = 0.0
    client._last_json_duration = 0.0

    # Discovery page response
    ev1 = make_raw_event("EV1", [make_raw_market("M1"), make_raw_market("M2")])
    client._get_json_response.return_value = ({"events": [ev1], "cursor": None}, None)

    # Event pre-filter response
    norm_m1 = unittest.mock.MagicMock()
    norm_m1.ticker = "M1"
    norm_m1.updated_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    norm_m1.yes_ask_dollars = Decimal("0.40")
    norm_m1.no_ask_dollars = Decimal("0.55")

    norm_m2 = unittest.mock.MagicMock()
    norm_m2.ticker = "M2"
    norm_m2.updated_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    norm_m2.yes_ask_dollars = Decimal("0.40")
    norm_m2.no_ask_dollars = Decimal("0.55")

    mock_event = unittest.mock.MagicMock()
    mock_event.event_ticker = "EV1"
    mock_event.markets = [norm_m1, norm_m2]
    client.get_event.return_value = mock_event

    # Order book responses
    book1 = NormalizedOrderBook(
        market_ticker="M1",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=Decimal("10")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=Decimal("10")),),
        source_timestamp=datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc),
    )
    book2 = NormalizedOrderBook(
        market_ticker="M2",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.40"), quantity=Decimal("10")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=Decimal("10")),),
        source_timestamp=datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc),
    )
    client.get_order_book.side_effect = lambda ticker: book1 if ticker == "M1" else book2

    # Instantiate runner with shared limiter
    config = BreadthScannerRunnerConfig(
        environment="production",
        max_requests_per_second=5.0,
        enable_event_prefilter=True,
        max_candidate_legs=8,
        min_net_profit_cents=1,
    )
    runner = BreadthScannerRunner(
        client=client,
        config=config,
        rate_limiter=shared_limiter,
        sleep_func=lambda s: None,
        time_provider=lambda: datetime(2026, 10, 28, 12, 0, 1, tzinfo=timezone.utc),
    )

    # Step A: Run catalog discovery pass (1 page)
    disc_result = runner._discovery_engine.discover_catalog()
    assert disc_result.pages_fetched == 1
    # Exactly 1 token acquired for discovery
    assert len(shared_limiter.acquire_calls) == 1

    # Step B: Qualify a 2-leg candidate (triggers 1 event pre-filter + 2 order books)
    candidate = make_test_candidate(market_tickers=("M1", "M2"))
    runner._qualifier.enqueue_candidate(candidate)
    qual_summary = runner._qualifier.process_queue()

    assert qual_summary.candidates_evaluated == 1
    # Total calls:
    # 1 (discovery page) + 1 (event pre-filter) + 2 (order books: M1, M2) = 4 total tokens acquired
    assert len(shared_limiter.acquire_calls) == 4


def test_concurrent_order_book_requests_rate_limited():
    """6. Verify concurrent worker threads safely serialize token acquisition without races."""
    slept_durations: List[float] = []
    fake_time = 1000.0
    time_lock = threading.Lock()

    def mock_time() -> float:
        nonlocal fake_time
        with time_lock:
            return fake_time

    def mock_sleep(d: float) -> None:
        nonlocal fake_time
        with time_lock:
            fake_time += d
            slept_durations.append(d)

    limiter = TokenBucketRateLimiter(
        rate_per_second=5.0,  # 0.2s interval between requests
        time_provider=mock_time,
        sleep_func=mock_sleep,
    )

    # Launch 5 concurrent workers attempting to acquire simultaneously
    def worker_acquire() -> float:
        return limiter.acquire()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(worker_acquire) for _ in range(5)]
        results = [f.result() for f in futures]

    # Exactly 5 tickets scheduled: interval = 0.2s
    # Total virtual time elapsed across tickets must be at least (5 - 1) * 0.2s = 0.8s
    assert len(results) == 5
    with time_lock:
        assert fake_time >= 1000.8


def test_default_rate_limit_configuration():
    """7. Verify default rate limit is preserved at 5.0 across all components."""
    runner_cfg = BreadthScannerRunnerConfig()
    assert runner_cfg.max_requests_per_second == 5.0

    qual_cfg = TargetedOrderBookQualifierConfig()
    assert qual_cfg.max_requests_per_second == 5.0

    limiter = TokenBucketRateLimiter()
    assert limiter.rate_per_second == 5.0


def test_standalone_discovery_without_rate_limiter_backwards_compatible():
    """8. Verify CatalogDiscoveryEngine functions cleanly without a rate limiter (backwards compatibility)."""
    ev = make_raw_event("EV1", [make_raw_market("M1")])
    page = {"events": [ev], "cursor": None}

    transport = MockTransport([page])
    client = KalshiRestClient(environment="production", transport=transport)
    engine = CatalogDiscoveryEngine(client=client, sleep_func=lambda s: None)

    assert engine.rate_limiter is None
    result = engine.discover_catalog()
    assert result.is_complete is True
    assert result.pages_fetched == 1
