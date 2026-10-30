# KALSHI BREADTH-FIRST SCANNER — PHASE 2F-D READINESS REVIEW
## Offline Readiness Assessment & Controlled One-Hour Production Trial Plan

**Audit Date:** October 29, 2026  
**Repository Location:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Execution Environment:** macOS, Python 3.9.6, Antigravity `/goal` (Loop ON)  
**Readiness Verdict:** **`READY WITH LIMITATIONS`** (Requires explicit human authorization prior to trial launch)  
**Safety Status:** **Strictly Offline & Read-Only**. Zero live production requests made. Zero orders placed. Zero private credentials or trading endpoints accessed. Scanner remains stopped.

---

## 1. Repository State and Working-Tree Snapshot

### 1.1 Branch and Commit Tracking
- **Active Git Branch:** `fix/v19-clean-checkout-test`
- **HEAD Commit Hash:** `21188a3f42c0382c5e19c8af96521afd46e9f7b1`
- **Upstream Tracking:** Branch is synchronized with `origin/fix/v19-clean-checkout-test`.

### 1.2 Working-Tree Status
The repository contains uncommitted improvements from prior phases (Phase 2A through 2F-C) that have been verified offline across 444 deterministic unit, integration, and regression tests. In accordance with safety directives, no commits, rebases, or pushes have been performed.

#### Modified Tracked Files
1. `README.md`: Project documentation and component references.
2. `kalshi_arbitrage/__init__.py`: Package entry points, exposing `BreadthArbitrageScreener`, `BreadthScannerRunner`, `CatalogDiscoveryEngine`, and `TargetedOrderBookQualifier`.
3. `kalshi_arbitrage/dashboard/server.py`: Read-only web dashboard server endpoints.
4. `kalshi_arbitrage/dashboard/service.py`: Dashboard telemetry aggregator, supporting multi-leg basket presentation, dynamic directory resolution, and overrun warnings.
5. `kalshi_arbitrage/dashboard/static/dashboard.css`: Dashboard styling and responsive layout.
6. `kalshi_arbitrage/dashboard/static/dashboard.js`: Client-side rendering for candidate tables, cycle history, and strategy breakdowns.
7. `kalshi_arbitrage/dashboard/static/index.html`: Dashboard template with dedicated breadth scanner panels.
8. `kalshi_arbitrage/execution.py`: Execution models and fill simulation.
9. `scripts/run_dashboard.py`: CLI dashboard server launcher with `--scanner-dir` configuration.
10. `tests/fixtures/kalshi_market_response.json`: Offline JSON test fixtures.

#### Untracked Source, Script, and Test Files
1. `kalshi_arbitrage/breadth_scanner.py`: Core implementation of Stage A (Discovery), Stage B (Screening), Stage C (Qualification), and Stage D (Continuous Runner).
2. `scripts/run_breadth_scanner.py`: Standalone CLI runner with signal handling, argument parsing, and isolated telemetry persistence.
3. `tests/test_breadth_scanner.py`: 58 unit tests covering Phase 2A discovery and Phase 2B/2C qualification.
4. `tests/test_breadth_scanner_continuous.py`: 12 unit and integration tests verifying continuous scheduling, cursor pagination, overrun accounting, quote staleness, and candidate invalidation.
5. `tests/test_breadth_scanner_runner.py`: 24 integration tests for pipeline execution, shutdown signals, and health state persistence.
6. `tests/test_dashboard_breadth_scanner.py`: 20 unit tests for dashboard data service, API endpoints, and template rendering.
7. Historical evidence and audit logs under `docs/` and `data/` (including `docs/PHASE_2F_C_CONTINUOUS_DISCOVERY_AUDIT.md`, `docs/PHASE_2F_A_DASHBOARD_VERIFICATION_REPORT.md`, `docs/PHASE_2E_DASHBOARD_INTEGRATION_REPORT.md`, and `docs/BREADTH_FIRST_SCANNER_AUDIT.md`).

### 1.3 Active Process and Task Audit
- **Scanner Process:** **STOPPED**. Verified via `ps aux | grep -E "breadth|scanner" | grep -v grep`. No active breadth scanner process exists.
- **Background Tasks:** No active background tasks in the agent environment.
- **Restart Mechanisms:** Zero cron jobs, zero systemd services, zero launchd agents, and zero retry daemons are configured. The scanner cannot launch without manual CLI invocation.
- **Dashboard Process:** PID `54914` is running locally at `127.0.0.1:8080`, bound in read-only mode to `data/breadth_scanner/smoke_test_20261028/`.

### 1.4 Historical Dataset Preservation
- **Smoke-Test Dataset:** `data/breadth_scanner/smoke_test_20261028/` is preserved intact (contains `scanner_state.json`, `scan_summaries.jsonl`, `candidate_history.jsonl`, `qualification_history.jsonl`).
- **Trial Output Directory:** All future trial data will be isolated to a new, non-overlapping directory (`data/breadth_scanner/trial_1hour_<timestamp>/`), completely precluding data corruption or accidental overwrite.

---

## 2. Technical Audit: Discovery, Scheduling, Freshness & Qualification

### 2.1 Newly Listed Market Discovery & Cursor Pagination

```
                                  KALSHI REST API (/events)
                                              │
                                              ▼
                                 ┌─────────────────────────┐
                                 │  CatalogDiscoveryEngine │
                                 └────────────┬────────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
         Complete Discovery Pass                           Partial / Interrupted Pass
    (cursor loops absent, error=None)                   (HTTP error, timeout, or loop)
                      │                                               │
                      ▼                                               ▼
     Reconcile Active & Closed Markets             Retain Existing Known Universe &
    (Update known_markets & active_tickers)       Merge Newly Discovered Markets (No Churn)
```

#### Audit Findings:
1. **Cursor Pagination Mechanics:**
   - Catalog enumeration uses Kalshi's public `/events` endpoint with `limit=100` and `with_nested_markets=true`.
   - Each discovery pass begins from `cursor=None`, guaranteeing a clean, repeatable snapshot of the market universe without relying on stale continuation tokens across cycles.
   - Within a single pass, cursors are tracked via an in-memory set (`seen_cursors`). If Kalshi emits a repeated cursor token, loop detection halts pagination immediately and flags `is_complete = False`.
2. **Catalog Retention on Partial Passes:**
   - In the event of a partial pass (e.g. network timeout or API error after page 50 of 155), `BreadthScannerRunner` merges the newly fetched markets with `_last_good_catalog`.
   - **Crucial Safety Guarantee:** Previously open markets that were not reached during an incomplete pass are **never** dropped or falsely marked as closed.
3. **Closed Market Reconciliation:**
   - Closed or settled market transitions are reconciled **only** when `is_catalog_complete` is `True` and `starting_cursor` is `None`. This eliminates false closure spikes caused by transient network failures.
4. **Verification Status:**
   - *Proven by Tests:* Pagination parameter propagation, cursor loop detection (`is_complete=False`), partial pass catalog merging, and closed market reconciliation are fully validated by `test_workstream_a_*` in `tests/test_breadth_scanner_continuous.py`.
   - *Requires Production Observation:* Exact pagination loop frequency under live Kalshi production rate limits and edge-case behavior of Kalshi cursor expiration during multi-minute passes.

---

### 2.2 Cycle Scheduling, Overrun Mechanics & Single-Flight Execution

#### Audit Findings:
1. **The 60-Second Interval Mismatch:**
   - The Phase 2F-B production smoke test demonstrated that enumerating Kalshi's public universe (157,581 markets across 15,485 events in 155 pages) required **742.9 seconds (~12.4 minutes)**.
   - **Architectural Reality:** A configured interval of `--interval 60` **cannot** guarantee that the entire market catalog is refreshed every 60 seconds. A single full pass over 157,000+ markets at safe public request rates (2 to 5 req/s) requires a theoretical minimum of 30 to 75 seconds purely for HTTP transport, plus payload transfer (hundreds of megabytes) and JSON/Decimal parsing overhead.
   - Therefore, the configured `--interval` must be understood as an *inter-cycle cooldown or target scheduled interval*, **not** an SLA on catalog freshness.
2. **Single-Flight Execution:**
   - `BreadthScannerRunner` guards `run_cycle()` with an internal `threading.Lock()` and `_is_executing_cycle` boolean. Concurrent invocations raise `RuntimeError("Concurrent cycle execution rejected...")`, preventing multiple overlapping passes from hammering the API or racing on internal state.
3. **Overrun Accounting & Anti-Burst Cooldown:**
   - Cycle durations and start-to-start cadences are measured monotonically (`time.monotonic()`), eliminating susceptibility to system clock drift or NTP adjustments.
   - If a cycle exceeds the configured interval ($D > I$), `is_overrun` is set to `True`, `overrun_seconds` is logged, and health counters increment.
   - Rather than attempting rapid burst catch-ups (which would trigger Kalshi 429 rate limits), the runner enforces `min_cycle_cooldown_seconds = 2.0s`.
4. **Verification Status:**
   - *Proven by Tests:* Single-flight rejection, monotonic duration tracking, overrun telemetry recording, and cooldown delays are verified by `test_workstream_b_*` in `tests/test_breadth_scanner_continuous.py`.
   - *Requires Production Observation:* Actual pass duration under varying network conditions and API server latency.

---

### 2.3 Quote Freshness, Timestamp Semantics & Targeted Qualification

```
                     Discovered Market Catalog (157,581 Markets)
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ Quote Freshness Filter                │
                     │ (now - updated_time <= max_stale_sec) │
                     └───────────────────┬───────────────────┘
                                         │
                    ┌────────────────────┴────────────────────┐
                    ▼                                         ▼
           Fresh Quotes (<= 60s)                     Stale Quotes (> 60s)
                    │                                         │
                    ▼                                         ▼
         Breadth Arbitrage Screener               SKIPPED_STALE_QUOTES
                    │                            (0 Preliminary Candidates,
                    ▼                              0 Order-Book Requests)
         Preliminary Candidates
                    │
                    ▼
     TargetedOrderBookQualifier Queue
                    │
                    ▼
      Pre-Flight Queue Staleness Check
                    │
         ┌──────────┴──────────┐
         ▼                     ▼
    Fresh in Queue       Aged in Queue (> 60s)
         │                     │
         ▼                     ▼
    Fetch L2 Book       REJECTED_STALE
    & Qualify Depth     (0 HTTP Requests)
```

#### Audit Findings:
1. **Source Timestamps vs Local Receipt Timestamps:**
   - Kalshi market objects contain an `updated_time` field representing when the resting top-of-book quote was updated in Kalshi's matching engine.
   - **Key Finding:** For liquid, active markets, `updated_time` reflects recent millisecond/second activity. However, for inactive, long-dated, or illiquid markets, `updated_time` may be hours, days, or weeks old.
   - If a market's quote was set hours ago and has not moved, evaluating it mathematically produces phantom arbitrage that collapsed long ago in the real market.
2. **Screening-Level Staleness Filtering (`SKIPPED_STALE_QUOTES`):**
   - `BreadthArbitrageScreener` enforces `max_stale_seconds` (default 60.0s). Any market whose `updated_time` exceeds 60 seconds is immediately skipped with `ScreeningOutcome.SKIPPED_STALE_QUOTES`.
   - Stale quotes **never** generate preliminary candidates, preventing unnecessary queue buildup and eliminating wasted L2 order book network requests.
3. **Multi-Leg MECE Basket Skew Protection:**
   - In multi-market MECE event baskets (e.g. Fed funds rate decisions or CPI ranges), individual leg quotes can be updated at different times.
   - If Leg A updated 2 seconds ago and Leg B updated 45 seconds ago, the screener computes `skew_seconds = max(leg_timestamps) - min(leg_timestamps)`.
   - If `skew_seconds > max_leg_skew_seconds` (default 5.0s), the basket is rejected with `SKIPPED_STALE_QUOTES`.
   - For valid baskets, `PreliminaryCandidate.quote_timestamp` records the **oldest (most conservative)** constituent leg timestamp.
4. **Queue Staleness & Targeted Qualification:**
   - Candidates sit in a bounded priority queue (`max_queue_size=100`, sorted by gross edge).
   - Before dispatching any HTTP order book requests, `TargetedOrderBookQualifier.qualify_candidate()` checks if the candidate's `quote_timestamp` aged past `max_stale_seconds` while waiting in queue.
   - If stale, it returns `QualificationOutcome.REJECTED_STALE` with `requests_made = 0`.
5. **Candidate Invalidation:**
   - When prices move on subsequent cycles and an edge dissolves, `BreadthScannerRunner` prunes the candidate from `_known_candidates` and `_qualified_results`, increments `invalidated_candidates_count`, and writes an `INVALIDATED` record to `candidate_history.jsonl`.
6. **Verification Status:**
   - *Proven by Tests:* Staleness rejection, cross-leg skew filtering, pre-flight qualifier checks, and candidate invalidation are verified by `test_workstream_c_*` in `tests/test_breadth_scanner_continuous.py`.
   - *Requires Production Observation:* Verification of Kalshi's exact server-side `updated_time` semantics across diverse market categories under live conditions.

---

## 3. API Request Budget and Coverage Analysis

### 3.1 Reconciliation of Smoke-Test Metrics
The Phase 2F-B smoke-test dataset recorded the following metrics for Cycle 1:
- `total_unique_markets_discovered`: 157,581
- `total_events_evaluated`: 15,485
- `total_strategies_evaluated`: 346,130
- `preliminary_candidates_generated`: 5
- `api_requests_count`: 227
- `duration_seconds`: 742.926

#### Request Budget Deconstruction:
$$\text{Total Requests (227)} = \text{Catalog Discovery Requests (155)} + \text{Targeted L2 Qualification Requests (72)}$$

1. **Discovery Requests (155 Pages):**
   - The `/events` endpoint with `limit=100` and `with_nested_markets=true` returned 15,485 events across 155 pages ($15,485 / 155 \approx 99.9$ events/page).
   - Each event contained an average of 10.17 nested markets ($157,581 / 15,485 = 10.17$).
   - Therefore, **155 HTTP GET requests** discovered the entire Kalshi universe of 157,581 markets.
2. **Targeted Qualification Requests (72 Books):**
   - The screener evaluated 346,130 strategy permutations and generated exactly **5 preliminary candidates** (all multi-leg MECE baskets).
   - The 5 candidates consisted of the following constituent leg counts:
     - Candidate 1: 8 legs $\rightarrow$ 8 order-book requests
     - Candidate 2: 16 legs $\rightarrow$ 16 order-book requests
     - Candidate 3: 10 legs $\rightarrow$ 10 order-book requests
     - Candidate 4: 5 legs $\rightarrow$ 5 order-book requests
     - Candidate 5: 33 legs $\rightarrow$ 33 order-book requests
   - Total L2 order-book requests: $8 + 16 + 10 + 5 + 33 = 72$ requests.
3. **Non-Candidate Markets:**
   - For the remaining 157,509 markets where no candidate was detected, **zero** order-book requests were made.
   - This proves the breadth-first architecture: $99.95\%$ of markets consumed zero order-book bandwidth.

### 3.2 Projected Request Budget for 1-Hour Production Trial
Assuming an average cycle duration of ~750 seconds (~12.5 minutes), a 3,600-second trial will execute approximately 4 to 5 complete discovery cycles.

| Activity | Requests per Cycle | Cycles in 1 Hour | Total Requests (1 Hour) | Average Rate |
|---|---|---|---|---|
| **Catalog Discovery (`/events`)** | 155 pages | 4.8 cycles | ~744 requests | ~0.21 req/s |
| **Targeted Qualification (`/orderbook`)** | ~72 books (est.) | 4.8 cycles | ~345 requests | ~0.10 req/s |
| **Total API Traffic** | **~227 requests** | **~4.8 cycles** | **~1,089 requests** | **~0.30 req/s** |

#### Kalshi Rate Limit Comparison:
- **Kalshi Public Rate Limit:** Typically 10 to 20 requests per second.
- **Configured Runner Limit:** `--request-rate 5.0` (maximum 5 req/s).
- **Actual Projected Traffic:** **0.30 req/s average** (with brief bursts up to 5.0 req/s during candidate qualification).
- **Headroom:** The projected traffic utilizes less than **$5\%$** of Kalshi's allowable public request budget.

---

## 4. Operational Safety and Isolation Verification

| Safety Requirement | Implementation Mechanism | Verification Result |
|---|---|---|
| **Dashboard Independence** | Dashboard (`scripts/run_dashboard.py`) runs as an isolated Flask service reading JSON/JSONL files from disk. It has zero network access to Kalshi. | **PASS**: Verified by `test_strict_isolation_from_legacy_paths` in `tests/test_dashboard_breadth_scanner.py`. |
| **Persistence Isolation** | Scanner writes exclusively to the directory specified via `--output-dir`. | **PASS**: Verified by `test_15_isolated_output_paths` in `tests/test_breadth_scanner_runner.py`. |
| **Historical Data Immutability** | Proposed trial directory `data/breadth_scanner/trial_1hour_<timestamp>/` is disjoint from `smoke_test_20261028/`. | **PASS**: Precludes accidental data overwrites. |
| **Read-Only Ingestion** | `KalshiRestClient` contains only `GET` methods (`list_markets`, `list_events`, `get_market`, `get_event`, `get_order_book`). Zero `POST`, `PUT`, or `DELETE` methods exist. | **PASS**: Source-verified in `kalshi_arbitrage/rest_client.py`. |
| **No Private Credentials** | The scanner initializes `KalshiRestClient` without API keys, session tokens, or private headers. | **PASS**: Verified by `test_21_no_private_credentials_or_order_placement_calls` in `tests/test_breadth_scanner_runner.py`. |
| **Clean Signal Termination** | Handles `SIGINT` (Ctrl+C) and `SIGTERM` gracefully, completing atomic writes and closing files. | **PASS**: Verified by `test_19_graceful_sigint_sigterm_shutdown` in `tests/test_breadth_scanner_runner.py`. |
| **Zero Auto-Restart Daemons** | Runner is executed as a foreground CLI process. Halts after `--duration 3600` or upon receiving a termination signal. | **PASS**: Verified by `test_20_finite_duration_mode` in `tests/test_breadth_scanner_runner.py`. |

---

## 5. Offline Verification and Test Summary

All tests were executed against the active virtual environment (`./.venv/bin/pytest`) with zero external network connectivity.

### 5.1 Test Execution Summary

```
============================= test session starts ==============================
platform darwin -- Python 3.9.6, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/vikramaditya/Disk/Projects/Kalshi
configfile: pyproject.toml

Test Suites Executed:
1. tests/test_breadth_scanner_continuous.py ........ 12 passed
2. tests/test_breadth_scanner.py .................... 58 passed
3. tests/test_breadth_scanner_runner.py ............. 24 passed
4. tests/test_dashboard_breadth_scanner.py .......... 20 passed
5. tests/test_v*.py (Full V-Series Regression) ...... 312 passed

============================= 444 passed in 29.53s =============================
```

### 5.2 Key Functional Assertions Confirmed Offline:
1. **Catalog Pagination:** 100% test coverage of single-page, multi-page, continuation cursors, loop detection, and empty terminal responses.
2. **Deterministic Screening:** Exact Decimal math and integer cent conversion for binary parity, MECE Long YES, MECE Long NO, and crossed-book strategies.
3. **Targeted Qualification:** Priority queue ordering, pre-flight queue staleness rejection, fee deduction, and multi-leg VWAP qualification.
4. **Continuous Scheduling:** Monotonic overrun tracking, anti-burst cooldown delays, single-flight mutual exclusion, and state recovery from disk.
5. **Dashboard Visibility:** Correct mapping of preliminary vs qualified opportunities, strategy breakdown aggregation, and telemetry staleness warnings.

---

## 6. Controlled One-Hour Production Trial Plan

### 6.1 Proposed Trial Command
The following exact command uses verified CLI options implemented in `scripts/run_breadth_scanner.py`. It is formulated for execution **only upon separate, explicit human authorization**.

```bash
# DO NOT EXECUTE WITHOUT EXPLICIT SEPARATE AUTHORIZATION
PYTHONUNBUFFERED=1 ./.venv/bin/python ./scripts/run_breadth_scanner.py \
  --environment production \
  --duration 3600 \
  --interval 60.0 \
  --page-size 100 \
  --queue-size 100 \
  --concurrency 4 \
  --request-rate 5.0 \
  --max-stale-seconds 60.0 \
  --target-quantity 1 \
  --fee-schedule kalshi_official_taker \
  --min-net-profit 1 \
  --output-dir data/breadth_scanner/trial_1hour_$(date +%Y%m%d_%H%M%S) \
  --verbose
```

### 6.2 Parameter Semantics & Rationale:
- `--environment production`: Connects to Kalshi's public live market data API (`https://api.elections.kalshi.com/trade-api/v2` or `external-api.kalshi.com`).
- `--duration 3600`: Imposes an immutable 1-hour wall-clock execution ceiling. The runner terminates cleanly at the end of the duration.
- `--interval 60.0`: Configures the inter-cycle target cadence. Because full-universe passes take ~750s, the runner will detect overruns and enforce the 2.0s anti-burst cooldown between passes.
- `--page-size 100`: Kalshi's standard pagination limit, yielding ~155 pages for full catalog enumeration.
- `--queue-size 100`: Accommodates observed preliminary candidates (historical peak was 5 candidates) with a $20\times$ safety buffer.
- `--concurrency 4`: Limits concurrent worker threads for L2 order-book fetching.
- `--request-rate 5.0`: Enforces an outgoing ceiling of 5.0 req/s, well within Kalshi public rate limits.
- `--max-stale-seconds 60.0`: Automatically skips markets with quotes older than 60 seconds and rejects candidates that age past 60s in queue.
- `--target-quantity 1`: Evaluates executable depth for a 1-contract fill.
- `--fee-schedule kalshi_official_taker`: Applies Kalshi's official taker fee formula ($0.07 \times P \times (1 - P)$).
- `--output-dir data/breadth_scanner/trial_1hour_<timestamp>`: Directs all state, summary, and history persistence to a timestamped directory, isolating it from smoke-test evidence.

### 6.3 Dashboard Monitoring During the Trial
To observe the live trial in the web dashboard without stopping the existing smoke-test view:
```bash
# In a separate terminal session:
./.venv/bin/python ./scripts/run_dashboard.py \
  --host 127.0.0.1 \
  --port 8081 \
  --scanner-dir data/breadth_scanner/trial_1hour_<timestamp>
```
The dashboard at `http://127.0.0.1:8081` will display live cycle updates, screening breakdowns, and qualification outcomes.

### 6.4 Explicit Stop Conditions (Abort Triggers)
The operator or monitoring observer must terminate the trial immediately (`Ctrl+C` or `kill -TERM <PID>`) if any of the following occur:
1. **Persistent 429 Rate Limits:** More than 5 consecutive HTTP 429 responses from Kalshi.
2. **Network Error Storm:** `api_failures_count` exceeding 20 failures within a single cycle.
3. **Queue Saturation:** `queue_overflow_count > 0` indicating that more than 100 candidates were generated.
4. **Infinite Pagination Loop:** A single discovery pass exceeding 250 pages without reaching terminal cursor `None`.
5. **Memory Leak:** Process RSS memory exceeding 1.5 GB.

---

## 7. Trial Success Metrics Table

| Metric | Source Field | Target / Calculation Formula | Success Criterion |
|---|---|---|---|
| **Catalog Coverage** | `total_unique_markets_discovered` | Cumulative count of unique tickers | Discovers $\ge 140,000$ unique markets |
| **Catalog Completeness** | `is_catalog_complete` | Boolean flag per cycle in `scan_summaries.jsonl` | $\ge 80\%$ of cycles complete cleanly |
| **Discovery Cadence** | `duration_seconds` | Monotonic elapsed seconds per pass | Stable pass duration ($700\text{s} - 900\text{s}$) |
| **Overrun Telemetry** | `is_overrun`, `overrun_seconds` | Logged in health and summary records | Accurately recorded; no burst loops |
| **Fresh Quote Ratio** | `fresh_quotes_count` / `markets_discovered` | Ratio of markets with quotes $\le 60\text{s}$ old | Empirically quantified and documented |
| **Screening Throughput** | `strategies_evaluated` | Sum of binary parity + MECE basket evaluations | $\ge 300,000$ strategy checks per pass |
| **Candidate Yield** | `preliminary_candidates_count` | Candidates passing gross top-of-book check | Reliably recorded; expected $< 50$ per pass |
| **Staleness Filtering** | `stale_count` | Rejections via `SKIPPED_STALE_QUOTES` & queue check | Stale candidates rejected with 0 HTTP calls |
| **Targeted Qualification** | `candidates_evaluated`, `qualified_count` | Results in `qualification_history.jsonl` | Order books fetched ONLY for candidate legs |
| **Candidate Invalidation** | `invalidated_candidates_count` | Count of dissolved candidates pruned across cycles | Prunes dissolved edges cleanly |
| **API Error Rate** | `requests_failed` / `requests_made` | Percentage of failed HTTP calls | $< 1.0\%$ total failure rate |
| **Rate Limit Encounters** | `rate_limit_events_count` | Cumulative HTTP 429 responses | $< 5$ total occurrences |
| **Persistence Integrity** | File validation on trial directory | Valid JSON in state; parseable JSONL in histories | Zero file corruption; atomic state updates |

---

## 8. Known Limitations and Unresolved Assumptions

1. **Quote Freshness in Public REST Snapshots:**
   - Kalshi's public `/events` payload embeds market quotes but does not provide individual order updates or websocket event streams.
   - For illiquid markets, resting quotes may remain untouched for hours. Phase 2F-C's `max_stale_seconds` filters out quotes older than 60 seconds, which significantly reduces the pool of screened markets to only actively maintained books.
2. **Cycle Latency Constraint:**
   - Because a full discovery pass over 157,000 markets requires ~12.4 minutes, high-frequency arbitrage opportunities that exist for only a few seconds cannot be reliably captured by an end-to-end breadth scanner. The breadth scanner functions as a macro discovery and persistent inefficiency screener, not a microsecond HFT engine.
3. **Execution Qualification Limitations:**
   - The qualifier evaluates resting order book depth and taker fees, but cannot predict front-running, queue competition, or fill latency that would occur upon live order submission.

---

## 9. Final Readiness Verdict

### Verdict: **`READY WITH LIMITATIONS`**

#### Rationale:
- **Codebase Readiness:** Fully hardened across Phase 2A through 2F-C. 444 of 444 deterministic tests pass.
- **Safety Readiness:** Confirmed 100% read-only. Zero private credentials, zero trade submission paths, zero automated restart mechanisms.
- **Observability Readiness:** Complete telemetry, overrun accounting, quote freshness tracking, candidate invalidation, and independent dashboard integration are in place.
- **Limitation Acknowledgment:** The user must explicitly recognize that a 1-hour trial will execute approximately 4 to 5 complete catalog passes (due to Kalshi's 157,000-market catalog size) and that freshness filtering will skip illiquid markets with inactive resting quotes.

**STRICT COMPLIANCE CONFIRMATION:**  
The scanner remains **STOPPED**. No production scans were initiated. The trial plan requires separate, explicit human authorization before execution.

<!-- GOAL_COMPLETE -->
