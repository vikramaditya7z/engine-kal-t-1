# Phase 2F-G2 Implementation Report: Event-Level Top-of-Book Pre-Filter

**Date:** 2026-10-30  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Working Branch:** `fix/v19-clean-checkout-test`  
**Milestone:** Phase 2F-G2 (Phase 2 of Reference Design `docs/PHASE_2F_F_QUALIFICATION_AND_PERFORMANCE_DESIGN.md`)  
**Safety Mandate:** Offline execution only. Zero live Kalshi API calls, zero orders placed, zero background processes started, preserved smoke test data untouched.

---

## 1. Executive Summary

Milestone G2 implements the second milestone of the qualification bottleneck remediation design:
1. **Event-Level Top-of-Book Pre-Filter:** Intercepts candidates that pass G1 leg-count gating before they enter the expensive constituent order-book qualification phase. The qualifier issues a single `GET /events/{event_ticker}?with_nested_markets=true` call to evaluate whether the preliminary arbitrage opportunity still exists in refreshed top-of-book quotes.
2. **Short-Circuit on Dissolved Edge:** If the refreshed quotes indicate that the gross edge has dissolved or fallen to $\le \$0.0000$, the candidate is immediately rejected with outcome `REJECTED_EVENT_PREFILTER`, recording the original gross edge, the refreshed gross edge, observation timestamp, and event ticker—**issuing zero constituent order-book requests**.
3. **Safe Deduplication Across Shared Events:** When multiple candidates belong to the same event (e.g. MECE Long YES and MECE Long NO on the same underlying question), the retrieved event response is cached in memory with a short, configurable TTL (`event_cache_ttl_seconds = 2.0s`), avoiding duplicate HTTP requests while preserving quote freshness guarantees.
4. **Conservative Failure Handling:** Timeouts, 404 missing events, missing constituent markets, malformed pricing data, and stale quotes ($>60\text{s}$) are never treated as proof that an opportunity has disappeared. Instead, they record typed, auditable rejection outcomes (`REJECTED_MISSING_MARKET`, `REJECTED_STALE`, or `ERROR`) and conservatively abort order-book fetching without corrupting queue state.
5. **Offline Test Coverage:** 11 dedicated deterministic mocked tests in `tests/test_breadth_scanner_phase_2f_g2.py` verifying all failure modes, edge cases, deduplication, and request accounting. All 155 breadth scanner tests and all 874 suite tests pass cleanly.

---

## 2. Configuration Defaults & Conventions

| Configuration Class / Entry Point | Parameter | Type | Default | Validation & Behavior |
| :--- | :--- | :---: | :---: | :--- |
| `TargetedOrderBookQualifierConfig` | `enable_event_prefilter` | `bool` | `True` | Enables/disables event-level top-of-book pre-filter. |
| `TargetedOrderBookQualifierConfig` | `event_cache_ttl_seconds` | `float` | `2.0` | Validates `non-negative`; cache window for event deduplication. |
| `BreadthScannerRunnerConfig` | `enable_event_prefilter` | `bool` | `True` | Passed down to qualifier configuration. |
| `BreadthScannerRunnerConfig` | `event_cache_ttl_seconds` | `float` | `2.0` | Passed down to qualifier configuration. |
| `scripts/run_breadth_scanner.py` (CLI) | `--enable-event-prefilter` / `--no-event-prefilter` | `flag` | `True` | Toggle for event pre-filter. |
| `scripts/run_breadth_scanner.py` (CLI) | `--event-cache-ttl` | `float` | `2.0` | Configures freshness cache TTL in seconds. |

---

## 3. Architecture & Data Flow

```
+----------------------------------------------------------------------------------------------------+
|                                      G2 QUALIFICATION PIPELINE                                     |
+----------------------------------------------------------------------------------------------------+
|  Preliminary Candidate (Screened from Catalog)                                                    |
|        |                                                                                           |
|        v                                                                                           |
|  [Step 0: G1 Leg-Count Gate] --------(Legs > 8)---------------------> [REJECTED_BASKET_TOO_LARGE]  |
|        |                                                               (0 requests made)           |
|        | (Legs <= 8)                                                                               |
|        v                                                                                           |
|  [Step 1: Staleness Gate] -----------(Quote age > 60s)--------------> [REJECTED_STALE]             |
|        |                                                               (0 requests made)           |
|        | (Quote is fresh)                                                                          |
|        v                                                                                           |
|  [Step 1.5: G2 Event Pre-Filter]                                                                   |
|        |---> Cache Hit within 2.0s? ---> Reuse Event (0 requests)                                  |
|        +---> Cache Miss? --------------> Fetch GET /events/{ticker} (1 request)                    |
|        |                                                                                           |
|        +---(404 or Missing Markets)---------------------------------> [REJECTED_MISSING_MARKET]   |
|        +---(Stale Markets > 60s)------------------------------------> [REJECTED_STALE]            |
|        +---(Malformed / Out of bounds)------------------------------> [ERROR]                     |
|        +---(Refreshed Edge <= 0.00)---------------------------------> [REJECTED_EVENT_PREFILTER]  |
|        |                                                               (1 request spent, saved N)  |
|        | (Refreshed Edge > 0.00 confirmed)                                                         |
|        v                                                                                           |
|  [Step 2: Constituent L2 Qualification]                                                            |
|        |                                                                                           |
|        +---> Fetch L2 books for constituent legs (N requests)                                      |
|        +---> Verify Depth, VWAP, Skew, Slippage, Quadratic Fees                                    |
|        v                                                                                           |
|  [Outcome: QUALIFIED or REJECTED_*] (Total Requests = 1 + N)                                       |
+----------------------------------------------------------------------------------------------------+
```

---

## 4. Key Implementation Details

### 4.1 Nested Market Support in Rest Client
- Enhanced `NormalizedEvent` in `kalshi_arbitrage/market_data.py` to optionally preserve nested markets:
  ```python
  markets: Optional[Tuple[NormalizedMarket, ...]] = None
  ```
- Updated `KalshiRestClient.get_event(event_ticker, with_nested_markets=True)` in `kalshi_arbitrage/rest_client.py` to normalize and attach all constituent markets to the returned `NormalizedEvent`.

### 4.2 Rejection Evidence & Telemetry
When an opportunity is rejected by the pre-filter, `TargetedQualificationResult` persists:
- `outcome = "REJECTED_EVENT_PREFILTER"`
- `refreshed_gross_edge_dollars = Decimal("-0.0200")`
- `gross_edge_cents = -2`
- `rejection_reason = "Event pre-filter rejected: refreshed gross edge $-0.0200 <= $0.0000 (original edge: $0.0800) for event EV-DISSOLVED at 2026-10-30T05:28:33.000000+00:00"`
- `requests_made = 1` (saved $N$ order-book calls).

---

## 5. Offline Test Results

The dedicated suite `tests/test_breadth_scanner_phase_2f_g2.py` includes 11 deterministic test cases:

| Test Case | Scenario / Condition | Expected Behavior | Status |
| :--- | :--- | :--- | :---: |
| `test_g2_event_prefilter_opportunity_survives_and_reaches_l2` | Positive refreshed edge ($10¢) | Pre-filter passes; fetches L2 books; total requests = $1 + 2 = 3$ | **PASS** |
| `test_g2_event_prefilter_opportunity_disappears_zero_l2_requests` | Quotes shift up, edge negative ($-2¢$) | Returns `REJECTED_EVENT_PREFILTER`; requests = 1; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_refreshed_edge_exactly_zero` | Refreshed edge exactly $0.00¢ | Returns `REJECTED_EVENT_PREFILTER`; requests = 1; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_missing_event_404` | Event returns 404 | Conservative failure `REJECTED_MISSING_MARKET`; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_missing_constituent_markets` | Constituent leg missing from event | Returns `REJECTED_MISSING_MARKET`; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_malformed_response_negative_quote` | Price out of range $[0, 1]$ | Returns `ERROR`; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_stale_event_response` | Event market updated 120s ago ($>60$s) | Returns `REJECTED_STALE`; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_timeout_handling` | Transport timeout on event call | Returns `ERROR`; `requests_failed >= 1`; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_multiple_candidates_sharing_event_deduplication` | MECE Long YES and MECE Long NO on same event | Exactly 1 event request across both candidates via 5.0s TTL cache | **PASS** |
| `test_g2_g1_gating_occurs_before_event_retrieval` | 9-leg candidate ($>8$ limit) | Blocked at G1 leg gate; zero event requests; zero L2 requests | **PASS** |
| `test_g2_event_prefilter_disabled_flag` | `enable_event_prefilter = False` | Pre-filter bypassed; proceeds directly to L2 | **PASS** |

**Regression Suite Status:** All 155 breadth scanner tests passed in 9.66s. Zero regressions.
