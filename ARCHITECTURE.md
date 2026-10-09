# Kalshi Arbitrage Engine — Architecture

## Objective and scope

The long-term system is a deterministic quantitative research and paper-trading engine for Kalshi prediction markets. It will ingest market observations, represent contracts and settlement payoffs, detect only logically established arbitrage relationships, evaluate executable profitability after costs and liquidity constraints, simulate fills, maintain a paper-trading ledger, and support historical replay and backtesting. No real-money execution is in scope for the initial versions.

This document describes the intended direction. **Today, this repository contains only this architecture document and `PROJECT_RULES.md`; no application component or roadmap milestone has been implemented.**

## Roadmap

- **V0 — Foundation:** Understand binary contracts and implement the basic contract/payoff model.
- **V1 — Market Data:** Connect to Kalshi’s API and normalize real market data.
- **V2 — Arbitrage Detection:** Detect mathematically valid opportunities between appropriately related contracts.
- **V3 — Execution Pricing:** Account for order-book depth, fees, liquidity, and execution costs.
- **V4 — Paper Trading:** Simulate orders and maintain an auditable paper-trading ledger.
- **V5 — Historical Replay:** Replay historical or recorded market events chronologically and evaluate strategy performance without look-ahead bias.
- **V6 — Live Paper Trading:** Run against live market data with simulated execution.
- **V7+ — Advanced Research:** Explore payoff optimization, linear programming, additional contract relationships, temporal opportunities, market making, and optional live execution.

The versions extend one codebase. Each version should deliver only the capabilities needed for that milestone.

## Intended high-level data flow

`Kalshi data → normalized market state → contract/payoff model → arbitrage detection → execution-cost evaluation → risk checks → paper execution → ledger/P&L → replay and analytics`

The strategy layer consumes stable, normalized domain objects—not raw REST or WebSocket response objects. Adapters may change as API details are learned, while domain and strategy logic remain insulated from transport-specific schemas.

## Eventual component responsibilities and boundaries

- **Data ingestion:** Obtain REST snapshots and, when appropriate, WebSocket events; validate transport payloads, timestamps, and provenance. It does not decide whether a trade exists.
- **Normalization:** Convert provider-specific payloads into a stable market-state representation, including quote/order-book observations and data quality/status. It does not infer settlement relationships.
- **Contract and payoff model:** Represent contract terms, settlement conditions, binary outcomes, positions, and payoff vectors. It is the authority for logical payoff calculations.
- **Arbitrage detection:** Compare appropriately related contracts or portfolios using the payoff model and identify mathematically valid candidate opportunities. It does not assume executability, place orders, or book accounting entries.
- **Execution-cost evaluation:** Apply observed depth, fees, slippage/fill assumptions, liquidity limits, and uncertainty to estimate executable outcomes. It must expose assumptions and distinguish theoretical from executable results.
- **Risk checks:** Enforce configurable exposure, concentration, capital, data-quality, staleness, and operational constraints. It approves or rejects simulated actions; it does not redefine settlement payoffs.
- **Paper execution:** Simulate order submission, matching, partial fills, cancellations, latency, and rejected/uncertain execution according to an explicit model. It does not calculate strategy signals or silently rewrite fills.
- **Ledger and P&L:** Record an auditable sequence of orders, fills, fees, cash/position movements, settlements, and realized/unrealized P&L using exact monetary accounting. It is separate from signal generation.
- **Replay and analytics:** Feed recorded events chronologically through the same relevant interfaces, enforce event-time information boundaries, and report performance and diagnostics. It must not provide future data to the strategy.

Strategy detection, execution simulation, and accounting are separate responsibilities even when they initially live in a small number of modules.

## Technology direction

Use Python as the primary implementation language, `pytest` for tests, and Kalshi REST/WebSocket APIs when the relevant milestone requires them. Use a suitable local storage format for recorded market data and replay inputs; select the format and other dependencies only when needed for the active milestone. API details, authentication, rate limits, and fee rules must be verified from authoritative current documentation before implementation; this architecture does not claim that they have been verified.

## Key correctness risks

- Settlement semantics may make apparently related markets logically unrelated.
- Fees may vary by action or product and can eliminate a theoretical edge.
- Displayed liquidity may not be fully executable; depth, slippage, latency, and partial fills matter.
- Stale, delayed, missing, or inconsistent quotes can invalidate a signal.
- Rejected, uncertain, and partially filled orders must be represented explicitly.
- Position, cash, fee, settlement, and P&L accounting must remain internally consistent.
- Historical replay must preserve event chronology and prevent look-ahead bias, including leakage from later corrections or settlements.
- Data normalization must preserve enough timestamp and provenance information to audit decisions.

## Present versus planned

The architecture above is a target boundary map, not a claim that these components exist. The current repository intentionally contains only project guidance. Implementation begins at V0 and should add the smallest tested slice required by that milestone; API connectivity, live data, execution simulation, ledgering, replay, and advanced research remain planned until their roadmap versions are reached.

