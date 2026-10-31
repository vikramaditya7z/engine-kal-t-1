# Kalshi Arbitrage Engine — Quantitative Principles & System Invariants

## 1. Purpose and Scope

The **Kalshi Arbitrage Engine** is a deterministic quantitative research, market microstructure observation, and paper-trading evaluation platform for Kalshi prediction markets. The system ingests public market data, models binary contract payoffs, evaluates theoretical and executable arbitrage, simulates multi-leg order execution against Level 2 depth, maintains an auditable double-entry paper-trading ledger, and supports historical backtesting without look-ahead bias.

All operations strictly respect the unauthenticated, read-only public market data boundary. The system contains zero capabilities, routes, credentials, or logic for real-money trading, order entry, exchange account management, or deposit/withdrawal interactions.

---

## 2. Quantitative Principles & Engineering Rules

1. **Use deterministic financial logic.**
   Trading, pricing, fee modeling, and arbitrage qualification decisions must be produced by explicit, testable mathematical calculations and deterministic rules. Large language models (LLMs) or heuristic AI systems may assist with documentation or development, but must never make trading decisions, generate orders, or alter authoritative financial calculations.

2. **Separate concerns strictly.**
   Keep public market data ingestion, normalized contract models, strategy detection, execution-cost pricing, execution simulation, risk limits, and accounting/ledger layers strictly separated. Strategy and pricing modules must consume normalized domain objects and must never depend directly on raw API responses.

3. **Financial correctness comes first.**
   A mathematically proven "no-trade" or "unprofitable after fees" verdict is infinitely preferable to an unsupported trade or superficial profit claim. Fail-closed gates must reject any candidate exhibiting uncertain settlement terms, leg skew, or insufficient depth.

4. **Test every financial boundary hermetically.**
   Enforce rigorous unit and property tests for mathematical invariants, complement payoffs, Level 2 depth traversals, official CFTC quadratic taker fee schedules, asynchronous multi-leg execution lifecycles, and double-entry ledger solvency invariants before relying on any component.

5. **Prove contractual relationships explicitly.**
   Similar names, ticker strings, topics, or expiries do not establish a valid financial relationship. Consult actual CFTC rule filings and legal contract specifications before classifying contracts as mutually exclusive and collectively exhaustive (MECE). Unproven or sub-stochastic partitions must remain unclassified.

6. **Qualify every opportunity against realistic market microstructure.**
   Never classify an opportunity as executable or profitable without fully modeling order-book depth, volume-weighted average price (VWAP), official CFTC quadratic taker fees, inter-leg observation skew, execution latency, and capital-at-risk bottleneck constraints.

7. **Prevent look-ahead bias.**
   Replay and backtests must use exclusively the information available at the simulated event timestamp. Event ordering, monotonic sequence IDs, and data provenance must be preserved; future quotes, fills, settlements, or revised catalogs must never leak into earlier evaluation frames.

8. **Use exact integer-cent monetary representations.**
   Authoritative monetary accounting must use exact representations: integer cents (`int` cents where \$1.00 = 100¢) for balances, cash reservations, and ledger journals, and exact `Decimal` with explicit quantization for fractional price points. Binary floating-point arithmetic is strictly prohibited for authoritative money, fees, balances, and P&L.

9. **Handle imperfect market data conservatively.**
   Missing, malformed, stale, contradictory, or delayed market data must trigger an explicit status code, candidate disqualification, or conservative failure. Never silently interpolate, fabricate, or substitute resting order-book depth.

10. **Maintain strict proportionality.**
    Keep the runtime architecture lightweight, modular, and dependency-free. The core quantitative engine runs entirely on the Python standard library with zero heavyweight database, message bus, or web framework dependencies.

11. **Follow the disciplined engineering workflow:**
    `Learn → Plan → Implement → Test → Verify → Document`.
    Every capability must be backed by reproducible test evidence and explicit architectural documentation.

12. **Protect credentials and environment boundaries.**
    Never hardcode API credentials, secret keys, or account identifiers. Use environment configuration and keep secret material out of source control. Maintain strict isolation between public market data streams and internal evaluation state.

---

## 3. Architecture & Research Synthesis

The project was executed across Milestones V0 through V25 and Phase 2F (Breadth-First Scanner), establishing:
- **Level 1 (Functional Discovery):** Automated broad-market catalog scanning across 168,000+ contracts with a 7-tier conservative MECE taxonomy.
- **Level 2 (Validated Qualification):** Exact integer-cent arithmetic, full Level 2 depth traversal, and official CFTC quadratic taker fee modeling.
- **Level 3 (Repeatable Historical Verification):** Empirical research across multiple economic series proving that liquid markets are kept strictly efficient by institutional market makers, while illiquid markets suffer from zero-bid liquidation traps or sub-stochastic contract terms.
- **Level 4 (Execution Architecture):** Deterministic asynchronous multi-leg paper execution simulation and double-entry ledger reconciliation.

For comprehensive architectural specifications and empirical findings, refer to:
- [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — System architecture, component manifests, and data flow.
- [`FINAL_ECONOMIC_VALIDATION_REPORT.md`](FINAL_ECONOMIC_VALIDATION_REPORT.md) — Exhaustive economic and microstructure evaluation of prediction market arbitrage.
- [`FINAL_PROJECT_COMPLETION_REPORT.md`](FINAL_PROJECT_COMPLETION_REPORT.md) — Complete engineering lifecycle report across all milestones.
