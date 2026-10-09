from decimal import Decimal
from typing import Optional
import pytest

from kalshi_arbitrage import (
    BinaryContract,
    Event,
    NormalizedMarket,
    Portfolio,
    Position,
    dollars_to_cents,
    cents_to_dollars,
    evaluate_binary_parity,
    evaluate_market_parity,
    evaluate_mece_event_basket,
    evaluate_mece_markets,
    evaluate_portfolio,
    ArbitrageInputError,
    ArbitrageOpportunity,
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_PORTFOLIO,
)


def sample_normalized_market(
    ticker: str = "KXTEST-1",
    event_ticker: str = "KXTEST",
    yes_ask: Optional[str] = "0.4000",
    no_ask: Optional[str] = "0.5500",
    market_type: str = "binary",
    status: str = "active",
) -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type=market_type,
        status=status,
        title=f"Market {ticker}",
        yes_ask_dollars=Decimal(yes_ask) if yes_ask is not None else None,
        no_ask_dollars=Decimal(no_ask) if no_ask is not None else None,
    )


# ---------------------------------------------------------------------------
# 1. Price conversion & Exact arithmetic boundary tests
# ---------------------------------------------------------------------------


def test_dollars_to_cents_exact_conversion():
    assert dollars_to_cents(Decimal("0.00")) == 0
    assert dollars_to_cents(Decimal("0.40")) == 40
    assert dollars_to_cents(Decimal("0.4000")) == 40
    assert dollars_to_cents(Decimal("0.99")) == 99
    assert dollars_to_cents(Decimal("1.00")) == 100
    assert dollars_to_cents("0.40") == 40
    assert dollars_to_cents(1) == 100
    assert dollars_to_cents(0) == 0


def test_dollars_to_cents_rejects_subcent_precision():
    # Sub-cent prices must fail rather than silently round or truncate
    with pytest.raises(ArbitrageInputError, match="sub-cent price"):
        dollars_to_cents(Decimal("0.4050"))
    with pytest.raises(ArbitrageInputError, match="sub-cent price"):
        dollars_to_cents("0.1234")


@pytest.mark.parametrize("invalid_dollars", [-0.01, 1.01, "bad", True, False, 0.5, Decimal("-0.01"), Decimal("1.0001")])
def test_dollars_to_cents_rejects_invalid_and_out_of_bounds(invalid_dollars):
    with pytest.raises(ArbitrageInputError):
        dollars_to_cents(invalid_dollars)


def test_cents_to_dollars_exact_conversion():
    assert cents_to_dollars(0) == Decimal("0.00")
    assert cents_to_dollars(40) == Decimal("0.40")
    assert cents_to_dollars(100) == Decimal("1.00")
    assert cents_to_dollars(125) == Decimal("1.25")
    assert cents_to_dollars(-50) == Decimal("-0.50")


@pytest.mark.parametrize("invalid_cents", [True, False, 40.5, "40", None])
def test_cents_to_dollars_rejects_invalid_types(invalid_cents):
    with pytest.raises(ArbitrageInputError):
        cents_to_dollars(invalid_cents)


# ---------------------------------------------------------------------------
# 2. Binary parity arbitrage tests
# ---------------------------------------------------------------------------


def test_binary_parity_valid_arbitrage():
    # 40¢ + 55¢ = 95¢ < 100¢ => gross edge = 5¢
    opp = evaluate_binary_parity(market_id="KXTEST-1", yes_price_cents=40, no_price_cents=55)

    assert opp.is_arbitrage is True
    assert opp.opportunity_type == OPPORTUNITY_BINARY_PARITY
    assert opp.total_cost_cents == 95
    assert opp.guaranteed_payout_cents == 100
    assert opp.gross_edge_cents == 5
    assert opp.total_cost_dollars == Decimal("0.95")
    assert opp.guaranteed_payout_dollars == Decimal("1.00")
    assert opp.gross_edge_dollars == Decimal("0.05")
    assert opp.payouts_by_outcome == {"YES": 100, "NO": 100}
    assert opp.profits_by_outcome == {"YES": 5, "NO": 5}
    assert opp.rejection_reason is None
    assert opp.is_executable is False
    assert opp.is_net_profitable is None  # no fees specified
    assert "Executability and net profitability are unverified" in opp.qualification_notes[0]


def test_binary_parity_scaled_quantity():
    # Quantity 5: cost = 5 * 95 = 475¢, payout = 500¢, edge = 25¢
    opp = evaluate_binary_parity(
        market_id="KXTEST-1",
        yes_price_cents=40,
        no_price_cents=55,
        quantity=5,
    )
    assert opp.is_arbitrage is True
    assert opp.total_cost_cents == 475
    assert opp.guaranteed_payout_cents == 500
    assert opp.gross_edge_cents == 25
    assert opp.payouts_by_outcome == {"YES": 500, "NO": 500}
    assert opp.profits_by_outcome == {"YES": 25, "NO": 25}


def test_binary_parity_no_edge():
    # 50¢ + 55¢ = 105¢ > 100¢ => gross edge = -5¢
    opp = evaluate_binary_parity(market_id="KXTEST-1", yes_price_cents=50, no_price_cents=55)

    assert opp.is_arbitrage is False
    assert opp.gross_edge_cents == -5
    assert opp.rejection_reason is not None
    assert "total cost (105¢) exceeds guaranteed payout (100¢)" in opp.rejection_reason


def test_binary_parity_break_even():
    # 40¢ + 60¢ = 100¢ => gross edge = 0¢
    opp = evaluate_binary_parity(market_id="KXTEST-1", yes_price_cents=40, no_price_cents=60)

    assert opp.is_arbitrage is False
    assert opp.gross_edge_cents == 0
    assert opp.rejection_reason is not None
    assert "Break-even" in opp.rejection_reason


def test_binary_parity_with_fees():
    # Gross edge = 5¢ across 2 contracts (1 YES + 1 NO).
    # Fee = 2¢ per contract => total fees = 4¢ => net edge = +1¢
    opp_profitable = evaluate_binary_parity(
        market_id="KXTEST-1",
        yes_price_cents=40,
        no_price_cents=55,
        fee_per_contract_cents=2,
    )
    assert opp_profitable.is_arbitrage is True
    assert opp_profitable.gross_edge_cents == 5
    assert opp_profitable.estimated_fees_cents == 4
    assert opp_profitable.net_edge_cents == 1
    assert opp_profitable.net_edge_dollars == Decimal("0.01")
    assert opp_profitable.is_net_profitable is True

    # Fee = 3¢ per contract => total fees = 6¢ => net edge = -1¢
    opp_unprofitable = evaluate_binary_parity(
        market_id="KXTEST-1",
        yes_price_cents=40,
        no_price_cents=55,
        fee_per_contract_cents=3,
    )
    assert opp_unprofitable.is_arbitrage is True  # gross edge is still positive
    assert opp_unprofitable.gross_edge_cents == 5
    assert opp_unprofitable.estimated_fees_cents == 6
    assert opp_unprofitable.net_edge_cents == -1
    assert opp_unprofitable.is_net_profitable is False


# ---------------------------------------------------------------------------
# 3. NormalizedMarket integration tests
# ---------------------------------------------------------------------------


def test_evaluate_market_parity_success():
    market = sample_normalized_market(yes_ask="0.4000", no_ask="0.5500")
    opp = evaluate_market_parity(market)
    assert opp.is_arbitrage is True
    assert opp.total_cost_cents == 95
    assert opp.gross_edge_cents == 5


def test_evaluate_market_parity_missing_quotes():
    market_missing_yes = sample_normalized_market(yes_ask=None, no_ask="0.5500")
    opp1 = evaluate_market_parity(market_missing_yes)
    assert opp1.is_arbitrage is False
    assert "missing yes_ask_dollars" in opp1.rejection_reason

    market_missing_both = sample_normalized_market(yes_ask=None, no_ask=None)
    opp2 = evaluate_market_parity(market_missing_both)
    assert opp2.is_arbitrage is False
    assert "missing yes_ask_dollars, no_ask_dollars" in opp2.rejection_reason


def test_evaluate_market_parity_rejects_non_binary():
    market = sample_normalized_market()
    object.__setattr__(market, "market_type", "scalar")
    with pytest.raises(ArbitrageInputError, match="only binary markets are supported"):
        evaluate_market_parity(market)


# ---------------------------------------------------------------------------
# 4. MECE event basket arbitrage tests
# ---------------------------------------------------------------------------


def test_mece_basket_long_yes_valid_arbitrage():
    event = Event(identifier="ELECTION", outcomes=("CANDIDATE_A", "CANDIDATE_B", "CANDIDATE_C"), relationship_established=True)
    prices = {"CANDIDATE_A": 30, "CANDIDATE_B": 30, "CANDIDATE_C": 30}

    opp = evaluate_mece_event_basket(event=event, outcome_prices_cents=prices, basket_side="YES")

    assert opp.is_arbitrage is True
    assert opp.opportunity_type == OPPORTUNITY_MECE_BASKET_LONG_YES
    assert opp.total_cost_cents == 90
    assert opp.guaranteed_payout_cents == 100
    assert opp.gross_edge_cents == 10
    assert opp.payouts_by_outcome == {"CANDIDATE_A": 100, "CANDIDATE_B": 100, "CANDIDATE_C": 100}
    assert opp.profits_by_outcome == {"CANDIDATE_A": 10, "CANDIDATE_B": 10, "CANDIDATE_C": 10}


def test_mece_basket_long_no_valid_arbitrage():
    # 3 outcomes: Long NO pays (3 - 1) * 100 = 200¢ in all states.
    # Cost = 60 + 60 + 60 = 180¢ => edge = 20¢
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)
    prices = {"A": 60, "B": 60, "C": 60}

    opp = evaluate_mece_event_basket(event=event, outcome_prices_cents=prices, basket_side="NO")

    assert opp.is_arbitrage is True
    assert opp.opportunity_type == OPPORTUNITY_MECE_BASKET_LONG_NO
    assert opp.total_cost_cents == 180
    assert opp.guaranteed_payout_cents == 200
    assert opp.gross_edge_cents == 20
    assert opp.payouts_by_outcome == {"A": 200, "B": 200, "C": 200}
    assert opp.profits_by_outcome == {"A": 20, "B": 20, "C": 20}


def test_mece_basket_no_edge_and_breakeven():
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)

    # Break-even: 30 + 30 + 40 = 100¢
    opp_be = evaluate_mece_event_basket(event=event, outcome_prices_cents={"A": 30, "B": 30, "C": 40})
    assert opp_be.is_arbitrage is False
    assert opp_be.gross_edge_cents == 0
    assert "Break-even" in opp_be.rejection_reason

    # Loss: 35 + 35 + 35 = 105¢
    opp_loss = evaluate_mece_event_basket(event=event, outcome_prices_cents={"A": 35, "B": 35, "C": 35})
    assert opp_loss.is_arbitrage is False
    assert opp_loss.gross_edge_cents == -5
    assert "total cost (105¢) exceeds guaranteed payout (100¢)" in opp_loss.rejection_reason


def test_mece_basket_rejects_undeclared_relationship():
    # relationship_established=False MUST produce an explicit rejection
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=False)
    prices = {"A": 30, "B": 30, "C": 30}

    opp = evaluate_mece_event_basket(event=event, outcome_prices_cents=prices)

    assert opp.is_arbitrage is False
    assert opp.rejection_reason is not None
    assert "Undeclared outcome relationship" in opp.rejection_reason


def test_mece_basket_rejects_incomplete_basket():
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)
    # Missing outcome "C"
    prices = {"A": 30, "B": 30}

    opp = evaluate_mece_event_basket(event=event, outcome_prices_cents=prices)

    assert opp.is_arbitrage is False
    assert opp.rejection_reason is not None
    assert "Incomplete basket" in opp.rejection_reason
    assert "C" in opp.rejection_reason


def test_mece_markets_integration():
    event = Event(identifier="SPORTS", outcomes=("TEAM_1", "TEAM_2"), relationship_established=True)
    m1 = sample_normalized_market(ticker="KX-T1", yes_ask="0.4500", no_ask="0.5500")
    m2 = sample_normalized_market(ticker="KX-T2", yes_ask="0.5000", no_ask="0.5000")

    # Mapping explicitly links market tickers to event outcomes
    market_map = {"KX-T1": "TEAM_1", "KX-T2": "TEAM_2"}

    # 45¢ + 50¢ = 95¢ => edge = 5¢
    opp = evaluate_mece_markets(event=event, markets=[m1, m2], market_outcome_map=market_map, basket_side="YES")
    assert opp.is_arbitrage is True
    assert opp.gross_edge_cents == 5
    assert opp.total_cost_cents == 95

    # Missing quotes on one market
    m2_missing = sample_normalized_market(ticker="KX-T2", yes_ask=None, no_ask="0.5000")
    opp_missing = evaluate_mece_markets(event=event, markets=[m1, m2_missing], market_outcome_map=market_map)
    assert opp_missing.is_arbitrage is False
    assert "Incomplete market quotes" in opp_missing.rejection_reason


# ---------------------------------------------------------------------------
# 5. General Portfolio evaluation & "Loss in one outcome" scenario
# ---------------------------------------------------------------------------


def test_portfolio_arbitrage_losing_scenario_rejected():
    """Test scenario where an apparently attractive price sum loses money in one state."""
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)

    contracts = (
        BinaryContract(identifier="A", price_cents=30, yes_outcome="A"),
        BinaryContract(identifier="B", price_cents=30, yes_outcome="B"),
        BinaryContract(identifier="C", price_cents=30, yes_outcome="C"),
    )

    # Buy YES on A (30¢) and YES on B (30¢). Total cost = 60¢.
    # Apparent temptation: if A wins, payout is 100¢ (+40¢). If B wins, payout is 100¢ (+40¢).
    # BUT if C wins: payout is 0¢! Loss = -60¢!
    positions = (
        Position(contract_id="A", side="YES", quantity=1),
        Position(contract_id="B", side="YES", quantity=1),
    )
    portfolio = Portfolio(event=event, contracts=contracts, positions=positions)
    entry_prices = {"A": 30, "B": 30}

    opp = evaluate_portfolio(portfolio=portfolio, entry_prices_cents=entry_prices)

    assert opp.is_arbitrage is False
    assert opp.total_cost_cents == 60
    assert opp.guaranteed_payout_cents == 0  # worst-case outcome 'C' pays 0¢
    assert opp.gross_edge_cents == -60
    assert opp.payouts_by_outcome == {"A": 100, "B": 100, "C": 0}
    assert opp.profits_by_outcome == {"A": 40, "B": 40, "C": -60}
    assert opp.rejection_reason is not None
    assert "worst-case outcome 'C' yields -60¢" in opp.rejection_reason


def test_portfolio_arbitrage_valid():
    event = Event(identifier="ELECTION", outcomes=("A", "B", "C"), relationship_established=True)
    contracts = (
        BinaryContract(identifier="A", price_cents=30, yes_outcome="A"),
        BinaryContract(identifier="B", price_cents=30, yes_outcome="B"),
        BinaryContract(identifier="C", price_cents=30, yes_outcome="C"),
    )
    # Buy YES on all 3 outcomes at 30¢ each (total cost 90¢).
    positions = (
        Position(contract_id="A", side="YES", quantity=1),
        Position(contract_id="B", side="YES", quantity=1),
        Position(contract_id="C", side="YES", quantity=1),
    )
    portfolio = Portfolio(event=event, contracts=contracts, positions=positions)
    entry_prices = {"A": 30, "B": 30, "C": 30}

    opp = evaluate_portfolio(portfolio=portfolio, entry_prices_cents=entry_prices)
    assert opp.is_arbitrage is True
    assert opp.opportunity_type == OPPORTUNITY_PORTFOLIO
    assert opp.guaranteed_payout_cents == 100
    assert opp.total_cost_cents == 90
    assert opp.gross_edge_cents == 10


def test_portfolio_arbitrage_undeclared_relationship_rejected():
    event = Event(identifier="ELECTION", outcomes=("A", "B"), relationship_established=False)
    contracts = (
        BinaryContract(identifier="A", price_cents=30, yes_outcome="A"),
        BinaryContract(identifier="B", price_cents=30, yes_outcome="B"),
    )
    positions = (Position(contract_id="A", side="YES", quantity=1),)
    portfolio = Portfolio(event=event, contracts=contracts, positions=positions)

    opp = evaluate_portfolio(portfolio=portfolio, entry_prices_cents={"A": 30})
    assert opp.is_arbitrage is False
    assert "Undeclared outcome relationship" in opp.rejection_reason


# ---------------------------------------------------------------------------
# 6. Malformed input and error path tests
# ---------------------------------------------------------------------------


def test_binary_parity_input_validation():
    with pytest.raises(ArbitrageInputError, match="market_id"):
        evaluate_binary_parity(market_id="", yes_price_cents=40, no_price_cents=50)

    with pytest.raises(ArbitrageInputError, match="yes_price_cents"):
        evaluate_binary_parity(market_id="KX", yes_price_cents=-1, no_price_cents=50)

    with pytest.raises(ArbitrageInputError, match="yes_price_cents"):
        evaluate_binary_parity(market_id="KX", yes_price_cents=101, no_price_cents=50)

    with pytest.raises(ArbitrageInputError, match="no_price_cents"):
        evaluate_binary_parity(market_id="KX", yes_price_cents=40, no_price_cents=40.5)

    with pytest.raises(ArbitrageInputError, match="quantity"):
        evaluate_binary_parity(market_id="KX", yes_price_cents=40, no_price_cents=50, quantity=0)

    with pytest.raises(ArbitrageInputError, match="fee_per_contract_cents"):
        evaluate_binary_parity(market_id="KX", yes_price_cents=40, no_price_cents=50, fee_per_contract_cents=-1)


def test_portfolio_input_validation():
    event = Event(identifier="E", outcomes=("A", "B"), relationship_established=True)
    contracts = (
        BinaryContract(identifier="A", price_cents=30, yes_outcome="A"),
        BinaryContract(identifier="B", price_cents=30, yes_outcome="B"),
    )
    positions = (Position(contract_id="A", side="YES", quantity=1),)
    portfolio = Portfolio(event=event, contracts=contracts, positions=positions)

    # Missing price in entry_prices_cents
    with pytest.raises(ArbitrageInputError, match="missing entry price"):
        evaluate_portfolio(portfolio=portfolio, entry_prices_cents={})

    # Empty positions
    empty_portfolio = Portfolio(event=event, contracts=contracts, positions=())
    with pytest.raises(ArbitrageInputError, match="at least one position"):
        evaluate_portfolio(portfolio=empty_portfolio, entry_prices_cents={})


def test_mece_markets_input_validation():
    event = Event(identifier="E", outcomes=("A", "B"), relationship_established=True)
    m1 = sample_normalized_market(ticker="M1")

    # Mapped ticker not in markets list
    with pytest.raises(ArbitrageInputError, match="not found in provided markets"):
        evaluate_mece_markets(event=event, markets=[m1], market_outcome_map={"M2": "A"})

    # Mapped outcome not in event
    with pytest.raises(ArbitrageInputError, match="not in event.outcomes"):
        evaluate_mece_markets(event=event, markets=[m1], market_outcome_map={"M1": "UNKNOWN"})
