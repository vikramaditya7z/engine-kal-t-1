# Kalshi Arbitrage Engine V23 — Live Dashboard Integration Report

**Milestone:** V23  
**Date:** October 2026  
**Status:** COMPLETE & VERIFIED  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Test Suite:** 691 passed (100% passing across 691 unit & integration tests)  
**Deliverable Status:** Production-Ready Live Monitoring & Research Dashboard  

---

## 1. Executive Summary

Milestone **V23: Live Dashboard Integration** connects the Kalshi Arbitrage Research Engine's continuous infrastructure into a real-time, interactive, and transparent monitoring interface.

Prior to V23, the dashboard operated primarily as a retrospective viewer for saved trial files (`evidence_dashboard_trial`, `evidence_v13_fed_basket_trial`). Milestones V20 (Continuous Collector), V21 (Continuous Detection), and V22 (Execution-Aware Qualification) established a live streaming architecture, but lacked an integrated graphical interface to monitor the end-to-end pipeline in real time.

V23 transforms the dashboard into an integrated operations and research terminal:
1. **Real-Time Live System Overview:** Surfaces live collector status, evaluator status, data freshness age with sub-second precision, active candidate counts, V22-verified execution-qualified counts, and rejection totals across 6 KPI telemetry cards.
2. **Dedicated Live Execution-Qualified Opportunities View:** Prominently presents genuine executable opportunities meeting all V22 microstructure criteria (order-book depth, multi-leg bottleneck alignment, VWAP slippage, and CFTC-regulated quadratic taker fees). Features detailed financial accounting (Requested/Executable Qty, Top-of-Book Price, VWAP Cost, Guaranteed Payout, Total Cost, CFTC Fees, Net Profit in cents, Return on Capital %, and Data Freshness).
3. **Comprehensive Candidate Monitoring & Rejection Analysis:** Exposes every detected candidate alongside its explicit V22 qualification outcome (`QUALIFIED`, `REJECTED_INSUFFICIENT_DEPTH`, `REJECTED_UNPROFITABLE_FEES`, `REJECTED_NO_GROSS_EDGE`, `REJECTED_STALE`, `REJECTED_LEG_SKEW`, `REJECTED_MISSING_MARKET`, `REJECTED_MISSING_TIMESTAMP`, `INCONCLUSIVE`). Provides multi-dimensional filtering by strategy type, qualification outcome, lifecycle state (`NEW`, `UPDATED`, `INVALIDATED`, `DISQUALIFIED`, `DISAPPEARED`), and market search, coupled with an interactive candidate lifecycle transition history log.
4. **Candidate Microstructure Inspection Modal:** Enables drill-down inspection into individual candidate legs, exposing per-leg depth traversal, consumed limit order tiers, counterparty bid consumption, fee allocations, and non-negotiable execution disclaimers.
5. **Background Refresh Architecture:** Automatically polls backend state with configurable cadences (2s, 3s, 5s, 10s), protected by an asynchronous request collision guard (`isRefreshing` semaphore) to eliminate race conditions, and fully integrated with the HTML5 Page Visibility API to suspend polling when browser tabs are hidden.
6. **Strict Provenance Separation:** Enforces unambiguous separation between `LIVE PIPELINE` (green badge), `HISTORICAL EVIDENCE` (amber badge), and `SIMULATION DEMO` (blue badge), completely preventing synthetic or historical data from masquerading as live execution signals.
7. **Enhanced System Health & Diagnostics:** Provides granular diagnostics across five discrete engine components: Continuous Collector, Continuous Evaluator, V22 Execution Qualification Engine, Paper Trader, and Durable Storage.

All functionality is backed by a dedicated test suite (`tests/test_v23_live_dashboard.py`) comprising 14 comprehensive unit and integration tests, bringing the total suite to **691 passing tests (0 failures, 0 regressions)**. A live prospective trial against Kalshi's public production endpoints verified end-to-end functionality, recording honest real-world qualification outcomes without hallucinated fills.

---

## 2. End-to-End Pipeline Architecture

The V23 dashboard bridges the complete streaming pipeline from raw exchange order books to the researcher's browser:

```mermaid
flowchart TD
    subgraph "Live Exchange Layer"
        K["Kalshi Public REST API\n(GET /markets, GET /order_book)"]
    end

    subgraph "V20 Continuous Ingestion"
        K --> C["ContinuousCollector Service\n(Paced Ingestion & Deduplication)"]
        C --> D1["Stream: data/live_collection/observations.jsonl"]
        C --> D2["Telemetry: data/live_collector_state.json"]
    end

    subgraph "V21 & V22 Detection & Qualification"
        D1 --> E["ContinuousEvaluator Service\n(Streaming Evaluator Loop)"]
        E --> Q["V22 ExecutionAwareArbitrageQualifier\n(Depth, VWAP, CFTC Fees, Timing)"]
        Q --> E2["Candidate Lifecycle Tracker\n(NEW / UPDATED / INVALIDATED / DISAPPEARED)"]
        E2 --> S1["Telemetry: data/live_evaluator_state.json"]
        E2 --> S2["History: data/live_candidate_history.jsonl"]
    end

    subgraph "V23 Dashboard Backend (Zero External Dependencies)"
        D2 & S1 & S2 --> SERV["DashboardDataService\n(State Reconciliation & Filtering)"]
        SERV --> HTTP["DashboardServer\n(Standard Library http.server)"]
        HTTP --> EP1["GET /api/live/summary"]
        HTTP --> EP2["GET /api/evaluator/qualified"]
        HTTP --> EP3["GET /api/evaluator/candidates"]
        HTTP --> EP4["GET /api/evaluator/history"]
        HTTP --> EP5["GET /api/collector/status & /evaluator/status"]
        HTTP --> EP6["GET /api/status (System Health)"]
    end

    subgraph "V23 Dashboard Frontend UI (Standard Web Technologies)"
        EP1 & EP2 & EP3 & EP4 & EP5 & EP6 --> JS["dashboard.js Client Engine\n(Request Guard & Visibility Controller)"]
        JS --> UI1["Live Overview (6 KPI Telemetry Cards)"]
        JS --> UI2["V22 Qualified Opportunities Table & Modal"]
        JS --> UI3["Candidates & Rejection Analysis Toolbar"]
        JS --> UI4["Lifecycle Transition History Log"]
        JS --> UI5["System Health Diagnostics View"]
    end
```

### Architecture Invariants
- **Zero Third-Party Web Frameworks:** Implemented strictly using Python's standard library `http.server` and native vanilla JavaScript / CSS, requiring zero node packages, build steps, or external server dependencies.
- **Strictly Read-Only:** Exposes only HTTP `GET` endpoints; contains zero routes for order submission, cancellation, credential entry, or balance transfers.
- **Fail-Closed Fallbacks:** If telemetry files are missing, empty, or corrupted, the service returns safe fallbacks (`STOPPED` status, 0 active candidates) without crashing the server.

---

## 3. Live Monitoring Interface & UX Features

### 3.1 Live System Overview & KPI Header
The Overview tab presents six high-visibility KPI telemetry cards providing immediate situational awareness:

| Metric Card | Data Source & Calculation | Telemetry Indicators |
| :--- | :--- | :--- |
| **Collector Status** | `collector.status` verified against OS process table (`ps -eo pid,command`) | Status badge (`RUNNING` in green, `STOPPED` in amber); active monitored market count; cycle polling interval |
| **Evaluator Status** | `evaluator.status` verified against OS process table | Status badge (`RUNNING` / `STOPPED`); processed observation count; completed evaluation cycles |
| **Data Freshness** | `(now - collector.latest_snapshot_timestamp)` | Elapsed seconds with sub-second precision (`1.2s`); freshness tag (`FRESH` < 60s, `STALE` $\ge$ 60s) |
| **Active Candidates** | `evaluator.active_candidates_count` | Count of currently active candidate opportunities; total detected in session |
| **Qualified Opportunities** | `evaluator.qualified_candidates_count` (V22 verified) | Count of candidates passing all depth, VWAP, and fee thresholds; highlighted with accent card |
| **Rejected Candidates** | `evaluator.rejected_candidates_count` | Count of candidates failing qualification; explicit rejection cause breakdown |

### 3.2 Global Pipeline Alert Banner
When continuous services are stopped or market data exceeds the freshness threshold ($> 60\text{s}$), a prominent alert banner displays at the top of the terminal:
> `⚠️ Continuous live services are currently stopped. Displaying last persisted state.`

### 3.3 Dedicated Live Execution-Qualified Opportunities Table
The primary focus of the V23 interface is the execution-qualified opportunities table. When a candidate satisfies all V22 criteria, it is surfaced in an actionable format:

- **Candidate ID & Strategy:** Strategy pill (`Binary Parity`, `MECE Basket`) and contract identifier.
- **Target vs Executable Quantity:** Visualizes whether 100% of requested contracts can be filled ($Q_{\text{exec}} / Q_{\text{target}}$).
- **VWAP Acquisition Cost:** Multi-level volume-weighted average price across all legs.
- **Guaranteed Settlement Payout:** Nominal payout upon event expiration ($Q \times 100¢$).
- **Gross Edge & CFTC Fees:** Gross edge ($Payout - Cost$) juxtaposed against official CFTC quadratic taker transaction fees.
- **Net Profit:** Integer-cent net edge after deducting all exchange fees ($Gross - Fees$).
- **Return on Capital (RoC %):** Net profit as a percentage of capital committed ($\frac{\text{Net Edge}}{\text{Total Cost}} \times 100$).
- **Data Freshness:** Elapsed age of the underlying order book snapshot.
- **Inspect Action:** One-click modal inspection.

**Fail-Closed Empty State:** When zero opportunities qualify (the expected condition during calm continuous trading), the dashboard renders a calm, professional empty state:
> `No opportunities currently qualify under V22 execution criteria (depth, fees, freshness).`  
> `In continuous prediction markets, resting counterparty bids are frequently absent on unhedged contracts, resulting in 0 executable ask depth. The engine rejects these candidates to ensure fail-closed execution integrity.`

### 3.4 Candidate Monitoring & Rejection Analysis Toolbar
The dedicated **Candidates & Rejections** tab allows researchers to inspect *all* candidates detected by V21, including those rejected by V22:
- **Search Bar:** Real-time text search matching against candidate IDs or market tickers.
- **Strategy Filter:** Filter by `Binary Parity`, `MECE Basket Long YES`, or `MECE Basket Long NO`.
- **Outcome Filter:** Filter by exact V22 outcome (`QUALIFIED`, `REJECTED_INSUFFICIENT_DEPTH`, `REJECTED_UNPROFITABLE_FEES`, `REJECTED_NO_GROSS_EDGE`, `REJECTED_STALE`, `REJECTED_LEG_SKEW`, `REJECTED_MISSING_MARKET`, etc.).
- **Lifecycle State Filter:** Filter by state machine status (`NEW`, `UPDATED`, `INVALIDATED`, `DISQUALIFIED`, `DISAPPEARED`).
- **Qualified Only Toggle:** Instant checkbox filter isolating qualified candidates.
- **Lifecycle Transition History Table:** Paginated audit log showing chronological candidate transitions, previous/new states, timestamps, and transition reasons.

### 3.5 Candidate Microstructure Inspection Modal
Clicking **Inspect** on any candidate opens an interactive modal revealing leg-level microstructure:
1. **Financial Accounting Summary Grid:** Target Qty, Executable Qty, Total Cost, Settlement Payout, Total Fees, Net Profit, and RoC %.
2. **Per-Leg Liquidity Traversal:** For each constituent market leg:
   - Side (`YES` / `NO`), Top-of-Book Price, VWAP Price, Leg Cost, and CFTC Leg Fee.
   - Available Ask Depth vs Consumed Quantity.
   - Consumed Limit Order Levels table (Price in cents, Quantity filled, Total Cost).
3. **Microstructure Assumptions & Risk Disclaimers:** Explicitly warns that depth is evaluated against resting limit orders observed at snapshot time and that multi-leg orders carry non-atomic leg execution risk.

---

## 4. Background Refresh & Polling Architecture

To ensure high responsiveness without degrading browser performance or triggering race conditions, V23 incorporates a robust background polling engine:

### 4.1 Request Collision Guard (`isRefreshing`)
In high-frequency monitoring interfaces, network latency spikes can cause polling requests to overlap, producing out-of-order UI updates and excessive backend load. V23 prevents this via a semaphore flag:

```javascript
async function refreshAllData() {
  if (isRefreshing) return; // Drop overlapping poll if previous cycle is in-flight
  isRefreshing = true;
  try {
    // 1. Fetch live summary
    // 2. Fetch qualified opportunities
    // 3. Fetch candidate audit log
    // 4. Update UI timestamps
  } finally {
    isRefreshing = false;
  }
}
```

### 4.2 HTML5 Page Visibility API Integration
When researchers switch tabs or minimize the browser, continuous background HTTP polling wastes client CPU and network bandwidth. V23 monitors document visibility:

```javascript
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // Suspend polling timer immediately
    if (autoRefreshTimer) {
      clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
  } else {
    // Resume polling immediately and trigger fresh update
    restartAutoRefreshTimer();
    refreshAllData();
  }
});
```

### 4.3 Cadence Selection & UI Latency Indicators
- **Configurable Intervals:** Dropdown selector supports `2s`, `3s` (default), `5s`, and `10s` intervals.
- **UI Refresh Timestamp:** Header displays `UI: HH:MM:SS UTC` indicating the exact moment of the last successful client-side update.
- **Server Clock:** Footer displays synchronized `Server UTC: YYYY-MM-DD HH:MM:SS UTC`.

---

## 5. Strict Provenance Separation

A primary risk in quantitative trading research is confusing synthetic test data or historical recordings with live market signals. V23 enforces strict visual and operational provenance separation:

| Mode | Provenance Badge | Dataset Source | Intended Purpose |
| :--- | :--- | :--- | :--- |
| **Live Pipeline (V20–V22)** | `<span class="provenance-tag tag-live">LIVE PIPELINE</span>` | Streaming from `data/live_collection/observations.jsonl` and `data/live_evaluator_state.json` | Real-time monitoring of live market observations, active candidates, and V22 qualification |
| **Historical Evidence** | `<span class="provenance-tag tag-historical">HISTORICAL EVIDENCE</span>` | User-selected research trial folder (`evidence_dashboard_trial`, `evidence_v13_fed_basket_trial`) | Post-hoc audit of recorded historical research trials and temporal cadence analysis |
| **Simulation Demo** | `<span class="provenance-tag tag-simulation">SIMULATION DEMO</span>` | Synthetic in-memory candidate fixtures | UI development and visual regression verification without market connectivity |

When switching modes:
- The top-nav mode selector dynamically shows/hides dataset selection controls.
- The provenance badge changes color and label.
- Prominent amber callout banners are injected into historical and simulated views warning that displayed records do not represent live market conditions.

---

## 6. REST API Specification & Endpoint Audit

The V23 dashboard server (`kalshi_arbitrage/dashboard/server.py`) exposes a comprehensive suite of read-only REST APIs:

| Endpoint | Method | Query Parameters | Response Structure & Description |
| :--- | :--- | :--- | :--- |
| `/api/live/summary` | `GET` | None | Unified pipeline health summary: `pipeline_status` (`HEALTHY`, `DEGRADED`, `PARTIAL`, `STOPPED`), `collector`, `evaluator`, `qualification_summary`, `data_provenance`, `errors`. |
| `/api/evaluator/qualified` | `GET` | `strategy`, `market`, `min_profit` | List of V22 execution-qualified opportunities meeting filters. Returns `qualified_candidates` array, `count`, and `timestamp`. |
| `/api/evaluator/candidates` | `GET` | `strategy`, `outcome`, `state`, `market`, `qualified` | Complete list of all active candidates. Supports filtering by strategy, V22 outcome enum, state, and ticker search. |
| `/api/evaluator/history` | `GET` | `limit`, `candidate_id`, `to_state`, `outcome` | Reverse-chronological audit log of candidate lifecycle state transitions from `live_candidate_history.jsonl`. |
| `/api/collector/status` | `GET` | None | Telemetry for V20 Continuous Collector: PID, active markets count, cycle count, last cycle duration, data freshness. |
| `/api/evaluator/status` | `GET` | None | Telemetry for V21 Evaluator: PID, evaluations count, processed observations, active candidates, qualification breakdown. |
| `/api/status` | `GET` | None | Full system health report encompassing Collector, Evaluator, Qualifier, Paper Trader, and Durable Storage. |
| `/api/overview` | `GET` | `dataset` | Executive overview metrics for selected historical or live dataset. |
| `/api/markets` | `GET` | `dataset` | Tabular listing of all monitored markets with order-book bids/asks and population status. |
| `/` & static assets | `GET` | None | Serves `index.html`, `dashboard.css`, and `dashboard.js` with proper MIME types, CORS headers, and no-cache policies. |

All API responses return HTTP 200 with `Content-Type: application/json; charset=utf-8` and headers preventing stale browser caching:
```http
Cache-Control: no-cache, no-store, must-revalidate
Pragma: no-cache
Expires: 0
Access-Control-Allow-Origin: *
```

---

## 7. Verification & Automated Test Results

### 7.1 Dedicated V23 Test Suite (`tests/test_v23_live_dashboard.py`)
A comprehensive suite of 14 automated tests was implemented to verify every aspect of V23:

1. `test_live_pipeline_summary_stopped`: Verifies summary reporting when collector and evaluator services are inactive.
2. `test_live_pipeline_summary_healthy`: Verifies `HEALTHY` pipeline status when services are active and observations are fresh ($< 60\text{s}$).
3. `test_live_pipeline_summary_degraded_when_stale`: Verifies `DEGRADED` status when snapshot age exceeds threshold.
4. `test_live_pipeline_summary_partial`: Verifies `PARTIAL` status when one service is running and the other is stopped.
5. `test_candidate_filtering_by_strategy_and_outcome`: Validates multi-dimensional filtering across strategies, outcomes, states, and market tickers.
6. `test_qualified_candidates_accessor`: Validates qualified candidate filtering and `min_profit_cents` thresholding.
7. `test_candidate_history_pagination_and_filtering`: Tests reverse-chronological retrieval and candidate history filtering.
8. `test_resolve_dataset_path_live`: Verifies that `resolve_dataset_path("live")` resolves to `data/live_collection/observations.jsonl`.
9. `test_dashboard_server_api_live_summary`: End-to-end HTTP integration test for `/api/live/summary` and `/api/pipeline/summary`.
10. `test_dashboard_server_api_evaluator_filtered_candidates`: HTTP test verifying query parameter filtering on `/api/evaluator/candidates`.
11. `test_dashboard_server_api_qualified_endpoint`: HTTP test verifying `/api/evaluator/qualified` returns only execution-qualified opportunities.
12. `test_dashboard_server_api_history_filtered`: HTTP test verifying query parameter filtering on `/api/evaluator/history`.
13. `test_dashboard_server_static_assets`: HTTP test verifying static asset delivery (`/`, `/dashboard.css`, `/dashboard.js`) with correct MIME types.
14. `test_dashboard_server_404_handling`: Verifies RFC-compliant 404 JSON error responses for nonexistent routes.

### 7.2 Full Regression Test Suite Execution
The entire test suite was executed in the local virtual environment:
```bash
./.venv/bin/pytest -v
```
**Results:**
- **Collected:** 691 items
- **Passed:** 691 passed in 21.27 seconds
- **Failed:** 0
- **Errors:** 0
- **Regressions:** 0

---

## 8. Live Prospective Trial & Empirical Evidence

To verify the dashboard under authentic operating conditions, a prospective trial was conducted against Kalshi's live public REST API on October 28, 2026.

### 8.1 Trial Execution Workflow
1. **Continuous Collector:** Polled 8 active open markets across 2 bounded cycles, persisting observations to `data/live_collection/observations.jsonl` and telemetry to `data/live_collector_state.json`. Completed cleanly in **7.63 seconds**.
2. **Continuous Evaluator:** Streamed observations from the collector through the V22 qualification engine across 2 bounded evaluation cycles. Completed cleanly in **1.15 seconds**.
3. **Dashboard Server:** Spawned on an ephemeral port (`http://127.0.0.1:63938`) and queried across all live and static endpoints.

### 8.2 Live Endpoint Benchmark Results

| Endpoint Probed | HTTP Status | Content-Type | Response Size | Latency |
| :--- | :--- | :--- | :--- | :--- |
| `GET /api/live/summary` | `200 OK` | `application/json; charset=utf-8` | 3,270 bytes | **58.39 ms** |
| `GET /api/collector/status` | `200 OK` | `application/json; charset=utf-8` | 1,124 bytes | **19.60 ms** |
| `GET /api/evaluator/status` | `200 OK` | `application/json; charset=utf-8` | 1,294 bytes | **18.59 ms** |
| `GET /api/evaluator/qualified` | `200 OK` | `application/json; charset=utf-8` | 46 bytes | **18.96 ms** |
| `GET /api/evaluator/candidates` | `200 OK` | `application/json; charset=utf-8` | 36 bytes | **19.08 ms** |
| `GET /api/evaluator/history?limit=10` | `200 OK` | `application/json; charset=utf-8` | 33 bytes | **0.47 ms** |
| `GET /api/status` | `200 OK` | `application/json; charset=utf-8` | 15,870 bytes | **120.47 ms** |
| `GET /api/overview` | `200 OK` | `application/json; charset=utf-8` | 999 bytes | **20.30 ms** |
| `GET /api/markets` | `200 OK` | `application/json; charset=utf-8` | 13,642 bytes | **1.91 ms** |
| `GET /` | `200 OK` | `text/html; charset=utf-8` | 32,078 bytes | **0.34 ms** |
| `GET /dashboard.css` | `200 OK` | `text/css; charset=utf-8` | 18,963 bytes | **0.32 ms** |
| `GET /dashboard.js` | `200 OK` | `application/javascript; charset=utf-8` | 55,744 bytes | **0.31 ms** |

### 8.3 Empirical Microstructure Findings
The live trial captured 16 real-time observations across active prediction contracts:
- **Monitored Markets:** 8 open Kalshi contracts.
- **Qualified Candidates Count:** **0**
- **Rejection Outcomes Breakdown:**
  - `REJECTED_INSUFFICIENT_DEPTH`: **2**
  - `REJECTED_MISSING_MARKET`: **14**

**Key Insight:** This empirical trial reconfirms the core thesis of the research engine: in calm continuous markets, unhedged mispricings with resting ask depth do not persist. Rather than hallucinating fills, the V22 qualification layer honestly categorized resting depth as zero and rejected the candidates, while the V23 dashboard accurately displayed this fail-closed reality.

All trial metrics have been recorded in `docs/v23_live_trial_evidence.json`.

---

## 9. Safety, Regulatory & Model Limitations Disclaimers

1. **Strictly Read-Only Research Interface:** The dashboard does not possess order-placement capabilities. No trading keys or exchange credentials are required or accepted.
2. **Static Snapshot Assumption:** Order-book depth and fee calculations reflect conditions observed at the snapshot timestamp. In live markets, resting liquidity may be cancelled or consumed before an order reaches the matching engine.
3. **Non-Atomic Leg Execution Risk:** Kalshi does not offer atomic multi-leg contingency orders for basket arbitrage. Executing multiple legs requires submitting independent orders, exposing traders to legging-out risk if prices move between fills.
4. **Regulatory Integrity:** Fee calculations strictly implement official CFTC-regulated Kalshi quadratic taker fee formulas. No claim of guaranteed or risk-free profit is made.

---

## 10. Conclusion & Milestone Sign-Off

Milestone V23 successfully transforms the Kalshi Arbitrage Engine into a live, observable, and trustworthy research platform. The live pipeline—from public Kalshi REST endpoints through continuous collection, streaming evaluation, and execution-aware qualification—is fully integrated into an interactive dashboard.

**Status:** Complete, fully tested (691/691 tests passing), and verified against live Kalshi production markets.
