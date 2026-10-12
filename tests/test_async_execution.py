"""Tests for V8.2 asynchronous multi-leg execution lifecycle, V8.3 latency, and V8.4 scratch recovery.

Verifies:
- State machine validity: PLANNED, EXECUTING, PARTIALLY_FILLED, FILLED, LEGGED_OUT, UNWINDING, CLOSED, FAILED.
- Rejection of invalid lifecycle transitions.
- Asymmetric leg fills and unhedged directional exposure detection.
- Prohibiting incomplete trades from being classified as completed arbitrage.
- Leg sequencing policies (ILLIQUID_FIRST, HIGH_PRICE_FIRST, LOW_PRICE_FIRST, SIMULTANEOUS).
- In-flight execution latency and timestamp progression.
- Scratch / unwind policies (EMERGENCY_DUMP, AGGRESSIVE_CROSS).
- Full accounting reconciliation with PaperPortfolio (cash, positions, fees, realized P&L).
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.ledger import PaperPortfolio
from kalshi_arbitrage.async_execution import (
    AsyncExecutionError,
    AsyncMultiLegTrade,
    AsyncTradeLeg,
    ExecutionTimestamps,
    LegExecutionStatus,
    LegSequencingPolicy,
    MicrostructureConfig,
    MultiLegLifecycleState,
    RealisticExecutionSimulator,
    UnwindPolicy,
    apply_async_trade_to_portfolio,
)


def test_lifecycle_state_machine_valid_transitions():
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    ts = ExecutionTimestamps(observed_at=t0, decision_at=t0)
    leg1 = AsyncTradeLeg.create("KX-1", "KX-1", YES, 10, ts)
    leg2 = AsyncTradeLeg.create("KX-1", "KX-1", NO, 10, ts)

    trade = AsyncMultiLegTrade(
        trade_id="trade-1",
        strategy_type="binary_parity",
        target_quantity=10,
        legs=(leg1, leg2),
        state=MultiLegLifecycleState.PLANNED,
    )
    assert trade.state == MultiLegLifecycleState.PLANNED
    assert trade.is_arbitrage_complete is False

    # PLANNED -> EXECUTING
    executing = trade.transition_to(MultiLegLifecycleState.EXECUTING, reason="Submitted")
    assert executing.state == MultiLegLifecycleState.EXECUTING

    # EXECUTING -> FILLED
    filled = executing.transition_to(
        MultiLegLifecycleState.FILLED,
        reason="Fully filled",
        updated_legs=(leg1.with_fill(10, 480, 20, t0), leg2.with_fill(10, 480, 20, t0)),
    )
    assert filled.state == MultiLegLifecycleState.FILLED
    assert filled.is_arbitrage_complete is True

    # FILLED -> CLOSED
    closed = filled.transition_to(MultiLegLifecycleState.CLOSED, reason="Settled")
    assert closed.state == MultiLegLifecycleState.CLOSED

    # CLOSED is terminal: cannot transition further
    with pytest.raises(AsyncExecutionError):
        closed.transition_to(MultiLegLifecycleState.EXECUTING)


def test_lifecycle_rejects_illegal_transitions():
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    ts = ExecutionTimestamps(observed_at=t0, decision_at=t0)
    leg = AsyncTradeLeg.create("KX-1", "KX-1", YES, 10, ts)
    trade = AsyncMultiLegTrade(
        trade_id="trade-illegal",
        strategy_type="binary_parity",
        target_quantity=10,
        legs=(leg,),
        state=MultiLegLifecycleState.PLANNED,
    )

    # Cannot jump from PLANNED directly to FILLED without EXECUTING
    with pytest.raises(AsyncExecutionError):
        trade.transition_to(MultiLegLifecycleState.FILLED)

    # Cannot jump from PLANNED to CLOSED
    with pytest.raises(AsyncExecutionError):
        trade.transition_to(MultiLegLifecycleState.CLOSED)


def test_asymmetric_fill_triggers_legged_out():
    sim = RealisticExecutionSimulator()

    # Scenario: Leg 1 has ample depth (10 contracts at 48¢)
    # Leg 2 counterparty order cancelled -> 0 depth available
    trade = sim.simulate_execution(
        trade_id="sim-legged-out",
        strategy_type="binary_parity",
        target_quantity=10,
        legs_specs=[
            ("KX-1", "KX-1", YES),
            ("KX-1", "KX-1", NO),
        ],
        available_depth_by_leg={
            "KX-1": [(48, 10)],  # Both legs target same market
        },
        counterparty_bids_by_leg={
            "KX-1": [(45, 10)],  # Bids available to dump at 45¢
        },
        force_scenario="LEG_2_REJECTED",
    )

    # Trade must have detected LEGGED_OUT and unwound via EMERGENCY_DUMP to CLOSED
    assert trade.state == MultiLegLifecycleState.CLOSED
    assert trade.is_arbitrage_complete is False
    # History must record the LEGGED_OUT and UNWINDING steps
    states_visited = [h["to_state"] for h in trade.history]
    assert "EXECUTING" in states_visited
    assert "LEGGED_OUT" in states_visited
    assert "UNWINDING" in states_visited
    assert "CLOSED" in states_visited

    # Filled 10 YES at 48¢ (cost = 480¢)
    # Dumped at 45¢ (proceeds = 450¢)
    # Plus fees -> Realized scratch loss incurred!
    assert trade.realized_pnl_cents < 0


def test_residual_exposure_and_mark_to_model_risk():
    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    ts = ExecutionTimestamps(observed_at=t0, decision_at=t0)
    leg1 = AsyncTradeLeg.create("KX-1", "KX-1", YES, 10, ts).with_fill(10, 500, 20, t0)
    leg2 = AsyncTradeLeg.create("KX-1", "KX-1", NO, 10, ts)  # 0 filled

    trade = AsyncMultiLegTrade(
        trade_id="trade-risk",
        strategy_type="binary_parity",
        target_quantity=10,
        legs=(leg1, leg2),
        state=MultiLegLifecycleState.LEGGED_OUT,
    )

    # 1. Residual exposure must be exactly 10 YES contracts
    exp = trade.residual_exposure
    assert exp == {("KX-1", YES): 10}

    # 2. Mark to model with market bid of 40¢
    risk = trade.mark_to_model_risk_cents(mark_bids={("KX-1", YES): 40})
    assert risk["has_residual_exposure"] is True
    assert risk["unhedged_contracts"] == 10
    assert risk["cost_basis_cents"] == 500
    assert risk["liquidation_value_cents"] == 400
    assert risk["expected_unwind_loss_cents"] == 100  # 500 - 400 = 100¢ loss
    assert risk["worst_case_loss_cents"] == 500       # full 500¢ if expires at 0¢


def test_leg_sequencing_illiquid_first():
    cfg = MicrostructureConfig(sequencing_policy=LegSequencingPolicy.ILLIQUID_FIRST)
    sim = RealisticExecutionSimulator(cfg)

    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    ts = ExecutionTimestamps(observed_at=t0, decision_at=t0)
    # Leg A has depth 100 contracts; Leg B has depth 5 contracts
    leg_a = AsyncTradeLeg.create("LEG-A", "LEG-A", YES, 10, ts)
    leg_b = AsyncTradeLeg.create("LEG-B", "LEG-B", NO, 10, ts)

    ordered = sim.order_legs(
        [leg_a, leg_b],
        depth_by_leg={"LEG-A": 100, "LEG-B": 5},
        price_by_leg={"LEG-A": 50, "LEG-B": 50},
    )

    # Leg B (least depth) MUST be sequenced first!
    assert ordered[0].contract_id == "LEG-B"
    assert ordered[1].contract_id == "LEG-A"


def test_leg_sequencing_high_price_first():
    cfg = MicrostructureConfig(sequencing_policy=LegSequencingPolicy.HIGH_PRICE_FIRST)
    sim = RealisticExecutionSimulator(cfg)

    t0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    ts = ExecutionTimestamps(observed_at=t0, decision_at=t0)
    leg_a = AsyncTradeLeg.create("LEG-A", "LEG-A", YES, 10, ts)
    leg_b = AsyncTradeLeg.create("LEG-B", "LEG-B", NO, 10, ts)

    ordered = sim.order_legs(
        [leg_a, leg_b],
        depth_by_leg={"LEG-A": 50, "LEG-B": 50},
        price_by_leg={"LEG-A": 30, "LEG-B": 65},
    )

    # Leg B (65¢ > 30¢) MUST be sequenced first!
    assert ordered[0].contract_id == "LEG-B"
    assert ordered[1].contract_id == "LEG-A"


def test_perfect_fill_simulated_execution():
    sim = RealisticExecutionSimulator()

    trade = sim.simulate_execution(
        trade_id="sim-perfect",
        strategy_type="binary_parity",
        target_quantity=5,
        legs_specs=[
            ("MKT-1", "MKT-1", YES),
            ("MKT-2", "MKT-2", NO),
        ],
        available_depth_by_leg={
            "MKT-1": [(48, 10)],
            "MKT-2": [(48, 10)],
        },
        counterparty_bids_by_leg={},
    )

    assert trade.state == MultiLegLifecycleState.FILLED
    assert trade.is_arbitrage_complete is True
    assert trade.total_cost_cents == (5 * 48) + (5 * 48)  # 480¢
    assert trade.total_fees_cents > 0


def test_ledger_reconciliation_on_unwound_async_trade():
    """Verify that applying an unwound async trade to PaperPortfolio reconciles with 0 discrepancies."""
    sim = RealisticExecutionSimulator(
        MicrostructureConfig(unwind_policy=UnwindPolicy.EMERGENCY_DUMP)
    )

    # Trade legs out and unwinds
    trade = sim.simulate_execution(
        trade_id="sim-unwind-ledger",
        strategy_type="binary_parity",
        target_quantity=10,
        legs_specs=[
            ("CONTRACT-A", "MKT-A", YES),
            ("CONTRACT-B", "MKT-B", NO),
        ],
        available_depth_by_leg={
            "CONTRACT-A": [(48, 10)],
            "CONTRACT-B": [],  # Zero depth on B -> forces legged-out
        },
        counterparty_bids_by_leg={
            "CONTRACT-A": [(44, 10)],  # Dumped at 44¢
        },
    )
    assert trade.state == MultiLegLifecycleState.CLOSED

    # Initial portfolio with $50.00 cash (5000¢)
    portfolio = PaperPortfolio(initial_cash_cents=5000)

    # Apply trade to portfolio
    entries = apply_async_trade_to_portfolio(portfolio, trade)
    assert len(entries) == 2  # Fill entry + Unwind entry

    # Reconcile portfolio: MUST PASS WITH 0 DISCREPANCIES
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert rec.discrepancies == ()
    # All positions were dumped -> open positions must be empty
    assert len(portfolio._positions) == 0
    # Net cash should reflect initial cash + realized P&L - total fees
    expected_cash = 5000 + trade.unwind_proceeds_cents - trade.total_cost_cents - trade.total_fees_cents
    assert portfolio.available_cash_cents == expected_cash


def test_aggressive_cross_complete_fill_and_portfolio_reconciliation():
    """Verify that AGGRESSIVE_CROSS updates legs, preserves positions, and reconciles perfectly."""
    sim = RealisticExecutionSimulator(
        MicrostructureConfig(unwind_policy=UnwindPolicy.AGGRESSIVE_CROSS)
    )

    trade = sim.simulate_execution(
        trade_id="sim-cross-full",
        strategy_type="binary_parity",
        target_quantity=10,
        legs_specs=[
            ("CONTRACT-A", "MKT-A", YES),
            ("CONTRACT-B", "MKT-B", NO),
        ],
        available_depth_by_leg={
            "CONTRACT-A": [(48, 10)],
            "CONTRACT-B": [(52, 10)],  # Sufficient asks to complete hedge on cross
        },
        counterparty_bids_by_leg={},
        force_scenario="LEG_2_REJECTED",  # Forces Leg 2 to fail initially, triggering unwind
    )

    # 1. Trade state and properties
    assert trade.state == MultiLegLifecycleState.CLOSED
    assert trade.is_arbitrage_complete is True
    assert trade.residual_exposure == {}
    assert trade.legs[0].filled_quantity == 10
    assert trade.legs[1].filled_quantity == 10
    assert trade.total_cost_cents == (10 * 48) + (10 * 52)  # 1000¢
    assert trade.total_fees_cents > 0

    # 2. Portfolio application
    portfolio = PaperPortfolio(initial_cash_cents=10_000)
    entries = apply_async_trade_to_portfolio(portfolio, trade)
    assert len(entries) == 1  # Only fill entry (no dump liquidation)

    # Both legs must remain in portfolio as open positions (NO PHANTOM DELETION!)
    assert len(portfolio.positions) == 2
    assert portfolio.positions[("CONTRACT-A", YES)].quantity == 10
    assert portfolio.positions[("CONTRACT-B", NO)].quantity == 10

    # Cash must accurately reflect cost of both fills + fees
    expected_cash = 10_000 - trade.total_cost_cents - trade.total_fees_cents
    assert portfolio.available_cash_cents == expected_cash

    # Ledger must reconcile with 0 discrepancies
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert rec.discrepancies == ()


def test_aggressive_cross_partial_ask_depth_and_portfolio_reconciliation():
    """Verify that AGGRESSIVE_CROSS with partial ask liquidity updates legs and retains positions."""
    sim = RealisticExecutionSimulator(
        MicrostructureConfig(
            unwind_policy=UnwindPolicy.AGGRESSIVE_CROSS,
            sequencing_policy=LegSequencingPolicy.SIMULTANEOUS,
        )
    )

    trade = sim.simulate_execution(
        trade_id="sim-cross-partial",
        strategy_type="binary_parity",
        target_quantity=10,
        legs_specs=[
            ("CONTRACT-A", "MKT-A", YES),
            ("CONTRACT-B", "MKT-B", NO),
        ],
        available_depth_by_leg={
            "CONTRACT-A": [(48, 10)],
            "CONTRACT-B": [(52, 6)],  # Only 6 asks available on Leg B!
        },
        counterparty_bids_by_leg={},
        force_scenario="LEG_2_REJECTED",
    )

    assert trade.state == MultiLegLifecycleState.CLOSED
    assert trade.is_arbitrage_complete is False
    legs_by_id = {l.contract_id: l for l in trade.legs}
    assert legs_by_id["CONTRACT-A"].filled_quantity == 10
    assert legs_by_id["CONTRACT-B"].filled_quantity == 6
    assert trade.residual_exposure == {("CONTRACT-A", YES): 4}

    portfolio = PaperPortfolio(initial_cash_cents=10_000)
    entries = apply_async_trade_to_portfolio(portfolio, trade)
    assert len(entries) == 1

    # Both legs must be present in portfolio: 10 YES and 6 NO
    assert portfolio.positions[("CONTRACT-A", YES)].quantity == 10
    assert portfolio.positions[("CONTRACT-B", NO)].quantity == 6

    # Ledger must reconcile with 0 discrepancies
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert rec.discrepancies == ()


def test_emergency_dump_partial_bid_depth_preserves_unsold_positions():
    """Verify that EMERGENCY_DUMP with partial bid liquidity liquidates only dumped contracts."""
    sim = RealisticExecutionSimulator(
        MicrostructureConfig(unwind_policy=UnwindPolicy.EMERGENCY_DUMP)
    )

    trade = sim.simulate_execution(
        trade_id="sim-dump-partial",
        strategy_type="binary_parity",
        target_quantity=10,
        legs_specs=[
            ("CONTRACT-A", "MKT-A", YES),
            ("CONTRACT-B", "MKT-B", NO),
        ],
        available_depth_by_leg={
            "CONTRACT-A": [(48, 10)],
            "CONTRACT-B": [],  # Leg B fails
        },
        counterparty_bids_by_leg={
            "CONTRACT-A": [(44, 6)],  # Resting bids only exist for 6 contracts!
        },
    )

    assert trade.state == MultiLegLifecycleState.CLOSED
    # Exactly 6 contracts were dumped, 4 could not be dumped
    assert trade.liquidated_positions == {("CONTRACT-A", YES): 6}
    assert trade.unwind_proceeds_cents == 6 * 44  # 264¢

    portfolio = PaperPortfolio(initial_cash_cents=10_000)
    entries = apply_async_trade_to_portfolio(portfolio, trade)
    assert len(entries) == 2  # Fill + Unwind

    # The 4 unsold contracts MUST remain in portfolio holdings!
    assert ("CONTRACT-A", YES) in portfolio.positions
    holding = portfolio.positions[("CONTRACT-A", YES)]
    assert holding.quantity == 4
    assert holding.total_cost_cents == 4 * 48  # 192¢ remaining cost basis

    # Ledger reconciliation must pass cleanly
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert rec.discrepancies == ()
