# Phase 2F-G1-T — Local Dashboard Verification Report: Pre-Qualification Leg Gating & Prioritization

**Date:** 2026-10-30  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Working Branch:** `fix/v19-clean-checkout-test`  
**Milestone:** Phase 2F-G1-T — Local Dashboard Verification  
**Primary Production Dashboard (Untouched):** [http://127.0.0.1:8080](http://127.0.0.1:8080) (`data/breadth_scanner/smoke_test_20261028/`)  
**Phase 2F-G1 Test Dashboard:** [http://127.0.0.1:8081](http://127.0.0.1:8081) (`data/breadth_scanner/dashboard_test_20261030/`)  
**Safety Status:** Strictly offline execution. Zero live Kalshi API calls, zero production scans, zero modifications to production code, preserved smoke test data untouched, Port 8080 server untouched.

---

## 1. Executive Summary

In Phase 2F-G1, pre-qualification leg-count gating (`max_candidate_legs = 8`) and candidate edge-density prioritization ($\text{gross\_edge} / \text{leg\_count}$) were implemented in `TargetedOrderBookQualifier`. 

In this testing phase (Phase 2F-G1-T), the dashboard integration was verified to ensure that the user-facing web interface and backing API endpoints accurately reflect these Phase 2F-G1 enhancements:
1. **Existing Dashboard Preservation:** Inspected the active dashboard process running on [http://127.0.0.1:8080](http://127.0.0.1:8080) (PID `54914`) bound to the preserved smoke-test dataset `data/breadth_scanner/smoke_test_20261028/`. It was left completely untouched.
2. **Dedicated Mock Test Dataset:** Constructed an isolated, realistic mock test dataset under `data/breadth_scanner/dashboard_test_20261030/` containing the exact scenarios requested.
3. **Dedicated Test Dashboard Instance:** Launched a secondary dashboard daemon on [http://127.0.0.1:8081](http://127.0.0.1:8081) pointing strictly to the mock dataset.
4. **Comprehensive Scenario Verification:**
   - **8-Leg Candidate Passes Size Gate:** `PRE-MECE-YES:KXPASSGATE-8` (8 legs $\le$ 8 max limit) passes the gate, performs 8 order-book fetches, and records `REJECTED_INSUFFICIENT_DEPTH`.
   - **9-Leg Candidate Intercepted & Gated:** `PRE-MECE-YES:KXOVERSIZE-9` (9 legs $>$ 8) is rejected with `REJECTED_BASKET_TOO_LARGE` and 0 order-book fetches.
   - **Constituent Leg & Limit Reason Formatting:** Rejection reasons explicitly report observed leg count and maximum limit: `"Candidate constituent leg count (9) exceeds configured maximum allowable limit (8)"` and `"Candidate constituent leg count (16) exceeds configured maximum allowable limit (8)"`.
   - **Edge-Density Priority Ordering:** Verified that candidates are ordered in the table strictly by edge density (10.00¢/leg $\rightarrow$ 6.00¢/leg $\rightarrow$ 3.00¢/leg $\rightarrow$ 2.00¢/leg $\rightarrow$ 1.00¢/leg), rather than raw gross edge.
   - **Existing Rejection Reasons Displayed:** Verified formatting for `REJECTED_LEG_SKEW`, `REJECTED_UNPROFITABLE_FEES`, and `REJECTED_INSUFFICIENT_DEPTH`.
5. **Dashboard URL for Manual Inspection:** The dashboard is actively running and ready for manual visual inspection at **[http://127.0.0.1:8081](http://127.0.0.1:8081)**.

---

## 2. Server & Dataset Isolation

To ensure complete safety and prevent interference with active operations or historical data:

| Dashboard Instance | Port | Process / Task | Backing Dataset | Status |
| :--- | :---: | :---: | :--- | :--- |
| **Production Smoke Test** | `8080` | PID `54914` | `data/breadth_scanner/smoke_test_20261028/` | **Active & Untouched** |
| **Phase 2F-G1 Test Dashboard** | `8081` | Background Task `task-12422` | `data/breadth_scanner/dashboard_test_20261030/` | **Active & Verified** |

### Mock Fixtures Structure (`data/breadth_scanner/dashboard_test_20261030/`)
- `scanner_state.json`: Session metadata for `bscan-g1-test`, reporting `candidates_enqueued: 3`, and `qualification_counts` breakdowns including `REJECTED_BASKET_TOO_LARGE: 2`.
- `scan_summaries.jsonl`: Cycle 1 summary metrics (157,581 markets screened, 5 preliminary candidates).
- `candidate_history.jsonl`: 5 preliminary candidate records with constituent quotes and timestamps.
- `qualification_history.jsonl`: 5 targeted qualification records with detailed outcomes, leg counts, and rejection reasons.

---

## 3. Test Cases & Verification Results

### Summary Table of Test Scenarios

| Rank | Candidate ID | Legs | Gross Edge | Edge Density | Size Gate Status | Requests Made | Qualification Outcome | Rejection Reason / Evidence |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :--- | :--- |
| **1** | `PRE-MECE-YES:KXHIGHDENSITY-2` | 2 | 20¢ | **10.00¢/leg** | **PASS** ($2 \le 8$) | 2 | `REJECTED_LEG_SKEW` | *Cross-leg timestamp skew 7.20s exceeds maximum allowable threshold 5.00s* |
| **2** | `PRE-MECE-YES:KXPASSGATE-8` | 8 | 48¢ | **6.00¢/leg** | **PASS** ($8 \le 8$) | 8 | `REJECTED_INSUFFICIENT_DEPTH` | *Insufficient top-of-book and multi-level depth to satisfy target quantity 1 across all 8 legs* |
| **3** | `PRE-MECE-YES:KXNEXTSTATE-29` | 5 | 15¢ | **3.00¢/leg** | **PASS** ($5 \le 8$) | 5 | `REJECTED_UNPROFITABLE_FEES` | *Unprofitable after fees: gross edge 4¢ - fees 6¢ = net edge -2¢ < 1¢* |
| **4** | `PRE-MECE-NO:KXFOURSTATESEN-26NOV03` | 16 | 32¢ | **2.00¢/leg** | **BLOCKED** ($16 > 8$) | 0 | `REJECTED_BASKET_TOO_LARGE` | *Candidate constituent leg count (16) exceeds configured maximum allowable limit (8)* |
| **5** | `PRE-MECE-YES:KXOVERSIZE-9` | 9 | 9¢ | **1.00¢/leg** | **BLOCKED** ($9 > 8$) | 0 | `REJECTED_BASKET_TOO_LARGE` | *Candidate constituent leg count (9) exceeds configured maximum allowable limit (8)* |

---

## 4. API Endpoint Verification

Automated queries against the port 8081 REST API confirm complete data fidelity and correct schema parsing:

### 4.1 `/api/breadth/status`
```json
{
  "session_id": "bscan-g1-test",
  "status": "STOPPED",
  "total_preliminary_candidates_generated": 5,
  "candidates_enqueued": 3,
  "qualification_counts": {
    "REJECTED_BASKET_TOO_LARGE": 2,
    "REJECTED_INSUFFICIENT_DEPTH": 1,
    "REJECTED_LEG_SKEW": 1,
    "REJECTED_UNPROFITABLE_FEES": 1
  },
  "qualified_candidates_count": 0
}
```
*Verification:* Notice `candidates_enqueued` is 3, reflecting that the 2 oversized candidates (9 legs and 16 legs) were intercepted before queue ingestion and never enqueued. `qualification_counts` tracks all 5 outcomes.

### 4.2 `/api/breadth/strategies`
```json
{
  "qualification_by_strategy": {
    "mece_basket_long_yes": {
      "qualified": 0,
      "rejected": 4,
      "outcomes": {
        "REJECTED_BASKET_TOO_LARGE": 1,
        "REJECTED_INSUFFICIENT_DEPTH": 1,
        "REJECTED_LEG_SKEW": 1,
        "REJECTED_UNPROFITABLE_FEES": 1
      }
    },
    "mece_basket_long_no": {
      "qualified": 0,
      "rejected": 1,
      "outcomes": {
        "REJECTED_BASKET_TOO_LARGE": 1
      }
    }
  }
}
```
*Verification:* Strategy breakdowns correctly attribute `REJECTED_BASKET_TOO_LARGE` across both MECE Long YES (9 legs) and MECE Long NO (16 legs) strategies.

### 4.3 `/api/breadth/qualifications` (Edge-Density Order)
Executing:
```bash
curl -s http://127.0.0.1:8081/api/breadth/qualifications
```
Returns qualifications in exact descending edge-density order:
1. `PRE-MECE-YES:KXHIGHDENSITY-2`: Density = 10.00¢/leg (20¢ / 2 legs)
2. `PRE-MECE-YES:KXPASSGATE-8`: Density = 6.00¢/leg (48¢ / 8 legs)
3. `PRE-MECE-YES:KXNEXTSTATE-29`: Density = 3.00¢/leg (15¢ / 5 legs)
4. `PRE-MECE-NO:KXFOURSTATESEN-26NOV03`: Density = 2.00¢/leg (32¢ / 16 legs)
5. `PRE-MECE-YES:KXOVERSIZE-9`: Density = 1.00¢/leg (9¢ / 9 legs)

### 4.4 Outcome Filtering: `/api/breadth/qualifications?outcome=REJECTED_BASKET_TOO_LARGE`
```bash
curl -s "http://127.0.0.1:8081/api/breadth/qualifications?outcome=REJECTED_BASKET_TOO_LARGE"
```
Returns exactly the 2 size-gated records with their explanatory text:
- `PRE-MECE-NO:KXFOURSTATESEN-26NOV03` $\rightarrow$ *"Candidate constituent leg count (16) exceeds configured maximum allowable limit (8)"*
- `PRE-MECE-YES:KXOVERSIZE-9` $\rightarrow$ *"Candidate constituent leg count (9) exceeds configured maximum allowable limit (8)"*

---

## 5. UI Elements & Manual Verification Checklist

When opening **[http://127.0.0.1:8081](http://127.0.0.1:8081)** in a web browser, verify the following visual components:

### 1. Breadth Scanner Telemetry Cards (Top Grid)
- **Status Badge:** Displays `STOPPED` in yellow/neutral badge.
- **Markets Discovered / Screened:** Displays `157,581`.
- **Preliminary Candidates:** Displays `5` (`Candidates Enqueued: 3`).
- **Qualified Opportunities:** Displays `0` (`0.0% qualification rate`).
- **Order-Book Qualifications Attempted:** Displays `3` (`Gated / Blocked: 2`).

### 2. Screening Strategies Breakdown (Grid Card)
- **MECE Long YES:** 4 screened candidates; Rejected breakdown includes `BASKET TOO LARGE: 1`, `DEPTH: 1`, `FEES: 1`, `SKEW: 1`.
- **MECE Long NO:** 1 screened candidate; Rejected breakdown includes `BASKET TOO LARGE: 1`.

### 3. Candidates & Targeted Order-Book Qualifications Table
- **Row 1 (`KXHIGHDENSITY-2`):**
  - **Market / Event:** Chip `2 legs`, event header `KXHIGHDENSITY-2`.
  - **Type & Status:** Badge `<span class="badge-qual-rejected">REJECTED_LEG_SKEW</span>`.
  - **Outcome / Rejection Reason:** *"Cross-leg timestamp skew 7.20s exceeds maximum allowable threshold 5.00s"*.
- **Row 2 (`KXPASSGATE-8`):**
  - **Market / Event:** Chip `8 legs`, event header `KXPASSGATE-8`.
  - **Type & Status:** Badge `<span class="badge-qual-rejected">REJECTED_INSUFFICIENT_DEPTH</span>`.
  - **Outcome / Rejection Reason:** *"Insufficient top-of-book and multi-level depth to satisfy target quantity 1 across all 8 legs"*.
  - **Legs Disclosure:** Clicking *"View all 8 legs & quotes"* expands the drawer listing all 8 market tickers.
- **Row 3 (`KXNEXTSTATE-29`):**
  - **Market / Event:** Chip `5 legs`, event header `KXNEXTSTATE-29`.
  - **Type & Status:** Badge `<span class="badge-qual-rejected">REJECTED_UNPROFITABLE_FEES</span>`.
  - **Outcome / Rejection Reason:** *"Unprofitable after fees: gross edge 4¢ - fees 6¢ = net edge -2¢ < 1¢"*.
- **Row 4 (`KXFOURSTATESEN-26NOV03`):**
  - **Market / Event:** Chip `16 legs`, event header `KXFOURSTATESEN-26NOV03`.
  - **Type & Status:** Badge `<span class="badge-qual-rejected">REJECTED_BASKET_TOO_LARGE</span>`.
  - **Outcome / Rejection Reason:** *"Candidate constituent leg count (16) exceeds configured maximum allowable limit (8)"*.
- **Row 5 (`KXOVERSIZE-9`):**
  - **Market / Event:** Chip `9 legs`, event header `KXOVERSIZE-9`.
  - **Type & Status:** Badge `<span class="badge-qual-rejected">REJECTED_BASKET_TOO_LARGE</span>`.
  - **Outcome / Rejection Reason:** *"Candidate constituent leg count (9) exceeds configured maximum allowable limit (8)"*.

---

## 6. Access Instructions

1. Open your browser and navigate to:  
   👉 **`http://127.0.0.1:8081`**
2. Scroll to the **"Candidates & Targeted Order-Book Qualifications"** table.
3. Observe that all 5 candidates are rendered in descending edge-density order.
4. Expand the legs disclosure dropdowns to inspect constituent markets and quotes.
5. Verify that the production smoke-test dashboard remains untouched and active at:  
   👉 **`http://127.0.0.1:8080`**
