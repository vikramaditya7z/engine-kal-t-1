import pytest

from kalshi_arbitrage.contract import (
    BinaryContract,
    ContractInputError,
    gross_profit_loss_cents,
)


@pytest.fixture
def standard_contract() -> BinaryContract:
    return BinaryContract(identifier="TEST-BINARY", price_cents=40)


def test_buying_yes_at_40_cents_when_yes_wins(standard_contract: BinaryContract) -> None:
    assert gross_profit_loss_cents(standard_contract, "YES", 1, 40, "YES") == 60


def test_buying_yes_at_40_cents_when_no_wins(standard_contract: BinaryContract) -> None:
    assert gross_profit_loss_cents(standard_contract, "YES", 1, 40, "NO") == -40


def test_buying_no_at_35_cents_when_no_wins(standard_contract: BinaryContract) -> None:
    assert gross_profit_loss_cents(standard_contract, "NO", 1, 35, "NO") == 65


def test_buying_no_at_35_cents_when_yes_wins(standard_contract: BinaryContract) -> None:
    assert gross_profit_loss_cents(standard_contract, "NO", 1, 35, "YES") == -35


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_cents", -1),
        ("price_cents", 101),
        ("price_cents", 40.5),
        ("price_cents", True),
        ("yes_payout_cents", -1),
        ("yes_payout_cents", True),
        ("no_payout_cents", 101),
        ("no_payout_cents", False),
        ("identifier", ""),
    ],
)
def test_invalid_contract_inputs_are_rejected(field: str, value: object) -> None:
    kwargs = {"identifier": "TEST", "price_cents": 40}
    kwargs[field] = value
    with pytest.raises(ContractInputError):
        BinaryContract(**kwargs)


@pytest.mark.parametrize("identifier", [None, 0])
def test_invalid_contract_identifiers_are_rejected(identifier: object) -> None:
    with pytest.raises(ContractInputError):
        BinaryContract(identifier=identifier, price_cents=40)


@pytest.mark.parametrize("yes_outcome", ["", 0, False])
def test_invalid_yes_outcome_mappings_are_rejected(yes_outcome: object) -> None:
    with pytest.raises(ContractInputError):
        BinaryContract(identifier="TEST", price_cents=40, yes_outcome=yes_outcome)


def test_custom_yes_and_no_payouts_are_used_for_the_matching_side() -> None:
    contract = BinaryContract(
        identifier="CUSTOM",
        price_cents=40,
        yes_payout_cents=75,
        no_payout_cents=80,
    )
    assert gross_profit_loss_cents(contract, "YES", 1, 40, "YES") == 35
    assert gross_profit_loss_cents(contract, "NO", 1, 40, "NO") == 40
    assert contract.payout_cents("YES") == 75
    assert contract.payout_cents("NO") == 80


@pytest.mark.parametrize(
    ("side", "quantity", "entry_price", "outcome"),
    [
        ("MAYBE", 1, 40, "YES"),
        ("YES", 0, 40, "YES"),
        ("YES", -1, 40, "YES"),
        ("YES", 1, -1, "YES"),
        ("YES", 1, 101, "YES"),
        ("YES", True, 40, "YES"),
        ("YES", 1, True, "YES"),
        ("YES", 1, 40, "MAYBE"),
        ("YES", 1.5, 40, "YES"),
    ],
)
def test_invalid_position_inputs_are_rejected(
    standard_contract: BinaryContract,
    side: str,
    quantity: object,
    entry_price: object,
    outcome: str,
) -> None:
    with pytest.raises(ContractInputError):
        gross_profit_loss_cents(
            standard_contract, side, quantity, entry_price, outcome
        )


@pytest.mark.parametrize("side", ["MAYBE", "", None, 1])
def test_invalid_position_sides_are_rejected(side: object) -> None:
    with pytest.raises(ContractInputError):
        gross_profit_loss_cents(
            BinaryContract("TEST", 40), side, 1, 40, "YES"
        )


def test_boundary_prices_zero_and_one_hundred_cents_are_supported() -> None:
    contract = BinaryContract(identifier="BOUNDARY", price_cents=0)
    assert gross_profit_loss_cents(contract, "YES", 2, 0, "YES") == 200
    assert gross_profit_loss_cents(contract, "YES", 2, 0, "NO") == 0

    contract = BinaryContract(identifier="BOUNDARY", price_cents=100)
    assert gross_profit_loss_cents(contract, "YES", 2, 100, "YES") == 0
    assert gross_profit_loss_cents(contract, "YES", 2, 100, "NO") == -200
