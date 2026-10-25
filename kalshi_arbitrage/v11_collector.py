"""Kalshi Arbitrage Engine V11 — Sustained Market Evidence Collection & Forward Evaluation.

V11 establishes a reliable, auditable, read-only market observation system and forward
paper-evaluation pipeline:
1. Bounded, rate-limited, restartable read-only market data collection.
2. Exact timing provenance (request dispatch, RTT latency, exchange Date, arrival).
3. Structured diagnostic error events stream (preserving transport failures without
   polluting valid observation logs).
4. Order-book stability, mutation, and repeated-snapshot tracking.
5. Forward prospective evaluation through V10 10-stage funnel and V8 simulation.
6. Double-entry ledger reconciliation and risk gating for paper evaluation.
7. Cryptographic dataset manifests (SHA256 integrity checksums).
8. Comprehensive evidence quality and cadence auditing.
9. Strict output path collision protection preserving all existing datasets and user state.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)
import uuid

from .arbitrage import (
    ArbitrageOpportunity,
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    dollars_to_cents,
    evaluate_market_parity,
    evaluate_mece_markets,
)
from .async_execution import (
    AsyncMultiLegTrade,
    MicrostructureConfig,
    MultiLegLifecycleState,
    RealisticExecutionSimulator,
    UnwindPolicy,
    apply_async_trade_to_portfolio,
)
from .contract import NO, YES
from .evidence_storage import (
    EVIDENCE_SCHEMA_VERSION,
    observation_from_dict,
    observation_to_dict,
    opportunity_from_dict,
    opportunity_to_dict,
)
from .execution import (
    ExecutionPricingResult,
    derive_ask_levels,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
)
from .fee_model import FeeModelConfig, FeeScheduleType
from .ledger import LedgerEntry, PaperPortfolio, ReconciliationResult
from .market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    normalize_market,
    normalize_order_book,
)
from .market_data_provider import (
    FixtureMarketDataProvider,
    MarketDataErrorCode,
    MarketDataProvider,
    MarketDataProviderError,
    RestMarketDataProvider,
    validate_order_book_freshness,
)
from .observer import (
    DeclaredMeceBasket,
    EvaluatedOpportunity,
    MarketObservation,
    ObserverError,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_RISK,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    load_declared_baskets,
)
from .paper_risk import PaperRiskConfig, PaperRiskGate
from .portfolio import Event
from .rest_client import KalshiClientError, KalshiRestClient
from .universe import (
    LiquidUniverseSelector,
    MarketLiquidityProfile,
    UniverseFilterConfig,
)
from .v10_evaluation import (
    V10BaselineComparison,
    V10EvaluationConfig,
    V10ExecutionQualityMetrics,
    V10FinancialMetrics,
    V10OpportunityFunnel,
    _safe_pct,
)

logger = logging.getLogger(__name__)

V11_SCHEMA_VERSION = "1.1"


# ---------------------------------------------------------------------------
# Diagnostic Events and Provenance Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CollectionDiagnosticEvent:
    """Timestamped record of a transport, validation, or rate-limit failure.

    Recorded to diagnostics.jsonl to maintain transparent operational visibility
    without treating failed requests as valid market observations.
    """

    event_id: str
    timestamp: datetime
    action: str
    error_code: str
    error_message: str
    ticker: Optional[str] = None
    attempt_number: int = 1
    duration_ms: float = 0.0
    http_status: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": V11_SCHEMA_VERSION,
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "ticker": self.ticker,
            "attempt_number": self.attempt_number,
            "duration_ms": round(self.duration_ms, 2),
            "http_status": self.http_status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CollectionDiagnosticEvent":
        ts = datetime.fromisoformat(d["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return cls(
            event_id=str(d["event_id"]),
            timestamp=ts,
            action=str(d["action"]),
            error_code=str(d["error_code"]),
            error_message=str(d["error_message"]),
            ticker=d.get("ticker"),
            attempt_number=int(d.get("attempt_number", 1)),
            duration_ms=float(d.get("duration_ms", 0.0)),
            http_status=d.get("http_status"),
        )


@dataclass(frozen=True)
class V11EvidenceManifest:
    """Cryptographic session manifest and provenance metadata."""

    session_id: str
    session_name: str
    started_at: datetime
    completed_at: Optional[datetime]
    total_cycles: int
    total_requests: int
    successful_observations: int
    diagnostic_events_count: int
    unique_tickers: Tuple[str, ...]
    observations_sha256: Optional[str] = None
    diagnostics_sha256: Optional[str] = None
    opportunities_sha256: Optional[str] = None
    manifest_sha256: Optional[str] = None
    basket_evaluations_count: int = 0
    collector_config: Dict[str, Any] = field(default_factory=dict)
    system_environment: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": V11_SCHEMA_VERSION,
            "session_id": self.session_id,
            "session_name": self.session_name,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "total_cycles": self.total_cycles,
            "total_requests": self.total_requests,
            "successful_observations": self.successful_observations,
            "diagnostic_events_count": self.diagnostic_events_count,
            "unique_tickers": list(self.unique_tickers),
            "observations_sha256": self.observations_sha256,
            "diagnostics_sha256": self.diagnostics_sha256,
            "opportunities_sha256": self.opportunities_sha256,
            "manifest_sha256": self.manifest_sha256,
            "basket_evaluations_count": self.basket_evaluations_count,
            "collector_config": self.collector_config,
            "system_environment": self.system_environment,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "V11EvidenceManifest":
        s_ts = datetime.fromisoformat(d["started_at"])
        if s_ts.tzinfo is None:
            s_ts = s_ts.replace(tzinfo=timezone.utc)
        c_ts = None
        if d.get("completed_at"):
            c_ts = datetime.fromisoformat(d["completed_at"])
            if c_ts.tzinfo is None:
                c_ts = c_ts.replace(tzinfo=timezone.utc)

        return cls(
            session_id=str(d["session_id"]),
            session_name=str(d["session_name"]),
            started_at=s_ts,
            completed_at=c_ts,
            total_cycles=int(d.get("total_cycles", 0)),
            total_requests=int(d.get("total_requests", 0)),
            successful_observations=int(d.get("successful_observations", 0)),
            diagnostic_events_count=int(d.get("diagnostic_events_count", 0)),
            unique_tickers=tuple(d.get("unique_tickers", ())),
            observations_sha256=d.get("observations_sha256"),
            diagnostics_sha256=d.get("diagnostics_sha256"),
            opportunities_sha256=d.get("opportunities_sha256"),
            manifest_sha256=d.get("manifest_sha256"),
            basket_evaluations_count=int(d.get("basket_evaluations_count", 0)),
            collector_config=dict(d.get("collector_config", {})),
            system_environment=dict(d.get("system_environment", {})),
        )


def _compute_file_sha256(path: Path) -> Optional[str]:
    """Compute exact SHA256 checksum of a file if it exists."""
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Collector Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V11CollectorConfig:
    """Configuration for reliable, bounded evidence collection & forward evaluation."""

    session_name: str = "v11_collection"
    output_dir: Union[str, Path] = "evidence_v11"
    target_tickers: Optional[Tuple[str, ...]] = None
    declared_baskets: Tuple[DeclaredMeceBasket, ...] = ()
    max_leg_timestamp_skew_seconds: float = 5.0
    max_markets_per_cycle: int = 20
    poll_interval_seconds: float = 2.0
    min_request_interval_seconds: float = 0.10  # Max 10 requests/s rate limit
    max_stale_seconds: float = 60.0
    max_cycles: Optional[int] = None
    max_duration_seconds: Optional[float] = None
    max_observations: Optional[int] = None
    max_retries: int = 2
    backoff_factor: float = 0.5
    prevent_overwrite: bool = True
    resume: bool = False
    enable_prospective_evaluation: bool = False
    fee_config: FeeModelConfig = field(default_factory=FeeModelConfig)
    risk_config: PaperRiskConfig = field(default_factory=PaperRiskConfig)
    initial_cash_cents: int = 1_000_000  # $10,000.00
    target_quantity: int = 1

    def __post_init__(self) -> None:
        if self.poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if self.min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must be non-negative")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if self.max_leg_timestamp_skew_seconds <= 0:
            raise ValueError("max_leg_timestamp_skew_seconds must be positive")
        if not isinstance(self.declared_baskets, tuple):
            raise ValueError("declared_baskets must be a tuple")
        for b in self.declared_baskets:
            if not isinstance(b, DeclaredMeceBasket):
                raise ValueError("all items in declared_baskets must be DeclaredMeceBasket instances")
        if self.max_cycles is not None and self.max_cycles <= 0:
            raise ValueError("max_cycles must be positive if set")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive if set")
        if self.max_observations is not None and self.max_observations <= 0:
            raise ValueError("max_observations must be positive if set")
        if self.max_markets_per_cycle <= 0:
            raise ValueError("max_markets_per_cycle must be positive")


# ---------------------------------------------------------------------------
# Evidence Quality and Cadence Auditor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class V11EvidenceQualityMetrics:
    """Exhaustive quality and coverage metrics for recorded evidence."""

    total_collection_attempts: int
    successful_requests: int
    failed_requests: int
    valid_observations: int
    invalid_or_stale_observations: int
    unique_markets_count: int
    unique_events_count: int
    observations_per_market: Dict[str, int]
    repeated_identical_books_count: int
    mutated_books_count: int
    empty_books_count: int
    populated_books_count: int
    both_sides_depth_count: int
    one_side_depth_count: int
    collection_start: Optional[datetime]
    collection_end: Optional[datetime]
    duration_seconds: float
    mean_sampling_cadence_seconds: float
    median_sampling_cadence_seconds: float
    cadence_gaps_count: int
    max_gap_seconds: float
    duplicate_observation_ids: int
    out_of_order_timestamps: int
    diagnostic_events_count: int
    failures_by_error_code: Dict[str, int]
    meets_minimum_research_requirements: bool
    requirements_gap_reasons: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_collection_attempts": self.total_collection_attempts,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "valid_observations": self.valid_observations,
            "invalid_or_stale_observations": self.invalid_or_stale_observations,
            "unique_markets_count": self.unique_markets_count,
            "unique_events_count": self.unique_events_count,
            "observations_per_market": self.observations_per_market,
            "repeated_identical_books_count": self.repeated_identical_books_count,
            "mutated_books_count": self.mutated_books_count,
            "empty_books_count": self.empty_books_count,
            "populated_books_count": self.populated_books_count,
            "both_sides_depth_count": self.both_sides_depth_count,
            "one_side_depth_count": self.one_side_depth_count,
            "collection_start": self.collection_start.isoformat() if self.collection_start else None,
            "collection_end": self.collection_end.isoformat() if self.collection_end else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "mean_sampling_cadence_seconds": round(self.mean_sampling_cadence_seconds, 2),
            "median_sampling_cadence_seconds": round(self.median_sampling_cadence_seconds, 2),
            "cadence_gaps_count": self.cadence_gaps_count,
            "max_gap_seconds": round(self.max_gap_seconds, 2),
            "duplicate_observation_ids": self.duplicate_observation_ids,
            "out_of_order_timestamps": self.out_of_order_timestamps,
            "diagnostic_events_count": self.diagnostic_events_count,
            "failures_by_error_code": self.failures_by_error_code,
            "meets_minimum_research_requirements": self.meets_minimum_research_requirements,
            "requirements_gap_reasons": list(self.requirements_gap_reasons),
        }


class V11EvidenceQualityAuditor:
    """Audits recorded observations and diagnostics to report exact data quality."""

    MIN_UNIQUE_MARKETS: int = 5
    MIN_TOTAL_OBSERVATIONS: int = 100
    MIN_DURATION_SECONDS: float = 600.0  # 10 minutes minimum
    MIN_POPULATED_BOOKS: int = 20

    @classmethod
    def audit(
        cls,
        observations: Sequence[MarketObservation],
        diagnostics: Sequence[CollectionDiagnosticEvent] = (),
        expected_cadence_seconds: float = 2.0,
    ) -> V11EvidenceQualityMetrics:
        """Audit observations and diagnostics without mutation."""
        obs_list = list(observations)
        total_obs = len(obs_list)
        diag_list = list(diagnostics)

        seen_ids: Set[str] = set()
        duplicate_ids = 0
        out_of_order = 0
        valid_count = 0
        stale_invalid = 0
        empty_books = 0
        populated_books = 0
        both_sides = 0
        one_side = 0

        obs_per_market: Dict[str, int] = {}
        events_set: Set[str] = set()
        last_book_fingerprint: Dict[str, str] = {}
        repeated_identical = 0
        mutated_books = 0

        timestamps: List[datetime] = []
        last_ts: Optional[datetime] = None

        for obs in obs_list:
            # Duplicate ID check
            if obs.observation_id in seen_ids:
                duplicate_ids += 1
            else:
                seen_ids.add(obs.observation_id)

            t = obs.observed_at
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            timestamps.append(t)

            if last_ts is not None and t < last_ts:
                out_of_order += 1
            last_ts = t

            if obs.is_success and not obs.is_stale:
                valid_count += 1
            else:
                stale_invalid += 1

            ticker = obs.ticker
            obs_per_market[ticker] = obs_per_market.get(ticker, 0) + 1

            if obs.market and obs.market.event_ticker:
                events_set.add(obs.market.event_ticker)

            book = obs.order_book
            if book is None or (not book.yes_bids and not book.no_bids):
                empty_books += 1
            else:
                populated_books += 1
                if book.yes_bids and book.no_bids:
                    both_sides += 1
                else:
                    one_side += 1

                # Stability / mutation check
                fp = f"Y:{[(str(l.price_dollars), str(l.quantity)) for l in book.yes_bids]}|N:{[(str(l.price_dollars), str(l.quantity)) for l in book.no_bids]}"
                if ticker in last_book_fingerprint:
                    if last_book_fingerprint[ticker] == fp:
                        repeated_identical += 1
                    else:
                        mutated_books += 1
                last_book_fingerprint[ticker] = fp

        # Temporal cadence and gaps
        timestamps.sort()
        start_ts = timestamps[0] if timestamps else None
        end_ts = timestamps[-1] if timestamps else None
        duration_s = (end_ts - start_ts).total_seconds() if start_ts and end_ts else 0.0

        intervals: List[float] = []
        gaps_count = 0
        max_gap = 0.0
        gap_threshold = max(5.0, expected_cadence_seconds * 2.5)

        for i in range(1, len(timestamps)):
            delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
            intervals.append(delta)
            if delta > gap_threshold:
                gaps_count += 1
            if delta > max_gap:
                max_gap = delta

        mean_cadence = sum(intervals) / len(intervals) if intervals else 0.0
        intervals.sort()
        median_cadence = intervals[len(intervals) // 2] if intervals else 0.0

        # Diagnostics breakdown
        failures_by_code: Dict[str, int] = {}
        for d in diag_list:
            failures_by_code[d.error_code] = failures_by_code.get(d.error_code, 0) + 1

        total_attempts = total_obs + len(diag_list)
        successful_reqs = valid_count
        failed_reqs = len(diag_list) + stale_invalid

        # Assess minimum research requirements
        reasons: List[str] = []
        if len(obs_per_market) < cls.MIN_UNIQUE_MARKETS:
            reasons.append(
                f"Insufficient market breadth: {len(obs_per_market)} unique markets (< {cls.MIN_UNIQUE_MARKETS} min)"
            )
        if total_obs < cls.MIN_TOTAL_OBSERVATIONS:
            reasons.append(
                f"Insufficient observation volume: {total_obs} snapshots (< {cls.MIN_TOTAL_OBSERVATIONS} min)"
            )
        if duration_s < cls.MIN_DURATION_SECONDS:
            reasons.append(
                f"Insufficient time coverage: {duration_s:.1f}s (< {cls.MIN_DURATION_SECONDS:.1f}s min)"
            )
        if populated_books < cls.MIN_POPULATED_BOOKS:
            reasons.append(
                f"Insufficient populated order books: {populated_books} (< {cls.MIN_POPULATED_BOOKS} min)"
            )

        meets_reqs = len(reasons) == 0

        return V11EvidenceQualityMetrics(
            total_collection_attempts=total_attempts,
            successful_requests=successful_reqs,
            failed_requests=failed_reqs,
            valid_observations=valid_count,
            invalid_or_stale_observations=stale_invalid,
            unique_markets_count=len(obs_per_market),
            unique_events_count=len(events_set),
            observations_per_market=obs_per_market,
            repeated_identical_books_count=repeated_identical,
            mutated_books_count=mutated_books,
            empty_books_count=empty_books,
            populated_books_count=populated_books,
            both_sides_depth_count=both_sides,
            one_side_depth_count=one_side,
            collection_start=start_ts,
            collection_end=end_ts,
            duration_seconds=duration_s,
            mean_sampling_cadence_seconds=mean_cadence,
            median_sampling_cadence_seconds=median_cadence,
            cadence_gaps_count=gaps_count,
            max_gap_seconds=max_gap,
            duplicate_observation_ids=duplicate_ids,
            out_of_order_timestamps=out_of_order,
            diagnostic_events_count=len(diag_list),
            failures_by_error_code=failures_by_code,
            meets_minimum_research_requirements=meets_reqs,
            requirements_gap_reasons=tuple(reasons),
        )


# ---------------------------------------------------------------------------
# Prospective Forward Evaluator
# ---------------------------------------------------------------------------

class V11ProspectiveEvaluator:
    """Evaluates incoming market observations in real-time or streaming replay.

    Enforces:
    - V10 10-stage fail-closed opportunity funnel.
    - Official CFTC quadratic taker fee modeling.
    - V8 realistic asynchronous multi-leg execution simulation.
    - Pre-trade risk approval and reservation.
    - Double-entry paper ledger cash conservation invariant.
    - Strict observation deduplication to prevent double-counting financial effects.
    """

    def __init__(
        self,
        config: V11CollectorConfig,
        universe_selector: Optional[LiquidUniverseSelector] = None,
        risk_gate: Optional[PaperRiskGate] = None,
    ) -> None:
        self.config = config
        self.universe_selector = universe_selector or LiquidUniverseSelector(
            config=UniverseFilterConfig(
                min_volume_24h=Decimal("10.00"),
                min_open_interest=Decimal("10.00"),
                max_spread_cents=10,
                min_bid_depth_contracts=1,
            )
        )
        self.risk_gate = risk_gate or PaperRiskGate(config=config.risk_config)
        self.portfolio = PaperPortfolio(initial_cash_cents=config.initial_cash_cents)
        self.simulator = RealisticExecutionSimulator()

        # Deduplication cache: observation_id -> evaluated
        self._processed_obs_ids: Set[str] = set()
        self._evaluated_opportunities: List[EvaluatedOpportunity] = []
        self._trades: List[AsyncMultiLegTrade] = []

        # Funnel counters
        self.observations_ingested = 0
        self.valid_snapshots = 0
        self.markets_evaluated = 0
        self.candidates_detected = 0
        self.rej_no_edge = 0
        self.rej_unprofitable_fees = 0
        self.rej_insufficient_depth = 0
        self.rej_illiquid = 0
        self.rej_stale = 0
        self.rej_risk = 0
        self.approved_for_execution = 0
        self.execution_attempts = 0
        self.fully_filled = 0
        self.partially_filled = 0
        self.legged_out = 0
        self.completed_arbs = 0

        # Basket funnel counters
        self.baskets_evaluated = 0
        self.basket_candidates_detected = 0
        self.basket_rej_missing_market = 0
        self.basket_rej_stale = 0
        self.basket_rej_leg_skew = 0
        self.basket_rej_insufficient_depth = 0
        self.basket_rej_no_gross_edge = 0
        self.basket_rej_unprofitable_fees = 0
        self.basket_rej_risk = 0
        self.basket_qualified = 0

    def process_observation(self, obs: MarketObservation) -> Optional[EvaluatedOpportunity]:
        """Prospective evaluation step for a single incoming observation."""
        if obs.observation_id in self._processed_obs_ids:
            logger.debug(f"Skipping duplicate observation {obs.observation_id}")
            return None
        self._processed_obs_ids.add(obs.observation_id)

        self.observations_ingested += 1

        if not obs.is_success or obs.is_stale or obs.market is None or obs.order_book is None:
            return None

        self.valid_snapshots += 1
        self.markets_evaluated += 1

        market = obs.market
        book = obs.order_book
        obs_time = obs.observed_at

        # 1. Freshness check
        age_s = (obs_time - book.source_timestamp).total_seconds() if book.source_timestamp else 0.0
        if age_s > self.config.max_stale_seconds:
            self.rej_stale += 1
            return None

        # 2. Binary parity candidate detection
        parity_opp = evaluate_market_parity(market, quantity=self.config.target_quantity)
        if not parity_opp.is_arbitrage:
            self.rej_no_edge += 1
            return None

        self.candidates_detected += 1

        # 3. Liquid universe profile
        profile = self.universe_selector.profile_market(market, book)
        if not profile.is_liquid:
            self.rej_illiquid += 1
            return None

        # 4. Depth & CFTC quadratic fee execution pricing
        exec_pricing = evaluate_binary_parity_execution(
            book,
            requested_quantity=self.config.target_quantity,
            fee_model=self.config.fee_config,
            series_ticker=obs.ticker,
        )

        if exec_pricing.supported_quantity <= 0:
            self.rej_insufficient_depth += 1
            return None

        if not exec_pricing.is_net_profitable:
            self.rej_unprofitable_fees += 1
            return None

        # 5. Pre-trade risk approval
        total_req_cost = exec_pricing.total_cost_cents + (exec_pricing.estimated_fees_cents or 0)
        if total_req_cost > self.portfolio.available_cash_cents:
            self.rej_risk += 1
            return None
        if total_req_cost > self.config.risk_config.max_cost_per_trade_cents:
            self.rej_risk += 1
            return None

        self.approved_for_execution += 1

        # 6. Simulated execution via V8 simulator
        self.execution_attempts += 1
        yes_cid = f"{obs.ticker}:YES"
        no_cid = f"{obs.ticker}:NO"
        legs_specs = [
            (yes_cid, obs.ticker, YES),
            (no_cid, obs.ticker, NO),
        ]

        yes_asks = derive_ask_levels(book, YES)
        no_asks = derive_ask_levels(book, NO)
        depth_by_leg = {
            yes_cid: [(lvl.price_cents, int(lvl.quantity)) for lvl in yes_asks],
            no_cid: [(lvl.price_cents, int(lvl.quantity)) for lvl in no_asks],
        }
        bids_by_leg = {
            yes_cid: [(dollars_to_cents(lvl.price_dollars), int(lvl.quantity)) for lvl in book.yes_bids],
            no_cid: [(dollars_to_cents(lvl.price_dollars), int(lvl.quantity)) for lvl in book.no_bids],
        }

        trade = self.simulator.simulate_execution(
            trade_id=f"v11-trade-{obs.observation_id}",
            strategy_type=OPPORTUNITY_BINARY_PARITY,
            target_quantity=self.config.target_quantity,
            legs_specs=legs_specs,
            available_depth_by_leg=depth_by_leg,
            counterparty_bids_by_leg=bids_by_leg,
            start_time=obs_time,
        )

        # 7. Commit to portfolio & double-entry ledger
        apply_async_trade_to_portfolio(
            self.portfolio, trade, timestamp=obs_time.isoformat()
        )
        self._trades.append(trade)

        if trade.state == MultiLegLifecycleState.FILLED:
            self.fully_filled += 1
            self.completed_arbs += 1
        elif trade.state == MultiLegLifecycleState.PARTIALLY_FILLED:
            self.partially_filled += 1
        elif trade.state in (
            MultiLegLifecycleState.LEGGED_OUT,
            MultiLegLifecycleState.UNWINDING,
            MultiLegLifecycleState.CLOSED,
        ):
            self.legged_out += 1

        # 8. Reconcile ledger
        rec = self.portfolio.reconcile()
        if not rec.is_reconciled:
            err = f"Accounting invariant broken in prospective evaluation: {rec.discrepancies}"
            logger.critical(err)
            raise RuntimeError(err)

        eval_opp = EvaluatedOpportunity(
            observation_id=obs.observation_id,
            opportunity_id=f"v11-opp-{obs.observation_id}",
            strategy_type=OPPORTUNITY_BINARY_PARITY,
            observed_at=obs_time,
            source_timestamps={obs.ticker: book.source_timestamp or obs_time},
            leg_timestamp_skew_seconds=0.0,
            market_tickers=(obs.ticker,),
            candidate_opportunity=parity_opp,
            pricing_result=exec_pricing,
            status=STATUS_QUALIFIED,
            rejection_reason=None,
            fee_per_contract_cents=exec_pricing.estimated_fees_cents,
            is_qualified=True,
            is_recurrent=False,
            paper_trade_id=trade.trade_id,
        )
        self._evaluated_opportunities.append(eval_opp)
        return eval_opp

    def process_basket(
        self,
        basket: DeclaredMeceBasket,
        observations: Mapping[str, MarketObservation],
        cycle_timestamp: Optional[datetime] = None,
    ) -> Optional[EvaluatedOpportunity]:
        """Prospective evaluation step for an explicitly declared MECE event basket."""
        now = cycle_timestamp or datetime.now(timezone.utc)
        self.baskets_evaluated += 1
        opp_id = f"v11-mece-{basket.event_ticker}-{int(now.timestamp())}-{uuid.uuid4().hex[:6]}"
        strat_type = (
            OPPORTUNITY_MECE_BASKET_LONG_YES
            if basket.basket_side == YES
            else OPPORTUNITY_MECE_BASKET_LONG_NO
        )

        # 1. Completeness check: All declared market tickers must be present in cycle observations
        missing_tickers = [t for t in basket.ordered_tickers if t not in observations]
        if missing_tickers:
            self.basket_rej_missing_market += 1
            eval_opp = EvaluatedOpportunity(
                observation_id=f"basket-{basket.event_ticker}-{int(now.timestamp())}",
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps={t: observations[t].source_timestamp for t in basket.ordered_tickers if t in observations},
                leg_timestamp_skew_seconds=0.0,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=None,
                pricing_result=None,
                status=STATUS_REJECTED_MISSING_MARKET,
                rejection_reason=f"Incomplete cycle snapshot: missing leg(s) {missing_tickers}",
                fee_per_contract_cents=None,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        basket_obs = [observations[t] for t in basket.ordered_tickers]
        failed_obs = [o for o in basket_obs if not o.is_success or o.market is None or o.order_book is None]
        if failed_obs:
            self.basket_rej_missing_market += 1
            eval_opp = EvaluatedOpportunity(
                observation_id=",".join(o.observation_id for o in basket_obs),
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps={o.ticker: o.source_timestamp for o in basket_obs},
                leg_timestamp_skew_seconds=0.0,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=None,
                pricing_result=None,
                status=STATUS_REJECTED_MISSING_MARKET,
                rejection_reason=f"Failed observation on leg(s): {[o.ticker for o in failed_obs]}",
                fee_per_contract_cents=None,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        source_ts_map = {o.ticker: o.source_timestamp for o in basket_obs}

        # 2. Freshness check: None of the legs may be marked stale
        stale_legs = [o for o in basket_obs if o.is_stale]
        if stale_legs:
            self.basket_rej_stale += 1
            eval_opp = EvaluatedOpportunity(
                observation_id=",".join(o.observation_id for o in basket_obs),
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=0.0,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=None,
                pricing_result=None,
                status=STATUS_REJECTED_STALE,
                rejection_reason=f"Stale leg(s): {'; '.join(f'{o.ticker}: {o.staleness_reason}' for o in stale_legs)}",
                fee_per_contract_cents=None,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        # 3. Timestamp skew check
        missing_ts_legs = [o.ticker for o in basket_obs if o.source_timestamp is None]
        if missing_ts_legs:
            self.basket_rej_stale += 1
            eval_opp = EvaluatedOpportunity(
                observation_id=",".join(o.observation_id for o in basket_obs),
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=0.0,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=None,
                pricing_result=None,
                status=STATUS_REJECTED_MISSING_TIMESTAMP,
                rejection_reason=f"Legs missing source_timestamp: {missing_ts_legs}",
                fee_per_contract_cents=None,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        ts_values = [o.source_timestamp.timestamp() for o in basket_obs if o.source_timestamp]
        skew_seconds = max(ts_values) - min(ts_values)
        if skew_seconds > self.config.max_leg_timestamp_skew_seconds:
            self.basket_rej_leg_skew += 1
            eval_opp = EvaluatedOpportunity(
                observation_id=",".join(o.observation_id for o in basket_obs),
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=skew_seconds,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=None,
                pricing_result=None,
                status=STATUS_REJECTED_LEG_SKEW,
                rejection_reason=f"Excessive leg timestamp skew: {skew_seconds:.2f}s > max {self.config.max_leg_timestamp_skew_seconds:.2f}s",
                fee_per_contract_cents=None,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        # 4. Construct Event and candidate detection (V2)
        event = Event(
            identifier=basket.event_ticker,
            outcomes=basket.outcomes,
            relationship_established=True,
        )
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
            logger.debug(f"Candidate evaluation failed for basket {basket.event_ticker}: {exc}")
            return None

        # 5. V3 Depth traversal and fee-adjusted execution pricing
        pricing = evaluate_mece_basket_execution(
            event=event,
            books_by_outcome=books_by_outcome,
            basket_side=basket.basket_side,
            requested_quantity=self.config.target_quantity,
            fee_model=self.config.fee_config,
            series_ticker=basket.event_ticker,
        )

        obs_id_combined = ",".join(o.observation_id for o in basket_obs)

        # 6. Qualification & Rejection classification
        if pricing.supported_quantity <= 0:
            self.basket_rej_insufficient_depth += 1
            status = STATUS_REJECTED_INSUFFICIENT_DEPTH
            reason = pricing.rejection_reason or "Order books have insufficient depth for common basket execution"
            eval_opp = EvaluatedOpportunity(
                observation_id=obs_id_combined,
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=skew_seconds,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=candidate,
                pricing_result=pricing,
                status=status,
                rejection_reason=reason,
                fee_per_contract_cents=pricing.estimated_fees_cents,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        if not pricing.is_gross_profitable:
            self.basket_rej_no_gross_edge += 1
            status = STATUS_REJECTED_NO_GROSS_EDGE
            reason = pricing.rejection_reason or f"No gross edge: total cost ({pricing.total_cost_cents}¢) exceeds guaranteed payout ({pricing.guaranteed_payout_cents}¢)"
            eval_opp = EvaluatedOpportunity(
                observation_id=obs_id_combined,
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=skew_seconds,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=candidate,
                pricing_result=pricing,
                status=status,
                rejection_reason=reason,
                fee_per_contract_cents=pricing.estimated_fees_cents,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        if pricing.is_net_profitable is False:
            self.basket_rej_unprofitable_fees += 1
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            reason = pricing.rejection_reason or f"Unprofitable after estimated fees: net edge {pricing.net_profit_cents}¢"
            eval_opp = EvaluatedOpportunity(
                observation_id=obs_id_combined,
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=skew_seconds,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=candidate,
                pricing_result=pricing,
                status=status,
                rejection_reason=reason,
                fee_per_contract_cents=pricing.estimated_fees_cents,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        # 7. Candidate is gross and net profitable! Pre-trade risk check
        self.basket_candidates_detected += 1
        total_req_cost = pricing.total_cost_cents + (pricing.estimated_fees_cents or 0)
        if (
            total_req_cost > self.portfolio.available_cash_cents
            or total_req_cost > self.config.risk_config.max_cost_per_trade_cents
        ):
            self.basket_rej_risk += 1
            eval_opp = EvaluatedOpportunity(
                observation_id=obs_id_combined,
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=skew_seconds,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=candidate,
                pricing_result=pricing,
                status=STATUS_REJECTED_RISK,
                rejection_reason="Pre-trade risk limit or available cash reservation exceeded",
                fee_per_contract_cents=pricing.estimated_fees_cents,
                is_qualified=False,
            )
            self._evaluated_opportunities.append(eval_opp)
            return eval_opp

        # 8. Simulate execution
        self.approved_for_execution += 1
        self.execution_attempts += 1
        legs_specs = [
            (f"{t}:{basket.basket_side}", t, basket.basket_side)
            for t in basket.ordered_tickers
        ]
        depth_by_leg = {}
        bids_by_leg = {}
        for t in basket.ordered_tickers:
            book = observations[t].order_book
            asks = derive_ask_levels(book, basket.basket_side)
            cid = f"{t}:{basket.basket_side}"
            depth_by_leg[cid] = [(lvl.price_cents, int(lvl.quantity)) for lvl in asks]
            bids = book.yes_bids if basket.basket_side == YES else book.no_bids
            bids_by_leg[cid] = [(dollars_to_cents(lvl.price_dollars), int(lvl.quantity)) for lvl in bids]

        trade = self.simulator.simulate_execution(
            trade_id=f"v11-trade-basket-{basket.event_ticker}-{int(now.timestamp())}",
            strategy_type=strat_type,
            target_quantity=pricing.supported_quantity,
            legs_specs=legs_specs,
            available_depth_by_leg=depth_by_leg,
            counterparty_bids_by_leg=bids_by_leg,
            start_time=now,
        )

        apply_async_trade_to_portfolio(self.portfolio, trade, timestamp=now.isoformat())
        self._trades.append(trade)

        if trade.state == MultiLegLifecycleState.FILLED:
            self.fully_filled += 1
            self.completed_arbs += 1
        elif trade.state == MultiLegLifecycleState.PARTIALLY_FILLED:
            self.partially_filled += 1
        elif trade.state in (
            MultiLegLifecycleState.LEGGED_OUT,
            MultiLegLifecycleState.UNWINDING,
            MultiLegLifecycleState.CLOSED,
        ):
            self.legged_out += 1

        rec = self.portfolio.reconcile()
        if not rec.is_reconciled:
            err = f"Accounting invariant broken in prospective basket evaluation: {rec.discrepancies}"
            logger.critical(err)
            raise RuntimeError(err)

        self.basket_qualified += 1
        eval_opp = EvaluatedOpportunity(
            observation_id=obs_id_combined,
            opportunity_id=opp_id,
            strategy_type=strat_type,
            observed_at=now,
            source_timestamps=source_ts_map,
            leg_timestamp_skew_seconds=skew_seconds,
            market_tickers=basket.ordered_tickers,
            candidate_opportunity=candidate,
            pricing_result=pricing,
            status=STATUS_QUALIFIED,
            rejection_reason=None,
            fee_per_contract_cents=pricing.estimated_fees_cents,
            is_qualified=True,
            is_recurrent=False,
            paper_trade_id=trade.trade_id,
        )
        self._evaluated_opportunities.append(eval_opp)
        return eval_opp

    def get_financial_summary(self) -> Dict[str, Any]:
        """Summary of current financial metrics."""
        rec = self.portfolio.reconcile()
        total_fees = sum(t.total_fees_cents for t in self._trades)
        gross_edge = sum(t.realized_pnl_cents + t.total_fees_cents for t in self._trades if t.is_arbitrage_complete)
        net_pnl = sum(t.realized_pnl_cents for t in self._trades)

        return {
            "initial_cash_cents": self.config.initial_cash_cents,
            "ending_cash_cents": self.portfolio.available_cash_cents,
            "cost_basis_cents": self.portfolio.total_cost_basis_cents(),
            "realized_gross_edge_cents": gross_edge,
            "total_fees_cents": total_fees,
            "net_realized_pnl_cents": net_pnl,
            "trades_count": len(self._trades),
            "completed_arbs": self.completed_arbs,
            "legged_out": self.legged_out,
            "is_ledger_reconciled": rec.is_reconciled,
            "discrepancies": rec.discrepancies,
            "baskets_evaluated": self.baskets_evaluated,
            "basket_qualified": self.basket_qualified,
            "basket_rej_missing_market": self.basket_rej_missing_market,
            "basket_rej_stale": self.basket_rej_stale,
            "basket_rej_leg_skew": self.basket_rej_leg_skew,
            "basket_rej_insufficient_depth": self.basket_rej_insufficient_depth,
            "basket_rej_no_gross_edge": self.basket_rej_no_gross_edge,
            "basket_rej_unprofitable_fees": self.basket_rej_unprofitable_fees,
            "basket_rej_risk": self.basket_rej_risk,
        }


# ---------------------------------------------------------------------------
# Bounded Market Evidence Collector
# ---------------------------------------------------------------------------

class V11MarketEvidenceCollector:
    """Bounded, rate-limited, restartable read-only market evidence collector."""

    def __init__(
        self,
        config: V11CollectorConfig,
        data_provider: Optional[MarketDataProvider] = None,
    ) -> None:
        self.config = config
        self.output_dir = Path(config.output_dir)
        self.observations_path = self.output_dir / "observations.jsonl"
        self.diagnostics_path = self.output_dir / "diagnostics.jsonl"
        self.opportunities_path = self.output_dir / "opportunities.jsonl"
        self.manifest_path = self.output_dir / "manifest.json"

        self.session_id = f"v11-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.data_provider = data_provider or RestMarketDataProvider(
            max_retries=config.max_retries,
            backoff_factor=config.backoff_factor,
            max_stale_seconds=config.max_stale_seconds,
        )

        self.prospective_evaluator = (
            V11ProspectiveEvaluator(config=config)
            if config.enable_prospective_evaluation
            else None
        )

        # Runtime state
        self._stop_requested = False
        self._cycle_index = 0
        self._total_requests = 0
        self._successful_observations = 0
        self._diagnostic_events_count = 0
        self._basket_evaluations_count = 0
        self._seen_tickers: Set[str] = set()
        self._last_request_time: float = 0.0

        # Memory buffer & cache for identical book tracking
        self._last_book_fingerprints: Dict[str, str] = {}
        self._persisted_obs_ids: Set[str] = set()

        # Enforce output path collision safety
        self._init_storage()

    def _init_storage(self) -> None:
        """Initialize storage and enforce collision safety."""
        if self.output_dir.exists():
            if self.observations_path.exists() and not self.config.resume:
                if self.config.prevent_overwrite:
                    raise FileExistsError(
                        f"Refusing to overwrite existing evidence in {self.output_dir}. "
                        "Specify a unique output directory or set resume=True."
                    )
            elif self.config.resume and self.observations_path.exists():
                logger.info(f"Resuming collection in existing directory: {self.output_dir}")
                self._load_resume_state()
        else:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def _load_resume_state(self) -> None:
        """Load existing observation IDs to prevent duplicate writes upon resume."""
        with open(self.observations_path, "r", encoding="utf-8") as f:
            for line in f:
                clean = line.strip()
                if not clean:
                    continue
                try:
                    d = json.loads(clean)
                    oid = d.get("observation_id")
                    if oid:
                        self._persisted_obs_ids.add(oid)
                    tk = d.get("ticker")
                    if tk:
                        self._seen_tickers.add(tk)
                    self._successful_observations += 1
                except Exception:
                    continue

        if self.diagnostics_path.exists():
            with open(self.diagnostics_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self._diagnostic_events_count += 1

        logger.info(
            f"Resumed session with {self._successful_observations} existing observations "
            f"and {self._diagnostic_events_count} diagnostics."
        )

    def request_stop(self) -> None:
        """Signal collector to halt gracefully after the active cycle."""
        logger.info("Graceful stop requested.")
        self._stop_requested = True

    def _enforce_rate_limit(self) -> None:
        """Sleep if required to respect minimum request interval."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self.config.min_request_interval_seconds:
            time.sleep(self.config.min_request_interval_seconds - elapsed)
        self._last_request_time = time.monotonic()

    def _append_record(self, path: Path, payload_dict: Dict[str, Any]) -> None:
        """Append line atomically with fsync flush."""
        line = json.dumps(payload_dict) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def record_diagnostic(
        self,
        action: str,
        error_code: str,
        error_message: str,
        ticker: Optional[str] = None,
        duration_ms: float = 0.0,
        http_status: Optional[int] = None,
    ) -> None:
        """Durably log a diagnostic failure event."""
        diag = CollectionDiagnosticEvent(
            event_id=f"diag-{uuid.uuid4().hex[:12]}",
            timestamp=datetime.now(timezone.utc),
            action=action,
            error_code=error_code,
            error_message=error_message,
            ticker=ticker,
            duration_ms=duration_ms,
            http_status=http_status,
        )
        self._append_record(self.diagnostics_path, diag.to_dict())
        self._diagnostic_events_count += 1

    def _execute_bounded_request(
        self,
        action_name: str,
        func: Callable[..., Any],
        *args: Any,
        ticker: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute a single REST query with timing and error isolation."""
        self._enforce_rate_limit()
        self._total_requests += 1

        t0 = time.monotonic()
        try:
            res = func(*args, **kwargs)
            return res
        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000.0
            code = "UNKNOWN"
            http_status = None
            if isinstance(exc, MarketDataProviderError):
                code = exc.code.value
            elif isinstance(exc, KalshiClientError):
                http_status = exc.status_code
                code = f"HTTP_{http_status}" if http_status else "CLIENT_ERROR"
            else:
                code = type(exc).__name__

            self.record_diagnostic(
                action=action_name,
                error_code=code,
                error_message=str(exc),
                ticker=ticker,
                duration_ms=duration,
                http_status=http_status,
            )
            raise

    def run_single_cycle(self) -> int:
        """Execute one collection cycle across configured markets."""
        self._cycle_index += 1
        cycle_start = datetime.now(timezone.utc)
        cycle_obs_count = 0

        # 1. Discover or select markets
        markets: List[NormalizedMarket] = []
        tickers_to_poll = list(self.config.target_tickers or ())
        for b in self.config.declared_baskets:
            for t in b.ordered_tickers:
                if t not in tickers_to_poll:
                    tickers_to_poll.append(t)

        if tickers_to_poll:
            for t in tickers_to_poll:
                try:
                    m = self._execute_bounded_request(
                        "get_market", self.data_provider.get_market, t, ticker=t
                    )
                    markets.append(m)
                except Exception as exc:
                    logger.debug(f"Could not load market for ticker {t}: {exc}")
        else:
            try:
                discovered = self._execute_bounded_request(
                    "discover_markets",
                    self.data_provider.discover_markets,
                    limit=self.config.max_markets_per_cycle,
                )
                markets = list(discovered)[: self.config.max_markets_per_cycle]
            except Exception as exc:
                logger.warning(f"Market discovery failed in cycle {self._cycle_index}: {exc}")
                return 0

        # 2. Poll order books for each market
        cycle_observations: Dict[str, MarketObservation] = {}
        for market in markets:
            if self._stop_requested:
                break
            if (
                self.config.max_observations is not None
                and self._successful_observations >= self.config.max_observations
            ):
                break

            ticker = market.ticker
            self._seen_tickers.add(ticker)

            try:
                t_req = time.monotonic()
                t_poll = (
                    getattr(self.data_provider, "current_time", None)
                    or datetime.now(timezone.utc)
                )
                book, _ = self._execute_bounded_request(
                    "get_order_book",
                    self.data_provider.get_order_book,
                    ticker,
                    ticker=ticker,
                )
                duration_ms = (time.monotonic() - t_req) * 1000.0
                obs_time = t_poll

                # Freshness verification
                is_stale = False
                staleness_reason = None
                if book.source_timestamp:
                    age_s = (obs_time - book.source_timestamp).total_seconds()
                    if age_s > self.config.max_stale_seconds:
                        is_stale = True
                        staleness_reason = f"Data age ({age_s:.1f}s) exceeds max ({self.config.max_stale_seconds}s)"
                else:
                    is_stale = True
                    staleness_reason = "Order book has no source_timestamp"

                obs_id = f"obs-{ticker}-{int(obs_time.timestamp())}-{uuid.uuid4().hex[:6]}"
                obs = MarketObservation(
                    observation_id=obs_id,
                    ticker=ticker,
                    observed_at=obs_time,
                    source_timestamp=book.source_timestamp,
                    market=market,
                    order_book=book,
                    is_success=True,
                    is_stale=is_stale,
                    staleness_reason=staleness_reason,
                )

                # Track in cycle observations
                cycle_observations[ticker] = obs

                # Persist observation atomically
                if obs.observation_id not in self._persisted_obs_ids:
                    self._append_record(self.observations_path, observation_to_dict(obs))
                    self._persisted_obs_ids.add(obs.observation_id)
                    self._successful_observations += 1
                    cycle_obs_count += 1

                # Prospective evaluation hook for single-market parity
                if self.prospective_evaluator:
                    eval_opp = self.prospective_evaluator.process_observation(obs)
                    if eval_opp:
                        self._append_record(self.opportunities_path, opportunity_to_dict(eval_opp))

            except Exception as exc:
                logger.debug(f"Failed observation for {ticker}: {exc}")

        # 3. Prospective evaluation hook for declared MECE baskets
        if self.config.declared_baskets and self.prospective_evaluator:
            for basket in self.config.declared_baskets:
                basket_opp = self.prospective_evaluator.process_basket(
                    basket, cycle_observations, cycle_timestamp=cycle_start
                )
                if basket_opp:
                    self._append_record(self.opportunities_path, opportunity_to_dict(basket_opp))
                    self._basket_evaluations_count += 1

        return cycle_obs_count

    def run(self) -> V11EvidenceManifest:
        """Run bounded collection session until criteria or stop signal."""
        session_start = datetime.now(timezone.utc)
        logger.info(
            f"Starting V11 market evidence collection session {self.session_id} "
            f"(target dir: {self.output_dir})..."
        )

        # Signal handlers for POSIX graceful termination
        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        def _signal_handler(sig, frame):
            logger.info(f"Received shutdown signal ({sig}). Halting collection...")
            self.request_stop()

        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            pass  # Running outside main thread in some test runners

        try:
            while not self._stop_requested:
                # Check cycle limit
                if (
                    self.config.max_cycles is not None
                    and self._cycle_index >= self.config.max_cycles
                ):
                    logger.info(f"Reached configured max_cycles ({self.config.max_cycles}).")
                    break

                # Check duration limit
                now = datetime.now(timezone.utc)
                if (
                    self.config.max_duration_seconds is not None
                    and (now - session_start).total_seconds() >= self.config.max_duration_seconds
                ):
                    logger.info(
                        f"Reached max_duration_seconds ({self.config.max_duration_seconds}s)."
                    )
                    break

                # Check observation limit
                if (
                    self.config.max_observations is not None
                    and self._successful_observations >= self.config.max_observations
                ):
                    logger.info(
                        f"Reached max_observations limit ({self.config.max_observations})."
                    )
                    break

                # Execute cycle
                self.run_single_cycle()

                # Sleep interval between cycles
                if not self._stop_requested and self.config.poll_interval_seconds > 0:
                    time.sleep(self.config.poll_interval_seconds)

        finally:
            try:
                signal.signal(signal.SIGINT, prev_sigint)
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, AttributeError):
                pass

        session_end = datetime.now(timezone.utc)
        manifest = self._write_manifest(session_start, session_end)
        logger.info(
            f"V11 collection complete: {self._successful_observations} observations, "
            f"{self._diagnostic_events_count} diagnostics across {self._cycle_index} cycles."
        )
        return manifest

    def _write_manifest(self, start_time: datetime, end_time: datetime) -> V11EvidenceManifest:
        """Write cryptographic dataset manifest."""
        obs_hash = _compute_file_sha256(self.observations_path)
        diag_hash = _compute_file_sha256(self.diagnostics_path)
        opps_hash = _compute_file_sha256(self.opportunities_path)

        cfg_dict = {
            "session_name": self.config.session_name,
            "max_markets_per_cycle": self.config.max_markets_per_cycle,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "min_request_interval_seconds": self.config.min_request_interval_seconds,
            "max_stale_seconds": self.config.max_stale_seconds,
            "max_cycles": self.config.max_cycles,
            "max_duration_seconds": self.config.max_duration_seconds,
            "max_observations": self.config.max_observations,
            "enable_prospective_evaluation": self.config.enable_prospective_evaluation,
            "declared_baskets_count": len(self.config.declared_baskets),
            "max_leg_timestamp_skew_seconds": self.config.max_leg_timestamp_skew_seconds,
        }

        env_dict = {
            "python_version": sys.version.split()[0],
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
        }

        manifest = V11EvidenceManifest(
            session_id=self.session_id,
            session_name=self.config.session_name,
            started_at=start_time,
            completed_at=end_time,
            total_cycles=self._cycle_index,
            total_requests=self._total_requests,
            successful_observations=self._successful_observations,
            diagnostic_events_count=self._diagnostic_events_count,
            unique_tickers=tuple(sorted(self._seen_tickers)),
            observations_sha256=obs_hash,
            diagnostics_sha256=diag_hash,
            opportunities_sha256=opps_hash,
            manifest_sha256=None,
            basket_evaluations_count=self._basket_evaluations_count,
            collector_config=cfg_dict,
            system_environment=env_dict,
        )

        manifest_dict = manifest.to_dict()
        # Compute self-hash excluding manifest_sha256
        content_to_hash = json.dumps(manifest_dict, sort_keys=True).encode("utf-8")
        manifest_hash = hashlib.sha256(content_to_hash).hexdigest()
        manifest_dict["manifest_sha256"] = manifest_hash

        with open(self.manifest_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest_dict, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())

        return V11EvidenceManifest.from_dict(manifest_dict)
