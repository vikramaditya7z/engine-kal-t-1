# Phase 2F-G5: Bounded Concurrent Order-Book Fetching Implementation Report

**Status:** Complete & Offline Verified  
**Date:** 2026-10-30  
**Repository Branch:** `fix/v19-clean-checkout-test`  
**Test Suite:** `tests/test_breadth_scanner_phase_2f_g5.py` (7 tests, 100% passing)  
**Full Scanner Tests:** 155 passing tests across all breadth scanner suites  

---

## 1. Executive Summary

Milestone G5 implements **Bounded Concurrent Order-Book Fetching** for multi-leg MECE basket qualification in the Kalshi Breadth-First Scanner (`TargetedOrderBookQualifier`), addressing the multi-leg timestamp skew bottleneck discovered in Phase 2F-F.

In previous phases, constituent legs of MECE baskets were fetched sequentially. For multi-leg baskets (up to 8 legs), sequential HTTP round-trips caused client fetch windows to stretch to 4.0–8.0 seconds, leading to `REJECTED_LEG_SKEW` rejections under network latency even when exchange resting orders were valid.

Milestone G5 introduces:
1. **Thread-Safe Token Bucket Rate Limiter (`TokenBucketRateLimiter`):** Atomic ticket scheduling across all concurrent worker threads, enforcing a strict upper bound of $\le 5.0\text{ req/s}$ with zero burst violations.
2. **Bounded Concurrency in MECE Basket Qualification:** Utilizes Python's `concurrent.futures.ThreadPoolExecutor` bounded by `workers = min(max_concurrency, len(tickers))`.
3. **Fail-Fast Worker Cancellation & Clean Shutdown:** When any constituent leg fails (e.g. 404, 500, network error), active sibling tasks are immediately cancelled via `cancel_event` and `future.cancel()`, returning `ERROR` outcome swiftly without orphan threads or lingering background processes.
4. **Deterministic Constituent Mapping:** Order books and their SHA-256 hashes are mapped back deterministically to their respective constituent tickers regardless of thread completion order.
5. **Preserved Dual-Skew & Leg Gating Guarantees:** Concurrency operates under strict dual-skew checks (`fetch_window_seconds <= 5.0s` and `server_timestamp_skew_seconds <= 2.0s`).

---

## 2. Key Architecture & Design

### A. Thread-Safe Token Bucket Rate Limiter (`TokenBucketRateLimiter`)

To avoid bursting or triggering upstream 429 rate limit errors from Kalshi's Cloudflare / AWS edge infrastructure, the rate limiter uses atomic ticket scheduling:

```python
class TokenBucketRateLimiter:
    def __init__(self, rate_per_second: float = 5.0, ...):
        self.rate_per_second = float(rate_per_second)
        self._lock = threading.Lock()
        self._next_available_time: float = 0.0

    def acquire(self) -> float:
        if self.rate_per_second <= 0:
            return 0.0
        interval = 1.0 / self.rate_per_second
        with self._lock:
            now = self._time()
            if self._next_available_time < now:
                self._next_available_time = now
            ticket_time = self._next_available_time
            self._next_available_time += interval

        now = self._time()
        sleep_duration = ticket_time - now
        if sleep_duration > 0:
            self._sleep(sleep_duration)
            return sleep_duration
        return 0.0
```

- When multiple threads attempt outbound requests simultaneously, each reserves an atomic ticket spaced by $1.0 / \text{rate}$ ($0.20\text{s}$ at 5.0 req/s).
- Sleeping is performed outside the lock, avoiding lock contention and maintaining high throughput.
- Zero burst buildup after idle periods: the first request is instant, and subsequent requests are spaced by $\ge 0.20\text{s}$.

### B. Bounded Multi-Leg Concurrent Fetching

When evaluating multi-leg MECE baskets (`ScreeningStrategyType.MECE_BASKET_LONG_YES` or `ScreeningStrategyType.MECE_BASKET_LONG_NO`):
- `workers = min(self.config.max_concurrency, len(tickers))` (default concurrency: 4).
- If `workers > 1`, tasks are dispatched to a `ThreadPoolExecutor`.
- If `workers <= 1`, execution degrades gracefully to the direct sequential loop.
- All outbound requests go through `self._rate_limiter.acquire()`, ensuring compliance across both concurrent order-book workers and event pre-filter queries.

### C. Worker Failure Fast-Cancellation & Zero-Thread Leakage

If any leg fails:
- `cancel_event.set()` signals sibling workers to skip subsequent retry attempts or backoff sleeps.
- `future.cancel()` cancels any pending tasks not yet started.
- Completed futures in `as_completed` are cleanly reaped; `CancelledError` is caught and ignored.
- The executor context manager exits with `executor.shutdown(wait=True)`, guaranteeing that all worker threads are joined and terminated before `qualify_candidate` returns.
- Active thread counts before and after qualification remain identical.

---

## 3. Verification & Test Results

The dedicated test suite `tests/test_breadth_scanner_phase_2f_g5.py` verifies all operational constraints offline:

| Test Case | Purpose | Result |
| :--- | :--- | :--- |
| `test_token_bucket_rate_limiter_pacing` | Confirms 5 concurrent worker threads are paced with $\ge 0.18\text{s}$ total span for 20 req/s | **PASSED** |
| `test_token_bucket_rate_limiter_idle_no_burst` | Confirms idle periods do not accumulate burst capacity | **PASSED** |
| `test_concurrent_order_book_fetching_speedup` | Confirms 4-leg fetch finishes significantly faster under concurrency=4 vs concurrency=1 | **PASSED** |
| `test_concurrent_fetching_shared_rate_limiter_pacing` | Confirms 4 concurrent workers respect 10 req/s limit ($\ge 0.28\text{s}$) | **PASSED** |
| `test_worker_failure_cancellation_and_cleanup` | Verifies immediate cancellation on 500 error, zero thread leak, and clean `ERROR` outcome | **PASSED** |
| `test_deterministic_order_book_mapping` | Verifies constituent leg details and hashes match exact ticker sequence across 5 repeated runs | **PASSED** |
| `test_dual_skew_validation_with_concurrency` | Verifies both client fetch window and server timestamp skew are enforced under concurrency | **PASSED** |

### Full Test Suite Run:
- 155 breadth scanner tests passed in 11.18 seconds (`pytest tests/test_breadth_scanner*.py`).

---

## 4. Production Safety & Invariants

- **Offline Only:** Tested exclusively against synthetic mocks and unit fixtures. Zero live Kalshi requests made.
- **Data Integrity:** Historical smoke-test data at `data/breadth_scanner/smoke_test_20261028/` remains completely untouched.
- **Port 8080 Dashboard:** Running instance left undisturbed.
- **Rate Limit Compliance:** Default 5.0 req/s is well within Kalshi's 10 req/s Basic Tier limit, with atomic ticket spacing preventing edge bursts.
