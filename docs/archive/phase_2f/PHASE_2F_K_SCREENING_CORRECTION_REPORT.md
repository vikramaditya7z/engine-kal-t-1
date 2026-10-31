# Phase 2F-K Implementation Report: Screening Pipeline Correction

**Date:** 2026-10-30  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Working Branch:** `fix/v19-clean-checkout-test`  
**Milestone:** Phase 2F-K (Screening Pipeline Correction)  
**Safety Mandate:** Offline verification only. Zero live Kalshi API calls, zero orders placed, scanner remained STOPPED, preserved datasets untouched.

---

## 1. Executive Summary

Milestone Phase 2F-K resolves the root cause identified during the Phase 2F-J Post-Trial Audit: **the premature elimination of preliminary arbitrage candidates in Stage B preliminary screening due to conflation of exchange contract modification timestamps (`updated_time`) with quote observation freshness.**

In the first production observation trial, 168,097 unique markets across 16,455 events and 1,086,056 strategies were evaluated, but zero preliminary candidates were generated. The audit demonstrated that because prediction markets feature resting limit orders that remain active for hours or days without trading, and because a full catalog discovery pass takes 10 to 23 minutes, enforcing `quote_age <= 60.0s` and `cross_leg_skew <= 5.0s` during preliminary screening dropped over 99.9% of binary strategies and 100% of multi-leg MECE basket strategies before mathematical edge could ever be evaluated.

Milestone Phase 2F-K implements the approved design corrections:
1. **Catalog `updated_time`-based staleness rejection removed from Stage B screening:** Mathematical edge ($P_{\text{YES}} + P_{\text{NO}} < \$1.00$ or $\sum P_{\text{YES}} < \$1.00$) is evaluated directly on available top-of-book quotes.
2. **Catalog cross-leg timestamp skew rejection removed from Stage B screening:** Cross-market timestamp discrepancies in catalog metadata no longer abort candidate discovery.
3. **Observation timestamps recorded separately from exchange quote timestamps:** `PreliminaryCandidate` models now capture `observation_timestamp` (when the market quote was ingested by the discovery engine) and `quote_timestamp` (the raw exchange `updated_time` timestamp, if available).
4. **Market eligibility and structural validation strictly preserved:** Non-binary market types, closed/settled statuses, non-mutually exclusive events, and incomplete outcome groups remain strictly rejected with typed screening outcomes (`SKIPPED_UNSUPPORTED_MARKET`, `SKIPPED_INCOMPLETE_EVENT_GROUP`, etc.).
5. **Stage C safeguards completely preserved:** All execution-aware safeguards—including actual order-book quote freshness ($\le 60.0$s), client-side cross-leg fetch window ($\le 5.0$s), server timestamp skew ($\le 2.0$s / $5.0$s), basket leg limit ($\le 8$), executable depth, VWAP calculations, official taker fees, and minimum net profit ($\ge 5$ cents)—remain fully intact.

---

## 2. Pipeline Modifications & Architecture

### 2.1 Decoupled Two-Stage Filtering Pipeline

```
+----------------------------------------------------------------------------------------------------+
|                                    CORRECTED ARBITRAGE PIPELINE                                    |
+----------------------------------------------------------------------------------------------------+
|  [Stage A: Catalog Discovery]                                                                      |
|    - Cursor pagination over /events (with nested markets)                                          |
|    - Normalization, deduplication, and active state reconciliation                                 |
|        |                                                                                           |
|        v                                                                                           |
|  [Stage B: Broad-Catalog Screener (CORRECTED)]                                                     |
|    - Verify market eligibility: market_type == "binary", status == "open"                          |
|    - Verify event structure: mutually_exclusive == True, collective exhaustiveness confirmed       |
|    - Evaluate mathematical pricing: payout - cost > 0.00                                           |
|    - Record observation_timestamp (discovery time) and quote_timestamp (exchange time)            |
|    - Zero staleness or skew rejections based on catalog metadata!                                  |
|        |                                                                                           |
|        v (Preliminary candidates enqueued to Priority Queue)                                       |
|  [Stage C: Targeted Order-Book Qualifier (PRESERVED)]                                              |
|    - Step 0: G1 Leg Count Gate (leg_count <= 8)                                                    |
|    - Step 1: Pre-flight Queue Staleness Gate (now_utc - observation_timestamp <= 60.0s)            |
|    - Step 1.5: G2 Event Top-of-Book Pre-Filter (recheck gross edge on refreshed event quotes)      |
|    - Step 2: G5 Bounded Concurrent L2 Fetch (ThreadPool concurrency=4, rate limit=5.0 req/s)       |
|    - Step 3: G3 Dual-Skew Enforcement (fetch_window <= 5.0s, server_skew <= 2.0s / 5.0s)           |
|    - Step 4: Full Execution Qualification (Depth, VWAP, Quadratic Taker Fees, Net Edge >= 5c)      |
+----------------------------------------------------------------------------------------------------+
```

### 2.2 Model & Dataclass Changes

#### `PreliminaryCandidate` ([`kalshi_arbitrage/breadth_scanner.py`](file:///Users/vikramaditya/Disk/Projects/Kalshi/kalshi_arbitrage/breadth_scanner.py#L934-L985))
- Added `observation_timestamp: Optional[str] = None`: captures the ISO 8601 UTC timestamp of the catalog discovery pass when this market pricing was observed.
- Preserved `quote_timestamp: Optional[str] = None`: records the exchange `updated_time` timestamp (if provided by Kalshi API).
- Updated `to_dict()`: cleanly serializes `observation_timestamp` for persistent candidate logging.

### 2.3 Screener Changes

#### 1. Binary Parity Screener (`screen_market_binary_parity`)
- Removed `if quote_age > self.config.max_stale_seconds` and `if market.updated_time is None` staleness gate.
- Added strict market status check: `market.status in ("open", "active")`, rejecting closed/settled markets with `SKIPPED_UNSUPPORTED_MARKET`.
- Computes mathematical gross edge $1.00 - (P_{\text{YES}} + P_{\text{NO}})$. If gross edge $> 0$, returns `PRELIMINARY_CANDIDATE` with `observation_timestamp=now_iso` and `quote_timestamp=market.updated_time.isoformat()`.

#### 2. Crossed Book Screener (`screen_market_crossed`)
- Removed `if quote_age > self.config.max_stale_seconds` staleness gate.
- Added market status check (`status in ("open", "active")`).
- Evaluates inverted spread or crossed complement bids ($P_{\text{YES\_bid}} + P_{\text{NO\_bid}} > 1.00$). If detected, returns `PRELIMINARY_CANDIDATE` with `observation_timestamp=now_iso` and `quote_timestamp=market.updated_time.isoformat()`.

#### 3. MECE Basket Screener (`screen_event_mece_baskets`)
- Removed constituent leg staleness checks (`missing_ts_legs` and `stale_legs`).
- Removed catalog timestamp cross-leg skew gate (`(max_ts - min_ts) > max_leg_skew_seconds`).
- Added constituent market status check: all constituent legs must have `status in ("open", "active")`, otherwise returns `SKIPPED_UNSUPPORTED_MARKET`.
- Preserved collective exhaustiveness verification (range partitions, declared baskets, or residual outcomes like `-OTHER`, `-NONE`, `-REST`, `-FIELD`).
- Preserved mutual exclusivity and complete event group validation.
- Extracts `oldest_quote_iso` across legs for conservative tracking without aborting screening.
- Generates `PRELIMINARY_CANDIDATE` for Long YES and Long NO baskets whenever mathematical gross edge is positive.

### 2.4 Qualifier Staleness Review & Pre-Flight Gating

#### `TargetedOrderBookQualifier.qualify_candidate()` ([`kalshi_arbitrage/breadth_scanner.py`](file:///Users/vikramaditya/Disk/Projects/Kalshi/kalshi_arbitrage/breadth_scanner.py#L2480-L2535))
- Pre-flight candidate queue staleness check now prioritizes `candidate.observation_timestamp`:
  ```python
  ref_ts_str = getattr(candidate, "observation_timestamp", None) or candidate.quote_timestamp
  ```
- If a candidate sits in the priority qualification queue longer than `max_stale_seconds` (60.0s), it is rejected with `REJECTED_STALE` without making network requests.
- If a candidate is fresh from the discovery engine, it passes to the G2 event pre-filter and Stage C L2 qualification.

#### Event Pre-Filter Review
- The event pre-filter evaluates refreshed top-of-book quotes from `GET /events/{ticker}` to ensure the gross edge has not dissolved.
- **Safety Rule:** HTTP response `Date` headers are **not** misrepresented as proof of quote freshness.
- Actual constituent quote freshness is verified against exchange timestamps and order-book snapshots during Stage C execution qualification.

---

## 3. Preservation of Stage C Financial & Operational Safeguards

All Stage C safeguards were verified to be strictly intact:

| Safeguard | Requirement | Implementation / Location | Status |
| :--- | :---: | :--- | :---: |
| **Max Actual Quote Age** | $\le 60.0\text{ s}$ | `TargetedOrderBookQualifier` + `ExecutionAwareArbitrageQualifier` | **ENFORCED** |
| **Client Fetch Window** | $\le 5.0\text{ s}$ | `fetch_window_seconds > max_fetch_window_seconds` $\to$ `REJECTED_LEG_SKEW` | **ENFORCED** |
| **Server Timestamp Skew** | $\le 2.0\text{ s} / 5.0\text{ s}$ | `server_timestamp_skew_seconds > max_leg_skew_seconds` $\to$ `REJECTED_LEG_SKEW` | **ENFORCED** |
| **Maximum Basket Legs** | $\le 8\text{ legs}$ | `cand_leg_count > max_candidate_legs` $\to$ `REJECTED_BASKET_TOO_LARGE` | **ENFORCED** |
| **Executable Depth** | $\ge 1\text{ contract}$ | Level-by-level bid consumption across all constituent order books | **ENFORCED** |
| **VWAP Pricing** | Exact Decimal | Volume-weighted average execution price across consumed levels | **ENFORCED** |
| **Exchange Taker Fees** | CFTC Official | Official Kalshi quadratic taker fee schedule evaluated per leg | **ENFORCED** |
| **Minimum Net Profit** | $\ge 5\text{ cents}$ | `min_net_profit_cents` enforced after subtracting all taker fees | **ENFORCED** |
| **Global Request Limiter** | $\le 5.0\text{ req/s}$ | Shared `TokenBucketRateLimiter` across catalog, event pre-filter, and L2 fetches | **ENFORCED** |
| **Bounded Concurrency** | $\le 4\text{ threads}$ | `ThreadPoolExecutor(max_workers=min(4, len(tickers)))` | **ENFORCED** |
| **Retry & Shutdown Safety** | Exponential backoff | Cooperative shutdown via lookahead gate and signal traps | **ENFORCED** |

---

## 4. Test Verification & Evidence

### 4.1 Dedicated Phase 2F-K Test Suite (`tests/test_breadth_scanner_phase_2f_k.py`)

Six dedicated unit tests were implemented to verify every requirement of Phase 2F-K:

```bash
$ ./.venv/bin/pytest tests/test_breadth_scanner_phase_2f_k.py -v
============================= test session starts ==============================
tests/test_breadth_scanner_phase_2f_k.py::test_k1_catalog_updated_time_staleness_not_rejected_in_stage_b PASSED [ 16%]
tests/test_breadth_scanner_phase_2f_k.py::test_k2_mece_basket_catalog_updated_time_staleness_not_rejected_in_stage_b PASSED [ 33%]
tests/test_breadth_scanner_phase_2f_k.py::test_k3_mece_basket_cross_leg_skew_not_rejected_in_stage_b PASSED [ 50%]
tests/test_breadth_scanner_phase_2f_k.py::test_k4_market_eligibility_and_structure_validation_preserved PASSED [ 66%]
tests/test_breadth_scanner_phase_2f_k.py::test_k5_observation_timestamp_recorded_separately_from_quote_timestamp PASSED [ 83%]
tests/test_breadth_scanner_phase_2f_k.py::test_k6_stage_c_safeguards_preserved_against_stale_actual_quotes PASSED [100%]
============================== 6 passed in 0.11s ===============================
```

### 4.2 Full Breadth Scanner Regression Run

```bash
$ ./.venv/bin/pytest tests/test_breadth_scanner*.py
============================= 175 passed in 11.22s =============================
```

All 175 breadth scanner tests pass with zero errors and zero warnings.

---

## 5. Phase 2F-K Verdict & Conclusion

### **Phase 2F-K Complete: `READY FOR PHASE 2F-L VALIDATION`**

The screening pipeline corrections have been verified offline. Mathematically valid candidates with resting catalog quotes can now flow into Stage C Targeted Order-Book Qualification without premature rejection, while all Stage C execution safeguards remain strictly enforced.
