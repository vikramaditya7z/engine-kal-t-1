from decimal import Decimal
import pytest

from kalshi_arbitrage import (
    ExecutionPricingResult,
    NO,
    PaperPortfolio,
    RiskCheckResult,
    RiskConfig,
    RiskError,
    RiskManager,
    TradeLeg,
    TradeState,
    YES,
    create_proposed_trade,
)


def make_trade_with_cost(
    trade_id: str = "T1",
    req_qty: int = 10,
    cost_cents: int = 400,
    fee_cents: int = 10,
    gross_profit_cents: int = 50,
    is_net_profitable: bool = True,
    contract_id: str = "KX1",
):
    pricing = ExecutionPricingResult(
        strategy_type="BINARY_PARITY",
        status="DEPTH_SUPPORTED_NET_PROFITABLE",
        is_depth_supported=True,
        is_gross_profitable=gross_profit_cents > 0,
        is_net_profitable=is_net_profitable,
        requested_quantity=req_qty,
        supported_quantity=req_qty,
        unfilled_quantity=0,
        total_cost_cents=cost_cents,
        total_cost_dollars=Decimal(cost_cents) / Decimal(100),
        guaranteed_payout_cents=cost_cents + gross_profit_cents,
        guaranteed_payout_dollars=Decimal(cost_cents + gross_profit_cents) / Decimal(100),
        gross_profit_cents=gross_profit_cents,
        gross_profit_dollars=Decimal(gross_profit_cents) / Decimal(100),
        estimated_fees_cents=fee_cents,
        estimated_fees_dollars=Decimal(fee_cents) / Decimal(100),
    )
    leg = TradeLeg(
        contract_id=contract_id,
        market_ticker=contract_id,
        side=YES,
        requested_quantity=req_qty,
        filled_quantity=req_qty,
        total_cost_cents=cost_cents,
        total_cost_dollars=Decimal(cost_cents) / Decimal(100),
        average_price_cents=Decimal(cost_cents) / Decimal(req_qty),
        average_price_dollars=Decimal(cost_cents) / Decimal(100) / Decimal(req_qty),
        consumed_levels=(),
    )
    return create_proposed_trade(
        trade_id=trade_id,
        strategy_type="BINARY_PARITY",
        requested_quantity=req_qty,
        execution_result=pricing,
        legs=(leg,),
    )


def test_risk_evaluation_passes_compliant_trade():
    p = PaperPortfolio(initial_cash_cents=10_000)
    rm = RiskManager()
    trade = make_trade_with_cost(req_qty=10, cost_cents=500, fee_cents=10)

    verdict = rm.evaluate_trade(p, trade)
    assert verdict.passed is True
    assert verdict.rejection_reason is None


def test_risk_max_requested_quantity():
    p = PaperPortfolio(initial_cash_cents=100_000)
    cfg = RiskConfig(max_requested_quantity=50)
    rm = RiskManager(cfg)

    trade = make_trade_with_cost(req_qty=100)
    verdict = rm.evaluate_trade(p, trade)

    assert verdict.passed is False
    assert verdict.violated_rule == "MAX_REQUESTED_QUANTITY"
    assert "exceeds maximum per-trade limit" in verdict.rejection_reason


def test_risk_max_cost_per_trade():
    p = PaperPortfolio(initial_cash_cents=100_000)
    cfg = RiskConfig(max_cost_per_trade_cents=5_000)
    rm = RiskManager(cfg)

    trade = make_trade_with_cost(cost_cents=6_000)
    verdict = rm.evaluate_trade(p, trade)

    assert verdict.passed is False
    assert verdict.violated_rule == "MAX_COST_PER_TRADE"


def test_risk_insufficient_available_cash():
    p = PaperPortfolio(initial_cash_cents=1_000)
    rm = RiskManager()

    # Trade requires 1,200¢ (1,150 cost + 50 fees)
    trade = make_trade_with_cost(cost_cents=1_150, fee_cents=50)
    verdict = rm.evaluate_trade(p, trade)

    assert verdict.passed is False
    assert verdict.violated_rule == "INSUFFICIENT_AVAILABLE_CASH"


def test_risk_max_aggregate_exposure():
    p = PaperPortfolio(initial_cash_cents=100_000)
    cfg = RiskConfig(max_aggregate_exposure_cents=10_000)
    rm = RiskManager(cfg)

    # First trade adds 8,000¢ of exposure
    t1 = make_trade_with_cost("T1", cost_cents=8_000)
    t1_filled = t1.transition_to(TradeState.ACCEPTED).transition_to(
        TradeState.FILLED, filled_quantity=10, total_cost_cents=8_000, estimated_fees_cents=10, guaranteed_payout_cents=9000, legs=t1.legs
    )
    p.apply_trade_fill(t1_filled)

    # Second trade requires 3,000¢ -> 8,000 + 3,000 = 11,000 > 10,000!
    t2 = make_trade_with_cost("T2", cost_cents=3_000)
    verdict = rm.evaluate_trade(p, t2)

    assert verdict.passed is False
    assert verdict.violated_rule == "MAX_AGGREGATE_EXPOSURE"


def test_risk_max_position_size():
    p = PaperPortfolio(initial_cash_cents=100_000)
    cfg = RiskConfig(max_position_size=50)
    rm = RiskManager(cfg)

    # First trade has 40 contracts in KX1
    t1 = make_trade_with_cost("T1", req_qty=40, cost_cents=1000)
    t1_filled = t1.transition_to(TradeState.ACCEPTED).transition_to(
        TradeState.FILLED, filled_quantity=40, total_cost_cents=1000, estimated_fees_cents=10, guaranteed_payout_cents=2000, legs=t1.legs
    )
    p.apply_trade_fill(t1_filled)

    # Second trade has 20 contracts in KX1 -> total 60 > 50!
    t2 = make_trade_with_cost("T2", req_qty=20, cost_cents=500)
    verdict = rm.evaluate_trade(p, t2)

    assert verdict.passed is False
    assert verdict.violated_rule == "MAX_POSITION_SIZE"


def test_risk_edge_requirements():
    p = PaperPortfolio(initial_cash_cents=100_000)
    cfg = RiskConfig(require_positive_gross_edge=True, require_positive_net_edge=True)
    rm = RiskManager(cfg)

    # Trade with negative net profit
    trade_bad_net = make_trade_with_cost("T1", gross_profit_cents=10, fee_cents=20, is_net_profitable=False)
    verdict = rm.evaluate_trade(p, trade_bad_net)
    assert verdict.passed is False
    assert verdict.violated_rule == "REQUIRE_POSITIVE_NET_EDGE"


def test_risk_duplicate_trade_id():
    p = PaperPortfolio(initial_cash_cents=100_000)
    rm = RiskManager()
    trade = make_trade_with_cost("T1")

    v1 = rm.evaluate_trade(p, trade)
    assert v1.passed is True
    rm.record_processed_trade("T1")

    v2 = rm.evaluate_trade(p, trade)
    assert v2.passed is False
    assert v2.violated_rule == "DUPLICATE_TRADE_ID"


def test_risk_rejection_leaves_portfolio_unmutated():
    p = PaperPortfolio(initial_cash_cents=10_000)
    rm = RiskManager(RiskConfig(max_requested_quantity=5))
    trade = make_trade_with_cost(req_qty=10)

    verdict = rm.evaluate_trade(p, trade)
    assert verdict.passed is False

    # Check portfolio state is 100% unchanged
    assert p.available_cash_cents == 10_000
    assert p.reserved_cash_cents == 0
    assert len(p.positions) == 0
    assert len(p.ledger) == 1  # Only initial deposit
