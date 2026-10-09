"""Deterministic portfolio accounting layer and append-only financial ledger.

V4 Ledger tracks cash, reservations, positions, fees, and realized P&L
using exact integer cents, with independent audit reconciliation.
"""

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple
import uuid

from .arbitrage import cents_to_dollars
from .contract import BinaryContract, NO, YES
from .paper_trade import PaperTrade, TradeLeg, TradeState


class LedgerError(ValueError):
    """Raised when an illegal financial operation or ledger entry is attempted."""


class DuplicateTradeError(LedgerError):
    """Raised when attempting to apply a trade that was already processed."""


class InsufficientCashError(LedgerError):
    """Raised when available cash is insufficient for the requested operation."""


class LedgerEntryType(str, Enum):
    """Classifies financial events recorded in the append-only ledger."""

    DEPOSIT = "DEPOSIT"
    RESERVE = "RESERVE"
    FILL = "FILL"
    CANCEL = "CANCEL"
    SETTLEMENT = "SETTLEMENT"


@dataclass(frozen=True)
class PositionDelta:
    """Represents a change to an individual contract holding."""

    contract_id: str
    side: str
    quantity_delta: int
    cost_delta_cents: int


@dataclass(frozen=True)
class LedgerEntry:
    """An immutable record of a financial event in the portfolio ledger."""

    entry_id: str
    entry_type: LedgerEntryType
    timestamp: str
    trade_id: Optional[str]
    cash_delta_cents: int
    reserved_delta_cents: int
    fees_delta_cents: int
    realized_pnl_delta_cents: int
    positions_delta: Tuple[PositionDelta, ...]
    balance_after_cents: int
    reserved_after_cents: int
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id:
            raise LedgerError("entry_id must be a non-empty string")
        if not isinstance(self.entry_type, LedgerEntryType):
            raise LedgerError(f"entry_type must be a LedgerEntryType instance, got {self.entry_type!r}")
        for delta in self.positions_delta:
            if not isinstance(delta, PositionDelta):
                raise LedgerError("positions_delta must contain PositionDelta instances")


@dataclass(frozen=True)
class PositionHolding:
    """Maintained holding in a specific contract and side."""

    contract_id: str
    side: str
    quantity: int
    total_cost_cents: int
    total_cost_dollars: Decimal
    average_price_cents: Decimal
    average_price_dollars: Decimal

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise LedgerError("position quantity must be positive")
        if self.total_cost_cents < 0:
            raise LedgerError("position total_cost_cents cannot be negative")

    @classmethod
    def create(cls, contract_id: str, side: str, quantity: int, cost_cents: int) -> "PositionHolding":
        if quantity <= 0:
            raise LedgerError("quantity must be positive")
        cost_dollars = cents_to_dollars(cost_cents)
        avg_cents = Decimal(cost_cents) / Decimal(quantity)
        avg_dollars = cost_dollars / Decimal(quantity)
        return cls(
            contract_id=contract_id,
            side=side,
            quantity=quantity,
            total_cost_cents=cost_cents,
            total_cost_dollars=cost_dollars,
            average_price_cents=avg_cents,
            average_price_dollars=avg_dollars,
        )

    def add(self, added_quantity: int, added_cost_cents: int) -> "PositionHolding":
        new_qty = self.quantity + added_quantity
        new_cost = self.total_cost_cents + added_cost_cents
        return PositionHolding.create(self.contract_id, self.side, new_qty, new_cost)


@dataclass(frozen=True)
class ReconciliationResult:
    """The outcome of independently reconstructing portfolio state from ledger entries."""

    is_reconciled: bool
    discrepancies: Tuple[str, ...]
    ledger_entries_count: int
    reconstructed_cash_cents: int
    reconstructed_reserved_cents: int
    reconstructed_fees_cents: int
    reconstructed_realized_pnl_cents: int
    reconstructed_positions_count: int


class PaperPortfolio:
    """Maintains cash, reservations, positions, and ledger for simulated trading."""

    def __init__(self, initial_cash_cents: int = 0):
        if isinstance(initial_cash_cents, bool) or not isinstance(initial_cash_cents, int):
            raise LedgerError("initial_cash_cents must be an integer")
        if initial_cash_cents < 0:
            raise LedgerError("initial_cash_cents cannot be negative")

        self._available_cash_cents: int = 0
        self._reserved_cash_cents: int = 0
        self._total_fees_cents: int = 0
        self._realized_pnl_cents: int = 0
        self._positions: Dict[Tuple[str, str], PositionHolding] = {}
        self._ledger: List[LedgerEntry] = []
        self._processed_trade_ids: Set[str] = set()
        self._trade_reservations: Dict[str, int] = {}  # trade_id -> reserved_amount_cents

        if initial_cash_cents > 0:
            self.deposit(initial_cash_cents, "Initial capital deposit", timestamp="START")

    @property
    def available_cash_cents(self) -> int:
        return self._available_cash_cents

    @property
    def reserved_cash_cents(self) -> int:
        return self._reserved_cash_cents

    @property
    def total_cash_cents(self) -> int:
        return self._available_cash_cents + self._reserved_cash_cents

    @property
    def available_cash_dollars(self) -> Decimal:
        return cents_to_dollars(self._available_cash_cents)

    @property
    def total_cash_dollars(self) -> Decimal:
        return cents_to_dollars(self.total_cash_cents)

    @property
    def total_fees_paid_cents(self) -> int:
        return self._total_fees_cents

    @property
    def total_fees_paid_dollars(self) -> Decimal:
        return cents_to_dollars(self._total_fees_cents)

    @property
    def realized_pnl_cents(self) -> int:
        return self._realized_pnl_cents

    @property
    def realized_pnl_dollars(self) -> Decimal:
        return cents_to_dollars(self._realized_pnl_cents)

    @property
    def positions(self) -> Mapping[Tuple[str, str], PositionHolding]:
        return dict(self._positions)

    @property
    def ledger(self) -> Tuple[LedgerEntry, ...]:
        return tuple(self._ledger)

    @property
    def processed_trade_ids(self) -> Set[str]:
        return set(self._processed_trade_ids)

    def deposit(
        self,
        amount_cents: int,
        description: str = "Cash deposit",
        *,
        timestamp: Optional[str] = None,
    ) -> LedgerEntry:
        """Add cash to the portfolio, recording a DEPOSIT ledger entry."""
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise LedgerError("amount_cents must be an integer")
        if amount_cents <= 0:
            raise LedgerError("deposit amount must be positive")

        self._available_cash_cents += amount_cents
        entry = LedgerEntry(
            entry_id=f"ent-{uuid.uuid4().hex[:12]}",
            entry_type=LedgerEntryType.DEPOSIT,
            timestamp=timestamp or "NOW",
            trade_id=None,
            cash_delta_cents=amount_cents,
            reserved_delta_cents=0,
            fees_delta_cents=0,
            realized_pnl_delta_cents=0,
            positions_delta=(),
            balance_after_cents=self._available_cash_cents,
            reserved_after_cents=self._reserved_cash_cents,
            description=description,
        )
        self._ledger.append(entry)
        return entry

    def reserve_for_trade(
        self,
        trade: PaperTrade,
        *,
        timestamp: Optional[str] = None,
    ) -> LedgerEntry:
        """Reserve cash for an accepted trade pending fill simulation."""
        if not isinstance(trade, PaperTrade):
            raise LedgerError("trade must be a PaperTrade instance")
        if trade.trade_id in self._processed_trade_ids or trade.trade_id in self._trade_reservations:
            raise DuplicateTradeError(f"Trade {trade.trade_id!r} already processed or reserved")

        needed = trade.total_cash_required_cents
        if self._available_cash_cents < needed:
            raise InsufficientCashError(
                f"Available cash ({self._available_cash_cents}¢) insufficient for trade "
                f"requirement ({needed}¢)"
            )

        self._available_cash_cents -= needed
        self._reserved_cash_cents += needed
        self._trade_reservations[trade.trade_id] = needed

        entry = LedgerEntry(
            entry_id=f"ent-{uuid.uuid4().hex[:12]}",
            entry_type=LedgerEntryType.RESERVE,
            timestamp=timestamp or trade.evaluated_at,
            trade_id=trade.trade_id,
            cash_delta_cents=-needed,
            reserved_delta_cents=needed,
            fees_delta_cents=0,
            realized_pnl_delta_cents=0,
            positions_delta=(),
            balance_after_cents=self._available_cash_cents,
            reserved_after_cents=self._reserved_cash_cents,
            description=f"Cash reserved for trade {trade.trade_id}",
        )
        self._ledger.append(entry)
        return entry

    def cancel_trade_reservation(
        self,
        trade_id: str,
        *,
        timestamp: Optional[str] = None,
        reason: str = "Trade cancelled",
    ) -> LedgerEntry:
        """Release previously reserved cash if an accepted trade is cancelled."""
        if trade_id not in self._trade_reservations:
            raise LedgerError(f"No active cash reservation for trade {trade_id!r}")

        reserved_amount = self._trade_reservations.pop(trade_id)
        self._available_cash_cents += reserved_amount
        self._reserved_cash_cents -= reserved_amount

        entry = LedgerEntry(
            entry_id=f"ent-{uuid.uuid4().hex[:12]}",
            entry_type=LedgerEntryType.CANCEL,
            timestamp=timestamp or "NOW",
            trade_id=trade_id,
            cash_delta_cents=reserved_amount,
            reserved_delta_cents=-reserved_amount,
            fees_delta_cents=0,
            realized_pnl_delta_cents=0,
            positions_delta=(),
            balance_after_cents=self._available_cash_cents,
            reserved_after_cents=self._reserved_cash_cents,
            description=f"Reservation released: {reason}",
        )
        self._ledger.append(entry)
        return entry

    def apply_trade_fill(
        self,
        trade: PaperTrade,
        *,
        timestamp: Optional[str] = None,
    ) -> LedgerEntry:
        """Apply a filled or partially filled trade to positions and cash."""
        if not isinstance(trade, PaperTrade):
            raise LedgerError("trade must be a PaperTrade instance")
        if trade.trade_id in self._processed_trade_ids:
            raise DuplicateTradeError(f"Trade {trade.trade_id!r} has already been applied")
        if trade.state not in (TradeState.FILLED, TradeState.PARTIALLY_FILLED):
            raise LedgerError(f"Cannot apply trade with state {trade.state.value} to ledger")

        actual_cost = trade.total_cost_cents
        actual_fees = trade.estimated_fees_cents
        actual_total = actual_cost + actual_fees

        reserved_amount = self._trade_reservations.get(trade.trade_id, 0)

        # Atomic pre-validation: verify cash sufficiency before mutating any portfolio state
        if reserved_amount > 0:
            excess_required = actual_total - reserved_amount
            if excess_required > 0 and self._available_cash_cents < excess_required:
                raise InsufficientCashError(
                    f"Available cash ({self._available_cash_cents}¢) insufficient for excess fill "
                    f"cost + fees ({excess_required}¢ beyond reserved {reserved_amount}¢)"
                )
        else:
            # Direct fill without prior reservation
            if self._available_cash_cents < actual_total:
                raise InsufficientCashError(
                    f"Available cash ({self._available_cash_cents}¢) insufficient for fill "
                    f"cost + fees ({actual_total}¢)"
                )

        if reserved_amount > 0:
            del self._trade_reservations[trade.trade_id]
            self._reserved_cash_cents -= reserved_amount
            cash_delta = reserved_amount - actual_total
            self._available_cash_cents += cash_delta
        else:
            self._available_cash_cents -= actual_total
            cash_delta = -actual_total

        self._total_fees_cents += actual_fees

        # Update position holdings
        pos_deltas: List[PositionDelta] = []
        for leg in trade.legs:
            if leg.filled_quantity <= 0:
                continue
            key = (leg.contract_id, leg.side)
            if key in self._positions:
                self._positions[key] = self._positions[key].add(
                    leg.filled_quantity, leg.total_cost_cents
                )
            else:
                self._positions[key] = PositionHolding.create(
                    leg.contract_id, leg.side, leg.filled_quantity, leg.total_cost_cents
                )
            pos_deltas.append(
                PositionDelta(
                    contract_id=leg.contract_id,
                    side=leg.side,
                    quantity_delta=leg.filled_quantity,
                    cost_delta_cents=leg.total_cost_cents,
                )
            )

        self._processed_trade_ids.add(trade.trade_id)

        entry = LedgerEntry(
            entry_id=f"ent-{uuid.uuid4().hex[:12]}",
            entry_type=LedgerEntryType.FILL,
            timestamp=timestamp or trade.evaluated_at,
            trade_id=trade.trade_id,
            cash_delta_cents=cash_delta,
            reserved_delta_cents=-reserved_amount,
            fees_delta_cents=actual_fees,
            realized_pnl_delta_cents=0,
            positions_delta=tuple(pos_deltas),
            balance_after_cents=self._available_cash_cents,
            reserved_after_cents=self._reserved_cash_cents,
            description=(
                f"Fill simulated for trade {trade.trade_id}: {trade.filled_quantity} units "
                f"cost {actual_cost}¢ + fees {actual_fees}¢"
            ),
        )
        self._ledger.append(entry)
        return entry

    def settle_position(
        self,
        contract_id: str,
        side: str,
        payout_per_contract_cents: int,
        *,
        timestamp: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> LedgerEntry:
        """Settle an individual open position holding at the specified contract payout.

        Args:
            contract_id: Explicit identifier of the contract holding to settle.
            side: Position side ('YES' or 'NO').
            payout_per_contract_cents: Deterministic settlement payout per contract in cents (0 to 100).
            timestamp: Optional ISO settlement timestamp.
            reason: Optional description for audit trail.
        """
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise LedgerError("contract_id must be a non-empty string")
        if side not in (YES, NO):
            raise LedgerError(f"side must be {YES!r} or {NO!r}")
        if isinstance(payout_per_contract_cents, bool) or not isinstance(payout_per_contract_cents, int):
            raise LedgerError("payout_per_contract_cents must be an integer")
        if not 0 <= payout_per_contract_cents <= 100:
            raise LedgerError("payout_per_contract_cents must be between 0 and 100")
        if (contract_id, side) not in self._positions:
            raise LedgerError(f"No open position holding for {contract_id!r} {side}")

        holding = self._positions.pop((contract_id, side))
        total_payout_cents = holding.quantity * payout_per_contract_cents
        cost_basis_cents = holding.total_cost_cents
        realized_pnl = total_payout_cents - cost_basis_cents

        self._available_cash_cents += total_payout_cents
        self._realized_pnl_cents += realized_pnl

        entry = LedgerEntry(
            entry_id=f"ent-{uuid.uuid4().hex[:12]}",
            entry_type=LedgerEntryType.SETTLEMENT,
            timestamp=timestamp or "SETTLED",
            trade_id=None,
            cash_delta_cents=total_payout_cents,
            reserved_delta_cents=0,
            fees_delta_cents=0,
            realized_pnl_delta_cents=realized_pnl,
            positions_delta=(
                PositionDelta(
                    contract_id=contract_id,
                    side=side,
                    quantity_delta=-holding.quantity,
                    cost_delta_cents=-cost_basis_cents,
                ),
            ),
            balance_after_cents=self._available_cash_cents,
            reserved_after_cents=self._reserved_cash_cents,
            description=(
                f"Settlement: {contract_id} {side} ({holding.quantity} contracts) "
                f"paid {total_payout_cents}¢; realized P&L: {realized_pnl}¢ "
                f"({reason or 'settled'})"
            ),
        )
        self._ledger.append(entry)
        return entry

    def settle_binary_contract(
        self,
        contract_id: str,
        winning_side: str,
        *,
        timestamp: Optional[str] = None,
    ) -> Tuple[LedgerEntry, ...]:
        """Settle open binary positions on a specific contract identifier given winning_side (YES or NO).

        In Kalshi binary options, the winning side pays 100¢ per contract and the losing side pays 0¢.
        Use this method when positions were opened using explicit contract identifiers that differ
        from market tickers (e.g. multi-contract portfolios where contract_id != market_ticker).
        """
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise LedgerError("contract_id must be a non-empty string")
        if winning_side not in (YES, NO):
            raise LedgerError(f"winning_side must be {YES!r} or {NO!r}")

        entries = []
        for side in (YES, NO):
            if (contract_id, side) in self._positions:
                payout_cents = 100 if side == winning_side else 0
                entries.append(
                    self.settle_position(
                        contract_id,
                        side,
                        payout_cents,
                        timestamp=timestamp,
                        reason=f"contract {contract_id} settled {winning_side}",
                    )
                )
        return tuple(entries)

    def settle_binary_market(
        self,
        market_ticker: str,
        winning_side: str,
        *,
        timestamp: Optional[str] = None,
    ) -> Tuple[LedgerEntry, ...]:
        """Settle open binary positions for a market ticker given winning_side (YES or NO).

        In single-market binary parity and MECE baskets, contract_id defaults to market_ticker.
        For general portfolios where contract_id differs from market_ticker, use
        settle_binary_contract(contract_id, winning_side) or settle_position(contract_id, side, payout_cents).
        """
        if not isinstance(market_ticker, str) or not market_ticker.strip():
            raise LedgerError("market_ticker must be a non-empty string")
        if winning_side not in (YES, NO):
            raise LedgerError(f"winning_side must be {YES!r} or {NO!r}")

        return self.settle_binary_contract(
            market_ticker,
            winning_side,
            timestamp=timestamp,
        )

    def total_cost_basis_cents(self) -> int:
        """Total acquisition cost basis of all currently open positions."""
        return sum(pos.total_cost_cents for pos in self._positions.values())

    def unrealized_pnl_cents(
        self,
        mark_prices_cents: Mapping[Tuple[str, str], int],
    ) -> int:
        """Calculate unrealized P/L against explicitly provided mark prices."""
        total_unrealized = 0
        for key, pos in self._positions.items():
            if key not in mark_prices_cents:
                raise LedgerError(f"Missing mark price for open position {key}")
            mark_price = mark_prices_cents[key]
            if not 0 <= mark_price <= 100:
                raise LedgerError(f"Mark price {mark_price} for {key} out of bounds")
            current_value = pos.quantity * mark_price
            total_unrealized += current_value - pos.total_cost_cents
        return total_unrealized

    def book_value_equity_cents(self) -> int:
        """Total book-value equity: total cash plus position acquisition cost basis.

        Book-value equity values open positions at historical acquisition cost (zero unrealized P&L)
        until marked to market or resolved at settlement. It is strictly distinct from
        market liquidation value or worst-case settlement payoff estimates.
        """
        return self.total_cash_cents + self.total_cost_basis_cents()

    def portfolio_equity_cents(
        self,
        mark_prices_cents: Optional[Mapping[Tuple[str, str], int]] = None,
    ) -> int:
        """Total portfolio equity (book value if mark_prices_cents is None; marked-to-market otherwise)."""
        if mark_prices_cents is None:
            return self.book_value_equity_cents()
        return (
            self.total_cash_cents
            + self.total_cost_basis_cents()
            + self.unrealized_pnl_cents(mark_prices_cents)
        )

    def reconcile(self) -> ReconciliationResult:
        """Independently re-simulate all ledger entries from scratch and verify state."""
        rec_available_cents = 0
        rec_reserved_cents = 0
        rec_fees_cents = 0
        rec_realized_pnl_cents = 0
        rec_positions: Dict[Tuple[str, str], Dict[str, int]] = {}
        discrepancies: List[str] = []

        for entry in self._ledger:
            rec_available_cents += entry.cash_delta_cents
            rec_reserved_cents += entry.reserved_delta_cents
            rec_fees_cents += entry.fees_delta_cents
            rec_realized_pnl_cents += entry.realized_pnl_delta_cents

            for p_delta in entry.positions_delta:
                key = (p_delta.contract_id, p_delta.side)
                if key not in rec_positions:
                    rec_positions[key] = {"qty": 0, "cost": 0}
                rec_positions[key]["qty"] += p_delta.quantity_delta
                rec_positions[key]["cost"] += p_delta.cost_delta_cents
                if rec_positions[key]["qty"] == 0:
                    del rec_positions[key]

        # Compare with maintained portfolio state
        if rec_available_cents != self._available_cash_cents:
            discrepancies.append(
                f"Cash mismatch: reconstructed {rec_available_cents}¢ vs maintained {self._available_cash_cents}¢"
            )
        if rec_reserved_cents != self._reserved_cash_cents:
            discrepancies.append(
                f"Reserved mismatch: reconstructed {rec_reserved_cents}¢ vs maintained {self._reserved_cash_cents}¢"
            )
        if rec_fees_cents != self._total_fees_cents:
            discrepancies.append(
                f"Fees mismatch: reconstructed {rec_fees_cents}¢ vs maintained {self._total_fees_cents}¢"
            )
        if rec_realized_pnl_cents != self._realized_pnl_cents:
            discrepancies.append(
                f"Realized P&L mismatch: reconstructed {rec_realized_pnl_cents}¢ vs maintained {self._realized_pnl_cents}¢"
            )

        if len(rec_positions) != len(self._positions):
            discrepancies.append(
                f"Position count mismatch: reconstructed {len(rec_positions)} vs maintained {len(self._positions)}"
            )
        else:
            for key, rec_pos in rec_positions.items():
                if key not in self._positions:
                    discrepancies.append(f"Position {key} missing in maintained state")
                else:
                    cur = self._positions[key]
                    if rec_pos["qty"] != cur.quantity:
                        discrepancies.append(
                            f"Position {key} quantity mismatch: {rec_pos['qty']} vs {cur.quantity}"
                        )
                    if rec_pos["cost"] != cur.total_cost_cents:
                        discrepancies.append(
                            f"Position {key} cost mismatch: {rec_pos['cost']}¢ vs {cur.total_cost_cents}¢"
                        )

        return ReconciliationResult(
            is_reconciled=len(discrepancies) == 0,
            discrepancies=tuple(discrepancies),
            ledger_entries_count=len(self._ledger),
            reconstructed_cash_cents=rec_available_cents,
            reconstructed_reserved_cents=rec_reserved_cents,
            reconstructed_fees_cents=rec_fees_cents,
            reconstructed_realized_pnl_cents=rec_realized_pnl_cents,
            reconstructed_positions_count=len(rec_positions),
        )
