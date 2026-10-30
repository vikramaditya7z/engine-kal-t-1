"""Continuous live arbitrage detection engine for Kalshi prediction markets.

V21 Continuous Live Arbitrage Detection:
Evaluates freshly collected market data from the V20 continuous collection infrastructure
and detects genuine, mathematically valid arbitrage candidates in real time.

Key Invariants:
1. Real-time Candidate Lifecycle: Tracks candidates through explicit states:
   NEW -> UPDATED -> DISQUALIFIED -> INVALIDATED -> DISAPPEARED.
2. Deduplication & Change Detection: Fingerprints order books to suppress redundant
   evaluations across unchanged market ticks.
3. Deterministic Arithmetic: Integer cents for all calculations; exact CFTC taker fee models.
4. Fail-Closed Validation: Missing markets, stale data (> max_stale_seconds), or
   excessive multi-leg timestamp skew (> max_leg_skew_seconds) immediately transition
   candidates to INVALIDATED.
5. In-Process & Streaming Decoupling: Evaluates via synchronous cycle callbacks or
   standalone JSONL file-tailing daemon.
6. Zero Live Trading: Purely analytical and observational; never places orders.
"""

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import (
    Any,
    Callable,
    Deque,
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

from .alert_engine import AlertConfig, AlertEngine
from .arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
)
from .contract import NO, YES
from .evidence_storage import observation_from_dict
from .execution import (
    ExecutionPricingResult,
    derive_ask_levels,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
)
from .execution_qualifier import (
    CandidateQualification,
    ExecutionAwareArbitrageQualifier,
    ExecutionQualifierConfig,
    LegExecutionDetail,
    QualificationOutcome,
)
from .fee_model import FeeModelConfig, FeeScheduleType
from .market_data import NormalizedMarket, NormalizedOrderBook
from .observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    load_declared_baskets,
)
from .portfolio import Event
from .snapshot_reconstruction import (
    EventSnapshotReconstructor,
    ReconstructedBasketSnapshot,
    SnapshotReconstructionConfig,
)

logger = logging.getLogger("continuous_evaluator")


class CandidateState(str, Enum):
    """Explicit lifecycle states for an arbitrage candidate."""

    NEW = "NEW"
    UPDATED = "UPDATED"
    DISQUALIFIED = "DISQUALIFIED"
    INVALIDATED = "INVALIDATED"
    DISAPPEARED = "DISAPPEARED"


class EvaluatorStatus(str, Enum):
    """Operational status of the continuous evaluation engine."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


def compute_book_fingerprint(book: Optional[NormalizedOrderBook]) -> str:
    """Compute deterministic cryptographic fingerprint of an order book state."""
    if book is None:
        return "NONE"
    yes_str = ";".join(f"{b.price_dollars}:{b.quantity}" for b in book.yes_bids)
    no_str = ";".join(f"{b.price_dollars}:{b.quantity}" for b in book.no_bids)
    raw = f"{book.market_ticker}|Y:{yes_str}|N:{no_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CandidateTransition:
    """Audit record for a state transition in a candidate's lifecycle."""

    transition_id: str
    candidate_id: str
    strategy_type: str
    from_state: Optional[str]
    to_state: str
    transitioned_at: str
    reason: Optional[str]
    gross_edge_cents: Optional[int]
    net_edge_cents: Optional[int]
    supported_quantity: int
    market_tickers: Tuple[str, ...]
    qualification_outcome: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "candidate_id": self.candidate_id,
            "strategy_type": self.strategy_type,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "transitioned_at": self.transitioned_at,
            "reason": self.reason,
            "gross_edge_cents": self.gross_edge_cents,
            "net_edge_cents": self.net_edge_cents,
            "supported_quantity": self.supported_quantity,
            "market_tickers": list(self.market_tickers),
            "qualification_outcome": self.qualification_outcome,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CandidateTransition":
        return cls(
            transition_id=str(d["transition_id"]),
            candidate_id=str(d["candidate_id"]),
            strategy_type=str(d["strategy_type"]),
            from_state=d.get("from_state"),
            to_state=str(d["to_state"]),
            transitioned_at=str(d["transitioned_at"]),
            reason=d.get("reason"),
            gross_edge_cents=d.get("gross_edge_cents"),
            net_edge_cents=d.get("net_edge_cents"),
            supported_quantity=int(d.get("supported_quantity", 0)),
            market_tickers=tuple(d.get("market_tickers", ())),
            qualification_outcome=d.get("qualification_outcome"),
        )


@dataclass
class ArbitrageCandidate:
    """An arbitrage opportunity actively tracked and managed across evaluation cycles."""

    candidate_id: str
    strategy_type: str
    event_ticker: Optional[str]
    market_tickers: Tuple[str, ...]
    state: CandidateState
    detected_at: str
    updated_at: str
    source_timestamps: Dict[str, Optional[str]]
    observation_ids: Tuple[str, ...]
    gross_edge_cents: int
    estimated_fees_cents: Optional[int]
    net_edge_cents: Optional[int]
    guaranteed_payout_cents: int
    total_cost_cents: int
    target_quantity: int
    supported_quantity: int
    is_mathematically_valid: bool
    is_depth_supported: bool
    is_net_profitable: bool
    is_qualified: bool
    status: str
    rejection_reason: Optional[str]
    market_state_fingerprint: str
    per_leg_prices_cents: Dict[str, Optional[int]]
    transitions: List[Dict[str, Any]] = field(default_factory=list)
    qualification: Optional[CandidateQualification] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "strategy_type": self.strategy_type,
            "event_ticker": self.event_ticker,
            "market_tickers": list(self.market_tickers),
            "state": self.state.value if isinstance(self.state, CandidateState) else str(self.state),
            "detected_at": self.detected_at,
            "updated_at": self.updated_at,
            "source_timestamps": dict(self.source_timestamps),
            "observation_ids": list(self.observation_ids),
            "gross_edge_cents": self.gross_edge_cents,
            "estimated_fees_cents": self.estimated_fees_cents,
            "net_edge_cents": self.net_edge_cents,
            "guaranteed_payout_cents": self.guaranteed_payout_cents,
            "total_cost_cents": self.total_cost_cents,
            "target_quantity": self.target_quantity,
            "supported_quantity": self.supported_quantity,
            "is_mathematically_valid": self.is_mathematically_valid,
            "is_depth_supported": self.is_depth_supported,
            "is_net_profitable": self.is_net_profitable,
            "is_qualified": self.is_qualified,
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "market_state_fingerprint": self.market_state_fingerprint,
            "per_leg_prices_cents": dict(self.per_leg_prices_cents),
            "transitions_count": len(self.transitions),
            "qualification": self.qualification.to_dict() if self.qualification else None,
            "gross_edge_per_unit_cents": (
                str(self.qualification.gross_edge_per_unit_cents)
                if self.qualification
                else None
            ),
            "net_edge_per_unit_cents": (
                str(self.qualification.net_edge_per_unit_cents)
                if self.qualification
                else None
            ),
            "return_on_capital_pct": (
                str(self.qualification.return_on_capital_pct)
                if (self.qualification and self.qualification.return_on_capital_pct is not None)
                else None
            ),
            "fees_per_leg_cents": (
                dict(self.qualification.fees_per_leg_cents)
                if self.qualification
                else {}
            ),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ArbitrageCandidate":
        state_val = d["state"]
        state = CandidateState(state_val) if state_val in CandidateState._value2member_map_ else CandidateState.UPDATED
        return cls(
            candidate_id=str(d["candidate_id"]),
            strategy_type=str(d["strategy_type"]),
            event_ticker=d.get("event_ticker"),
            market_tickers=tuple(d.get("market_tickers", ())),
            state=state,
            detected_at=str(d["detected_at"]),
            updated_at=str(d["updated_at"]),
            source_timestamps=dict(d.get("source_timestamps", {})),
            observation_ids=tuple(d.get("observation_ids", ())),
            gross_edge_cents=int(d["gross_edge_cents"]),
            estimated_fees_cents=d.get("estimated_fees_cents"),
            net_edge_cents=d.get("net_edge_cents"),
            guaranteed_payout_cents=int(d["guaranteed_payout_cents"]),
            total_cost_cents=int(d["total_cost_cents"]),
            target_quantity=int(d["target_quantity"]),
            supported_quantity=int(d["supported_quantity"]),
            is_mathematically_valid=bool(d["is_mathematically_valid"]),
            is_depth_supported=bool(d["is_depth_supported"]),
            is_net_profitable=bool(d["is_net_profitable"]),
            is_qualified=bool(d["is_qualified"]),
            status=str(d["status"]),
            rejection_reason=d.get("rejection_reason"),
            market_state_fingerprint=str(d["market_state_fingerprint"]),
            per_leg_prices_cents=dict(d.get("per_leg_prices_cents", {})),
            transitions=list(d.get("transitions", [])),
            qualification=(
                CandidateQualification.from_dict(d["qualification"])
                if d.get("qualification") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ContinuousEvaluatorConfig:
    """Configuration options for continuous arbitrage evaluation."""

    poll_interval_seconds: float = 1.0
    max_stale_seconds: float = 60.0
    max_leg_skew_seconds: float = 5.0
    target_quantity: int = 1
    fee_schedule: FeeScheduleType = FeeScheduleType.KALSHI_OFFICIAL_TAKER
    baskets: Optional[Tuple[DeclaredMeceBasket, ...]] = None
    baskets_path: Optional[Path] = None
    observations_file: Optional[Path] = None
    state_file: Optional[Path] = None
    history_file: Optional[Path] = None
    session_id: Optional[str] = None
    max_cycles: Optional[int] = None
    disappear_timeout_seconds: float = 180.0
    max_history_records: int = 500
    require_full_quantity: bool = True
    min_net_profit_cents: int = 1
    require_source_timestamp: bool = True
    alerts_enabled: bool = True
    alert_config: Optional[AlertConfig] = None
    alert_state_file: Optional[Path] = None
    alert_history_file: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if self.max_leg_skew_seconds <= 0:
            raise ValueError("max_leg_skew_seconds must be positive")
        if self.target_quantity <= 0:
            raise ValueError("target_quantity must be a positive integer")
        if self.disappear_timeout_seconds <= 0:
            raise ValueError("disappear_timeout_seconds must be positive")
        if self.max_history_records <= 0:
            raise ValueError("max_history_records must be a positive integer")
        if self.max_cycles is not None and self.max_cycles <= 0:
            raise ValueError("max_cycles must be a positive integer")
        if self.min_net_profit_cents < 0:
            raise ValueError("min_net_profit_cents cannot be negative")


@dataclass(frozen=True)
class EvaluatorHealth:
    """Point-in-time telemetry and health status of the continuous evaluator."""

    status: str
    pid: Optional[int]
    session_id: Optional[str]
    started_at: Optional[str]
    stopped_at: Optional[str]
    last_evaluation_time: Optional[str]
    evaluations_count: int
    processed_observations_count: int
    active_candidates_count: int
    qualified_candidates_count: int
    rejected_candidates_count: int
    total_candidates_detected: int
    candidate_transitions_count: int
    is_running: bool
    latest_input_timestamp: Optional[str]
    last_error: Optional[str] = None
    last_error_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ContinuousEvaluator:
    """Continuous arbitrage evaluation service for live Kalshi market observations."""

    def __init__(
        self,
        config: Optional[ContinuousEvaluatorConfig] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or ContinuousEvaluatorConfig()
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))

        self.session_id: str = (
            self.config.session_id
            or f"eval-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        )

        # Operational state
        self._status: EvaluatorStatus = EvaluatorStatus.STOPPED
        self._started_at: Optional[datetime] = None
        self._stopped_at: Optional[datetime] = None
        self._last_evaluation_time: Optional[datetime] = None
        self._latest_input_timestamp: Optional[datetime] = None
        self._evaluations_count: int = 0
        self._processed_observations_count: int = 0
        self._total_candidates_detected: int = 0
        self._candidate_transitions_count: int = 0
        self._last_error: Optional[str] = None
        self._last_error_code: Optional[str] = None

        # Threading and synchronization
        self._eval_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # Candidate tracking
        self._active_candidates: Dict[str, ArbitrageCandidate] = {}
        self._all_candidates: Dict[str, ArbitrageCandidate] = {}
        self._candidate_history: Deque[Dict[str, Any]] = deque(
            maxlen=self.config.max_history_records
        )
        self._market_fingerprints: Dict[str, str] = {}
        self._market_last_seen: Dict[str, datetime] = {}
        self._recent_observations: Dict[str, MarketObservation] = {}
        self._observation_history: Deque[MarketObservation] = deque(maxlen=200)

        # Configure fee model
        self._fee_model = FeeModelConfig(schedule_type=self.config.fee_schedule)

        # Load declared MECE baskets
        self._baskets: Tuple[DeclaredMeceBasket, ...] = ()
        if self.config.baskets:
            self._baskets = tuple(self.config.baskets)
        elif self.config.baskets_path:
            try:
                self._baskets = load_declared_baskets(self.config.baskets_path)
                logger.info("Loaded %d declared MECE baskets from %s", len(self._baskets), self.config.baskets_path)
            except Exception as exc:
                logger.error("Failed to load declared baskets from %s: %s", self.config.baskets_path, exc)
                self._last_error = f"Failed to load baskets: {exc}"
                self._last_error_code = "BASKET_LOAD_ERROR"

        # Reconstructor for multi-market event baskets
        self._reconstructor = EventSnapshotReconstructor(
            config=SnapshotReconstructionConfig(
                max_leg_timestamp_skew_seconds=self.config.max_leg_skew_seconds,
                cycle_window_seconds=15.0,
                max_stale_seconds=self.config.max_stale_seconds,
                require_source_timestamp=True,
            )
        )

        # Execution-aware qualification engine (V22)
        self._qualification_outcomes_count: Dict[str, int] = {}
        qualifier_cfg = ExecutionQualifierConfig(
            target_quantity=self.config.target_quantity,
            max_stale_seconds=self.config.max_stale_seconds,
            max_leg_skew_seconds=self.config.max_leg_skew_seconds,
            fee_model_config=self._fee_model,
            require_full_quantity=self.config.require_full_quantity,
            min_net_profit_cents=self.config.min_net_profit_cents,
            require_source_timestamp=self.config.require_source_timestamp,
        )
        self.qualifier = ExecutionAwareArbitrageQualifier(config=qualifier_cfg)

        # Qualified-Opportunity Alert Engine (V24)
        self.alert_engine: Optional[AlertEngine] = None
        if self.config.alerts_enabled:
            alert_cfg = self.config.alert_config
            if alert_cfg is None:
                alert_state = self.config.alert_state_file or (
                    self.config.state_file.parent / "live_alert_state.json"
                    if self.config.state_file
                    else Path("data/live_alert_state.json")
                )
                alert_hist = self.config.alert_history_file or (
                    self.config.history_file.parent / "live_alert_history.jsonl"
                    if self.config.history_file
                    else Path("data/live_alert_history.jsonl")
                )
                alert_cfg = AlertConfig(
                    state_file=alert_state,
                    history_file=alert_hist,
                )
            self.alert_engine = AlertEngine(config=alert_cfg, time_provider=self._time_provider)

        # File paths
        self._state_file_path: Optional[Path] = self.config.state_file
        self._history_file_path: Optional[Path] = self.config.history_file

        if self._state_file_path is not None:
            self._state_file_path.parent.mkdir(parents=True, exist_ok=True)
            self._save_state()

        if self._history_file_path is not None:
            self._history_file_path.parent.mkdir(parents=True, exist_ok=True)

    def _now(self) -> datetime:
        return self._time_provider()

    @property
    def status(self) -> EvaluatorStatus:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._status == EvaluatorStatus.RUNNING

    def get_health(self) -> EvaluatorHealth:
        """Produce point-in-time evaluator health and telemetry."""
        qualified_count = sum(1 for c in self._active_candidates.values() if c.is_qualified)
        rejected_count = sum(
            count for outcome, count in self._qualification_outcomes_count.items()
            if outcome != QualificationOutcome.QUALIFIED.value
        )
        return EvaluatorHealth(
            status=self._status.value,
            pid=os.getpid() if self._status == EvaluatorStatus.RUNNING else None,
            session_id=self.session_id,
            started_at=self._started_at.isoformat() if self._started_at else None,
            stopped_at=self._stopped_at.isoformat() if self._stopped_at else None,
            last_evaluation_time=(
                self._last_evaluation_time.isoformat()
                if self._last_evaluation_time
                else None
            ),
            evaluations_count=self._evaluations_count,
            processed_observations_count=self._processed_observations_count,
            active_candidates_count=len(self._active_candidates),
            qualified_candidates_count=qualified_count,
            rejected_candidates_count=rejected_count,
            total_candidates_detected=self._total_candidates_detected,
            candidate_transitions_count=self._candidate_transitions_count,
            is_running=self.is_running,
            latest_input_timestamp=(
                self._latest_input_timestamp.isoformat()
                if self._latest_input_timestamp
                else None
            ),
            last_error=self._last_error,
            last_error_code=self._last_error_code,
        )

    def get_active_candidates(self) -> List[Dict[str, Any]]:
        """Return serialized list of all currently active arbitrage candidates."""
        with self._eval_lock:
            return [c.to_dict() for c in self._active_candidates.values()]

    def get_qualified_candidates(self) -> List[Dict[str, Any]]:
        """Return serialized list of all currently active candidates that meet full qualification."""
        with self._eval_lock:
            return [c.to_dict() for c in self._active_candidates.values() if c.is_qualified]

    def get_qualification_summary(self) -> Dict[str, Any]:
        """Return summary breakdown of candidate qualification outcomes."""
        with self._eval_lock:
            return {
                "total_evaluated": sum(self._qualification_outcomes_count.values()),
                "qualified_count": sum(1 for c in self._active_candidates.values() if c.is_qualified),
                "outcomes_breakdown": dict(self._qualification_outcomes_count),
            }

    def get_candidate_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent candidate state transitions in reverse chronological order."""
        with self._eval_lock:
            items = list(self._candidate_history)
            items.reverse()
            return items[:limit]

    def _record_transition(
        self,
        candidate_id: str,
        strategy_type: str,
        from_state: Optional[str],
        to_state: str,
        reason: Optional[str],
        gross_edge_cents: Optional[int],
        net_edge_cents: Optional[int],
        supported_quantity: int,
        market_tickers: Tuple[str, ...],
        now_iso: str,
        qualification_outcome: Optional[str] = None,
    ) -> CandidateTransition:
        """Record and persist an explicit candidate lifecycle transition."""
        trans = CandidateTransition(
            transition_id=f"trans-{uuid.uuid4().hex[:10]}",
            candidate_id=candidate_id,
            strategy_type=strategy_type,
            from_state=from_state,
            to_state=to_state,
            transitioned_at=now_iso,
            reason=reason,
            gross_edge_cents=gross_edge_cents,
            net_edge_cents=net_edge_cents,
            supported_quantity=supported_quantity,
            market_tickers=market_tickers,
            qualification_outcome=qualification_outcome,
        )
        t_dict = trans.to_dict()
        self._candidate_history.append(t_dict)
        self._candidate_transitions_count += 1

        if self._history_file_path is not None:
            try:
                with open(self._history_file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(t_dict) + "\n")
            except OSError as exc:
                logger.warning("Failed appending transition to %s: %s", self._history_file_path, exc)

        return trans

    def _save_state(self) -> None:
        """Persist evaluator state atomically to disk."""
        if self._state_file_path is None:
            return
        tmp_path = self._state_file_path.with_suffix(".tmp")
        try:
            health = self.get_health()
            payload = health.to_dict()
            payload["active_candidates"] = [c.to_dict() for c in self._active_candidates.values()]
            payload["qualified_candidates"] = [
                c.to_dict() for c in self._active_candidates.values() if c.is_qualified
            ]
            payload["qualification_summary"] = {
                "qualified_count": sum(1 for c in self._active_candidates.values() if c.is_qualified),
                "total_evaluated": sum(self._qualification_outcomes_count.values()),
                "outcomes_breakdown": dict(self._qualification_outcomes_count),
            }
            payload["config"] = {
                "poll_interval_seconds": self.config.poll_interval_seconds,
                "max_stale_seconds": self.config.max_stale_seconds,
                "max_leg_skew_seconds": self.config.max_leg_skew_seconds,
                "target_quantity": self.config.target_quantity,
                "fee_schedule": self.config.fee_schedule.value,
                "baskets_count": len(self._baskets),
                "require_full_quantity": self.config.require_full_quantity,
                "min_net_profit_cents": self.config.min_net_profit_cents,
                "alerts_enabled": self.config.alerts_enabled,
            }
            if self.alert_engine is not None:
                payload["alert_engine"] = self.alert_engine.get_status()
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp_path.replace(self._state_file_path)
        except OSError as exc:
            logger.warning("Failed to persist evaluator state: %s", exc)

    def evaluate_observations(
        self, observations: Sequence[MarketObservation]
    ) -> Tuple[List[ArbitrageCandidate], List[CandidateTransition]]:
        """Evaluate freshly observed market data and update active candidate lifecycle.

        Thread-safe, non-blocking with respect to collector operations.
        Returns newly active or updated candidates and recorded transitions.
        """
        if not observations:
            return [], []

        with self._eval_lock:
            now = self._now()
            now_iso = now.isoformat()
            self._last_evaluation_time = now
            self._evaluations_count += 1
            self._processed_observations_count += len(observations)

            emitted_candidates: List[ArbitrageCandidate] = []
            recorded_transitions: List[CandidateTransition] = []

            # 1. Update observation index and check input timestamps
            for obs in observations:
                if obs is None or not obs.ticker:
                    continue
                self._market_last_seen[obs.ticker] = now
                self._recent_observations[obs.ticker] = obs
                self._observation_history.append(obs)
                eff_ts = obs.source_timestamp or obs.observed_at
                if eff_ts is not None:
                    if self._latest_input_timestamp is None or eff_ts > self._latest_input_timestamp:
                        self._latest_input_timestamp = eff_ts

            # 2. Evaluate Single-Market Binary Parity Opportunities
            for obs in observations:
                if obs is None or not obs.ticker:
                    continue
                cand_id = f"BP:{obs.ticker}"
                market_tickers = (obs.ticker,)
                prev_cand = self._active_candidates.get(cand_id)

                book = obs.order_book
                raw_fp = compute_book_fingerprint(book)
                stale_flag = "stale" if obs.is_stale else "fresh"
                new_fp = f"{raw_fp}:{stale_flag}"
                prev_fp = self._market_fingerprints.get(obs.ticker)

                # Deduplication: If order book fingerprint unchanged and candidate already evaluated, skip!
                if prev_fp == new_fp and prev_cand is not None:
                    continue
                self._market_fingerprints[obs.ticker] = new_fp

                # Execution-Aware Qualification (V22)
                qual = self.qualifier.qualify_binary_parity(
                    observation=obs,
                    requested_quantity=self.config.target_quantity,
                    now=now,
                )
                self._qualification_outcomes_count[qual.outcome.value] = (
                    self._qualification_outcomes_count.get(qual.outcome.value, 0) + 1
                )

                # Per-leg ask prices
                yes_asks = derive_ask_levels(book, YES) if book else ()
                no_asks = derive_ask_levels(book, NO) if book else ()
                per_leg_prices = {
                    f"{obs.ticker}:YES": yes_asks[0].price_cents if yes_asks else None,
                    f"{obs.ticker}:NO": no_asks[0].price_cents if no_asks else None,
                }

                if qual.is_qualified:
                    # Qualified arbitrage opportunity
                    if prev_cand is None:
                        # NEW candidate
                        self._total_candidates_detected += 1
                        trans = self._record_transition(
                            candidate_id=cand_id,
                            strategy_type=OPPORTUNITY_BINARY_PARITY,
                            from_state=None,
                            to_state=CandidateState.NEW.value,
                            reason="Qualified execution-feasible arbitrage candidate detected",
                            gross_edge_cents=qual.gross_edge_cents,
                            net_edge_cents=qual.net_edge_cents,
                            supported_quantity=qual.executable_quantity,
                            market_tickers=market_tickers,
                            now_iso=now_iso,
                            qualification_outcome=qual.outcome.value,
                        )
                        recorded_transitions.append(trans)
                        cand = ArbitrageCandidate(
                            candidate_id=cand_id,
                            strategy_type=OPPORTUNITY_BINARY_PARITY,
                            event_ticker=obs.market.event_ticker if obs.market else None,
                            market_tickers=market_tickers,
                            state=CandidateState.NEW,
                            detected_at=now_iso,
                            updated_at=now_iso,
                            source_timestamps={obs.ticker: obs.source_timestamp.isoformat() if obs.source_timestamp else None},
                            observation_ids=(obs.observation_id,),
                            gross_edge_cents=qual.gross_edge_cents,
                            estimated_fees_cents=qual.total_fees_cents,
                            net_edge_cents=qual.net_edge_cents,
                            guaranteed_payout_cents=qual.guaranteed_payout_cents,
                            total_cost_cents=qual.total_cost_cents,
                            target_quantity=self.config.target_quantity,
                            supported_quantity=qual.executable_quantity,
                            is_mathematically_valid=True,
                            is_depth_supported=True,
                            is_net_profitable=True,
                            is_qualified=True,
                            status=STATUS_QUALIFIED,
                            rejection_reason=None,
                            market_state_fingerprint=new_fp,
                            per_leg_prices_cents=per_leg_prices,
                            transitions=[trans.to_dict()],
                            qualification=qual,
                        )
                        self._active_candidates[cand_id] = cand
                        self._all_candidates[cand_id] = cand
                        emitted_candidates.append(cand)
                    else:
                        # Existing candidate updated
                        edge_changed = (
                            prev_cand.net_edge_cents != qual.net_edge_cents
                            or prev_cand.supported_quantity != qual.executable_quantity
                            or prev_cand.total_cost_cents != qual.total_cost_cents
                        )
                        if edge_changed:
                            trans = self._record_transition(
                                candidate_id=cand_id,
                                strategy_type=OPPORTUNITY_BINARY_PARITY,
                                from_state=prev_cand.state.value,
                                to_state=CandidateState.UPDATED.value,
                                reason="Candidate quotes, edge, or supported depth changed",
                                gross_edge_cents=qual.gross_edge_cents,
                                net_edge_cents=qual.net_edge_cents,
                                supported_quantity=qual.executable_quantity,
                                market_tickers=market_tickers,
                                now_iso=now_iso,
                                qualification_outcome=qual.outcome.value,
                            )
                            recorded_transitions.append(trans)
                            prev_cand.transitions.append(trans.to_dict())

                        prev_cand.state = CandidateState.UPDATED
                        prev_cand.updated_at = now_iso
                        prev_cand.gross_edge_cents = qual.gross_edge_cents
                        prev_cand.estimated_fees_cents = qual.total_fees_cents
                        prev_cand.net_edge_cents = qual.net_edge_cents
                        prev_cand.guaranteed_payout_cents = qual.guaranteed_payout_cents
                        prev_cand.total_cost_cents = qual.total_cost_cents
                        prev_cand.supported_quantity = qual.executable_quantity
                        prev_cand.market_state_fingerprint = new_fp
                        prev_cand.per_leg_prices_cents = per_leg_prices
                        prev_cand.qualification = qual
                        emitted_candidates.append(prev_cand)
                else:
                    # Not qualified
                    if prev_cand is not None:
                        to_state = (
                            CandidateState.INVALIDATED.value
                            if qual.outcome in (
                                QualificationOutcome.REJECTED_STALE,
                                QualificationOutcome.REJECTED_LEG_SKEW,
                                QualificationOutcome.REJECTED_MISSING_MARKET,
                                QualificationOutcome.REJECTED_MISSING_TIMESTAMP,
                            )
                            else CandidateState.DISQUALIFIED.value
                        )
                        trans = self._record_transition(
                            candidate_id=cand_id,
                            strategy_type=OPPORTUNITY_BINARY_PARITY,
                            from_state=prev_cand.state.value,
                            to_state=to_state,
                            reason=qual.rejection_reason,
                            gross_edge_cents=qual.gross_edge_cents,
                            net_edge_cents=qual.net_edge_cents,
                            supported_quantity=qual.executable_quantity,
                            market_tickers=market_tickers,
                            now_iso=now_iso,
                            qualification_outcome=qual.outcome.value,
                        )
                        recorded_transitions.append(trans)
                        prev_cand.state = CandidateState(to_state)
                        prev_cand.rejection_reason = qual.rejection_reason
                        prev_cand.updated_at = now_iso
                        prev_cand.status = qual.outcome.value
                        prev_cand.qualification = qual
                        prev_cand.transitions.append(trans.to_dict())
                        del self._active_candidates[cand_id]

            # 3. Evaluate Multi-Market MECE Event Baskets
            for basket in self._baskets:
                cand_id = f"MECE:{basket.event_ticker}:{basket.basket_side}"
                opp_type = (
                    OPPORTUNITY_MECE_BASKET_LONG_YES
                    if basket.basket_side == YES
                    else OPPORTUNITY_MECE_BASKET_LONG_NO
                )
                market_tickers = tuple(basket.ordered_tickers)
                prev_cand = self._active_candidates.get(cand_id)

                # Fingerprint constituent books for change detection
                basket_fps = []
                for t in basket.ordered_tickers:
                    b_obs = self._recent_observations.get(t)
                    if b_obs:
                        src_ts_iso = b_obs.source_timestamp.isoformat() if b_obs.source_timestamp else "none"
                        stale_flag = "stale" if b_obs.is_stale else "fresh"
                        basket_fps.append(f"{compute_book_fingerprint(b_obs.order_book)}:{src_ts_iso}:{stale_flag}")
                    else:
                        basket_fps.append("NONE")
                basket_fp = hashlib.sha256(";".join(basket_fps).encode()).hexdigest()[:16]

                if prev_cand is not None and prev_cand.market_state_fingerprint == basket_fp:
                    continue

                # Execution-Aware Qualification (V22)
                qual = self.qualifier.qualify_mece_basket(
                    basket=basket,
                    observations=self._recent_observations,
                    requested_quantity=self.config.target_quantity,
                    now=now,
                )
                self._qualification_outcomes_count[qual.outcome.value] = (
                    self._qualification_outcomes_count.get(qual.outcome.value, 0) + 1
                )

                # Per-leg ask prices
                per_leg_prices = {}
                for t in basket.ordered_tickers:
                    b_obs = self._recent_observations.get(t)
                    if b_obs and b_obs.order_book:
                        asks = derive_ask_levels(b_obs.order_book, basket.basket_side)
                        per_leg_prices[t] = asks[0].price_cents if asks else None
                    else:
                        per_leg_prices[t] = None

                src_ts_map = {
                    t: (
                        self._recent_observations[t].source_timestamp.isoformat()
                        if self._recent_observations.get(t) and self._recent_observations[t].source_timestamp
                        else None
                    )
                    for t in basket.ordered_tickers
                }
                obs_ids = tuple(
                    self._recent_observations[t].observation_id
                    for t in basket.ordered_tickers
                    if self._recent_observations.get(t)
                )

                if qual.is_qualified:
                    if prev_cand is None:
                        self._total_candidates_detected += 1
                        trans = self._record_transition(
                            candidate_id=cand_id,
                            strategy_type=opp_type,
                            from_state=None,
                            to_state=CandidateState.NEW.value,
                            reason="Qualified MECE basket arbitrage candidate detected",
                            gross_edge_cents=qual.gross_edge_cents,
                            net_edge_cents=qual.net_edge_cents,
                            supported_quantity=qual.executable_quantity,
                            market_tickers=market_tickers,
                            now_iso=now_iso,
                            qualification_outcome=qual.outcome.value,
                        )
                        recorded_transitions.append(trans)
                        cand = ArbitrageCandidate(
                            candidate_id=cand_id,
                            strategy_type=opp_type,
                            event_ticker=basket.event_ticker,
                            market_tickers=market_tickers,
                            state=CandidateState.NEW,
                            detected_at=now_iso,
                            updated_at=now_iso,
                            source_timestamps=src_ts_map,
                            observation_ids=obs_ids,
                            gross_edge_cents=qual.gross_edge_cents,
                            estimated_fees_cents=qual.total_fees_cents,
                            net_edge_cents=qual.net_edge_cents,
                            guaranteed_payout_cents=qual.guaranteed_payout_cents,
                            total_cost_cents=qual.total_cost_cents,
                            target_quantity=self.config.target_quantity,
                            supported_quantity=qual.executable_quantity,
                            is_mathematically_valid=True,
                            is_depth_supported=True,
                            is_net_profitable=True,
                            is_qualified=True,
                            status=STATUS_QUALIFIED,
                            rejection_reason=None,
                            market_state_fingerprint=basket_fp,
                            per_leg_prices_cents=per_leg_prices,
                            transitions=[trans.to_dict()],
                            qualification=qual,
                        )
                        self._active_candidates[cand_id] = cand
                        self._all_candidates[cand_id] = cand
                        emitted_candidates.append(cand)
                    else:
                        edge_changed = (
                            prev_cand.net_edge_cents != qual.net_edge_cents
                            or prev_cand.supported_quantity != qual.executable_quantity
                            or prev_cand.total_cost_cents != qual.total_cost_cents
                        )
                        if edge_changed:
                            trans = self._record_transition(
                                candidate_id=cand_id,
                                strategy_type=opp_type,
                                from_state=prev_cand.state.value,
                                to_state=CandidateState.UPDATED.value,
                                reason="MECE candidate quotes, edge, or depth changed",
                                gross_edge_cents=qual.gross_edge_cents,
                                net_edge_cents=qual.net_edge_cents,
                                supported_quantity=qual.executable_quantity,
                                market_tickers=market_tickers,
                                now_iso=now_iso,
                                qualification_outcome=qual.outcome.value,
                            )
                            recorded_transitions.append(trans)
                            prev_cand.transitions.append(trans.to_dict())

                        prev_cand.state = CandidateState.UPDATED
                        prev_cand.updated_at = now_iso
                        prev_cand.gross_edge_cents = qual.gross_edge_cents
                        prev_cand.estimated_fees_cents = qual.total_fees_cents
                        prev_cand.net_edge_cents = qual.net_edge_cents
                        prev_cand.guaranteed_payout_cents = qual.guaranteed_payout_cents
                        prev_cand.total_cost_cents = qual.total_cost_cents
                        prev_cand.supported_quantity = qual.executable_quantity
                        prev_cand.market_state_fingerprint = basket_fp
                        prev_cand.per_leg_prices_cents = per_leg_prices
                        prev_cand.qualification = qual
                        emitted_candidates.append(prev_cand)
                else:
                    if prev_cand is not None:
                        to_state = (
                            CandidateState.INVALIDATED.value
                            if qual.outcome in (
                                QualificationOutcome.REJECTED_STALE,
                                QualificationOutcome.REJECTED_LEG_SKEW,
                                QualificationOutcome.REJECTED_MISSING_MARKET,
                                QualificationOutcome.REJECTED_MISSING_TIMESTAMP,
                            )
                            else CandidateState.DISQUALIFIED.value
                        )
                        trans = self._record_transition(
                            candidate_id=cand_id,
                            strategy_type=opp_type,
                            from_state=prev_cand.state.value,
                            to_state=to_state,
                            reason=qual.rejection_reason,
                            gross_edge_cents=qual.gross_edge_cents,
                            net_edge_cents=qual.net_edge_cents,
                            supported_quantity=qual.executable_quantity,
                            market_tickers=market_tickers,
                            now_iso=now_iso,
                            qualification_outcome=qual.outcome.value,
                        )
                        recorded_transitions.append(trans)
                        prev_cand.state = CandidateState(to_state)
                        prev_cand.rejection_reason = qual.rejection_reason
                        prev_cand.updated_at = now_iso
                        prev_cand.status = qual.outcome.value
                        prev_cand.qualification = qual
                        prev_cand.transitions.append(trans.to_dict())
                        del self._active_candidates[cand_id]

            # 4. Check for Disappeared Markets across Active Candidates
            disappeared_ids: List[str] = []
            for c_id, cand in self._active_candidates.items():
                for t in cand.market_tickers:
                    last_seen = self._market_last_seen.get(t)
                    if last_seen is not None:
                        elapsed = (now - last_seen).total_seconds()
                        if elapsed > self.config.disappear_timeout_seconds:
                            disappeared_ids.append(c_id)
                            break

            for c_id in disappeared_ids:
                cand = self._active_candidates[c_id]
                trans = self._record_transition(
                    candidate_id=c_id,
                    strategy_type=cand.strategy_type,
                    from_state=cand.state.value,
                    to_state=CandidateState.DISAPPEARED.value,
                    reason=f"Constituent market not observed for > {self.config.disappear_timeout_seconds}s",
                    gross_edge_cents=cand.gross_edge_cents,
                    net_edge_cents=cand.net_edge_cents,
                    supported_quantity=cand.supported_quantity,
                    market_tickers=cand.market_tickers,
                    now_iso=now_iso,
                )
                recorded_transitions.append(trans)
                cand.state = CandidateState.DISAPPEARED
                cand.updated_at = now_iso
                cand.rejection_reason = trans.reason
                cand.transitions.append(trans.to_dict())
                del self._active_candidates[c_id]

            # 5. Process alerts for qualified opportunities (V24)
            if self.alert_engine is not None:
                try:
                    self.alert_engine.process_evaluator_cycle(
                        active_candidates=self._active_candidates,
                        transitions=recorded_transitions,
                        now=now,
                    )
                except Exception as exc:
                    logger.warning("Error processing alerts: %s", exc)

            # Save state
            self._save_state()

            return emitted_candidates, recorded_transitions

    def tail_observations_file(self) -> None:
        """Stream and evaluate newly appended observations from an observations JSONL file."""
        if self.config.observations_file is None:
            raise ValueError("observations_file must be specified in config to tail file")

        path = Path(self.config.observations_file)
        logger.info("Starting file-tailing continuous evaluator on %s", path)

        file_offset = 0
        current_inode = None
        cycles = 0

        while not self._stop_event.is_set():
            if self.config.max_cycles is not None and cycles >= self.config.max_cycles:
                logger.info("Reached max_cycles (%d). Stopping evaluator.", self.config.max_cycles)
                break

            cycles += 1

            if not path.exists():
                self._stop_event.wait(self.config.poll_interval_seconds)
                continue

            try:
                stat = path.stat()
                # Check for file rotation / recreation
                if current_inode is not None and stat.st_ino != current_inode:
                    logger.info("Evidence file rotated (inode changed). Resetting offset.")
                    file_offset = 0
                current_inode = stat.st_ino

                # Check for file truncation
                if stat.st_size < file_offset:
                    logger.info("Evidence file truncated. Resetting offset.")
                    file_offset = 0

                new_observations: List[MarketObservation] = []
                with open(path, "r", encoding="utf-8") as f:
                    f.seek(file_offset)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            obs = observation_from_dict(d)
                            new_observations.append(obs)
                        except Exception as exc:
                            logger.debug("Skipping unparseable evidence line: %s", exc)
                    file_offset = f.tell()

                if new_observations:
                    self.evaluate_observations(new_observations)

            except Exception as exc:
                logger.warning("Error reading observations file %s: %s", path, exc)
                self._last_error = str(exc)
                self._last_error_code = "FILE_TAIL_ERROR"

            self._stop_event.wait(self.config.poll_interval_seconds)

    def run(self) -> None:
        """Execute continuous evaluation until stopped."""
        self._status = EvaluatorStatus.RUNNING
        self._started_at = self._now()
        self._stop_event.clear()
        if self.alert_engine is not None:
            self.alert_engine.start()
        self._save_state()
        logger.info("Continuous live evaluator started (session: %s)", self.session_id)

        try:
            if self.config.observations_file is not None:
                self.tail_observations_file()
            else:
                # Listener mode: wait until stop event is signaled
                while not self._stop_event.is_set():
                    self._stop_event.wait(1.0)
        except Exception as exc:
            self._status = EvaluatorStatus.FAILED
            self._last_error = str(exc)
            self._last_error_code = "RUN_LOOP_ERROR"
            logger.error("Continuous live evaluator crashed: %s", exc, exc_info=True)
            self._save_state()
            raise
        finally:
            if self.alert_engine is not None:
                self.alert_engine.stop()
            self._status = EvaluatorStatus.STOPPED
            self._stopped_at = self._now()
            self._save_state()
            logger.info("Continuous live evaluator stopped.")

    def stop(self) -> None:
        """Signal the evaluator to shut down gracefully."""
        self._status = EvaluatorStatus.STOPPING
        self._stop_event.set()
        if self.alert_engine is not None:
            self.alert_engine.stop()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
        self._status = EvaluatorStatus.STOPPED
        self._stopped_at = self._now()
        self._save_state()
