from decimal import Decimal
import pytest

from kalshi_arbitrage import (
    DuplicateTradeError,
    InsufficientCashError,
    LedgerEntryType,
    LedgerError,
    NO,
    PaperPortfolio,
    PositionDelta,
    PositionHolding,
    ReconciliationResult,
    TradeLeg,
    TradeState,
    YES,
    create_proposed_trade,
)


def make_test_trade(trade_id: str = "T1", qty: int = 5, cost_cents: int = 400, fee_cents: int = 10):
    trade = create_proposed_trade(trade_id=trade_id, strategy_type="BINARY_PARITY", requested_quantity=qty)
    leg_yes = TradeLeg(
        contract_id="KX1",
        market_ticker="KX1",
        side=YES,
        requested_quantity=qty,
        filled_quantity=qty,
        total_cost_cents=cost_cents // 2,
        total_cost_dollars=Decimal(cost_cents // 2) / Decimal(100),
        average_price_cents=Decimal(cost_cents // 2) / Decimal(qty),
        average_price_dollars=Decimal(cost_cents // 2) / Decimal(100) / Decimal(qty),
        consumed_levels=(),
    )
    leg_no = TradeLeg(
        contract_id="KX1",
        market_ticker="KX1",
        side=NO,
        requested_quantity=qty,
        filled_quantity=qty,
        total_cost_cents=cost_cents // 2,
        total_cost_dollars=Decimal(cost_cents // 2) / Decimal(100),
        average_price_cents=Decimal(cost_cents // 2) / Decimal(qty),
        average_price_dollars=Decimal(cost_cents // 2) / Decimal(100) / Decimal(qty),
        consumed_levels=(),
    )
    return trade.transition_to(
        TradeState.ACCEPTED
    ).transition_to(
        TradeState.FILLED,
        filled_quantity=qty,
        total_cost_cents=cost_cents,
        estimated_fees_cents=fee_cents,
        guaranteed_payout_cents=qty * 100,
        legs=(leg_yes, leg_no),
    )


def test_initial_cash_and_deposit():
    p = PaperPortfolio(initial_cash_cents=10_000)
    assert p.available_cash_cents == 10_000
    assert p.reserved_cash_cents == 0
    assert p.total_cash_cents == 10_000
    assert len(p.ledger) == 1
    assert p.ledger[0].entry_type == LedgerEntryType.DEPOSIT

    entry = p.deposit(5_000, "Bonus deposit")
    assert p.available_cash_cents == 15_000
    assert entry.cash_delta_cents == 5_000
    assert len(p.ledger) == 2


def test_reservation_and_fill_flow():
    p = PaperPortfolio(initial_cash_cents=10_000)
    trade = make_test_trade(trade_id="T1", qty=5, cost_cents=450, fee_cents=10)

    # 1. Reserve cash for trade (450 + 10 = 460¢)
    res_entry = p.reserve_for_trade(trade)
    assert p.available_cash_cents == 9_540
    assert p.reserved_cash_cents == 460
    assert p.total_cash_cents == 10_000  # Total cash unchanged!
    assert res_entry.cash_delta_cents == -460
    assert res_entry.reserved_delta_cents == 460

    # 2. Fill trade: reservation released, actual cost + fees deducted
    fill_entry = p.apply_trade_fill(trade)
    assert p.reserved_cash_cents == 0
    assert p.available_cash_cents == 9_540  # 10,000 - 460
    assert p.total_cash_cents == 9_540
    assert p.total_fees_paid_cents == 10
    assert fill_entry.cash_delta_cents == 0  # 460 reserved was already set aside
    assert fill_entry.reserved_delta_cents == -460

    # Positions updated
    assert ("KX1", YES) in p.positions
    assert p.positions[("KX1", YES)].quantity == 5
    assert p.positions[("KX1", YES)].total_cost_cents == 225

    assert ("KX1", NO) in p.positions
    assert p.positions[("KX1", NO)].quantity == 5
    assert p.positions[("KX1", NO)].total_cost_cents == 225


def test_cancel_trade_reservation():
    p = PaperPortfolio(initial_cash_cents=10_000)
    trade = make_test_trade(trade_id="T1", qty=2, cost_cents=180, fee_cents=4)

    p.reserve_for_trade(trade)
    assert p.available_cash_cents == 9_816
    assert p.reserved_cash_cents == 184

    # Cancel reservation
    p.cancel_trade_reservation("T1", reason="Pre-fill timeout")
    assert p.available_cash_cents == 10_000
    assert p.reserved_cash_cents == 0
    assert p.total_cash_cents == 10_000


def test_direct_fill_without_prior_reservation():
    p = PaperPortfolio(initial_cash_cents=5_000)
    trade = make_test_trade(trade_id="T2", qty=3, cost_cents=270, fee_cents=6)

    fill_entry = p.apply_trade_fill(trade)
    assert p.available_cash_cents == 5_000 - 276
    assert p.total_cash_cents == 4_724
    assert p.total_fees_paid_cents == 6
    assert fill_entry.cash_delta_cents == -276


def test_duplicate_trade_rejection():
    p = PaperPortfolio(initial_cash_cents=10_000)
    trade = make_test_trade(trade_id="T1")
    p.apply_trade_fill(trade)

    with pytest.raises(DuplicateTradeError, match="already been applied"):
        p.apply_trade_fill(trade)

    with pytest.raises(DuplicateTradeError, match="already processed or reserved"):
        p.reserve_for_trade(trade)


def test_insufficient_cash_rejected():
    p = PaperPortfolio(initial_cash_cents=100)
    trade = make_test_trade(cost_cents=200, fee_cents=5)

    with pytest.raises(InsufficientCashError, match="insufficient"):
        p.reserve_for_trade(trade)

    with pytest.raises(InsufficientCashError, match="insufficient"):
        p.apply_trade_fill(trade)


def test_settlement_and_realized_pnl():
    p = PaperPortfolio(initial_cash_cents=1_000)
    trade = make_test_trade(trade_id="T1", qty=5, cost_cents=450, fee_cents=10)
    # YES leg: 5 contracts @ 225¢ (45¢ each)
    # NO leg:  5 contracts @ 225¢ (45¢ each)
    p.apply_trade_fill(trade)

    # Cash after trade = 1000 - 460 = 540¢
    assert p.available_cash_cents == 540

    # Settle market: YES wins!
    # YES payout = 5 * 100 = 500¢. Cost basis = 225¢. Realized P&L = +275¢.
    # NO payout = 0¢. Cost basis = 225¢. Realized P&L = -225¢.
    # Total net settlement payout added to cash = 500¢.
    # Total realized P&L = 275 - 225 = +50¢.
    entries = p.settle_binary_market("KX1", winning_side=YES)
    assert len(entries) == 2

    assert p.available_cash_cents == 540 + 500  # 1,040¢
    assert p.realized_pnl_cents == 50  # Gross arbitrage edge = 50¢
    # Net overall after 10¢ fee: 1040 - 1000 = +40¢
    assert len(p.positions) == 0


def test_unrealized_pnl():
    p = PaperPortfolio(initial_cash_cents=1_000)
    trade = make_test_trade(trade_id="T1", qty=2, cost_cents=180, fee_cents=4)
    # YES: 2 @ 90¢ (45¢ each)
    # NO:  2 @ 90¢ (45¢ each)
    p.apply_trade_fill(trade)

    # Mark prices: YES is now 60¢, NO is now 35¢
    marks = {
        ("KX1", YES): 60,  # 2 * 60 = 120¢ (vs 90¢ cost -> +30¢)
        ("KX1", NO): 35,   # 2 * 35 = 70¢  (vs 90¢ cost -> -20¢)
    }
    unrealized = p.unrealized_pnl_cents(marks)
    assert unrealized == 10  # 30 - 20 = +10¢
    # Total equity = cash (1000 - 184 = 816) + cost basis (180) + unrealized (10) = 1,006¢
    assert p.portfolio_equity_cents(marks) == 1_006


def test_independent_reconciliation_success():
    p = PaperPortfolio(initial_cash_cents=2_000)
    t1 = make_test_trade(trade_id="T1", qty=3, cost_cents=270, fee_cents=6)
    t2 = make_test_trade(trade_id="T2", qty=2, cost_cents=180, fee_cents=4)

    p.reserve_for_trade(t1)
    p.apply_trade_fill(t1)
    p.apply_trade_fill(t2)
    p.settle_binary_market("KX1", winning_side=NO)

    result = p.reconcile()
    assert result.is_reconciled is True
    assert len(result.discrepancies) == 0
    assert result.reconstructed_cash_cents == p.available_cash_cents
    assert result.reconstructed_fees_cents == p.total_fees_paid_cents
    assert result.reconstructed_realized_pnl_cents == p.realized_pnl_cents


def test_independent_reconciliation_detects_tampered_cash():
    p = PaperPortfolio(initial_cash_cents=2_000)
    trade = make_test_trade(trade_id="T1")
    p.apply_trade_fill(trade)

    # Artificially tamper with maintained cash
    p._available_cash_cents += 100

    result = p.reconcile()
    assert result.is_reconciled is False
    assert any("Cash mismatch" in disc for disc in result.discrepancies)


def test_negative_cash_defense_in_reservation_branch_atomic():
    # Initial cash: 500¢
    p = PaperPortfolio(initial_cash_cents=500)
    # Trade initially requires 460¢ (450¢ cost + 10¢ fee)
    trade = make_test_trade(trade_id="T1", qty=5, cost_cents=450, fee_cents=10)

    # Reserve cash: available becomes 40¢, reserved becomes 460¢
    p.reserve_for_trade(trade)
    assert p.available_cash_cents == 40
    assert p.reserved_cash_cents == 460
    assert p.total_cash_cents == 500
    assert len(p.ledger) == 2  # Deposit + Reserve

    # Simulate an altered trade requiring 600¢ total (140¢ beyond reserved 460¢)
    # Available cash (40¢) is less than excess required (140¢)!
    altered_trade = make_test_trade(trade_id="T1", qty=5, cost_cents=580, fee_cents=20)

    with pytest.raises(InsufficientCashError, match="insufficient for excess fill"):
        p.apply_trade_fill(altered_trade)

    # Verify atomic state preservation: zero mutations on rejection!
    assert p.available_cash_cents == 40
    assert p.reserved_cash_cents == 460
    assert p.total_cash_cents == 500
    assert p.total_fees_paid_cents == 0
    assert p._trade_reservations["T1"] == 460
    assert "T1" not in p.processed_trade_ids
    assert len(p.positions) == 0
    assert len(p.ledger) == 2  # No phantom FILL entry
    assert p.reconcile().is_reconciled is True


def test_settlement_identifier_contract_and_contract_id_distinction():
    p = PaperPortfolio(initial_cash_cents=2_000)

    # Create a trade where contract_id is explicitly distinct from market_ticker
    trade = create_proposed_trade(trade_id="T_CUSTOM", strategy_type="PORTFOLIO", requested_quantity=2)
    leg = TradeLeg(
        contract_id="CUSTOM_C1",
        market_ticker="MKT_TICKER_X",
        side=YES,
        requested_quantity=2,
        filled_quantity=2,
        total_cost_cents=120,
        total_cost_dollars=Decimal("1.20"),
        average_price_cents=Decimal("60"),
        average_price_dollars=Decimal("0.60"),
        consumed_levels=(),
    )
    filled_trade = trade.transition_to(TradeState.ACCEPTED).transition_to(
        TradeState.FILLED,
        filled_quantity=2,
        total_cost_cents=120,
        estimated_fees_cents=4,
        guaranteed_payout_cents=200,
        legs=(leg,),
    )
    p.apply_trade_fill(filled_trade)

    assert ("CUSTOM_C1", YES) in p.positions
    assert ("MKT_TICKER_X", YES) not in p.positions

    # settle_binary_market for market_ticker finds no matching positions
    market_entries = p.settle_binary_market("MKT_TICKER_X", winning_side=YES)
    assert len(market_entries) == 0
    assert ("CUSTOM_C1", YES) in p.positions  # Still open

    # settle_binary_contract explicitly settles by contract_id
    contract_entries = p.settle_binary_contract("CUSTOM_C1", winning_side=YES)
    assert len(contract_entries) == 1
    assert ("CUSTOM_C1", YES) not in p.positions
    assert contract_entries[0].cash_delta_cents == 200  # 2 * 100¢ payout
    assert p.reconcile().is_reconciled is True

    # Validation checks on inputs
    with pytest.raises(LedgerError, match="contract_id must be a non-empty string"):
        p.settle_position("", YES, 100)
    with pytest.raises(LedgerError, match="market_ticker must be a non-empty string"):
        p.settle_binary_market("   ", YES)
    with pytest.raises(LedgerError, match="winning_side must be"):
        p.settle_binary_contract("CUSTOM_C1", "MAYBE")
    with pytest.raises(LedgerError, match="payout_per_contract_cents must be an integer"):
        p.settle_position("CUSTOM_C1", YES, "100")
    with pytest.raises(LedgerError, match="between 0 and 100"):
        p.settle_position("CUSTOM_C1", YES, 101)


def test_book_value_equity_distinct_from_marked_to_market():
    p = PaperPortfolio(initial_cash_cents=10_000)
    trade = make_test_trade(trade_id="T1", qty=5, cost_cents=450, fee_cents=10)

    # 1. Before trade: equity = 10,000¢
    assert p.book_value_equity_cents() == 10_000
    assert p.portfolio_equity_cents() == 10_000

    # 2. Fill trade: cash drops by 460¢ (450¢ cost + 10¢ fee) to 9,540¢.
    # Cost basis is 450¢. Book value equity = 9,540 + 450 = 9,990¢ (drawdown = 10¢ fee paid).
    p.apply_trade_fill(trade)
    assert p.available_cash_cents == 9_540
    assert p.total_cost_basis_cents() == 450
    assert p.book_value_equity_cents() == 9_990
    assert p.portfolio_equity_cents() == 9_990  # Default equity model is book value!

    # 3. Mark to market with hypothetical prices:
    # YES: 60¢ (5 contracts = 300¢ vs 225¢ cost -> +75¢)
    # NO: 35¢ (5 contracts = 175¢ vs 225¢ cost -> -50¢)
    # Total unrealized = +25¢
    marks = {("KX1", YES): 60, ("KX1", NO): 35}
    assert p.unrealized_pnl_cents(marks) == 25
    assert p.portfolio_equity_cents(marks) == 9_990 + 25  # 10,015¢

    # 4. Settle market: YES wins (payout = 500¢, realized P&L = +50¢)
    p.settle_binary_market("KX1", winning_side=YES)
    assert p.available_cash_cents == 9_540 + 500  # 10,040¢
    assert p.total_cost_basis_cents() == 0
    assert p.book_value_equity_cents() == 10_040
    assert p.portfolio_equity_cents() == 10_040
    assert p.reconcile().is_reconciled is True

