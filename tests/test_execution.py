from decimal import Decimal
import pytest

from kalshi_arbitrage import (
    BinaryContract,
    Event,
    ExecutionError,
    NO,
    NormalizedOrderBook,
    OrderBookLevel,
    Portfolio,
    Position,
    STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE,
    STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
    STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE,
    STATUS_INSUFFICIENT_DEPTH,
    STATUS_INVALID_INPUT,
    STATUS_NO_GROSS_EDGE,
    STATUS_PARTIALLY_SUPPORTED,
    YES,
    derive_ask_levels,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
    evaluate_portfolio_execution,
    traverse_order_book_depth,
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


# ---------------------------------------------------------------------------
# Section A: Order-book validation & Ask level derivation
# ---------------------------------------------------------------------------


def test_derive_ask_levels_computes_correct_complementary_prices():
    # In Kalshi binary markets:
    # YES asks come from NO bids: yes_ask = 100 - no_bid
    # NO bids at 0.40 (qty 10) and 0.60 (qty 5) =>
    # YES asks at 40¢ (qty 5, from 60¢ bid) and 60¢ (qty 10, from 40¢ bid).
    book = make_book(no_bids=[("0.4000", "10.00"), ("0.6000", "5.00")])
    asks = derive_ask_levels(book, YES)

    assert len(asks) == 2
    # Cheapest ask first (price priority)
    assert asks[0].side == YES
    assert asks[0].price_cents == 40
    assert asks[0].price_dollars == Decimal("0.40")
    assert asks[0].quantity == 5
    assert asks[0].source_bid_cents == 60

    assert asks[1].price_cents == 60
    assert asks[1].price_dollars == Decimal("0.60")
    assert asks[1].quantity == 10
    assert asks[1].source_bid_cents == 40


def test_derive_ask_levels_no_side_from_yes_bids():
    # NO asks come from YES bids: no_ask = 100 - yes_bid
    book = make_book(yes_bids=[("0.2500", "8.00"), ("0.7000", "12.00")])
    asks = derive_ask_levels(book, NO)

    assert len(asks) == 2
    assert asks[0].side == NO
    assert asks[0].price_cents == 30  # 100 - 70 = 30
    assert asks[0].quantity == 12

    assert asks[1].price_cents == 75  # 100 - 25 = 75
    assert asks[1].quantity == 8


def test_derive_ask_levels_empty_side():
    book = make_book(no_bids=[])
    asks = derive_ask_levels(book, YES)
    assert asks == ()


@pytest.mark.parametrize("invalid_side", ["MAYBE", "", None, 123])
def test_derive_ask_levels_invalid_side(invalid_side):
    book = make_book()
    with pytest.raises(ExecutionError):
        derive_ask_levels(book, invalid_side)


# ---------------------------------------------------------------------------
# Section B: Depth traversal routine & invariants
# ---------------------------------------------------------------------------


def test_depth_traversal_single_level_full_fill():
    # NO bid at 0.55 (qty 20) => YES ask at 45¢ (qty 20).
    book = make_book(no_bids=[("0.5500", "20.00")])
    res = traverse_order_book_depth(book, YES, requested_quantity=10)

    assert res.market_ticker == "KXTEST"
    assert res.side == YES
    assert res.requested_quantity == 10
    assert res.supported_quantity == 10
    assert res.unfilled_quantity == 0
    assert res.is_full_fill is True
    assert res.is_partial_fill is False
    assert res.is_empty_fill is False
    assert res.total_cost_cents == 450  # 10 * 45¢
    assert res.total_cost_dollars == Decimal("4.50")
    assert res.average_price_cents == Decimal("45")
    assert res.average_price_dollars == Decimal("0.45")
    assert len(res.consumed_levels) == 1
    assert res.consumed_levels[0].price_cents == 45
    assert res.consumed_levels[0].quantity == 10
    assert res.consumed_levels[0].cost_cents == 450
    assert res.status == "FULL_FILL"
    assert res.rejection_reason is None


def test_depth_traversal_multi_level_best_price_priority():
    # NO bids at:
    # 0.40 (qty 10) => YES ask at 60¢ (qty 10)
    # 0.50 (qty 5)  => YES ask at 50¢ (qty 5)
    # 0.70 (qty 8)  => YES ask at 30¢ (qty 8)
    # Request 15: should take all 8 @ 30¢, all 5 @ 50¢, and 2 @ 60¢.
    # Cost = 8*30 (240) + 5*50 (250) + 2*60 (120) = 610¢
    book = make_book(no_bids=[("0.4000", "10.00"), ("0.5000", "5.00"), ("0.7000", "8.00")])
    res = traverse_order_book_depth(book, YES, requested_quantity=15)

    assert res.is_full_fill is True
    assert res.supported_quantity == 15
    assert res.total_cost_cents == 610
    assert res.total_cost_dollars == Decimal("6.10")
    assert res.average_price_cents == Decimal("610") / Decimal("15")

    assert len(res.consumed_levels) == 3
    assert res.consumed_levels[0].price_cents == 30
    assert res.consumed_levels[0].quantity == 8
    assert res.consumed_levels[1].price_cents == 50
    assert res.consumed_levels[1].quantity == 5
    assert res.consumed_levels[2].price_cents == 60
    assert res.consumed_levels[2].quantity == 2


def test_depth_traversal_exact_depth_exhaustion():
    # Total available: 5 contracts
    book = make_book(no_bids=[("0.6000", "5.00")])
    res = traverse_order_book_depth(book, YES, requested_quantity=5)

    assert res.is_full_fill is True
    assert res.supported_quantity == 5
    assert res.unfilled_quantity == 0
    assert res.total_cost_cents == 200  # 5 * 40¢


def test_depth_traversal_insufficient_depth_partial_fill():
    # Total available: 5 contracts. Request: 10 contracts.
    book = make_book(no_bids=[("0.6000", "5.00")])
    res = traverse_order_book_depth(book, YES, requested_quantity=10)

    assert res.is_full_fill is False
    assert res.is_partial_fill is True
    assert res.supported_quantity == 5
    assert res.unfilled_quantity == 5
    assert res.total_cost_cents == 200
    assert res.status == "PARTIAL_FILL"
    assert "requested 10, but only 5 available" in res.rejection_reason


def test_depth_traversal_empty_book():
    book = make_book(no_bids=[])
    res = traverse_order_book_depth(book, YES, requested_quantity=10)

    assert res.is_empty_fill is True
    assert res.supported_quantity == 0
    assert res.unfilled_quantity == 10
    assert res.total_cost_cents == 0
    assert res.total_cost_dollars == Decimal("0.00")
    assert res.average_price_cents is None
    assert res.status == "INSUFFICIENT_DEPTH"
    assert "No ask liquidity available" in res.rejection_reason


@pytest.mark.parametrize("invalid_qty", [0, -1, True, False, 1.5, "10", None])
def test_depth_traversal_invalid_quantities_rejected(invalid_qty):
    book = make_book(no_bids=[("0.5000", "10.00")])
    with pytest.raises(ExecutionError):
        traverse_order_book_depth(book, YES, requested_quantity=invalid_qty)


# ---------------------------------------------------------------------------
# Section C: Binary Parity Execution Pricing
# ---------------------------------------------------------------------------


def test_binary_parity_execution_full_depth_supported_profitable():
    # YES asks from NO bids: NO bid @ 0.60 (qty 10) => YES ask @ 40¢ (qty 10)
    # NO asks from YES bids: YES bid @ 0.45 (qty 10) => NO ask @ 55¢ (qty 10)
    # Request 5 pairs:
    # Cost per pair = 40 + 55 = 95¢. Payout = 100¢. Gross edge = 5¢ per pair => 25¢ total.
    book = make_book(
        yes_bids=[("0.4500", "10.00")],
        no_bids=[("0.6000", "10.00")],
    )

    res = evaluate_binary_parity_execution(book, requested_quantity=5)

    assert res.status == STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE
    assert res.is_depth_supported is True
    assert res.is_gross_profitable is True
    assert res.is_net_profitable is None  # no fees
    assert res.requested_quantity == 5
    assert res.supported_quantity == 5
    assert res.unfilled_quantity == 0
    assert res.total_cost_cents == 475  # 5 * 95¢
    assert res.guaranteed_payout_cents == 500  # 5 * 100¢
    assert res.gross_profit_cents == 25
    assert res.payouts_by_outcome == {YES: 500, NO: 500}
    assert res.profits_by_outcome == {YES: 25, NO: 25}
    assert res.rejection_reason is None


def test_binary_parity_execution_with_fees():
    book = make_book(
        yes_bids=[("0.4500", "10.00")],
        no_bids=[("0.6000", "10.00")],
    )

    # 5 pairs = 10 contracts.
    # Gross edge = 25¢.
    # Fee = 2¢/contract => 20¢ fees => net profit = +5¢
    res_profitable = evaluate_binary_parity_execution(book, requested_quantity=5, fee_per_contract_cents=2)
    assert res_profitable.status == STATUS_DEPTH_SUPPORTED_NET_PROFITABLE
    assert res_profitable.is_net_profitable is True
    assert res_profitable.estimated_fees_cents == 20
    assert res_profitable.net_profit_cents == 5

    # Fee = 3¢/contract => 30¢ fees => net profit = -5¢
    res_unprofitable = evaluate_binary_parity_execution(book, requested_quantity=5, fee_per_contract_cents=3)
    assert res_unprofitable.status == STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE
    assert res_unprofitable.is_net_profitable is False
    assert res_unprofitable.estimated_fees_cents == 30
    assert res_unprofitable.net_profit_cents == -5


def test_binary_parity_execution_slippage_eliminates_edge():
    # Top of book has 2 pairs at 40¢ + 55¢ = 95¢ (edge = 5¢/pair).
    # Next level has NO ask at 65¢ (from YES bid 0.35) for 10 contracts.
    # Request 5 pairs:
    # YES: 5 @ 40¢ = 200¢
    # NO: 2 @ 55¢ (110¢) + 3 @ 65¢ (195¢) = 305¢
    # Total cost = 200 + 305 = 505¢. Guaranteed payout = 500¢.
    # Depth slippage makes gross profit = -5¢!
    book = make_book(
        yes_bids=[("0.3500", "10.00"), ("0.4500", "2.00")],
        no_bids=[("0.6000", "10.00")],
    )

    res = evaluate_binary_parity_execution(book, requested_quantity=5)

    assert res.status == STATUS_NO_GROSS_EDGE
    assert res.is_depth_supported is True
    assert res.is_gross_profitable is False
    assert res.gross_profit_cents == -5
    assert "No gross edge" in res.rejection_reason


def test_binary_parity_execution_partial_support_across_asymmetric_depth():
    # YES side has 10 contracts available (NO bid qty 10).
    # NO side has only 4 contracts available (YES bid qty 4).
    # Request 8 pairs: common quantity is min(10, 4) = 4 pairs!
    book = make_book(
        yes_bids=[("0.4500", "4.00")],
        no_bids=[("0.6000", "10.00")],
    )

    res = evaluate_binary_parity_execution(book, requested_quantity=8)

    assert res.status == STATUS_PARTIALLY_SUPPORTED
    assert res.is_depth_supported is False
    assert res.requested_quantity == 8
    assert res.supported_quantity == 4
    assert res.unfilled_quantity == 4
    assert res.total_cost_cents == 4 * 95  # 380¢
    assert res.guaranteed_payout_cents == 400
    assert res.gross_profit_cents == 20
    assert "Partially supported: requested 8 pairs, but order book supports only 4 pairs" in res.rejection_reason


def test_binary_parity_execution_insufficient_depth_zero_fill():
    # YES side has 10 contracts, but NO side has 0 contracts (empty yes_bids)
    book = make_book(
        yes_bids=[],
        no_bids=[("0.6000", "10.00")],
    )

    res = evaluate_binary_parity_execution(book, requested_quantity=5)

    assert res.status == STATUS_INSUFFICIENT_DEPTH
    assert res.supported_quantity == 0
    assert res.unfilled_quantity == 5
    assert res.total_cost_cents == 0
    assert "cannot form complete binary pairs" in res.rejection_reason


# ---------------------------------------------------------------------------
# Section D: MECE Event Basket Execution Pricing
# ---------------------------------------------------------------------------


def test_mece_basket_execution_full_depth_supported():
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)

    # 3 outcomes: to buy YES at 30¢, we need NO bids at 70¢
    books = {
        "A": make_book("M-A", no_bids=[("0.7000", "10.00")]),
        "B": make_book("M-B", no_bids=[("0.7000", "10.00")]),
        "C": make_book("M-C", no_bids=[("0.7000", "10.00")]),
    }

    # Request 5 baskets:
    # Cost = 5 * (30 + 30 + 30) = 450¢
    # Guaranteed payout = 5 * 100 = 500¢
    # Gross edge = 50¢
    res = evaluate_mece_basket_execution(event, books, basket_side=YES, requested_quantity=5)

    assert res.status == STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE
    assert res.is_depth_supported is True
    assert res.supported_quantity == 5
    assert res.total_cost_cents == 450
    assert res.guaranteed_payout_cents == 500
    assert res.gross_profit_cents == 50
    assert res.payouts_by_outcome == {"A": 500, "B": 500, "C": 500}


def test_mece_basket_execution_bottleneck_by_shallow_leg():
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)

    # Outcome C only has 3 contracts available!
    books = {
        "A": make_book("M-A", no_bids=[("0.7000", "10.00")]),
        "B": make_book("M-B", no_bids=[("0.7000", "10.00")]),
        "C": make_book("M-C", no_bids=[("0.7000", "3.00")]),
    }

    res = evaluate_mece_basket_execution(event, books, basket_side=YES, requested_quantity=5)

    assert res.status == STATUS_PARTIALLY_SUPPORTED
    assert res.is_depth_supported is False
    assert res.supported_quantity == 3
    assert res.unfilled_quantity == 2
    assert res.total_cost_cents == 3 * 90  # 270¢
    assert res.guaranteed_payout_cents == 300
    assert res.gross_profit_cents == 30


def test_mece_basket_execution_undeclared_relationship_rejected():
    event = Event(identifier="ELECTION", outcomes=("A", "B"), relationship_established=False)
    books = {
        "A": make_book("M-A", no_bids=[("0.7000", "10.00")]),
        "B": make_book("M-B", no_bids=[("0.7000", "10.00")]),
    }

    res = evaluate_mece_basket_execution(event, books, basket_side=YES, requested_quantity=2)

    assert res.status == STATUS_INVALID_INPUT
    assert "Undeclared outcome relationship" in res.rejection_reason


def test_mece_basket_execution_incomplete_books_rejected():
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)
    # Missing book for C
    books = {
        "A": make_book("M-A", no_bids=[("0.7000", "10.00")]),
        "B": make_book("M-B", no_bids=[("0.7000", "10.00")]),
    }

    res = evaluate_mece_basket_execution(event, books, basket_side=YES, requested_quantity=2)

    assert res.status == STATUS_INVALID_INPUT
    assert "order book missing for outcome" in res.rejection_reason


def test_mece_basket_execution_no_side():
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)

    # 3 outcomes: to buy NO at 30¢/20¢/40¢, we need YES bids at 70¢/80¢/60¢
    books = {
        "A": make_book("M-A", yes_bids=[("0.7000", "10.00")]),  # NO ask = 30¢, qty 10
        "B": make_book("M-B", yes_bids=[("0.8000", "10.00")]),  # NO ask = 20¢, qty 10
        "C": make_book("M-C", yes_bids=[("0.6000", "10.00")]),  # NO ask = 40¢, qty 10
    }

    # Request 4 baskets:
    # Cost per basket = 30 + 20 + 40 = 90¢
    # Total cost = 4 * 90 = 360¢
    # In MECE 3-outcome event, 1 occurs, so (3-1) = 2 settle to NO.
    # Guaranteed payout per basket = 2 * 100 = 200¢.
    # Total guaranteed payout = 4 * 200 = 800¢.
    # Gross profit = 800 - 360 = 440¢.
    # Total filled contracts = 4 units * 3 legs = 12 contracts.
    # Fee = 2¢/contract => 24¢ fee. Net profit = 416¢.
    res = evaluate_mece_basket_execution(
        event,
        books,
        basket_side=NO,
        requested_quantity=4,
        fee_per_contract_cents=2,
    )

    assert res.status == STATUS_DEPTH_SUPPORTED_NET_PROFITABLE
    assert res.is_depth_supported is True
    assert res.is_gross_profitable is True
    assert res.is_net_profitable is True
    assert res.supported_quantity == 4
    assert res.total_cost_cents == 360
    assert res.guaranteed_payout_cents == 800
    assert res.gross_profit_cents == 440
    assert res.estimated_fees_cents == 24
    assert res.net_profit_cents == 416
    assert res.payouts_by_outcome == {"A": 800, "B": 800, "C": 800}


# ---------------------------------------------------------------------------
# Section E: Portfolio Execution Pricing
# ---------------------------------------------------------------------------


def _make_test_portfolio():
    event = Event(identifier="BINARY_EVENT", outcomes=("OUTCOME_1", "OUTCOME_2"), relationship_established=True)
    c1 = BinaryContract(identifier="C1", price_cents=50, yes_outcome="OUTCOME_1")
    c2 = BinaryContract(identifier="C2", price_cents=50, yes_outcome="OUTCOME_2")
    pos1 = Position(contract_id="C1", side=YES, quantity=1)
    pos2 = Position(contract_id="C2", side=YES, quantity=1)
    portfolio = Portfolio(
        event=event,
        contracts=(c1, c2),
        positions=(pos1, pos2),
    )
    return portfolio


def test_portfolio_execution_full_depth_supported_net_profitable():
    portfolio = _make_test_portfolio()

    # Books for C1 and C2
    books = {
        "C1": make_book("M-C1", no_bids=[("0.5500", "20.00")]),  # YES ask at 45¢, qty 20
        "C2": make_book("M-C2", no_bids=[("0.5500", "20.00")]),  # YES ask at 45¢, qty 20
    }

    res = evaluate_portfolio_execution(
        portfolio,
        books,
        unit_quantity=10,
        fee_per_contract_cents=1,
    )

    assert res.status == STATUS_DEPTH_SUPPORTED_NET_PROFITABLE
    assert res.is_depth_supported is True
    assert res.is_gross_profitable is True
    assert res.is_net_profitable is True
    assert res.supported_quantity == 10
    assert res.unfilled_quantity == 0
    # Cost = 10 * 45 + 10 * 45 = 900¢
    assert res.total_cost_cents == 900
    # Exactly one outcome occurs: payout = 10 * 100 = 1000¢
    assert res.guaranteed_payout_cents == 1000
    assert res.gross_profit_cents == 100
    # Fees = 20 contracts * 1¢ = 20¢
    assert res.estimated_fees_cents == 20
    assert res.net_profit_cents == 80


def test_portfolio_execution_bottleneck_by_position_scale():
    event = Event(identifier="EV", outcomes=("O1", "O2"), relationship_established=True)
    c1 = BinaryContract(identifier="C1", price_cents=50, yes_outcome="O1")
    c2 = BinaryContract(identifier="C2", price_cents=50, yes_outcome="O2")
    # Position 1 requires 2 contracts per unit; Position 2 requires 3 contracts per unit
    pos1 = Position(contract_id="C1", side=YES, quantity=2)
    pos2 = Position(contract_id="C2", side=YES, quantity=3)
    portfolio = Portfolio(
        event=event,
        contracts=(c1, c2),
        positions=(pos1, pos2),
    )

    # Books:
    # C1 has 20 contracts -> supports 20 // 2 = 10 units
    # C2 has 9 contracts -> supports 9 // 3 = 3 units
    books = {
        "C1": make_book("M-C1", no_bids=[("0.7000", "20.00")]),  # YES ask at 30¢
        "C2": make_book("M-C2", no_bids=[("0.7000", "9.00")]),   # YES ask at 30¢
    }

    res = evaluate_portfolio_execution(
        portfolio,
        books,
        unit_quantity=5,
    )

    assert res.status == STATUS_PARTIALLY_SUPPORTED
    assert res.is_depth_supported is False
    assert res.supported_quantity == 3
    assert res.unfilled_quantity == 2
    # C1 filled = 3 units * 2 = 6 contracts @ 30¢ = 180¢
    # C2 filled = 3 units * 3 = 9 contracts @ 30¢ = 270¢
    assert res.total_cost_cents == 450
    assert res.guaranteed_payout_cents == 600
    assert res.gross_profit_cents == 150
    assert res.leg_traversals[0].supported_quantity == 6
    assert res.leg_traversals[1].supported_quantity == 9


def test_portfolio_execution_insufficient_depth():
    portfolio = _make_test_portfolio()
    # Book for C2 is empty
    books = {
        "C1": make_book("M-C1", no_bids=[("0.5500", "20.00")]),
        "C2": make_book("M-C2", no_bids=[]),
    }

    res = evaluate_portfolio_execution(portfolio, books, unit_quantity=5)

    assert res.status == STATUS_INSUFFICIENT_DEPTH
    assert res.supported_quantity == 0
    assert res.unfilled_quantity == 5
    assert res.total_cost_cents == 0


def test_portfolio_execution_fee_erosion_to_net_unprofitable():
    portfolio = _make_test_portfolio()
    # Each contract costs 48¢, total cost = 96¢ for 1 unit (2 contracts).
    # Guaranteed payout = 100¢.
    # Gross edge = 4¢.
    # Fee = 3¢ per contract => 2 contracts * 3¢ = 6¢ fees.
    # Net profit = 4 - 6 = -2¢.
    books = {
        "C1": make_book("M-C1", no_bids=[("0.5200", "10.00")]),  # 48¢
        "C2": make_book("M-C2", no_bids=[("0.5200", "10.00")]),  # 48¢
    }

    res = evaluate_portfolio_execution(
        portfolio,
        books,
        unit_quantity=1,
        fee_per_contract_cents=3,
    )

    assert res.status == STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE
    assert res.is_depth_supported is True
    assert res.is_gross_profitable is True
    assert res.is_net_profitable is False
    assert res.gross_profit_cents == 4
    assert res.estimated_fees_cents == 6
    assert res.net_profit_cents == -2


def test_portfolio_execution_rejects_undeclared_relationship():
    event = Event(identifier="EV", outcomes=("O1", "O2"), relationship_established=False)
    c1 = BinaryContract(identifier="C1", price_cents=50, yes_outcome="O1")
    c2 = BinaryContract(identifier="C2", price_cents=50, yes_outcome="O2")
    portfolio = Portfolio(
        event=event,
        contracts=(c1, c2),
        positions=(Position("C1", YES, 1), Position("C2", YES, 1)),
    )
    books = {
        "C1": make_book("M-C1", no_bids=[("0.5000", "10.00")]),
        "C2": make_book("M-C2", no_bids=[("0.5000", "10.00")]),
    }

    res = evaluate_portfolio_execution(portfolio, books, unit_quantity=1)
    assert res.status == STATUS_INVALID_INPUT
    assert "Undeclared outcome relationship" in res.rejection_reason


def test_portfolio_execution_rejects_missing_book():
    portfolio = _make_test_portfolio()
    books = {
        "C1": make_book("M-C1", no_bids=[("0.5000", "10.00")]),
        # C2 missing
    }
    res = evaluate_portfolio_execution(portfolio, books, unit_quantity=1)
    assert res.status == STATUS_INVALID_INPUT
    assert "Order book missing for contract 'C2'" in res.rejection_reason


def test_portfolio_execution_input_validation():
    portfolio = _make_test_portfolio()
    books = {
        "C1": make_book("M-C1", no_bids=[("0.5000", "10.00")]),
        "C2": make_book("M-C2", no_bids=[("0.5000", "10.00")]),
    }

    with pytest.raises(ExecutionError, match="portfolio must be a Portfolio"):
        evaluate_portfolio_execution("not_a_portfolio", books)

    with pytest.raises(ExecutionError, match="books_by_contract must be a mapping"):
        evaluate_portfolio_execution(portfolio, ["not_a_map"])

    with pytest.raises(ExecutionError, match="unit_quantity must be positive"):
        evaluate_portfolio_execution(portfolio, books, unit_quantity=0)

    with pytest.raises(ExecutionError, match="fee_per_contract_cents must be non-negative"):
        evaluate_portfolio_execution(portfolio, books, unit_quantity=1, fee_per_contract_cents=-1)


# ---------------------------------------------------------------------------
# Section F: Large Quantities & Multi-level Traversal
# ---------------------------------------------------------------------------


def test_large_requested_quantity_depth_exhaustion():
    # Request 10,000 contracts when book only has 50 contracts across 2 levels
    book = make_book(
        no_bids=[
            ("0.5500", "30.00"),  # YES ask 45¢, qty 30
            ("0.6000", "20.00"),  # YES ask 40¢, qty 20
        ]
    )

    res = traverse_order_book_depth(book, YES, requested_quantity=10000)

    assert res.is_full_fill is False
    assert res.requested_quantity == 10000
    assert res.supported_quantity == 50
    assert res.unfilled_quantity == 9950
    # Cost = 20 * 40 + 30 * 45 = 800 + 1350 = 2150¢
    assert res.total_cost_cents == 2150
    assert len(res.consumed_levels) == 2


