"""Atomic state persistence, schema versioning, and crash-recovery for autonomous paper trading.

V9 Paper State Store:
- Schema version 2.0 with backward compatibility support for 1.0.
- Mandatory execution_mode == "paper" validation.
- Full persistent fidelity: portfolio, ledger entries, async multi-leg trades,
  cycle summaries, and audit recovery events.
- Atomic file writes (temp file + fsync + atomic rename).
- Crash-recovery manager:
  - Detects interrupted trades (EXECUTING, UNWINDING).
  - Reconciles orphan reservations.
  - Preserves partial fills and residual exposures without fabricating fills.
  - Fails closed on corrupted JSON or failed reconciliation.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from .arbitrage import cents_to_dollars
from .async_execution import (
    AsyncMultiLegTrade,
    AsyncTradeLeg,
    ExecutionTimestamps,
    LegExecutionStatus,
    LegSequencingPolicy,
    MultiLegLifecycleState,
    UnwindPolicy,
)
from .execution import ConsumedLevel
from .ledger import (
    LedgerEntry,
    LedgerEntryType,
    PaperPortfolio,
    PositionDelta,
    PositionHolding,
    ReconciliationResult,
)
from .paper_risk import PaperRiskConfig
from .persistence import (
    CorruptedStateError,
    PersistenceError,
    ReconciliationRecoveryError,
    _ledger_entry_from_dict,
    _ledger_entry_to_dict,
)

logger = logging.getLogger(__name__)

PAPER_SCHEMA_VERSION = "2.0"


def _consumed_level_to_dict(lvl: ConsumedLevel) -> Dict[str, Any]:
    return {
        "price_cents": lvl.price_cents,
        "quantity": lvl.quantity,
        "cost_cents": lvl.cost_cents,
    }


def _consumed_level_from_dict(d: Dict[str, Any]) -> ConsumedLevel:
    price_cents = int(d["price_cents"])
    qty = int(d["quantity"])
    cost_cents = int(d["cost_cents"])
    return ConsumedLevel(
        price_cents=price_cents,
        price_dollars=cents_to_dollars(price_cents),
        quantity=qty,
        cost_cents=cost_cents,
        cost_dollars=cents_to_dollars(cost_cents),
    )


def _async_leg_to_dict(leg: AsyncTradeLeg) -> Dict[str, Any]:
    return {
        "contract_id": leg.contract_id,
        "market_ticker": leg.market_ticker,
        "side": leg.side,
        "intended_quantity": leg.intended_quantity,
        "filled_quantity": leg.filled_quantity,
        "remaining_quantity": leg.remaining_quantity,
        "total_cost_cents": leg.total_cost_cents,
        "fees_cents": leg.fees_cents,
        "average_price_cents": (
            str(leg.average_price_cents) if leg.average_price_cents is not None else None
        ),
        "status": leg.status.value,
        "consumed_levels": [_consumed_level_to_dict(cl) for cl in leg.consumed_levels],
        "rejection_reason": leg.rejection_reason,
    }


def _async_leg_from_dict(d: Dict[str, Any]) -> AsyncTradeLeg:
    cost_cents = int(d["total_cost_cents"])
    fees_cents = int(d["fees_cents"])
    qty = int(d["filled_quantity"])
    avg_price = Decimal(d["average_price_cents"]) if d.get("average_price_cents") else None
    levels = tuple(_consumed_level_from_dict(cd) for cd in d.get("consumed_levels", []))

    return AsyncTradeLeg(
        contract_id=str(d["contract_id"]),
        market_ticker=str(d["market_ticker"]),
        side=str(d["side"]),
        intended_quantity=int(d["intended_quantity"]),
        filled_quantity=qty,
        remaining_quantity=int(d["remaining_quantity"]),
        total_cost_cents=cost_cents,
        fees_cents=fees_cents,
        average_price_cents=avg_price,
        status=LegExecutionStatus(d["status"]),
        consumed_levels=levels,
        rejection_reason=d.get("rejection_reason"),
    )


def _async_trade_to_dict(trade: AsyncMultiLegTrade) -> Dict[str, Any]:
    liq_pos = {
        f"{cid}:{side}": qty for (cid, side), qty in trade.liquidated_positions.items()
    }
    return {
        "trade_id": trade.trade_id,
        "strategy_type": trade.strategy_type,
        "target_quantity": trade.target_quantity,
        "state": trade.state.value,
        "sequencing_policy": trade.sequencing_policy.value,
        "unwind_policy": trade.unwind_policy.value,
        "total_cost_cents": trade.total_cost_cents,
        "total_fees_cents": trade.total_fees_cents,
        "realized_pnl_cents": trade.realized_pnl_cents,
        "unwind_proceeds_cents": trade.unwind_proceeds_cents,
        "rejection_reason": trade.rejection_reason,
        "history": list(trade.history),
        "liquidated_positions": liq_pos,
        "legs": [_async_leg_to_dict(leg) for leg in trade.legs],
    }


def _async_trade_from_dict(d: Dict[str, Any]) -> AsyncMultiLegTrade:
    legs = tuple(_async_leg_from_dict(ld) for ld in d.get("legs", []))
    liq_pos = {}
    for k, v in d.get("liquidated_positions", {}).items():
        parts = k.split(":")
        if len(parts) == 2:
            liq_pos[(parts[0], parts[1])] = int(v)

    return AsyncMultiLegTrade(
        trade_id=str(d["trade_id"]),
        strategy_type=str(d["strategy_type"]),
        target_quantity=int(d["target_quantity"]),
        legs=legs,
        state=MultiLegLifecycleState(d["state"]),
        sequencing_policy=LegSequencingPolicy(d.get("sequencing_policy", LegSequencingPolicy.ILLIQUID_FIRST.value)),
        unwind_policy=UnwindPolicy(d.get("unwind_policy", UnwindPolicy.AGGRESSIVE_CROSS.value)),
        total_cost_cents=int(d["total_cost_cents"]),
        total_fees_cents=int(d["total_fees_cents"]),
        realized_pnl_cents=int(d.get("realized_pnl_cents", 0)),
        unwind_proceeds_cents=int(d.get("unwind_proceeds_cents", 0)),
        rejection_reason=d.get("rejection_reason"),
        history=tuple(d.get("history", ())),
        liquidated_positions=liq_pos,
    )


@dataclass
class PaperSessionState:
    """Encapsulates durable paper trading runtime state."""

    session_id: str
    execution_mode: str = "paper"
    status: str = "STOPPED"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    initial_cash_cents: int = 1_000_000
    risk_config: PaperRiskConfig = field(default_factory=PaperRiskConfig)
    portfolio: PaperPortfolio = field(default_factory=lambda: PaperPortfolio(initial_cash_cents=1_000_000))
    trades: Dict[str, AsyncMultiLegTrade] = field(default_factory=dict)
    cycle_history: List[Dict[str, Any]] = field(default_factory=list)
    last_completed_cycle: int = 0
    recovery_events: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.execution_mode != "paper":
            raise CorruptedStateError(
                f"Invalid execution_mode {self.execution_mode!r}. Only 'paper' mode is permitted."
            )


class PaperStateStore:
    """Manages crash-resilient atomic storage and recovery of PaperSessionState."""

    def __init__(self, filepath: Union[str, Path]) -> None:
        self.filepath = Path(filepath)

    def save(self, state: PaperSessionState) -> None:
        """Atomically persist paper session state with fsync."""
        if state.execution_mode != "paper":
            raise PersistenceError("Refusing to save state: execution_mode must be 'paper'")

        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.filepath.with_suffix(f"{self.filepath.suffix}.tmp")

        state.updated_at = datetime.now(timezone.utc).isoformat()
        rc = state.risk_config

        payload = {
            "schema_version": PAPER_SCHEMA_VERSION,
            "session_id": state.session_id,
            "execution_mode": state.execution_mode,
            "status": state.status,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "initial_cash_cents": state.initial_cash_cents,
            "last_completed_cycle": state.last_completed_cycle,
            "recovery_events": state.recovery_events,
            "risk_config": {
                "max_cost_per_trade_cents": rc.max_cost_per_trade_cents,
                "max_requested_quantity": rc.max_requested_quantity,
                "max_position_size": rc.max_position_size,
                "max_aggregate_exposure_cents": rc.max_aggregate_exposure_cents,
                "max_concurrent_trades": rc.max_concurrent_trades,
                "max_daily_realized_loss_cents": rc.max_daily_realized_loss_cents,
                "max_drawdown_cents": rc.max_drawdown_cents,
                "max_data_age_seconds": rc.max_data_age_seconds,
                "max_consecutive_failures": rc.max_consecutive_failures,
                "require_positive_gross_edge": rc.require_positive_gross_edge,
                "require_positive_net_edge": rc.require_positive_net_edge,
                "allow_duplicate_trades": rc.allow_duplicate_trades,
            },
            "portfolio": {
                "available_cash_cents": state.portfolio.available_cash_cents,
                "reserved_cash_cents": state.portfolio.reserved_cash_cents,
                "total_fees_cents": state.portfolio.total_fees_paid_cents,
                "realized_pnl_cents": state.portfolio.realized_pnl_cents,
                "processed_trade_ids": list(state.portfolio.processed_trade_ids),
                "positions": [
                    {
                        "contract_id": pos.contract_id,
                        "side": pos.side,
                        "quantity": pos.quantity,
                        "total_cost_cents": pos.total_cost_cents,
                    }
                    for pos in state.portfolio.positions.values()
                ],
            },
            "ledger": [_ledger_entry_to_dict(entry) for entry in state.portfolio.ledger],
            "trades": [_async_trade_to_dict(t) for t in state.trades.values()],
            "cycle_history": state.cycle_history[-100:],  # Store recent 100 summaries
        }

        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.filepath)
        except Exception as exc:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise PersistenceError(f"Failed to persist state to {self.filepath}: {exc}") from exc

    def load_and_recover(self) -> PaperSessionState:
        """Loads state from disk, validates integrity, handles interrupted operations, and reconciles."""
        if not self.filepath.exists():
            raise PersistenceError(f"State file does not exist: {self.filepath}")

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise CorruptedStateError(f"Failed to parse JSON state file {self.filepath}: {exc}") from exc

        if not isinstance(data, dict):
            raise CorruptedStateError("Root state must be a JSON object")

        version = data.get("schema_version")
        if version not in ("1.0", PAPER_SCHEMA_VERSION):
            raise CorruptedStateError(
                f"Unsupported schema version {version!r}. Expected {PAPER_SCHEMA_VERSION!r} or '1.0'"
            )

        mode = data.get("execution_mode", "paper")
        if mode != "paper":
            raise CorruptedStateError(f"Prohibited execution mode {mode!r}. Must be 'paper'")

        session_id = str(data.get("session_id", "recovered-session"))
        status = str(data.get("status", "STOPPED"))
        created_at = str(data.get("created_at", datetime.now(timezone.utc).isoformat()))
        updated_at = str(data.get("updated_at", datetime.now(timezone.utc).isoformat()))
        initial_cash = int(data.get("initial_cash_cents", 1_000_000))
        last_cycle = int(data.get("last_completed_cycle", 0))
        recovery_events = list(data.get("recovery_events", []))
        cycle_history = list(data.get("cycle_history", []))

        # 1. Reconstruct PaperRiskConfig
        rc_data = data.get("risk_config", {})
        risk_config = PaperRiskConfig(
            max_cost_per_trade_cents=int(rc_data.get("max_cost_per_trade_cents", 50_000)),
            max_requested_quantity=int(rc_data.get("max_requested_quantity", 1_000)),
            max_position_size=int(rc_data.get("max_position_size", 1_000)),
            max_aggregate_exposure_cents=int(rc_data.get("max_aggregate_exposure_cents", 200_000)),
            max_concurrent_trades=int(rc_data.get("max_concurrent_trades", 5)),
            max_daily_realized_loss_cents=int(rc_data.get("max_daily_realized_loss_cents", 20_000)),
            max_drawdown_cents=int(rc_data.get("max_drawdown_cents", 50_000)),
            max_data_age_seconds=float(rc_data.get("max_data_age_seconds", 60.0)),
            max_consecutive_failures=int(rc_data.get("max_consecutive_failures", 5)),
            require_positive_gross_edge=bool(rc_data.get("require_positive_gross_edge", True)),
            require_positive_net_edge=bool(rc_data.get("require_positive_net_edge", True)),
            allow_duplicate_trades=bool(rc_data.get("allow_duplicate_trades", False)),
        )

        # 2. Reconstruct PaperPortfolio
        portfolio = PaperPortfolio(initial_cash_cents=0)
        p_data = data.get("portfolio", {})
        portfolio._available_cash_cents = int(p_data["available_cash_cents"])
        portfolio._reserved_cash_cents = int(p_data["reserved_cash_cents"])
        portfolio._total_fees_cents = int(p_data["total_fees_cents"])
        portfolio._realized_pnl_cents = int(p_data["realized_pnl_cents"])
        portfolio._processed_trade_ids = set(p_data.get("processed_trade_ids", []))

        for pos_data in p_data.get("positions", []):
            cid = str(pos_data["contract_id"])
            side = str(pos_data["side"])
            qty = int(pos_data["quantity"])
            cost = int(pos_data["total_cost_cents"])
            portfolio._positions[(cid, side)] = PositionHolding.create(cid, side, qty, cost)

        # 3. Reconstruct Ledger
        portfolio._ledger = [
            _ledger_entry_from_dict(entry_d) for entry_d in data.get("ledger", [])
        ]

        # 4. Reconstruct Trades
        trades: Dict[str, AsyncMultiLegTrade] = {}
        for td in data.get("trades", []):
            trade = _async_trade_from_dict(td)
            trades[trade.trade_id] = trade

        # 5. Handle interrupted trades (crash recovery)
        now_iso = datetime.now(timezone.utc).isoformat()
        interrupted_count = 0
        for trade_id, trade in list(trades.items()):
            if trade.state in (
                MultiLegLifecycleState.PLANNED,
                MultiLegLifecycleState.EXECUTING,
                MultiLegLifecycleState.UNWINDING,
            ):
                interrupted_count += 1
                has_ledger_fill = any(
                    e.entry_type == LedgerEntryType.FILL and e.trade_id == trade_id
                    for e in portfolio.ledger
                )
                if not has_ledger_fill:
                    # No fills recorded: safely fail trade and release any reserved cash
                    trade_failed = trade.transition_to(
                        MultiLegLifecycleState.FAILED,
                        reason="Process crash/restart during execution before fills were recorded",
                    )
                    trades[trade_id] = trade_failed
                    recovery_events.append({
                        "timestamp": now_iso,
                        "trade_id": trade_id,
                        "action": "TRANSITION_TO_FAILED",
                        "reason": "Process interrupted before fills recorded",
                    })
                else:
                    # Fills were already recorded in ledger: retain positions, mark closed or partially filled
                    if trade.is_arbitrage_complete:
                        filled_trade = (
                            trade.transition_to(MultiLegLifecycleState.FILLED, reason="Fills verified from ledger")
                            if trade.state == MultiLegLifecycleState.EXECUTING
                            else trade
                        )
                        trades[trade_id] = filled_trade.transition_to(
                            MultiLegLifecycleState.CLOSED,
                            reason="Recovered completed trade from persisted ledger",
                        )
                    else:
                        trades[trade_id] = (
                            trade.transition_to(MultiLegLifecycleState.PARTIALLY_FILLED, reason="Partial fills verified from ledger")
                            if trade.state == MultiLegLifecycleState.EXECUTING
                            else trade
                        )
                    recovery_events.append({
                        "timestamp": now_iso,
                        "trade_id": trade_id,
                        "action": "RECOVER_EXISTING_FILLS",
                        "state": trades[trade_id].state.value,
                        "reason": "Preserving existing ledger fills",
                    })

        # 6. Reconcile orphan reservations
        if portfolio.reserved_cash_cents > 0:
            # If all active trades are now resolved, unlock reserved cash
            active_unfilled = any(
                t.state in (MultiLegLifecycleState.PLANNED, MultiLegLifecycleState.EXECUTING)
                for t in trades.values()
            )
            if not active_unfilled:
                recovery_events.append({
                    "timestamp": now_iso,
                    "action": "RELEASE_ORPHAN_RESERVATION",
                    "amount_cents": portfolio.reserved_cash_cents,
                })
                portfolio._available_cash_cents += portfolio.reserved_cash_cents
                portfolio._reserved_cash_cents = 0

        # 7. Independent Ledger Reconciliation Audit (Fail closed if corrupt!)
        rec = portfolio.reconcile()
        if not rec.is_reconciled:
            raise ReconciliationRecoveryError(
                f"Recovered state failed independent audit reconciliation: {rec.discrepancies}"
            )

        logger.info(
            f"State recovered cleanly from {self.filepath} (session={session_id}, "
            f"interrupted_trades={interrupted_count}, positions={len(portfolio.positions)}, "
            f"cash={portfolio.available_cash_cents}¢)"
        )

        return PaperSessionState(
            session_id=session_id,
            execution_mode="paper",
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            initial_cash_cents=initial_cash,
            risk_config=risk_config,
            portfolio=portfolio,
            trades=trades,
            cycle_history=cycle_history,
            last_completed_cycle=last_cycle,
            recovery_events=recovery_events,
        )
