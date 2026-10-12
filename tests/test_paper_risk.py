"""Tests for V9 Paper Risk Gate, limits, capital reservation, and kill-switch."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
import pytest

from kalshi_arbitrage.execution import (
    ExecutionPricingResult,
    STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
)
from kalshi_arbitrage.ledger import PaperPortfolio, PositionHolding
from kalshi_arbitrage.opportunity_evaluator import EvaluatedCandidate
from kalshi_arbitrage.paper_risk import PaperRiskConfig, PaperRiskGate


def _make_candidate(
    cand_id: str = "cand-1",
    ticker: str = "TICK-1",
    total_cost_cents: int = 75,
    fees_cents: int = 4,
    net_profit_cents: int = 21,
    supported_qty: int = 1,
    eval_time: Optional[datetime] = None,
) -> EvaluatedCandidate:
    gross = 100 * supported_qty - total_cost_cents
    exec_res = ExecutionPricingResult(
        strategy_type="BINARY_PARITY",
        status=STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
        is_depth_supported=True,
        is_gross_profitable=gross > 0,
        is_net_profitable=net_profit_cents > 0,
        requested_quantity=supported_qty,
        supported_quantity=supported_qty,
        unfilled_quantity=0,
        total_cost_cents=total_cost_cents,
        total_cost_dollars=Decimal(total_cost_cents) / Decimal("100"),
        guaranteed_payout_cents=100 * supported_qty,
        guaranteed_payout_dollars=Decimal(supported_qty),
        gross_profit_cents=gross,
        gross_profit_dollars=Decimal(gross) / Decimal("100"),
        estimated_fees_cents=fees_cents,
        estimated_fees_dollars=Decimal(fees_cents) / Decimal("100"),
        net_profit_cents=net_profit_cents,
        net_profit_dollars=Decimal(net_profit_cents) / Decimal("100"),
        payouts_by_outcome={"YES": 100 * supported_qty, "NO": 100 * supported_qty},
        profits_by_outcome={"YES": gross, "NO": gross},
        leg_traversals=(),
    )
    return EvaluatedCandidate(
        candidate_id=cand_id,
        strategy_type="BINARY_PARITY",
        market_ticker=ticker,
        evaluated_at=eval_time or datetime.now(timezone.utc),
        execution_result=exec_res,
    )


def test_risk_gate_approves_valid_candidate():
    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    gate = PaperRiskGate()
    cand = _make_candidate()

    res = gate.evaluate_candidate(portfolio, cand)
    assert res.passed is True
    assert res.rejection_reason is None


def test_risk_gate_rejects_when_cash_insufficient():
    portfolio = PaperPortfolio(initial_cash_cents=50)  # only 50¢ available
    gate = PaperRiskGate()
    cand = _make_candidate(total_cost_cents=75, fees_cents=4)  # needs 79¢

    res = gate.evaluate_candidate(portfolio, cand)
    assert res.passed is False
    assert res.violated_rule == "INSUFFICIENT_AVAILABLE_CASH"


def test_risk_gate_rejects_when_trade_cost_exceeds_limit():
    portfolio = PaperPortfolio(initial_cash_cents=500_000)
    config = PaperRiskConfig(max_cost_per_trade_cents=5_000)  # max $50
    gate = PaperRiskGate(config)
    cand = _make_candidate(total_cost_cents=6_000, fees_cents=100, supported_qty=70)

    res = gate.evaluate_candidate(portfolio, cand)
    assert res.passed is False
    assert res.violated_rule == "MAX_COST_PER_TRADE"


def test_risk_gate_rejects_when_aggregate_exposure_exceeded():
    portfolio = PaperPortfolio(initial_cash_cents=500_000)
    # Add existing position
    portfolio._positions[("T1", "YES")] = PositionHolding.create("T1", "YES", 100, 190_000)
    config = PaperRiskConfig(max_aggregate_exposure_cents=200_000)
    gate = PaperRiskGate(config)
    cand = _make_candidate(total_cost_cents=20_000, supported_qty=250)  # 190k + 20k = 210k > 200k

    res = gate.evaluate_candidate(portfolio, cand)
    assert res.passed is False
    assert res.violated_rule == "MAX_AGGREGATE_EXPOSURE"


def test_risk_gate_rejects_when_concurrency_limit_reached():
    portfolio = PaperPortfolio(initial_cash_cents=500_000)
    config = PaperRiskConfig(max_concurrent_trades=2)
    gate = PaperRiskGate(config)

    gate.register_active_trade("trade-1")
    gate.register_active_trade("trade-2")

    cand = _make_candidate()
    res = gate.evaluate_candidate(portfolio, cand)
    assert res.passed is False
    assert res.violated_rule == "MAX_CONCURRENT_TRADES"


def test_risk_gate_rejects_stale_data():
    portfolio = PaperPortfolio(initial_cash_cents=500_000)
    config = PaperRiskConfig(max_data_age_seconds=30.0)
    gate = PaperRiskGate(config)

    old_time = datetime.now(timezone.utc) - timedelta(seconds=45)
    cand = _make_candidate(eval_time=old_time)

    res = gate.evaluate_candidate(portfolio, cand)
    assert res.passed is False
    assert res.violated_rule == "DATA_AGE_EXCEEDED"


def test_global_kill_switch_blocks_trades():
    portfolio = PaperPortfolio(initial_cash_cents=500_000)
    gate = PaperRiskGate()
    cand = _make_candidate()

    assert gate.is_kill_switch_active is False
    gate.activate_kill_switch("Emergency operational stop")
    assert gate.is_kill_switch_active is True

    res = gate.evaluate_candidate(portfolio, cand)
    assert res.passed is False
    assert res.violated_rule == "KILL_SWITCH_ACTIVE"
    assert "Emergency operational stop" in res.rejection_reason

    # Deactivate
    gate.deactivate_kill_switch()
    assert gate.is_kill_switch_active is False
    res2 = gate.evaluate_candidate(portfolio, _make_candidate(cand_id="cand-2"))
    assert res2.passed is True


def test_consecutive_failures_trigger_automatic_kill_switch():
    config = PaperRiskConfig(max_consecutive_failures=3)
    gate = PaperRiskGate(config)

    gate.record_failure("error 1")
    gate.record_failure("error 2")
    assert gate.is_kill_switch_active is False

    gate.record_failure("error 3")
    assert gate.is_kill_switch_active is True
    assert "Consecutive failure threshold reached" in gate.kill_switch_reason


def test_capital_reservation_and_release():
    portfolio = PaperPortfolio(initial_cash_cents=10_000)
    gate = PaperRiskGate()

    assert gate.reserved_capital_cents == 0

    gate.reserve_capital(portfolio, 2_500)
    assert gate.reserved_capital_cents == 2_500

    gate.release_reservation(portfolio, 2_500)
    assert gate.reserved_capital_cents == 0
