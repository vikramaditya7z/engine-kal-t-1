"""Deterministic binary contract and settlement payoff calculations."""

from dataclasses import dataclass
from typing import Optional


class ContractInputError(ValueError):
    """Raised when a contract or position input is invalid."""


YES = "YES"
NO = "NO"
_OUTCOMES = frozenset((YES, NO))


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractInputError(f"{name} must be an integer")
    return value


def _require_cents(value: object, name: str) -> int:
    value = _require_int(value, name)
    if not 0 <= value <= 100:
        raise ContractInputError(f"{name} must be between 0 and 100 cents")
    return value


def _require_outcome(value: object, name: str) -> str:
    if not isinstance(value, str) or value not in _OUTCOMES:
        raise ContractInputError(f"{name} must be 'YES' or 'NO'")
    return value


@dataclass(frozen=True)
class BinaryContract:
    """A binary contract whose monetary values are represented in cents.

    The default payoff is the standard binary payoff: 100 cents for the
    winning side and 0 cents for the losing side. Custom payoffs are allowed
    only to keep the mathematical model explicit; all values remain bounded
    to the 0-100 cent contract unit.
    """

    identifier: str
    price_cents: int
    yes_payout_cents: int = 100
    no_payout_cents: int = 100
    yes_outcome: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier:
            raise ContractInputError("identifier must be a non-empty string")
        _require_cents(self.price_cents, "price_cents")
        _require_cents(self.yes_payout_cents, "yes_payout_cents")
        _require_cents(self.no_payout_cents, "no_payout_cents")
        if self.yes_outcome is not None and (
            not isinstance(self.yes_outcome, str) or not self.yes_outcome
        ):
            raise ContractInputError("yes_outcome must be a non-empty string")

    def payout_cents(self, settlement_outcome: str) -> int:
        """Return the configured payout for the winning YES or NO side."""
        settlement_outcome = _require_outcome(settlement_outcome, "settlement_outcome")
        return (
            self.yes_payout_cents
            if settlement_outcome == YES
            else self.no_payout_cents
        )


def gross_profit_loss_cents(
    contract: BinaryContract,
    side: str,
    quantity: int,
    entry_price_cents: int,
    settlement_outcome: str,
) -> int:
    """Calculate gross settlement P/L in cents, excluding fees.

    ``side`` is the position held (``YES`` or ``NO``). A NO position receives
    the contract's NO payout when NO settles, and receives zero when YES
    settles. Entry price is the price paid for the held side per contract.
    """
    if not isinstance(contract, BinaryContract):
        raise ContractInputError("contract must be a BinaryContract")
    side = _require_outcome(side, "side")
    quantity = _require_int(quantity, "quantity")
    if quantity <= 0:
        raise ContractInputError("quantity must be positive")
    entry_price_cents = _require_cents(entry_price_cents, "entry_price_cents")
    settlement_outcome = _require_outcome(settlement_outcome, "settlement_outcome")

    winning_side = settlement_outcome
    payout_per_contract = (
        contract.payout_cents(settlement_outcome)
        if side == winning_side
        else 0
    )
    return quantity * (payout_per_contract - entry_price_cents)
