from decimal import Decimal
import json
from pathlib import Path
import pytest

from kalshi_arbitrage import (
    EVENT_TYPE_OPPORTUNITY,
    EVENT_TYPE_SETTLEMENT,
    NO,
    NormalizedMarket,
    NormalizedOrderBook,
    OPPORTUNITY_BINARY_PARITY,
    OrderBookLevel,
    PaperExecutionEngine,
    PaperPortfolio,
    ReplayEngine,
    ReplayEvent,
    RiskConfig,
    RiskManager,
    TradeState,
    YES,
    load_state,
    normalize_market,
    save_state,
)


def make_level(price_str: str, qty_str: str) -> OrderBookLevel:
    return OrderBookLevel(price_dollars=Decimal(price_str), quantity=Decimal(qty_str))


def make_book(ticker: str, yes_bids=(), no_bids=()) -> NormalizedOrderBook:
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=tuple(make_level(p, q) for p, q in yes_bids),
        no_bids=tuple(make_level(p, q) for p, q in no_bids),
    )


def test_end_to_end_pipeline_simulation_settlement_persistence_recovery(tmp_path):
    storage_file = tmp_path / "portfolio_state.json"

    # Step 1: Initialize paper trading system with $1,000.00 (100,000 cents)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    risk_config = RiskConfig(
        max_cost_per_trade_cents=50_000,
        max_requested_quantity=500,
        max_position_size=500,
        max_aggregate_exposure_cents=80_000,
    )
    risk_manager = RiskManager(risk_config)
    executor = PaperExecutionEngine(default_fee_per_contract_cents=1)

    # Step 2: Observe market order book for market "PRES-2028"
    # NO bids at 0.60 (qty 20) -> YES asks at 40¢ (qty 20)
    # YES bids at 0.45 (qty 20) -> NO asks at 55¢ (qty 20)
    # Pair acquisition cost = 40 + 55 = 95¢. Guaranteed payout = 100¢. Edge = 5¢/pair.
    book = make_book(
        "PRES-2028",
        yes_bids=[("0.4500", "20.00")],
        no_bids=[("0.6000", "20.00")],
    )

    # Step 3: Simulate binary parity execution for 10 pairs
    trade = executor.simulate_binary_parity(
        book,
        requested_quantity=10,
        portfolio=portfolio,
        risk_manager=risk_manager,
    )

    assert trade.state == TradeState.FILLED
    assert trade.filled_quantity == 10
    # Cost = 10 * 95 = 950¢. Fees = 20 contracts * 1¢ = 20¢. Total required = 970¢.
    assert trade.total_cost_cents == 950
    assert trade.estimated_fees_cents == 20
    assert trade.guaranteed_payout_cents == 1000

    # Step 4: Apply filled trade to the ledger
    portfolio.apply_trade_fill(trade)
    risk_manager.record_processed_trade(trade.trade_id)

    assert portfolio.available_cash_cents == 100_000 - 970  # 99,030¢
    assert portfolio.total_fees_paid_cents == 20
    assert portfolio.positions[("PRES-2028", YES)].quantity == 10
    assert portfolio.positions[("PRES-2028", NO)].quantity == 10

    # Step 5: Persist portfolio state before market settlement
    save_state(storage_file, portfolio, [trade], risk_config)

    # Step 6: Simulate process restart and recover state
    restored_portfolio, restored_trades, restored_cfg = load_state(storage_file)

    assert restored_portfolio.available_cash_cents == 99_030
    assert restored_portfolio.total_fees_paid_cents == 20
    assert len(restored_portfolio.positions) == 2
    assert len(restored_trades) == 1
    assert restored_trades[0].trade_id == trade.trade_id

    # Verify reconciliation on restored portfolio
    assert restored_portfolio.reconcile().is_reconciled is True

    # Step 7: Market settles to YES!
    settlement_entries = restored_portfolio.settle_binary_market("PRES-2028", winning_side=YES)
    assert len(settlement_entries) == 2

    # Payout: 10 * 100 = 1000¢. Cost basis = 950¢. Realized P&L = +50¢.
    # Available cash: 99,030 + 1,000 = 100,030¢.
    # Overall net profit after 20¢ fees: 100,030 - 100,000 = +30¢!
    assert restored_portfolio.available_cash_cents == 100_030
    assert restored_portfolio.realized_pnl_cents == 50
    assert len(restored_portfolio.positions) == 0

    # Final post-settlement reconciliation
    rec_final = restored_portfolio.reconcile()
    assert rec_final.is_reconciled is True


def test_integration_with_captured_real_market_fixture(tmp_path):
    fixture_path = Path("tests/fixtures/kalshi_market_response.json")
    if not fixture_path.exists():
        pytest.skip("Fixture not found")

    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    norm_market: NormalizedMarket = normalize_market(data.get("market", data))
    assert norm_market.ticker is not None

    # Construct order book using real market's bid prices if present
    yes_bid_str = str(norm_market.yes_bid_dollars) if norm_market.yes_bid_dollars else "0.45"
    no_bid_str = str(norm_market.no_bid_dollars) if norm_market.no_bid_dollars else "0.50"

    book = make_book(
        norm_market.ticker,
        yes_bids=[(yes_bid_str, "10.00")],
        no_bids=[(no_bid_str, "10.00")],
    )

    portfolio = PaperPortfolio(initial_cash_cents=50_000)
    engine = PaperExecutionEngine(default_fee_per_contract_cents=1)

    trade = engine.simulate_binary_parity(book, requested_quantity=2)

    # Trade is either filled, partially filled, or rejected depending on prices
    assert trade.state in (TradeState.FILLED, TradeState.PARTIALLY_FILLED, TradeState.REJECTED)

    if trade.state in (TradeState.FILLED, TradeState.PARTIALLY_FILLED):
        portfolio.apply_trade_fill(trade)
        assert portfolio.reconcile().is_reconciled is True


def test_replay_multi_event_session_metrics():
    engine = ReplayEngine(default_fee_per_contract_cents=1)

    # 3 sequential events:
    # Event 1: Buy 5 pairs on M1 at 95¢ (475¢ + 10¢ fee)
    # Event 2: Buy 5 pairs on M2 at 90¢ (450¢ + 10¢ fee)
    # Event 3: Settle M1 (YES wins -> 500¢ payout)
    book1 = make_book("M1", yes_bids=[("0.4500", "10.00")], no_bids=[("0.6000", "10.00")])
    book2 = make_book("M2", yes_bids=[("0.4000", "10.00")], no_bids=[("0.7000", "10.00")])

    events = [
        ReplayEvent("2026-10-09T09:00:00Z", EVENT_TYPE_OPPORTUNITY, OPPORTUNITY_BINARY_PARITY, book=book1, requested_quantity=5),
        ReplayEvent("2026-10-09T10:00:00Z", EVENT_TYPE_OPPORTUNITY, OPPORTUNITY_BINARY_PARITY, book=book2, requested_quantity=5),
        ReplayEvent("2026-10-09T11:00:00Z", EVENT_TYPE_SETTLEMENT, settle_market_ticker="M1", settle_winning_side=YES),
    ]

    report = engine.run(events, initial_cash_cents=10_000)

    m = report.metrics
    assert m.trades_accepted == 2
    assert m.trades_filled == 2
    assert m.total_fees_cents == 20  # 10¢ + 10¢
    assert m.realized_pnl_cents == 25  # M1 paid 500¢ vs 475¢ cost
    # Open positions on M2 still held (cost basis 450¢)
    assert ("M2", YES) in report.portfolio.positions
    assert ("M2", NO) in report.portfolio.positions

    # Equity = cash + cost basis of open M2 positions
    assert report.portfolio.portfolio_equity_cents() == m.final_equity_cents
    assert report.portfolio.reconcile().is_reconciled is True
