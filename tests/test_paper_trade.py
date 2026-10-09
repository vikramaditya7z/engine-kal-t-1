from decimal import Decimal
import pytest

from kalshi_arbitrage import (
    NO,
    PaperTrade,
    TradeLeg,
    TradeLifecycleError,
    TradeState,
    create_proposed_trade,
)


def make_test_leg(
    contract_id: str = "C1",
    side: str = "YES",
    req_qty: int = 5,
    filled_qty: int = 5,
    cost_cents: int = 200,
) -> TradeLeg:
    return TradeLeg(
        contract_id=contract_id,
        market_ticker="M-TEST",
        side=side,
        requested_quantity=req_qty,
        filled_quantity=filled_qty,
        total_cost_cents=cost_cents,
        total_cost_dollars=Decimal(cost_cents) / Decimal(100),
        average_price_cents=Decimal(cost_cents) / Decimal(filled_qty) if filled_qty > 0 else None,
        average_price_dollars=(Decimal(cost_cents) / Decimal(100)) / Decimal(filled_qty) if filled_qty > 0 else None,
        consumed_levels=(),
    )


def test_proposed_trade_creation_and_attributes():
    leg1 = make_test_leg("C1", "YES", 5, 0, 0)
    trade = create_proposed_trade(
        trade_id="TR-1",
        strategy_type="BINARY_PARITY",
        requested_quantity=5,
        created_at="2026-10-09T10:00:00Z",
        legs=(leg1,),
    )

    assert trade.trade_id == "TR-1"
    assert trade.strategy_type == "BINARY_PARITY"
    assert trade.state == TradeState.PROPOSED
    assert trade.requested_quantity == 5
    assert trade.filled_quantity == 0
    assert trade.unfilled_quantity == 5
    assert trade.is_terminal is False
    assert len(trade.state_history) == 1
    assert trade.state_history[0][0] == TradeState.PROPOSED.value


def test_valid_lifecycle_transitions():
    trade = create_proposed_trade(
        trade_id="TR-1",
        strategy_type="BINARY_PARITY",
        requested_quantity=5,
    )

    # PROPOSED -> ACCEPTED
    accepted = trade.transition_to(TradeState.ACCEPTED, reason="Accepted by risk")
    assert accepted.state == TradeState.ACCEPTED
    assert accepted.is_terminal is False
    assert len(accepted.state_history) == 2

    # ACCEPTED -> FILLED
    filled = accepted.transition_to(
        TradeState.FILLED,
        filled_quantity=5,
        total_cost_cents=450,
        estimated_fees_cents=10,
        guaranteed_payout_cents=500,
        reason="Full fill simulated",
    )
    assert filled.state == TradeState.FILLED
    assert filled.filled_quantity == 5
    assert filled.unfilled_quantity == 0
    assert filled.total_cost_cents == 450
    assert filled.total_cash_required_cents == 460
    assert filled.is_terminal is True
    assert len(filled.state_history) == 3


def test_partial_fill_lifecycle():
    trade = create_proposed_trade(
        trade_id="TR-2",
        strategy_type="BINARY_PARITY",
        requested_quantity=10,
    )

    accepted = trade.transition_to(TradeState.ACCEPTED)
    partial = accepted.transition_to(
        TradeState.PARTIALLY_FILLED,
        filled_quantity=4,
        total_cost_cents=360,
        estimated_fees_cents=8,
        guaranteed_payout_cents=400,
        reason="Bottleneck on leg 2",
    )

    assert partial.state == TradeState.PARTIALLY_FILLED
    assert partial.filled_quantity == 4
    assert partial.unfilled_quantity == 6
    assert partial.total_cost_cents == 360

    # Partial fill can cancel remaining unfilled quantity
    cancelled = partial.transition_to(TradeState.CANCELLED, reason="Cancelled remaining")
    assert cancelled.state == TradeState.CANCELLED
    assert cancelled.is_terminal is True


def test_proposed_rejection():
    trade = create_proposed_trade(
        trade_id="TR-3",
        strategy_type="BINARY_PARITY",
        requested_quantity=5,
    )

    rejected = trade.transition_to(TradeState.REJECTED, reason="Insufficient depth")
    assert rejected.state == TradeState.REJECTED
    assert rejected.is_terminal is True
    assert rejected.rejection_reason == "Insufficient depth"


@pytest.mark.parametrize(
    "initial_state, illegal_target",
    [
        (TradeState.REJECTED, TradeState.ACCEPTED),
        (TradeState.REJECTED, TradeState.FILLED),
        (TradeState.FILLED, TradeState.PROPOSED),
        (TradeState.FILLED, TradeState.ACCEPTED),
        (TradeState.FILLED, TradeState.CANCELLED),
        (TradeState.CANCELLED, TradeState.ACCEPTED),
        (TradeState.PROPOSED, TradeState.FILLED),  # Must be accepted first
    ],
)
def test_illegal_lifecycle_transitions_rejected(initial_state, illegal_target):
    # Construct trade directly in initial_state
    trade = PaperTrade(
        trade_id="TR-ERR",
        strategy_type="BINARY_PARITY",
        state=initial_state,
        requested_quantity=5,
        filled_quantity=5 if initial_state == TradeState.FILLED else 0,
        unfilled_quantity=0 if initial_state == TradeState.FILLED else 5,
        total_cost_cents=450 if initial_state == TradeState.FILLED else 0,
        total_cost_dollars=Decimal("4.50") if initial_state == TradeState.FILLED else Decimal("0.00"),
        estimated_fees_cents=10 if initial_state == TradeState.FILLED else 0,
        estimated_fees_dollars=Decimal("0.10") if initial_state == TradeState.FILLED else Decimal("0.00"),
        guaranteed_payout_cents=500 if initial_state == TradeState.FILLED else 0,
        guaranteed_payout_dollars=Decimal("5.00") if initial_state == TradeState.FILLED else Decimal("0.00"),
        legs=(),
        created_at="2026-10-09T10:00:00Z",
        evaluated_at="2026-10-09T10:00:00Z",
    )

    with pytest.raises(TradeLifecycleError, match="Illegal trade transition"):
        trade.transition_to(illegal_target)


def test_trade_leg_validation():
    with pytest.raises(TradeLifecycleError, match="contract_id must be a non-empty string"):
        make_test_leg(contract_id="")

    with pytest.raises(TradeLifecycleError, match="side must be 'YES' or 'NO'"):
        make_test_leg(side="MAYBE")

    with pytest.raises(TradeLifecycleError, match="requested_quantity must be positive"):
        make_test_leg(req_qty=0)

    with pytest.raises(TradeLifecycleError, match="filled_quantity cannot exceed requested_quantity"):
        make_test_leg(req_qty=5, filled_qty=6)

    with pytest.raises(TradeLifecycleError, match="total_cost_cents cannot be negative"):
        make_test_leg(cost_cents=-1)
