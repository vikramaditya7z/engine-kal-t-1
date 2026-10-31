# Stage 3 — Strategy Research & Candidate Quality Improvement Report

**Date:** October 30, 2026  
**System:** Kalshi Breadth-First Arbitrage Engine  
**Repository:** `/Users/vikramaditya/Disk/Projects/Kalshi`  
**Git Branch:** `fix/v19-clean-checkout-test`  
**Author:** Antigravity Autonomous Audit Agent  

---

## 1. Executive Summary

This research report investigates the mathematical validity, fee economics, market structure constraints, and qualification behavior of arbitrage strategies executable on the Kalshi prediction exchange.

Grounding this analysis in the empirical results from **Trial 2** (`data/breadth_scanner/trial_short_15min_20261030/`), we analyze:
1. **Binary Market Parity (Buy-side & Sell-side).**
2. **Crossed Top-of-Book Pricing.**
3. **MECE Basket Construction (Long YES and Long NO).**
4. **The Theoretical Minimum Taker Fee Constraint:** Why multi-leg baskets require a minimum gross edge threshold of at least $N \times \$0.01$ to survive exchange fees.
5. **Event Pre-Filter Decoupling:** Re-aligning the event pre-filter to evaluate price viability without misrepresenting database record modification timestamps as quote staleness.

---

## 2. In-Depth Strategy Investigation

### 2.1 Strategy A: Binary Market Parity (Single Market Complement)

#### A. Buy-Side Complementary Arbitrage (Long Parity)
- **Mathematical Invariant:**
  In a binary prediction contract, outcome YES and outcome NO are mutually exclusive and collectively exhaustive. Exactly one contract settles to \$1.00 (100¢) and the other settles to \$0.00:
  $$\text{Payout} \equiv \$1.0000$$
  If a trader concurrently buys 1 YES contract at $\text{YES ask}$ and 1 NO contract at $\text{NO ask}$:
  $$\text{Total Cost} = \text{YES ask} + \text{NO ask}$$
  $$\text{Gross Edge} = \$1.0000 - (\text{YES ask} + \text{NO ask})$$

- **CFTC-Regulated Taker Fee Schedule:**
  Kalshi charges taker fees on each executed leg according to the quadratic formula:
  $$\text{Fee} = \left\lceil N \times 0.07 \times P \times (1 - P) \right\rceil$$
  where $P$ is the contract price in dollars and rounding is ceiled to the nearest integer cent.
  For $N = 1$ contract:
  - If $P = 0.50$: $\text{Fee} = \lceil 1 \times 0.07 \times 0.25 \rceil = \lceil 0.0175 \rceil = \$0.02$.
  - If $P = 0.10$ or $0.90$: $\text{Fee} = \lceil 1 \times 0.07 \times 0.09 \rceil = \lceil 0.0063 \rceil = \$0.01$.
  - For any price $0.01 \le P \le 0.99$, the fee per contract is strictly either **1¢ or 2¢**.
  
- **Economic Viability Threshold:**
  To achieve positive net profit after fees:
  $$\text{Total Fees} = \text{Fee}_{\text{YES}} + \text{Fee}_{\text{NO}} \ge \$0.02 \text{ to } \$0.04$$
  To satisfy the scanner's configured minimum net profit threshold ($\text{Net Profit} \ge \$0.05$):
  $$\text{Gross Edge} \ge \text{Total Fees} + \$0.05 \ge \$0.07 \text{ to } \$0.09$$
  $$\implies \text{YES ask} + \text{NO ask} \le \$0.9100 \text{ to } \$0.9300$$
  **Key Takeaway:** An apparent gross edge of 1¢ to 4¢ on a binary contract is economically unviable and cannot yield net profit.

#### B. Sell-Side Complementary Arbitrage (Short Parity)
- **Mathematical Invariant:**
  $$\text{Bids Sum} = \text{YES bid} + \text{NO bid} > \$1.0000$$
  Selling 1 YES and 1 NO concurrently generates immediate cash equal to $\text{YES bid} + \text{NO bid}$. At settlement, paying the winning contract costs exactly \$1.00.
  Gross edge: $(\text{YES bid} + \text{NO bid}) - \$1.0000$.
- **Capital & Collateral Requirements:**
  On Kalshi, selling contracts short without holding an existing long position requires posting maximum payout collateral (\$1.00 per share) held in margin until settlement.
  Return on Capital (ROC) is bounded by:
  $$\text{ROC} = \frac{\text{Net Edge}}{\$1.00 - \text{Proceeds}}$$

---

### 2.2 Strategy B: Crossed Top-of-Book Pricing

- **Conditions Evaluated:**
  1. $\text{YES bid} > \text{YES ask}$ (Inverted YES spread).
  2. $\text{NO bid} > \text{NO ask}$ (Inverted NO spread).
  3. $\text{YES bid} + \text{NO bid} > \$1.00$ (Inverted complement).
- **Exchange Matching Invariants:**
  Kalshi's internal CLOB (Central Limit Order Book) enforces continuous price-time matching. Under CFTC DCM regulations, incoming bids $\ge$ lowest resting ask match immediately.
  Therefore, resting crossed quotes observed in REST snapshots represent either:
  1. Transient quotes captured during microsecond matching queue transitions.
  2. Latency artifacts where an order cancellation or fill occurred on one leg before the snapshot finished streaming.
  Stage C order-book qualification safely rejects these artifacts if the crossing has dissipated by the time the targeted L2 snapshot arrives.

---

### 2.3 Strategy C: MECE Basket Arbitrage (Mutually Exclusive, Collectively Exhaustive)

#### A. Long YES Basket Construction
For an event with $N$ exhaustive outcomes $E_1, E_2, \dots, E_N$ where exactly one outcome must resolve YES:
$$\text{Cost} = \sum_{i=1}^N \text{YES ask}_i < \$1.0000$$
$$\text{Guaranteed Payout} = \$1.0000$$
$$\text{Gross Edge} = \$1.0000 - \sum_{i=1}^N \text{YES ask}_i$$

#### B. Long NO Basket Construction
When $\sum_{i=1}^N \text{NO ask}_i < \$(N - 1) \times \$1.0000$:
$$\text{Guaranteed Payout} = \$(N - 1) \times \$1.0000$$
$$\text{Gross Edge} = (N - 1) - \sum_{i=1}^N \text{NO ask}_i$$

#### C. The Mathematical Minimum Fee Lower Bound for Multi-Leg Baskets
Every non-exempt Kalshi contract execution incurs at least \$0.01 in taker fees due to integer cent ceiling rounding.
For an $N$-leg basket, the **absolute minimum taker fee** is:
$$\text{Fee}_{\text{min}}(N) = N \times \$0.01$$
For typical contract prices, average taker fees range from **1.2¢ to 1.8¢ per leg**.

#### Empirical Proof from Trial 2 Data:
Evaluating the 10 candidates discovered in Trial 2 against the minimum fee formula:

| Candidate ID | Legs ($N$) | Gross Edge | Min Fee ($N \times 1¢$) | Max Possible Net Edge | Economic Viability |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `KXBILLBOARDRUNNERUPALBUM` | 15 | **80¢** | 15¢ | **+65¢** | Viable (Rejected by Size Gate: $15 > 8$) |
| `KXTOPALBUM` | 15 | **25¢** | 15¢ | **+10¢** | Viable (Rejected by Size Gate: $15 > 8$) |
| `KXMODELHIGH-27-1550` | 8 | **25¢** | 8¢ | **+17¢** | **Genuinely Viable ($\ge 5¢$ net)!** |
| `KXNFLRACE-KCDEN` | 3 | 5¢ | 3¢ | +2¢ | Unviable ($< 5¢$ min profit) |
| `KXNFLRACE-CLEPIT` | 3 | 4¢ | 3¢ | +1¢ | Unviable ($< 5¢$ min profit) |
| `KXNFLRACE-PHIWAS` | 3 | 2¢ | 3¢ | -1¢ (Net Loss) | Unviable (Mathematical loss) |
| `KXFEDDECISION` | 5 | 2¢ | 5¢ | -3¢ (Net Loss) | Unviable (Mathematical loss) |
| `KXNEXTSTATE` | 5 | 1.5¢ | 5¢ | -3.5¢ (Net Loss) | Unviable (Mathematical loss) |
| `KXUKPARTY` | 8 | 1.4¢ | 8¢ | -6.6¢ (Net Loss) | Unviable (Mathematical loss) |
| `KXNFL1H-KCDEN` | 3 | 1.0¢ | 3¢ | -2.0¢ (Net Loss) | Unviable (Mathematical loss) |

**Conclusion:** 7 out of the 10 preliminary candidates identified in Trial 2 were mathematically guaranteed to produce net losses or sub-threshold profits once taker fees were accounted for.

Only **`KXMODELHIGH-27-1550`** (8 legs, 25¢ gross edge) had sufficient edge density ($3.125¢/leg$) to clear both the $N \times 1¢$ fee lower bound and the 5¢ minimum profit threshold.

---

## 3. Recommended Pipeline Improvements

### Recommendation 1: Upstream Fee-Aware Filtering in Stage B
To prevent wasting Stage C priority queue capacity on baskets that are mathematically incapable of yielding profit:
In `BreadthArbitrageScreener.screen_event_mece_baskets`:
$$\text{Require: } \text{Gross Edge} > (N \times \$0.01) + \text{min\_net\_profit\_dollars}$$
For an 8-leg basket with a 5¢ net profit threshold, require $\text{Gross Edge} > 8¢ + 5¢ = 13¢$. Candidates with gross edges below this threshold can be classified immediately as `NO_CANDIDATE: Gross edge insufficient to cover minimum taker fees`.

### Recommendation 2: Event Pre-Filter Decoupling
In `TargetedOrderBookQualifier._execute_event_prefilter`:
The event pre-filter's primary responsibility is verifying that the refreshed top-of-book prices STILL offer a gross edge before deep order-book calls are dispatched.
Database modification timestamps (`m.updated_time`) should not be checked against `max_stale_seconds` in the pre-filter. Quote freshness of resting orders must be checked strictly against `book.source_timestamp` (the HTTP response `Date` header) in Stage C.
