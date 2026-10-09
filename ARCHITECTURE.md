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

Today, the implemented flow spans the complete paper-trading pipeline:

`public REST response → transport/error handling → validation and normalization → normalized market, event, or order-book model → contract/portfolio payoff model → arbitrage detection → execution pricing & depth evaluation → pre-trade risk checks → paper execution simulation → portfolio ledger & accounting → persistence & historical replay`

The V2 arbitrage detection engine bridges normalized market data to the deterministic V0 contract and portfolio payoff models. The V3 execution pricing layer evaluates those candidates against observable order-book depth and explicit fees. The V4 paper-trading engine simulates execution, enforces risk limits, maintains an append-only cash and position ledger, persists state with automated reconciliation, and supports historical replay. No real-money orders, live execution credentials, or live order-routing are implemented.

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

Python is the implementation language and `pytest` is used for offline tests. The current REST client uses the Python standard library and exposes read-only GET access to production or demo Kalshi REST endpoints for market listing, a single market, a single event, and an order book. It has finite timeouts, explicit HTTP/transport/JSON/envelope errors, and cursor-loop protection. It has no authentication, retry policy, WebSocket support, or order placement.

Normalized API prices and quantities use `Decimal` without float parsing or silent rounding. V0 settlement payouts and V4 ledger accounting remain exact integer cents. A recorded real market response supports deterministic offline normalization tests; the smoke script makes one public market request and refreshes that fixture only after successful normalization. Storage for historical replay and persistence uses atomic file-based JSON with versioned schemas.

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
- **V1 public REST boundary:** `rest_client.py` supplies a small GET-only client for listing markets with explicit cursor pagination and for fetching one market, event, or order book (`get_order_book`). Each payload is validated and normalized before it is returned. Malformed data is surfaced as an error rather than silently discarded.
- **V2 arbitrage detection engine:** `arbitrage.py` evaluates mathematically valid candidate opportunities for single-market binary complement parity and explicitly established MECE event baskets. Exact price conversions ensure no precision loss between `Decimal` dollars and integer cents. Opportunities report worst-case guaranteed payouts across all states, total costs, and gross edge, explicitly qualifying that executability and net profitability are unverified. Relationships are never inferred from market titles or tickers.
- **V3 execution-pricing layer:** `execution.py` evaluates candidate opportunities against actual order-book depth, supported quantities, partial fills, slippage, and explicit caller-supplied fees. It derives complementary ask levels ($P_{\text{ask}} = 100 - P_{\text{bid}}$) strictly from resting counter-party bids according to Kalshi's binary contract settlement invariants. Depth traversal walks price levels in price priority order to compute exact volume-weighted total acquisition costs in integer cents. Evaluates binary parity (`evaluate_binary_parity_execution`), MECE event baskets (`evaluate_mece_basket_execution`), and general portfolios (`evaluate_portfolio_execution`). Multi-leg evaluation deterministically identifies common bottleneck quantities across legs, re-traversing depth at the common supported scale so partial fills reflect true available depth without leg desynchronization.
- **V4 paper trading, portfolio ledger & simulation engine:**
  - `paper_trade.py`: Explicit lifecycle state machine (`PROPOSED`, `ACCEPTED`, `REJECTED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`), multi-leg trade models, and transition validation.
  - `paper_executor.py`: Paper execution simulator that transforms V3 liquidity evaluation into concrete paper trade fills and partial fills.
  - `ledger.py`: Append-only financial ledger with cash reservations, position inventory tracking, trading fees, settlement methods (`settle_binary_market`, `settle_binary_contract`, `settle_position`), book-value equity (`book_value_equity_cents`), and independent audit reconciliation (`reconcile`). Positions are keyed by `(contract_id, side)`; single-market strategies default `contract_id` to `market_ticker`, while multi-contract portfolios use explicit contract identifiers.
  - `risk.py`: Pre-trade risk controls (`RiskConfig`, `RiskManager`) evaluating capital, per-trade cost, position size, aggregate exposure, and edge limits before simulated acceptance.
  - `persistence.py`: Atomic state persistence with schema versioning (`1.0`) and automated crash recovery with fail-closed ledger reconciliation and trade-to-ledger cross-validation.
  - `replay.py`: Deterministic historical chronological replay engine (`ReplayEngine`) calculating book-value equity curves (`cash + cost_basis`), maximum drawdowns, and audit reports without look-ahead bias.
- **V5 live market observer & opportunity evaluation:**
  - `observer.py`: Continuous read-only public market observer (`MarketObserver`, `MarketObserverConfig`, `MarketObservation`, `EvaluatedOpportunity`) connecting public REST polling to V2 detection and V3 execution pricing. Enforces explicit fail-closed freshness policies using transport-level HTTP `Date` response headers from order-book GET responses (rejecting books with missing, malformed, clock-drifted $>10$s in the future, or stale $>60$s headers, with the documented limitation that HTTP `Date` establishes response transmission time rather than matching-engine snapshot generation or upstream cache bypass), validates multi-leg snapshot consistency (rejecting combinations exceeding leg timestamp skew tolerances), and provides opportunity-level deduplication to prevent duplicate paper trade submissions across poll cycles.
  - `evidence_storage.py`: Crash-resilient, schema-versioned (`1.0`) file storage (`EvidenceStore`) persisting timestamped observations and evaluated opportunities with atomic writes and offline replay capability (`replay_recorded_evidence`).
  - `metrics.py`: Structured metrics (`ObserverMetrics`) and auditable evaluation reporting (`ObserverReport`) tracking API reliability, rejections by stage, edge and depth distributions, and observation-based opportunity lifetimes.
  - `scripts/run_observer.py`: Read-only CLI entry point with clean signal handling and summary report generation.
- **Verification assets:** pytest coverage includes V0 settlement behavior, V1 market-data validation, REST fake-transport behavior, a captured public market fixture, V2 deterministic arbitrage detection, V3 execution pricing across depth traversal, V4 complete paper-trading lifecycle, ledger, risk, persistence, and replay suites, and V5 continuous observation, evidence persistence, metrics, and end-to-end integration suites.

### Still planned

WebSockets, streaming market data, live paper trading against continuous WebSocket feeds, order placement, and advanced research are not implemented. Implementation should continue to add only the smallest tested capability required by the active roadmap milestone.
