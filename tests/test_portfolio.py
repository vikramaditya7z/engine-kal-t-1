import pytest

from kalshi_arbitrage import (
    BinaryContract,
    ContractInputError,
    Event,
    Position,
    Portfolio,
    gross_profit_loss_cents,
    portfolio_payout_cents,
    portfolio_payouts_cents,
)


OUTCOMES = ("A", "B", "C")


def make_contracts():
    return tuple(
        BinaryContract(identifier=outcome, price_cents=40, yes_outcome=outcome)
        for outcome in OUTCOMES
    )


def make_portfolio(positions, relationship_established=True):
    event = Event("ELECTION", OUTCOMES, relationship_established)
    return Portfolio(event, make_contracts(), tuple(positions))


def test_yes_on_all_three_outcomes_pays_100_in_every_state():
    portfolio = make_portfolio(
        [Position(outcome, "YES", 1) for outcome in OUTCOMES]
    )
    assert portfolio_payouts_cents(portfolio) == {"A": 100, "B": 100, "C": 100}


def test_no_on_all_three_outcomes_pays_200_in_every_state():
    portfolio = make_portfolio(
        [Position(outcome, "NO", 1) for outcome in OUTCOMES]
    )
    assert portfolio_payouts_cents(portfolio) == {"A": 200, "B": 200, "C": 200}


def test_mixed_portfolio_has_expected_payout_for_each_state():
    portfolio = make_portfolio(
        [Position("A", "YES", 1), Position("B", "NO", 1), Position("C", "YES", 2)]
    )
    assert portfolio_payouts_cents(portfolio) == {"A": 200, "B": 0, "C": 300}


def test_individual_position_payout_matches_single_contract_logic():
    portfolio = make_portfolio([Position("A", "YES", 1)])
    contract = portfolio.contracts[0]
    assert portfolio_payout_cents(portfolio, "A") == (
        gross_profit_loss_cents(contract, "YES", 1, 40, "YES") + 40
    )
    assert portfolio_payout_cents(portfolio, "B") == (
        gross_profit_loss_cents(contract, "YES", 1, 40, "NO") + 40
    )


def test_no_position_payout_matches_single_contract_logic():
    portfolio = make_portfolio([Position("A", "NO", 1)])
    contract = portfolio.contracts[0]
    assert portfolio_payout_cents(portfolio, "A") == (
        gross_profit_loss_cents(contract, "NO", 1, 35, "YES") + 35
    )
    assert portfolio_payout_cents(portfolio, "B") == (
        gross_profit_loss_cents(contract, "NO", 1, 35, "NO") + 35
    )


def test_each_valid_event_state_is_evaluated_once_and_in_event_order():
    portfolio = make_portfolio([Position("A", "YES", 1)])
    payouts = portfolio_payouts_cents(portfolio)
    assert list(payouts) == list(OUTCOMES)
    assert len(payouts) == len(OUTCOMES)


def test_unknown_event_outcomes_are_rejected():
    portfolio = make_portfolio([Position("A", "YES", 1)])
    with pytest.raises(ContractInputError):
        portfolio_payout_cents(portfolio, "D")


@pytest.mark.parametrize(
    "outcomes",
    [(), ("A", "A"), ("A", "")],
)
def test_invalid_event_outcomes_are_rejected(outcomes):
    with pytest.raises(ContractInputError):
        Event("E", outcomes, True)


def test_invalid_event_identifier_and_relationship_declaration_are_rejected():
    with pytest.raises(ContractInputError):
        Event("", OUTCOMES, True)
    with pytest.raises(ContractInputError):
        Event(None, OUTCOMES, True)
    with pytest.raises(ContractInputError):
        Event("E", OUTCOMES, "yes")


def test_contracts_and_positions_must_be_valid_for_the_event():
    event = Event("E", OUTCOMES, True)
    with pytest.raises(ContractInputError):
        Portfolio(event, (BinaryContract("A", 40),), (Position("A", "YES", 1),))
    with pytest.raises(ContractInputError):
        Portfolio(
            event,
            (BinaryContract("D", 40, yes_outcome="D"),),
            (Position("D", "YES", 1),),
        )
    with pytest.raises(ContractInputError):
        Portfolio(
            event,
            make_contracts(),
            (Position("UNKNOWN", "YES", 1),),
        )
    with pytest.raises(ContractInputError):
        Portfolio(
            event,
            make_contracts(),
            (Position("A", "YES", 1), Position("A", "NO", 1)),
        )
    with pytest.raises(ContractInputError):
        Portfolio(
            event,
            (make_contracts()[0], make_contracts()[0]),
            (Position("A", "YES", 1),),
        )


@pytest.mark.parametrize("side", ["MAYBE", "", None, 1])
def test_invalid_position_sides_are_rejected(side: object):
    with pytest.raises(ContractInputError):
        Position("A", side, 1)


@pytest.mark.parametrize("quantity", [True, 1.5, None])
def test_non_integer_quantities_are_rejected(quantity: object):
    with pytest.raises(ContractInputError):
        Position("A", "YES", quantity)


def test_invalid_position_identifiers_are_rejected():
    with pytest.raises(ContractInputError):
        Position("", "YES", 1)
    with pytest.raises(ContractInputError):
        Position(None, "YES", 1)


def test_non_iterable_portfolio_collections_are_rejected():
    event = Event("E", OUTCOMES, True)
    with pytest.raises(ContractInputError):
        Portfolio(event, None, ())
    with pytest.raises(ContractInputError):
        Portfolio(event, (), None)


@pytest.mark.parametrize("quantity", [0, -1])
def test_non_positive_quantities_are_rejected(quantity):
    with pytest.raises(ContractInputError):
        Position("A", "YES", quantity)


def test_exhaustive_evaluation_requires_explicit_relationship_declaration():
    portfolio = make_portfolio(
        [Position("A", "YES", 1)], relationship_established=False
    )
    with pytest.raises(ContractInputError):
        portfolio_payouts_cents(portfolio)
