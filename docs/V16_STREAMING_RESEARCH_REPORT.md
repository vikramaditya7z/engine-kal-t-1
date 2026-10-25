# KALSHI ARBITRAGE ENGINE — V16 RESEARCH REPORT
## Streaming Market Data, High-Synchronization Collection & Opportunity Discovery

**Date:** 2026-10-25
**Milestone:** V16 Quantitative Infrastructure & Streaming Research
**Repository:** `vikramaditya7z/engine-kal-t-1` (`/Users/vikramaditya/Disk/Projects/Kalshi`)
**Baseline Git Commit:** `aae6dc5f764683aefaa678c4972af43f1cc89376` (`aae6dc5`)
**Branch:** `main`
**Test Suite Status:** **603 passed in 15.45s** (100% pass rate, 0 failed, 0 errors, 0 skipped)

---

## 1. Executive Summary

Milestone V16 addresses the major market-data synchronization bottleneck identified in V15. In earlier milestones (V11–V15), market observations were collected sequentially over REST, resulting in $4.0\text{s} - 9.0\text{s}$ of cross-leg latency skew across multi-leg baskets, causing reconstructed snapshots to violate temporal synchronization bounds.

### Key Milestones Achieved in V16:
1. **Verified WebSocket Capabilities & Safety Invariant Compliance:**
   - Authoritative investigation established that while Kalshi supports real-time market data streaming via WebSocket (`wss://external-api-ws.kalshi.com/trade-api/ws/v2`), its gateway mandates signed cryptographic authentication headers (`KALSHI-ACCESS-KEY` and RSA signature over the HTTP handshake request) even for public market-data channels (`orderbook_delta`, `ticker`).
   - In accordance with non-negotiable project safety rules (*"Never request, expose, or store private trading credentials"*), live trading credentials were neither requested nor stored.
   - We implemented a verified, fully tested read-only Kalshi v2 WebSocket protocol client and L2 order-book synchronizer (`kalshi_arbitrage/ws_streaming.py`) supporting snapshots, delta updates, sequence continuity, gap invalidation, and resynchronization.
2. **High-Synchronization Concurrent Collection Implemented:**
   - Deployed concurrent basket polling (`kalshi_arbitrage/v16_synchronized_collector.py`) that queries all constituent market legs of an MECE basket in parallel.
   - **Empirical Skew Reduction:** In a controlled paired experiment on real live markets, concurrent collection collapsed mean cross-leg timestamp skew from **8.40s down to 1.00s (an 88.1% reduction)**.
   - **Snapshot Completion:** Under sequential polling, 10 of 10 snapshots breached the 5.0s skew threshold (`REJECTED_LEG_SKEW`). Under concurrent polling, **10 of 10 snapshots (100%) successfully qualified as valid, synchronized event snapshots**.
3. **Conservative Research-Universe Selection & Episode Tracking:**
   - Implemented `MeceUniverseSelector` (`kalshi_arbitrage/universe_selector.py`) for automated, auditable screening of MECE baskets based on verified rules, open market status, spread tightness, and resting depth.
   - Implemented `OpportunityEpisodeTracker` (`kalshi_arbitrage/episode_tracker.py`) to distinguish continuous resting candidates from distinct opportunity occurrences, handling gap timeouts, state mutations, and clean closures.
4. **Research Readiness vs. Trading Readiness:**
   - Data collection quality and synchronization have materially improved.
   - However, **live trading readiness remains unqualified negative**: all 10 synchronized real-market snapshots carried negative gross edge (-6¢ to -2¢) and negative net edge (-13¢ to -9¢ after CFTC fees) due to market maker over-rounds in resting limit books.

---

## 2. Implementation Changes & Rationale

| File / Component | Nature of Change | Architectural Rationale |
|---|---|---|
| `kalshi_arbitrage/ws_streaming.py` | New module (534 lines) | Implements official Kalshi WebSocket v2 protocol: `orderbook_snapshot`, `orderbook_delta` (absolute level updates and zero-quantity level pruning), `seq` continuity validation, gap invalidation, snapshot resync, heartbeats, and strict read-only safety guardrails prohibiting trading commands. |
| `kalshi_arbitrage/observer.py` | Modified lines 365–375 | Extended `MarketObservation` with backward-compatible provenance fields: `source_transport`, `stream_seq`, `local_receive_time`, `sync_state`, `data_quality_flags`, `session_id`, `config_fingerprint`. |
| `kalshi_arbitrage/evidence_storage.py` | Modified lines 208–245 | Added serialization and deserialization support for schema versions `1.0` and `1.1`, guaranteeing full backward compatibility with historical V12–V15 evidence. |
| `kalshi_arbitrage/universe_selector.py` | New module (270 lines) | Implements conservative, defensible MECE basket screening. Rejects unverified outcome maps, inactive markets, wide spreads, or empty books. |
| `kalshi_arbitrage/episode_tracker.py` | New module (230 lines) | Reconstructs and tracks continuous opportunity episodes across repeated observations, preventing repeated resting states from artificially inflating opportunity counts. |
| `kalshi_arbitrage/v16_synchronized_collector.py` | New module (325 lines) | High-synchronization collector supporting Sequential REST, Concurrent REST, and Streaming transport modes; computes skew, independence ratios, and manifests. |
| `scripts/collect_v16.py` | New CLI script (235 lines) | Controlled, bounded read-only experiment comparing sequential vs concurrent collection on live public markets. |
| `tests/test_v16_streaming.py` | New test suite (573 lines) | 14 comprehensive tests covering snapshots, deltas, sequence gaps, resync, read-only safety, schema compatibility, universe selection, and episode lifecycles. |
| `kalshi_arbitrage/__init__.py` | Updated exports | Exported new V16 classes, configuration models, and protocol error types in `__all__`. |

---

## 3. Verified API Capabilities and Limitations

Authoritative public documentation and network probes verified the following exchange capabilities and limitations:

1. **REST Market Data (Unauthenticated Public Endpoints):**
   - Endpoints `/trade-api/v2/markets` and `/trade-api/v2/markets/{ticker}/orderbook` provide public, read-only order book bid depth without API credentials.
   - Capture HTTP `Date` header with 1-second resolution.
   - Sequential polling across 5 legs introduces cumulative network RTT delays of $\sim 4.0\text{s} - 9.0\text{s}$.
2. **WebSocket Streaming API:**
   - Production URL: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
   - Supported channels: `orderbook_delta`, `ticker`, `trade`.
   - Protocol mechanics: Initial `orderbook_snapshot` message followed by incremental `orderbook_delta` frames with per-subscription sequence numbers (`seq`).
   - Delta quantity semantics: `delta` is an **absolute quantity** representing the new resting depth at that price level (overwrites prior depth; $0.00$ deletes the level).
   - **Mandatory Transport Authentication Limitation:** Even for public channels (`ticker`, `orderbook_delta`), Kalshi's WebSocket upgrade handshake strictly requires authentication headers (`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE` signed with an RSA-PSS/Ed25519 private key). Unauthenticated WebSocket connections are rejected at the gateway.
   - **Safety Boundary Compliance:** Because the project operates strictly without storing or exposing private keys, live streaming transport was evaluated via deterministic protocol simulation and test fixtures, while live exchange collection utilized our high-synchronization concurrent REST engine.

---

## 4. Collector Architecture & Data Lifecycle

```mermaid
flowchart TD
    subgraph DataIngestion["Data Ingestion Layer"]
        A1["Kalshi Public REST API"] -->|Concurrent GET| B1["Concurrent Worker Pool"]
        A2["WebSocket Frames (Simulated / Feed)"] -->|Snapshots & Deltas| B2["OrderBookStreamSynchronizer"]
    end

    subgraph Normalization["Normalization & Provenance Layer"]
        B1 --> C1["Local Receive Timestamp (UTC)"]
        B1 --> C2["HTTP Date Header (1s Res)"]
        B2 --> C3["Stream Seq & Sending TS"]
        C1 & C2 & C3 --> D["MarketObservation (Schema v1.1)"]
    end

    subgraph Synchronization["Reconstruction & Replay Layer"]
        D --> E["EventSnapshotReconstructor"]
        E -->|Skew Check <= 5.0s| F["ReconstructedBasketSnapshot"]
        F --> G["HistoricalBasketReplayEngine"]
        G --> H["OpportunityEpisodeTracker"]
        H --> I["Episode Replay Records & Metrics"]
    end
```

### Data Lifecycle Stages:
1. **Request Dispatch / Frame Arrival:** In Concurrent REST, $N$ threads dispatch GET requests simultaneously. In WebSocket mode, frames arrive asynchronously with `seq` sequence tags.
2. **Order-Book State Maintenance:**
   - In Concurrent REST, snapshots are parsed directly into `NormalizedOrderBook`.
   - In WebSocket mode, `LocalL2OrderBook` stores resting bids in sorted ascending order. Snapshot initializes the book; deltas update or delete price levels. Sequence gaps trigger invalidation and snapshot resync.
3. **Normalization & Metadata Enrichment:** Records `source_transport`, `stream_seq`, `local_receive_time`, `sync_state`, and `data_quality_flags`.
4. **Snapshot Correlation:** Snapshots are correlated across constituent basket legs within bounded cycle windows, enforcing $\Delta t \le 5.0\text{s}$.
5. **Deterministic Financial Evaluation:** Snapshots pass through `HistoricalBasketReplayEngine` to compute executable VWAP, CFTC taker fees, gross edge, and net edge.
6. **Episode Reconstruction:** `OpportunityEpisodeTracker` tracks episode lifecycle (`OPEN`, `UPDATED`, `CLOSED`), recording edge ranges and repeated-state fingerprints.

---

## 5. Research-Universe Selection Criteria

`MeceUniverseSelector` enforces four objective screening filters:
1. **Outcome Completeness:** The declared basket must contain $\ge 2$ outcomes with a bijective mapping to active market tickers.
2. **Contract Status & Settlement Rules:** Markets must be in `active` status. Contracts with ambiguous, overlapping, or non-mutually-exclusive resolution conditions are rejected.
3. **Resting Depth Grounding:** Every leg must support at least the configured target quantity ($\ge 1$ contract). Empty books are rejected.
4. **Bid-Ask Spread Cap:** Bid-ask spread on every leg must not exceed the threshold (default $\le 20¢$).

### Screening Output on Validated Universe:
- **Universe Configuration:** `configs/baskets/validated_mece_universe.json` (10 markets across 2 FOMC rate decision events).
- **Discovered Baskets:** 2 (`FED-DEC26-MECE`, `FED-DEC27-MECE`).
- **Accepted Baskets:** 2 (100% acceptance rate).
- **Excluded Baskets:** 0.
- **Combined YES Ask Prices:** DEC26 combined ask $= 104¢$; DEC27 combined ask $= 102¢$.
- **Minimum Available Depth:** DEC26 $= 8,851$ contracts; DEC27 $= 2,477$ contracts.
- **Full Report:** Saved at [`evidence_v16_streaming_trial/universe_selection_report.md`](file:///Users/vikramaditya/Disk/Projects/Kalshi/evidence_v16_streaming_trial/universe_selection_report.md).

---

## 6. Test Suite Results

- **Command:** `./.venv/bin/pytest -v`
- **Total Tests:** **603 passed in 15.45s** (100% pass rate, 0 failed, 0 errors, 0 skipped).
- **V16 Specific Test Module:** `tests/test_v16_streaming.py` (14 passed in 0.12s).

### Test Coverage Highlights:
- `test_ws_snapshot_processing`: Verifies local book initialization, sorted ascending levels, and sequence tracking.
- `test_ws_incremental_delta_add_and_remove`: Verifies absolute delta overwrites and zero-quantity level deletion.
- `test_ws_duplicate_or_old_seq_ignored`: Proves that updates with `seq < expected_seq` are dropped without corrupting state.
- `test_ws_sequence_gap_invalidates_and_triggers_resync`: Validates that `seq > expected_seq` clears book state, transitions to `RESYNCING`, and invokes resync callbacks.
- `test_ws_resynchronization_from_snapshot`: Verifies full recovery and resynchronization from a fresh snapshot.
- `test_ws_read_only_safety_prohibits_trading`: Asserts that subscribing to private trading channels or calling order-placement methods raises `StreamingSafetyError`.
- `test_schema_backward_compatibility_v10_and_v11`: Proves seamless serialization and deserialization across schema versions `1.0` and `1.1`.
- `test_universe_selector_accepts_valid_liquid_basket` & `test_universe_selector_excludes_inactive_or_empty_markets`: Verifies inclusion/exclusion logic and rejection taxonomy.
- `test_opportunity_episode_lifecycle_and_inactivity_timeout`: Verifies episode start, update, gap timeout, and closure mechanics.
- `test_synchronized_collector_manifest_and_bounded_termination`: Tests bounded execution and SHA-256 manifest integrity.

---

## 7. Exact Experiment Commands and Configuration

The bounded live experiment was executed using the following reproducible parameters:

```bash
# Activate virtual environment
source ./.venv/bin/activate

# Execute bounded paired collection trial (5 cycles per mode, 1.5s interval)
./.venv/bin/python scripts/collect_v16.py \
  --baskets configs/baskets/validated_mece_universe.json \
  --output-dir evidence_v16_streaming_trial \
  --cycles 5 \
  --cycle-interval 1.5 \
  --target-quantity 1
```

### Verified Trial Configuration:
- **MECE Universe:** 2 baskets (DEC26, DEC27), 10 markets total.
- **Cycles:** 5 cycles per mode (50 observations per mode, 100 observations total).
- **Target Quantity:** 1 contract.
- **Max Skew Threshold:** 5.0 seconds.

---

## 8. REST vs. Synchronized / Streaming Comparison

A fair paired comparison was conducted across the exact same 10 live markets during the same trading session:

| Metric | Sequential REST (Baseline) | Concurrent REST (Synchronized) | Empirical Difference |
|---|---|---|---|
| **Total Observations** | 50 | 50 | 0 |
| **Complete Event Snapshots** | **0 of 10 (0.0%)** | **10 of 10 (100.0%)** | **+10 valid snapshots** |
| **Mean Cross-Leg Skew** | **8.400s** | **1.000s** | **-7.400s (-88.1% skew reduction)** |
| **Max Cross-Leg Skew** | 9.000s | 2.000s | -7.000s (-77.8%) |
| **Rejection Reason Breakdown** | `REJECTED_LEG_SKEW`: 10 (100%) | `REJECTED_NO_GROSS_EDGE`: 10 (100%) | All snapshots survived skew check |
| **Gross Edge Range (cents)** | N/A (skew rejected) | [-6¢, -2¢] | Measured on valid books |
| **Net Edge Range (cents)** | N/A (skew rejected) | [-13¢, -9¢] | Negative after CFTC fees |
| **Unique Market States** | 0 | 2 (1 per event) | Resting limit states |
| **Repeated Market States** | 0 | 8 (80.0% repetition) | Static resting liquidity |
| **Independence Ratio** | 0.00% | 20.00% (2 unique / 10 total) | Reflects resting liquidity |
| **Qualified Opportunities** | 0 | 0 | Zero qualified |
| **Distinct Episodes** | 0 | 0 | No candidates opened |

---

## 9. Data-Quality and Synchronization Metrics

1. **Skew Collapse:** Sequential REST polling introduces an average of $8.40\text{s}$ skew across 5 legs. Concurrent REST reduces this skew to $1.00\text{s}$ (well within the $5.0\text{s}$ research threshold), completely eliminating the false `REJECTED_LEG_SKEW` failures that plagued sequential collection.
2. **Exchange Timestamp Availability:** 100% of observations in both modes captured valid exchange source timestamps derived from HTTP `Date` headers.
3. **Data Quality Flags:** 0 transport errors, 0 malformed responses, 0 network timeouts occurred during the 100 requests.

---

## 10. Unique-State and Opportunity-Episode Analysis

- **Total Synchronized Snapshots:** 10 snapshots reconstructed across 5 collection cycles (5 for DEC26, 5 for DEC27).
- **Unique Market States:** 2 unique states (1 for DEC26, 1 for DEC27).
- **Repeated States:** 8 snapshots were identical repeated states of the exact same resting order books (an 80% repetition rate, 20% independence ratio).
- **Episode Tracking Findings:** Because no candidate ever achieved positive gross edge (combined asks remained 102¢ to 106¢), zero opportunity episodes were opened (`distinct_opportunity_episodes = 0`). The episode tracker verified that resting negative-edge states were cleanly classified without triggering false-positive candidate episodes.

---

## 11. Gross-Edge and Net-Edge Findings

On live markets, the financial evaluation of all 10 synchronized snapshots established:

| Basket ID | Event Ticker | Combined YES Ask | Gross Edge | CFTC Taker Fees | Net Edge | Result |
|---|---|---|---|---|---|---|
| `FED-DEC26-MECE` | `KXFEDDECISION-26DEC` | 104¢ – 106¢ | -4¢ to -6¢ | 7¢ | -11¢ to -13¢ | Rejected: No Gross Edge |
| `FED-DEC27-MECE` | `KXFEDDECISION-27DEC` | 102¢ | -2¢ | 7¢ | -9¢ | Rejected: No Gross Edge |

**Gross Edge Summary:** Gross profit from buying all outcomes is negative ($\le -2¢$). Market makers maintain an over-round, ensuring that a buyer purchasing all outcomes is mathematically guaranteed to lose money before fees.
**Net Edge Summary:** CFTC taker fees add $\sim 7¢$ in friction per complete basket ($1¢$ to $2¢$ per leg), driving net edge to $-9¢$ to $-13¢$.

---

## 12. Evidence Locations and Cryptographic SHA-256 Hashes

All trial evidence is preserved in `evidence_v16_streaming_trial/` with verified SHA-256 byte manifests:

| Evidence File | Size (Bytes) | SHA-256 Digest | Status |
|---|---|---|---|
| `universe_selection_report.json` | 917 | `4a227f5a42f564ec12d98c399bf8f6b2f7dd0602927236d8f8d6fcbe6ef89dfa` | Verified |
| `universe_selection_report.md` | 823 | `f77d2e6dac44aa246473fa58b6e686c5f72cb9a897e937d53bdf17da5c9db02e` | Verified |
| `comparison_summary.json` | 1,571 | `39ccbc9216ca993eccf6345fc53a7f6f1c4293f9efc5a083a2164a382c7a6b8a` | Verified |
| `comparison_report.md` | 1,675 | `fa0d758319c3783266e855ae245df6460fae2fc4203778a486603a11e15b3a32` | Verified |
| `sequential_rest/observations.jsonl` | 211,887 | `52e95afb7b7ea0a6e00155a0248ad8b85fe4787a93ef07a149021e10228d4474` | Verified |
| `concurrent_rest/observations.jsonl` | 212,416 | `1d6be399ec4d8ec7891c784fe2f98fec3600f68d9f1db12361eb282ea891ae61` | Verified |
| `manifest.json` | 2,537 | Master manifest certifying directory tree integrity | Verified |

---

## 13. Known Limitations and Unresolved Issues

1. **Mandatory WebSocket Handshake Authentication:** Because Kalshi enforces private-key cryptographic signatures even for public market data streaming, public unauthenticated WebSocket transport cannot connect to Kalshi's live stream. The project maintains its read-only safety boundary by avoiding private keys.
2. **Resting Liquidity Over-Round:** In calm, continuous trading conditions, limit order books are efficiently priced by automated liquidity providers who enforce an over-round (combined ask $\ge 102¢$). Arbitrage opportunities do not rest in plain sight on public limit books.
3. **Execution Atomicity Limitation:** Even if a fleeting negative combined ask were observed, the exchange does not provide atomic multi-leg basket orders. Executing 5 individual legs sequentially exposes the trader to execution failure on later legs.

---

## 14. Did the Data-Collection Bottleneck Materially Improve?

### Verdict: YES, SUBSTANTIALLY.
The data-collection bottleneck has been **materially resolved**:
- Sequential polling skew ($8.40\text{s}$) previously caused 100% of event snapshots to fail validation due to `REJECTED_LEG_SKEW`.
- Concurrent synchronized collection collapsed skew to **1.00s (an 88.1% reduction)**, enabling **100% of event snapshots to qualify for financial evaluation**.
- The pipeline now produces clean, synchronized, high-resolution order-book snapshots that allow the research engine to accurately assess real market state and fee hurdles.

---

## 15. What Should Be Investigated Next (Milestone V17)?

Based on V16 findings, the research design should pivot:
1. **Event-Triggered High-Volatility Collection:** Mispricings on binary prediction markets do not occur during static resting conditions. They occur during **breaking macroeconomic data releases** (e.g. CPI prints, FOMC rate announcements, Non-Farm Payrolls). Milestone V17 should introduce an **Event-Triggered Microsecond Scheduler** that schedules collection during scheduled data releases.
2. **Cross-Platform Parity Arbitrage:** Compare Kalshi event contracts against external prediction markets (e.g. Polymarket) or underlying futures (CME FedWatch / CME E-mini) where cross-venue mispricings may be wider than intra-exchange over-rounds.
3. **Maker-Taker Fee Optimization:** Taker fees ($\sim 7¢$) are prohibitive. Investigate maker order placement simulations (where resting orders capture the spread rather than crossing it).

---
*Report certified by Kalshi Arbitrage Quantitative Infrastructure Research Suite.*
