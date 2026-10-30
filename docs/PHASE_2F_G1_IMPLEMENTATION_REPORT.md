# Phase 2F-G1 Implementation Report: Pre-Qualification Leg Gating & Candidate Prioritization

**Date:** 2026-10-29  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Working Branch:** `fix/v19-clean-checkout-test`  
**Phase:** Phase 2F-G1 (Phase 1 of Reference Design `docs/PHASE_2F_F_QUALIFICATION_AND_PERFORMANCE_DESIGN.md`)  
**Safety Status:** Offline execution only. Zero live Kalshi API calls, zero orders placed, zero background processes started, preserved smoke test data untouched.

---

## 1. Executive Summary

Phase 2F-G1 implements the first phase of the qualification bottleneck remediation design:
1. **Pre-Qualification Constituent Leg Gating:** Added a configurable `max_candidate_legs` setting (default: `8`). Candidates exceeding this threshold (such as Candidate 5 with 33 legs) are intercepted before entering the targeted order-book request queue and immediately rejected with explicit outcome `REJECTED_BASKET_TOO_LARGE` and **zero order-book network requests**.
2. **Candidate Prioritization by Edge Density:** Replaced raw gross dollar sorting in the priority qualification queue with an **edge-density** metric ($\text{gross\_edge} / \text{leg\_count}$), accompanied by a deterministic tie-breaker (higher gross edge, then earlier alphabetical candidate ID/stable key).
3. **Queue Eviction Alignment:** Aligned the bounded queue eviction policy (`max_queue_size`) with the edge-density priority function, ensuring high-density candidates never suffer premature eviction by low-density baskets with large gross dollars.
4. **Offline Test Coverage:** Added 12 new deterministic offline tests in `tests/test_breadth_scanner_phase_2f_g1.py`. All 144 breadth scanner tests and all 863 suite tests pass cleanly.

---

## 2. Configuration Defaults & Conventions

| Configuration Class / Entry Point | Parameter | Type | Default | Validation & Behavior |
| :--- | :--- | :---: | :---: | :--- |
| `TargetedOrderBookQualifierConfig` | `max_candidate_legs` | `int` | `8` | Validates `> 0` in `__post_init__`; raises `ValueError` if non-positive. |
| `BreadthScannerRunnerConfig` | `max_candidate_legs` | `int` | `8` | Validates `> 0` in `__post_init__`; passed down to qualifier configuration. |
| `scripts/run_breadth_scanner.py` (CLI) | `--max-candidate-legs` | `int` | `8` | Validates `> 0` in `parse_args()`; outputs setting in CLI startup banner. |

---

## 3. Rejection Evidence & Leg Gating Architecture

### Shared Interception Mechanism

Leg gating is enforced centrally within `TargetedOrderBookQualifier` across two critical entry points:

1. **At `enqueue_candidate(candidate)`:**
   - Evaluates `extract_constituent_leg_count(candidate)`.
   - If leg count exceeds `config.max_candidate_legs` or leg data is malformed:
     - The candidate is **not added to `self._queue`** (the order-book request queue).
     - An auditable `TargetedQualificationResult` is generated with `outcome = "REJECTED_BASKET_TOO_LARGE"` and `requests_made = 0`.
     - The result is buffered in `self._gated_rejections`.
     - The candidate's stable key is cached in `_evaluated_candidates` to prevent repeated redundant processing.
     - `enqueue_candidate` returns `False`.
2. **At `qualify_candidate(candidate)`:**
   - Before evaluating quote staleness or initiating any HTTP requests, `extract_constituent_leg_count(candidate)` is verified.
   - If oversized or malformed, returns `TargetedQualificationResult` immediately with `outcome = "REJECTED_BASKET_TOO_LARGE"` and `requests_made = 0`.
3. **At `process_queue()`:**
   - Pre-qualification rejections in `self._gated_rejections` are collected into `summary.results`.
   - Tracked under `outcomes_breakdown["REJECTED_BASKET_TOO_LARGE"]`.
   - Exported to `qualification_history.jsonl` and `scanner_state.json` via existing runner persistence.

### Rejection Reason Evidence Format

When a candidate is rejected by the leg gate, `rejection_reason` explicitly documents both the observed constituent leg count and the configured threshold:
```
"Candidate constituent leg count (9) exceeds configured maximum allowable limit (8)"
```
If constituent leg data is missing or malformed:
```
"Candidate constituent leg data is invalid: Candidate market_tickers is missing (None)"
"Candidate constituent leg data is invalid: Candidate market_tickers is empty"
"Candidate constituent leg data is invalid: Candidate market_tickers[0] is not a valid non-empty string: 123"
```
Under no circumstances is an unknown or missing leg count silently treated as zero.

---

## 4. Prioritization Behavior & Edge-Density Mechanics

### Edge-Density Formula

For any candidate $C$:
$$\text{Edge Density}(C) = \frac{\text{gross\_edge\_dollars}}{\text{constituent\_leg\_count}}$$

If $\text{gross\_edge\_dollars} \le 0$, or $\text{gross\_edge\_dollars}$ is invalid/non-finite, or $\text{constituent\_leg\_count} \le 0$:
$$\text{Edge Density}(C) = 0.00$$

### Deterministic Priority Ordering (`CandidatePriority`)

Candidates in `TargetedOrderBookQualifier._queue` are sorted in descending priority using `CandidatePriority`:
1. **Primary Key:** `edge_density` (higher is better, descending).
2. **Secondary Key:** `gross_edge_dollars` (higher total dollar profit is better, descending).
3. **Tie-Breaker:** `candidate_id` / `stable_key` alphabetical order (earlier ID is strictly prioritized over later ID, guaranteeing deterministic ordering across runs).

### Eviction Consistency

When the bounded queue is full (`len(_queue) >= max_queue_size`):
```python
min_idx = min(range(len(self._queue)), key=lambda i: compute_candidate_priority(self._queue[i]))
lowest = self._queue[min_idx]
if cand_prio > compute_candidate_priority(lowest):
    self._queue_keys.remove(lowest.stable_key)
    self._queue.pop(min_idx)
```
This ensures that incoming candidates with higher edge density safely evict lower-density candidates, preventing queue starvation by large baskets with modest edge.

---

## 5. Offline Test Results

### Test Execution Summary

All tests executed strictly in offline mode using mock transports and deterministic fixtures:

1. **Phase 2F-G1 Dedicated Suite (`tests/test_breadth_scanner_phase_2f_g1.py`):**
   ```bash
   ./.venv/bin/pytest tests/test_breadth_scanner_phase_2f_g1.py -v
   # 12 passed in 1.98s
   ```
   - `test_basket_with_8_legs_not_rejected_by_size_gate`: PASSED
   - `test_basket_with_9_legs_rejected_before_qualification`: PASSED
   - `test_oversized_candidate_qualify_candidate_direct_call_zero_requests`: PASSED
   - `test_smaller_candidates_reach_normal_qualification_path`: PASSED
   - `test_missing_or_malformed_leg_data_handled_safely`: PASSED
   - `test_higher_edge_density_prioritized_over_lower_edge_density`: PASSED
   - `test_equal_edge_densities_deterministic_tie_breaking`: PASSED
   - `test_equal_edge_densities_higher_gross_edge_preferred`: PASSED
   - `test_invalid_denominators_and_malformed_edges_cannot_crash_queue`: PASSED
   - `test_bounded_queue_eviction_uses_edge_density`: PASSED
   - `test_rejection_outcome_persisted_to_history_and_telemetry`: PASSED
   - `test_cli_argument_max_candidate_legs`: PASSED

2. **Breadth Scanner Full Test Group:**
   ```bash
   ./.venv/bin/pytest tests/test_breadth_scanner_continuous.py tests/test_breadth_scanner.py tests/test_breadth_scanner_runner.py tests/test_dashboard_breadth_scanner.py tests/test_breadth_scanner_phase_2f_g1.py
   # 144 passed in 4.62s
   ```

3. **Core Qualifier & Evaluation Group:**
   ```bash
   ./.venv/bin/pytest tests/test_v22_execution_qualifier.py tests/test_v21_continuous_evaluator.py tests/test_arbitrage.py tests/test_evaluation.py
   # 96 passed in 3.06s
   ```

4. **Full Repository Regression Suite:**
   ```bash
   ./.venv/bin/pytest
   # 863 passed in 31.2s (Zero regressions, zero failures)
   ```

---

## 6. Changed Files & Scope Verification

Only the specific files authorized for Phase 1 were modified:

| File Path | Description of Changes |
| :--- | :--- |
| `kalshi_arbitrage/execution_qualifier.py` | Added `REJECTED_BASKET_TOO_LARGE` member to `QualificationOutcome` enum. |
| `kalshi_arbitrage/breadth_scanner.py` | Added `extract_constituent_leg_count()`, `CandidatePriority`, `compute_candidate_edge_density()`, `compute_candidate_priority()`; added `max_candidate_legs` to configs; added leg gating and edge-density prioritization to `TargetedOrderBookQualifier` and `BreadthScannerRunner`. |
| `scripts/run_breadth_scanner.py` | Added `--max-candidate-legs` CLI argument, argument validation, banner reporting, and runner config plumbing. |
| `tests/test_breadth_scanner_phase_2f_g1.py` | Added 12 comprehensive unit and integration tests covering all Phase 1 gating and prioritization requirements. |

### Preserved Invariants
- `data/breadth_scanner/smoke_test_20261028/` remains completely unmodified.
- No network endpoints were contacted.
- No trading or execution thresholds were loosened.
- Dashboard server (PID 54914) remained uninterrupted.

---

## 7. Remaining Risks & Phase 2 Outlook

1. **Constituent Skew on Surviving 6–8 Leg Baskets:**
   - Gating baskets $>8$ legs eliminates extreme skews (e.g. 33 legs).
   - However, sequential fetching of 6 to 8 legs at 0.8–1.0 s/req can still observe 6.0–8.0 s of latency, risking skew rejection until Phase 5 (Bounded Concurrent Retrieval) is implemented.
2. **Next Logical Step (Phase 2):**
   - Implement Phase 2: Top-of-Book Event Pre-Filter (`GET /events/{event_ticker}`) to check whether preliminary edge still exists with 1 request before querying L2 books for 2–8 legs.
