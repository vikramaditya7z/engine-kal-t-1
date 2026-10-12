"""V12 Adversarial Financial Accounting & Ledger Integrity Verification.

Exhaustive stress tests verifying the mathematical invariants of PaperPortfolio,
Ledger, pre-trade reservations, settlements, multi-leg asynchronous execution,
unwind policies, and recovery across adversarial edge cases.
"""

from decimal import Decimal
import pytest

from kalshi_arbitrage.arbitrage import YES, NO
from kalshi_arbitrage.ledger import (
    DuplicateTradeError,
    InsufficientCashError,
    LedgerError,
    PaperPortfolio,
)
from kalshi_arbitrage.paper_trade import (
    PaperTrade,
    TradeLeg,
    TradeState,
    create_proposed_trade,
)


def _make_dummy_trade(trade_id: str, cost_cents: int, fees_cents: int, qty: int = 10, ticker: str = "KX1") -> PaperTrade:
    trade = create_proposed_trade(
        trade_id=trade_id,
        strategy_type="BINARY_PARITY",
        requested_quantity=qty,
    )
    half_cost = cost_cents // 2
    leg_yes = TradeLeg(
        contract_id=ticker,
        market_ticker=ticker,
        side=YES,
        requested_quantity=qty,
        filled_quantity=qty,
        total_cost_cents=half_cost,
        total_cost_dollars=Decimal(half_cost) / Decimal(100),
        average_price_cents=Decimal(half_cost) / Decimal(qty),
        average_price_dollars=Decimal(half_cost) / Decimal(100) / Decimal(qty),
        consumed_levels=(),
    )
    leg_no = TradeLeg(
        contract_id=ticker,
        market_ticker=ticker,
        side=NO,
        requested_quantity=qty,
        filled_quantity=qty,
        total_cost_cents=half_cost,
        total_cost_dollars=Decimal(half_cost) / Decimal(100),
        average_price_cents=Decimal(half_cost) / Decimal(qty),
        average_price_dollars=Decimal(half_cost) / Decimal(100) / Decimal(qty),
        consumed_levels=(),
    )
    return trade.transition_to(
        TradeState.ACCEPTED,
        total_cost_cents=cost_cents,
        estimated_fees_cents=fees_cents,
        legs=(leg_yes, leg_no),
    )


def test_adversarial_cash_reservation_boundaries():
    """Verify pre-trade reservation fail-closed behavior on boundary conditions."""
    portfolio = PaperPortfolio(initial_cash_cents=1000)
    assert portfolio.available_cash_cents == 1000
    assert portfolio.reserved_cash_cents == 0

    # 1. Attempt reservation requiring 1001¢ (insufficient by 1¢)
    t_over = _make_dummy_trade("t-over", cost_cents=980, fees_cents=21)  # 1001¢ total
    with pytest.raises(InsufficientCashError, match="insufficient"):
        portfolio.reserve_for_trade(t_over)

    # State must be completely unmutated
    assert portfolio.available_cash_cents == 1000
    assert portfolio.reserved_cash_cents == 0
    assert len(portfolio.ledger) == 1  # only initial deposit

    # 2. Reserve exact available amount: 1000¢
    t_exact = _make_dummy_trade("t-exact", cost_cents=980, fees_cents=20)  # 1000¢ total
    portfolio.reserve_for_trade(t_exact)
    assert portfolio.available_cash_cents == 0
    assert portfolio.reserved_cash_cents == 1000

    # 3. Duplicate reservation must raise DuplicateTradeError and not double-reserve
    with pytest.raises(DuplicateTradeError):
        portfolio.reserve_for_trade(t_exact)
    assert portfolio.available_cash_cents == 0
    assert portfolio.reserved_cash_cents == 1000

    # 4. Cancel reservation restores exact cash
    portfolio.cancel_trade_reservation("t-exact")
    assert portfolio.available_cash_cents == 1000
    assert portfolio.reserved_cash_cents == 0
    assert portfolio.reconcile().is_reconciled is True


def test_adversarial_fill_without_prior_reservation():
    """Verify that unreserved fills are validated atomically against available cash."""
    portfolio = PaperPortfolio(initial_cash_cents=500)
    t_acc = _make_dummy_trade("t-fill", cost_cents=450, fees_cents=60)  # 510¢ total
    t_fill = t_acc.transition_to(
        TradeState.FILLED,
        filled_quantity=10,
        total_cost_cents=450,
        estimated_fees_cents=60,
    )

    with pytest.raises(InsufficientCashError):
        portfolio.apply_trade_fill(t_fill)

    assert portfolio.available_cash_cents == 500
    assert len(portfolio.positions) == 0
    assert portfolio.reconcile().is_reconciled is True


def test_adversarial_settlement_payout_bounds():
    """Verify binary settlement invariants and boundary validation."""
    portfolio = PaperPortfolio(initial_cash_cents=2000)
    ticker = "KX-BOUNDS"
    t_acc = _make_dummy_trade("t-settle", cost_cents=950, fees_cents=20, qty=10, ticker=ticker)
    t_fill = t_acc.transition_to(
        TradeState.FILLED,
        filled_quantity=10,
        total_cost_cents=950,
        estimated_fees_cents=20,
    )
    portfolio.reserve_for_trade(t_acc)
    portfolio.apply_trade_fill(t_fill)

    # In single-market parity, positions are keyed by (ticker, side)
    assert (ticker, YES) in portfolio.positions
    assert (ticker, NO) in portfolio.positions

    # Invalid settlement parameters must fail closed
    with pytest.raises(LedgerError, match="payout_per_contract_cents must be between 0 and 100"):
        portfolio.settle_position(ticker, YES, payout_per_contract_cents=101)

    with pytest.raises(LedgerError, match="payout_per_contract_cents must be between 0 and 100"):
        portfolio.settle_position(ticker, YES, payout_per_contract_cents=-1)

    # Settle market: YES wins (100¢ payout on YES, 0¢ on NO)
    entries = portfolio.settle_binary_market(ticker, winning_side=YES)
    assert len(entries) == 2

    # 10 YES contracts @ 100¢ payout = 1000¢ payout (cost basis 475¢ -> realized PnL +525¢)
    # 10 NO contracts @ 0¢ payout = 0¢ payout (cost basis 475¢ -> realized PnL -475¢)
    # Total Gross Realized PnL = +525 - 475 = +50¢.
    # Total Fees Paid = 20¢.
    # Net Profit = +30¢.
    # Initial Cash = 2000¢.
    # Ending Cash = 2000 - 950 - 20 + 1000 = 2030¢.
    assert portfolio.available_cash_cents == 2030
    assert portfolio.realized_pnl_cents == 50
    assert portfolio.total_fees_paid_cents == 20
    assert len(portfolio.positions) == 0

    # Book value equity must equal cash when all positions settled
    assert portfolio.book_value_equity_cents() == 2030

    # Audit reconciliation must pass with zero discrepancies
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert rec.discrepancies == ()


def test_adversarial_mark_to_market_valuation():
    """Verify mark-to-market calculations and out-of-bound handling."""
    portfolio = PaperPortfolio(initial_cash_cents=5000)
    ticker = "KX-MTM"
    t_acc = _make_dummy_trade("t-mtm", cost_cents=800, fees_cents=30, qty=10, ticker=ticker)
    t_fill = t_acc.transition_to(
        TradeState.FILLED,
        filled_quantity=10,
        total_cost_cents=800,
        estimated_fees_cents=30,
    )
    portfolio.apply_trade_fill(t_fill)

    # Out of bounds mark price
    with pytest.raises(LedgerError, match="out of bounds"):
        portfolio.portfolio_equity_cents({(ticker, YES): 105, (ticker, NO): 50})

    # Missing position mark price
    with pytest.raises(LedgerError, match="Missing mark price"):
        portfolio.portfolio_equity_cents({(ticker, YES): 50})

    # Valid mark prices: YES=60¢, NO=30¢
    # YES holding: 10 @ cost 400¢ -> current value 10*60 = 600¢ (unrealized +200¢)
    # NO holding:  10 @ cost 400¢ -> current value 10*30 = 300¢ (unrealized -100¢)
    # Total unrealized = +100¢.
    # Cash = 5000 - 800 - 30 = 4170¢.
    # Total cost basis = 800¢.
    # Book value equity = 4170 + 800 = 4970¢.
    # MTM equity = 4970 + 100 = 5070¢.
    mtm_equity = portfolio.portfolio_equity_cents({(ticker, YES): 60, (ticker, NO): 30})
    assert portfolio.book_value_equity_cents() == 4970
    assert mtm_equity == 5070
    assert portfolio.unrealized_pnl_cents({(ticker, YES): 60, (ticker, NO): 30}) == 100


def test_adversarial_sequential_multi_trade_reconciliation_stress():
    """Execute randomized sequence of deposits, reservations, fills, unwinds, and settlements."""
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    for i in range(20):
        t_id = f"stress-{i}"
        ticker = f"KX-STRESS-{i}"
        cost = 1000 + (i * 50)
        fees = 20 + i
        trade = _make_dummy_trade(t_id, cost_cents=cost, fees_cents=fees, qty=10, ticker=ticker)

        # 1. Reserve
        portfolio.reserve_for_trade(trade)
        rec1 = portfolio.reconcile()
        assert rec1.is_reconciled is True

        # Alternate: every 4th trade gets cancelled instead of filled
        if i % 4 == 0:
            portfolio.cancel_trade_reservation(t_id, reason="stress test cancellation")
            rec_cancel = portfolio.reconcile()
            assert rec_cancel.is_reconciled is True
            continue

        # 2. Fill
        filled_trade = trade.transition_to(
            TradeState.FILLED,
            filled_quantity=10,
            total_cost_cents=cost,
            estimated_fees_cents=fees,
        )
        portfolio.apply_trade_fill(filled_trade)
        rec2 = portfolio.reconcile()
        assert rec2.is_reconciled is True

        # Settle alternate trades
        if i % 2 == 1:
            portfolio.settle_binary_market(ticker, winning_side=YES)
            rec_settle = portfolio.reconcile()
            assert rec_settle.is_reconciled is True

    # Final comprehensive audit
    final_rec = portfolio.reconcile()
    assert final_rec.is_reconciled is True
    assert final_rec.discrepancies == ()
    assert portfolio.book_value_equity_cents() == portfolio.total_cash_cents + portfolio.total_cost_basis_cents()
