# KALSHI ARBITRAGE ENGINE — V19.0.1 PATCH REPORT
## Clean-Checkout Real Evidence Test Guard

**Patch Date:** 2026-10-25
**Target Version:** V19.0.1
**Base Release:** [`v19.0`](https://github.com/vikramaditya7z/engine-kal-t-1/releases/tag/v19.0) (commit `73afc04513019fae421e6f5d489c06daa103246f`)
**Branch:** `fix/v19-clean-checkout-test`
**Patch Author:** Autonomous Senior Quantitative Financial Infrastructure Auditor & Systems Engineer
**Status:** **VERIFIED & PREPARED (0 commits, 0 tags, 0 pushes made)**

---

## 1. Executive Summary

During the post-publication end-to-end verification of the Kalshi Arbitrage Research Engine release `v19.0` in a separate clean temporary checkout, 626 of 628 automated tests passed, 1 test skipped, and exactly 1 test failed:

```
FAILED tests/test_v13_basket_replay.py::test_replay_real_v12_trial_evidence_exact_matching
AssertionError: evidence_v12_fed_basket_trial/observations.jsonl must exist
```

This patch resolves the clean-checkout failure by adding an explicit availability check to `tests/test_v13_basket_replay.py:680`, gracefully skipping the test with a descriptive explanation when local raw research trial directories are absent, while preserving 100% of the substantive assertions whenever the trial directory is present.

---

## 2. Investigation & Confirmed Root Cause

### 2.1 The Failing Test
[`tests/test_v13_basket_replay.py`](file:///Users/vikramaditya/Disk/Projects/Kalshi/tests/test_v13_basket_replay.py#L674) defines `test_replay_real_v12_trial_evidence_exact_matching()`, which replays 50 recorded observations across 10 cycles from a bounded live-data trial collected during Milestone V12.

The test originally contained:
```python
evidence_path = Path("evidence_v12_fed_basket_trial/observations.jsonl")
manifest_path = Path("evidence_v12_fed_basket_trial/manifest.json")
config_path = Path("configs/baskets/fed_dec_2027_mece.json")

assert evidence_path.exists(), "evidence_v12_fed_basket_trial/observations.jsonl must exist"
assert manifest_path.exists(), "evidence_v12_fed_basket_trial/manifest.json must exist"
assert config_path.exists(), "configs/baskets/fed_dec_2027_mece.json must exist"
```

### 2.2 Why It Passed Locally in V19.0 but Failed in Clean Checkouts
1. In the authoring workspace (`/Users/vikramaditya/Disk/Projects/Kalshi`), the directory `evidence_v12_fed_basket_trial/` was present on disk from original local experiment runs.
2. During the V19 release execution, raw trial directories (`evidence_v12_*`, `evidence_v16_*`, `evidence_v17_*`, `evidence_v18_*`) were intentionally excluded from Git staging to avoid repository bloat and maintain strict release hygiene.
3. In `tests/test_v18_economics.py:262` and `tests/test_v19_release_gates.py:80`, existence guards were already implemented:
   ```python
   # tests/test_v18_economics.py
   v17_obs_path = Path("evidence_v17_broad_market_trial/observations.jsonl")
   if not v17_obs_path.exists():
       pytest.skip("V17 evidence file not present")
   ```
4. However, `tests/test_v13_basket_replay.py:680` lacked this existence check, asserting unconditional file existence.
5. Consequently, any clean clone of `v19.0` from GitHub produced 1 failure due to the absence of the uncommitted raw trial folder.

---

## 3. The Fix and Rationale

### 3.1 Code Modification
In [`tests/test_v13_basket_replay.py`](file:///Users/vikramaditya/Disk/Projects/Kalshi/tests/test_v13_basket_replay.py#L674):

```diff
--- a/tests/test_v13_basket_replay.py
+++ b/tests/test_v13_basket_replay.py
@@ -677,8 +677,9 @@ def test_replay_real_v12_trial_evidence_exact_matching():
     manifest_path = Path("evidence_v12_fed_basket_trial/manifest.json")
     config_path = Path("configs/baskets/fed_dec_2027_mece.json")

-    assert evidence_path.exists(), "evidence_v12_fed_basket_trial/observations.jsonl must exist"
-    assert manifest_path.exists(), "evidence_v12_fed_basket_trial/manifest.json must exist"
+    if not evidence_path.exists() or not manifest_path.exists():
+        pytest.skip("V12 trial evidence not present (run collection trial or provide evidence_v12_fed_basket_trial to execute real-evidence validation)")
+
     assert config_path.exists(), "configs/baskets/fed_dec_2027_mece.json must exist"

     # Verify original evidence hash first
```

### 3.2 Rationale
- **Smallest Correct Change:** Exactly 3 lines replaced 2 lines (+3/-2 lines). Zero changes were made to production library code (`kalshi_arbitrage/`).
- **Standardized Pattern:** Aligns `test_v13_basket_replay.py` with `test_v18_economics.py:262` and `test_v19_release_gates.py:80`.
- **Zero Dilution of Assertions:** When the evidence is present, 100% of substantive assertions execute, verifying SHA-256 integrity, 50 observations, 10 complete snapshots, negative gross edge (-2¢), net edge (-9¢), and timestamp skew (3.0s–5.0s).

---

## 4. Verification Results

### 4.1 Test Behavior Matrix

| Environment | Evidence Directory State | `test_replay_real_v12_trial_evidence_exact_matching` | Full Suite Result |
| :--- | :--- | :---: | :---: |
| **Local Workspace** (`/Users/vikramaditya/Disk/Projects/Kalshi`) | `evidence_v12_fed_basket_trial/` **PRESENT** | **PASSED** (0.12s) | **628 passed, 0 skipped** (15.05s) |
| **Isolated Clean Checkout** (`/tmp/kalshi_v19_fix_work`) | `evidence_v12_fed_basket_trial/` **ABSENT** | **SKIPPED** (0.16s) | **626 passed, 2 skipped** (15.54s) |
| **Temporary Evidence Test** (`/tmp/kalshi_v19_fix_work`) | Evidence copied in temporarily | **PASSED** (0.08s) | **627 passed, 1 skipped** |

### 4.2 Exact Substantive Assertions Preserved When Evidence is Present
When `evidence_v12_fed_basket_trial` is present, the test executes:
1. Manifest SHA-256 validation: `actual_hash == manifest["observations_sha256"]`.
2. Input inventory: `total_input_observations == 50`, `duplicate_observations_count == 0`.
3. Snapshot counts: `total_snapshots_reconstructed == 10`, `complete_snapshots_count == 10`.
4. Rejection attribution: `rejected_snapshots_count == 10`, `rejected_by_reason == {STATUS_REJECTED_NO_GROSS_EDGE: 10}`.
5. Profitability: `qualified_count == 0`.
6. Gross edge: `min == -2.0`, `max == -2.0`, `mean == -2.0`.
7. Net edge: `min == -9.0`, `max == -9.0`, `mean == -9.0`.
8. Skew: `min == 3.0`, `max == 5.0`, `mean == 4.0`, `p50 == 4.0`.

### 4.3 Git Whitespace Check
```bash
$ git diff --check
# Exit code 0 (0 whitespace errors)
```

---

## 5. Scope of Changed Files

Only 2 files are modified/added on branch `fix/v19-clean-checkout-test`:
1. [`tests/test_v13_basket_replay.py`](file:///Users/vikramaditya/Disk/Projects/Kalshi/tests/test_v13_basket_replay.py): +3 lines, -2 lines.
2. [`docs/V19_0_1_PATCH_REPORT.md`](file:///Users/vikramaditya/Disk/Projects/Kalshi/docs/V19_0_1_PATCH_REPORT.md): +150 lines (this patch report).

Zero changes to core logic, zero changes to financial calculations, and zero changes to read-only safety invariants.

---

## 6. Publication Readiness

The patch is completely verified and ready for publication:
- Clean checkout test suite passes with **0 failures** (626 passed, 2 skipped in 15.54s).
- Full workspace test suite passes with **0 failures** (628 passed in 15.05s).
- Strict non-negotiable safety rules adhered to: **Zero commits, zero tags, and zero pushes performed.**
