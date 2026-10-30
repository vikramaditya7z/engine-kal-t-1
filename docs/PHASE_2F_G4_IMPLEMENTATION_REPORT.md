# Phase 2F-G4 Implementation Report: Strict Runtime & Duration Enforcement

## 1. Executive Summary

Milestone G4 implements strict runtime boundaries, lookahead cycle initiation gating, monotonic deadline propagation, and atomic final persistence across the Kalshi Breadth-First Scanner.

This milestone guarantees that prospective observation trials (e.g., 1-hour or multi-hour production trials) respect their requested runtime without overrun:
1. **Lookahead Cycle Gate:** Before initiating Cycle $N+1$, the runner verifies that remaining duration is strictly greater than the estimated cycle duration ($\max(\text{last\_cycle\_duration}, \text{lookahead\_threshold})$). If remaining time is insufficient, the runner halts cleanly with `stopped_reason = "DURATION_EXPIRED"` rather than starting a cycle doomed to overrun.
2. **Monotonic Deadline Propagation:** A wall-clock monotonic deadline (`start_mono + duration_seconds`) is passed down through `run_cycle`, `discover_catalog`, and `process_queue`.
3. **Graceful Discovery Interruption & State Retention:** If catalog pagination hits the deadline mid-pass, pagination halts cleanly (`is_complete = False`, `error_message = "HALTED_BY_RUN_DURATION_DEADLINE"`). Prior discovered markets are preserved in memory; market closures are NOT evaluated on partial passes, preventing spurious market-closure churn.
4. **Queue Bounding with `SKIPPED_TIMEOUT`:** If qualification queue processing hits the deadline, unprocessed candidates are marked `SKIPPED_TIMEOUT` and recorded with zero orphan threads.
5. **Auditable Final Persistence:** The `finally:` block of `runner.run()` guarantees that `stopped_reason`, `final_elapsed_seconds`, and `shutdown_cleanly = True` are written atomically to `scanner_state.json`.

All 5 targeted G4 tests and 148 total breadth scanner tests pass cleanly in offline mock mode.

---

## 2. Architecture & Design Implementation

### 2.1 Lookahead Cycle Initiation Gate

In long-running trials, starting a 740-second discovery/screening/qualification cycle with only 200 seconds remaining inevitably causes an overrun.
The runner now evaluates:
```python
if self.config.enable_lookahead_gate and self._cycle_counter >= 1:
    if self.config.duration_seconds is not None and self.config.duration_seconds >= self.config.lookahead_threshold_seconds:
        estimated_cycle_time = max(self._health.last_cycle_duration_seconds, self.config.lookahead_threshold_seconds)
        if remaining_duration < estimated_cycle_time:
            logger.info(
                "Lookahead gate halted cycle %d: remaining duration %.2fs < estimated cycle duration %.2fs",
                self._cycle_counter + 1, remaining_duration, estimated_cycle_time
            )
            self._health.stopped_reason = "DURATION_EXPIRED"
            break
```
This guarantees that for a 3600s trial with ~740s cycles, the runner halts after Cycle 4 at $T \approx 2970\text{s}$ ($\le 3600\text{s}$), eliminating overruns.

### 2.2 Mid-Cycle Deadline Propagation in Discovery & Qualification

- **`CatalogDiscoveryEngine.discover_catalog(deadline=...)`:**
  Between pagination requests and during retry backoffs, `time.monotonic() >= deadline` is checked. If reached, discovery halts immediately:
  - `result.is_complete = False`
  - `result.error_message = "HALTED_BY_RUN_DURATION_DEADLINE"`
  - `self.active_tickers.update(discovered_markets)` (merges discovered subset without pruning absent tickers).
- **`TargetedOrderBookQualifier.process_queue(deadline=...)`:**
  Before evaluating each enqueued candidate:
  - If deadline is exceeded, remaining enqueued items are recorded with `outcome = "SKIPPED_TIMEOUT"`, `is_qualified = False`, and `rejection_reason = "Qualification halted by run duration deadline"`.

### 2.3 Comprehensive Health Telemetry Additions

`BreadthScannerHealth` and its serialized JSON dictionary include:
- `stopped_reason`: Exact termination cause (`"DURATION_EXPIRED"`, `"USER_REQUEST"`, `"MAX_CYCLES_REACHED"`, `"SINGLE_PASS_COMPLETE"`, or `"STOPPED"`).
- `final_elapsed_seconds`: Wall-clock monotonic elapsed run duration.
- `shutdown_cleanly`: Boolean confirming all state files were flushed and locked resources released.
- `total_cycles_incomplete`: Cumulative count of cycles with partial or interrupted discovery.

---

## 3. Verification & Test Evidence

### 3.1 Milestone G4 Test Suite (`tests/test_breadth_scanner_phase_2f_g4.py`)

Five targeted offline tests verify every G4 requirement:
1. `test_runner_lookahead_gate_prevents_overrun`: Verifies runner halts before next cycle when remaining duration is below threshold, maintaining total elapsed time strictly $\le \text{duration\_seconds}$.
2. `test_runner_mid_cycle_discovery_deadline_partial_pass`: Verifies discovery deadline halts pagination mid-pass, retains partial markets, and preserves existing catalog.
3. `test_runner_mid_cycle_qualification_deadline_skips_queue`: Verifies qualification deadline halts queue processing and records remaining items as `SKIPPED_TIMEOUT`.
4. `test_runner_clean_persistence_on_user_stop`: Verifies cooperative stop writes atomic state file with `stopped_reason = "USER_REQUEST"` and `shutdown_cleanly = True`.
5. `test_runner_max_cycles_reached_sets_stopped_reason`: Verifies cycle limit halts with `stopped_reason = "MAX_CYCLES_REACHED"`.

### 3.2 Test Run Results

```bash
$ ./.venv/bin/pytest tests/test_breadth_scanner_phase_2f_g4.py -v
tests/test_breadth_scanner_phase_2f_g4.py::test_runner_lookahead_gate_prevents_overrun PASSED [ 20%]
tests/test_breadth_scanner_phase_2f_g4.py::test_runner_mid_cycle_discovery_deadline_partial_pass PASSED [ 40%]
tests/test_breadth_scanner_phase_2f_g4.py::test_runner_mid_cycle_qualification_deadline_skips_queue PASSED [ 60%]
tests/test_breadth_scanner_phase_2f_g4.py::test_runner_clean_persistence_on_user_stop PASSED [ 80%]
tests/test_breadth_scanner_phase_2f_g4.py::test_runner_max_cycles_reached_sets_stopped_reason PASSED [100%]
============================== 5 passed in 0.30s ===============================
```

Full breadth scanner regression:
```bash
$ ./.venv/bin/pytest tests/test_breadth_scanner*.py
============================= 148 passed in 9.66s ==============================
```

---

## 4. Safety Audit & Production Integrity

- **Strictly Offline:** Zero live Kalshi API endpoints were called.
- **Atomic Persistence:** State files are updated using `.tmp` write and atomic filesystem rename (`os.replace` / `Path.replace`).
- **Dashboard Integrity:** Local port 8080 process and smoke-test dataset were completely untouched.
