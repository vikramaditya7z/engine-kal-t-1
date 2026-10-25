"""Kalshi Arbitrage Engine V13 — Historical MECE Basket Replay & Research.

Provides deterministic historical replay of declared MECE event baskets across
reconstructed multi-market snapshots. Reuses V2 theoretical candidate detection,
V3 execution depth traversal, and CFTC fee modeling.

Purely analytical: zero live order submission and zero paper portfolio mutation.
Produces structured, auditable outputs with cryptographic byte manifests.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
from pathlib import Path
import statistics
import time
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
import uuid

from .contract import NO, YES
from .execution import (
    ExecutionPricingResult,
    derive_ask_levels,
    evaluate_mece_basket_execution,
)
from .fee_model import FeeModelConfig, FeeScheduleType
from .observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_INSUFFICIENT_QUANTITY,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    evaluate_mece_markets,
)
from .portfolio import Event
from .snapshot_reconstruction import (
    EventSnapshotReconstructor,
    ReconstructedBasketSnapshot,
    STATUS_VALID_SNAPSHOT,
    SnapshotReconstructionConfig,
)

logger = logging.getLogger("v13_basket_replay")

REPLAY_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class HistoricalBasketReplayConfig:
    """Configuration for historical MECE basket replay."""

    target_quantity: int = 1
    max_leg_timestamp_skew_seconds: float = 5.0
    cycle_window_seconds: float = 15.0
    max_stale_seconds: float = 60.0
    require_source_timestamp: bool = True
    fee_config: FeeModelConfig = field(
        default_factory=lambda: FeeModelConfig(
            schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER
        )
    )

    def __post_init__(self) -> None:
        if self.target_quantity <= 0:
            raise ValueError("target_quantity must be positive")
        if self.max_leg_timestamp_skew_seconds <= 0:
            raise ValueError("max_leg_timestamp_skew_seconds must be positive")
        if self.cycle_window_seconds <= 0:
            raise ValueError("cycle_window_seconds must be positive")


@dataclass(frozen=True)
class HistoricalBasketReplayRecord:
    """Audit record for a single historical basket evaluation."""

    replay_id: str
    snapshot_id: str
    basket_id: Optional[str]
    event_ticker: str
    basket_side: str
    market_tickers: Tuple[str, ...]
    market_outcome_map: Mapping[str, str]
    source_timestamps: Mapping[str, Optional[str]]
    source_observation_ids: Tuple[str, ...]
    snapshot_min_timestamp: Optional[str]
    snapshot_max_timestamp: Optional[str]
    leg_timestamp_skew_seconds: float
    max_allowed_skew_seconds: float
    per_leg_prices_cents: Mapping[str, Optional[int]]
    per_leg_depth: Mapping[str, int]
    common_executable_quantity: int
    gross_acquisition_cost_cents: Optional[int]
    guaranteed_payout_cents: Optional[int]
    gross_edge_cents: Optional[int]
    estimated_fees_cents: Optional[int]
    net_edge_cents: Optional[int]
    is_gross_profitable: bool
    is_net_profitable: bool
    is_depth_supported: bool
    is_qualified: bool
    status: str
    rejection_reason: Optional[str]
    source_evidence_digest: Optional[str]
    market_state_fingerprint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert record to JSON-serializable dictionary."""
        return {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "replay_id": self.replay_id,
            "snapshot_id": self.snapshot_id,
            "basket_id": self.basket_id,
            "event_ticker": self.event_ticker,
            "basket_side": self.basket_side,
            "market_tickers": list(self.market_tickers),
            "market_outcome_map": dict(self.market_outcome_map),
            "source_timestamps": dict(self.source_timestamps),
            "source_observation_ids": list(self.source_observation_ids),
            "snapshot_min_timestamp": self.snapshot_min_timestamp,
            "snapshot_max_timestamp": self.snapshot_max_timestamp,
            "leg_timestamp_skew_seconds": self.leg_timestamp_skew_seconds,
            "max_allowed_skew_seconds": self.max_allowed_skew_seconds,
            "per_leg_prices_cents": dict(self.per_leg_prices_cents),
            "per_leg_depth": dict(self.per_leg_depth),
            "common_executable_quantity": self.common_executable_quantity,
            "gross_acquisition_cost_cents": self.gross_acquisition_cost_cents,
            "guaranteed_payout_cents": self.guaranteed_payout_cents,
            "gross_edge_cents": self.gross_edge_cents,
            "estimated_fees_cents": self.estimated_fees_cents,
            "net_edge_cents": self.net_edge_cents,
            "is_gross_profitable": self.is_gross_profitable,
            "is_net_profitable": self.is_net_profitable,
            "is_depth_supported": self.is_depth_supported,
            "is_qualified": self.is_qualified,
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "source_evidence_digest": self.source_evidence_digest,
            "market_state_fingerprint": self.market_state_fingerprint,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HistoricalBasketReplayRecord":
        """Deserialize from dictionary."""
        return cls(
            replay_id=str(d["replay_id"]),
            snapshot_id=str(d["snapshot_id"]),
            basket_id=d.get("basket_id"),
            event_ticker=str(d["event_ticker"]),
            basket_side=str(d["basket_side"]),
            market_tickers=tuple(d.get("market_tickers", ())),
            market_outcome_map=dict(d.get("market_outcome_map", {})),
            source_timestamps=dict(d.get("source_timestamps", {})),
            source_observation_ids=tuple(d.get("source_observation_ids", ())),
            snapshot_min_timestamp=d.get("snapshot_min_timestamp"),
            snapshot_max_timestamp=d.get("snapshot_max_timestamp"),
            leg_timestamp_skew_seconds=float(d.get("leg_timestamp_skew_seconds", 0.0)),
            max_allowed_skew_seconds=float(d.get("max_allowed_skew_seconds", 0.0)),
            per_leg_prices_cents=dict(d.get("per_leg_prices_cents", {})),
            per_leg_depth=dict(d.get("per_leg_depth", {})),
            common_executable_quantity=int(d.get("common_executable_quantity", 0)),
            gross_acquisition_cost_cents=d.get("gross_acquisition_cost_cents"),
            guaranteed_payout_cents=d.get("guaranteed_payout_cents"),
            gross_edge_cents=d.get("gross_edge_cents"),
            estimated_fees_cents=d.get("estimated_fees_cents"),
            net_edge_cents=d.get("net_edge_cents"),
            is_gross_profitable=bool(d.get("is_gross_profitable", False)),
            is_net_profitable=bool(d.get("is_net_profitable", False)),
            is_depth_supported=bool(d.get("is_depth_supported", False)),
            is_qualified=bool(d.get("is_qualified", False)),
            status=str(d["status"]),
            rejection_reason=d.get("rejection_reason"),
            source_evidence_digest=d.get("source_evidence_digest"),
            market_state_fingerprint=d.get("market_state_fingerprint"),
        )


@dataclass(frozen=True)
class HistoricalBasketReplaySummary:
    """Comprehensive summary of historical MECE basket replay."""

    replay_id: str
    source_path: str
    dataset_sha256: Optional[str]
    evaluated_at: str
    baskets_configured: Tuple[str, ...]
    total_input_observations: int
    duplicate_observations_count: int
    total_snapshots_reconstructed: int
    complete_snapshots_count: int
    rejected_snapshots_count: int
    rejected_by_reason: Mapping[str, int]
    total_basket_evaluations: int
    gross_positive_count: int
    fee_adjusted_positive_count: int
    depth_supported_count: int
    qualified_count: int
    gross_edge_distribution: Mapping[str, Any]
    net_edge_distribution: Mapping[str, Any]
    skew_distribution: Mapping[str, Any]
    data_quality_limitations: Tuple[str, ...]
    records: Tuple[HistoricalBasketReplayRecord, ...]
    unique_market_states_count: int = 0
    repeated_market_states_count: int = 0
    unmatched_baskets: Tuple[str, ...] = ()
    opportunity_classifications: Mapping[str, int] = field(default_factory=dict)
    sensitivity_analysis: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert summary to JSON dictionary."""
        return {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "replay_id": self.replay_id,
            "source_path": self.source_path,
            "dataset_sha256": self.dataset_sha256,
            "evaluated_at": self.evaluated_at,
            "baskets_configured": list(self.baskets_configured),
            "total_input_observations": self.total_input_observations,
            "duplicate_observations_count": self.duplicate_observations_count,
            "total_snapshots_reconstructed": self.total_snapshots_reconstructed,
            "complete_snapshots_count": self.complete_snapshots_count,
            "rejected_snapshots_count": self.rejected_snapshots_count,
            "rejected_by_reason": dict(self.rejected_by_reason),
            "total_basket_evaluations": self.total_basket_evaluations,
            "gross_positive_count": self.gross_positive_count,
            "fee_adjusted_positive_count": self.fee_adjusted_positive_count,
            "depth_supported_count": self.depth_supported_count,
            "qualified_count": self.qualified_count,
            "gross_edge_distribution": dict(self.gross_edge_distribution),
            "net_edge_distribution": dict(self.net_edge_distribution),
            "skew_distribution": dict(self.skew_distribution),
            "data_quality_limitations": list(self.data_quality_limitations),
            "records_count": len(self.records),
            "unique_market_states_count": self.unique_market_states_count,
            "repeated_market_states_count": self.repeated_market_states_count,
            "unmatched_baskets": list(self.unmatched_baskets),
            "opportunity_classifications": dict(self.opportunity_classifications),
            "sensitivity_analysis": dict(self.sensitivity_analysis),
        }

    def summary_markdown(self) -> str:
        """Render publication-grade markdown summary report."""
        lines = [
            "# Kalshi V13 Historical MECE Basket Replay Report",
            "",
            f"**Replay ID:** `{self.replay_id}`  ",
            f"**Evaluated At:** `{self.evaluated_at}`  ",
            f"**Source Dataset:** `{self.source_path}`  ",
            f"**Dataset SHA-256:** `{self.dataset_sha256 or 'N/A'}`  ",
            "",
            "---",
            "",
            "## 1. Executive Summary & Verdict",
            "",
        ]

        if self.qualified_count > 0:
            lines.append(f"**Verdict: QUALIFIED OPPORTUNITIES DETECTED ({self.qualified_count} qualified)**")
        elif self.total_basket_evaluations > 0:
            lines.append("**Verdict: NO QUALIFIED BASKETS (ZERO PROFITABLE ARBITRAGE)**")
        else:
            lines.append("**Verdict: INCONCLUSIVE (NO EVALUABLE BASKETS RECONSTRUCTED)**")

        lines.extend([
            "",
            "| Metric | Count / Value |",
            "| :--- | :--- |",
            f"| Total Input Observations | {self.total_input_observations} |",
            f"| Duplicate Records Pruned | {self.duplicate_observations_count} |",
            f"| Reconstructed Snapshots | {self.total_snapshots_reconstructed} |",
            f"| Complete Snapshots | {self.complete_snapshots_count} |",
            f"| Unique Market States | {self.unique_market_states_count} |",
            f"| Repeated Market States | {self.repeated_market_states_count} |",
            f"| Rejected Snapshots | {self.rejected_snapshots_count} |",
            f"| Total Basket Evaluations | {self.total_basket_evaluations} |",
            f"| Gross-Positive Candidates | {self.gross_positive_count} |",
            f"| Depth-Supported Candidates | {self.depth_supported_count} |",
            f"| Net-Profitable (After Fees) | {self.fee_adjusted_positive_count} |",
            f"| **Qualified Arbitrage Trades** | **{self.qualified_count}** |",
            "",
            "---",
            "",
            "## 2. Canonical Opportunity Classifications",
            "",
            "| Classification Category | Count | Status / Definition |",
            "| :--- | :--- | :--- |",
            f"| 1. Structurally Invalid / Unverified Basket | {self.opportunity_classifications.get('STRUCTURALLY_INVALID_BASKET', 0)} | Failed MECE structure or universe check |",
            f"| 2. Incomplete / Insufficient Synchronization | {self.opportunity_classifications.get('INSUFFICIENT_SYNCHRONIZATION', 0)} | Missing leg, error, or timestamp skew |",
            f"| 3. No Executable Depth | {self.opportunity_classifications.get('NO_EXECUTABLE_DEPTH', 0)} | Zero fillable contracts across legs |",
            f"| 4. Negative or Zero Gross Edge | {self.opportunity_classifications.get('NO_GROSS_EDGE', 0)} | Gross acquisition cost >= payout |",
            f"| 5. Positive Gross Edge, Unprofitable After Fees | {self.opportunity_classifications.get('UNPROFITABLE_AFTER_FEES', 0)} | Gross edge eroded by CFTC taker fees |",
            f"| 6. Positive Net Edge, Insufficient Common Qty | {self.opportunity_classifications.get('INSUFFICIENT_COMMON_QUANTITY', 0)} | Fillable quantity < target quantity |",
            f"| **7. Qualified Research Candidates** | **{self.opportunity_classifications.get('QUALIFIED_RESEARCH_CANDIDATE', 0)}** | **Fully executable, synchronized, fee-adjusted edge** |",
            "",
            "---",
            "",
            "## 3. Rejection Breakdown",
            "",
        ])

        if self.rejected_by_reason:
            lines.append("| Rejection Reason / Code | Occurrences |")
            lines.append("| :--- | :--- |")
            for reason, cnt in sorted(self.rejected_by_reason.items(), key=lambda x: -x[1]):
                lines.append(f"| `{reason}` | {cnt} |")
        else:
            lines.append("No snapshots or candidates were rejected.")

        lines.extend([
            "",
            "---",
            "",
            "## 4. Quantitative Distributions & Sensitivity",
            "",
            "### Gross Edge Distribution (¢)",
            f"- Min: {self.gross_edge_distribution.get('min', 'N/A')}¢",
            f"- Max: {self.gross_edge_distribution.get('max', 'N/A')}¢",
            f"- Mean: {self.gross_edge_distribution.get('mean', 'N/A')}¢",
            f"- Sample Count: {self.gross_edge_distribution.get('count', 0)}",
            "",
            "### Net Edge Distribution (¢, After CFTC Fees)",
            f"- Min: {self.net_edge_distribution.get('min', 'N/A')}¢",
            f"- Max: {self.net_edge_distribution.get('max', 'N/A')}¢",
            f"- Mean: {self.net_edge_distribution.get('mean', 'N/A')}¢",
            f"- Sample Count: {self.net_edge_distribution.get('count', 0)}",
            "",
            "### Timestamp Skew Distribution (Seconds)",
            f"- Min Skew: {self.skew_distribution.get('min', 'N/A')}s",
            f"- Max Skew: {self.skew_distribution.get('max', 'N/A')}s",
            f"- Mean Skew: {self.skew_distribution.get('mean', 'N/A')}s",
            f"- Median (p50): {self.skew_distribution.get('p50', 'N/A')}s",
            f"- 95th Percentile (p95): {self.skew_distribution.get('p95', 'N/A')}s",
            "",
            "### Skew Threshold Sensitivity",
        ])

        skew_sens = self.sensitivity_analysis.get("skew_threshold_sensitivity", {})
        if skew_sens:
            for thresh_key, count in sorted(skew_sens.items()):
                lines.append(f"- Snapshots with {thresh_key}: {count}")
        else:
            lines.append("- N/A")

        lines.extend([
            "",
            "---",
            "",
            "## 5. Methodological & Data-Quality Limitations",
            "",
        ])

        for limitation in self.data_quality_limitations:
            lines.append(f"- {limitation}")

        lines.append("")
        return "\n".join(lines)

    def write_artifacts(self, output_dir: Path) -> Dict[str, Path]:
        """Persist replay records, summary JSON, report Markdown, and manifest."""
        output_dir.mkdir(parents=True, exist_ok=True)
        records_path = output_dir / "replay_records.jsonl"
        summary_path = output_dir / "replay_summary.json"
        report_path = output_dir / "replay_report.md"
        manifest_path = output_dir / "manifest.json"

        # 1. Write records JSONL
        with open(records_path, "w", encoding="utf-8") as f:
            for rec in self.records:
                f.write(json.dumps(rec.to_dict()) + "\n")

        # 2. Write summary JSON
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.to_dict(), indent=2) + "\n")

        # 3. Write report Markdown
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(self.summary_markdown() + "\n")

        # 4. Compute byte-level SHA-256 digests
        def _sha(p: Path) -> str:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                while chunk := fh.read(65536):
                    h.update(chunk)
            return h.hexdigest()

        records_sha = _sha(records_path)
        summary_sha = _sha(summary_path)
        report_sha = _sha(report_path)

        manifest_data = {
            "schema_version": REPLAY_SCHEMA_VERSION,
            "replay_id": self.replay_id,
            "evaluated_at": self.evaluated_at,
            "source_path": self.source_path,
            "dataset_sha256": self.dataset_sha256,
            "records_count": len(self.records),
            "files": {
                "replay_records.jsonl": records_sha,
                "replay_summary.json": summary_sha,
                "replay_report.md": report_sha,
            },
        }

        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

        return {
            "records": records_path,
            "summary": summary_path,
            "report": report_path,
            "manifest": manifest_path,
        }


def _compute_stats(values: List[float]) -> Dict[str, Any]:
    """Compute min, max, mean, and count for a list of floats."""
    if not values:
        return {"min": None, "max": None, "mean": None, "count": 0}
    return {
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "mean": round(statistics.mean(values), 2),
        "count": len(values),
    }


def _compute_percentiles(values: List[float]) -> Dict[str, Any]:
    """Compute distribution metrics including p50 and p95."""
    if not values:
        return {"min": None, "max": None, "mean": None, "p50": None, "p95": None, "count": 0}
    sorted_v = sorted(values)
    n = len(sorted_v)
    p50_idx = int(n * 0.50)
    p95_idx = min(int(n * 0.95), n - 1)
    return {
        "min": round(sorted_v[0], 2),
        "max": round(sorted_v[-1], 2),
        "mean": round(statistics.mean(values), 2),
        "p50": round(sorted_v[p50_idx], 2),
        "p95": round(sorted_v[p95_idx], 2),
        "count": n,
    }


class HistoricalBasketReplayEngine:
    """Historical replay engine for explicitly declared MECE event baskets."""

    def __init__(self, config: Optional[HistoricalBasketReplayConfig] = None) -> None:
        self.config = config or HistoricalBasketReplayConfig()
        self.reconstructor = EventSnapshotReconstructor(
            config=SnapshotReconstructionConfig(
                max_leg_timestamp_skew_seconds=self.config.max_leg_timestamp_skew_seconds,
                cycle_window_seconds=self.config.cycle_window_seconds,
                max_stale_seconds=self.config.max_stale_seconds,
                require_source_timestamp=self.config.require_source_timestamp,
            )
        )

    def replay(
        self,
        observations: Sequence[MarketObservation],
        baskets: Sequence[DeclaredMeceBasket],
        source_path: str = "",
        dataset_sha256: Optional[str] = None,
    ) -> HistoricalBasketReplaySummary:
        """Replay historical observations across declared MECE baskets.

        Args:
            observations: Chronological or un-ordered market observations.
            baskets: Declared MECE basket configurations.
            source_path: Path or description of original evidence file.
            dataset_sha256: SHA-256 hash of source observations file.

        Returns:
            Comprehensive HistoricalBasketReplaySummary with full audit trail.
        """
        now = datetime.now(timezone.utc)
        replay_id = f"v13-replay-{int(time.time())}-{uuid.uuid4().hex[:6]}"

        if not baskets:
            return HistoricalBasketReplaySummary(
                replay_id=replay_id,
                source_path=source_path,
                dataset_sha256=dataset_sha256,
                evaluated_at=now.isoformat(),
                baskets_configured=(),
                total_input_observations=len(observations),
                duplicate_observations_count=0,
                total_snapshots_reconstructed=0,
                complete_snapshots_count=0,
                rejected_snapshots_count=0,
                rejected_by_reason={},
                total_basket_evaluations=0,
                gross_positive_count=0,
                fee_adjusted_positive_count=0,
                depth_supported_count=0,
                qualified_count=0,
                gross_edge_distribution={"count": 0},
                net_edge_distribution={"count": 0},
                skew_distribution={"count": 0},
                data_quality_limitations=("No declared baskets configured for replay.",),
                records=(),
            )

        # Enforce that declared baskets have matching observations in dataset
        if observations:
            obs_tickers = {o.ticker for o in observations if o and o.ticker}
            all_basket_tickers = {t for b in baskets for t in b.ordered_tickers}
            matched_tickers = obs_tickers.intersection(all_basket_tickers)
            if not matched_tickers:
                b_ids = [b.basket_id or b.event_ticker for b in baskets]
                raise ValueError(
                    f"Dataset contains zero matching observations for declared basket(s): {b_ids}. "
                    f"Declared tickers: {sorted(list(all_basket_tickers))[:8]}, "
                    f"Observed tickers in dataset: {sorted(list(obs_tickers))[:8]}."
                )

        # 1. Reconstruct snapshots
        snapshots, dups = self.reconstructor.reconstruct(observations, baskets)

        records: List[HistoricalBasketReplayRecord] = []
        rej_counts: Dict[str, int] = {}
        gross_edges: List[float] = []
        net_edges: List[float] = []
        skews: List[float] = []

        gross_pos = 0
        fee_adj_pos = 0
        depth_supp = 0
        qualified = 0
        complete_snaps = 0
        rejected_snaps = 0

        for snap in snapshots:
            skews.append(snap.leg_timestamp_skew_seconds)
            basket = snap.basket

            if not snap.is_valid:
                rejected_snaps += 1
                rej_counts[snap.status] = rej_counts.get(snap.status, 0) + 1
                rec = HistoricalBasketReplayRecord(
                    replay_id=replay_id,
                    snapshot_id=snap.snapshot_id,
                    basket_id=basket.basket_id,
                    event_ticker=basket.event_ticker,
                    basket_side=basket.basket_side,
                    market_tickers=basket.ordered_tickers,
                    market_outcome_map=basket.market_outcome_map,
                    source_timestamps=snap.source_timestamps,
                    source_observation_ids=snap.observation_ids,
                    snapshot_min_timestamp=snap.min_timestamp.isoformat() if snap.min_timestamp else None,
                    snapshot_max_timestamp=snap.max_timestamp.isoformat() if snap.max_timestamp else None,
                    leg_timestamp_skew_seconds=snap.leg_timestamp_skew_seconds,
                    max_allowed_skew_seconds=self.config.max_leg_timestamp_skew_seconds,
                    per_leg_prices_cents={t: None for t in basket.ordered_tickers},
                    per_leg_depth={t: 0 for t in basket.ordered_tickers},
                    common_executable_quantity=0,
                    gross_acquisition_cost_cents=None,
                    guaranteed_payout_cents=None,
                    gross_edge_cents=None,
                    estimated_fees_cents=None,
                    net_edge_cents=None,
                    is_gross_profitable=False,
                    is_net_profitable=False,
                    is_depth_supported=False,
                    is_qualified=False,
                    status=snap.status,
                    rejection_reason=snap.rejection_reason,
                    source_evidence_digest=dataset_sha256,
                )
                records.append(rec)
                continue

            complete_snaps += 1

            # 2. Evaluate candidate and execution pricing
            event = Event(
                identifier=basket.event_ticker,
                outcomes=basket.outcomes,
                relationship_established=True,
            )
            basket_obs = [snap.observations[t] for t in basket.ordered_tickers]
            markets = [o.market for o in basket_obs]
            books_by_outcome = {
                basket.market_outcome_map[o.ticker]: o.order_book for o in basket_obs
            }

            try:
                candidate = evaluate_mece_markets(
                    event=event,
                    markets=markets,
                    market_outcome_map=basket.market_outcome_map,
                    basket_side=basket.basket_side,
                    quantity=self.config.target_quantity,
                )
            except Exception as exc:
                logger.debug(f"Candidate check failed for {basket.event_ticker}: {exc}")
                candidate = None

            pricing = evaluate_mece_basket_execution(
                event=event,
                books_by_outcome=books_by_outcome,
                basket_side=basket.basket_side,
                requested_quantity=self.config.target_quantity,
                fee_model=self.config.fee_config,
                series_ticker=basket.event_ticker,
            )

            # Per-leg metrics
            per_leg_prices: Dict[str, Optional[int]] = {}
            per_leg_depth: Dict[str, int] = {}
            for t in pricing.leg_traversals:
                if t.average_price_cents is not None:
                    per_leg_prices[t.market_ticker] = int(round(float(t.average_price_cents)))
                else:
                    per_leg_prices[t.market_ticker] = None

            for ticker in basket.ordered_tickers:
                obs_ticker = snap.observations.get(ticker)
                book = obs_ticker.order_book if obs_ticker else None
                if book:
                    asks = derive_ask_levels(book, basket.basket_side)
                    per_leg_depth[ticker] = sum(int(lvl.quantity) for lvl in asks)
                else:
                    per_leg_depth[ticker] = 0
                if ticker not in per_leg_prices:
                    per_leg_prices[ticker] = None

            is_depth_ok = pricing.supported_quantity >= self.config.target_quantity
            is_gross_ok = pricing.is_gross_profitable
            is_net_ok = pricing.is_net_profitable is True
            is_partially_supported = 0 < pricing.supported_quantity < self.config.target_quantity

            if is_depth_ok:
                depth_supp += 1
            if is_gross_ok:
                gross_pos += 1
            if is_net_ok:
                fee_adj_pos += 1

            gross_edges.append(float(pricing.gross_profit_cents))
            if pricing.net_profit_cents is not None:
                net_edges.append(float(pricing.net_profit_cents))

            # Classify status & rejection
            if pricing.supported_quantity == 0:
                status = STATUS_REJECTED_INSUFFICIENT_DEPTH
                reason = pricing.rejection_reason or "Insufficient order book depth across legs"
                is_qual = False
            elif not is_gross_ok:
                status = STATUS_REJECTED_NO_GROSS_EDGE
                reason = (
                    pricing.rejection_reason
                    or f"No gross edge: total cost ({pricing.total_cost_cents}¢) exceeds payout ({pricing.guaranteed_payout_cents}¢)"
                )
                is_qual = False
            elif not is_net_ok:
                status = STATUS_REJECTED_UNPROFITABLE_FEES
                reason = (
                    pricing.rejection_reason
                    or f"Unprofitable after estimated fees: net edge {pricing.net_profit_cents}¢"
                )
                is_qual = False
            elif is_partially_supported:
                status = STATUS_REJECTED_INSUFFICIENT_QUANTITY
                reason = (
                    f"Insufficient common quantity: requested {self.config.target_quantity}, "
                    f"but only {pricing.supported_quantity} available across all legs"
                )
                is_qual = False
            else:
                status = STATUS_QUALIFIED
                reason = None
                is_qual = True
                qualified += 1

            if not is_qual:
                rejected_snaps += 1
                rej_counts[status] = rej_counts.get(status, 0) + 1

            rec = HistoricalBasketReplayRecord(
                replay_id=replay_id,
                snapshot_id=snap.snapshot_id,
                basket_id=basket.basket_id,
                event_ticker=basket.event_ticker,
                basket_side=basket.basket_side,
                market_tickers=basket.ordered_tickers,
                market_outcome_map=basket.market_outcome_map,
                source_timestamps=snap.source_timestamps,
                source_observation_ids=snap.observation_ids,
                snapshot_min_timestamp=snap.min_timestamp.isoformat() if snap.min_timestamp else None,
                snapshot_max_timestamp=snap.max_timestamp.isoformat() if snap.max_timestamp else None,
                leg_timestamp_skew_seconds=snap.leg_timestamp_skew_seconds,
                max_allowed_skew_seconds=self.config.max_leg_timestamp_skew_seconds,
                per_leg_prices_cents=per_leg_prices,
                per_leg_depth=per_leg_depth,
                common_executable_quantity=pricing.supported_quantity,
                gross_acquisition_cost_cents=pricing.total_cost_cents,
                guaranteed_payout_cents=pricing.guaranteed_payout_cents,
                gross_edge_cents=pricing.gross_profit_cents,
                estimated_fees_cents=pricing.estimated_fees_cents,
                net_edge_cents=pricing.net_profit_cents,
                is_gross_profitable=is_gross_ok,
                is_net_profitable=is_net_ok,
                is_depth_supported=is_depth_ok,
                is_qualified=is_qual,
                status=status,
                rejection_reason=reason,
                source_evidence_digest=dataset_sha256,
                market_state_fingerprint=snap.market_state_fingerprint,
            )
            records.append(rec)

        # 3. Market state deduplication & sensitivity metrics
        valid_fingerprints = [
            r.market_state_fingerprint
            for r in records
            if r.market_state_fingerprint is not None
        ]
        unique_market_states_count = len(set(valid_fingerprints))
        repeated_market_states_count = len(valid_fingerprints) - unique_market_states_count

        unmatched_baskets = getattr(self.reconstructor, "last_unmatched_baskets", ())

        opp_classifications = {
            "STRUCTURALLY_INVALID_BASKET": 0,
            "INSUFFICIENT_SYNCHRONIZATION": 0,
            "NO_EXECUTABLE_DEPTH": 0,
            "NO_GROSS_EDGE": 0,
            "UNPROFITABLE_AFTER_FEES": 0,
            "INSUFFICIENT_COMMON_QUANTITY": 0,
            "QUALIFIED_RESEARCH_CANDIDATE": 0,
        }
        for rec in records:
            if rec.status == STATUS_QUALIFIED:
                opp_classifications["QUALIFIED_RESEARCH_CANDIDATE"] += 1
            elif rec.status == STATUS_REJECTED_NO_GROSS_EDGE:
                opp_classifications["NO_GROSS_EDGE"] += 1
            elif rec.status == STATUS_REJECTED_UNPROFITABLE_FEES:
                opp_classifications["UNPROFITABLE_AFTER_FEES"] += 1
            elif rec.status == STATUS_REJECTED_INSUFFICIENT_DEPTH:
                opp_classifications["NO_EXECUTABLE_DEPTH"] += 1
            elif rec.status == STATUS_REJECTED_INSUFFICIENT_QUANTITY:
                opp_classifications["INSUFFICIENT_COMMON_QUANTITY"] += 1
            elif rec.status in (
                STATUS_REJECTED_MISSING_MARKET,
                STATUS_REJECTED_LEG_SKEW,
                STATUS_REJECTED_STALE,
                STATUS_REJECTED_MISSING_TIMESTAMP,
            ):
                opp_classifications["INSUFFICIENT_SYNCHRONIZATION"] += 1
            else:
                opp_classifications[rec.status] = opp_classifications.get(rec.status, 0) + 1

        skew_thresholds = [1.0, 2.0, 3.0, 5.0, 10.0]
        skew_counts = {
            f"skew_le_{thresh:.1f}s": sum(1 for s in skews if s <= thresh)
            for thresh in skew_thresholds
        }
        depths = [r.common_executable_quantity for r in records if r.is_depth_supported]
        depth_stats = _compute_stats([float(d) for d in depths])

        sensitivity_analysis = {
            "skew_threshold_sensitivity": skew_counts,
            "common_depth_distribution": depth_stats,
            "unique_market_states": unique_market_states_count,
            "repeated_market_states": repeated_market_states_count,
            "opportunity_classifications": opp_classifications,
        }

        # 4. Data quality limitations
        limitations = [
            "Sequential REST polling introduces non-zero inter-leg latency skew; quotes are not atomic exchange snapshots.",
            "Historical replay reflects static order-book snapshots; live multi-leg execution is exposed to queue priority and execution race conditions.",
            "Regulatory CFTC fee schedules are applied deterministically under official taker rates (7% up to caps).",
        ]
        if len(snapshots) < 30:
            limitations.append(
                f"Small sample size ({len(snapshots)} reconstructed snapshots) provides functional pipeline verification but is not statistically representative."
            )
        if unmatched_baskets:
            limitations.append(
                f"Unmatched declared basket(s) with zero observations in dataset: {list(unmatched_baskets)}."
            )

        basket_ids = tuple(b.basket_id or b.event_ticker for b in baskets)

        return HistoricalBasketReplaySummary(
            replay_id=replay_id,
            source_path=source_path,
            dataset_sha256=dataset_sha256,
            evaluated_at=now.isoformat(),
            baskets_configured=basket_ids,
            total_input_observations=len(observations),
            duplicate_observations_count=dups,
            total_snapshots_reconstructed=len(snapshots),
            complete_snapshots_count=complete_snaps,
            rejected_snapshots_count=rejected_snaps,
            rejected_by_reason=rej_counts,
            total_basket_evaluations=len(snapshots),
            gross_positive_count=gross_pos,
            fee_adjusted_positive_count=fee_adj_pos,
            depth_supported_count=depth_supp,
            qualified_count=qualified,
            gross_edge_distribution=_compute_stats(gross_edges),
            net_edge_distribution=_compute_stats(net_edges),
            skew_distribution=_compute_percentiles(skews),
            data_quality_limitations=tuple(limitations),
            records=tuple(records),
            unique_market_states_count=unique_market_states_count,
            repeated_market_states_count=repeated_market_states_count,
            unmatched_baskets=unmatched_baskets,
            opportunity_classifications=opp_classifications,
            sensitivity_analysis=sensitivity_analysis,
        )
