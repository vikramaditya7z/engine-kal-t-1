# Phase 2F-G3 Implementation Report: Timestamp, Freshness & Performance Telemetry

## 1. Executive Summary

Milestone G3 implements fine-grained timestamp, freshness, and sub-phase performance telemetry across the Kalshi Breadth-First Scanner catalog discovery and targeted qualification pipelines.

This milestone resolves the key observability and safety challenges identified during Phase 2F-D/F:
1. **Dual-Skew Validation & Tracking:** Disentangles client-side network fetch duration (`fetch_window_seconds`) from server-side quote timestamp drift (`server_timestamp_skew_seconds`).
2. **Sub-Phase Timing Breakdown:** Instruments non-overlapping timers for network I/O, JSON decoding, market normalization, catalog deduplication/reconciliation, arbitrage screening, and order-book qualification.
3. **Deterministic Quote Freshness Telemetry:** Populates `quote_age_seconds` and `candidate_leg_count` across all qualification outcomes, ensuring stale quotes or oversize baskets are auditable with zero ambiguity.
4. **Backward Compatibility:** All new metrics have clean defaults (`0.0` or `None`), preserving 100% compatibility with existing dashboard schemas, persistence mechanisms, and historical telemetry files.

All 8 targeted G3 tests and 143 total breadth scanner tests pass cleanly in offline mock mode.

---

## 2. Architecture & Design Implementation

### 2.1 Dual-Skew Metrics & Threshold Enforcement

When evaluating multi-leg candidates (e.g. MECE event baskets), quote freshness can be compromised in two distinct ways:
- **Client-Side Fetch Window Latency (`fetch_window_seconds`):** Wall-clock duration between starting the first order-book fetch and receiving the last constituent book response. If this duration exceeds `max_fetch_window_seconds` (default: 5.0s), the market quotes may have moved significantly between the start and end of the fetch window.
- **Server-Side Timestamp Drift (`server_timestamp_skew_seconds`):** Difference between the maximum and minimum exchange/edge timestamps across constituent books. If this delta exceeds `max_leg_skew_seconds` (default: 2.0s), constituent legs represent differing market moments and are rejected.

Both metrics are captured, recorded on `TargetedQualificationResult`, and enforced via explicit pre-qualification checks:
- Rejection reason for fetch window:
  `"Candidate order-book fetch window ({fetch_window:.3f}s) exceeded limit ({limit:.3f}s)"`
- Rejection reason for server skew:
  `"Constituent leg timestamp skew ({server_skew:.3f}s) exceeded limit ({limit:.3f}s)"`
- Both rejections map safely to `QualificationOutcome.REJECTED_LEG_SKEW`.

### 2.2 Sub-Phase Discovery Instrumentation

`CatalogDiscoveryResult` and `ScanCycleSummary` are instrumented with non-overlapping sub-phase timers:

| Telemetry Field | Measurement Scope |
| :--- | :--- |
| `network_time_seconds` | Transport `get()` call duration across all HTTP pagination requests. |
| `json_parse_time_seconds` | Time spent decoding JSON response strings into Python objects via `json.loads`. |
| `normalization_time_seconds` | Parsing and validating `NormalizedEvent` and `NormalizedMarket` objects. |
| `deduplication_time_seconds` | In-flight cursor loop checks, state reconciliation (new vs updated vs unchanged vs closed). |
| `screening_time_seconds` | Stage B breadth arbitrage screener execution time. |
| `total_discovery_time_seconds` | Cumulative end-to-end duration of catalog discovery pass. |
| `total_qualification_time_seconds` | Cumulative end-to-end duration of targeted qualification queue processing. |

### 2.3 Deterministic Quote Age & Constituent Leg Count

Every `TargetedQualificationResult` now records:
- `candidate_leg_count`: Extracted integer count of constituent legs. Populated even on gated size-limit rejections, pre-filter rejections, and quote staleness rejections.
- `quote_age_seconds`: Exact elapsed seconds between market quote timestamp (`quote_timestamp` or `market.updated_time`) and qualification evaluation timestamp (`now_utc`).

---

## 3. Verification & Test Evidence

### 3.1 Milestone G3 Test Suite (`tests/test_breadth_scanner_phase_2f_g3.py`)

Eight comprehensive offline tests were created to verify every requirement:
1. `test_g3_quote_age_calculation_deterministic`: Verifies quote age computation against a deterministic clock.
2. `test_g3_quote_age_stale_rejection_populates_telemetry`: Verifies rejection with `REJECTED_STALE` populates `quote_age_seconds` and `candidate_leg_count`.
3. `test_g3_server_timestamp_skew_cross_leg_delta`: Verifies cross-leg server timestamp delta computation on multi-leg order books.
4. `test_g3_server_timestamp_skew_exceeded_rejection`: Verifies rejection with `REJECTED_LEG_SKEW` when server timestamp delta exceeds threshold.
5. `test_g3_client_fetch_window_exceeded_rejection`: Verifies rejection with `REJECTED_LEG_SKEW` when client wall-clock fetch duration exceeds `max_fetch_window_seconds`.
6. `test_g3_discovery_sub_phase_timings`: Verifies `CatalogDiscoveryResult` sub-phase timers are non-overlapping and strictly positive.
7. `test_g3_scan_cycle_summary_telemetry_serialization`: Verifies `ScanCycleSummary.to_dict()` captures and formats all sub-phase timers.
8. `test_g3_backward_compatibility_old_telemetry_records`: Verifies instantiation of legacy qualification records without new G3 arguments defaults cleanly without raising `TypeError` or `KeyError`.

### 3.2 Test Run Results

```bash
$ ./.venv/bin/pytest tests/test_breadth_scanner_phase_2f_g3.py -v
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_quote_age_calculation_deterministic PASSED [ 12%]
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_quote_age_stale_rejection_populates_telemetry PASSED [ 25%]
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_server_timestamp_skew_cross_leg_delta PASSED [ 37%]
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_server_timestamp_skew_exceeded_rejection PASSED [ 50%]
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_client_fetch_window_exceeded_rejection PASSED [ 62%]
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_discovery_sub_phase_timings PASSED [ 75%]
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_scan_cycle_summary_telemetry_serialization PASSED [ 87%]
tests/test_breadth_scanner_phase_2f_g3.py::test_g3_backward_compatibility_old_telemetry_records PASSED [100%]
============================== 8 passed in 0.97s ===============================
```

Full breadth scanner regression:
```bash
$ ./.venv/bin/pytest tests/test_breadth_scanner*.py
============================= 143 passed in 9.44s ==============================
```

---

## 4. Safety Audit & Production Integrity

- **Offline Only:** All tests use offline mock transports; zero live Kalshi API endpoints were contacted.
- **Zero Invalidation of Preserved Data:** `data/breadth_scanner/smoke_test_20261028/` was not touched.
- **Port 8080 Dashboard:** Running instance was untouched.
- **Fail-Soft Telemetry:** Missing timestamp headers or unparseable formats fallback gracefully to `None` without crashing the scanner loop.
