"""Atomic persistence, schema versioning, and crash-recovery for paper trading.

V4 Persistence ensures trade records, ledger entries, and portfolio state can be
saved atomically and recovered with automated post-restart audit reconciliation.
"""

from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .arbitrage import cents_to_dollars
from .ledger import (
    LedgerEntry,
    LedgerEntryType,
    PaperPortfolio,
    PositionDelta,
    PositionHolding,
)
from .paper_trade import PaperTrade, TradeLeg, TradeState
from .risk import RiskConfig

SCHEMA_VERSION = "1.0"


class PersistenceError(Exception):
    """Base exception for persistence and recovery failures."""


class CorruptedStateError(PersistenceError):
    """Raised when persisted data is malformed, incomplete, or incompatible."""


class ReconciliationRecoveryError(PersistenceError):
    """Raised when restored state fails independent audit reconciliation."""


def _trade_leg_to_dict(leg: TradeLeg) -> Dict[str, Any]:
    return {
        "contract_id": leg.contract_id,
        "market_ticker": leg.market_ticker,
        "side": leg.side,
        "requested_quantity": leg.requested_quantity,
        "filled_quantity": leg.filled_quantity,
        "total_cost_cents": leg.total_cost_cents,
        "average_price_cents": (
            str(leg.average_price_cents) if leg.average_price_cents is not None else None
        ),
    }


def _trade_leg_from_dict(d: Dict[str, Any]) -> TradeLeg:
    cost_cents = int(d["total_cost_cents"])
    qty = int(d["filled_quantity"])
    avg_price = Decimal(d["average_price_cents"]) if d.get("average_price_cents") else None
    return TradeLeg(
        contract_id=str(d["contract_id"]),
        market_ticker=str(d["market_ticker"]),
        side=str(d["side"]),
        requested_quantity=int(d["requested_quantity"]),
        filled_quantity=qty,
        total_cost_cents=cost_cents,
        total_cost_dollars=cents_to_dollars(cost_cents),
        average_price_cents=avg_price,
        average_price_dollars=cents_to_dollars(cost_cents) / Decimal(qty) if qty > 0 else None,
        consumed_levels=(),
    )


def _trade_to_dict(trade: PaperTrade) -> Dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "strategy_type": trade.strategy_type,
        "state": trade.state.value,
        "requested_quantity": trade.requested_quantity,
        "filled_quantity": trade.filled_quantity,
        "unfilled_quantity": trade.unfilled_quantity,
        "total_cost_cents": trade.total_cost_cents,
        "estimated_fees_cents": trade.estimated_fees_cents,
        "guaranteed_payout_cents": trade.guaranteed_payout_cents,
        "created_at": trade.created_at,
        "evaluated_at": trade.evaluated_at,
        "rejection_reason": trade.rejection_reason,
        "state_history": [list(h) for h in trade.state_history],
        "legs": [_trade_leg_to_dict(leg) for leg in trade.legs],
    }


def _trade_from_dict(d: Dict[str, Any]) -> PaperTrade:
    cost_cents = int(d["total_cost_cents"])
    fees_cents = int(d["estimated_fees_cents"])
    payout_cents = int(d["guaranteed_payout_cents"])
    legs = tuple(_trade_leg_from_dict(leg_d) for leg_d in d.get("legs", []))
    history = tuple(tuple(h) for h in d.get("state_history", []))

    return PaperTrade(
        trade_id=str(d["trade_id"]),
        strategy_type=str(d["strategy_type"]),
        state=TradeState(d["state"]),
        requested_quantity=int(d["requested_quantity"]),
        filled_quantity=int(d["filled_quantity"]),
        unfilled_quantity=int(d["unfilled_quantity"]),
        total_cost_cents=cost_cents,
        total_cost_dollars=cents_to_dollars(cost_cents),
        estimated_fees_cents=fees_cents,
        estimated_fees_dollars=cents_to_dollars(fees_cents),
        guaranteed_payout_cents=payout_cents,
        guaranteed_payout_dollars=cents_to_dollars(payout_cents),
        legs=legs,
        created_at=str(d["created_at"]),
        evaluated_at=str(d["evaluated_at"]),
        rejection_reason=d.get("rejection_reason"),
        state_history=history,
    )


def _ledger_entry_to_dict(entry: LedgerEntry) -> Dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "entry_type": entry.entry_type.value,
        "timestamp": entry.timestamp,
        "trade_id": entry.trade_id,
        "cash_delta_cents": entry.cash_delta_cents,
        "reserved_delta_cents": entry.reserved_delta_cents,
        "fees_delta_cents": entry.fees_delta_cents,
        "realized_pnl_delta_cents": entry.realized_pnl_delta_cents,
        "positions_delta": [
            {
                "contract_id": pd.contract_id,
                "side": pd.side,
                "quantity_delta": pd.quantity_delta,
                "cost_delta_cents": pd.cost_delta_cents,
            }
            for pd in entry.positions_delta
        ],
        "balance_after_cents": entry.balance_after_cents,
        "reserved_after_cents": entry.reserved_after_cents,
        "description": entry.description,
    }


def _ledger_entry_from_dict(d: Dict[str, Any]) -> LedgerEntry:
    pos_deltas = tuple(
        PositionDelta(
            contract_id=str(pd["contract_id"]),
            side=str(pd["side"]),
            quantity_delta=int(pd["quantity_delta"]),
            cost_delta_cents=int(pd["cost_delta_cents"]),
        )
        for pd in d.get("positions_delta", [])
    )
    return LedgerEntry(
        entry_id=str(d["entry_id"]),
        entry_type=LedgerEntryType(d["entry_type"]),
        timestamp=str(d["timestamp"]),
        trade_id=d.get("trade_id"),
        cash_delta_cents=int(d["cash_delta_cents"]),
        reserved_delta_cents=int(d["reserved_delta_cents"]),
        fees_delta_cents=int(d["fees_delta_cents"]),
        realized_pnl_delta_cents=int(d["realized_pnl_delta_cents"]),
        positions_delta=pos_deltas,
        balance_after_cents=int(d["balance_after_cents"]),
        reserved_after_cents=int(d["reserved_after_cents"]),
        description=str(d["description"]),
    )


def save_state(
    filepath: Union[str, Path],
    portfolio: PaperPortfolio,
    trades: Sequence[PaperTrade],
    risk_config: Optional[RiskConfig] = None,
) -> None:
    """Atomically persist portfolio state, ledger entries, and trades to disk."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")

    cfg = risk_config or RiskConfig()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "risk_config": {
            "max_cost_per_trade_cents": cfg.max_cost_per_trade_cents,
            "max_requested_quantity": cfg.max_requested_quantity,
            "max_position_size": cfg.max_position_size,
            "max_aggregate_exposure_cents": cfg.max_aggregate_exposure_cents,
            "require_positive_gross_edge": cfg.require_positive_gross_edge,
            "require_positive_net_edge": cfg.require_positive_net_edge,
            "allow_duplicate_trades": cfg.allow_duplicate_trades,
        },
        "portfolio": {
            "available_cash_cents": portfolio.available_cash_cents,
            "reserved_cash_cents": portfolio.reserved_cash_cents,
            "total_fees_cents": portfolio.total_fees_paid_cents,
            "realized_pnl_cents": portfolio.realized_pnl_cents,
            "processed_trade_ids": list(portfolio.processed_trade_ids),
            "positions": [
                {
                    "contract_id": pos.contract_id,
                    "side": pos.side,
                    "quantity": pos.quantity,
                    "total_cost_cents": pos.total_cost_cents,
                }
                for pos in portfolio.positions.values()
            ],
        },
        "ledger": [_ledger_entry_to_dict(entry) for entry in portfolio.ledger],
        "trades": [_trade_to_dict(trade) for trade in trades],
    }

    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink()
        raise PersistenceError(f"Failed to persist state to {path}: {exc}") from exc


def load_state(
    filepath: Union[str, Path],
) -> Tuple[PaperPortfolio, Tuple[PaperTrade, ...], RiskConfig]:
    """Recover state from disk and verify integrity with independent reconciliation.

    Fails closed if the file is missing, corrupt, or fails audit reconciliation.
    """
    path = Path(filepath)
    if not path.exists():
        raise PersistenceError(f"Persistence file does not exist: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise CorruptedStateError(f"Malformed or unreadable JSON file at {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise CorruptedStateError(f"Expected JSON root object, got {type(data).__name__}")

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise CorruptedStateError(
            f"Unsupported or missing schema version: {version!r} (expected {SCHEMA_VERSION!r})"
        )

    for required_key in ("risk_config", "portfolio", "ledger", "trades"):
        if required_key not in data:
            raise CorruptedStateError(f"Missing required top-level section: {required_key!r}")

    try:
        # Reconstruct RiskConfig
        rc_data = data["risk_config"]
        risk_config = RiskConfig(
            max_cost_per_trade_cents=int(rc_data["max_cost_per_trade_cents"]),
            max_requested_quantity=int(rc_data["max_requested_quantity"]),
            max_position_size=int(rc_data["max_position_size"]),
            max_aggregate_exposure_cents=int(rc_data["max_aggregate_exposure_cents"]),
            require_positive_gross_edge=bool(rc_data["require_positive_gross_edge"]),
            require_positive_net_edge=bool(rc_data["require_positive_net_edge"]),
            allow_duplicate_trades=bool(rc_data["allow_duplicate_trades"]),
        )

        # Reconstruct PaperPortfolio
        portfolio = PaperPortfolio(initial_cash_cents=0)
        p_data = data["portfolio"]
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

        # Reconstruct Ledger entries
        portfolio._ledger = [
            _ledger_entry_from_dict(entry_d) for entry_d in data.get("ledger", [])
        ]

        # Reconstruct Trades
        trades = tuple(_trade_from_dict(td) for td in data.get("trades", []))

    except Exception as exc:
        raise CorruptedStateError(f"Failed to decode state components: {exc}") from exc

    # Fail closed: Run independent reconciliation before returning!
    rec_result = portfolio.reconcile()
    if not rec_result.is_reconciled:
        raise ReconciliationRecoveryError(
            f"State recovery failed audit reconciliation: {rec_result.discrepancies}"
        )

    # Fail closed: Cross-validate trade records against ledger entries
    _validate_trade_ledger_consistency(portfolio, trades)

    return portfolio, trades, risk_config


def _validate_trade_ledger_consistency(
    portfolio: PaperPortfolio,
    trades: Sequence[PaperTrade],
) -> None:
    """Cross-validate trade records against ledger journal entries and processed IDs.

    Fails closed with ReconciliationRecoveryError if trade records diverge from
    the financial events recorded in the ledger. Note: this verifies structural and
    accounting consistency across persisted sections; it is not cryptographic proof.
    """
    trades_by_id: Dict[str, PaperTrade] = {}
    for t in trades:
        if t.trade_id in trades_by_id:
            raise ReconciliationRecoveryError(
                f"Duplicate trade_id {t.trade_id!r} detected in persisted trades list"
            )
        trades_by_id[t.trade_id] = t

    # 1. Index ledger FILL entries by trade_id
    ledger_fills: Dict[str, List[LedgerEntry]] = {}
    for entry in portfolio.ledger:
        if entry.entry_type == LedgerEntryType.FILL:
            if not entry.trade_id:
                raise ReconciliationRecoveryError(
                    f"Ledger FILL entry {entry.entry_id} has no trade_id"
                )
            ledger_fills.setdefault(entry.trade_id, []).append(entry)

    # 2. Verify every FILL entry corresponds to a known trade
    for trade_id, fill_entries in ledger_fills.items():
        if trade_id not in trades_by_id:
            raise ReconciliationRecoveryError(
                f"Ledger contains FILL entry for trade {trade_id!r}, but trade record is missing from trades"
            )
        trade = trades_by_id[trade_id]
        if trade.state not in (TradeState.FILLED, TradeState.PARTIALLY_FILLED):
            raise ReconciliationRecoveryError(
                f"Trade {trade_id} has state {trade.state.value} in trades record, "
                f"but has a FILL entry in the ledger"
            )
        if trade_id not in portfolio.processed_trade_ids:
            raise ReconciliationRecoveryError(
                f"Trade {trade_id} has a FILL entry in the ledger, but is missing from portfolio.processed_trade_ids"
            )

        # Verify financial consistency between trade record and ledger entry
        total_ledger_fees = sum(fe.fees_delta_cents for fe in fill_entries)
        if total_ledger_fees != trade.estimated_fees_cents:
            raise ReconciliationRecoveryError(
                f"Trade {trade_id} fees ({trade.estimated_fees_cents}¢) do not match "
                f"ledger fill fees ({total_ledger_fees}¢)"
            )

        total_ledger_pos_cost = sum(
            pd.cost_delta_cents
            for fe in fill_entries
            for pd in fe.positions_delta
        )
        if total_ledger_pos_cost != trade.total_cost_cents:
            raise ReconciliationRecoveryError(
                f"Trade {trade_id} total cost ({trade.total_cost_cents}¢) does not match "
                f"ledger positions cost ({total_ledger_pos_cost}¢)"
            )

    # 3. Verify every filled or partially filled trade in trades has a matching FILL in ledger
    for trade in trades:
        if trade.state in (TradeState.FILLED, TradeState.PARTIALLY_FILLED):
            if trade.trade_id not in ledger_fills:
                raise ReconciliationRecoveryError(
                    f"Trade {trade.trade_id} is marked {trade.state.value}, "
                    f"but has no FILL entry in the ledger"
                )
            if trade.trade_id not in portfolio.processed_trade_ids:
                raise ReconciliationRecoveryError(
                    f"Trade {trade.trade_id} is marked {trade.state.value}, "
                    f"but is missing from portfolio.processed_trade_ids"
                )
        elif trade.state in (TradeState.REJECTED, TradeState.PROPOSED):
            if trade.trade_id in ledger_fills:
                raise ReconciliationRecoveryError(
                    f"Trade {trade.trade_id} is marked {trade.state.value}, but has a FILL entry in the ledger"
                )
