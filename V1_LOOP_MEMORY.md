# Memory

### Done

Implemented V2 Arbitrage Detection for the Kalshi Arbitrage Engine in `kalshi_arbitrage/arbitrage.py` and exported public interfaces in `kalshi_arbitrage/__init__.py`.

The module implements:
1. Exact, validated price conversions between V1 `Decimal` dollars and V0 integer cents (`dollars_to_cents`, `cents_to_dollars`), strictly rejecting sub-cent precision to avoid silent rounding loss.
2. Single-market binary complement parity evaluation (`evaluate_binary_parity` and `evaluate_market_parity`).
3. MECE event basket arbitrage evaluation across explicitly declared and established event outcomes (`evaluate_mece_event_basket` and `evaluate_mece_markets`).
4. General portfolio arbitrage evaluation across all declared states (`evaluate_portfolio`).
5. Structured `ArbitrageOpportunity` result model reporting guaranteed payout, total cost, gross edge in integer cents and Decimal dollars, outcome-by-outcome payout/profit vectors, fee impact, and explicit disclaimers for executability and net profitability.
6. Rigorous test suite in `tests/test_arbitrage.py` (36 tests) covering valid arbitrage, break-even, no edge, outcome losses despite attractive price sums, invalid/sub-cent prices, undeclared relationships, fees, and boundary conditions.

Bytecode compilation passed (`PYTHONPYCACHEPREFIX=/tmp/kalshi-pycache .venv/bin/python -m compileall -q kalshi_arbitrage scripts tests`). The full test suite passed with `.venv/bin/python -m pytest`: 155 passed in 0.10s (119 existing V0/V1 tests without regression + 36 new V2 tests). `git diff --check` passed with 0 formatting or whitespace errors. Documentation in `ARCHITECTURE.md` was updated.

### Worked

Using V0's deterministic `portfolio_payouts_cents` under the hood for event basket and portfolio evaluation ensures 100% mathematical consistency with V0 models. Explicitly requiring `relationship_established=True` prevents unsupported cross-market relationship inferences. Rejecting sub-cent precision preserves exact monetary integrity without floating-point arithmetic.

### Failed — don't retry

Do not infer event exhaustiveness or outcome mappings from market titles or tickers. Do not allow floating-point values into monetary paths or silently round sub-cent prices. Do not misrepresent gross theoretical edge as net or executable profit without fee and order-book models.

### Next

Await human review of V2 changes. All files remain uncommitted. When ready, stage and commit the V2 implementation. The next milestone is V3 — Execution Pricing (order-book depth, exchange fees, liquidity limits, and execution-cost evaluation).
