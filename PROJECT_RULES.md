# Kalshi Arbitrage Engine — Project Rules

## Purpose and scope

Build one evolving, deterministic quantitative research and paper-trading codebase for Kalshi prediction markets. The system may ingest market data, model contract payoffs, identify logically valid arbitrage, evaluate executable profitability, simulate execution, maintain an auditable paper-trading ledger, and support replay/backtesting. Initial versions do not place real-money orders.

## Development rules

1. **Use deterministic financial logic.** Trading and arbitrage decisions must be produced by explicit, testable calculations and rules. LLMs may assist with documentation or development, but must not make trading decisions or alter authoritative financial results.
2. **Separate responsibilities.** Keep data ingestion, normalized market data, domain/contract models, strategy detection, execution-cost pricing, execution simulation, risk checks, and accounting/ledger concerns separate. Do not let strategy code depend directly on raw API response objects.
3. **Financial correctness comes first.** A mathematically correct “no trade” result is preferable to an unsupported trade or profitability claim.
4. **Test each financial boundary as it is introduced.** Add tests for mathematical invariants, payoff calculations, fees, order-book handling, partial/complete execution, and ledger accounting before relying on those components.
5. **Prove relationships between contracts.** Similar names, topics, or expiries do not establish a valid relationship. Use the actual settlement conditions and document the logical relationship before detecting cross-contract opportunities.
6. **Qualify every opportunity.** Do not call an opportunity executable or guaranteed profitable without showing the relevant assumptions, prices, fees, liquidity, fill model, and calculations. Treat uncertain inputs as uncertainty, not as profit.
7. **Prevent look-ahead bias.** Replay and backtests may use only information available at the simulated event time. Preserve event ordering, timestamps, and data provenance; do not use future quotes, fills, settlements, or revised information.
8. **Use exact monetary representations.** Authoritative monetary accounting must use an exact representation appropriate to the instrument and currency (for example integer minor units or `Decimal` with explicit quantization). Do not use binary floating-point for authoritative money, fees, balances, or P&L.
9. **Handle imperfect data explicitly.** Missing, malformed, stale, contradictory, delayed, or uncertain market data must produce an explicit status, rejection, or conservative behavior. Never silently substitute invented values.
10. **Keep architecture proportional to the milestone.** Prefer a small local codebase and simple storage while the relevant milestone is small. Do not introduce microservices, a frontend, AI agents, or abstractions without a current requirement.
11. **Follow the workflow:** learn → plan → implement → test → review. Implement only the current milestone; later-version capabilities remain documented until their milestone is active.
12. **Protect credentials.** Never hardcode API credentials or commit secrets. Use local/environment configuration and keep secret material out of source control.

## Seven-version roadmap

- **V0 — Foundation:** Understand binary contracts and implement the basic contract/payoff model.
- **V1 — Market Data:** Connect to Kalshi’s API and normalize real market data.
- **V2 — Arbitrage Detection:** Detect mathematically valid opportunities between appropriately related contracts.
- **V3 — Execution Pricing:** Account for order-book depth, fees, liquidity, and execution costs.
- **V4 — Paper Trading:** Simulate orders and maintain an auditable paper-trading ledger.
- **V5 — Historical Replay:** Replay historical or recorded market events chronologically and evaluate strategy performance without look-ahead bias.
- **V6 — Live Paper Trading:** Run against live market data with simulated execution.
- **V7+ — Advanced Research:** Explore payoff optimization, linear programming, additional contract relationships, temporal opportunities, market making, and optional live execution.

These are milestones within one evolving codebase, not separate applications. Later capabilities must not be implemented prematurely.

