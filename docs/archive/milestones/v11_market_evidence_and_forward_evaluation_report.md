# Kalshi Arbitrage Engine V11: Sustained Market Evidence Collection, Forward Evaluation & Research Readiness Report

**Date:** 2026-10-12
**Milestone:** V11 Sustained Market Evidence Collection, Forward Evaluation & Research Readiness
**Author:** Quantitative Systems Engineering & Audit Agent
**Codebase Commit Baseline:** `39fbaa59b1f32b0934bc35524990a6c6d873b541` (Branch: `main`)
**Overall System Verdict:** **EVIDENCE PIPELINE VALIDATED**
**Strategy Economic Edge Status:** **INCONCLUSIVE (PERSISTENT DATA SPARSITY / EMPTY BOOKS)**
**Production Capital Deployment Decision:** **STRICTLY REJECTED / DO NOT DEPLOY LIVE CAPITAL**

---

## 1. Executive Summary & Release Verdict

### 1.1 Milestone Release Verdict: **EVIDENCE PIPELINE VALIDATED**
The Kalshi Arbitrage Engine Milestone V11 has successfully established an operational, auditable, read-only market evidence collection system and prospective forward paper-evaluation pipeline.

The collection, diagnostic logging, data quality auditing, forward paper trading, and V10 replay equivalence pipelines were verified both deterministically through automated test suites (501 passing tests repository-wide, zero failures) and operationally through bounded live REST polling sessions against public Kalshi prediction markets.

### 1.2 Core Verification Highlights:
1. **Bounded, Rate-Limited, Read-Only Collection:**
   The new V11 collector ([`kalshi_arbitrage/v11_collector.py`](../kalshi_arbitrage/v11_collector.py)) enforces hard limits on polling intervals, request concurrency (minimum 100ms request spacing enforcing $\le 10$ req/s rate limits), market capacity, total cycle counts, and session duration. It operates strictly via read-only public endpoints without write credentials or order-placement capabilities.
2. **Diagnostic Failure Isolation:**
   Transient transport errors (rate limits, timeouts, server errors, empty books) are written to an isolated, append-only [`diagnostics.jsonl`](../evidence_v11_trial/diagnostics.jsonl) stream. Transport errors are preserved for health monitoring without contaminating the clean [`observations.jsonl`](../evidence_v11_trial/observations.jsonl) evidence records.
3. **Cryptographic Provenance & Manifest Integrity:**
   Every collection session writes a deterministic [`manifest.json`](../evidence_v11_trial/manifest.json) containing SHA256 hashes of observation and diagnostic files, runtime parameters, cycle counts, request counts, and environment metadata.
4. **Collision Protection & Non-Destructive Resumption:**
   The collector enforces strict output collision protection. It refuses to overwrite existing directories unless explicit resume mode (`--resume`) is set. Resuming an interrupted session re-loads existing record IDs and sequence counters, appending new snapshots without data duplication or loss.
5. **Prospective Forward Paper Evaluation & Replay Equivalence:**
   Incoming market observations can be evaluated prospectively in real time through V10's 10-stage opportunity funnel, V8's asynchronous multi-leg simulation, and V9's pre-trade risk gate. Live-collected evidence replayed offline through [`V10HistoricalStrategyEvaluator`](../kalshi_arbitrage/v10_evaluation.py) yields identical funnel decisions and trade outcomes.
6. **Strict Double-Entry Ledger Reconciliation:**
   All prospective paper trades maintain a 100% exact match on the fundamental cash conservation invariant:
   $$\text{Available Cash} + \text{Reserved Cash} + \text{Position Cost Basis} = \text{Initial Cash} + \text{Realized P\&L} - \text{Total Fees Paid}$$

### 1.3 Strategy Economic Edge Status: **INCONCLUSIVE (MARKET SPARSITY)**
While the data collection and evaluation pipeline is fully validated, **the strategy's economic edge remains unresolved due to persistent empirical data sparsity**.

In live operational trials conducted across open Kalshi binary contracts:
- 100% of newly collected order-book snapshots contained empty order books (`yes_bids=()`, `no_bids=()`).
- Zero gross parity arbitrage candidates were detected.
- As predicted by Milestone V10 research, prediction market contracts sampled at standard intervals frequently have zero resting counterparty bids.
- **The system truthfully reported zero trades and zero fabricated edge**, strictly preserving the foundational principle: *Never manufacture profitability or loosen risk limits to force trades.*

---

## 2. Operational Evidence Quality & Cadence Audit

Two controlled operational validation sessions were conducted using the production CLI tool [`scripts/collect_v11.py`](../scripts/collect_v11.py):

### 2.1 Operational Validation Session 1: Baseline Collection & Resume Audit (`evidence_v11_trial`)
- **Initial Run:** 2 cycles, 2 markets per cycle $\to$ 4 observations recorded.
- **Resume Run:** Resumed for 2 additional cycles $\to$ 4 additional observations appended without duplicate IDs.
- **Total Ingested Records:** 8 observations across 2 unique contract tickers.
- **Transport Failures:** 0 errors (100% request success rate).
- **Stale Records:** 0.
- **Empty Order Books:** 8 (100.0%).
- **Populated Order Books:** 0 (0.0%).
- **Cadence Metrics:** Mean sampling interval = 1.45s; max latency gap = 2.53s.
- **Observations SHA256:** `526b677a2c87a2027c7dc84c522e4953e09c447e685c2a2a2e6845eb82186d46`.

### 2.2 Operational Validation Session 2: Prospective Forward Evaluation (`evidence_v11_prospective_trial`)
- **Run Configuration:** 2 cycles, 5 markets per cycle, prospective evaluation enabled.
- **Total Valid Observations:** 10 observations across 5 unique market tickers and events.
- **Execution Mode:** Prospective Forward Paper Evaluation (`V11ProspectiveEvaluator`).
- **Funnel Attrition:**
  - Observations Ingested: 10
  - Valid Snapshots: 10
  - Markets Evaluated: 10
  - Gross Arbitrage Detected: 0
  - Approved by Risk: 0
  - Executed Trades: 0
- **Ending Financial State:** Initial Cash = \$10,000.00, Ending Cash = \$10,000.00, Net Realized P&L = +0¢.
- **Ledger Invariant Audit:** **PASS (100% Exact Invariant Match, 0 Discrepancies)**.
- **Offline V10 Replay Match:** Replaying `evidence_v11_prospective_trial/observations.jsonl` through `V10HistoricalStrategyEvaluator` yielded identical results: 10 observations, 0 gross arbs, 0 trades, 100% ledger match.

---

## 3. Evidence Quality Metrics & Minimum Research Thresholds

The V11 Evidence Quality Auditor ([`V11EvidenceQualityAuditor`](../kalshi_arbitrage/v11_collector.py)) enforces objective criteria before any dataset is deemed sufficient for statistical performance claims:

| Requirement Dimension | Minimum Standard for Research | Current Operational Trial | Status |
| :--- | :---: | :---: | :---: |
| **Unique Market Breadth** | $\ge 5$ liquid markets | 5 markets | **MET** |
| **Total Observation Volume** | $\ge 100$ snapshots | 10 snapshots | **GAP (Trial sample)** |
| **Temporal Duration** | $\ge 600.0\text{s}$ (10 minutes) | 8.9s | **GAP (Trial sample)** |
| **Populated Order Books** | $\ge 20$ populated books | 0 books | **GAP (Empty books)** |
| **Duty Cycle / Cadence Gaps** | $\le 5\%$ gaps $> 2.5\times$ poll | 0 gaps (0.0%) | **MET** |
| **Cryptographic Integrity** | Verified SHA256 manifest | Verified SHA256 | **MET** |

### Critical Scientific Caveat on Sampling:
1. **Consecutive Snapshots Are Not Independent:** Consecutive observations of the same order book frequently exhibit identical prices and quantities (`repeated_identical_books_count`). High snapshot counts cannot be substituted for true temporal coverage or independent opportunity arrivals.
2. **Breadth vs Depth Trade-Off:** Polling 50 markets at 1-minute intervals yields wide cross-sectional breadth but coarse temporal resolution, whereas polling 2 markets at 100ms intervals yields high-resolution microstructure evidence for those specific contracts.

---

## 4. Prospective Evaluation & Microstructure Execution Architecture

```
[REST Market Data Provider]
            │ (Bounded rate-limiting: min 100ms interval)
            ▼
[V11MarketEvidenceCollector]
    ├─> [diagnostics.jsonl]  (Transport errors, timeouts, rate limits)
    ├─> [observations.jsonl] (Immutable, append-only, fsync-flushed)
    └─> [V11ProspectiveEvaluator]
            │
            ├─ [1. Freshness Check] (age_s <= max_stale_seconds)
            ├─ [2. Binary Parity Detection] (Ask_YES + Ask_NO < 1.00)
            ├─ [3. Liquid Universe Filter] (volume, OI, spread)
            ├─ [4. Depth & CFTC Fee Pricing] (Quadratic taker fee schedule)
            ├─ [5. Pre-Trade Risk Gate] (Max cost, capital reservation)
            ├─ [6. V8 Async Simulator] (Sequential leg delay, unwind)
            ├─ [7. PaperPortfolio Ledger] (Double-entry cash conservation)
            └─ [8. Evaluated Opportunity Record] (opportunities.jsonl)
```

### Provenance Tracking:
- $T_{\text{disp}}$: Local monotonic timestamp when REST query was dispatched.
- $T_{\text{rtt}}$: Measured round-trip network transit latency (in milliseconds).
- $T_{\text{src}}$: Exchange HTTP `Date` response header timestamp.
- $T_{\text{rcv}}$: Local UTC timestamp when response was parsed and validated.
- All timestamps are explicitly preserved in the observation and diagnostic schemas, preventing matching-engine clock ambiguity.

---

## 5. Operational Commands & CLI Reference

All V11 operational workflows are accessible via the production CLI tool [`scripts/collect_v11.py`](../scripts/collect_v11.py):

### 1. Bounded Evidence Collection Session
```bash
# Collect up to 20 cycles across 10 markets, polling every 2.0s
python scripts/collect_v11.py --collect \
  --output-dir evidence_v11_run \
  --max-cycles 20 \
  --max-markets 10 \
  --poll-interval 2.0
```

### 2. Collection with Forward Paper Evaluation
```bash
# Collect evidence and prospectively evaluate opportunities into a paper portfolio
python scripts/collect_v11.py --collect \
  --output-dir evidence_v11_eval_run \
  --max-cycles 50 \
  --prospective-eval
```

### 3. Resuming an Interrupted Session
```bash
# Resume an existing session safely without duplicating or overwriting data
python scripts/collect_v11.py --collect \
  --output-dir evidence_v11_run \
  --max-cycles 50 \
  --resume
```

### 4. Auditing Evidence Quality and Cadence
```bash
# Audit sampling intervals, missing data, gaps, and depth
python scripts/collect_v11.py --audit \
  --input-dir evidence_v11_run \
  --output-json data/v11_quality_audit.json
```

### 5. Historical / Prospective V10 Strategy Evaluation
```bash
# Replay newly collected observations through V10 evaluation engine
python scripts/collect_v11.py --evaluate \
  --file evidence_v11_run/observations.jsonl \
  --output-json data/v11_replay_report.json \
  --output-report docs/v11_replay_summary.md
```

---

## 6. Verification & Automated Test Suite Results

### 6.1 Unit and Integration Test Results ([`tests/test_v11_collector.py`](../tests/test_v11_collector.py))
- **Total V11 Tests:** 21 passed in 0.44s.
- **Coverage Summary:**
  1. Bounded cycle and observation limits (`test_bounded_polling_limits`, `test_bounded_observations_limit`).
  2. Minimum request interval enforcement (`test_rate_limit_compliance`).
  3. Diagnostic failure event isolation (`test_diagnostic_failure_isolation`).
  4. Stale data detection and reason tagging (`test_stale_data_flagging`).
  5. Observation deduplication and financial idempotency (`test_prospective_evaluation_idempotency`).
  6. Append-only persistence with atomic fsync (`test_append_only_persistence`).
  7. Non-destructive resume behavior (`test_session_resume_without_duplicate_records`).
  8. Timestamp provenance preservation (`test_timestamp_provenance_preservation`).
  9. Repeated identical order book detection (`test_repeated_identical_book_tracking`).
  10. Cadence gaps and sampling jitter auditing (`test_cadence_gap_detection`).
  11. Minimum research requirements gating (`test_minimum_research_requirements`).
  12. V10 replay equivalence (`test_prospective_v10_replay_equivalence`).
  13. Pre-trade risk rejection and capital safety (`test_pre_trade_risk_rejection`).
  14. Output path collision protection (`test_output_path_collision_protection`).
  15. Graceful shutdown signaling (`test_graceful_stop_signal`).
  16. Cryptographic SHA256 manifest verification (`test_manifest_sha256_verification`).
  17. Prohibition of live trading capabilities (`test_prohibition_of_live_trading_capabilities`).
  18. Empty and sparse dataset auditing (`test_empty_and_sparse_dataset_auditing`).
  19. Partial fills, asymmetric depth, unwind, and ledger reconciliation (`test_asymmetric_depth_unwind_reconciliation`).
  20. CLI audit and evaluate execution integration (`test_cli_execution_audit_and_evaluate`).

### 6.2 Full Repository Regression Suite
```bash
./.venv/bin/python -m pytest -k "not test_http_api_endpoints_integration" -q
........................................................................ [ 14%]
........................................................................ [ 28%]
........................................................................ [ 43%]
........................................................................ [ 57%]
........................................................................ [ 71%]
........................................................................ [ 86%]
.....................................................................    [100%]
501 passed, 1 deselected in 9.19s
```
*(1 deselected integration test requires unisolated external network access)*.

---

## 7. Remaining Limitations & Research Prerequisites

While Milestone V11 delivers a robust, production-grade evidence collection and evaluation pipeline, the following research gaps remain:

1. **Exchange Liquidity Characteristics:**
   Empirical Kalshi order books in off-peak hours and across niche series remain completely unpopulated. Polling empty books produces zero candidates, regardless of collector frequency.
2. **WebSocket Microstructure Stream:**
   REST snapshot polling cannot capture order-book updates that persist for only 50ms to 200ms. A sustained WebSocket L2 stream is necessary to capture fleeting quote discrepancies before automated market makers eliminate them.
3. **Queue Priority & Cancellation Realism:**
   REST order-book snapshots cannot establish queue position. The simulation models conservative price depth traversal, but actual fills in competitive books depend on matching engine FIFO queue priority.

---

## 8. Final Verdict & Release Decision

- **Pipeline Technical Release Verdict:** **EVIDENCE PIPELINE VALIDATED**
- **Live Trading Deployment Recommendation:** **STRICTLY REJECTED**
  The engine must remain exclusively in observation and paper-evaluation mode. Live order submission is not justified and remains strictly prohibited.
