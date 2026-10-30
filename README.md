# Kalshi Arbitrage Engine

A deterministic quantitative research, market microstructure simulation, and empirical evaluation engine for Kalshi prediction markets (Milestones V0–V25).

> **IMPORTANT REGULATORY & SAFETY NOTICE:**
> This repository is strictly a **quantitative research, market observation, and paper-trading evaluation system**. It connects exclusively to read-only public REST market data endpoints. **It contains zero capabilities for real-money execution, live order routing, exchange credential handling, or deposit/withdrawal interactions.** All trading evaluations and settlements occur in deterministic simulation ledgers with exact integer-cent double-entry accounting reconciliation.

---

## 1. Project Overview

The **Kalshi Arbitrage Engine** is a rigorous, open-source quantitative research platform built to investigate market efficiency, cross-contract pricing relationships, and potential arbitrage in binary prediction markets.

Kalshi binary event contracts settle at exactly **\$1.00 (100¢)** if the underlying event occurs, and **\$0.00 (0¢)** if it does not. The engine provides mathematical frameworks, real-time concurrent REST collectors, deterministic event snapshot reconstructors, fee models, and causal attribution analyzers to determine whether executable, fee-adjusted arbitrage exists in practice.

---

## 2. Architecture & Data Flow

The engine enforces an uncompromising separation between market ingestion, snapshot reconstruction, deterministic evaluation, and accounting:

```mermaid
flowchart TD
    A["Public Kalshi REST API (Read-Only)"] --> B["KalshiRestClient (GET only, no credentials)"]
    B --> C["BroadMarketDiscoveryEngine (7-Tier MECE Taxonomy)"]
    C --> D["Synchronized Concurrent REST Ingestion (Mean Skew ~1.0-1.4s)"]
    D --> E["EventSnapshotReconstructor (Cycle-Bound & Skew Filters)"]
    E --> F["Reconstructed Event Snapshots (Deterministic Isolation)"]
    F --> G["HistoricalBasketReplayEngine & OpportunityEconomicsAnalyzer"]
    G --> H["Execution Depth & VWAP Model (Counterparty Bid Derivation)"]
    G --> I["CFTC Quadratic Taker Fee Schedule (Volume Tiers)"]
    G --> J["Two-Sided Attribution (Long YES vs Long NO)"]
    J --> K["OpportunityEpisodeTracker (State Independence & Duration)"]
    J --> L["Durable JSONL Storage & Cryptographic Manifests (SHA-256)"]
```

### Key Modules:
- **`kalshi_arbitrage/rest_client.py`:** Strictly read-only public REST client for market, order-book, and event ingestion.
- **`kalshi_arbitrage/discovery.py`:** Automated broad-market catalog scanner with a 7-tier conservative MECE taxonomy.
- **`kalshi_arbitrage/snapshot_reconstruction.py`:** Chronological event snapshot reconstructor enforcing strict basket isolation and sub-5.0s cross-leg synchronization.
- **`kalshi_arbitrage/attribution.py`:** Two-sided (Long YES and Long NO) economic attribution engine verifying microstructure invariants and failure causes.
- **`kalshi_arbitrage/execution.py`:** Full order-book depth traversal, volume-weighted average price (VWAP) calculation, and multi-leg bottleneck sizing.
- **`kalshi_arbitrage/fee_model.py`:** Exact implementation of Kalshi's CFTC-regulated quadratic taker and maker fee formulas.
- **`kalshi_arbitrage/continuous_collector.py`:** Continuous live market data collection service with concurrency and backoff.
- **`kalshi_arbitrage/continuous_evaluator.py`:** Continuous live candidate detection and lifecycle state tracking.
- **`kalshi_arbitrage/execution_qualifier.py`:** Deterministic execution-aware qualification engine evaluating order-book depth, multi-leg bottlenecks, and CFTC quadratic taker fees.
- **`kalshi_arbitrage/breadth_scanner.py`:** Standalone Breadth-First Scanner decoupling Stage A (catalog discovery), Stage B (zero-API candidate screening), and Stage C (bounded targeted order-book qualification).
- **`kalshi_arbitrage/historical_opportunity_tracker.py`:** Deep historical analytics engine calculating candidate lifetime, persistence across cycles, qualification funnels, false-positive forensics, and category concentration.
- **`kalshi_arbitrage/alert_engine.py`:** Asynchronous alert dispatch, persistent deduplication, material-change filtering, secret redaction, and multi-channel notifications.
- **`kalshi_arbitrage/dashboard/`:** Live monitoring terminal and read-only REST APIs visualizing pipeline health, breadth scanner progress, candidate lifecycle transitions, and opportunity alert streams.

---

## 3. Implemented & Verified Capabilities (V0–V25 & Phase 2F Breadth Scanner)

| Capability Area | Implemented Functionality | Verification Status |
| :--- | :--- | :--- |
| **Financial Arithmetic** | Integer-cent payouts, exact Decimal currency representations, rejection of float casting. | Verified by 100% test coverage |
| **Contract Semantics** | Binary parity complement ($\text{YES} + \text{NO} \equiv 1$), finite-outcome MECE partitions ($N \ge 2$). | Verified across $N \in [2, 14]$ |
| **Public Market Data** | Read-only REST client (`get_market`, `get_order_book`, `list_events`, `list_markets`). | Verified on live Kalshi public API |
| **MECE Classification** | 7-tier taxonomy distinguishing range partitions, pairwise choices, and open-ended lists. | Verified on 100+ Kalshi events |
| **Concurrent Ingestion** | High-synchronization multi-thread REST polling (reducing mean skew from 8.4s to 1.0–1.4s). | Verified in V16–V18 trials |
| **Snapshot Reconstruction** | Deterministic multi-leg cycle matching, timestamp validation, strict cross-event isolation. | Verified in 120+ live snapshots |
| **Two-Sided Evaluation** | Long YES payout ($Q \times 100¢$), Long NO payout ($Q \times (N-1) \times 100¢$), spread invariant. | Mathematically proven & verified |
| **Fee Modeling** | Official CFTC quadratic taker fee: $\lceil M \times \text{Rate} \times C \times P(1-P) \times 100 \rceil$. | Verified against exchange specs |
| **Depth & Liquidity** | Multi-level order-book consumption, limiting bottleneck identification, VWAP pricing. | Verified with synthetic & live data |
| **Episode Tracking** | Cryptographic state hashing, distinct episode duration tracking, repeated state filtering. | Verified in V16–V18 experiments |
| **Evidence Provenance** | Atomic writes, schema versioning ("1.0", "1.1"), byte-level SHA-256 manifests. | 100% manifest verification pass |
| **Paper Ledger** | Append-only double-entry ledger, solvency invariants, pre-trade risk controls. | Verified with adversarial tests |
| **Packaging & Setup** | Zero external runtime dependencies; standard-library-only implementation. | Verified in clean isolated virtualenv |
| **Continuous Live Ingestion (V20)** | Long-running live REST collector, backoff, pacing, thread-safe deduplication, telemetry APIs. | Verified on live Kalshi public API |
| **Continuous Detection (V21)** | Real-time candidate detection, lifecycle state management (NEW/UPDATED/DISQUALIFIED/INVALIDATED/DISAPPEARED), change fingerprinting. | Verified across 658 automated tests |
| **Execution Qualification (V22)** | Realistic depth traversal, multi-leg bottleneck sizing, CFTC quadratic fees per leg, explicit rejection taxonomy. | Verified across 677 automated tests |
| **Live Dashboard Integration (V23)** | Real-time pipeline monitoring, V22 qualified opportunity display, candidate rejections audit, auto-refresh with collision guard, Page Visibility suspension, strict provenance separation (LIVE / HISTORICAL / SIMULATION). | Verified across 691 automated tests |
| **Alerts & Reliability (V24)** | Qualified-only opportunity alerts, persistent deduplication across daemon restarts, material-change filtering, re-alert cooldown, multi-channel dispatch (local history, macOS desktop, webhook), strict secret redaction, and bounded queues. | Verified across 706 automated tests |
| **Extended Live Validation (V25)** | Full continuous pipeline live trial under real market conditions, 9 automated fault-injection recovery scenarios, mathematical execution qualification reconciliation, and SHA-256 evidence manifest. | Verified across 719 automated tests |
| **Breadth-First Scanner (Phase 2F)** | Full-catalog pagination (168k+ markets), Stage B zero-API screening (364k+ strategies in 2.4s), constituent leg gating ($\le 8$ legs), global token bucket rate limiting (5 req/s), connection pooling, and decoupled quote staleness. | Verified across 946 automated tests |
| **Historical Opportunity Tracking** | Deep historical analytics engine calculating candidate lifetime, multi-cycle persistence, qualification funnels, fee haircuts, and market concentration across all persisted datasets. | Verified with real trial datasets |

---

## 4. Key Empirical Research Findings (V16–V18)

Across three bounded prospective research trials covering **45 markets across 6 events** in multiple economic domains (Technology IPOs, Annual GDP Growth brackets, and Federal Reserve Monetary Policy decisions), the engine established the following empirical and mathematical findings:

### 1. The Fundamental Two-Sided Complementarity Theorem
For any $N$-outcome mutually exclusive and collectively exhaustive event basket:
$$\text{Gross Edge}_{\text{YES}} = 100¢ - \sum_{i=1}^N \text{Ask}_{\text{YES}, i}$$
$$\text{Gross Edge}_{\text{NO}} = \sum_{i=1}^N \text{Bid}_{\text{YES}, i} - 100¢$$
$$\text{Gross Edge}_{\text{YES}} + \text{Gross Edge}_{\text{NO}} \equiv -\sum_{i=1}^N (\text{Ask}_{\text{YES}, i} - \text{Bid}_{\text{YES}, i}) = -\sum_{i=1}^N \text{Spread}_i \le 0$$
- In any limit order book with non-negative spreads, **both sides cannot simultaneously be profitable**.
- The non-arbitrage gap between Long YES and Long NO is identically equal to the aggregate spread.

### 2. Market Maker Over-Round Pricing
In calm, continuous trading conditions, market makers consistently post limit orders such that the true settlement probability sits securely inside the aggregate spread:
$$\sum_{i=1}^N \text{Bid}_{\text{YES}, i} \le 100¢ \le \sum_{i=1}^N \text{Ask}_{\text{YES}, i}$$
- **Cohort A (Tech IPO & 14-Leg GDP Brackets):** Mean aggregate spread was **56.67¢** (16¢ to 82¢). Long YES gross edges ranged from **-31¢ to -8¢**; Long NO gross edges ranged from **-51¢ to -8¢**.
- **Cohort B (Monetary Policy Decisions):** Mean aggregate spread was **6.67¢** (5¢ to 9¢). Long YES gross edges ranged from **-6¢ to -2¢**.

### 3. Tail-Risk Asymmetry (Long NO Bottleneck)
In multi-outcome events, deep out-of-the-money legs (e.g. extreme rate cuts) have resting asks at 1¢, but **zero resting bids** (`Bid = None`). Because buying NO contracts consumes NO asks (which derive from resting YES bids: $\text{Ask}_{\text{NO}} = 100 - \text{Bid}_{\text{YES}}$), a missing YES bid results in **zero executable depth** for Long NO baskets.

### 4. Absence of Static Arbitrage in Liquid Markets
**Zero qualified arbitrage opportunities were observed across all 120+ reconstructed snapshots in liquid financial/economic markets.** Resting limit order books in active series like `KXINX`, `KXNASDAQ100`, and `KXFEDDECISION` do not contain unhedged mispricings during continuous trading.

### 5. Phase 2F Full-Catalog Breadth Scanner Trials & Opportunity Discovery
During Phase 2F full-catalog discovery across 168,000+ contracts, the engine demonstrated:
- **Trial 1 (1-Hour Continuous):** Screened 168,097 markets across 16,455 events; identified Stage B screening metadata staleness.
- **Trial 2 (15-Minute Controlled):** Verified Stage B correction, producing 10 preliminary candidates and proving connection pooling (4x latency drop). Identified event pre-filter catalog timestamp staleness.
- **Final Validation Trial:** Decoupled pre-filter quote staleness. The qualifier dispatched 212 API requests across 8 candidates, rejecting 2 by leg gating ($> 8$ legs), 3 due to quotes moving, 4 due to CFTC regulatory fee floors, and successfully qualified **1 execution-viable MECE arbitrage opportunity** (`MECE-YES:KXMODELHIGH-27-1550` with 24¢ gross edge, 9¢ CFTC fees, 15¢ net profit, and 19.74% ROC).
- For complete forensic reconstruction, contract settlement evaluation, and the final economic validation research verdict, see:
  - **[`docs/FINAL_ECONOMIC_VALIDATION_REPORT.md`](file:///Users/vikramaditya/Disk/Projects/Kalshi/docs/FINAL_ECONOMIC_VALIDATION_REPORT.md)** (Final economic validation, contract rule investigation, and paper-trading verdict)
  - **[`docs/FINAL_OPPORTUNITY_VERIFICATION_AND_RESEARCH_REPORT.md`](file:///Users/vikramaditya/Disk/Projects/Kalshi/docs/FINAL_OPPORTUNITY_VERIFICATION_AND_RESEARCH_REPORT.md)**
  - **[`docs/FINAL_PROJECT_COMPLETION_REPORT.md`](file:///Users/vikramaditya/Disk/Projects/Kalshi/docs/FINAL_PROJECT_COMPLETION_REPORT.md)**

---

## 5. Important Project Limitations

1. **No Live Order Execution:** The engine is strictly a research and simulation system. It cannot and will not route orders to an exchange.
2. **No Demonstrated Profitability:** Empirical experiments demonstrate that static resting limit order books consistently exhibit negative gross and net edges.
3. **Bounded Sampling:** Research experiments were conducted over discrete observation windows during continuous trading hours. They do not represent 24/7 market behavior.
4. **WebSocket Authentication:** While the WebSocket v2 protocol handler is fully implemented and verified via deterministic simulation fixtures, Kalshi's production WebSocket requires RSA key-pair authentication even for public subscriptions. Production streaming was not connected to live keys to maintain the read-only safety boundary.
5. **Execution Risk ("Legging Out"):** In live multi-leg execution, fills occur sequentially across legs. Even if an ephemeral mispricing appears, attempting to cross multiple books exposes the trader to execution latency and adverse selection.

---

## 6. Reproducible Setup & Developer Guide

### 6.1 Prerequisites
- Python **3.9 or higher** (tested on Python 3.9 through 3.12).
- Zero external runtime dependencies (standard library only).
- `pytest` for running test suites.

### 6.2 Installation from Source

```bash
# 1. Clone repository
git clone https://github.com/vikramaditya7z/engine-kal-t-1.git
cd engine-kal-t-1

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install in editable mode with development dependencies
pip install --upgrade pip
pip install -e ".[dev]"
```

### 6.3 Running Tests

```bash
# Run the complete test suite (946 tests)
pytest

# Run tests in quiet mode
pytest -q

# Run specific milestone test suites
pytest tests/test_v18_economics.py tests/test_v19_release_gates.py -v
```

### 6.4 Reproducing Historical Research Evidence

```bash
# Replay recorded V12 trial evidence (50 observations -> 10 snapshots)
python scripts/evaluate_v10.py --evidence-dir evidence_v12_fed_basket_trial

# Audit V16, V17, and V18 trial manifests and data provenance
python -c "
import json
for m in ['evidence_v16_streaming_trial', 'evidence_v17_broad_market_trial', 'evidence_v18_economics_trial']:
    meta = json.loads(open(f'{m}/manifest.json').read())
    print(f'Trial {m}: {len(meta.get(\"files\", {}))} manifested files verified.')
"
```

### 6.5 Running a Bounded Prospective Collection Trial

```bash
# Run a 10-cycle bounded prospective trial across broad-market events
python scripts/collect_v17.py \
  --output-dir evidence_v17_test_trial \
  --cycles 10 \
  --cycle-interval 2.0 \
  --max-events-to-scan 50 \
  --max-selected-events 3

# Run a two-sided economic attribution trial comparing distinct cohorts
python scripts/evaluate_v18.py \
  --output-dir evidence_v18_test_trial \
  --cycles 10 \
  --cycle-interval 2.0 \
  --concurrency 8
```

### 6.6 Running the Continuous Live Market Data Service (V20)

```bash
# Run the continuous live market data collector (runs indefinitely until Ctrl+C)
python scripts/run_continuous_collector.py \
  --environment production \
  --interval 2.0 \
  --concurrency 4 \
  --max-markets 30 \
  --output-dir data/live_collection

# Or monitor specific market tickers:
python scripts/run_continuous_collector.py \
  --tickers KXFEDDECISION-26OCT-T4.50 KXFEDDECISION-26OCT-T4.75 \
  --interval 1.0

# Graceful shutdown:
# Press Ctrl+C or send SIGTERM (in-flight cycles finish cleanly, writing manifest and state)
```

### 6.7 Running the Live Monitoring & Research Dashboard (V23)

```bash
# Start read-only HTTP live dashboard terminal on http://127.0.0.1:8080
python scripts/run_dashboard.py --port 8080

# Key live dashboard endpoints:
# GET http://127.0.0.1:8080/                      (Interactive web terminal UI)
# GET http://127.0.0.1:8080/api/live/summary      (Unified pipeline status, health, and error tracking)
# GET http://127.0.0.1:8080/api/evaluator/qualified (V22 execution-qualified opportunities)
# GET http://127.0.0.1:8080/api/evaluator/candidates (Active candidates with outcome filters)
# GET http://127.0.0.1:8080/api/evaluator/history   (Candidate lifecycle transition history)
# GET http://127.0.0.1:8080/api/alerts/status     (Opportunity alert engine operational status)
# GET http://127.0.0.1:8080/api/alerts/history    (Opportunity alert history log)
# GET http://127.0.0.1:8080/api/collector/status  (Continuous collector health and telemetry)
# GET http://127.0.0.1:8080/api/evaluator/status  (Continuous evaluator health and telemetry)
# GET http://127.0.0.1:8080/api/status            (Comprehensive system-wide diagnostics)
```

### 6.8 Running the Continuous Live Arbitrage Detector & Alert Engine (V21–V24)

```bash
# Evaluate fresh observations with asynchronous qualified-opportunity alerts:
python scripts/run_continuous_evaluator.py \
  --observations-file data/live_collection/observations.jsonl \
  --alerts-enabled

# Deliver alerts via webhook with custom thresholds:
python scripts/run_continuous_evaluator.py \
  --observations-file data/live_collection/observations.jsonl \
  --webhook-url "https://discord.com/api/webhooks/YOUR_WEBHOOK" \
  --profit-change-threshold 5 \
  --quantity-change-threshold 1

# Deliver native desktop notifications on macOS:
python scripts/run_continuous_evaluator.py \
  --observations-file data/live_collection/observations.jsonl \
  --desktop-alerts

# Run in pure evaluation mode with alerts disabled:
python scripts/run_continuous_evaluator.py --no-alerts
```

### 6.9 Running the End-to-End Live Validation Runner (V25)

```bash
# Execute the complete sustained live validation trial with pre-flight check,
# continuous ingestion, evaluation, live dashboard, 9 fault scenarios, and SHA-256 evidence:
python scripts/run_live_validation.py \
  --duration 60 \
  --interval 2.0 \
  --max-markets 5 \
  --port 8089 \
  --output-dir data/v25_validation_evidence
```

### 6.10 Running the Breadth-First Arbitrage Scanner (Phase 2F)

```bash
# Run a bounded, single-pass breadth-first scan across all active Kalshi markets:
python scripts/run_breadth_scanner.py \
  --single-pass \
  --request-rate 5.0 \
  --concurrency 4 \
  --max-candidate-legs 8 \
  --min-net-profit 5 \
  --output-dir data/breadth_scanner/my_scan

# Run a continuous scanner session with a 1-hour maximum duration and lookahead gating:
python scripts/run_breadth_scanner.py \
  --duration 3600 \
  --interval 30.0 \
  --request-rate 5.0 \
  --concurrency 4 \
  --max-candidate-legs 8 \
  --min-net-profit 5 \
  --output-dir data/breadth_scanner/session_1hour
```

### 6.11 Running Historical Opportunity Analytics & Replay

```python
from kalshi_arbitrage.historical_opportunity_tracker import HistoricalOpportunityTracker

# Analyze a persisted scanner session dataset:
tracker = HistoricalOpportunityTracker("data/breadth_scanner/trial_validation_final")
report = tracker.analyze()

print(f"Total Cycles: {report.total_cycles}")
print(f"Preliminary Candidates: {report.total_preliminary_candidates}")
print(f"Qualified Arbitrages: {report.total_qualified_candidates}")
print(f"Rejection Breakdown: {report.rejection_breakdown}")
print(f"Category Concentration: {report.category_concentration}")
```

---

## 7. Security and Safety Policy

- **No Private Keys:** The repository contains zero private API keys, passphrases, or secrets.
- **Read-Only Endpoints:** All network interactions are strictly constrained to public `GET` requests against Kalshi's public market-data endpoints (`https://api.elections.kalshi.com/trade-api/v2`).
- **No Mutating Endpoints:** The HTTP client contains zero logic for `POST /portfolio/orders`, `DELETE /portfolio/orders`, or any order routing actions.
- **Double-Entry Solvency:** Paper portfolios enforce cash balance checks and fail-closed margin reconciliation.

---

## 8. License & Disclaimer

This software is provided for educational and academic research purposes only. Nothing in this repository constitutes financial, investment, legal, or tax advice. Prediction market trading involves significant risk of loss. Past performance in historical replay or paper simulation does not guarantee future results.
