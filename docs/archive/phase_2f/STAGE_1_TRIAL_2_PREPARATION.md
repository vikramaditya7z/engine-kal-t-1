# Stage 1 — Short Controlled Observation Trial (15-Minute): Launch Specification

**Date:** October 30, 2026  
**System:** Kalshi Breadth-First Arbitrage Scanner  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Git Branch:** `fix/v19-clean-checkout-test`  
**Target Trial Type:** Short Controlled Observation (15 Minutes)  
**Output Directory:** `data/breadth_scanner/trial_short_15min_20261030`  
**Status:** **CLI VERIFIED — READY FOR MANUAL EXECUTION**  
**Safety Gate:** The scanner is **STOPPED**. Zero live API calls made by the agent. Awaiting manual user execution.

---

## 1. Objectives of the Short Controlled Trial

1. **Verify Screening Pipeline Correction (Phase 2F-K):**
   - Confirm that Stage B (`BreadthArbitrageScreener`) no longer rejects preliminary candidates based on catalog `updated_time` staleness or cross-leg metadata skew.
   - Confirm that preliminary candidates are stamped with `observation_timestamp` and successfully enqueued for Stage C.
2. **Verify Execution Qualification (Phase 2F-L):**
   - Confirm that Stage C (`TargetedOrderBookQualifier`) evaluates actual L2 order-book quote freshness ($\le 60$s), client fetch window ($\le 5$s), server timestamp skew ($\le 2$s), target depth ($\ge 1$), CFTC taker fees, and minimum net profitability ($\ge \$0.05$).
3. **Verify HTTP Connection Pooling (Phase 2F-M):**
   - Confirm persistent connection reuse across the ~162 pagination calls per cycle, observing cycle duration reduction from ~600s down to ~40–60s.
4. **Verify Shared Rate Limiting:**
   - Confirm the global 5.0 requests/second rate limiter smoothly gates all outbound traffic without HTTP 429 rate limit events.
5. **Verify Monotonic Duration Boundary & Lookahead Shutdown:**
   - Confirm clean shutdown when the 15-minute deadline is reached, flushing all telemetry to disk atomically with `stopped_reason="DURATION_EXPIRED"` and `shutdown_cleanly=True`.

---

## 2. CLI Argument Specification & Calibration

| Parameter | CLI Flag | Value | Purpose & Verification Detail |
| :--- | :--- | :---: | :--- |
| **API Environment** | `--environment` | `production` | Connects strictly to Kalshi public read-only endpoints (`https://trading-api.kalshi.com/trade-api/v2`). |
| **Run Duration** | `--duration` | `900` | 15 minutes (900.0 seconds). Monotonically enforced by `BreadthScannerRunner`. |
| **Lookahead Threshold** | `--lookahead-threshold` | `60.0` | **Calibrated for Connection Pooling:** With HTTP connection pooling, a complete catalog discovery and screening pass takes ~35–60s (vs 603s previously). Setting `--lookahead-threshold 60.0` ensures the runner completes multiple cycles and halts cleanly before starting a cycle that would overrun the 15-minute deadline. |
| **Request Rate** | `--request-rate` | `5.0` | Global token bucket rate limit (5.0 req/s) shared across catalog pagination, event pre-filter, and order-book fetches. |
| **Concurrency** | `--concurrency` | `4` | Fixed worker pool for concurrent L2 book qualification (`ThreadPoolExecutor`). |
| **Leg Size Gate** | `--max-candidate-legs` | `8` | Rejects baskets $> 8$ legs with `REJECTED_BASKET_TOO_LARGE` prior to network dispatch. |
| **Min Net Profit** | `--min-net-profit` | `5` | Requires $\ge 5$ cents ($0.05 per contract) net edge after official CFTC taker fees. |
| **Event Pre-Filter** | `--enable-event-prefilter` | flag | Re-checks top-of-book prices at event level before deep book fetches. |
| **Output Directory** | `--output-dir` | `data/breadth_scanner/trial_short_15min_20261030` | Unique directory. Verified does NOT currently exist. |

---

## 3. Expected Shutdown & Persistence Behavior

1. **Duration Boundary:**
   - Monotonic deadline: $t_{\text{deadline}} = t_{\text{start}} + 900.0\text{s}$.
2. **Lookahead Boundary Check:**
   - Before starting cycle $k+1$, the runner checks:
     $$\text{remaining\_duration} < \max(t_{\text{last\_cycle}}, 60.0\text{s})$$
   - If insufficient time remains, the runner logs `Lookahead gate halted cycle k+1...` and initiates clean shutdown.
3. **Atomic Persistence:**
   - `scanner_state.json` is written via `.tmp` file and atomic `replace()`.
   - `stopped_reason` is set to `"DURATION_EXPIRED"`.
   - `status` is set to `"STOPPED"`.
   - `shutdown_cleanly` is set to `True`.
   - Candidate and qualification histories are appended and flushed.

---

## 4. Exact Manual Launch Commands

### Terminal 1 — Run the Short Controlled Observation Trial
Run from repository root (`/Users/vikramaditya/Disk/Projects/Kalshi`):

```bash
.venv/bin/python scripts/run_breadth_scanner.py \
  --environment production \
  --duration 900 \
  --lookahead-threshold 60.0 \
  --request-rate 5.0 \
  --concurrency 4 \
  --enable-event-prefilter \
  --max-candidate-legs 8 \
  --min-net-profit 5 \
  --output-dir data/breadth_scanner/trial_short_15min_20261030
```

### Terminal 2 — Launch Dedicated Trial Monitor Dashboard
Run from repository root:

```bash
.venv/bin/python scripts/run_dashboard.py \
  --host 127.0.0.1 \
  --port 8082 \
  --scanner-dir data/breadth_scanner/trial_short_15min_20261030
```
Open **`http://127.0.0.1:8082`** in a browser to monitor live trial telemetry.

---

## 5. Next Step

Awaiting user manual execution. After execution completes, proceed to **Stage 2: Second Trial Post-Run Analysis**.
