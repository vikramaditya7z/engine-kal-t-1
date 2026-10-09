"""Finite-outcome event and portfolio settlement payoff models."""

from dataclasses import dataclass
from typing import Iterable, Dict, Tuple

from .contract import BinaryContract, ContractInputError, NO, YES


def _as_non_empty_strings(values: Iterable[str], name: str) -> Tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ContractInputError(f"{name} must be a collection of strings")
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise ContractInputError(f"{name} must be a collection of strings") from exc
    if not normalized or any(not isinstance(value, str) or not value for value in normalized):
        raise ContractInputError(f"{name} must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ContractInputError(f"{name} must contain unique values")
    return normalized


@dataclass(frozen=True)
class Event:
    """A finite event with an explicitly declared settlement relationship."""

    identifier: str
    outcomes: Tuple[str, ...]
    relationship_established: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier:
            raise ContractInputError("event identifier must be a non-empty string")
        object.__setattr__(self, "outcomes", _as_non_empty_strings(self.outcomes, "outcomes"))
        if not isinstance(self.relationship_established, bool):
            raise ContractInputError("relationship_established must be a boolean")


@dataclass(frozen=True)
class Position:
    """An already-acquired YES or NO holding, excluding entry price."""

    contract_id: str
    side: str
    quantity: int

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not self.contract_id:
            raise ContractInputError("contract_id must be a non-empty string")
        if self.side not in (YES, NO):
            raise ContractInputError("side must be 'YES' or 'NO'")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise ContractInputError("quantity must be an integer")
        if self.quantity <= 0:
            raise ContractInputError("quantity must be positive")


@dataclass(frozen=True)
class Portfolio:
    """A set of uniquely identified contracts and positions for one event."""

    event: Event
    contracts: Tuple[BinaryContract, ...]
    positions: Tuple[Position, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.event, Event):
            raise ContractInputError("event must be an Event")

        try:
            contracts = tuple(self.contracts)
        except TypeError as exc:
            raise ContractInputError("contracts must be an iterable") from exc
        try:
            positions = tuple(self.positions)
        except TypeError as exc:
            raise ContractInputError("positions must be an iterable") from exc
        if any(not isinstance(contract, BinaryContract) for contract in contracts):
            raise ContractInputError("contracts must contain BinaryContract objects")
        if any(not isinstance(position, Position) for position in positions):
            raise ContractInputError("positions must contain Position objects")

        contract_ids = [contract.identifier for contract in contracts]
        if len(set(contract_ids)) != len(contract_ids):
            raise ContractInputError("contract identifiers must be unique")
        contracts_by_id = {contract.identifier: contract for contract in contracts}
        for contract in contracts:
            if contract.yes_outcome not in self.event.outcomes:
                raise ContractInputError(
                    f"contract {contract.identifier!r} maps outside the event"
                )

        position_ids = [position.contract_id for position in positions]
        if len(set(position_ids)) != len(position_ids):
            raise ContractInputError(
                "duplicate positions for a contract are not supported"
            )
        if any(position_id not in contracts_by_id for position_id in position_ids):
            raise ContractInputError("position refers to an unknown contract")

        object.__setattr__(self, "contracts", contracts)
        object.__setattr__(self, "positions", positions)


def portfolio_payout_cents(portfolio: Portfolio, settled_outcome: str) -> int:
    """Return total settlement payout for one valid event outcome."""
    if not isinstance(portfolio, Portfolio):
        raise ContractInputError("portfolio must be a Portfolio")
    if settled_outcome not in portfolio.event.outcomes:
        raise ContractInputError("settled_outcome is not an event outcome")

    contracts_by_id = {contract.identifier: contract for contract in portfolio.contracts}
    total = 0
    for position in portfolio.positions:
        contract = contracts_by_id[position.contract_id]
        yes_wins = settled_outcome == contract.yes_outcome
        position_wins = yes_wins if position.side == YES else not yes_wins
        if position_wins:
            payout = (
                contract.yes_payout_cents
                if position.side == YES
                else contract.no_payout_cents
            )
            total += position.quantity * payout
    return total


def portfolio_payouts_cents(portfolio: Portfolio) -> Dict[str, int]:
    """Return one deterministic payout for every exhaustively valid state."""
    if not isinstance(portfolio, Portfolio):
        raise ContractInputError("portfolio must be a Portfolio")
    if not portfolio.event.relationship_established:
        raise ContractInputError(
            "exhaustive payout evaluation requires an explicitly established "
            "mutually exclusive and collectively exhaustive relationship"
        )
    return {
        outcome: portfolio_payout_cents(portfolio, outcome)
        for outcome in portfolio.event.outcomes
    }
