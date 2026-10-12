"""Tests for V9 Paper Execution Manager and realistic multi-leg execution."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from kalshi_arbitrage.arbitrage import OPPORTUNITY_BINARY_PARITY
from kalshi_arbitrage.async_execution import (
    LegSequencingPolicy,
    MicrostructureConfig,
    MultiLegLifecycleState,
    UnwindPolicy,
)
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.execution import (
    ExecutionPricingResult,
    STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
)
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.ledger import PaperPortfolio
from kalshi_arbitrage.market_data import NormalizedOrderBook, OrderBookLevel
from kalshi_arbitrage.opportunity_evaluator import EvaluatedCandidate
from kalshi_arbitrage.paper_execution import PaperExecutionConfig, PaperExecutionManager


def _make_candidate(
    ticker: str = "TICK-ARB",
    total_cost_cents: int = 75,
    fees_cents: int = 4,
    supported_qty: int = 2,
) -> EvaluatedCandidate:
    gross = 100 * supported_qty - total_cost_cents
    exec_res = ExecutionPricingResult(
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        status=STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
        is_depth_supported=True,
        is_gross_profitable=gross > 0,
        is_net_profitable=True,
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
        net_profit_cents=gross - fees_cents,
        net_profit_dollars=Decimal(gross - fees_cents) / Decimal("100"),
        payouts_by_outcome={"YES": 100 * supported_qty, "NO": 100 * supported_qty},
        profits_by_outcome={"YES": gross, "NO": gross},
        leg_traversals=(),
    )
    return EvaluatedCandidate(
        candidate_id="cand-exec-1",
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        market_ticker=ticker,
        evaluated_at=datetime.now(timezone.utc),
        execution_result=exec_res,
    )


def test_paper_execution_full_fill_and_reconciliation():
    portfolio = PaperPortfolio(initial_cash_cents=100_000)
    manager = PaperExecutionManager()

    # Candidate needs 2 units.
    # To buy YES ask: derived from NO bids. We provide NO bids: 60¢ qty 5 => ask is 40¢
    # To buy NO ask: derived from YES bids. We provide YES bids: 65¢ qty 5 => ask is 35¢
    # Total cost for 2 units = 2 * 40¢ + 2 * 35¢ = 80¢ + 70¢ = 150¢
    book = NormalizedOrderBook(
        market_ticker="TICK-ARB",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.65"), quantity=Decimal("5")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.60"), quantity=Decimal("5")),),
        source_timestamp=datetime.now(timezone.utc),
    )

    cand = _make_candidate(ticker="TICK-ARB", total_cost_cents=150, fees_cents=8, supported_qty=2)

    trade = manager.execute_candidate(
        candidate=cand,
        order_book=book,
        portfolio=portfolio,
    )

    assert trade.is_arbitrage_complete is True
    assert trade.state in (MultiLegLifecycleState.FILLED, MultiLegLifecycleState.CLOSED)
    assert trade.target_quantity == 2
    assert trade.total_cost_cents == 150
    assert trade.total_fees_cents > 0

    # Verify portfolio state
    assert portfolio.available_cash_cents == 100_000 - (150 + trade.total_fees_cents)
    assert ("TICK-ARB:YES", YES) in portfolio.positions
    assert ("TICK-ARB:NO", NO) in portfolio.positions
    assert portfolio.positions[("TICK-ARB:YES", YES)].quantity == 2
    assert portfolio.positions[("TICK-ARB:NO", NO)].quantity == 2

    # Verify ledger audit reconciliation
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert len(rec.discrepancies) == 0


def test_paper_execution_emergency_dump_unwind():
    fee_cfg = FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)
    micro_cfg = MicrostructureConfig(
        unwind_policy=UnwindPolicy.EMERGENCY_DUMP,
        fee_config=fee_cfg,
    )
    exec_cfg = PaperExecutionConfig(
        microstructure_config=micro_cfg,
        unwind_policy=UnwindPolicy.EMERGENCY_DUMP,
    )
    manager = PaperExecutionManager(config=exec_cfg)
    portfolio = PaperPortfolio(initial_cash_cents=100_000)

    # Book where YES ask has depth (no_bids=5), but NO ask has depth 0 (yes_bids=0)
    book = NormalizedOrderBook(
        market_ticker="TICK-DUMP",
        yes_bids=(),  # NO ask has 0 depth!
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.60"), quantity=Decimal("5")),),
        source_timestamp=datetime.now(timezone.utc),
    )

    cand = _make_candidate(ticker="TICK-DUMP", total_cost_cents=80, fees_cents=4, supported_qty=2)

    # In this scenario, first leg fills but second leg legs out
    trade = manager.execute_candidate(
        candidate=cand,
        order_book=book,
        portfolio=portfolio,
    )

    assert trade.is_arbitrage_complete is False
    assert trade.state == MultiLegLifecycleState.CLOSED

    # Ledger must reconcile with zero discrepancy
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert len(rec.discrepancies) == 0
