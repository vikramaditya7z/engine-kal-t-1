"""Asynchronous multi-leg execution lifecycle, latency simulation, and scratch risk engine.

V8.2 Multi-Leg Execution Lifecycle:
- Represents real-world electronic trading reality: Kalshi requires independent orders per leg.
- Explicit lifecycle states: PLANNED, EXECUTING, PARTIALLY_FILLED, FILLED, LEGGED_OUT, UNWINDING, CLOSED, FAILED.
- Detailed tracking per leg: intended vs filled vs remaining quantities, execution prices, fees, timestamps.
- Residual exposure calculation & mark-to-model risk for unhedged positions.
- Prohibits treating incomplete trades as completed arbitrage.

V8.3 Latency & Microstructure Simulation:
- Separate observation, decision, submission, and fill timestamps.
- Sequential leg execution with configurable network RTT and inter-leg pacing.
- Sequencing policies: ILLIQUID_FIRST, HIGH_PRICE_FIRST, LOW_PRICE_FIRST, SIMULTANEOUS.
- Deterministic fill scenarios and queue probability models.

V8.4 Unwind, Scratch & Risk Management:
- Automated recovery when an opportunity legs out.
- Unwind policies: EMERGENCY_DUMP (liquidate filled leg), AGGRESSIVE_CROSS (complete hedge at market), CONSERVATIVE_LIMIT.
- Exact fee calculation via V8.1 FeeModel and slippage bounds.
- Full reconciliation with PaperPortfolio and financial ledger.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
import uuid

from .arbitrage import cents_to_dollars, dollars_to_cents
from .contract import NO, YES
from .execution import ConsumedLevel, DepthTraversalResult, ExecutionPricingResult
from .fee_model import (
    CalculatedFee,
    FeeModelConfig,
    FeeScheduleType,
    FillTier,
    calculate_multi_level_fee,
    calculate_single_level_fee,
)
from .ledger import LedgerEntry, LedgerEntryType, LedgerError, PaperPortfolio, PositionDelta


class MultiLegLifecycleState(str, Enum):
    """Explicit lifecycle states for a multi-leg electronic trade."""

    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    LEGGED_OUT = "LEGGED_OUT"
    UNWINDING = "UNWINDING"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


# Valid state transitions: source -> tuple of allowable destination states
_VALID_LIFECYCLE_TRANSITIONS: Dict[MultiLegLifecycleState, Tuple[MultiLegLifecycleState, ...]] = {
    MultiLegLifecycleState.PLANNED: (
        MultiLegLifecycleState.EXECUTING,
        MultiLegLifecycleState.FAILED,
    ),
    MultiLegLifecycleState.EXECUTING: (
        MultiLegLifecycleState.PARTIALLY_FILLED,
        MultiLegLifecycleState.FILLED,
        MultiLegLifecycleState.LEGGED_OUT,
        MultiLegLifecycleState.FAILED,
    ),
    MultiLegLifecycleState.PARTIALLY_FILLED: (
        MultiLegLifecycleState.EXECUTING,
        MultiLegLifecycleState.FILLED,
        MultiLegLifecycleState.LEGGED_OUT,
        MultiLegLifecycleState.UNWINDING,
        MultiLegLifecycleState.FAILED,
    ),
    MultiLegLifecycleState.FILLED: (
        MultiLegLifecycleState.CLOSED,
    ),
    MultiLegLifecycleState.LEGGED_OUT: (
        MultiLegLifecycleState.UNWINDING,
        MultiLegLifecycleState.CLOSED,
        MultiLegLifecycleState.FAILED,
    ),
    MultiLegLifecycleState.UNWINDING: (
        MultiLegLifecycleState.CLOSED,
        MultiLegLifecycleState.FAILED,
    ),
    MultiLegLifecycleState.CLOSED: (),  # Terminal
    MultiLegLifecycleState.FAILED: (),  # Terminal
}


class LegExecutionStatus(str, Enum):
    """Execution status for an individual leg."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class LegSequencingPolicy(str, Enum):
    """Ordering rule for submitting sequential legs."""

    ILLIQUID_FIRST = "ILLIQUID_FIRST"      # Send order for the leg with least depth first
    HIGH_PRICE_FIRST = "HIGH_PRICE_FIRST"  # Send order for highest-price leg first
    LOW_PRICE_FIRST = "LOW_PRICE_FIRST"    # Send order for lowest-price leg first
    SIMULTANEOUS = "SIMULTANEOUS"          # Send all orders in parallel burst


class UnwindPolicy(str, Enum):
    """Recovery strategy when a trade legs out."""

    EMERGENCY_DUMP = "EMERGENCY_DUMP"      # Sell filled leg to market bid immediately
    AGGRESSIVE_CROSS = "AGGRESSIVE_CROSS"  # Buy missing leg at market ask paying slippage
    CONSERVATIVE_LIMIT = "CONSERVATIVE_LIMIT"


class AsyncExecutionError(ValueError):
    """Raised when an asynchronous execution transition or calculation is invalid."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ExecutionTimestamps:
    """Microsecond-resolution timestamps across the electronic execution pipeline."""

    observed_at: datetime
    decision_at: datetime
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    unwind_started_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

    def latency_to_decision_ms(self) -> float:
        return (self.decision_at - self.observed_at).total_seconds() * 1000.0

    def latency_to_fill_ms(self) -> Optional[float]:
        if self.filled_at is None:
            return None
        return (self.filled_at - self.decision_at).total_seconds() * 1000.0


@dataclass(frozen=True)
class AsyncTradeLeg:
    """An individual contract leg within an asynchronous multi-leg execution."""

    contract_id: str
    market_ticker: str
    side: str  # YES or NO
    intended_quantity: int
    filled_quantity: int = 0
    remaining_quantity: int = 0
    total_cost_cents: int = 0
    fees_cents: int = 0
    average_price_cents: Optional[Decimal] = None
    status: LegExecutionStatus = LegExecutionStatus.PENDING
    timestamps: Optional[ExecutionTimestamps] = None
    consumed_levels: Tuple[ConsumedLevel, ...] = ()
    rejection_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.side not in (YES, NO):
            raise AsyncExecutionError(f"side must be {YES!r} or {NO!r}")
        if self.intended_quantity <= 0:
            raise AsyncExecutionError("intended_quantity must be positive")
        if self.filled_quantity < 0:
            raise AsyncExecutionError("filled_quantity cannot be negative")
        if self.filled_quantity > self.intended_quantity:
            raise AsyncExecutionError("filled_quantity cannot exceed intended_quantity")
        if self.remaining_quantity < 0:
            raise AsyncExecutionError("remaining_quantity cannot be negative")

    @classmethod
    def create(
        cls,
        contract_id: str,
        market_ticker: str,
        side: str,
        quantity: int,
        timestamps: Optional[ExecutionTimestamps] = None,
    ) -> "AsyncTradeLeg":
        return cls(
            contract_id=contract_id,
            market_ticker=market_ticker,
            side=side,
            intended_quantity=quantity,
            remaining_quantity=quantity,
            timestamps=timestamps,
        )

    def with_fill(
        self,
        filled_qty: int,
        cost_cents: int,
        fees_cents: int,
        fill_time: datetime,
        consumed_levels: Sequence[ConsumedLevel] = (),
    ) -> "AsyncTradeLeg":
        """Record fill execution for this leg."""
        if filled_qty < 0:
            raise AsyncExecutionError("filled_qty cannot be negative")
        new_filled = self.filled_quantity + filled_qty
        if new_filled > self.intended_quantity:
            raise AsyncExecutionError(f"overfill: {new_filled} > intended {self.intended_quantity}")
        new_remaining = self.intended_quantity - new_filled
        new_cost = self.total_cost_cents + cost_cents
        new_fees = self.fees_cents + fees_cents
        avg_price = (
            (Decimal(new_cost) / Decimal(new_filled)).quantize(Decimal("0.01"))
            if new_filled > 0
            else None
        )
        new_status = (
            LegExecutionStatus.FILLED
            if new_remaining == 0
            else LegExecutionStatus.PARTIALLY_FILLED
        )
        new_ts = (
            self.timestamps
            if self.timestamps is None
            else ExecutionTimestamps(
                observed_at=self.timestamps.observed_at,
                decision_at=self.timestamps.decision_at,
                submitted_at=self.timestamps.submitted_at,
                filled_at=fill_time,
                unwind_started_at=self.timestamps.unwind_started_at,
                closed_at=self.timestamps.closed_at,
            )
        )
        return AsyncTradeLeg(
            contract_id=self.contract_id,
            market_ticker=self.market_ticker,
            side=self.side,
            intended_quantity=self.intended_quantity,
            filled_quantity=new_filled,
            remaining_quantity=new_remaining,
            total_cost_cents=new_cost,
            fees_cents=new_fees,
            average_price_cents=avg_price,
            status=new_status,
            timestamps=new_ts,
            consumed_levels=tuple(list(self.consumed_levels) + list(consumed_levels)),
        )

    def with_rejection(self, reason: str) -> "AsyncTradeLeg":
        return AsyncTradeLeg(
            contract_id=self.contract_id,
            market_ticker=self.market_ticker,
            side=self.side,
            intended_quantity=self.intended_quantity,
            filled_quantity=self.filled_quantity,
            remaining_quantity=self.remaining_quantity,
            total_cost_cents=self.total_cost_cents,
            fees_cents=self.fees_cents,
            average_price_cents=self.average_price_cents,
            status=LegExecutionStatus.REJECTED,
            timestamps=self.timestamps,
            consumed_levels=self.consumed_levels,
            rejection_reason=reason,
        )


@dataclass(frozen=True)
class AsyncMultiLegTrade:
    """Domain model and state tracker for realistic asynchronous multi-leg execution."""

    trade_id: str
    strategy_type: str
    target_quantity: int
    legs: Tuple[AsyncTradeLeg, ...]
    state: MultiLegLifecycleState = MultiLegLifecycleState.PLANNED
    sequencing_policy: LegSequencingPolicy = LegSequencingPolicy.ILLIQUID_FIRST
    unwind_policy: UnwindPolicy = UnwindPolicy.EMERGENCY_DUMP
    total_cost_cents: int = 0
    total_fees_cents: int = 0
    realized_pnl_cents: int = 0
    unwind_proceeds_cents: int = 0
    rejection_reason: Optional[str] = None
    history: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    liquidated_positions: Mapping[Tuple[str, str], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trade_id:
            raise AsyncExecutionError("trade_id must be non-empty")
        if not self.legs:
            raise AsyncExecutionError("trade must have at least one leg")
        if self.target_quantity <= 0:
            raise AsyncExecutionError("target_quantity must be positive")

    @property
    def is_arbitrage_complete(self) -> bool:
        """True ONLY if all legs have fully filled for the target quantity."""
        if self.state not in (MultiLegLifecycleState.FILLED, MultiLegLifecycleState.CLOSED):
            return False
        # All legs must have filled exactly target_quantity
        return all(leg.filled_quantity == self.target_quantity for leg in self.legs)

    @property
    def total_fee_cents(self) -> int:
        return self.total_fees_cents

    @property
    def gross_pnl_cents(self) -> int:
        """Realized or settlement gross edge in cents before fees."""
        if self.is_arbitrage_complete:
            common_units = min(leg.filled_quantity for leg in self.legs) if self.legs else 0
            payout = 100 * common_units
            return payout - self.total_cost_cents
        elif self.state == MultiLegLifecycleState.CLOSED and self.unwind_proceeds_cents > 0:
            return self.unwind_proceeds_cents - self.total_cost_cents
        return 0

    @property
    def scratch_loss_cents(self) -> int:
        """Loss incurred during scratch / unwind liquidation."""
        if self.state in (MultiLegLifecycleState.CLOSED, MultiLegLifecycleState.LEGGED_OUT) and not self.is_arbitrage_complete:
            return abs(min(0, self.realized_pnl_cents))
        return 0

    @property
    def residual_exposure(self) -> Dict[Tuple[str, str], int]:
        """Calculates unhedged net positions across legs: (contract_id, side) -> net_qty."""
        # For single-market binary parity: 1 YES and 1 NO cancel out
        # In general: compute unhedged imbalance
        filled_by_side = {leg.side: leg.filled_quantity for leg in self.legs}
        if len(self.legs) == 2 and set(filled_by_side.keys()) == {YES, NO}:
            yes_qty = filled_by_side[YES]
            no_qty = filled_by_side[NO]
            common = min(yes_qty, no_qty)
            exposure: Dict[Tuple[str, str], int] = {}
            if yes_qty > common:
                yes_leg = next(l for l in self.legs if l.side == YES)
                exposure[(yes_leg.contract_id, YES)] = yes_qty - common
            elif no_qty > common:
                no_leg = next(l for l in self.legs if l.side == NO)
                exposure[(no_leg.contract_id, NO)] = no_qty - common
            return exposure

        # MECE or general portfolio: unhedged is any deviation from common filled units
        common_units = min(leg.filled_quantity for leg in self.legs)
        exposure = {}
        for leg in self.legs:
            unhedged = leg.filled_quantity - common_units
            if unhedged > 0:
                exposure[(leg.contract_id, leg.side)] = unhedged
        return exposure

    def mark_to_model_risk_cents(
        self,
        mark_bids: Mapping[Tuple[str, str], int],
    ) -> Dict[str, Any]:
        """Calculate unrealized exposure liquidation value and worst-case risk."""
        exp = self.residual_exposure
        if not exp:
            return {
                "has_residual_exposure": False,
                "unhedged_contracts": 0,
                "liquidation_value_cents": 0,
                "worst_case_loss_cents": 0,
            }

        unhedged_contracts = sum(exp.values())
        liquidation_val = 0
        cost_of_unhedged = 0

        for (cid, side), qty in exp.items():
            leg = next(l for l in self.legs if l.contract_id == cid and l.side == side)
            unit_cost = int(leg.average_price_cents or 0)
            cost_of_unhedged += qty * unit_cost
            bid = mark_bids.get((cid, side), 0)
            liquidation_val += qty * bid

        worst_case = cost_of_unhedged  # if asset expires at 0¢
        expected_unwind_loss = cost_of_unhedged - liquidation_val

        return {
            "has_residual_exposure": True,
            "unhedged_contracts": unhedged_contracts,
            "cost_basis_cents": cost_of_unhedged,
            "liquidation_value_cents": liquidation_val,
            "expected_unwind_loss_cents": expected_unwind_loss,
            "worst_case_loss_cents": worst_case,
        }

    def transition_to(
        self,
        new_state: MultiLegLifecycleState,
        *,
        reason: str = "",
        timestamp: Optional[datetime] = None,
        updated_legs: Optional[Sequence[AsyncTradeLeg]] = None,
        realized_pnl_delta_cents: int = 0,
        unwind_proceeds_cents: int = 0,
        liquidated_positions: Optional[Mapping[Tuple[str, str], int]] = None,
    ) -> "AsyncMultiLegTrade":
        """Advance the multi-leg lifecycle with strict transition validation."""
        valid_targets = _VALID_LIFECYCLE_TRANSITIONS.get(self.state, ())
        if new_state not in valid_targets:
            raise AsyncExecutionError(
                f"Illegal state transition from {self.state.value} to {new_state.value}. "
                f"Allowable targets: {[s.value for s in valid_targets]}"
            )

        legs_to_use = tuple(updated_legs) if updated_legs is not None else self.legs
        tot_cost = sum(l.total_cost_cents for l in legs_to_use)
        tot_fees = sum(l.fees_cents for l in legs_to_use)

        ts = (timestamp or _utc_now()).isoformat()
        new_hist = list(self.history)
        new_hist.append(
            {
                "from_state": self.state.value,
                "to_state": new_state.value,
                "timestamp": ts,
                "reason": reason,
                "total_cost_cents": tot_cost,
                "total_fees_cents": tot_fees,
            }
        )

        return AsyncMultiLegTrade(
            trade_id=self.trade_id,
            strategy_type=self.strategy_type,
            target_quantity=self.target_quantity,
            legs=legs_to_use,
            state=new_state,
            sequencing_policy=self.sequencing_policy,
            unwind_policy=self.unwind_policy,
            total_cost_cents=tot_cost,
            total_fees_cents=tot_fees,
            realized_pnl_cents=self.realized_pnl_cents + realized_pnl_delta_cents,
            unwind_proceeds_cents=self.unwind_proceeds_cents + unwind_proceeds_cents,
            rejection_reason=reason if new_state in (MultiLegLifecycleState.FAILED, MultiLegLifecycleState.LEGGED_OUT) else self.rejection_reason,
            history=tuple(new_hist),
            liquidated_positions=dict(liquidated_positions) if liquidated_positions is not None else self.liquidated_positions,
        )


@dataclass(frozen=True)
class MicrostructureConfig:
    """Parameters governing realistic execution timing, fill probabilities, and risk."""

    network_latency_ms: float = 50.0          # RTT from decision to exchange
    decision_latency_ms: float = 5.0          # Strategy computation time
    sequential_leg_delay_ms: float = 25.0     # Time between submitting leg 1 and leg 2
    scratch_timeout_ms: float = 500.0         # Max wait in LEGGED_OUT before initiating unwind
    max_scratch_slippage_cents: int = 5       # Max loss tolerated when aggressive-crossing
    sequencing_policy: LegSequencingPolicy = LegSequencingPolicy.ILLIQUID_FIRST
    unwind_policy: UnwindPolicy = UnwindPolicy.EMERGENCY_DUMP
    fee_config: FeeModelConfig = field(default_factory=FeeModelConfig)
    enable_latency_decay: bool = False
    latency_decay_threshold_ms: float = 100.0
    latency_decay_rate_per_100ms: float = 0.20
    adverse_price_move_threshold_ms: float = 500.0


class RealisticExecutionSimulator:
    """Deterministic simulation engine for multi-leg execution, latency, and scratch risk."""

    def __init__(self, config: Optional[MicrostructureConfig] = None):
        self.config = config or MicrostructureConfig()

    def order_legs(
        self,
        legs: Sequence[AsyncTradeLeg],
        depth_by_leg: Mapping[str, int],
        price_by_leg: Mapping[str, int],
    ) -> Tuple[AsyncTradeLeg, ...]:
        """Sequence legs according to configured LegSequencingPolicy."""
        policy = self.config.sequencing_policy

        if policy == LegSequencingPolicy.ILLIQUID_FIRST:
            # Sort ascending by available book depth (least depth first)
            return tuple(sorted(legs, key=lambda l: depth_by_leg.get(l.contract_id, 999999)))
        elif policy == LegSequencingPolicy.HIGH_PRICE_FIRST:
            # Sort descending by contract ask price
            return tuple(sorted(legs, key=lambda l: price_by_leg.get(l.contract_id, 0), reverse=True))
        elif policy == LegSequencingPolicy.LOW_PRICE_FIRST:
            # Sort ascending by contract ask price
            return tuple(sorted(legs, key=lambda l: price_by_leg.get(l.contract_id, 0)))
        else:
            # SIMULTANEOUS: preserve original order
            return tuple(legs)

    def simulate_execution(
        self,
        trade_id: str,
        strategy_type: str,
        target_quantity: int,
        legs_specs: Sequence[Tuple[str, str, str]],  # (contract_id, market_ticker, side)
        available_depth_by_leg: Mapping[str, Sequence[Tuple[int, int]]],  # contract_id -> list of (price_cents, qty)
        counterparty_bids_by_leg: Mapping[str, Sequence[Tuple[int, int]]], # contract_id -> list of (bid_cents, qty)
        start_time: Optional[datetime] = None,
        force_scenario: Optional[str] = None,  # For reproducible test cases
    ) -> AsyncMultiLegTrade:
        """Simulate realistic multi-leg execution with latency, partial fills, and recovery."""
        t0 = start_time or _utc_now()
        t_dec = t0 + timedelta(milliseconds=self.config.decision_latency_ms)

        # 1. Initialize trade in PLANNED state
        planned_legs = []
        depth_map: Dict[str, int] = {}
        top_price_map: Dict[str, int] = {}

        for cid, ticker, side in legs_specs:
            ts = ExecutionTimestamps(observed_at=t0, decision_at=t_dec)
            planned_legs.append(AsyncTradeLeg.create(cid, ticker, side, target_quantity, timestamps=ts))
            depth_tiers = available_depth_by_leg.get(cid, ())
            total_d = sum(qty for _, qty in depth_tiers)
            depth_map[cid] = total_d
            top_p = depth_tiers[0][0] if depth_tiers else 100
            top_price_map[cid] = top_p

        ordered_legs = self.order_legs(planned_legs, depth_map, top_price_map)
        trade = AsyncMultiLegTrade(
            trade_id=trade_id,
            strategy_type=strategy_type,
            target_quantity=target_quantity,
            legs=ordered_legs,
            state=MultiLegLifecycleState.PLANNED,
            sequencing_policy=self.config.sequencing_policy,
            unwind_policy=self.config.unwind_policy,
        )

        # 2. Transition to EXECUTING
        t_exec = t_dec + timedelta(milliseconds=self.config.network_latency_ms)
        trade = trade.transition_to(
            MultiLegLifecycleState.EXECUTING,
            reason="Orders routed to exchange matching engine",
            timestamp=t_exec,
        )

        # 3. Simulate sequential leg executions
        updated_legs: List[AsyncTradeLeg] = []
        current_time = t_exec
        fill_count_by_leg: Dict[str, int] = {}

        for idx, leg in enumerate(trade.legs):
            if idx > 0:
                current_time += timedelta(milliseconds=self.config.sequential_leg_delay_ms)

            tiers = available_depth_by_leg.get(leg.contract_id, ())

            # Apply latency-induced quote degradation and depth decay if enabled
            if self.config.enable_latency_decay:
                elapsed_ms = (
                    self.config.decision_latency_ms
                    + self.config.network_latency_ms
                    + idx * self.config.sequential_leg_delay_ms
                )
                if elapsed_ms > self.config.latency_decay_threshold_ms:
                    excess = elapsed_ms - self.config.latency_decay_threshold_ms
                    decay_factor = min(1.0, (excess / 100.0) * self.config.latency_decay_rate_per_100ms)
                    slippage = 1 if elapsed_ms >= self.config.adverse_price_move_threshold_ms else 0
                    decayed_tiers = []
                    for p_cents, qty in tiers:
                        p_adj = min(99, p_cents + slippage)
                        q_adj = max(0, int(qty * (1.0 - decay_factor)))
                        if q_adj > 0:
                            decayed_tiers.append((p_adj, q_adj))
                    tiers = tuple(decayed_tiers)

            # Apply test scenario overrides if requested
            if force_scenario == "LEG_2_REJECTED" and idx == 1:
                updated_legs.append(leg.with_rejection("Counterparty quote cancelled in-flight"))
                continue
            elif force_scenario == "LEG_2_PARTIAL" and idx == 1:
                # Force only 2 contracts filled on leg 2
                tiers = [(tiers[0][0], min(2, target_quantity))] if tiers else ()

            # Walk available depth tiers for this leg
            remaining_to_fill = target_quantity
            leg_cost = 0
            leg_filled = 0
            consumed: List[ConsumedLevel] = []

            for p_cents, qty in tiers:
                if remaining_to_fill <= 0:
                    break
                take = min(remaining_to_fill, qty)
                cost = take * p_cents
                consumed.append(
                    ConsumedLevel(
                        price_cents=p_cents,
                        price_dollars=cents_to_dollars(p_cents),
                        quantity=take,
                        cost_cents=cost,
                        cost_dollars=cents_to_dollars(cost),
                    )
                )
                leg_filled += take
                leg_cost += cost
                remaining_to_fill -= take

            # Calculate verified dynamic fees via FeeModel
            if leg_filled > 0:
                fill_tiers = [FillTier(lvl.price_cents, lvl.quantity) for lvl in consumed]
                fee_calc = calculate_multi_level_fee(
                    fill_tiers, config=self.config.fee_config, series_ticker=leg.market_ticker
                )
                leg_fees = fee_calc.total_fee_cents
                filled_leg = leg.with_fill(leg_filled, leg_cost, leg_fees, current_time, consumed)
                updated_legs.append(filled_leg)
                fill_count_by_leg[leg.contract_id] = leg_filled
            else:
                updated_legs.append(leg.with_rejection("Zero executable liquidity at limit price"))
                fill_count_by_leg[leg.contract_id] = 0

        # Evaluate outcome across all legs
        filled_quantities = [l.filled_quantity for l in updated_legs]
        all_full = all(q == target_quantity for q in filled_quantities)
        all_zero = all(q == 0 for q in filled_quantities)
        all_equal = len(set(filled_quantities)) == 1 and not all_zero

        if all_full:
            # Perfect complete arbitrage fill!
            trade = trade.transition_to(
                MultiLegLifecycleState.FILLED,
                reason="All legs fully executed across depth",
                timestamp=current_time,
                updated_legs=updated_legs,
            )
            return trade

        if all_zero:
            # Clean failure with 0 exposure
            trade = trade.transition_to(
                MultiLegLifecycleState.FAILED,
                reason="All legs rejected or lacked depth; zero capital committed",
                timestamp=current_time,
                updated_legs=updated_legs,
            )
            return trade

        if all_equal:
            # Symmetric partial fill: e.g. 5 on leg 1 and 5 on leg 2
            # Still valid arbitrage, but partial!
            trade = trade.transition_to(
                MultiLegLifecycleState.PARTIALLY_FILLED,
                reason=f"Symmetric partial fill of {filled_quantities[0]}/{target_quantity} units",
                timestamp=current_time,
                updated_legs=updated_legs,
            )
            return trade

        # 4. Asymmetric Fill -> LEGGED_OUT!
        trade = trade.transition_to(
            MultiLegLifecycleState.LEGGED_OUT,
            reason="Asymmetric fill across legs: unhedged directional exposure incurred",
            timestamp=current_time,
            updated_legs=updated_legs,
        )

        # 5. Execute Unwind / Scratch Recovery
        t_unwind = current_time + timedelta(milliseconds=self.config.scratch_timeout_ms)
        trade = trade.transition_to(
            MultiLegLifecycleState.UNWINDING,
            reason=f"Initiating scratch policy {self.config.unwind_policy.value} after timeout",
            timestamp=t_unwind,
        )

        unwound_trade = self._execute_unwind(
            trade, counterparty_bids_by_leg, available_depth_by_leg, t_unwind
        )
        return unwound_trade

    def _execute_unwind(
        self,
        trade: AsyncMultiLegTrade,
        bids_by_leg: Mapping[str, Sequence[Tuple[int, int]]],
        asks_by_leg: Mapping[str, Sequence[Tuple[int, int]]],
        unwind_time: datetime,
    ) -> AsyncMultiLegTrade:
        """Execute recovery policy to eliminate unhedged residual exposure."""
        policy = self.config.unwind_policy
        exp = trade.residual_exposure

        if not exp:
            return trade.transition_to(
                MultiLegLifecycleState.CLOSED,
                reason="Zero residual exposure; trade closed",
                timestamp=unwind_time,
            )

        if policy == UnwindPolicy.EMERGENCY_DUMP:
            # Sell the excess filled contracts to resting bids immediately
            realized_scratch_loss = 0
            dump_proceeds = 0
            unwind_fees = 0
            actually_dumped: Dict[Tuple[str, str], int] = {}

            for (cid, side), excess_qty in exp.items():
                leg = next(l for l in trade.legs if l.contract_id == cid and l.side == side)
                unit_cost = int(leg.average_price_cents or 0)

                # Walk resting bids for this contract
                bids = bids_by_leg.get(cid, ())
                rem = excess_qty
                proceeds = 0
                consumed_dump_tiers: List[FillTier] = []

                for bid_p, b_qty in bids:
                    if rem <= 0:
                        break
                    take = min(rem, b_qty)
                    proceeds += take * bid_p
                    consumed_dump_tiers.append(FillTier(price_cents=bid_p, quantity=take))
                    rem -= take

                dumped_qty = excess_qty - rem
                if dumped_qty > 0:
                    actually_dumped[(cid, side)] = dumped_qty
                    cost_of_dumped = dumped_qty * unit_cost
                    dump_proceeds += proceeds
                    # Calculate fee for dumping contracts based on actual consumed tiers
                    fee_c = calculate_multi_level_fee(
                        consumed_dump_tiers,
                        config=self.config.fee_config,
                        series_ticker=leg.market_ticker,
                    ).total_fee_cents
                    unwind_fees += fee_c
                    loss = cost_of_dumped - proceeds + fee_c
                    realized_scratch_loss -= loss

            return trade.transition_to(
                MultiLegLifecycleState.CLOSED,
                reason=f"Emergency dump completed: liquidated {sum(actually_dumped.values())} unhedged contracts with {realized_scratch_loss}¢ PnL",
                timestamp=unwind_time + timedelta(milliseconds=20),
                realized_pnl_delta_cents=realized_scratch_loss,
                unwind_proceeds_cents=dump_proceeds,
                liquidated_positions=actually_dumped,
            )

        elif policy == UnwindPolicy.AGGRESSIVE_CROSS:
            # Buy the missing contracts on the incomplete leg even if slipping
            common = min(l.filled_quantity for l in trade.legs)
            lagging_legs = [l for l in trade.legs if l.filled_quantity == common]
            leading_qty = max(l.filled_quantity for l in trade.legs)
            extra_cost_total = 0
            extra_fees_total = 0
            updated_legs_map = {l.contract_id: l for l in trade.legs}
            all_filled = True

            for l in lagging_legs:
                needed = leading_qty - l.filled_quantity
                asks = asks_by_leg.get(l.contract_id, ())
                rem = needed
                consumed: List[ConsumedLevel] = []
                extra_cost = 0

                for ask_p, qty in asks:
                    if rem <= 0:
                        break
                    take = min(rem, qty)
                    cost = take * ask_p
                    consumed.append(
                        ConsumedLevel(
                            price_cents=ask_p,
                            price_dollars=cents_to_dollars(ask_p),
                            quantity=take,
                            cost_cents=cost,
                            cost_dollars=cents_to_dollars(cost),
                        )
                    )
                    extra_cost += cost
                    rem -= take

                crossed_qty = needed - rem
                if rem > 0:
                    all_filled = False

                if crossed_qty > 0:
                    fill_tiers = [FillTier(price_cents=c.price_cents, quantity=c.quantity) for c in consumed]
                    fee_c = calculate_multi_level_fee(
                        fill_tiers,
                        config=self.config.fee_config,
                        series_ticker=l.market_ticker,
                    ).total_fee_cents
                    extra_cost_total += extra_cost
                    extra_fees_total += fee_c
                    updated_legs_map[l.contract_id] = l.with_fill(
                        filled_qty=crossed_qty,
                        cost_cents=extra_cost,
                        fees_cents=fee_c,
                        fill_time=unwind_time + timedelta(milliseconds=30),
                        consumed_levels=consumed,
                    )

            updated_legs = tuple(updated_legs_map[l.contract_id] for l in trade.legs)

            if all_filled:
                return trade.transition_to(
                    MultiLegLifecycleState.CLOSED,
                    reason=f"Aggressive cross completed: hedge completed paying {extra_cost_total}¢ extra cost and {extra_fees_total}¢ fees",
                    timestamp=unwind_time + timedelta(milliseconds=30),
                    updated_legs=updated_legs,
                    realized_pnl_delta_cents=0,
                    liquidated_positions={},
                )
            else:
                return trade.transition_to(
                    MultiLegLifecycleState.CLOSED,
                    reason=f"Aggressive cross partially filled: filled {leading_qty - min(l.filled_quantity for l in updated_legs)} missing contracts; remaining depth exhausted",
                    timestamp=unwind_time + timedelta(milliseconds=30),
                    updated_legs=updated_legs,
                    realized_pnl_delta_cents=0,
                    liquidated_positions={},
                )

        else:
            # CONSERVATIVE_LIMIT: Failed to fill within timeout
            return trade.transition_to(
                MultiLegLifecycleState.FAILED,
                reason="Conservative limit order timed out unfilled during unwind",
                timestamp=unwind_time + timedelta(milliseconds=500),
                liquidated_positions={},
            )


def apply_async_trade_to_portfolio(
    portfolio: PaperPortfolio,
    trade: AsyncMultiLegTrade,
    timestamp: Optional[str] = None,
) -> List[LedgerEntry]:
    """Reconciles an AsyncMultiLegTrade with PaperPortfolio, maintaining 100% financial integrity."""
    if not isinstance(portfolio, PaperPortfolio):
        raise LedgerError("portfolio must be a PaperPortfolio instance")
    if not isinstance(trade, AsyncMultiLegTrade):
        raise LedgerError("trade must be an AsyncMultiLegTrade instance")

    entries: List[LedgerEntry] = []
    ts = timestamp or _utc_now().isoformat()

    # 1. Apply fills for each filled leg
    pos_deltas: List[PositionDelta] = []
    total_cost = trade.total_cost_cents
    total_fees = trade.total_fees_cents
    total_cash_needed = total_cost + total_fees

    if total_cash_needed > 0 and portfolio.available_cash_cents < total_cash_needed:
        raise LedgerError(
            f"Insufficient cash ({portfolio.available_cash_cents}¢) to fund async trade fills ({total_cash_needed}¢)"
        )

    for leg in trade.legs:
        if leg.filled_quantity <= 0:
            continue
        key = (leg.contract_id, leg.side)
        if key in portfolio._positions:
            portfolio._positions[key] = portfolio._positions[key].add(
                leg.filled_quantity, leg.total_cost_cents
            )
        else:
            from .ledger import PositionHolding
            portfolio._positions[key] = PositionHolding.create(
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

    portfolio._available_cash_cents -= total_cash_needed
    portfolio._total_fees_cents += total_fees

    fill_entry = LedgerEntry(
        entry_id=f"ent-{uuid.uuid4().hex[:12]}",
        entry_type=LedgerEntryType.FILL,
        timestamp=ts,
        trade_id=trade.trade_id,
        cash_delta_cents=-total_cash_needed,
        reserved_delta_cents=0,
        fees_delta_cents=total_fees,
        realized_pnl_delta_cents=0,
        positions_delta=tuple(pos_deltas),
        balance_after_cents=portfolio._available_cash_cents,
        reserved_after_cents=portfolio._reserved_cash_cents,
        description=f"Async multi-leg fills for trade {trade.trade_id} ({trade.state.value})",
    )
    portfolio._ledger.append(fill_entry)
    entries.append(fill_entry)

    # 2. If trade was unwound via emergency dump, liquidate ONLY the actually dumped holdings
    if trade.liquidated_positions:
        # Emergency dump liquidation of actually dumped positions
        unwind_deltas: List[PositionDelta] = []
        for (cid, side), dumped_q in trade.liquidated_positions.items():
            if dumped_q <= 0:
                continue
            key = (cid, side)
            if key in portfolio._positions:
                holding = portfolio._positions[key]
                close_q = min(holding.quantity, dumped_q)
                cost_portion = int(holding.total_cost_cents * (close_q / holding.quantity))
                if holding.quantity == close_q:
                    del portfolio._positions[key]
                else:
                    new_q = holding.quantity - close_q
                    new_c = holding.total_cost_cents - cost_portion
                    from .ledger import PositionHolding
                    portfolio._positions[key] = PositionHolding.create(cid, side, new_q, new_c)
                unwind_deltas.append(
                    PositionDelta(
                        contract_id=cid,
                        side=side,
                        quantity_delta=-close_q,
                        cost_delta_cents=-cost_portion,
                    )
                )

        portfolio._available_cash_cents += trade.unwind_proceeds_cents
        portfolio._realized_pnl_cents += trade.realized_pnl_cents

        unwind_entry = LedgerEntry(
            entry_id=f"ent-{uuid.uuid4().hex[:12]}",
            entry_type=LedgerEntryType.SETTLEMENT,
            timestamp=ts,
            trade_id=trade.trade_id,
            cash_delta_cents=trade.unwind_proceeds_cents,
            reserved_delta_cents=0,
            fees_delta_cents=0,
            realized_pnl_delta_cents=trade.realized_pnl_cents,
            positions_delta=tuple(unwind_deltas),
            balance_after_cents=portfolio._available_cash_cents,
            reserved_after_cents=portfolio._reserved_cash_cents,
            description=f"Async trade unwind liquidation for trade {trade.trade_id}: PnL {trade.realized_pnl_cents}¢",
        )
        portfolio._ledger.append(unwind_entry)
        entries.append(unwind_entry)

    portfolio._processed_trade_ids.add(trade.trade_id)
    return entries
