==============================================================================
KALSHI ARBITRAGE ENGINE — V10 HISTORICAL STRATEGY VALIDATION REPORT
==============================================================================
Evaluated At:                  2026-10-12T12:11:53.141106+00:00
Dataset Source:                data/v5_trial/observations.jsonl
Dataset SHA256:                5bef3c34d07a941f...d8e60308
Executive Research Verdict:    INCONCLUSIVE
Verdict Rationale:             Across 21 observations (6 populated books), zero gross parity or basket mispricings were detected. Real prediction market pricing was efficient or order books were uncrossed during the observation period.

1. EVIDENCE INVENTORY & CADENCE QUALITY:
  Total Raw Records:           21
  Valid Observations:          21
  Malformed / Corrupted:       0
  Duplicate Record IDs:        0
  Out-of-Order Timestamps:     0
  Stale Records (> 60s):      0
  Empty Order Books:           15
  Populated Order Books:       6
  Unique Tickers Ingested:     7
  Observation Span:            2026-10-09T14:42:09.248672+00:00 -> 2026-10-09T15:01:02.584569+00:00
  Duration Span:               1133.3 seconds

2. MULTI-STAGE OPPORTUNITY FUNNEL:
  1. Observations Ingested:    21
  2. Valid Snapshots:          21
  3. Markets Evaluated:        21
  4. Gross Arbitrage Detected: 0
     - Rejected (No Edge):     21
     - Rejected (CFTC Fees):   0
     - Rejected (No Depth):    0
     - Rejected (Illiquid):    0
     - Rejected (Stale Data):  0
     - Rejected (Risk Limits): 0
  5. Approved for Execution:   0
  6. Execution Attempts:       0
  7. Fully Filled Trades:      0
  8. Partially Filled Trades:  0
  9. Legged-Out (Unwound):     0
 10. Completed Arbitrage:      0 (0.00% conversion)

3. FINANCIAL RESULTS & PORTFOLIO VALUATION:
  Initial Simulated Capital:   $10,000.00
  Ending Available Cash:       $10,000.00
  Position Cost Basis:         $0.00
  Marked Portfolio Value:      $0.00 (MARK_TO_MARKET_BID)
  Realized Gross Edge:         +0¢ ($+0.00)
  Exchange Fees Paid (CFTC):   0¢ ($0.00)
  Scratch / Unwind Drag:       +0¢ ($+0.00)
  Net Realized P&L:            +0¢ ($+0.00)
  Unrealized P&L:              +0¢ ($+0.00)
  Total Economic Return:       +0¢ ($+0.00)
  Max Drawdown:                0¢ (0.00%)
  Capital Utilization:         0.00%
  Total Trades Executed:       0
  Win / Loss / Scratch:        0 / 0 / 0
  Double-Entry Ledger Audit:   PASS (100% Invariant Match)

4. EXECUTION QUALITY & MICROSTRUCTURE IMPACT:
  Target vs Filled Contracts:  0 / 0 (0.00%)
  Full-Fill Rate:              0.00%
  Partial-Fill Rate:           0.00%
  Unwind / Scratch Rate:       0.00%
  Avg Slippage / Contract:     0.00¢
  Fees as % of Gross Edge:     0.00%
  Residual Unhedged Contracts: 0

5. HIERARCHICAL COMPARATIVE BASELINES:
  Tier Name               Opportunities   Trades   Gross P&L    Fees Paid   Net P&L    Comp %
  ---------------------   -------------   ------   ---------    ---------   -------    ------
  Tier 0: No-Trade                    0        0         +0¢          0¢       +0¢     0.0%
  Tier 1: Gross Edge                  0        0         +0¢          0¢       +0¢     0.0%
  Tier 2: Net Top-of-Book             0        0         +0¢          0¢       +0¢     0.0%
  Tier 3: Depth Executable             0        0         +0¢          0¢       +0¢     0.0%
  Tier 4: Realistic Simulation             0        0         +0¢          0¢       +0¢     0.0%
  Tier 5: Stress Scenario             0        0         +0¢          0¢       +0¢     0.0%

6. CHRONOLOGICAL OUT-OF-SAMPLE VALIDATION:
  Split Viable:                False
  Insufficiency Note:          Dataset contains 21 observations (< 30 minimum threshold); insufficient volume for statistically valid out-of-sample partitioning.

7. SENSITIVITY & STRESS ANALYSIS MATRIX:
  Latency  Haircut  Fee Model       Attempted  Completed  Leg-Out  Net P&L   Scratch Drag
  -------  -------  --------------  ---------  ---------  -------  -------   ------------
      0ms     1.00  CFTC_TAKER              0          0        0      +0¢            +0¢
      0ms     0.75  CFTC_TAKER              0          0        0      +0¢            +0¢
      0ms     0.50  CFTC_TAKER              0          0        0      +0¢            +0¢
      0ms     0.25  CFTC_TAKER              0          0        0      +0¢            +0¢
     25ms     1.00  CFTC_TAKER              0          0        0      +0¢            +0¢
     25ms     0.75  CFTC_TAKER              0          0        0      +0¢            +0¢
     25ms     0.50  CFTC_TAKER              0          0        0      +0¢            +0¢
     25ms     0.25  CFTC_TAKER              0          0        0      +0¢            +0¢
     50ms     1.00  CFTC_TAKER              0          0        0      +0¢            +0¢
     50ms     0.75  CFTC_TAKER              0          0        0      +0¢            +0¢
     50ms     0.50  CFTC_TAKER              0          0        0      +0¢            +0¢
     50ms     0.25  CFTC_TAKER              0          0        0      +0¢            +0¢
    100ms     1.00  CFTC_TAKER              0          0        0      +0¢            +0¢
    100ms     0.75  CFTC_TAKER              0          0        0      +0¢            +0¢
    100ms     0.50  CFTC_TAKER              0          0        0      +0¢            +0¢
    100ms     0.25  CFTC_TAKER              0          0        0      +0¢            +0¢
    250ms     1.00  CFTC_TAKER              0          0        0      +0¢            +0¢
    250ms     0.75  CFTC_TAKER              0          0        0      +0¢            +0¢
    250ms     0.50  CFTC_TAKER              0          0        0      +0¢            +0¢
    250ms     0.25  CFTC_TAKER              0          0        0      +0¢            +0¢
    500ms     1.00  CFTC_TAKER              0          0        0      +0¢            +0¢
    500ms     0.75  CFTC_TAKER              0          0        0      +0¢            +0¢
    500ms     0.50  CFTC_TAKER              0          0        0      +0¢            +0¢
    500ms     0.25  CFTC_TAKER              0          0        0      +0¢            +0¢
==============================================================================
