# Kalshi Arbitrage Engine — Architecture

## Objective and scope

The long-term system is a deterministic quantitative research and paper-trading engine for Kalshi prediction markets. It will ingest market observations, represent contracts and settlement payoffs, detect only logically established arbitrage relationships, evaluate executable profitability after costs and liquidity constraints, simulate fills, maintain a paper-trading ledger, and support historical replay and backtesting. No real-money execution is in scope for the initial versions.

This document describes both the intended direction and the currently implemented V0/V1/V2 boundary. V0, V1, and V2 are implemented; later roadmap capabilities remain planned.

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

Today, the implemented flow reaches candidate arbitrage detection:

`public REST response → transport/error handling → validation and normalization → normalized market or event model → contract/portfolio payoff model → arbitrage detection`

The V2 arbitrage detection engine bridges normalized market data to the deterministic V0 contract and portfolio payoff models. It does not perform execution-cost evaluation, paper execution, or live order placement.

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

Python is the implementation language and `pytest` is used for offline tests. The current V1 REST client uses the Python standard library and exposes read-only GET access to production or demo Kalshi REST endpoints for market listing, a single market, and a single event. It has finite timeouts, explicit HTTP/transport/JSON/envelope errors, and cursor-loop protection. It has no authentication, retry policy, WebSocket support, or order placement.

Normalized API prices and quantities use `Decimal` without float parsing or silent rounding. V0 settlement payouts remain integer cents. A recorded real market response supports deterministic offline normalization tests; the smoke script makes one public market request and refreshes that fixture only after successful normalization. Storage for historical replay remains a later decision.

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

### Implemented today

- **V0 contract/payoff model:** `contract.py` models deterministic binary settlement and gross P/L using integer cents. `portfolio.py` evaluates finite-outcome event portfolios only when the mutually exclusive and collectively exhaustive relationship is explicitly declared.
- **V1 normalized market-data model:** `market_data.py` validates binary market and event metadata, timestamps, exact `Decimal` prices and quantities, and order-book levels without allowing raw API dictionaries into future strategy code. Unknown lifecycle statuses are preserved as raw status strings rather than treated as open. Event `mutually_exclusive` metadata is preserved, but collective exhaustiveness and cross-market settlement relationships are never inferred.
- **V1 public REST boundary:** `rest_client.py` supplies a small GET-only client for listing markets with explicit cursor pagination and for fetching one market or event. Each payload is validated and normalized before it is returned. Malformed data is surfaced as an error rather than silently discarded.
- **V2 arbitrage detection engine:** `arbitrage.py` evaluates mathematically valid candidate opportunities for single-market binary complement parity and explicitly established MECE event baskets. Exact price conversions ensure no precision loss between `Decimal` dollars and integer cents. Opportunities report worst-case guaranteed payouts across all states, total costs, and gross edge, explicitly qualifying that executability and net profitability are unverified. Relationships are never inferred from market titles or tickers.
- **Verification assets:** pytest coverage includes V0 settlement behavior, V1 market-data validation, REST fake-transport behavior, a captured public market fixture, and V2 deterministic arbitrage detection across valid, break-even, losing, and malformed candidates.

### Still planned

WebSockets, live order-book retrieval, authentication, order placement, execution-cost pricing, risk controls, paper execution, ledgering, historical replay, and advanced research are not implemented. Implementation should continue to add only the smallest tested capability required by the active roadmap milestone.

