"""Tests for V9 Autonomous Opportunity Evaluator, ranking, and cooldown."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.opportunity_evaluator import (
    AutonomousOpportunityEvaluator,
    EvaluatedCandidate,
    OpportunityEvaluatorConfig,
)
from kalshi_arbitrage.universe import MarketLiquidityProfile


def _make_market(ticker: str, yes_bid: Decimal = Decimal("0.40"), no_bid: Decimal = Decimal("0.40")) -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker="EVENT-1",
        market_type="binary",
        status="active",
        title="Test Binary Market",
        yes_subtitle="Yes",
        no_subtitle="No",
        yes_bid_dollars=yes_bid,
        no_bid_dollars=no_bid,
        yes_ask_dollars=Decimal("1.00") - no_bid,
        no_ask_dollars=Decimal("1.00") - yes_bid,
    )


def _make_book(ticker: str, yes_bid_p: str = "0.40", no_bid_p: str = "0.40", qty: int = 10) -> NormalizedOrderBook:
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=Decimal(yes_bid_p), quantity=Decimal(qty)),),
        no_bids=(OrderBookLevel(price_dollars=Decimal(no_bid_p), quantity=Decimal(qty)),),
        source_timestamp=datetime.now(timezone.utc),
    )


def test_evaluator_detects_net_profitable_binary_parity():
    # yes_bid = 0.40 => yes_ask = 100 - 40 = 60¢
    # no_bid = 0.55 => yes_ask from no_bid = 100 - 55 = 45¢
    # Complement: buy YES (derived from no_bid 0.55 => ask 45¢) + buy NO (derived from yes_bid 0.40 => ask 60¢) = 105¢ (not profitable)
    # To make it profitable:
    # We want buy YES ask + buy NO ask < 100¢.
    # buy YES ask is derived from NO bid: ask_yes = 100 - bid_no. So we need high bid_no (e.g. 0.60 => ask_yes = 40¢).
    # buy NO ask is derived from YES bid: ask_no = 100 - bid_yes. So we need high bid_yes (e.g. 0.65 => ask_no = 35¢).
    # Total cost = 40¢ + 35¢ = 75¢. Payout = 100¢. Gross edge = 25¢.
    m = _make_market("MKT-ARB", yes_bid=Decimal("0.65"), no_bid=Decimal("0.60"))
    b = _make_book("MKT-ARB", yes_bid_p="0.65", no_bid_p="0.60", qty=5)

    evaluator = AutonomousOpportunityEvaluator()
    candidate = evaluator.evaluate_market_binary_parity(m, b, current_cycle=1)

    assert candidate is not None
    assert candidate.market_ticker == "MKT-ARB"
    assert candidate.is_execution_feasible is True
    assert candidate.is_net_profitable is True
    assert candidate.gross_profit_cents == 25
    assert candidate.net_profit_cents > 0
    assert candidate.supported_quantity == 1


def test_evaluator_rejects_unprofitable_opportunities():
    # Costs exceed payout: bid_yes = 0.40, bid_no = 0.40 => asks are 60¢ + 60¢ = 120¢
    m = _make_market("MKT-LOSS", yes_bid=Decimal("0.40"), no_bid=Decimal("0.40"))
    b = _make_book("MKT-LOSS", yes_bid_p="0.40", no_bid_p="0.40", qty=5)

    evaluator = AutonomousOpportunityEvaluator()
    candidate = evaluator.evaluate_market_binary_parity(m, b, current_cycle=1)

    assert candidate is not None
    assert candidate.is_net_profitable is False
    assert candidate.gross_profit_cents < 0


def test_evaluator_handles_empty_order_book():
    m = _make_market("MKT-EMPTY")
    b = NormalizedOrderBook(
        market_ticker="MKT-EMPTY",
        yes_bids=(),
        no_bids=(),
        source_timestamp=datetime.now(timezone.utc),
    )

    evaluator = AutonomousOpportunityEvaluator()
    candidate = evaluator.evaluate_market_binary_parity(m, b, current_cycle=1)

    assert candidate is not None
    assert candidate.is_execution_feasible is False
    assert candidate.supported_quantity == 0


def test_evaluator_cooldown_prevents_duplicate_re_evaluation():
    m = _make_market("MKT-COOL", yes_bid=Decimal("0.65"), no_bid=Decimal("0.60"))
    b = _make_book("MKT-COOL", yes_bid_p="0.65", no_bid_p="0.60", qty=5)

    config = OpportunityEvaluatorConfig(cooldown_cycles=3)
    evaluator = AutonomousOpportunityEvaluator(config)

    # First cycle
    cand1 = evaluator.evaluate_market_binary_parity(m, b, current_cycle=1)
    assert cand1 is not None

    # Mark as traded in cycle 1
    evaluator.mark_traded("MKT-COOL", current_cycle=1)

    # In cycle 2, market is in cooldown:
    cand2 = evaluator.evaluate_market_binary_parity(m, b, current_cycle=2)
    assert cand2 is None

    # In cycle 3, still in cooldown:
    cand3 = evaluator.evaluate_market_binary_parity(m, b, current_cycle=3)
    assert cand3 is None

    # In cycle 5, cooldown expired (5 - 1 >= 3):
    cand5 = evaluator.evaluate_market_binary_parity(m, b, current_cycle=5)
    assert cand5 is not None


def test_evaluator_ranks_candidates_deterministically():
    # MKT-1 has 25¢ gross edge (cost 75¢)
    m1 = _make_market("MKT-1", yes_bid=Decimal("0.65"), no_bid=Decimal("0.60"))
    b1 = _make_book("MKT-1", yes_bid_p="0.65", no_bid_p="0.60", qty=5)

    # MKT-2 has 15¢ gross edge (cost 85¢)
    m2 = _make_market("MKT-2", yes_bid=Decimal("0.60"), no_bid=Decimal("0.55"))
    b2 = _make_book("MKT-2", yes_bid_p="0.60", no_bid_p="0.55", qty=5)

    # MKT-3 has 35¢ gross edge (cost 65¢)
    m3 = _make_market("MKT-3", yes_bid=Decimal("0.70"), no_bid=Decimal("0.65"))
    b3 = _make_book("MKT-3", yes_bid_p="0.70", no_bid_p="0.65", qty=5)

    evaluator = AutonomousOpportunityEvaluator()
    c1 = evaluator.evaluate_market_binary_parity(m1, b1, current_cycle=1)
    c2 = evaluator.evaluate_market_binary_parity(m2, b2, current_cycle=1)
    c3 = evaluator.evaluate_market_binary_parity(m3, b3, current_cycle=1)

    ranked = evaluator.rank_candidates([c1, c2, c3])
    assert len(ranked) == 3
    # MKT-3 has highest net profit, then MKT-1, then MKT-2
    assert ranked[0].market_ticker == "MKT-3"
    assert ranked[1].market_ticker == "MKT-1"
    assert ranked[2].market_ticker == "MKT-2"
