from decimal import Decimal
import pytest

from kalshi_arbitrage import (
    BinaryContract,
    Event,
    NO,
    NormalizedOrderBook,
    OrderBookLevel,
    PaperExecutionEngine,
    PaperExecutionError,
    Portfolio,
    Position,
    TradeState,
    YES,
)


def make_level(price_str: str, qty_str: str) -> OrderBookLevel:
    return OrderBookLevel(price_dollars=Decimal(price_str), quantity=Decimal(qty_str))


def make_book(
    ticker: str = "KXTEST",
    yes_bids=(),
    no_bids=(),
) -> NormalizedOrderBook:
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=tuple(make_level(p, q) for p, q in yes_bids),
        no_bids=tuple(make_level(p, q) for p, q in no_bids),
    )


def test_simulate_binary_parity_full_fill():
    engine = PaperExecutionEngine(default_fee_per_contract_cents=1)
    book = make_book(
        yes_bids=[("0.4500", "10.00")],  # NO ask = 55¢ (qty 10)
        no_bids=[("0.6000", "10.00")],  # YES ask = 40¢ (qty 10)
    )

    trade = engine.simulate_binary_parity(book, requested_quantity=5)

    assert trade.state == TradeState.FILLED
    assert trade.requested_quantity == 5
    assert trade.filled_quantity == 5
    assert trade.unfilled_quantity == 0
    # Cost = 5 * (40 + 55) = 475¢
    assert trade.total_cost_cents == 475
    # Fees = 10 contracts * 1¢ = 10¢
    assert trade.estimated_fees_cents == 10
    assert trade.total_cash_required_cents == 485
    assert trade.guaranteed_payout_cents == 500
    assert len(trade.legs) == 2
    assert trade.legs[0].filled_quantity == 5
    assert trade.legs[1].filled_quantity == 5


def test_simulate_binary_parity_partial_fill_bottleneck():
    engine = PaperExecutionEngine()
    book = make_book(
        yes_bids=[("0.4500", "3.00")],   # NO ask = 55¢ (qty 3)
        no_bids=[("0.6000", "10.00")],  # YES ask = 40¢ (qty 10)
    )

    trade = engine.simulate_binary_parity(book, requested_quantity=8)

    assert trade.state == TradeState.PARTIALLY_FILLED
    assert trade.requested_quantity == 8
    assert trade.filled_quantity == 3
    assert trade.unfilled_quantity == 5
    assert trade.total_cost_cents == 3 * 95  # 285¢
    assert trade.guaranteed_payout_cents == 300
    assert "Partial fill simulated" in trade.rejection_reason


def test_simulate_binary_parity_insufficient_depth_rejected():
    engine = PaperExecutionEngine()
    book = make_book(
        yes_bids=[],
        no_bids=[("0.6000", "10.00")],
    )

    trade = engine.simulate_binary_parity(book, requested_quantity=5)

    assert trade.state == TradeState.REJECTED
    assert trade.filled_quantity == 0
    assert "cannot form complete binary pairs" in trade.rejection_reason


def test_simulate_binary_parity_unprofitable_after_fees_rejected_when_enforced():
    engine = PaperExecutionEngine()
    book = make_book(
        yes_bids=[("0.4500", "10.00")],  # NO ask = 55¢
        no_bids=[("0.5400", "10.00")],  # YES ask = 46¢
    )
    # Cost per pair = 101¢ -> gross loss even before fees
    trade = engine.simulate_binary_parity(book, requested_quantity=5)
    assert trade.state == TradeState.REJECTED
    assert "No gross edge" in trade.rejection_reason


def test_simulate_mece_basket_yes_full_fill():
    engine = PaperExecutionEngine(default_fee_per_contract_cents=1)
    event = Event(identifier="EV3", outcomes=("A", "B", "C"), relationship_established=True)
    books = {
        "A": make_book("M-A", no_bids=[("0.7000", "10.00")]),  # YES ask 30¢
        "B": make_book("M-B", no_bids=[("0.7000", "10.00")]),  # YES ask 30¢
        "C": make_book("M-C", no_bids=[("0.7000", "10.00")]),  # YES ask 30¢
    }

    trade = engine.simulate_mece_basket(event, books, basket_side=YES, requested_quantity=4)

    assert trade.state == TradeState.FILLED
    assert trade.filled_quantity == 4
    # 4 baskets * (30 + 30 + 30) = 360¢
    assert trade.total_cost_cents == 360
    # Fees = 4 units * 3 legs = 12 contracts * 1¢ = 12¢
    assert trade.estimated_fees_cents == 12
    # Payout = 4 * 100 = 400¢
    assert trade.guaranteed_payout_cents == 400
    assert len(trade.legs) == 3


def test_simulate_mece_basket_undeclared_relationship_rejected():
    engine = PaperExecutionEngine()
    event = Event(identifier="EV-UNDEC", outcomes=("A", "B"), relationship_established=False)
    books = {
        "A": make_book("M-A", no_bids=[("0.7000", "10.00")]),
        "B": make_book("M-B", no_bids=[("0.7000", "10.00")]),
    }

    trade = engine.simulate_mece_basket(event, books, basket_side=YES, requested_quantity=2)

    assert trade.state == TradeState.REJECTED
    assert "Undeclared outcome relationship" in trade.rejection_reason


def test_simulate_portfolio_partial_bottleneck():
    engine = PaperExecutionEngine()
    event = Event(identifier="EV", outcomes=("O1", "O2"), relationship_established=True)
    c1 = BinaryContract(identifier="C1", price_cents=50, yes_outcome="O1")
    c2 = BinaryContract(identifier="C2", price_cents=50, yes_outcome="O2")
    portfolio = Portfolio(
        event=event,
        contracts=(c1, c2),
        positions=(Position("C1", YES, 2), Position("C2", YES, 3)),
    )
    books = {
        "C1": make_book("M-C1", no_bids=[("0.7000", "20.00")]),  # YES ask 30¢
        "C2": make_book("M-C2", no_bids=[("0.7000", "9.00")]),   # YES ask 30¢, supports 9//3 = 3 units
    }

    trade = engine.simulate_portfolio(portfolio, books, unit_quantity=5)

    assert trade.state == TradeState.PARTIALLY_FILLED
    assert trade.filled_quantity == 3
    assert trade.unfilled_quantity == 2
    # C1: 3 * 2 = 6 contracts @ 30¢ = 180¢
    # C2: 3 * 3 = 9 contracts @ 30¢ = 270¢
    assert trade.total_cost_cents == 450
    assert trade.guaranteed_payout_cents == 600
    assert len(trade.legs) == 2
    assert trade.legs[0].contract_id == "C1"
    assert trade.legs[0].filled_quantity == 6
    assert trade.legs[1].contract_id == "C2"
    assert trade.legs[1].filled_quantity == 9


def test_execute_from_pricing_result_type_error():
    engine = PaperExecutionEngine()
    with pytest.raises(PaperExecutionError, match="pricing_result must be an ExecutionPricingResult"):
        engine.execute_from_pricing_result("not_a_result")
