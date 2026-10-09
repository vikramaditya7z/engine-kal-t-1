"""Durable, timestamped evidence storage for market observations and evaluated opportunities.

V5 Evidence Storage persists observations and evaluated opportunities to disk
using atomic writes, explicit schema versioning ("1.0"), and crash-resilient recovery,
enabling offline replay and audits without live network access.
"""

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .arbitrage import (
    ArbitrageOpportunity,
    cents_to_dollars,
    dollars_to_cents,
)
from .contract import NO, YES
from .execution import (
    ConsumedLevel,
    DepthTraversalResult,
    ExecutionPricingResult,
)
from .market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PriceRange,
)
from .observer import (
    EvaluatedOpportunity,
    MarketObservation,
)

EVIDENCE_SCHEMA_VERSION = "1.0"


class EvidenceStorageError(Exception):
    """Base exception for evidence persistence and retrieval errors."""


class CorruptedEvidenceError(EvidenceStorageError):
    """Raised when persisted evidence is malformed, unreadable, or invalid."""


# ---------------------------------------------------------------------------
# Serialization Helpers
# ---------------------------------------------------------------------------

def _datetime_to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _iso_to_datetime(val: Optional[str]) -> Optional[datetime]:
    if val is None:
        return None
    return datetime.fromisoformat(val)


def _order_book_to_dict(book: Optional[NormalizedOrderBook]) -> Optional[Dict[str, Any]]:
    if book is None:
        return None
    return {
        "market_ticker": book.market_ticker,
        "yes_bids": [
            {"price_dollars": str(lvl.price_dollars), "quantity": str(lvl.quantity)}
            for lvl in book.yes_bids
        ],
        "no_bids": [
            {"price_dollars": str(lvl.price_dollars), "quantity": str(lvl.quantity)}
            for lvl in book.no_bids
        ],
    }


def _order_book_from_dict(d: Optional[Dict[str, Any]]) -> Optional[NormalizedOrderBook]:
    if d is None:
        return None
    return NormalizedOrderBook(
        market_ticker=d["market_ticker"],
        yes_bids=tuple(
            OrderBookLevel(
                price_dollars=Decimal(item["price_dollars"]),
                quantity=Decimal(item["quantity"]),
            )
            for item in d.get("yes_bids", [])
        ),
        no_bids=tuple(
            OrderBookLevel(
                price_dollars=Decimal(item["price_dollars"]),
                quantity=Decimal(item["quantity"]),
            )
            for item in d.get("no_bids", [])
        ),
    )


def _market_to_dict(m: Optional[NormalizedMarket]) -> Optional[Dict[str, Any]]:
    if m is None:
        return None
    return {
        "ticker": m.ticker,
        "event_ticker": m.event_ticker,
        "market_type": m.market_type,
        "status": m.status,
        "title": m.title,
        "subtitle": m.subtitle,
        "yes_subtitle": m.yes_subtitle,
        "no_subtitle": m.no_subtitle,
        "rules_primary": m.rules_primary,
        "rules_secondary": m.rules_secondary,
        "expiration_value": m.expiration_value,
        "result": m.result,
        "created_time": _datetime_to_iso(m.created_time),
        "updated_time": _datetime_to_iso(m.updated_time),
        "open_time": _datetime_to_iso(m.open_time),
        "close_time": _datetime_to_iso(m.close_time),
        "expiration_time": _datetime_to_iso(m.expiration_time),
        "settlement_ts": _datetime_to_iso(m.settlement_ts),
        "yes_bid_dollars": str(m.yes_bid_dollars) if m.yes_bid_dollars is not None else None,
        "yes_ask_dollars": str(m.yes_ask_dollars) if m.yes_ask_dollars is not None else None,
        "no_bid_dollars": str(m.no_bid_dollars) if m.no_bid_dollars is not None else None,
        "no_ask_dollars": str(m.no_ask_dollars) if m.no_ask_dollars is not None else None,
        "last_price_dollars": str(m.last_price_dollars) if m.last_price_dollars is not None else None,
        "settlement_value_dollars": (
            str(m.settlement_value_dollars) if m.settlement_value_dollars is not None else None
        ),
        "yes_bid_size": str(m.yes_bid_size) if m.yes_bid_size is not None else None,
        "yes_ask_size": str(m.yes_ask_size) if m.yes_ask_size is not None else None,
        "volume": str(m.volume) if m.volume is not None else None,
        "volume_24h": str(m.volume_24h) if m.volume_24h is not None else None,
        "open_interest": str(m.open_interest) if m.open_interest is not None else None,
        "price_ranges": [
            {"start": str(r.start), "end": str(r.end), "step": str(r.step)}
            for r in m.price_ranges
        ],
        "is_provisional": m.is_provisional,
    }


def _market_from_dict(d: Optional[Dict[str, Any]]) -> Optional[NormalizedMarket]:
    if d is None:
        return None
    return NormalizedMarket(
        ticker=d["ticker"],
        event_ticker=d["event_ticker"],
        market_type=d["market_type"],
        status=d["status"],
        title=d["title"],
        subtitle=d.get("subtitle"),
        yes_subtitle=d.get("yes_subtitle"),
        no_subtitle=d.get("no_subtitle"),
        rules_primary=d.get("rules_primary"),
        rules_secondary=d.get("rules_secondary"),
        expiration_value=d.get("expiration_value"),
        result=d.get("result"),
        created_time=_iso_to_datetime(d.get("created_time")),
        updated_time=_iso_to_datetime(d.get("updated_time")),
        open_time=_iso_to_datetime(d.get("open_time")),
        close_time=_iso_to_datetime(d.get("close_time")),
        expiration_time=_iso_to_datetime(d.get("expiration_time")),
        settlement_ts=_iso_to_datetime(d.get("settlement_ts")),
        yes_bid_dollars=Decimal(d["yes_bid_dollars"]) if d.get("yes_bid_dollars") else None,
        yes_ask_dollars=Decimal(d["yes_ask_dollars"]) if d.get("yes_ask_dollars") else None,
        no_bid_dollars=Decimal(d["no_bid_dollars"]) if d.get("no_bid_dollars") else None,
        no_ask_dollars=Decimal(d["no_ask_dollars"]) if d.get("no_ask_dollars") else None,
        last_price_dollars=Decimal(d["last_price_dollars"]) if d.get("last_price_dollars") else None,
        settlement_value_dollars=(
            Decimal(d["settlement_value_dollars"]) if d.get("settlement_value_dollars") else None
        ),
        yes_bid_size=Decimal(d["yes_bid_size"]) if d.get("yes_bid_size") else None,
        yes_ask_size=Decimal(d["yes_ask_size"]) if d.get("yes_ask_size") else None,
        volume=Decimal(d["volume"]) if d.get("volume") else None,
        volume_24h=Decimal(d["volume_24h"]) if d.get("volume_24h") else None,
        open_interest=Decimal(d["open_interest"]) if d.get("open_interest") else None,
        price_ranges=tuple(
            PriceRange(
                start=Decimal(r["start"]),
                end=Decimal(r["end"]),
                step=Decimal(r["step"]),
            )
            for r in d.get("price_ranges", [])
        ),
        is_provisional=d.get("is_provisional"),
    )


def observation_to_dict(obs: MarketObservation) -> Dict[str, Any]:
    """Serialize a MarketObservation into a JSON-compatible dictionary."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "observation_id": obs.observation_id,
        "ticker": obs.ticker,
        "observed_at": _datetime_to_iso(obs.observed_at),
        "source_timestamp": _datetime_to_iso(obs.source_timestamp),
        "market": _market_to_dict(obs.market),
        "order_book": _order_book_to_dict(obs.order_book),
        "is_success": obs.is_success,
        "is_stale": obs.is_stale,
        "staleness_reason": obs.staleness_reason,
        "error_message": obs.error_message,
    }


def observation_from_dict(d: Dict[str, Any]) -> MarketObservation:
    """Deserialize a dictionary into a validated MarketObservation."""
    if not isinstance(d, dict):
        raise CorruptedEvidenceError(f"Expected dict, got {type(d).__name__}")
    version = d.get("schema_version")
    if version != EVIDENCE_SCHEMA_VERSION:
        raise CorruptedEvidenceError(
            f"Unsupported schema version {version!r} (expected {EVIDENCE_SCHEMA_VERSION!r})"
        )

    for field in ("observation_id", "ticker", "observed_at", "is_success", "is_stale"):
        if field not in d:
            raise CorruptedEvidenceError(f"Missing required observation field: {field!r}")

    return MarketObservation(
        observation_id=str(d["observation_id"]),
        ticker=str(d["ticker"]),
        observed_at=_iso_to_datetime(d["observed_at"]),
        source_timestamp=_iso_to_datetime(d.get("source_timestamp")),
        market=_market_from_dict(d.get("market")),
        order_book=_order_book_from_dict(d.get("order_book")),
        is_success=bool(d["is_success"]),
        is_stale=bool(d["is_stale"]),
        staleness_reason=d.get("staleness_reason"),
        error_message=d.get("error_message"),
    )


def _arbitrage_opportunity_to_dict(opp: ArbitrageOpportunity) -> Dict[str, Any]:
    return {
        "opportunity_type": opp.opportunity_type,
        "is_arbitrage": opp.is_arbitrage,
        "guaranteed_payout_cents": opp.guaranteed_payout_cents,
        "total_cost_cents": opp.total_cost_cents,
        "gross_edge_cents": opp.gross_edge_cents,
        "payouts_by_outcome": opp.payouts_by_outcome,
        "profits_by_outcome": opp.profits_by_outcome,
        "guaranteed_payout_dollars": str(opp.guaranteed_payout_dollars),
        "total_cost_dollars": str(opp.total_cost_dollars),
        "gross_edge_dollars": str(opp.gross_edge_dollars),
        "is_executable": opp.is_executable,
        "is_net_profitable": opp.is_net_profitable,
        "net_edge_cents": opp.net_edge_cents,
        "net_edge_dollars": str(opp.net_edge_dollars) if opp.net_edge_dollars is not None else None,
        "estimated_fees_cents": opp.estimated_fees_cents,
        "rejection_reason": opp.rejection_reason,
        "qualification_notes": list(opp.qualification_notes),
        "contract_ids": list(opp.contract_ids),
    }


def _arbitrage_opportunity_from_dict(d: Dict[str, Any]) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        opportunity_type=d["opportunity_type"],
        is_arbitrage=d["is_arbitrage"],
        guaranteed_payout_cents=d["guaranteed_payout_cents"],
        total_cost_cents=d["total_cost_cents"],
        gross_edge_cents=d["gross_edge_cents"],
        payouts_by_outcome=d["payouts_by_outcome"],
        profits_by_outcome=d["profits_by_outcome"],
        guaranteed_payout_dollars=Decimal(d["guaranteed_payout_dollars"]),
        total_cost_dollars=Decimal(d["total_cost_dollars"]),
        gross_edge_dollars=Decimal(d["gross_edge_dollars"]),
        is_executable=d.get("is_executable", False),
        is_net_profitable=d.get("is_net_profitable"),
        net_edge_cents=d.get("net_edge_cents"),
        net_edge_dollars=Decimal(d["net_edge_dollars"]) if d.get("net_edge_dollars") else None,
        estimated_fees_cents=d.get("estimated_fees_cents"),
        rejection_reason=d.get("rejection_reason"),
        qualification_notes=tuple(d.get("qualification_notes", ())),
        contract_ids=tuple(d.get("contract_ids", ())),
    )


def _consumed_level_to_dict(lvl: ConsumedLevel) -> Dict[str, Any]:
    return {
        "price_cents": lvl.price_cents,
        "price_dollars": str(lvl.price_dollars),
        "quantity": lvl.quantity,
        "cost_cents": lvl.cost_cents,
        "cost_dollars": str(lvl.cost_dollars),
    }


def _consumed_level_from_dict(d: Dict[str, Any]) -> ConsumedLevel:
    return ConsumedLevel(
        price_cents=d["price_cents"],
        price_dollars=Decimal(d["price_dollars"]),
        quantity=d["quantity"],
        cost_cents=d["cost_cents"],
        cost_dollars=Decimal(d["cost_dollars"]),
    )


def _depth_traversal_to_dict(trav: DepthTraversalResult) -> Dict[str, Any]:
    return {
        "market_ticker": trav.market_ticker,
        "side": trav.side,
        "requested_quantity": trav.requested_quantity,
        "supported_quantity": trav.supported_quantity,
        "unfilled_quantity": trav.unfilled_quantity,
        "is_full_fill": trav.is_full_fill,
        "is_partial_fill": trav.is_partial_fill,
        "is_empty_fill": trav.is_empty_fill,
        "total_cost_cents": trav.total_cost_cents,
        "total_cost_dollars": str(trav.total_cost_dollars),
        "average_price_cents": str(trav.average_price_cents) if trav.average_price_cents is not None else None,
        "average_price_dollars": (
            str(trav.average_price_dollars) if trav.average_price_dollars is not None else None
        ),
        "consumed_levels": [_consumed_level_to_dict(l) for l in trav.consumed_levels],
        "status": trav.status,
        "rejection_reason": trav.rejection_reason,
    }


def _depth_traversal_from_dict(d: Dict[str, Any]) -> DepthTraversalResult:
    return DepthTraversalResult(
        market_ticker=d["market_ticker"],
        side=d["side"],
        requested_quantity=d["requested_quantity"],
        supported_quantity=d["supported_quantity"],
        unfilled_quantity=d["unfilled_quantity"],
        is_full_fill=d["is_full_fill"],
        is_partial_fill=d["is_partial_fill"],
        is_empty_fill=d["is_empty_fill"],
        total_cost_cents=d["total_cost_cents"],
        total_cost_dollars=Decimal(d["total_cost_dollars"]),
        average_price_cents=Decimal(d["average_price_cents"]) if d.get("average_price_cents") else None,
        average_price_dollars=Decimal(d["average_price_dollars"]) if d.get("average_price_dollars") else None,
        consumed_levels=tuple(_consumed_level_from_dict(l) for l in d.get("consumed_levels", [])),
        status=d["status"],
        rejection_reason=d.get("rejection_reason"),
    )


def _pricing_result_to_dict(res: Optional[ExecutionPricingResult]) -> Optional[Dict[str, Any]]:
    if res is None:
        return None
    return {
        "strategy_type": res.strategy_type,
        "status": res.status,
        "is_depth_supported": res.is_depth_supported,
        "is_gross_profitable": res.is_gross_profitable,
        "is_net_profitable": res.is_net_profitable,
        "requested_quantity": res.requested_quantity,
        "supported_quantity": res.supported_quantity,
        "unfilled_quantity": res.unfilled_quantity,
        "total_cost_cents": res.total_cost_cents,
        "total_cost_dollars": str(res.total_cost_dollars),
        "guaranteed_payout_cents": res.guaranteed_payout_cents,
        "guaranteed_payout_dollars": str(res.guaranteed_payout_dollars),
        "gross_profit_cents": res.gross_profit_cents,
        "gross_profit_dollars": str(res.gross_profit_dollars),
        "estimated_fees_cents": res.estimated_fees_cents,
        "estimated_fees_dollars": (
            str(res.estimated_fees_dollars) if res.estimated_fees_dollars is not None else None
        ),
        "net_profit_cents": res.net_profit_cents,
        "net_profit_dollars": (
            str(res.net_profit_dollars) if res.net_profit_dollars is not None else None
        ),
        "payouts_by_outcome": res.payouts_by_outcome,
        "profits_by_outcome": res.profits_by_outcome,
        "leg_traversals": [_depth_traversal_to_dict(t) for t in res.leg_traversals],
        "rejection_reason": res.rejection_reason,
        "qualification_notes": list(res.qualification_notes),
    }


def _pricing_result_from_dict(d: Optional[Dict[str, Any]]) -> Optional[ExecutionPricingResult]:
    if d is None:
        return None
    return ExecutionPricingResult(
        strategy_type=d["strategy_type"],
        status=d["status"],
        is_depth_supported=d["is_depth_supported"],
        is_gross_profitable=d["is_gross_profitable"],
        is_net_profitable=d.get("is_net_profitable"),
        requested_quantity=d["requested_quantity"],
        supported_quantity=d["supported_quantity"],
        unfilled_quantity=d["unfilled_quantity"],
        total_cost_cents=d["total_cost_cents"],
        total_cost_dollars=Decimal(d["total_cost_dollars"]),
        guaranteed_payout_cents=d["guaranteed_payout_cents"],
        guaranteed_payout_dollars=Decimal(d["guaranteed_payout_dollars"]),
        gross_profit_cents=d["gross_profit_cents"],
        gross_profit_dollars=Decimal(d["gross_profit_dollars"]),
        estimated_fees_cents=d.get("estimated_fees_cents"),
        estimated_fees_dollars=Decimal(d["estimated_fees_dollars"]) if d.get("estimated_fees_dollars") else None,
        net_profit_cents=d.get("net_profit_cents"),
        net_profit_dollars=Decimal(d["net_profit_dollars"]) if d.get("net_profit_dollars") else None,
        payouts_by_outcome=d.get("payouts_by_outcome", {}),
        profits_by_outcome=d.get("profits_by_outcome", {}),
        leg_traversals=tuple(_depth_traversal_from_dict(t) for t in d.get("leg_traversals", [])),
        rejection_reason=d.get("rejection_reason"),
        qualification_notes=tuple(d.get("qualification_notes", ())),
    )


def opportunity_to_dict(opp: EvaluatedOpportunity) -> Dict[str, Any]:
    """Serialize an EvaluatedOpportunity into a JSON-compatible dictionary."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "observation_id": opp.observation_id,
        "opportunity_id": opp.opportunity_id,
        "strategy_type": opp.strategy_type,
        "observed_at": _datetime_to_iso(opp.observed_at),
        "source_timestamps": {
            k: _datetime_to_iso(v) for k, v in opp.source_timestamps.items()
        },
        "leg_timestamp_skew_seconds": opp.leg_timestamp_skew_seconds,
        "market_tickers": list(opp.market_tickers),
        "candidate_opportunity": _arbitrage_opportunity_to_dict(opp.candidate_opportunity),
        "pricing_result": _pricing_result_to_dict(opp.pricing_result),
        "status": opp.status,
        "rejection_reason": opp.rejection_reason,
        "fee_per_contract_cents": opp.fee_per_contract_cents,
        "is_qualified": opp.is_qualified,
        "is_recurrent": opp.is_recurrent,
        "paper_trade_id": opp.paper_trade_id,
    }


def opportunity_from_dict(d: Dict[str, Any]) -> EvaluatedOpportunity:
    """Deserialize a dictionary into a validated EvaluatedOpportunity."""
    if not isinstance(d, dict):
        raise CorruptedEvidenceError(f"Expected dict, got {type(d).__name__}")
    version = d.get("schema_version")
    if version != EVIDENCE_SCHEMA_VERSION:
        raise CorruptedEvidenceError(
            f"Unsupported schema version {version!r} (expected {EVIDENCE_SCHEMA_VERSION!r})"
        )

    for field in (
        "observation_id",
        "opportunity_id",
        "strategy_type",
        "observed_at",
        "candidate_opportunity",
        "status",
        "is_qualified",
    ):
        if field not in d:
            raise CorruptedEvidenceError(f"Missing required opportunity field: {field!r}")

    source_ts = {
        k: _iso_to_datetime(v) for k, v in d.get("source_timestamps", {}).items()
    }

    return EvaluatedOpportunity(
        observation_id=str(d["observation_id"]),
        opportunity_id=str(d["opportunity_id"]),
        strategy_type=str(d["strategy_type"]),
        observed_at=_iso_to_datetime(d["observed_at"]),
        source_timestamps=source_ts,
        leg_timestamp_skew_seconds=float(d.get("leg_timestamp_skew_seconds", 0.0)),
        market_tickers=tuple(d.get("market_tickers", ())),
        candidate_opportunity=_arbitrage_opportunity_from_dict(d["candidate_opportunity"]),
        pricing_result=_pricing_result_from_dict(d.get("pricing_result")),
        status=str(d["status"]),
        rejection_reason=d.get("rejection_reason"),
        fee_per_contract_cents=d.get("fee_per_contract_cents"),
        is_qualified=bool(d["is_qualified"]),
        is_recurrent=bool(d.get("is_recurrent", False)),
        paper_trade_id=d.get("paper_trade_id"),
    )


# ---------------------------------------------------------------------------
# Atomic File Evidence Store
# ---------------------------------------------------------------------------

class EvidenceStore:
    """Crash-resilient, schema-versioned file store for observations and opportunities."""

    def __init__(
        self,
        storage_dir: Union[str, Path],
        *,
        retention_max_records: Optional[int] = None,
    ) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.retention_max_records = retention_max_records

        self.observations_path = self.storage_dir / "observations.jsonl"
        self.opportunities_path = self.storage_dir / "opportunities.jsonl"

    def _append_atomic(self, path: Path, payload_dict: Dict[str, Any]) -> None:
        """Append one record to a JSONL file with crash resilience."""
        line = json.dumps(payload_dict) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _prune_if_needed(self, path: Path) -> None:
        """Prune older lines if retention_max_records limit is configured."""
        if not self.retention_max_records or not path.exists():
            return

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if len(lines) > self.retention_max_records:
            trimmed = lines[-self.retention_max_records :]
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                f.writelines(trimmed)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)

    def record_observation(self, obs: MarketObservation) -> None:
        """Durably record a market observation."""
        payload = observation_to_dict(obs)
        self._append_atomic(self.observations_path, payload)
        self._prune_if_needed(self.observations_path)

    def record_opportunity(self, opp: EvaluatedOpportunity) -> None:
        """Durably record an evaluated opportunity."""
        payload = opportunity_to_dict(opp)
        self._append_atomic(self.opportunities_path, payload)
        self._prune_if_needed(self.opportunities_path)

    def load_observations(self) -> Tuple[MarketObservation, ...]:
        """Load all recorded observations, validating schema and handling corruption."""
        if not self.observations_path.exists():
            return ()

        observations = []
        with open(self.observations_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean = line.strip()
                if not clean:
                    continue
                try:
                    data = json.loads(clean)
                    observations.append(observation_from_dict(data))
                except Exception as exc:
                    raise CorruptedEvidenceError(
                        f"Corrupted observation at {self.observations_path}:{line_no}: {exc}"
                    ) from exc

        return tuple(observations)

    def load_opportunities(self) -> Tuple[EvaluatedOpportunity, ...]:
        """Load all recorded opportunities, validating schema and handling corruption."""
        if not self.opportunities_path.exists():
            return ()

        opportunities = []
        with open(self.opportunities_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean = line.strip()
                if not clean:
                    continue
                try:
                    data = json.loads(clean)
                    opportunities.append(opportunity_from_dict(data))
                except Exception as exc:
                    raise CorruptedEvidenceError(
                        f"Corrupted opportunity at {self.opportunities_path}:{line_no}: {exc}"
                    ) from exc

        return tuple(opportunities)


def replay_recorded_evidence(
    store: EvidenceStore,
    *,
    initial_cash_cents: int = 100_000,
    risk_config: Optional[Any] = None,
    default_fee_per_contract_cents: Optional[int] = None,
    target_quantity: int = 1,
) -> Any:
    """Replay recorded historical observations through the V4 ReplayEngine without network access."""
    from .arbitrage import OPPORTUNITY_BINARY_PARITY
    from .replay import EVENT_TYPE_OPPORTUNITY, ReplayEngine, ReplayEvent

    observations = store.load_observations()
    events = []

    for obs in observations:
        if obs.is_success and obs.order_book is not None and not obs.is_stale:
            events.append(
                ReplayEvent(
                    timestamp=obs.observed_at.isoformat(),
                    event_type=EVENT_TYPE_OPPORTUNITY,
                    strategy_type=OPPORTUNITY_BINARY_PARITY,
                    book=obs.order_book,
                    requested_quantity=target_quantity,
                    fee_per_contract_cents=default_fee_per_contract_cents,
                )
            )

    engine = ReplayEngine(
        risk_config=risk_config,
        default_fee_per_contract_cents=default_fee_per_contract_cents,
    )
    return engine.run(events, initial_cash_cents=initial_cash_cents)

