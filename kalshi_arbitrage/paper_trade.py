"""Paper trade domain models and explicit lifecycle state machine.

V4 Paper Trading simulates trades evaluated by V3 execution pricing,
recording complete audit trails without real-money order submission.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .arbitrage import cents_to_dollars
from .contract import NO, YES
from .execution import ConsumedLevel, ExecutionPricingResult


class TradeLifecycleError(ValueError):
    """Raised when an illegal paper-trade state transition or input is attempted."""


class TradeState(str, Enum):
    """Explicit lifecycle states for a simulated paper trade."""

    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


# Valid state transitions: source -> set of allowable destination states
_VALID_TRANSITIONS: Dict[TradeState, Tuple[TradeState, ...]] = {
    TradeState.PROPOSED: (TradeState.ACCEPTED, TradeState.REJECTED),
    TradeState.ACCEPTED: (
        TradeState.FILLED,
        TradeState.PARTIALLY_FILLED,
        TradeState.CANCELLED,
    ),
    TradeState.REJECTED: (),  # Terminal
    TradeState.PARTIALLY_FILLED: (TradeState.CANCELLED,),  # Can cancel remaining unfilled
    TradeState.FILLED: (),  # Terminal
    TradeState.CANCELLED: (),  # Terminal
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TradeLeg:
    """An individual contract leg within a multi-leg simulated trade."""

    contract_id: str
    market_ticker: str
    side: str  # YES or NO
    requested_quantity: int
    filled_quantity: int
    total_cost_cents: int
    total_cost_dollars: Decimal
    average_price_cents: Optional[Decimal]
    average_price_dollars: Optional[Decimal]
    consumed_levels: Tuple[ConsumedLevel, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.contract_id, str) or not self.contract_id:
            raise TradeLifecycleError("contract_id must be a non-empty string")
        if not isinstance(self.market_ticker, str) or not self.market_ticker:
            raise TradeLifecycleError("market_ticker must be a non-empty string")
        if self.side not in (YES, NO):
            raise TradeLifecycleError(f"side must be {YES!r} or {NO!r}")
        if isinstance(self.requested_quantity, bool) or not isinstance(
            self.requested_quantity, int
        ):
            raise TradeLifecycleError("requested_quantity must be an integer")
        if self.requested_quantity <= 0:
            raise TradeLifecycleError("requested_quantity must be positive")
        if isinstance(self.filled_quantity, bool) or not isinstance(
            self.filled_quantity, int
        ):
            raise TradeLifecycleError("filled_quantity must be an integer")
        if self.filled_quantity < 0:
            raise TradeLifecycleError("filled_quantity cannot be negative")
        if self.filled_quantity > self.requested_quantity:
            raise TradeLifecycleError("filled_quantity cannot exceed requested_quantity")
        if isinstance(self.total_cost_cents, bool) or not isinstance(
            self.total_cost_cents, int
        ):
            raise TradeLifecycleError("total_cost_cents must be an integer")
        if self.total_cost_cents < 0:
            raise TradeLifecycleError("total_cost_cents cannot be negative")


@dataclass(frozen=True)
class PaperTrade:
    """An auditable simulated trade with complete lifecycle tracking."""

    trade_id: str
    strategy_type: str
    state: TradeState
    requested_quantity: int
    filled_quantity: int
    unfilled_quantity: int
    total_cost_cents: int
    total_cost_dollars: Decimal
    estimated_fees_cents: int
    estimated_fees_dollars: Decimal
    guaranteed_payout_cents: int
    guaranteed_payout_dollars: Decimal
    legs: Tuple[TradeLeg, ...]
    created_at: str
    evaluated_at: str
    execution_result: Optional[ExecutionPricingResult] = None
    risk_notes: Tuple[str, ...] = ()
    rejection_reason: Optional[str] = None
    state_history: Tuple[Tuple[str, str, Optional[str]], ...] = ()  # (state, timestamp, reason)

    def __post_init__(self) -> None:
        if not isinstance(self.trade_id, str) or not self.trade_id:
            raise TradeLifecycleError("trade_id must be a non-empty string")
        if not isinstance(self.strategy_type, str) or not self.strategy_type:
            raise TradeLifecycleError("strategy_type must be a non-empty string")
        if not isinstance(self.state, TradeState):
            raise TradeLifecycleError(f"state must be a TradeState enum instance, got {self.state!r}")
        if isinstance(self.requested_quantity, bool) or not isinstance(
            self.requested_quantity, int
        ):
            raise TradeLifecycleError("requested_quantity must be an integer")
        if self.requested_quantity <= 0:
            raise TradeLifecycleError("requested_quantity must be positive")
        if isinstance(self.filled_quantity, bool) or not isinstance(
            self.filled_quantity, int
        ):
            raise TradeLifecycleError("filled_quantity must be an integer")
        if self.filled_quantity < 0:
            raise TradeLifecycleError("filled_quantity cannot be negative")
        if self.filled_quantity > self.requested_quantity:
            raise TradeLifecycleError("filled_quantity cannot exceed requested_quantity")
        if isinstance(self.total_cost_cents, bool) or not isinstance(
            self.total_cost_cents, int
        ):
            raise TradeLifecycleError("total_cost_cents must be an integer")
        if self.total_cost_cents < 0:
            raise TradeLifecycleError("total_cost_cents cannot be negative")
        if isinstance(self.estimated_fees_cents, bool) or not isinstance(
            self.estimated_fees_cents, int
        ):
            raise TradeLifecycleError("estimated_fees_cents must be an integer")
        if self.estimated_fees_cents < 0:
            raise TradeLifecycleError("estimated_fees_cents cannot be negative")

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            TradeState.FILLED,
            TradeState.REJECTED,
            TradeState.CANCELLED,
        )

    @property
    def total_cash_required_cents(self) -> int:
        """Total cash needed to support acquisition cost and estimated fees."""
        return self.total_cost_cents + self.estimated_fees_cents

    def transition_to(
        self,
        new_state: TradeState,
        timestamp: Optional[str] = None,
        reason: Optional[str] = None,
        filled_quantity: Optional[int] = None,
        total_cost_cents: Optional[int] = None,
        estimated_fees_cents: Optional[int] = None,
        guaranteed_payout_cents: Optional[int] = None,
        legs: Optional[Tuple[TradeLeg, ...]] = None,
    ) -> "PaperTrade":
        """Advance trade lifecycle to new_state enforcing explicit transition rules."""
        if not isinstance(new_state, TradeState):
            raise TradeLifecycleError(f"new_state must be a TradeState enum instance, got {new_state!r}")

        allowed = _VALID_TRANSITIONS.get(self.state, ())
        if new_state not in allowed:
            raise TradeLifecycleError(
                f"Illegal trade transition: cannot transition from {self.state.value} to {new_state.value}"
            )

        ts = timestamp or _utc_now_iso()
        new_history = self.state_history + ((new_state.value, ts, reason),)

        actual_filled = self.filled_quantity if filled_quantity is None else filled_quantity
        actual_unfilled = self.requested_quantity - actual_filled
        actual_cost = self.total_cost_cents if total_cost_cents is None else total_cost_cents
        actual_fees = (
            self.estimated_fees_cents
            if estimated_fees_cents is None
            else estimated_fees_cents
        )
        actual_payout = (
            self.guaranteed_payout_cents
            if guaranteed_payout_cents is None
            else guaranteed_payout_cents
        )
        actual_legs = self.legs if legs is None else legs

        return replace(
            self,
            state=new_state,
            filled_quantity=actual_filled,
            unfilled_quantity=actual_unfilled,
            total_cost_cents=actual_cost,
            total_cost_dollars=cents_to_dollars(actual_cost),
            estimated_fees_cents=actual_fees,
            estimated_fees_dollars=cents_to_dollars(actual_fees),
            guaranteed_payout_cents=actual_payout,
            guaranteed_payout_dollars=cents_to_dollars(actual_payout),
            legs=actual_legs,
            rejection_reason=reason if reason is not None else self.rejection_reason,
            state_history=new_history,
        )


def create_proposed_trade(
    trade_id: str,
    strategy_type: str,
    requested_quantity: int,
    created_at: Optional[str] = None,
    evaluated_at: Optional[str] = None,
    execution_result: Optional[ExecutionPricingResult] = None,
    legs: Tuple[TradeLeg, ...] = (),
) -> PaperTrade:
    """Create a new paper trade in the initial PROPOSED state."""
    now = _utc_now_iso()
    c_at = created_at or now
    e_at = evaluated_at or c_at
    history = ((TradeState.PROPOSED.value, c_at, "Trade proposed"),)

    cost_cents = execution_result.total_cost_cents if execution_result else 0
    fees_cents = (
        execution_result.estimated_fees_cents
        if execution_result and execution_result.estimated_fees_cents is not None
        else 0
    )
    payout_cents = execution_result.guaranteed_payout_cents if execution_result else 0

    return PaperTrade(
        trade_id=trade_id,
        strategy_type=strategy_type,
        state=TradeState.PROPOSED,
        requested_quantity=requested_quantity,
        filled_quantity=0,
        unfilled_quantity=requested_quantity,
        total_cost_cents=cost_cents,
        total_cost_dollars=cents_to_dollars(cost_cents),
        estimated_fees_cents=fees_cents,
        estimated_fees_dollars=cents_to_dollars(fees_cents),
        guaranteed_payout_cents=payout_cents,
        guaranteed_payout_dollars=cents_to_dollars(payout_cents),
        legs=legs,
        created_at=c_at,
        evaluated_at=e_at,
        execution_result=execution_result,
        state_history=history,
    )
