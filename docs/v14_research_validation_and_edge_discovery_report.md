# Kalshi Arbitrage Engine V14: Research Validation, Data Provenance & Edge Discovery

**Date:** 2026-10-24
**Milestone:** V14 Research Validation, Data Provenance & Edge Discovery
**Author:** Quantitative Systems Engineering & Audit Agent
**Baseline Commit:** `aae6dc5f764683aefaa678c4972af43f1cc89376` (Branch: `main`)
**Overall System Verdict:** **RESEARCH ENGINE VALIDATED & PROVENANCE SAFEGUARDS ENFORCED**
**Real-Market Opportunity Verdict:** **ZERO ARBITRAGE (BOOKMAKER OVER-ROUND OF 102¢–104¢ / NEGATIVE GROSS EDGE)**
**Production Capital Deployment Decision:** **STRICTLY PROHIBITED (RESEARCH-ONLY)**

---

## 1. Provenance Audit: V13 Ticker Discrepancy Resolution

### 1.1 The Investigation
The user prompt highlighted a discrepancy: the V12 trial report listed five tickers:
- `KXFEDDECISION-27DEC-C26` (Cut > 25 bps)
- `KXFEDDECISION-27DEC-C25` (Cut 25 bps)
- `KXFEDDECISION-27DEC-H0` (Maintain 0 bps)
- `KXFEDDECISION-27DEC-H25` (Hike 25 bps)
- `KXFEDDECISION-27DEC-H26` (Hike > 25 bps)

Whereas the V13 final summary message displayed:
- `KXFEDDECISION-27DEC-T3.25`
- `KXFEDDECISION-27DEC-T3.50`
- `KXFEDDECISION-27DEC-T3.75`
- `KXFEDDECISION-27DEC-T4.00`
- `KXFEDDECISION-27DEC-H4.00`

### 1.2 Root Cause Analysis
An exhaustive byte-level inspection of the repository confirmed:
1. **Raw Observations (`evidence_v12_fed_basket_trial/observations.jsonl`):** Contains exclusively `KXFEDDECISION-27DEC-C26`, `C25`, `H0`, `H25`, `H26`.
2. **Basket Configuration (`configs/baskets/fed_dec_2027_mece.json`):** Contains exclusively `KXFEDDECISION-27DEC-C26`, `C25`, `H0`, `H25`, `H26`.
3. **Persisted Replay Records (`data/v13_historical_replay_trial/replay_records.jsonl`):** Contains exclusively `KXFEDDECISION-27DEC-C26`, `C25`, `H0`, `H25`, `H26`.
4. **Persisted Replay Report (`data/v13_historical_replay_trial/replay_report.md`):** Contains no mention of `T3.25`.
5. **Codebase Search (`grep -rn "KXFEDDECISION-27DEC-T3.25"`):** Zero occurrences in any Python file, test file, or on-disk documentation.

**Root Cause:** The `T3.25` tickers were an errant transcription error in the LLM chat output table of the prior turn (confusing Fed rate strike levels from an earlier discovery query with Fed Decision rate change categories). The actual code, configuration, dataset, and persisted replay outputs were 100% consistent.

### 1.3 Resolution & Safeguards Implemented
To prevent a replay from ever evaluating a different basket or failing silently:
1. **Strict Basket Match Verification:** `HistoricalBasketReplayEngine.replay()` now validates that declared baskets have matching constituent observations in the dataset. If a dataset has zero constituent overlap with declared baskets, it raises `ValueError` immediately with detailed diagnostics.
2. **Event Ticker Cross-Contamination Guard:** `EventSnapshotReconstructor` verifies `obs.market.event_ticker == basket.event_ticker`, preventing cross-event absorption if two events share constituent market ticker substrings.
3. **Unmatched Basket Audit Trail:** Any basket lacking observations in a multi-basket session is tracked in `unmatched_baskets` and recorded in `data_quality_limitations`.
4. **Independent Reproduction:** Replayed `evidence_v12_fed_basket_trial/observations.jsonl` into a clean directory `data/v14_reproduced_fed_replay/` and confirmed byte-level manifest integrity.

---

## 2. Research Universe Validation

A rigorous research-universe configuration was established at [`configs/baskets/validated_mece_universe.json`](file:///Users/vikramaditya/Disk/Projects/Kalshi/configs/baskets/validated_mece_universe.json).

### 2.1 Included & Validated Events
| Basket ID | Event Ticker | Legs | Outcomes | Settlement & Exhaustiveness Source |
|---|---|---|---|---|
| `FED-DEC26-MECE` | `KXFEDDECISION-26DEC` | 5 | C26, C25, H0, H25, H26 | Official Kalshi FOMC meeting partition. Partitions entire real line ($\Delta r < -25, \Delta r = -25, \Delta r = 0, \Delta r = 25, \Delta r > 25$). Cancellation resolves H0 to YES. |
| `FED-DEC27-MECE` | `KXFEDDECISION-27DEC` | 5 | C26, C25, H0, H25, H26 | Official Kalshi FOMC meeting partition. Same 5-outcome partition for Dec 2027 meeting. |

### 2.2 Excluded Events & Justifications
1. **`KXFED-28JAN` (Series `KXFED`):** `mutually_exclusive = False` in API metadata. Strikes represent cumulative threshold contracts ($>3.25\%$, $>3.50\%$, etc.), which overlap rather than partition outcomes.
2. **`KXINX-26OCT30H1600` (Series `KXINX`):** 30 constituent legs make sequential REST polling latency skew excessive ($>15$ seconds skew per cycle).
3. **`KXNEXTNATOSECGEN-99` / `KXNEWPOPE-70` / `KXNEXTDNCCHAIR-45`:** Discrete person markets without a guaranteed residual ("Other / Field") contract. If an unlisted person is chosen, no contract settles to YES, violating collective exhaustiveness.

---

## 3. Read-Only Multi-Basket Evidence Collection

Executed a bounded read-only multi-basket collection session:
- **Output Directory:** [`evidence_v14_multi_basket_trial/`](file:///Users/vikramaditya/Disk/Projects/Kalshi/evidence_v14_multi_basket_trial/)
- **Session ID:** `v11-1792845267-7da106bf`
- **Configured Baskets:** `FED-DEC26-MECE`, `FED-DEC27-MECE` (10 unique constituent markets)
- **Bounds:** 5 cycles, 50 observations, 0 diagnostics, 0 rate limit violations.
- **Deduplication:** Polled each constituent market once per cycle without redundant requests.
- **SHA-256 Digest (`observations.jsonl`):** `4ae47259cabd2c2a3ab44bbef3625dc2bfc3e5f94d49bfe6e8d309bbfb686b81`

---

## 4. Multi-Basket Replay & Edge Discovery

Replayed the newly collected multi-basket evidence into [`data/v14_multi_basket_replay/`](file:///Users/vikramaditya/Disk/Projects/Kalshi/data/v14_multi_basket_replay/):

```bash
./.venv/bin/python scripts/evaluate_v10.py \
  --file evidence_v14_multi_basket_trial/observations.jsonl \
  --basket-file configs/baskets/validated_mece_universe.json \
  --basket-output-dir data/v14_multi_basket_replay \
  --max-leg-skew 5.0
```

### 4.1 Canonical Opportunity Classifications (7 Categories)
| Classification Category | Count | Status / Definition |
|---|---|---|
| 1. Structurally Invalid / Unverified Basket | 0 | Failed MECE structure or universe check |
| 2. Incomplete / Insufficient Synchronization | 0 | Missing leg, error, or timestamp skew |
| 3. No Executable Depth | 0 | Zero fillable contracts across legs |
| 4. Negative or Zero Gross Edge | **10** | **Gross acquisition cost $\ge$ payout** |
| 5. Positive Gross Edge, Unprofitable After Fees | 0 | Gross edge eroded by CFTC taker fees |
| 6. Positive Net Edge, Insufficient Common Qty | 0 | Fillable quantity < target quantity |
| **7. Qualified Research Candidates** | **0** | **Fully executable, synchronized, fee-adjusted edge** |

### 4.2 Market State Deduplication & Quote Persistence
- **Reconstructed Complete Snapshots:** 10
- **Unique Market States (Independent Book Quotes):** 2 (1 for DEC26, 1 for DEC27)
- **Repeated Market States (Unchanged Resting Quotes):** 8
- **Conclusion:** Resting liquidity did not change across the 5 observation cycles. Repeated snapshots reflect the same market state rather than independent opportunities.

### 4.3 Quantitative Price & Edge Breakdown
| Event | Best YES Asks Across 5 Legs | Total Cost | Guaranteed Payout | Gross Edge | CFTC Fees | Net Edge |
|---|---|---|---|---|---|---|
| `KXFEDDECISION-26DEC` | 1¢ + 2¢ + 23¢ + 73¢ + 5¢ | 104¢ | 100¢ | **-4¢** | 7¢ | **-11¢** |
| `KXFEDDECISION-27DEC` | 1¢ + 19¢ + 63¢ + 16¢ + 3¢ | 102¢ | 100¢ | **-2¢** | 7¢ | **-9¢** |

In both events, market makers maintain an over-round (positive ask margin above 100¢), guaranteeing that an arbitrageur buying the YES basket loses money on every fill before and after fees.

---

## 5. Statistical & Robustness Sensitivity

1. **Timestamp Skew Distribution:**
   - All snapshots exhibited exactly 4.0s skew across 5 sequential REST requests (~0.8s per request).
   - Skew $\le 1.0\text{s}$: 0%
   - Skew $\le 3.0\text{s}$: 0%
   - Skew $\le 5.0\text{s}$: 100%
2. **Fee Sensitivity:**
   - Even if CFTC fees were completely waived ($0\text{¢}$ fee schedule), gross edges remain strictly negative (-4¢ for DEC26, -2¢ for DEC27). The strategy is blocked by gross pricing, not merely fees.
3. **Depth Feasibility:**
   - Common depth on the YES basket is constrained by the most liquid middle strikes (DEC26 bottleneck: H25 at 9 contracts; DEC27 bottleneck: C25 at 25 contracts).
4. **NO Basket Asymmetry:**
   - Buying NO contracts across all 5 legs requires a payout of 400¢ ($(5 - 1) \times 100¢$). However, deep out-of-the-money legs (e.g., C26) have 0 YES bids, which means NO asks have 0 executable depth. The NO basket fails with `NO_EXECUTABLE_DEPTH`.

---

## 6. Safety & Test Suite Verification

- **Total Passing Tests:** **569 passed** (0 failed, 0 errors, in 15.42s).
- **V14 New Tests ([`tests/test_v14_research_validation.py`](file:///Users/vikramaditya/Disk/Projects/Kalshi/tests/test_v14_research_validation.py)):** 11 tests covering unmatched basket errors, event ticker isolation, market state fingerprinting, 7 canonical classifications, YES/NO payout exactness, CFTC fee exactness, multi-basket deduplication, manifest byte SHA-256 verification, and AST safety verification prohibiting live order execution calls.
- **Safety Boundary:** Strict read-only isolation maintained. Zero order placement or portfolio mutation.
