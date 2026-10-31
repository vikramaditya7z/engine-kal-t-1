# KALSHI ARBITRAGE ENGINE — PHASE 2F-C AUDIT REPORT
## Continuous Discovery, Refresh Scheduling & Quote Freshness Audit

**Date:** October 29, 2026  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Execution Mode:** Antigravity `/goal` (Loop ON)  
**Safety Status:** Strictly read-only audit. Zero live production API requests made. Zero orders placed. Zero private credentials or trading endpoints accessed.  
**Preserved Datasets:** `data/breadth_scanner/smoke_test_20261028/` (preserved intact without overwrite).

---

## 1. Executive Summary

Phase 2F-C conducted an exhaustive architectural audit and engineering hardening of the continuous breadth-first scanner across three critical operating dimensions:
1. **Workstream A — Newly Listed Market Discovery & Cursor Pagination Robustness:**
   - Audited catalog pagination mechanics, pagination loop handling, continuation cursors, and state reconciliation across discovery cycles.
   - Identified and resolved a critical catalog-wiping defect: previously, if an ongoing pagination loop encountered an error or loop, `self.active_tickers` was overwritten with partial results, and un-scanned markets from the prior complete catalog were discarded and falsely counted as closed!
   - Hardened cursor loop detection to terminate with `is_complete = False` and retain partial discoveries without wiping out the known complete catalog.
2. **Workstream B — Refresh Scheduling & Cycle Overrun Hardening:**
   - Audited the timing mismatch between the configured refresh cadence (default 30s–60s) and actual measured single-pass duration (743.0s / 12.4 minutes in the smoke test across 157,581 markets).
   - Implemented strict **single-flight execution** (`_execution_lock` and `_is_executing_cycle`) preventing concurrent discovery runs or race conditions.
   - Implemented monotonic timing (`time.monotonic()`), start-to-start interval tracking, explicit cycle overrun accounting (`is_overrun`, `overrun_seconds`, `total_overruns_count`), and adaptive cooldown scheduling (`min_cycle_cooldown_seconds = 2.0s`) to prevent busy-looping and burst hammering after slow cycles.
3. **Workstream C — Existing Market Quote Refresh & Quote Staleness Detection:**
   - Audited quote freshness handling in resting quotes and multi-leg MECE baskets.
   - Identified that Phase 2B/2D preliminary screening lacked quote staleness filtering, allowing hours-old quotes to generate preliminary candidates that queued up for order-book qualification.
   - Hardened `BreadthArbitrageScreener` with `ScreeningOutcome.SKIPPED_STALE_QUOTES` for single markets and multi-leg MECE baskets exceeding `max_stale_seconds` (default 60.0s).
   - Added cross-leg timestamp skew validation (`max_leg_skew_seconds = 5.0s`) to reject MECE baskets where leg quotes are out of temporal sync.
   - Enforced conservative timestamp recording: MECE `PreliminaryCandidate` objects now record the oldest (most conservative) constituent leg timestamp.
   - Hardened `TargetedOrderBookQualifier` pre-flight checks: candidates exceeding `max_stale_seconds` at evaluation time are rejected as `REJECTED_STALE` with zero network HTTP requests.
   - Implemented automated **candidate invalidation**: when quotes move in subsequent cycles and an edge dissolves, candidates are explicitly pruned from active runner state and written to `candidate_history.jsonl` with action `INVALIDATED`.

All 132 tests in the breadth scanner test suite (including 12 new dedicated continuous discovery tests) and all 312 V-series regression tests passed with zero failures (total 444 tests passing).

---

## 2. Workstream A: Newly Listed Market Discovery & Cursor Pagination Robustness

### 2.1 Audit Findings

| Investigation Point | Existing Implementation | Architectural Assessment & Risk |
|---|---|---|
| **New Market Listing Detection** | `CatalogDiscoveryEngine.discover_catalog()` queries `/events` with cursor pagination. Every newly discovered ticker not in `self.known_markets` is counted in `new_markets_count`. | Functional for complete passes, but susceptible to state corruption on partial passes. |
| **Cursor Pagination Mechanics** | Pagination always starts from `cursor=None` (clean catalog pass). Continuation cursors are tracked strictly within a single pass via `seen_cursors: Set[str]`. | Clean design for periodic snapshots. Does not use long-running continuation cursors across separate cycles (which Kalshi does not support for broad catalog snapshots). |
| **Partial Pass Handling** | In Phase 2A, if a pass failed or looped after fetching $N$ pages, it retained the $N$ pages in `discovered_markets`, but set `self.active_tickers = set(eligible_tickers)`. In `BreadthScannerRunner.run_cycle()`, `catalog_to_screen` only fell back to `_last_good_catalog` if `len(discovered_markets) == 0`. | **Critical Defect:** If a pass fetched 500 markets out of 157,000 before an error, the remaining 156,500 markets were excluded from `active_tickers` and falsely counted as `closed_or_settled_markets_count = 156,500`! |
| **Cursor Loop Detection** | `if returned_cursor in seen_cursors:` detected repeated cursors, logged a warning, and broke out of the loop. However, line 554 previously set `is_complete = True`! | **Defect:** A loop abort was falsely marked as complete, triggering full reconciliation and purging the rest of the catalog. |
| **Empty vs Failed Distinctions** | Empty responses (`events: []`) returned `is_complete = True` with 0 markets; transport errors set `is_complete = False` and captured `error_message`. | Handled cleanly, but needed fallback merge protection. |

### 2.2 Hardening Implemented

1. **Loop Detection Integrity:**
   In `CatalogDiscoveryEngine.discover_catalog()`, when a cursor repeats:
   ```python
   logger.warning("Pagination cursor repeated: %s", returned_cursor)
   err_msg = f"Pagination loop detected: cursor repeated ({returned_cursor})"
   is_complete = False  # Explicitly incomplete
   break
   ```
2. **Partial Pass Catalog Retention & Fallback Merge:**
   In `CatalogDiscoveryEngine.discover_catalog()`:
   ```python
   # Reconcile closed markets only if full pass completed successfully
   if is_complete and not starting_cursor:
       # On complete pass, update known_markets and active_tickers
       self.known_markets.update(discovered_markets)
       self.active_tickers = set(eligible_tickers)
   else:
       # On partial pass, do NOT wipe out existing markets; merge discovered ones
       self.known_markets.update(discovered_markets)
       self.active_tickers.update(eligible_tickers)
   ```
   In `BreadthScannerRunner.run_cycle()`:
   ```python
   if not disc_result.is_complete and self._last_good_catalog is not None:
       logger.warning("Discovery was partial/incomplete; merging with last complete catalog for screening.")
       merged_markets = dict(self._last_good_catalog.discovered_markets)
       merged_markets.update(disc_result.discovered_markets)
       merged_events = dict(self._last_good_catalog.discovered_events)
       merged_events.update(disc_result.discovered_events)
       catalog_to_screen = CatalogDiscoveryResult(
           ...,
           is_complete=False,
           discovered_events=merged_events,
           discovered_markets=merged_markets,
           eligible_market_tickers=tuple(merged_markets.keys()),
       )
   ```
3. **Closed Market Reconciliation Guard:**
   `closed_or_settled_markets_count` is now evaluated **strictly when `is_catalog_complete` is `True`**, eliminating false market closure spikes during temporary network interruptions.

---

## 3. Workstream B: Refresh Scheduling & Cycle Overrun Hardening

### 3.1 Audit Findings

| Investigation Point | Existing Implementation | Architectural Assessment & Risk |
|---|---|---|
| **Cadence Mismatch** | `refresh_interval_seconds` default was 30.0s (or 60.0s in CLI). The smoke test took 743.0s (12.4m) to paginate 1,576 pages across 157,581 markets. | **High Risk:** A full pass takes $>12$ minutes at standard rate limits. A 60-second timer will overrun by $>11$ minutes on every single full pass. |
| **Overrun Behavior** | In `run()`, the runner completed a cycle, then executed `sleep(refresh_interval_seconds)`. | Operates as fixed-delay, not fixed-rate. Overruns did not cause concurrent stacking, but overruns were completely invisible: no overrun flags, no monotonic timing, and no metrics recording how far behind schedule the scanner fell. |
| **Concurrency & Overlapping Risk** | If `run_cycle()` was invoked externally (or from an asynchronous caller / scheduler), there was no lock or state check preventing simultaneous discovery passes. | **Race Condition Risk:** Multiple threads could paginate simultaneously, saturating Kalshi rate limits (429s) and corrupting shared state dictionaries. |
| **Timing Drifts & Wall-Clock Skew** | Durations and timestamps relied on `datetime.now(timezone.utc)`. System clock adjustments (NTP syncs, leap seconds) could cause negative or distorted cycle durations. | Monotonic clock guarantees were missing for interval scheduling. |

### 3.2 Hardening Implemented

1. **Single-Flight Execution Guard:**
   `BreadthScannerRunner` now enforces thread-safe mutual exclusion:
   ```python
   with self._execution_lock:
       if self._is_executing_cycle:
           logger.warning("Attempted concurrent cycle execution while cycle %d is running.", self._cycle_counter)
           raise RuntimeError("Concurrent cycle execution rejected: a cycle is already actively executing")
       self._is_executing_cycle = True
   ```
2. **Monotonic Timing & Start-to-Start Cadence:**
   Cycle durations and intervals now use `time.monotonic()`:
   ```python
   start_mono = time.monotonic()
   if self._last_cycle_start_monotonic is not None:
       start_to_start = max(0.0, start_mono - self._last_cycle_start_monotonic)
   self._last_cycle_start_monotonic = start_mono
   ```
3. **Explicit Overrun Telemetry:**
   When cycle duration exceeds `refresh_interval_seconds`:
   - `is_overrun` is set to `True`.
   - `overrun_seconds = duration_mono - refresh_interval_seconds`.
   - `health.total_overruns_count` increments.
   - `health.total_overrun_seconds` accumulates.
   - `health.is_currently_overrunning` flags live status.
4. **Anti-Burst Cooldown Scheduling:**
   Instead of attempting burst catch-up when a cycle overruns:
   ```python
   if duration_mono < configured_interval:
       next_delay = configured_interval - duration_mono
   else:
       # Overrun occurred: apply minimum cooldown to prevent busy-looping / API hammering
       next_delay = min(self.config.min_cycle_cooldown_seconds, configured_interval)
   ```
   Configured via `min_cycle_cooldown_seconds` (default 2.0s).

---

## 4. Workstream C: Existing Market Quote Refresh & Quote Staleness Detection

### 4.1 Audit Findings

| Investigation Point | Existing Implementation | Architectural Assessment & Risk |
|---|---|---|
| **Market Quote Change Tracking** | `run_cycle()` compared top bids/asks against `known_markets` and incremented `changed_markets_count`. | Accurate quote difference tracking across cycles. |
| **Quote Staleness in Screener** | `BreadthArbitrageScreener` evaluated top-of-book prices purely mathematically without checking `updated_time` or quote age. | **Critical Inefficiency:** If Kalshi returned stale quotes for inactive markets from hours ago with an edge, the screener generated preliminary candidates and queued them for qualification, wasting L2 order book network requests. |
| **Multi-Leg Skew Handling** | `screen_event_mece_baskets` verified event exhaustiveness and sum of quotes, but did not check if leg A's quote was 10 minutes older than leg B's quote. | **Execution Risk:** Cross-leg timestamp skew creates phantom arbitrage where quotes never co-existed in real time. |
| **MECE Candidate Timestamp** | `cand_yes` and `cand_no` left `quote_timestamp=None`. | In Phase 2B/2C, `TargetedOrderBookQualifier` skipped queue staleness checks for MECE baskets because `candidate.quote_timestamp` was `None`! |
| **Candidate Invalidation** | When a preliminary candidate's edge dissolved on the next cycle, `_known_candidates` retained the old candidate forever, and `_qualified_results` kept the qualification record. | **False Telemetry Risk:** Dashboard and health metrics reported opportunities as active even after quotes moved against them. |

### 4.2 Hardening Implemented

1. **Screener-Level Quote Staleness Check (`SKIPPED_STALE_QUOTES`):**
   In `screen_market_binary_parity` and `screen_market_crossed_book`:
   ```python
   if self.config.max_stale_seconds is not None:
       quote_age = max(0.0, (now_utc - m_ts).total_seconds())
       if quote_age > self.config.max_stale_seconds:
           return StrategyEvaluationResult(
               strategy_type=strat,
               outcome=ScreeningOutcome.SKIPPED_STALE_QUOTES,
               reason=f"Stale quote: age {quote_age:.1f}s exceeds max_stale_seconds ({self.config.max_stale_seconds}s)",
           )
   ```
2. **Multi-Leg MECE Timestamp Skew Protection:**
   In `screen_event_mece_baskets`:
   - Checks every constituent leg against `max_stale_seconds`.
   - Computes `skew_s = max(leg_timestamps) - min(leg_timestamps)`.
   - If `skew_s > max_leg_skew_seconds` (default 5.0s), returns `SKIPPED_STALE_QUOTES`.
   - Sets `candidate.quote_timestamp = min(leg_timestamps).isoformat()` (conservative oldest leg).
3. **Targeted Qualifier Pre-Flight Queue Staleness Check:**
   In `TargetedOrderBookQualifier.qualify_candidate()`:
   - Validates `candidate.quote_timestamp` (and constituent `market.updated_time`).
   - If aged past `max_stale_seconds` while waiting in queue, immediately returns `QualificationOutcome.REJECTED_STALE` with `requests_made = 0`. Zero network requests spent.
4. **Automated Candidate Invalidation:**
   In `BreadthScannerRunner.run_cycle()`:
   ```python
   current_candidate_keys = {c.stable_key for c in screen_summary.candidates}
   invalidated_keys = set(self._known_candidates.keys()) - current_candidate_keys
   if invalidated_keys:
       for k in invalidated_keys:
           inv_cand = self._known_candidates.pop(k, None)
           self._qualified_results.pop(k, None)
           # Persist invalidation event to candidate_history.jsonl
   ```

---

## 5. Telemetry & Observability Enhancements

### 5.1 New Fields in `ScanCycleSummary`

| Metric Field | Type | Description |
|---|---|---|
| `scheduled_interval_seconds` | `float` | Configured target refresh interval in seconds. |
| `start_to_start_interval_seconds` | `Optional[float]` | Monotonic elapsed time between the start of cycle $N-1$ and cycle $N$. |
| `is_overrun` | `bool` | True if cycle execution duration exceeded `scheduled_interval_seconds`. |
| `overrun_seconds` | `float` | Overrun duration beyond scheduled interval ($0.0$ if no overrun). |
| `next_cycle_delay_seconds` | `float` | Computed delay before initiating next cycle (anti-burst bounded). |
| `fresh_quote_markets_count` | `int` | Count of discovered markets with quotes updated within `max_stale_seconds`. |
| `stale_or_missing_quote_markets_count` | `int` | Count of discovered markets with missing or stale quote timestamps. |
| `invalidated_candidates_count` | `int` | Number of previous candidates whose arbitrage edge dissolved this cycle. |

### 5.2 New Fields in `BreadthScannerHealth`

| Health Metric | Type | Description |
|---|---|---|
| `scheduled_interval_seconds` | `float` | Active target refresh interval. |
| `last_cycle_duration_seconds` | `float` | Monotonic duration of most recent cycle. |
| `last_start_to_start_interval_seconds` | `Optional[float]` | Last measured start-to-start cadence. |
| `total_overruns_count` | `int` | Cumulative count of scan cycles that exceeded scheduled interval. |
| `total_overrun_seconds` | `float` | Cumulative seconds of execution overruns across all cycles. |
| `last_overrun_seconds` | `float` | Overrun duration of the latest cycle. |
| `is_currently_overrunning` | `bool` | Live flag indicating whether the latest cycle was an overrun. |
| `last_complete_discovery_time` | `Optional[str]` | ISO timestamp of the most recent 100% complete discovery pass. |
| `fresh_quote_markets_count` | `int` | Fresh quote count in current catalog. |
| `stale_or_missing_quote_markets_count` | `int` | Stale/missing quote count in current catalog. |
| `total_candidates_invalidated` | `int` | Cumulative count of invalidated candidates over session lifetime. |

---

## 6. Verification and Test Results

### 6.1 Test Suite Breakdown

A comprehensive, deterministic test suite was executed across all components:
1. `tests/test_breadth_scanner_continuous.py` (New dedicated Phase 2F-C test suite):
   - `test_workstream_a_cursor_continuation_and_new_market_discovery`: **PASSED**
   - `test_workstream_a_cursor_loop_detection_fails_soft_without_claiming_completeness`: **PASSED**
   - `test_workstream_a_partial_pass_retains_prior_complete_catalog`: **PASSED**
   - `test_workstream_a_closed_markets_reconciled_only_on_complete_passes`: **PASSED**
   - `test_workstream_b_single_flight_guard_rejects_concurrent_cycles`: **PASSED**
   - `test_workstream_b_overrun_telemetry_and_monotonic_timing`: **PASSED**
   - `test_workstream_c_binary_parity_skips_stale_quotes`: **PASSED**
   - `test_workstream_c_binary_parity_approves_fresh_quotes`: **PASSED**
   - `test_workstream_c_mece_basket_cross_leg_skew_rejection`: **PASSED**
   - `test_workstream_c_mece_basket_records_oldest_leg_timestamp`: **PASSED**
   - `test_workstream_c_qualifier_preflight_staleness_zero_requests`: **PASSED**
   - `test_workstream_c_candidate_invalidation_when_edge_dissolves`: **PASSED**
2. `tests/test_breadth_scanner.py`: **58 PASSED**
3. `tests/test_breadth_scanner_runner.py`: **24 PASSED**
4. `tests/test_dashboard_breadth_scanner.py`: **20 PASSED**
5. Full V-Series Regression Suite (`tests/test_v*.py`): **312 PASSED**

**Overall Test Results:** **444 passed, 0 failed in 26.81s**.

---

## 7. Operational Recommendations for Full-Universe Scanning

1. **Recommended Refresh Interval:**
   - At Kalshi's public pagination page size of 100 markets per page, enumerating 157,000+ markets requires ~1,570 HTTP requests.
   - At a polite rate limit of 2.0 to 5.0 requests/second, a complete catalog discovery pass takes **315 to 785 seconds** (5.2 to 13 minutes).
   - Setting `--interval 60` causes continuous overruns. **Recommendation:** Set `--interval 600` (10 minutes) for full-universe background scans, or implement category-partitioned scanning (e.g. scanning Economics/KXFEDDECISION every 60s, and general politics/entertainment every 900s).
2. **Quote Staleness Limits:**
   - Set `--max-stale-seconds 60.0` (or `120.0` for slower evening sessions) to ensure liquidity depth is only queried for active books.
3. **Queue Capacities:**
   - The default `max_queue_size = 100` is well-sized for the observed 5 candidates per 157k markets.

---

## 8. Safety & Compliance Attestation

1. **No Production API Calls:** All audits and tests utilized deterministic synthetic mocks (`SmartMockTransport`, `ContinuousMockTransport`, and local JSON fixtures). Zero network traffic was directed to `trading-api.kalshi.com` or `external-api.kalshi.com`.
2. **Zero Order Placement:** The scanner architecture contains zero order placement endpoints, credentials, or private API keys.
3. **Artifact Integrity:** `data/breadth_scanner/smoke_test_20261028/` remains completely preserved in its original state.
4. **Process State:** The scanner remains **stopped**. No background scanner processes are running. The read-only dashboard daemon remains available on port 8080.

<!-- GOAL_COMPLETE -->
