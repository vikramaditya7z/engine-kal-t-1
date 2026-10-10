"""
Kalshi Arbitrage Engine — V7 Historical Validation, Execution Realism & Strategy Performance

This module implements the V7 validation and performance-analysis layer:
1. Deep evidence-quality and provenance auditing (sampling cadence, gaps, liquidity health).
2. Multi-stage execution-realism validation (Theoretical vs Qualified vs Execution-Model vs Paper vs Realized).
3. Strategy performance distributions (gross edge, net edge, supported quantity, episode duration).
4. Ten-section auditable reporting and structured JSON serialization.
5. Parameter sensitivity sweeps and explicit paper-trading execution verification.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    cents_to_dollars,
)
from .contract import YES, NO
from .evaluation import (
    EvaluationConfig,
    EvaluationReport,
    HistoricalDataset,
    HistoricalEvaluator,
    OpportunityEvaluationRecord,
    SensitivityProfile,
    SensitivityReport,
    _normalize_timestamp,
    run_sensitivity_analysis,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
)
from .observer import DeclaredMeceBasket, MarketObservation
from .paper_trade import TradeState


# ---------------------------------------------------------------------------
# Distribution Statistics Helper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DistributionStats:
    """Exact percentile and distribution metrics across numeric samples."""

    count: int
    min_val: Optional[Decimal] = None
    p25: Optional[Decimal] = None
    median_val: Optional[Decimal] = None
    mean_val: Optional[Decimal] = None
    p75: Optional[Decimal] = None
    p90: Optional[Decimal] = None
    max_val: Optional[Decimal] = None

    @classmethod
    def from_values(cls, values: Sequence[Union[int, float, Decimal]]) -> "DistributionStats":
        """Compute distribution statistics with linear interpolation for percentiles."""
        if not values:
            return cls(count=0)

        dec_vals = sorted([Decimal(str(v)) for v in values])
        n = len(dec_vals)
        min_v = dec_vals[0]
        max_v = dec_vals[-1]
        mean_v = sum(dec_vals) / Decimal(n)

        def _percentile(p_str: str) -> Decimal:
            p = Decimal(p_str)
            k = Decimal(n - 1) * p
            f = int(k)
            c = f + 1
            if c >= n:
                return dec_vals[-1]
            return dec_vals[f] + (dec_vals[c] - dec_vals[f]) * (k - Decimal(f))

        return cls(
            count=n,
            min_val=min_v,
            p25=_percentile("0.25"),
            median_val=_percentile("0.50"),
            mean_val=mean_v,
            p75=_percentile("0.75"),
            p90=_percentile("0.90"),
            max_val=max_v,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert statistics to serializable dictionary."""
        return {
            "count": self.count,
            "min": str(self.min_val) if self.min_val is not None else None,
            "p25": str(self.p25.quantize(Decimal("0.01"))) if self.p25 is not None else None,
            "median": str(self.median_val.quantize(Decimal("0.01"))) if self.median_val is not None else None,
            "mean": str(self.mean_val.quantize(Decimal("0.01"))) if self.mean_val is not None else None,
            "p75": str(self.p75.quantize(Decimal("0.01"))) if self.p75 is not None else None,
            "p90": str(self.p90.quantize(Decimal("0.01"))) if self.p90 is not None else None,
            "max": str(self.max_val) if self.max_val is not None else None,
        }


# ---------------------------------------------------------------------------
# Evidence Quality & Provenance Audit Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObservationGap:
    """An interval between successive observations exceeding the cadence threshold."""

    from_timestamp: datetime
    to_timestamp: datetime
    duration_seconds: float
    from_ticker: str
    to_ticker: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_timestamp": self.from_timestamp.isoformat(),
            "to_timestamp": self.to_timestamp.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "from_ticker": self.from_ticker,
            "to_ticker": self.to_ticker,
        }


@dataclass(frozen=True)
class EvidenceAuditReport:
    """Complete provenance, data-quality, and order-book health audit of historical evidence."""

    source_description: str
    total_raw_records: int
    valid_records_count: int
    malformed_records_count: int
    duplicate_records_count: int
    out_of_order_records_count: int
    unique_tickers_count: int
    unique_tickers: Tuple[str, ...]
    unique_events_count: int
    unique_events: Tuple[str, ...]
    earliest_timestamp: Optional[datetime]
    latest_timestamp: Optional[datetime]
    time_span_seconds: float
    mean_interval_seconds: Optional[float]
    median_interval_seconds: Optional[float]
    min_interval_seconds: Optional[float]
    max_interval_seconds: Optional[float]
    gaps: Tuple[ObservationGap, ...]
    fresh_records_count: int
    stale_records_count: int
    future_drift_records_count: int
    missing_source_ts_count: int
    empty_books_count: int
    populated_books_count: int
    two_sided_books_count: int
    one_sided_books_count: int
    total_bid_depth_contracts: Decimal
    rejections_by_reason: Dict[str, int]
    ticker_distribution: Dict[str, int]
    sample_size_assessment: str
    audit_notes: Tuple[str, ...]

    def summary(self) -> str:
        """Render human-readable evidence audit summary."""
        lines = [
            "=" * 78,
            "KALSHI ARBITRAGE ENGINE — V7 EVIDENCE QUALITY AUDIT",
            "=" * 78,
            f"  Source Description:            {self.source_description}",
            f"  Total Raw Records:             {self.total_raw_records}",
            f"  Valid Parsed Observations:     {self.valid_records_count}",
            f"  Malformed Records:             {self.malformed_records_count}",
            f"  Duplicate Records:             {self.duplicate_records_count}",
            f"  Out-of-Order Ingestion Count:  {self.out_of_order_records_count}",
            f"  Unique Tickers / Events:       {self.unique_tickers_count} tickers / {self.unique_events_count} events",
            f"  Time Span:                     {self.earliest_timestamp.isoformat() if self.earliest_timestamp else 'N/A'} -> {self.latest_timestamp.isoformat() if self.latest_timestamp else 'N/A'} ({self.time_span_seconds:.1f}s)",
            f"  Observation Cadence (Mean):    {f'{self.mean_interval_seconds:.2f}s' if self.mean_interval_seconds is not None else 'N/A'} (Median: {f'{self.median_interval_seconds:.2f}s' if self.median_interval_seconds is not None else 'N/A'}, Max: {f'{self.max_interval_seconds:.2f}s' if self.max_interval_seconds is not None else 'N/A'})",
            f"  Cadence Gaps Detected:         {len(self.gaps)}",
            "",
            "DATA INTEGRITY & FRESHNESS:",
            f"  Fresh / Valid Order Books:     {self.fresh_records_count} ({self.fresh_records_count / self.valid_records_count * 100:.1f}%)" if self.valid_records_count else "  Fresh / Valid Order Books:     0",
            f"  Stale Order Books (> threshold):{self.stale_records_count}",
            f"  Future-Drifted Snapshots:      {self.future_drift_records_count}",
            f"  Missing Source Timestamps:     {self.missing_source_ts_count}",
            "",
            "ORDER BOOK LIQUIDITY PROFILE:",
            f"  Empty Order Books (0 bids):    {self.empty_books_count} ({self.empty_books_count / self.valid_records_count * 100:.1f}%)" if self.valid_records_count else "  Empty Order Books:             0",
            f"  Populated Books (>= 1 bid):    {self.populated_books_count}",
            f"  Two-Sided Books (YES & NO):    {self.two_sided_books_count}",
            f"  One-Sided Books (Only 1 side): {self.one_sided_books_count}",
            f"  Total Observable Depth:        {self.total_bid_depth_contracts} contracts (cumulative across repeated snapshot polls; not unique simultaneous liquidity)",
            "",
            f"EVIDENCE SAMPLE ASSESSMENT:      {self.sample_size_assessment}",
        ]
        if self.audit_notes:
            lines.extend(["", "AUDIT FINDINGS & CAVEATS:"])
            for note in self.audit_notes:
                lines.append(f"  - {note}")
        lines.append("=" * 78)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert audit report to serializable dictionary."""
        return {
            "source_description": self.source_description,
            "total_raw_records": self.total_raw_records,
            "valid_records_count": self.valid_records_count,
            "malformed_records_count": self.malformed_records_count,
            "duplicate_records_count": self.duplicate_records_count,
            "out_of_order_records_count": self.out_of_order_records_count,
            "unique_tickers_count": self.unique_tickers_count,
            "unique_tickers": list(self.unique_tickers),
            "unique_events_count": self.unique_events_count,
            "unique_events": list(self.unique_events),
            "earliest_timestamp": self.earliest_timestamp.isoformat() if self.earliest_timestamp else None,
            "latest_timestamp": self.latest_timestamp.isoformat() if self.latest_timestamp else None,
            "time_span_seconds": self.time_span_seconds,
            "mean_interval_seconds": self.mean_interval_seconds,
            "median_interval_seconds": self.median_interval_seconds,
            "min_interval_seconds": self.min_interval_seconds,
            "max_interval_seconds": self.max_interval_seconds,
            "gaps_count": len(self.gaps),
            "gaps": [g.to_dict() for g in self.gaps],
            "fresh_records_count": self.fresh_records_count,
            "stale_records_count": self.stale_records_count,
            "future_drift_records_count": self.future_drift_records_count,
            "missing_source_ts_count": self.missing_source_ts_count,
            "empty_books_count": self.empty_books_count,
            "populated_books_count": self.populated_books_count,
            "two_sided_books_count": self.two_sided_books_count,
            "one_sided_books_count": self.one_sided_books_count,
            "total_bid_depth_contracts": str(self.total_bid_depth_contracts),
            "total_bid_depth_cumulative_note": "Cumulative across repeated snapshot polls; not unique simultaneous liquidity",
            "rejections_by_reason": self.rejections_by_reason,
            "ticker_distribution": self.ticker_distribution,
            "sample_size_assessment": self.sample_size_assessment,
            "audit_notes": list(self.audit_notes),
        }


# ---------------------------------------------------------------------------
# Evidence Auditor Implementation
# ---------------------------------------------------------------------------

class EvidenceAuditor:
    """Performs deep, reproducible evidence quality, temporal cadence, and book depth auditing."""

    def __init__(
        self,
        max_stale_seconds: float = 60.0,
        max_future_seconds: float = 10.0,
        gap_threshold_seconds: float = 60.0,
        require_source_timestamp: bool = True,
    ):
        self.max_stale_seconds = max_stale_seconds
        self.max_future_seconds = max_future_seconds
        self.gap_threshold_seconds = gap_threshold_seconds
        self.require_source_timestamp = require_source_timestamp

    def audit(self, dataset: HistoricalDataset) -> EvidenceAuditReport:
        """Audit dataset and return structured metrics."""
        obs_list = dataset.observations
        valid_count = len(obs_list)

        events_set: Set[str] = set()
        ticker_counts: Dict[str, int] = {}
        rejection_reasons: Dict[str, int] = {}

        fresh_count = 0
        stale_count = 0
        future_drift_count = 0
        missing_source_count = 0

        empty_books = 0
        populated_books = 0
        two_sided_books = 0
        one_sided_books = 0
        total_depth = Decimal("0")

        intervals: List[float] = []
        gaps: List[ObservationGap] = []

        norm_timestamps: List[datetime] = []
        for i, obs in enumerate(obs_list):
            ticker_counts[obs.ticker] = ticker_counts.get(obs.ticker, 0) + 1
            if obs.market and obs.market.event_ticker:
                events_set.add(obs.market.event_ticker)

            norm_obs = _normalize_timestamp(obs.observed_at)
            norm_timestamps.append(norm_obs)

            # Cadence intervals
            if i > 0:
                prev_norm = norm_timestamps[i - 1]
                delta_sec = (norm_obs - prev_norm).total_seconds()
                if delta_sec >= 0:
                    intervals.append(delta_sec)
                    if delta_sec > self.gap_threshold_seconds:
                        gaps.append(
                            ObservationGap(
                                from_timestamp=obs_list[i - 1].observed_at,
                                to_timestamp=obs.observed_at,
                                duration_seconds=delta_sec,
                                from_ticker=obs_list[i - 1].ticker,
                                to_ticker=obs.ticker,
                            )
                        )

            # Freshness and staleness tracking
            source_ts = obs.source_timestamp
            is_stale_flag = obs.is_stale
            is_record_fresh = True

            if is_stale_flag:
                is_record_fresh = False
                stale_count += 1
                reason = obs.staleness_reason or "Marked as stale by observer"
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            elif source_ts is None:
                missing_source_count += 1
                if self.require_source_timestamp:
                    is_record_fresh = False
                    reason = "Missing exchange source timestamp"
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            else:
                norm_src = _normalize_timestamp(source_ts)
                age_sec = (norm_obs - norm_src).total_seconds()
                if age_sec < -self.max_future_seconds:
                    is_record_fresh = False
                    future_drift_count += 1
                    reason = f"Source timestamp is from the future ({age_sec:.1f}s)"
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                elif age_sec > self.max_stale_seconds:
                    is_record_fresh = False
                    stale_count += 1
                    reason = f"Order book age {age_sec:.1f}s > max {self.max_stale_seconds:.1f}s"
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

            if is_record_fresh:
                fresh_count += 1

            # Order book liquidity profiling
            if obs.order_book is not None:
                yes_len = len(obs.order_book.yes_bids)
                no_len = len(obs.order_book.no_bids)
                if yes_len == 0 and no_len == 0:
                    empty_books += 1
                else:
                    populated_books += 1
                    if yes_len > 0 and no_len > 0:
                        two_sided_books += 1
                    else:
                        one_sided_books += 1

                    for bid in obs.order_book.yes_bids:
                        total_depth += bid.quantity
                    for bid in obs.order_book.no_bids:
                        total_depth += bid.quantity
            else:
                empty_books += 1

        # Summary intervals
        mean_int = sum(intervals) / len(intervals) if intervals else None
        sorted_ints = sorted(intervals)
        med_int = (
            sorted_ints[len(sorted_ints) // 2]
            if sorted_ints
            else None
        )
        min_int = min(intervals) if intervals else None
        max_int = max(intervals) if intervals else None

        # Time span
        earliest_norm = _normalize_timestamp(dataset.earliest_timestamp)
        latest_norm = _normalize_timestamp(dataset.latest_timestamp)
        span_sec = (
            (latest_norm - earliest_norm).total_seconds()
            if earliest_norm and latest_norm
            else 0.0
        )

        # Sample size and evidentiary confidence assessment
        notes: List[str] = []
        if valid_count == 0:
            assessment = "EMPTY_DATASET"
            notes.append("Dataset contains 0 valid observations. No evaluation possible.")
        elif valid_count < 100 or span_sec < 3600:
            assessment = "LIMITED_TRIAL_SAMPLE"
            notes.append(
                f"Sample size ({valid_count} observations over {span_sec / 60:.1f} minutes) "
                "represents a short trial run rather than a representative historical regime."
            )
            notes.append("Results cannot be used to infer long-term strategy profitability.")
        else:
            assessment = "SUBSTANTIAL_HISTORICAL_SAMPLE"
            notes.append("Sample size supports robust empirical evaluation.")

        if empty_books > 0:
            pct_empty = (empty_books / valid_count) * 100 if valid_count else 0
            notes.append(
                f"{empty_books}/{valid_count} ({pct_empty:.1f}%) observations have completely empty order books "
                "(zero counter-party bid liquidity)."
            )

        if stale_count > 0:
            pct_stale = (stale_count / valid_count) * 100 if valid_count else 0
            notes.append(
                f"{stale_count}/{valid_count} ({pct_stale:.1f}%) observations reflect stale snapshots "
                f"exceeding the {self.max_stale_seconds:.1f}s threshold."
            )

        return EvidenceAuditReport(
            source_description=dataset.source_description,
            total_raw_records=dataset.total_raw_records,
            valid_records_count=valid_count,
            malformed_records_count=dataset.malformed_records_count,
            duplicate_records_count=dataset.duplicate_records_count,
            out_of_order_records_count=dataset.out_of_order_records_count,
            unique_tickers_count=len(dataset.unique_tickers),
            unique_tickers=dataset.unique_tickers,
            unique_events_count=len(events_set),
            unique_events=tuple(sorted(events_set)),
            earliest_timestamp=dataset.earliest_timestamp,
            latest_timestamp=dataset.latest_timestamp,
            time_span_seconds=span_sec,
            mean_interval_seconds=mean_int,
            median_interval_seconds=med_int,
            min_interval_seconds=min_int,
            max_interval_seconds=max_int,
            gaps=tuple(gaps),
            fresh_records_count=fresh_count,
            stale_records_count=stale_count,
            future_drift_records_count=future_drift_count,
            missing_source_ts_count=missing_source_count,
            empty_books_count=empty_books,
            populated_books_count=populated_books,
            two_sided_books_count=two_sided_books,
            one_sided_books_count=one_sided_books,
            total_bid_depth_contracts=total_depth,
            rejections_by_reason=rejection_reasons,
            ticker_distribution=ticker_counts,
            sample_size_assessment=assessment,
            audit_notes=tuple(notes),
        )


# ---------------------------------------------------------------------------
# V7 Validation Report & Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V7ValidationConfig:
    """Comprehensive execution-realism and validation configuration."""

    evaluation_config: EvaluationConfig = field(default_factory=EvaluationConfig)
    gap_threshold_seconds: float = 60.0
    audit_only: bool = False
    run_sensitivity: bool = False


@dataclass(frozen=True)
class V7ValidationReport:
    """Ten-section auditable historical validation report."""

    config: EvaluationConfig
    audit_report: EvidenceAuditReport
    evaluation_report: Optional[EvaluationReport]
    funnel_raw_observations: int
    funnel_fresh_observations: int
    funnel_theoretical_candidates: int
    funnel_execution_model_candidates: int
    funnel_net_profitable_candidates: int
    funnel_qualified_candidates: int
    funnel_initial_episodes: int
    funnel_paper_trades_submitted: int
    funnel_paper_trades_filled: int
    funnel_paper_trades_partial: int
    funnel_paper_trades_rejected: int
    funnel_realized_trades: int  # Always 0 in historical backtests without counter-party settlement
    gross_edge_distribution: DistributionStats
    net_edge_distribution: DistributionStats
    quantity_distribution: DistributionStats
    episode_duration_distribution: DistributionStats
    results_by_market: Dict[str, Dict[str, Any]]
    results_by_strategy: Dict[str, Dict[str, Any]]
    sensitivity_report: Optional[SensitivityReport]
    data_limitations: Tuple[str, ...]
    final_verdict: str
    representative_qualified: Tuple[Dict[str, Any], ...]
    representative_rejected: Tuple[Dict[str, Any], ...]
    funnel_raw_ingestion_records: int = 0
    funnel_valid_parsed_observations: int = 0

    def __post_init__(self) -> None:
        if self.funnel_raw_ingestion_records == 0 and self.audit_report:
            object.__setattr__(self, "funnel_raw_ingestion_records", self.audit_report.total_raw_records)
        if self.funnel_valid_parsed_observations == 0 and self.audit_report:
            object.__setattr__(self, "funnel_valid_parsed_observations", self.audit_report.valid_records_count)

    def summary(self) -> str:
        """Render the complete 10-section human-readable report."""
        cfg = self.config
        aud = self.audit_report
        ev = self.evaluation_report

        lines = [
            "=" * 92,
            "KALSHI ARBITRAGE ENGINE — V7 HISTORICAL VALIDATION & STRATEGY PERFORMANCE REPORT",
            "=" * 92,
            "",
            "SECTION 1: DATASET IDENTITY & COVERAGE",
            f"  Source Description:                {aud.source_description}",
            f"  Raw Ingestion Records:             {aud.total_raw_records}",
            f"  Valid Parsed Observations:         {aud.valid_records_count}",
            f"  Unique Markets Observed:           {aud.unique_tickers_count} tickers across {aud.unique_events_count} events",
            f"  Chronological Span:                {aud.earliest_timestamp.isoformat() if aud.earliest_timestamp else 'N/A'} -> {aud.latest_timestamp.isoformat() if aud.latest_timestamp else 'N/A'}",
            f"  Total Duration:                    {aud.time_span_seconds:.1f}s ({aud.time_span_seconds / 60:.1f} minutes)",
            f"  Sampling Cadence (Mean / Median):  {f'{aud.mean_interval_seconds:.2f}s' if aud.mean_interval_seconds is not None else 'N/A'} / {f'{aud.median_interval_seconds:.2f}s' if aud.median_interval_seconds is not None else 'N/A'}",
            "",
            "SECTION 2: EVIDENCE QUALITY & DATA LIMITATIONS",
            f"  Data Ingestion Quality:            {aud.malformed_records_count} malformed, {aud.duplicate_records_count} duplicate, {aud.out_of_order_records_count} out-of-order",
            f"  Freshness Pass Rate:               {aud.fresh_records_count}/{aud.valid_records_count} ({aud.fresh_records_count / aud.valid_records_count * 100:.1f}%)" if aud.valid_records_count else "  Freshness Pass Rate:               N/A",
            f"  Stale Observations (> max age):    {aud.stale_records_count}",
            f"  Future-Drifted Snapshots:          {aud.future_drift_records_count}",
            f"  Missing Source Timestamps:         {aud.missing_source_ts_count}",
            f"  Empty Books (Zero Bids):           {aud.empty_books_count} ({aud.empty_books_count / aud.valid_records_count * 100:.1f}%)" if aud.valid_records_count else "  Empty Books (Zero Bids):           N/A",
            f"  Total Cumulative Bid Depth:        {aud.total_bid_depth_contracts} contracts (cumulative across repeated snapshot polls; not unique simultaneous liquidity)",
            f"  Sample Size Assessment:            {aud.sample_size_assessment}",
            "",
            "SECTION 3: EVALUATION CONFIGURATION & EXECUTION ASSUMPTIONS",
            f"  Target Quantity:                   {cfg.target_quantity} contracts",
            f"  Configured Fee per Contract:       {f'{cfg.default_fee_per_contract_cents}¢' if cfg.default_fee_per_contract_cents is not None else 'None (Unconfigured)'}",
            f"  Conservative Slippage Haircut:     {cfg.conservative_haircut_cents}¢ per contract",
            f"  Max Freshness Stale Threshold:     {cfg.max_stale_seconds:.1f}s",
            f"  Max Future Drift Threshold:        {cfg.max_future_seconds:.1f}s",
            f"  Max Multi-Leg Timestamp Skew:      {cfg.max_leg_timestamp_skew_seconds:.1f}s",
            f"  Episode Inactivity Timeout:        {cfg.episode_timeout_seconds:.1f}s",
            f"  Enforce Net Profitability:         {cfg.enforce_net_profitability}",
            f"  Paper Trading Simulation:          {'ENABLED' if cfg.enable_paper_trading else 'DISABLED (Evaluation Only)'}",
            "",
            "SECTION 4: CANDIDATE QUALIFICATION & MULTI-STAGE REJECTION BREAKDOWN",
            f"  Total Raw Ingestion Records:       {self.funnel_raw_ingestion_records}",
            f"  Valid Parsed Observations:         {self.funnel_valid_parsed_observations}",
            f"  Fresh Observations:                {self.funnel_fresh_observations}",
            f"  Theoretical Candidates (Edge > 0): {self.funnel_theoretical_candidates}",
            f"  Execution-Model Candidates:        {self.funnel_execution_model_candidates}",
            f"  Net-Profitable Candidates:         {self.funnel_net_profitable_candidates}",
            f"  Qualified Opportunities:           {self.funnel_qualified_candidates}",
            f"  Initial Episodes (Trade Eligible): {self.funnel_initial_episodes}",
        ]

        if ev and ev.metrics.rejections_by_status:
            lines.append("  Rejections by Pipeline Stage:")
            for status, count in sorted(ev.metrics.rejections_by_status.items()):
                lines.append(f"    - {status}: {count}")

        lines.extend([
            "",
            "SECTION 5: GROSS AND MODELED NET EDGE DISTRIBUTIONS",
            (
                f"  Theoretical Gross Edge (cents):    Min: {self.gross_edge_distribution.min_val}¢ | "
                f"Median: {self.gross_edge_distribution.median_val:.1f}¢ | "
                f"Mean: {self.gross_edge_distribution.mean_val:.2f}¢ | "
                f"P90: {self.gross_edge_distribution.p90:.1f}¢ | "
                f"Max: {self.gross_edge_distribution.max_val}¢"
                if self.gross_edge_distribution.count > 0
                else "  Theoretical Gross Edge (cents):    (No candidate opportunities observed)"
            ),
            (
                f"  Modeled Net Edge (cents):          Min: {self.net_edge_distribution.min_val}¢ | "
                f"Median: {self.net_edge_distribution.median_val:.1f}¢ | "
                f"Mean: {self.net_edge_distribution.mean_val:.2f}¢ | "
                f"P90: {self.net_edge_distribution.p90:.1f}¢ | "
                f"Max: {self.net_edge_distribution.max_val}¢"
                if self.net_edge_distribution.count > 0
                else "  Modeled Net Edge (cents):          (No candidate opportunities observed)"
            ),
            (
                f"  Episode Duration (seconds):        Min: {self.episode_duration_distribution.min_val:.1f}s | "
                f"Median: {self.episode_duration_distribution.median_val:.1f}s | "
                f"Mean: {self.episode_duration_distribution.mean_val:.2f}s | "
                f"P90: {self.episode_duration_distribution.p90:.1f}s | "
                f"Max: {self.episode_duration_distribution.max_val:.1f}s"
                if self.episode_duration_distribution.count > 0
                else "  Episode Duration (seconds):        (No candidate opportunities observed)"
            ),
            "",
            "SECTION 6: LIQUIDITY AND SUPPORTED QUANTITY ANALYSIS",
            f"  Requested Target Quantity:         {cfg.target_quantity} contracts",
            (
                f"  Executable Quantity Supported:     Min: {self.quantity_distribution.min_val} | "
                f"Median: {self.quantity_distribution.median_val:.1f} | "
                f"Mean: {self.quantity_distribution.mean_val:.2f} | "
                f"Max: {self.quantity_distribution.max_val}"
                if self.quantity_distribution.count > 0
                else "  Executable Quantity Supported:     (No candidate opportunities observed)"
            ),
            f"  Total Observable Counterparty Bids:{aud.total_bid_depth_contracts} contracts (cumulative across repeated snapshot polls; not unique simultaneous liquidity)",
            "",
            "SECTION 7: PAPER TRADING PERFORMANCE (SIMULATED LEDGER)",
        ])

        if cfg.enable_paper_trading and ev:
            m = ev.metrics
            lines.extend([
                f"  Simulated Trades Submitted:        {self.funnel_paper_trades_submitted}",
                f"  Simulated Trades Filled:           {self.funnel_paper_trades_filled}",
                f"  Simulated Trades Partially Filled: {self.funnel_paper_trades_partial}",
                f"  Simulated Trades Rejected (Risk):  {self.funnel_paper_trades_rejected}",
                f"  Realized Counterparty Trades:      {self.funnel_realized_trades} (Historical replay does not place real orders)",
                f"  Simulated Fill Rate:               {f'{m.paper_fill_rate:.1%}' if m.paper_fill_rate is not None else 'N/A'}",
                f"  Total Simulated Fees Paid:         {m.paper_total_fees_cents}¢ (${cents_to_dollars(m.paper_total_fees_cents):.2f})",
                f"  Simulated Realized P&L:            {m.paper_realized_pnl_cents}¢ (${cents_to_dollars(m.paper_realized_pnl_cents):.2f})",
                f"  Simulated Unrealized P&L:          {m.paper_unrealized_pnl_cents}¢ (Valued strictly at book cost basis)",
                f"  Final Available Cash:              {m.paper_final_cash_cents}¢ (${cents_to_dollars(m.paper_final_cash_cents or 0):.2f})",
                f"  Final Portfolio Book Equity:       {m.paper_final_equity_cents}¢ (${cents_to_dollars(m.paper_final_equity_cents or 0):.2f})",
                f"  Max Drawdown (Book Equity):        {m.paper_max_drawdown_cents}¢ ({m.paper_max_drawdown_pct:.2f}%)",
                f"  Ledger Consistency Reconciliation: {'RECONCILED (0 discrepancies)' if m.ledger_is_reconciled else 'DISCREPANCY DETECTED'}",
            ])
        else:
            lines.extend([
                "  Paper Trading Simulation:          DISABLED (Pass --enable-paper-trading to execute simulated ledger)",
            ])

        lines.extend([
            "",
            "SECTION 8: PARAMETER SENSITIVITY ANALYSIS",
        ])
        if self.sensitivity_report:
            lines.append("  Summary of Assumptions Tested:")
            for row in self.sensitivity_report.rows:
                fee_str = f"{row.fee_per_contract_cents}¢" if row.fee_per_contract_cents is not None else "None"
                net_str = f"{row.mean_net_edge_cents:.2f}¢" if row.mean_net_edge_cents is not None else "N/A"
                lines.append(
                    f"    - {row.name:<24} | Fee: {fee_str:<4} | Haircut: {row.conservative_haircut_cents}¢ | Stale: {int(row.max_stale_seconds)}s | Qualified: {row.qualified_count:<4} | Rate: {row.qualification_rate_pct:>5.1f}% | Net: {net_str}"
                )
        else:
            lines.append("  (Sensitivity analysis not executed; pass --sensitivity to run parameter sweeps)")

        lines.extend([
            "",
            "SECTION 9: DATA & MODEL LIMITATIONS",
        ])
        for lim in self.data_limitations:
            lines.append(f"  - {lim}")

        lines.extend([
            "",
            "SECTION 10: FINAL EVIDENCE-BACKED VERDICT",
            f"  {self.final_verdict}",
            "=" * 92,
        ])
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert V7 report to serializable dictionary for JSON exports."""
        cfg = self.config
        ev = self.evaluation_report
        m = ev.metrics if ev else None

        return {
            "section_1_dataset_identity": {
                "source_description": self.audit_report.source_description,
                "total_raw_records": self.audit_report.total_raw_records,
                "valid_parsed_observations": self.audit_report.valid_records_count,
                "unique_tickers_count": self.audit_report.unique_tickers_count,
                "unique_events_count": self.audit_report.unique_events_count,
                "earliest_timestamp": self.audit_report.earliest_timestamp.isoformat() if self.audit_report.earliest_timestamp else None,
                "latest_timestamp": self.audit_report.latest_timestamp.isoformat() if self.audit_report.latest_timestamp else None,
                "time_span_seconds": self.audit_report.time_span_seconds,
                "mean_interval_seconds": self.audit_report.mean_interval_seconds,
                "median_interval_seconds": self.audit_report.median_interval_seconds,
            },
            "section_2_evidence_quality": self.audit_report.to_dict(),
            "section_3_evaluation_configuration": {
                "target_quantity": cfg.target_quantity,
                "default_fee_per_contract_cents": cfg.default_fee_per_contract_cents,
                "conservative_haircut_cents": cfg.conservative_haircut_cents,
                "max_stale_seconds": cfg.max_stale_seconds,
                "max_future_seconds": cfg.max_future_seconds,
                "max_leg_timestamp_skew_seconds": cfg.max_leg_timestamp_skew_seconds,
                "episode_timeout_seconds": cfg.episode_timeout_seconds,
                "enforce_net_profitability": cfg.enforce_net_profitability,
                "enable_paper_trading": cfg.enable_paper_trading,
            },
            "section_4_qualification_funnel": {
                "raw_ingestion_records": self.funnel_raw_ingestion_records,
                "total_raw_ingestion_records": self.funnel_raw_ingestion_records,
                "valid_parsed_observations": self.funnel_valid_parsed_observations,
                "raw_observations": self.funnel_raw_observations,
                "fresh_observations": self.funnel_fresh_observations,
                "theoretical_candidates": self.funnel_theoretical_candidates,
                "execution_model_candidates": self.funnel_execution_model_candidates,
                "net_profitable_candidates": self.funnel_net_profitable_candidates,
                "qualified_candidates": self.funnel_qualified_candidates,
                "initial_episodes": self.funnel_initial_episodes,
                "rejections_by_status": m.rejections_by_status if m else {},
            },
            "section_5_edge_distributions": {
                "theoretical_gross_edge": self.gross_edge_distribution.to_dict(),
                "modeled_net_edge": self.net_edge_distribution.to_dict(),
                "episode_duration_seconds": self.episode_duration_distribution.to_dict(),
            },
            "section_6_liquidity_analysis": {
                "supported_quantity": self.quantity_distribution.to_dict(),
                "total_bid_depth_contracts": str(self.audit_report.total_bid_depth_contracts),
                "total_bid_depth_cumulative_note": "Cumulative across repeated snapshot polls; not unique simultaneous liquidity",
            },
            "section_7_paper_trading_performance": {
                "enabled": cfg.enable_paper_trading,
                "paper_trades_submitted": self.funnel_paper_trades_submitted,
                "paper_trades_filled": self.funnel_paper_trades_filled,
                "paper_trades_partial": self.funnel_paper_trades_partial,
                "paper_trades_rejected": self.funnel_paper_trades_rejected,
                "realized_trades": self.funnel_realized_trades,
                "fill_rate": str(m.paper_fill_rate) if m and m.paper_fill_rate is not None else None,
                "total_fees_cents": m.paper_total_fees_cents if m else 0,
                "realized_pnl_cents": m.paper_realized_pnl_cents if m else 0,
                "unrealized_pnl_cents": m.paper_unrealized_pnl_cents if m else 0,
                "final_cash_cents": m.paper_final_cash_cents if m else None,
                "final_equity_cents": m.paper_final_equity_cents if m else None,
                "max_drawdown_cents": m.paper_max_drawdown_cents if m else 0,
                "max_drawdown_pct": str(m.paper_max_drawdown_pct) if m else "0",
                "ledger_is_reconciled": m.ledger_is_reconciled if m else True,
            },
            "section_8_sensitivity_analysis": [
                {
                    "profile_name": r.name,
                    "fee_cents": r.fee_per_contract_cents,
                    "haircut_cents": r.conservative_haircut_cents,
                    "stale_seconds": r.max_stale_seconds,
                    "target_quantity": r.target_quantity,
                    "qualified_count": r.qualified_count,
                    "qualification_rate_pct": str(r.qualification_rate_pct),
                    "mean_net_edge_cents": str(r.mean_net_edge_cents) if r.mean_net_edge_cents is not None else None,
                    "simulated_net_profit_cents": r.simulated_net_profit_cents,
                }
                for r in self.sensitivity_report.rows
            ] if self.sensitivity_report else [],
            "section_9_limitations": list(self.data_limitations),
            "section_10_final_verdict": self.final_verdict,
            "provenance_qualified_cases": list(self.representative_qualified),
            "provenance_rejected_cases": list(self.representative_rejected),
            "breakdown_by_market": self.results_by_market,
            "breakdown_by_strategy": self.results_by_strategy,
        }


# ---------------------------------------------------------------------------
# V7 Historical Validator Pipeline
# ---------------------------------------------------------------------------

class V7HistoricalValidator:
    """Rigorous historical validation and execution realism engine."""

    def __init__(self, config: Optional[V7ValidationConfig] = None):
        self.config = config or V7ValidationConfig()
        self.auditor = EvidenceAuditor(
            max_stale_seconds=self.config.evaluation_config.max_stale_seconds,
            max_future_seconds=self.config.evaluation_config.max_future_seconds,
            gap_threshold_seconds=self.config.gap_threshold_seconds,
            require_source_timestamp=self.config.evaluation_config.require_source_timestamp,
        )

    def validate(self, dataset: HistoricalDataset) -> V7ValidationReport:
        """Run complete V7 validation pipeline against a historical dataset."""
        cfg = self.config.evaluation_config

        # 1. Evidence Quality Audit
        audit_rep = self.auditor.audit(dataset)

        # 2. Historical Opportunity Evaluation (Chronological Replay)
        eval_rep: Optional[EvaluationReport] = None
        records: Sequence[OpportunityEvaluationRecord] = ()
        paper_trades: Sequence[PaperTrade] = ()

        if not self.config.audit_only:
            evaluator = HistoricalEvaluator(cfg)
            eval_rep = evaluator.evaluate(dataset)
            records = eval_rep.evaluated_records
            paper_trades = eval_rep.paper_trades

        # 3. Qualification Funnel Metrics
        raw_ingest = audit_rep.total_raw_records
        valid_obs = audit_rep.valid_records_count
        raw_obs = valid_obs
        fresh_obs = audit_rep.fresh_records_count

        # Theoretical candidates meet mathematical arbitrage edge
        theor_cand = sum(
            1 for r in records if r.theoretical_gross_edge_cents > 0
        )
        # Execution-model candidates have observable counter-party depth
        exec_model_cand = sum(
            1 for r in records if r.supported_quantity > 0
        )
        # Net-profitable candidates have modeled net edge after fees and haircut
        net_prof_cand = sum(
            1 for r in records if r.net_edge_cents is not None and r.net_edge_cents > 0
        )
        # Qualified candidates pass all freshness, depth, and net edge checks
        qual_cand = sum(1 for r in records if r.is_qualified)
        # Initial episodes are qualified and not recurrent
        init_episodes = sum(1 for r in records if r.is_qualified and not r.is_recurrent)

        # Paper trade status
        submitted = len(paper_trades)
        filled = sum(1 for t in paper_trades if t.state == TradeState.FILLED)
        partial = sum(1 for t in paper_trades if t.state == TradeState.PARTIALLY_FILLED)
        rejected = sum(1 for t in paper_trades if t.state == TradeState.REJECTED)

        # 4. Statistical Distributions
        gross_edges = [r.theoretical_gross_edge_cents for r in records]
        net_edges = [r.net_edge_cents for r in records if r.net_edge_cents is not None]
        quantities = [r.supported_quantity for r in records if r.supported_quantity > 0]

        gross_dist = DistributionStats.from_values(gross_edges)
        net_dist = DistributionStats.from_values(net_edges)
        qty_dist = DistributionStats.from_values(quantities)

        # Episode duration distribution (qualified episodes only, respecting episode boundaries)
        episode_spans: List[float] = []
        active_qualified_episodes: Dict[str, Tuple[datetime, datetime]] = {}

        for r in records:
            opp_id = r.opportunity_id
            norm_obs = _normalize_timestamp(r.observed_at)

            if r.is_qualified:
                if opp_id in active_qualified_episodes:
                    start_dt, last_dt = active_qualified_episodes[opp_id]
                    norm_last = _normalize_timestamp(last_dt)
                    gap_seconds = (norm_obs - norm_last).total_seconds()

                    # An episode continues only if marked recurrent and within inactivity timeout
                    if r.is_recurrent and gap_seconds <= cfg.episode_timeout_seconds:
                        active_qualified_episodes[opp_id] = (start_dt, r.observed_at)
                    else:
                        # Prior episode ended; finalize its duration
                        span = (norm_last - _normalize_timestamp(start_dt)).total_seconds()
                        episode_spans.append(span)
                        # Start new episode
                        active_qualified_episodes[opp_id] = (r.observed_at, r.observed_at)
                else:
                    # Start new qualified episode
                    active_qualified_episodes[opp_id] = (r.observed_at, r.observed_at)
            else:
                # Intervening non-qualified snapshot cleanly terminates active episode
                if opp_id in active_qualified_episodes:
                    start_dt, last_dt = active_qualified_episodes[opp_id]
                    span = (_normalize_timestamp(last_dt) - _normalize_timestamp(start_dt)).total_seconds()
                    episode_spans.append(span)
                    del active_qualified_episodes[opp_id]

        # Finalize any remaining open qualified episodes at end of replay
        for start_dt, last_dt in active_qualified_episodes.values():
            span = (_normalize_timestamp(last_dt) - _normalize_timestamp(start_dt)).total_seconds()
            episode_spans.append(span)

        duration_dist = DistributionStats.from_values(episode_spans)

        # 5. Breakdowns by Market and Strategy
        market_stats: Dict[str, Dict[str, Any]] = {}
        for r in records:
            for ticker in r.market_tickers:
                st = market_stats.setdefault(
                    ticker,
                    {
                        "total_candidates": 0,
                        "qualified": 0,
                        "rejected": 0,
                        "mean_gross_edge_cents": Decimal("0"),
                        "total_gross_cents": 0,
                    },
                )
                st["total_candidates"] += 1
                if r.is_qualified:
                    st["qualified"] += 1
                else:
                    st["rejected"] += 1
                st["total_gross_cents"] += r.theoretical_gross_edge_cents

        for ticker, st in market_stats.items():
            cnt = st["total_candidates"]
            st["mean_gross_edge_cents"] = (
                str(Decimal(st["total_gross_cents"]) / Decimal(cnt))
                if cnt > 0
                else "0.00"
            )

        strategy_stats: Dict[str, Dict[str, Any]] = {}
        for r in records:
            s_st = strategy_stats.setdefault(
                r.strategy_type,
                {
                    "total_candidates": 0,
                    "qualified": 0,
                    "rejected": 0,
                },
            )
            s_st["total_candidates"] += 1
            if r.is_qualified:
                s_st["qualified"] += 1
            else:
                s_st["rejected"] += 1

        # 6. Sensitivity Analysis
        sens_rep: Optional[SensitivityReport] = None
        if self.config.run_sensitivity and not self.config.audit_only:
            sens_rep = run_sensitivity_analysis(dataset, base_config=cfg)

        # 7. Representative Cases with Provenance
        qualified_cases: List[Dict[str, Any]] = []
        rejected_cases: List[Dict[str, Any]] = []

        for r in records:
            case_data = {
                "evaluation_id": r.evaluation_id,
                "opportunity_id": r.opportunity_id,
                "strategy_type": r.strategy_type,
                "tickers": list(r.market_tickers),
                "observed_at": r.observed_at.isoformat(),
                "theoretical_gross_edge_cents": r.theoretical_gross_edge_cents,
                "net_edge_cents": r.net_edge_cents,
                "supported_quantity": r.supported_quantity,
                "status": r.status,
                "rejection_reason": r.rejection_reason,
            }
            if r.is_qualified and len(qualified_cases) < 5:
                qualified_cases.append(case_data)
            elif not r.is_qualified and len(rejected_cases) < 5:
                rejected_cases.append(case_data)

        # 8. Data & Model Limitations
        limitations = [
            f"EVIDENTIARY HORIZON: Dataset spans {audit_rep.time_span_seconds:.1f}s ({audit_rep.time_span_seconds / 60:.1f} min) across {audit_rep.unique_tickers_count} markets. Short trial observations cannot establish regime persistence or statistical alpha.",
            f"ORDER BOOK LIQUIDITY: {audit_rep.empty_books_count}/{audit_rep.valid_records_count} ({audit_rep.empty_books_count / audit_rep.valid_records_count * 100:.1f}%) snapshots had completely empty order books with zero resting bids." if audit_rep.valid_records_count else "ORDER BOOK LIQUIDITY: No valid observations loaded.",
            "REST TRANSPORT TIMESTAMP PROXY: Freshness relies on exchange HTTP Date headers, which reflect response serialization time rather than matching-engine tick generation.",
            "SIMULATED LIQUIDITY FILL MODEL: Paper trading walks resting depth instantaneously and does not model queue priority, matching engine latency, adverse selection, or aggressive cancellations.",
            "CAPITAL & RESOLUTION: Replay equity reflects book-value cost basis. Arbitrage gains remain unrealized until official contract settlement and payout distribution.",
        ]

        # 9. Final Evidence-Backed Verdict
        if self.config.audit_only:
            verdict = (
                f"EVIDENCE AUDIT ONLY: Audited {raw_obs} observations across {audit_rep.unique_tickers_count} markets. "
                "Opportunity evaluation and paper trading were skipped."
            )
        elif raw_obs == 0:
            verdict = "EMPTY DATASET: Zero observations provided. No strategy viability or market evaluation can be established."
        elif audit_rep.populated_books_count == 0:
            verdict = (
                f"NO LIQUIDITY IN EVIDENCE: All {raw_obs} order book snapshots are completely empty (zero bids). "
                "The engine correctly rejects phantom trades (fail-closed integrity verified), but market executability cannot be evaluated."
            )
        elif qual_cand == 0:
            if audit_rep.sample_size_assessment == "LIMITED_TRIAL_SAMPLE":
                verdict = (
                    f"INSUFFICIENT HISTORICAL EVIDENCE: Evaluated trial dataset of {raw_obs} observations across {audit_rep.unique_tickers_count} markets over {audit_rep.time_span_seconds / 60:.1f} minutes. "
                    "The engine demonstrated fail-closed data integrity (rejecting empty and stale books), but zero candidate arbitrage opportunities exist in this trial sample. "
                    "Strategy profitability cannot be evaluated without broader, continuous market data."
                )
            else:
                verdict = (
                    f"NO EXECUTABLE ARBITRAGE DETECTED: Across {raw_obs} valid observations, zero opportunities satisfied all data-freshness, book-depth, fee, and risk constraints. "
                    "Available evidence does not support an active arbitrage edge under current market assumptions."
                )
        else:
            pnl_str = f"{eval_rep.metrics.paper_realized_pnl_cents}¢" if eval_rep else "0¢"
            verdict = (
                f"QUALIFIED OPPORTUNITIES CONFIRMED: {qual_cand} opportunity snapshots ({init_episodes} distinct episodes) survived depth and fee constraints. "
                f"In simulated paper trading, {filled} full fills and {partial} partial fills executed. "
                "Note: Paper performance does not establish live profitability until counter-party fills and market settlement are confirmed."
            )

        return V7ValidationReport(
            config=cfg,
            audit_report=audit_rep,
            evaluation_report=eval_rep,
            funnel_raw_observations=raw_obs,
            funnel_fresh_observations=fresh_obs,
            funnel_theoretical_candidates=theor_cand,
            funnel_execution_model_candidates=exec_model_cand,
            funnel_net_profitable_candidates=net_prof_cand,
            funnel_qualified_candidates=qual_cand,
            funnel_initial_episodes=init_episodes,
            funnel_paper_trades_submitted=submitted,
            funnel_paper_trades_filled=filled,
            funnel_paper_trades_partial=partial,
            funnel_paper_trades_rejected=rejected,
            funnel_realized_trades=0,
            gross_edge_distribution=gross_dist,
            net_edge_distribution=net_dist,
            quantity_distribution=qty_dist,
            episode_duration_distribution=duration_dist,
            results_by_market=market_stats,
            results_by_strategy=strategy_stats,
            sensitivity_report=sens_rep,
            data_limitations=tuple(limitations),
            final_verdict=verdict,
            representative_qualified=tuple(qualified_cases),
            representative_rejected=tuple(rejected_cases),
            funnel_raw_ingestion_records=raw_ingest,
            funnel_valid_parsed_observations=valid_obs,
        )


def validate_historical_dataset(
    dataset_or_path: Union[HistoricalDataset, str, Path],
    config: Optional[V7ValidationConfig] = None,
) -> V7ValidationReport:
    """Convenience entrypoint to run V7 validation on a dataset or file path."""
    if isinstance(dataset_or_path, HistoricalDataset):
        dataset = dataset_or_path
    else:
        dataset = HistoricalDataset.from_file(dataset_or_path)
    validator = V7HistoricalValidator(config=config)
    return validator.validate(dataset)
