# Kalshi Arbitrage Engine V20 — Continuous Live Market Data Infrastructure Report

**Milestone:** V20  
**Implementation Date:** 2026-10-28  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Base Release:** `v19.0.1` (`21188a3f42c0382c5e19c8af96521afd46e9f7b1`)  
**Overall Status:** **COMPLETE & VERIFIED (LIVE DATA PIPELINE OPERATIONAL)**  

---

## 1. Executive Summary

Milestone **V20: Continuous Live Market Data Infrastructure** transforms the Kalshi Arbitrage Engine from bounded, manual, or batch research scripts into a continuously running, reliable live market data ingestion service.

### Key Capabilities Delivered:
1. **Continuous Live Collector Engine (`ContinuousCollector`):**
   - Continuously monitors active Kalshi prediction markets via the public, unauthenticated read-only REST API.
   - Bounded concurrent worker pool (`concurrency: 4`) minimizes cross-market and cross-leg observation skew.
   - Single-flight cycle execution lock prevents overlapping cycles even under heavy network latency or backoff.
   - Resilient HTTP 429 rate-limit handling with exponential backoff and retry pacing.
   - Bounded memory footprint ($O(1)$ ring buffer) to eliminate memory leaks during indefinite long-term execution.
   - Durable streaming to JSON Lines (`observations.jsonl`) accompanied by cryptographic SHA-256 integrity manifests (`manifest.json`).
2. **Collector Health & Status Interface:**
   - Atomic state persistence (`data/live_collector_state.json`) updated on every cycle and shutdown.
   - REST API endpoints (`/api/collector/status` and `/api/status`) providing real-time telemetry on collector state, snapshot age, consecutive failure counts, and freshness status.
3. **Simple CLI Service Runner (`scripts/run_continuous_collector.py`):**
   - Clean, standard command-line service with configurable intervals, concurrency, market limits, and targets.
   - Graceful signal handling (`SIGINT` / `SIGTERM`) completing in-flight cycles, updating state to `STOPPED`, and cleanly exiting.
4. **Verification & Testing:**
   - **645 tests passing in 16.23s** (100% pass rate: 628 existing tests + 17 new V20 tests).
   - **Live Production Trial:** Successfully executed against Kalshi production API (`https://external-api.kalshi.com/trade-api/v2`), capturing 20 live observations across 4 consecutive cycles with verified advancing exchange timestamps (`source_timestamp`).

---

## 2. Architecture & Data Flow

```
                  [ Kalshi Public REST API ]
                              │
                    (Read-Only HTTP GET)
                              │
                              ▼
                  [ KalshiRestClient (urllib) ]
                              │
          ┌───────────────────┴───────────────────┐
          │ (Concurrent Worker Threads, pool=4)  │
          ▼                                       ▼
  [ Fetch Market Meta ]                  [ Fetch Level-2 Book ]
          │                                       │
          └───────────────────┬───────────────────┘
                              │
                              ▼
            [ Timestamp & Freshness Validation ]
             - HTTP Date source_timestamp
             - Age check: now - source_ts <= max_stale
             - Future timestamp guard (< 10s)
             - Empty book / malformed payload check
                              │
                              ▼
                [ MarketObservation Record ]
                 - Transport: REST_CONTINUOUS_V20
                 - Session ID: v20-continuous-{ts}-{pid}
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
  [ Bounded Deque ]   [ JSONL Streaming ]   [ Atomic State File ]
  (maxlen=200 in RAM) (observations.jsonl)  (live_collector_state.json)
                              │                           │
                              ▼                           ▼
                     [ SHA-256 Manifest ]       [ Dashboard API ]
                       (manifest.json)       (/api/collector/status)
```

---

## 3. Inventory of Changes & New Files

| File Path | Action | Description |
|---|---|---|
| `kalshi_arbitrage/continuous_collector.py` | **NEW** | Core continuous live market data collector, configuration dataclasses, cycle summary, and health telemetry. |
| `scripts/run_continuous_collector.py` | **NEW** | Standalone CLI entry point for continuous live data service with signal trapping. |
| `tests/test_v20_continuous_collector.py` | **NEW** | Comprehensive unit & integration tests (17 tests) covering all failure, retry, backoff, and lifecycle scenarios. |
| `kalshi_arbitrage/__init__.py` | **MODIFIED** | Exported `ContinuousCollector`, `ContinuousCollectorConfig`, `CollectorHealth`, `CollectorStatus`, `CollectorCycleSummary`. |
| `kalshi_arbitrage/dashboard/service.py` | **MODIFIED** | Added `get_continuous_collector_status()` and `get_continuous_collector_process_status()`, incorporated into `get_system_health()`. |
| `kalshi_arbitrage/dashboard/server.py` | **MODIFIED** | Added `/api/collector` and `/api/collector/status` HTTP GET endpoints, fixed ephemeral port binding on port 0. |
| `docs/V20_CONTINUOUS_COLLECTION_REPORT.md` | **NEW** | Complete architectural documentation and empirical verification report. |

---

## 4. Startup and Shutdown Instructions

### 4.1 Running the Continuous Collector
Run the collector in discovery mode (automatically discovers active open markets):
```bash
./.venv/bin/python scripts/run_continuous_collector.py \
  --environment production \
  --interval 2.0 \
  --concurrency 4 \
  --max-markets 30 \
  --output-dir data/live_collection
```

Or monitor specific market tickers:
```bash
./.venv/bin/python scripts/run_continuous_collector.py \
  --tickers KXFEDDECISION-26OCT-T4.50 KXFEDDECISION-26OCT-T4.75 KXFEDDECISION-26OCT-T5.00 \
  --interval 1.0 \
  --concurrency 4
```

### 4.2 Graceful Shutdown
To stop the continuous collector cleanly:
- Press **`Ctrl+C`** in the terminal where the process is running, or send `SIGTERM` / `SIGINT`:
  ```bash
  kill -SIGTERM <PID>
  ```
- The service will finish any active cycle, flush pending evidence to `observations.jsonl`, write the updated `manifest.json`, persist `"status": "STOPPED"` to `collector_state.json`, and terminate cleanly with exit code 0.

---

## 5. Configuration Options & Defaults

| Option | Flag | Type | Default | Description |
|---|---|---|---|---|
| `environment` | `--environment` | `str` | `"production"` | Kalshi environment (`"production"` or `"demo"`). |
| `poll_interval_seconds` | `--interval` | `float` | `2.0` | Pacing delay between consecutive collection cycles in seconds. |
| `concurrency` | `--concurrency` | `int` | `4` | Maximum worker threads fetching order books in parallel. |
| `target_tickers` | `--tickers` | `list` | `None` | Explicit market tickers. If omitted, automatically discovers open markets. |
| `max_markets_to_poll` | `--max-markets` | `int` | `30` | Maximum open markets to poll per cycle in discovery mode. |
| `max_stale_seconds` | `--max-stale-seconds`| `float` | `60.0` | Freshness cutoff; books older than this are flagged stale. |
| `request_timeout_seconds`| N/A | `float` | `10.0` | Socket timeout for individual HTTP GET requests. |
| `max_retries` | N/A | `int` | `3` | Maximum retry attempts for transient errors before failing closed. |
| `rate_limit_backoff_seconds`| N/A | `float` | `2.0` | Base exponential backoff seconds on HTTP 429 response. |
| `output_dir` | `--output-dir` | `Path` | `data/live_collection` | Directory to stream `observations.jsonl` and `manifest.json`. |
| `state_file` | `--state-file` | `Path` | `data/live_collector_state.json` | JSON path to publish real-time health telemetry. |
| `max_cycles` | `--cycles` | `int` | `None` | Maximum cycles before halting (None = continuous indefinite). |
| `max_memory_observations`| N/A | `int` | `200` | In-memory deque size limit for memory leak prevention. |

---

## 6. Collector Health & Status Interface

### 6.1 State File (`data/live_collector_state.json`)
Every cycle and shutdown event atomically writes this file:
```json
{
  "status": "RUNNING",
  "pid": 14309,
  "session_id": "v20-continuous-1793168275-14309",
  "environment": "production",
  "started_at": "2026-10-28T06:17:55.212874+00:00",
  "stopped_at": null,
  "last_successful_collection_time": "2026-10-28T06:18:12.828806+00:00",
  "last_attempted_collection_time": "2026-10-28T06:18:08.879702+00:00",
  "latest_snapshot_timestamp": "2026-10-28T06:18:12+00:00",
  "latest_snapshot_age_seconds": 0.83,
  "is_data_fresh": true,
  "consecutive_failures": 0,
  "successful_cycles_count": 4,
  "failed_cycles_count": 0,
  "total_cycles_count": 4,
  "total_observations_count": 20,
  "active_monitored_markets_count": 5,
  "last_cycle_duration_seconds": 3.95,
  "last_cycle_markets_polled": 5,
  "last_cycle_markets_succeeded": 5,
  "last_cycle_markets_failed": 0,
  "last_error": null,
  "last_error_code": null,
  "rate_limit_count": 0,
  "poll_interval_seconds": 1.5,
  "max_stale_seconds": 60.0
}
```

### 6.2 Dashboard HTTP REST Endpoints
- **`GET /api/collector/status`**: Returns the health telemetry object shown above.
- **`GET /api/status`**: Includes `"continuous_collector": { ... }` alongside system diagnostics.

---

## 7. Data Freshness and Integrity Guarantees

1. **Exchange Transport Timestamp:**
   - Extracts the authoritative HTTP `Date` response header from the Kalshi order-book response (`source_timestamp`).
   - If `(now - source_timestamp) > max_stale_seconds`, the observation is recorded with `is_stale = True` and an explicit `staleness_reason`.
   - If `source_timestamp` is in the future by more than 10.0 seconds (clock drift), it is flagged `is_stale = True`.
2. **No Synthetic Value Substitution:**
   - When an API request fails, times out, or encounters a rate limit, the collector **never** substitutes historical data, synthetic prices, or made-up quantities.
   - Unsuccessful fetches are explicitly flagged as `is_success = False`, `is_stale = True`, and preserve the error code (`HTTP 429`, `TIMEOUT`, `SERVER_ERROR`).
3. **Empty Book Representation:**
   - Order books with zero bids or asks are normalized as valid, empty `NormalizedOrderBook` structures (`yes_bids=()`, `no_bids=()`) rather than raising errors or faking liquidity.

---

## 8. Failure and Recovery Behavior

| Scenario | Behavior & Recovery Mechanism |
|---|---|
| **HTTP 429 Rate Limit** | Increments `rate_limit_count`, logs warning, extracts `Retry-After` header or applies exponential backoff ($2.0 \times 2^a$ up to 30.0s), and pauses worker threads. |
| **HTTP 5xx Server Error** | Applies bounded exponential retry ($0.5 \times 2^a$) up to `max_retries` (3 attempts). Fails closed if the server remains unresponsive. |
| **Socket Timeout / Disconnect** | Retries up to `max_retries`. If all retries fail, records failure for that market while allowing other healthy markets in the cycle to succeed. |
| **Malformed JSON Payload** | Trapped by `MarketDataInputError`; marks observation as `is_success = False`, `is_stale = True`, `staleness_reason = "Malformed market data payload"`. Process does not crash. |
| **Overlapping Cycles** | Protected by `_cycle_lock.acquire(blocking=False)`. If a cycle takes longer than `poll_interval_seconds`, subsequent triggers are skipped cleanly without stacking concurrent threads. |
| **Process Crash / SIGTERM** | Signal handler immediately triggers `collector.stop()`, waits for in-flight cycle to release lock, updates manifest and state file, and exits cleanly. |

---

## 9. Automated Test Suite Results

- **Test Command:** `./.venv/bin/pytest -v tests/test_v20_continuous_collector.py`
- **Result:** **17 passed in 1.09s** (100% pass rate).
- **Full Suite Command:** `./.venv/bin/pytest -q`
- **Result:** **645 passed in 16.23s** (0 failed, 0 errors, 0 skipped).

### Verified Test Cases:
1. `test_continuous_collector_config_validation` — Parameter bounds checks.
2. `test_repeated_successful_collection_cycles` — Multi-cycle collection and state tracking.
3. `test_configurable_poll_interval_pacing` — Adaptive sleep and cycle duration pacing.
4. `test_graceful_shutdown` — Threaded startup and clean signal-based stopping.
5. `test_rate_limit_429_handling_and_backoff` — 429 detection and exponential backoff.
6. `test_rate_limit_429_exhaustion_marks_failure` — Terminal failure when 429 persists.
7. `test_transient_transport_error_retries_and_succeeds` — Network retry recovery.
8. `test_recovery_after_temporary_cycle_failure` — Resetting `consecutive_failures` upon recovery.
9. `test_malformed_market_payload_fails_closed` — Malformed response fail-closed guard.
10. `test_empty_order_books_handled_without_fabrication` — Clean handling of 0-depth books.
11. `test_stale_order_book_detected_against_threshold` — Stale data threshold check.
12. `test_future_timestamp_detected_and_rejected` — Future clock drift rejection.
13. `test_prevention_of_overlapping_cycles` — Single-flight lock verification.
14. `test_bounded_memory_growth` — Ring buffer capping (`max_memory_observations`).
15. `test_persistence_and_manifest_integrity` — JSONL streaming and SHA-256 manifest.
16. `test_dashboard_continuous_collector_status` — Service state reading.
17. `test_dashboard_server_api_collector_endpoint` — HTTP REST API `/api/collector/status`.

---

## 10. Live Empirical Verification Evidence

A live 4-cycle verification trial was executed against the public Kalshi production API (`https://external-api.kalshi.com/trade-api/v2`):
- **Command:** `./.venv/bin/python scripts/run_continuous_collector.py --interval 1.5 --concurrency 4 --max-markets 5 --cycles 4 --output-dir /tmp/v20_live_trial`
- **Session ID:** `v20-continuous-1793168275-14309`
- **Total Observations:** 20 (5 markets $\times$ 4 cycles)
- **Successful Cycles:** 4 of 4 (100%)
- **Rate Limit Events:** 0
- **Evidence SHA-256:** `e04cfc25676a6460143826fdb2d2eeb52203e098a5d3f23a54d5884976781298` (Verified)

### Observed Timestamp Progression across Consecutive Cycles:
| Ticker | Cycle | `observed_at` (UTC) | `source_timestamp` (UTC) | Status | Fresh? |
|---|---|---|---|---|---|
| `KXMVECROSSCATEGORY-...-5BMA3ZZOK` | 1 | 06:17:58.879 | 06:17:58+00:00 | Succeeded | Fresh |
| `KXMVECROSSCATEGORY-...-5BMA3ZZOK` | 2 | 06:18:02.823 | 06:18:02+00:00 | Succeeded | Fresh |
| `KXMVECROSSCATEGORY-...-5BMA3ZZOK` | 3 | 06:18:06.974 | 06:18:06+00:00 | Succeeded | Fresh |
| `KXMVECROSSCATEGORY-...-5BMA3ZZOK` | 4 | 06:18:10.905 | 06:18:10+00:00 | Succeeded | Fresh |
| `KXMVECROSSCATEGORY-...-Z36HGK5YK` | 1 | 06:18:00.777 | 06:18:00+00:00 | Succeeded | Fresh |
| `KXMVECROSSCATEGORY-...-Z36HGK5YK` | 2 | 06:18:05.020 | 06:18:04+00:00 | Succeeded | Fresh |
| `KXMVECROSSCATEGORY-...-Z36HGK5YK` | 3 | 06:18:08.876 | 06:18:08+00:00 | Succeeded | Fresh |
| `KXMVECROSSCATEGORY-...-Z36HGK5YK` | 4 | 06:18:12.828 | 06:18:12+00:00 | Succeeded | Fresh |

Both local receive times and exchange HTTP Date timestamps advanced with each cycle, verifying that fresh live market data is actively ingested without caching or duplication.

---

## 11. Scope Limitations & Handoff to V21

In strict adherence to the milestone boundaries:
- **No Arbitrage Detection Added (V21 Scope):** V20 focuses exclusively on the continuous live market data infrastructure. Real-time arbitrage candidate detection, notification queues, and continuous opportunity filtering belong to V21.
- **No Automated Trading (V22 Scope):** V20 contains zero live order routing, order cancellation, or trading credential logic.
- **No Dashboard UI Redesign (V23 Scope):** V20 exposed the backend REST APIs (`/api/collector/status`) and health telemetry. Dynamic UI widgets for live data streams will be implemented in V23.
- **WebSocket v2 Live Auth:** The WebSocket v2 protocol implementation remains hermetically verified; public REST polling provides continuous market updates without requiring RSA-signed credentials.
