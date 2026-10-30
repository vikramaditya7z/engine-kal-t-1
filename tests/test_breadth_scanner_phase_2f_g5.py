"""Tests for Phase 2F-G5: Bounded Concurrent Order-Book Fetching.

Verifies:
1. TokenBucketRateLimiter thread-safety, ticket pacing, and burst prevention.
2. Concurrent order-book fetching speedup for multi-leg MECE baskets.
3. Thread-safe rate limiter enforcement across concurrent worker threads.
4. Worker failure fast cancellation and clean thread-pool cleanup.
5. Deterministic order-book mapping to constituent tickers regardless of thread completion order.
6. Dual-skew validation (fetch window vs server skew) under concurrent execution.
7. Single-threaded fallback when max_concurrency=1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import threading
import time
from typing import Dict, List, Optional
import pytest

from kalshi_arbitrage.breadth_scanner import (
    PreliminaryCandidate,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
    TokenBucketRateLimiter,
)
from kalshi_arbitrage.execution_qualifier import QualificationOutcome
from kalshi_arbitrage.market_data import (
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.rest_client import KalshiHTTPError


def make_test_candidate(
    strategy_type: str = ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
    event_ticker: str = "EVT-G5",
    market_tickers: tuple = ("M1", "M2", "M3", "M4"),
    gross_edge: Decimal = Decimal("0.0500"),
    candidate_id: str = "cand-g5-test",
) -> PreliminaryCandidate:
    cents = int(gross_edge * Decimal("100"))
    now_iso = datetime.now(timezone.utc).isoformat()
    return PreliminaryCandidate(
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        event_ticker=event_ticker,
        market_tickers=tuple(market_tickers),
        gross_edge_dollars=gross_edge,
        gross_edge_cents=cents,
        estimated_entry_cost_dollars=Decimal("0.9500"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={t: {"yes_ask": "0.2000", "no_ask": "0.8000"} for t in market_tickers},
        screening_timestamp=now_iso,
        candidate_rationale="Test candidate",
        validation_limitations=(),
        stable_key=f"st-{candidate_id}",
        is_execution_qualified=False,
        quote_timestamp=now_iso,
    )


class MockG5Client:
    def __init__(
        self,
        order_books: Optional[Dict[str, NormalizedOrderBook]] = None,
        event: Optional[NormalizedEvent] = None,
        delay_seconds: float = 0.0,
        fail_tickers: Optional[Dict[str, Exception]] = None,
    ) -> None:
        self.order_books = order_books or {}
        self.event = event
        self.delay_seconds = delay_seconds
        self.fail_tickers = fail_tickers or {}
        self.call_history: List[Tuple[str, float]] = []
        self._lock = threading.Lock()

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> NormalizedEvent:
        if self.event:
            return self.event
        # Return synthetic event covering all markets
        markets = []
        for t in self.order_books:
            markets.append(
                NormalizedMarket(
                    ticker=t,
                    event_ticker=event_ticker,
                    market_type="binary",
                    status="active",
                    yes_ask_dollars=Decimal("0.2000"),
                    no_ask_dollars=Decimal("0.8000"),
                    updated_time=datetime.now(timezone.utc),
                )
            )
        return NormalizedEvent(
            ticker=event_ticker,
            series_ticker="SERIES-G5",
            title="Event G5",
            status="active",
            markets=tuple(markets),
        )

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        call_time = time.time()
        with self._lock:
            self.call_history.append((ticker, call_time))

        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        if ticker in self.fail_tickers:
            raise self.fail_tickers[ticker]

        if ticker in self.order_books:
            return self.order_books[ticker]

        # Default book
        return NormalizedOrderBook(
            market_ticker=ticker,
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.20"), quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.75"), quantity=50),),
            source_timestamp=datetime.now(timezone.utc),
        )


def test_token_bucket_rate_limiter_pacing():
    """Verify TokenBucketRateLimiter paces concurrent requests without bursting."""
    limiter = TokenBucketRateLimiter(rate_per_second=20.0)  # 50ms interval
    dispatch_times: List[float] = []
    lock = threading.Lock()

    def worker():
        limiter.acquire()
        t = time.monotonic()
        with lock:
            dispatch_times.append(t)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    t_start = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    dispatch_times.sort()
    assert len(dispatch_times) == 5
    # Total elapsed for 5 tickets at 20 req/s (0.05s interval) is at least ~0.18s
    elapsed = dispatch_times[-1] - dispatch_times[0]
    assert elapsed >= 0.18, f"Expected elapsed >= 0.18s, got {elapsed:.4f}s"


def test_token_bucket_rate_limiter_idle_no_burst():
    """Verify that after an idle period, only the first request is instant, subsequent are spaced."""
    limiter = TokenBucketRateLimiter(rate_per_second=10.0)  # 100ms interval
    # First request
    dur1 = limiter.acquire()
    assert dur1 == 0.0

    # Sleep 0.2s to simulate idle
    time.sleep(0.2)

    # Subsequent requests should still be spaced 0.10s apart
    dur2 = limiter.acquire()
    assert dur2 == 0.0

    t_before = time.monotonic()
    limiter.acquire()
    t_after = time.monotonic()
    gap = t_after - t_before
    assert gap >= 0.08, f"Expected gap >= 0.08s, got {gap:.4f}s"


def test_concurrent_order_book_fetching_speedup():
    """Verify 4-leg candidate qualifies much faster with concurrency=4 vs concurrency=1."""
    tickers = ("M1", "M2", "M3", "M4")
    now_utc = datetime.now(timezone.utc)
    books = {
        t: NormalizedOrderBook(
            market_ticker=t,
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.20"), quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.80"), quantity=50),),
            source_timestamp=now_utc,
        )
        for t in tickers
    }

    # 40ms network delay per book fetch
    mock_client = MockG5Client(order_books=books, delay_seconds=0.04)

    # 1. Sequential (concurrency = 1)
    qualifier_seq = TargetedOrderBookQualifier(
        client=mock_client,
        config=TargetedOrderBookQualifierConfig(
            max_concurrency=1,
            max_requests_per_second=1000.0,  # Uncapped rate limit to isolate concurrency
            enable_event_prefilter=False,
        ),
    )
    t0 = time.monotonic()
    res_seq = qualifier_seq.qualify_candidate(make_test_candidate(market_tickers=tickers))
    seq_duration = time.monotonic() - t0
    assert res_seq.is_qualified
    assert seq_duration >= 0.15, f"Sequential should take >= 0.15s, took {seq_duration:.3f}s"

    # 2. Concurrent (concurrency = 4)
    mock_client_conc = MockG5Client(order_books=books, delay_seconds=0.04)
    qualifier_conc = TargetedOrderBookQualifier(
        client=mock_client_conc,
        config=TargetedOrderBookQualifierConfig(
            max_concurrency=4,
            max_requests_per_second=1000.0,
            enable_event_prefilter=False,
        ),
    )
    t1 = time.monotonic()
    res_conc = qualifier_conc.qualify_candidate(make_test_candidate(market_tickers=tickers))
    conc_duration = time.monotonic() - t1
    assert res_conc.is_qualified

    # Concurrency speedup: concurrent duration should be significantly less than sequential
    assert conc_duration < seq_duration * 0.75, (
        f"Concurrent ({conc_duration:.3f}s) was not significantly faster than sequential ({seq_duration:.3f}s)"
    )


def test_concurrent_fetching_shared_rate_limiter_pacing():
    """Verify that concurrent fetching strictly respects max_requests_per_second."""
    tickers = ("M1", "M2", "M3", "M4")
    now_utc = datetime.now(timezone.utc)
    books = {
        t: NormalizedOrderBook(
            market_ticker=t,
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.20"), quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.80"), quantity=50),),
            source_timestamp=now_utc,
        )
        for t in tickers
    }

    mock_client = MockG5Client(order_books=books, delay_seconds=0.0)
    # Rate limit = 10 req/s -> 100ms interval between requests
    qualifier = TargetedOrderBookQualifier(
        client=mock_client,
        config=TargetedOrderBookQualifierConfig(
            max_concurrency=4,
            max_requests_per_second=10.0,
            enable_event_prefilter=False,
        ),
    )

    t0 = time.monotonic()
    res = qualifier.qualify_candidate(make_test_candidate(market_tickers=tickers))
    duration = time.monotonic() - t0

    assert res.is_qualified
    assert res.requests_made == 4
    # 4 requests at 10 req/s require at least 3 intervals * 0.10s = ~0.28s
    assert duration >= 0.28, f"Expected duration >= 0.28s, got {duration:.3f}s"


def test_worker_failure_cancellation_and_cleanup():
    """Verify that when one constituent leg fails, siblings are cancelled and error returned immediately."""
    tickers = ("M1", "M2", "M3", "M4")
    now_utc = datetime.now(timezone.utc)
    books = {
        t: NormalizedOrderBook(
            market_ticker=t,
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.20"), quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.80"), quantity=50),),
            source_timestamp=now_utc,
        )
        for t in tickers
    }

    # Leg M2 fails with HTTP 500 error
    mock_client = MockG5Client(
        order_books=books,
        delay_seconds=0.05,
        fail_tickers={"M2": KalshiHTTPError(500, "/markets/M2/orderbook", "Internal Error")},
    )

    qualifier = TargetedOrderBookQualifier(
        client=mock_client,
        config=TargetedOrderBookQualifierConfig(
            max_concurrency=4,
            max_retries=1,  # 1 retry to fail fast
            retry_backoff_seconds=0.01,
            max_requests_per_second=100.0,
            enable_event_prefilter=False,
        ),
    )

    t0 = time.monotonic()
    active_threads_before = threading.active_count()
    res = qualifier.qualify_candidate(make_test_candidate(market_tickers=tickers))
    active_threads_after = threading.active_count()
    duration = time.monotonic() - t0

    assert not res.is_qualified
    assert res.outcome == "ERROR"
    assert "M2" in res.rejection_reason
    # Verify no leaked threads from ThreadPoolExecutor
    assert active_threads_after <= active_threads_before + 1
    # Duration should be short because M2 failed and cancelled siblings
    assert duration < 1.0


def test_deterministic_order_book_mapping():
    """Verify constituent order books map deterministically to the correct market tickers."""
    tickers = ("LEG_A", "LEG_B", "LEG_C", "LEG_D")
    now_utc = datetime.now(timezone.utc)
    # Distinct prices for each leg: total 10 + 15 + 20 + 25 = 70 cents cost -> 30 cents gross edge
    prices = {"LEG_A": Decimal("0.10"), "LEG_B": Decimal("0.15"), "LEG_C": Decimal("0.20"), "LEG_D": Decimal("0.25")}
    books = {
        t: NormalizedOrderBook(
            market_ticker=t,
            yes_bids=(OrderBookLevel(price_dollars=prices[t], quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("1.00") - prices[t], quantity=50),),
            source_timestamp=now_utc,
        )
        for t in tickers
    }

    mock_client = MockG5Client(order_books=books, delay_seconds=0.01)
    qualifier = TargetedOrderBookQualifier(
        client=mock_client,
        config=TargetedOrderBookQualifierConfig(
            max_concurrency=4,
            max_requests_per_second=100.0,
            enable_event_prefilter=False,
        ),
    )

    # Run 5 times to ensure complete determinism across thread scheduling variations
    for _ in range(5):
        res = qualifier.qualify_candidate(make_test_candidate(market_tickers=tickers))
        assert res.is_qualified
        assert len(res.leg_details) == 4
        # Verify leg details are in exact original ticker order
        for detail, expected_ticker in zip(res.leg_details, tickers):
            assert detail.market_ticker == expected_ticker
            expected_cost_cents = int(prices[expected_ticker] * 100)
            assert detail.total_cost_cents == expected_cost_cents
            assert expected_ticker in res.order_book_hashes


def test_dual_skew_validation_with_concurrency():
    """Verify client fetch window and server skew are checked and populated under concurrency."""
    tickers = ("M1", "M2")
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 28, 12, 0, 3, tzinfo=timezone.utc)  # 3.0s server skew

    books = {
        "M1": NormalizedOrderBook(
            market_ticker="M1",
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.20"), quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.75"), quantity=50),),
            source_timestamp=t0,
        ),
        "M2": NormalizedOrderBook(
            market_ticker="M2",
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.20"), quantity=50),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.75"), quantity=50),),
            source_timestamp=t1,
        ),
    }

    mock_client = MockG5Client(order_books=books, delay_seconds=0.01)
    # Server skew is 3.0s > 2.0s max_leg_skew_seconds limit
    qualifier = TargetedOrderBookQualifier(
        client=mock_client,
        config=TargetedOrderBookQualifierConfig(
            max_concurrency=2,
            max_leg_skew_seconds=2.0,
            max_fetch_window_seconds=5.0,
            max_requests_per_second=100.0,
            enable_event_prefilter=False,
        ),
    )

    res = qualifier.qualify_candidate(make_test_candidate(market_tickers=tickers))
    assert not res.is_qualified
    assert res.outcome == QualificationOutcome.REJECTED_LEG_SKEW.value
    assert "timestamp skew" in res.rejection_reason
    assert res.server_timestamp_skew_seconds == 3.0
    assert res.fetch_window_seconds is not None
    assert res.fetch_window_seconds > 0.0
