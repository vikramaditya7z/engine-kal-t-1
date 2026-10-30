"""Kalshi Arbitrage Engine V20 — Continuous Live Market Data Infrastructure.

Provides a robust, continuously running, read-only market data collection service
for public Kalshi prediction markets.

Governing Principles:
1. Strict Read-Only Interaction: Consumes only public read-only REST endpoints;
   never places trades, modifies orders, or requires trading credentials.
2. High Synchronization with Bounded Concurrency: Uses worker pools to fetch
   market metadata and order books concurrently, minimizing cross-market skew
   while respecting API rate limits.
3. Overlap Prevention & Cycle Pacing: Single-flight cycle execution guarantees
   cycles never overlap, even under network latency or backoff delays.
4. Resilient Error Handling & Backoff: Explicit handling for HTTP 429 rate limits,
   connection timeouts, and 5xx errors with bounded exponential backoff.
5. Trustworthy Freshness & Provenance: Validates exchange source timestamps (HTTP Date),
   detects stale data (> max_stale_seconds), flags empty/malformed books, and never
   fabricates synthetic quotes or masks failures with old data.
6. Bounded Resource Footprint: Uses fixed-capacity in-memory buffers to prevent
   unbounded memory growth during indefinite continuous operation, streaming durable
   observations to JSONL with cryptographic manifests.
7. Observable Health Interface: Exposes real-time health and metrics via atomic
   state files and read-only REST endpoints.
"""

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from .evidence_storage import (
    EVIDENCE_SCHEMA_VERSION,
    observation_to_dict,
)
from .market_data import (
    MarketDataInputError,
    NormalizedMarket,
    NormalizedOrderBook,
)
from .observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
)
from .rest_client import (
    KalshiClientError,
    KalshiHTTPError,
    KalshiResponseError,
    KalshiRestClient,
    KalshiTransportError,
)

logger = logging.getLogger(__name__)


class CollectorStatus(str, Enum):
    """Lifecycle states of the continuous market data collector."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ContinuousCollectorConfig:
    """Configuration options for continuous live market data collection."""

    environment: str = "production"
    poll_interval_seconds: float = 2.0
    max_stale_seconds: float = 60.0
    concurrency: int = 4
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_backoff_factor: float = 0.5
    rate_limit_backoff_seconds: float = 2.0
    max_rate_limit_backoff_seconds: float = 30.0
    target_tickers: Optional[Tuple[str, ...]] = None
    max_markets_to_poll: int = 30
    discovery_refresh_interval_seconds: float = 300.0
    output_dir: Optional[Path] = None
    state_file: Optional[Path] = None
    session_id: Optional[str] = None
    max_memory_observations: int = 200
    max_cycles: Optional[int] = None
    manifest_interval_cycles: int = 10
    on_cycle_complete: Optional[Callable[[Sequence[MarketObservation], "CollectorCycleSummary"], None]] = None

    def __post_init__(self) -> None:
        if self.poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.retry_backoff_factor < 0:
            raise ValueError("retry_backoff_factor must be non-negative")
        if self.rate_limit_backoff_seconds <= 0:
            raise ValueError("rate_limit_backoff_seconds must be positive")
        if self.max_rate_limit_backoff_seconds < self.rate_limit_backoff_seconds:
            raise ValueError("max_rate_limit_backoff_seconds must be >= rate_limit_backoff_seconds")
        if self.max_markets_to_poll <= 0:
            raise ValueError("max_markets_to_poll must be a positive integer")
        if self.discovery_refresh_interval_seconds <= 0:
            raise ValueError("discovery_refresh_interval_seconds must be positive")
        if self.max_memory_observations <= 0:
            raise ValueError("max_memory_observations must be a positive integer")
        if self.max_cycles is not None and self.max_cycles <= 0:
            raise ValueError("max_cycles must be a positive integer")
        if self.manifest_interval_cycles <= 0:
            raise ValueError("manifest_interval_cycles must be a positive integer")


@dataclass(frozen=True)
class CollectorHealth:
    """Serializable operational health and freshness telemetry for the collector."""

    status: str
    pid: Optional[int]
    session_id: str
    environment: str
    started_at: Optional[str]
    stopped_at: Optional[str]
    last_successful_collection_time: Optional[str]
    last_attempted_collection_time: Optional[str]
    latest_snapshot_timestamp: Optional[str]
    latest_snapshot_age_seconds: Optional[float]
    is_data_fresh: bool
    consecutive_failures: int
    successful_cycles_count: int
    failed_cycles_count: int
    total_cycles_count: int
    total_observations_count: int
    active_monitored_markets_count: int
    last_cycle_duration_seconds: float
    last_cycle_markets_polled: int
    last_cycle_markets_succeeded: int
    last_cycle_markets_failed: int
    last_error: Optional[str] = None
    last_error_code: Optional[str] = None
    rate_limit_count: int = 0
    poll_interval_seconds: float = 2.0
    max_stale_seconds: float = 60.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert health status to a JSON-serializable dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class CollectorCycleSummary:
    """Summary of an individual continuous collection cycle."""

    cycle_index: int
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    markets_polled: int
    markets_succeeded: int
    markets_failed: int
    markets_stale: int
    is_success: bool
    error_message: Optional[str] = None


class ContinuousCollector:
    """Continuously observes live Kalshi public markets with bounded concurrency and resilient retry."""

    def __init__(
        self,
        client: Optional[KalshiRestClient] = None,
        config: Optional[ContinuousCollectorConfig] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or ContinuousCollectorConfig()
        self.client = client or KalshiRestClient(
            environment=self.config.environment,
            timeout_seconds=self.config.request_timeout_seconds,
        )
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))

        self.session_id = (
            self.config.session_id
            or f"v20-continuous-{int(self._now().timestamp())}-{os.getpid()}"
        )

        # Lifecycle & synchronization
        self._status: CollectorStatus = CollectorStatus.STOPPED
        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._cycle_index = 0

        # Health & statistics
        self._started_at: Optional[datetime] = None
        self._stopped_at: Optional[datetime] = None
        self._last_successful_collection_time: Optional[datetime] = None
        self._last_attempted_collection_time: Optional[datetime] = None
        self._latest_snapshot_timestamp: Optional[datetime] = None
        self._consecutive_failures = 0
        self._successful_cycles_count = 0
        self._failed_cycles_count = 0
        self._total_observations_count = 0
        self._rate_limit_count = 0
        self._last_cycle_duration = 0.0
        self._last_cycle_polled = 0
        self._last_cycle_succeeded = 0
        self._last_cycle_failed = 0
        self._last_error: Optional[str] = None
        self._last_error_code: Optional[str] = None

        # Market tracking
        self._monitored_tickers: Tuple[str, ...] = tuple(self.config.target_tickers or ())
        self._last_discovery_time: float = 0.0

        # Memory buffer (strictly bounded to prevent unbounded memory growth)
        self._recent_observations: Deque[MarketObservation] = deque(
            maxlen=self.config.max_memory_observations
        )
        self._recent_summaries: Deque[CollectorCycleSummary] = deque(maxlen=50)

        # Evidence file handles and locks
        self._file_lock = threading.Lock()
        self._evidence_file_path: Optional[Path] = None
        self._manifest_file_path: Optional[Path] = None

        if self.config.output_dir is not None:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            self._evidence_file_path = self.config.output_dir / "observations.jsonl"
            self._manifest_file_path = self.config.output_dir / "manifest.json"

        # State and cycle listeners
        self._cycle_listeners: List[Callable[[Sequence[MarketObservation], CollectorCycleSummary], None]] = []
        if self.config.on_cycle_complete is not None:
            self._cycle_listeners.append(self.config.on_cycle_complete)

        # Initialize state file location
        self._state_file_path: Optional[Path] = self.config.state_file
        if self._state_file_path is None and self.config.output_dir is not None:
            self._state_file_path = self.config.output_dir / "collector_state.json"

        if self._state_file_path is not None:
            self._state_file_path.parent.mkdir(parents=True, exist_ok=True)
            self._save_state()

    def add_cycle_listener(
        self,
        listener: Callable[[Sequence[MarketObservation], CollectorCycleSummary], None],
    ) -> None:
        """Register a callback to be invoked at the end of each collection cycle."""
        self._cycle_listeners.append(listener)

    def _notify_cycle_listeners(
        self, observations: Sequence[MarketObservation], summary: CollectorCycleSummary
    ) -> None:
        """Notify all registered cycle listeners safely."""
        for listener in self._cycle_listeners:
            try:
                listener(observations, summary)
            except Exception as exc:
                logger.warning("Cycle listener %s raised exception: %s", listener, exc)

    def _now(self) -> datetime:
        return self._time_provider()

    @property
    def status(self) -> CollectorStatus:
        return self._status

    @property
    def is_running(self) -> bool:
        return self._status == CollectorStatus.RUNNING

    def get_health(self) -> CollectorHealth:
        """Produce point-in-time collector health and freshness telemetry."""
        now = self._now()
        snapshot_age: Optional[float] = None
        if self._latest_snapshot_timestamp is not None:
            latest = self._latest_snapshot_timestamp
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            snapshot_age = max(0.0, (now - latest).total_seconds())

        is_fresh = (
            self._status == CollectorStatus.RUNNING
            and self._consecutive_failures == 0
            and snapshot_age is not None
            and snapshot_age <= self.config.max_stale_seconds
        )

        return CollectorHealth(
            status=self._status.value,
            pid=os.getpid() if self._status == CollectorStatus.RUNNING else None,
            session_id=self.session_id,
            environment=self.config.environment,
            started_at=self._started_at.isoformat() if self._started_at else None,
            stopped_at=self._stopped_at.isoformat() if self._stopped_at else None,
            last_successful_collection_time=(
                self._last_successful_collection_time.isoformat()
                if self._last_successful_collection_time
                else None
            ),
            last_attempted_collection_time=(
                self._last_attempted_collection_time.isoformat()
                if self._last_attempted_collection_time
                else None
            ),
            latest_snapshot_timestamp=(
                self._latest_snapshot_timestamp.isoformat()
                if self._latest_snapshot_timestamp
                else None
            ),
            latest_snapshot_age_seconds=snapshot_age,
            is_data_fresh=is_fresh,
            consecutive_failures=self._consecutive_failures,
            successful_cycles_count=self._successful_cycles_count,
            failed_cycles_count=self._failed_cycles_count,
            total_cycles_count=self._cycle_index,
            total_observations_count=self._total_observations_count,
            active_monitored_markets_count=len(self._monitored_tickers),
            last_cycle_duration_seconds=self._last_cycle_duration,
            last_cycle_markets_polled=self._last_cycle_polled,
            last_cycle_markets_succeeded=self._last_cycle_succeeded,
            last_cycle_markets_failed=self._last_cycle_failed,
            last_error=self._last_error,
            last_error_code=self._last_error_code,
            rate_limit_count=self._rate_limit_count,
            poll_interval_seconds=self.config.poll_interval_seconds,
            max_stale_seconds=self.config.max_stale_seconds,
        )

    def _save_state(self) -> None:
        """Atomically persist current collector health to disk for API consumption."""
        if self._state_file_path is None:
            return
        try:
            self._state_file_path.parent.mkdir(parents=True, exist_ok=True)
            health_dict = self.get_health().to_dict()
            tmp_path = self._state_file_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(health_dict, f, indent=2)
            tmp_path.replace(self._state_file_path)
        except Exception as exc:
            logger.warning("Failed to atomically write collector state to %s: %s", self._state_file_path, exc)

    def _update_manifest(self) -> None:
        """Write cryptographic SHA-256 manifest of recorded evidence."""
        if self._manifest_file_path is None or self._evidence_file_path is None:
            return
        if not self._evidence_file_path.exists():
            return

        try:
            self._manifest_file_path.parent.mkdir(parents=True, exist_ok=True)
            content = self._evidence_file_path.read_bytes()
            sha256 = hashlib.sha256(content).hexdigest()
            manifest_data = {
                "session_id": self.session_id,
                "environment": self.config.environment,
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "updated_at": self._now().isoformat(),
                "total_records": self._total_observations_count,
                "files": {
                    self._evidence_file_path.name: {
                        "size_bytes": len(content),
                        "sha256": sha256,
                    }
                },
            }
            tmp_manifest = self._manifest_file_path.with_suffix(".tmp")
            with open(tmp_manifest, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, indent=2)
            tmp_manifest.replace(self._manifest_file_path)
        except Exception as exc:
            logger.warning("Failed to update evidence manifest: %s", exc)

    def check_order_book_freshness(
        self,
        book: Optional[NormalizedOrderBook],
        source_timestamp: Optional[datetime],
        reference_time: datetime,
    ) -> Tuple[bool, Optional[str]]:
        """Validate whether an order book's timestamp meets freshness constraints."""
        if book is None:
            return True, "No order book available"
        if source_timestamp is None:
            return True, "Missing exchange source timestamp"

        ref = reference_time
        src = source_timestamp
        if src.tzinfo is None and ref.tzinfo is not None:
            src = src.replace(tzinfo=timezone.utc)
        elif src.tzinfo is not None and ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)

        age_seconds = (ref - src).total_seconds()

        # Reject unreasonable future timestamps (>10.0s)
        if age_seconds < -10.0:
            return True, f"Source timestamp is from the future ({age_seconds:.1f}s)"

        # Reject stale timestamps
        if age_seconds > self.config.max_stale_seconds:
            return (
                True,
                f"Order book is stale (age: {age_seconds:.1f}s > max: {self.config.max_stale_seconds:.1f}s)",
            )

        return False, None

    def fetch_single_ticker(self, ticker: str) -> MarketObservation:
        """Fetch market metadata and L2 order book for a single ticker with retry and backoff."""
        now = self._now()
        obs_id = f"obs-{ticker}-{int(now.timestamp())}-{now.microsecond}"

        market: Optional[NormalizedMarket] = None
        book: Optional[NormalizedOrderBook] = None
        attempts = 0
        max_attempts = 1 + self.config.max_retries

        while attempts < max_attempts and not self._stop_event.is_set():
            attempts += 1
            try:
                # 1. Fetch market metadata
                market = self.client.get_market(ticker)
                # 2. Fetch order book
                book = self.client.get_order_book(ticker)
                break
            except KalshiHTTPError as exc:
                if exc.status_code == 429:
                    self._rate_limit_count += 1
                    backoff = min(
                        self.config.max_rate_limit_backoff_seconds,
                        self.config.rate_limit_backoff_seconds * (2 ** (attempts - 1)),
                    )
                    logger.warning("HTTP 429 Rate limit on %s. Backing off for %.2fs", ticker, backoff)
                    self._stop_event.wait(backoff)
                    if attempts >= max_attempts:
                        t_err = self._now()
                        return MarketObservation(
                            observation_id=obs_id,
                            ticker=ticker,
                            observed_at=t_err,
                            source_timestamp=None,
                            market=market,
                            order_book=None,
                            is_success=False,
                            is_stale=True,
                            staleness_reason="HTTP 429 Rate Limit",
                            error_message=str(exc),
                            source_transport="REST_CONTINUOUS_V20",
                            local_receive_time=t_err,
                            sync_state="FAILED",
                            session_id=self.session_id,
                        )
                elif 500 <= exc.status_code <= 599:
                    backoff = self.config.retry_backoff_factor * (2 ** (attempts - 1))
                    logger.warning("Server error %d on %s. Retrying in %.2fs", exc.status_code, ticker, backoff)
                    self._stop_event.wait(backoff)
                else:
                    # Non-retryable client error (e.g. 404)
                    t_err = self._now()
                    return MarketObservation(
                        observation_id=obs_id,
                        ticker=ticker,
                        observed_at=t_err,
                        source_timestamp=None,
                        market=None,
                        order_book=None,
                        is_success=False,
                        is_stale=True,
                        staleness_reason=f"HTTP {exc.status_code}",
                        error_message=str(exc),
                        source_transport="REST_CONTINUOUS_V20",
                        local_receive_time=t_err,
                        sync_state="FAILED",
                        session_id=self.session_id,
                    )
            except (KalshiTransportError, TimeoutError, OSError) as exc:
                backoff = self.config.retry_backoff_factor * (2 ** (attempts - 1))
                logger.warning("Transport error fetching %s: %s. Retrying in %.2fs", ticker, exc, backoff)
                self._stop_event.wait(backoff)
            except MarketDataInputError as exc:
                # Malformed payload from exchange
                t_err = self._now()
                return MarketObservation(
                    observation_id=obs_id,
                    ticker=ticker,
                    observed_at=t_err,
                    source_timestamp=None,
                    market=market,
                    order_book=None,
                    is_success=False,
                    is_stale=True,
                    staleness_reason="Malformed market data payload",
                    error_message=str(exc),
                    source_transport="REST_CONTINUOUS_V20",
                    local_receive_time=t_err,
                    sync_state="FAILED",
                    session_id=self.session_id,
                )
            except Exception as exc:
                t_err = self._now()
                return MarketObservation(
                    observation_id=obs_id,
                    ticker=ticker,
                    observed_at=t_err,
                    source_timestamp=None,
                    market=market,
                    order_book=None,
                    is_success=False,
                    is_stale=True,
                    staleness_reason="Unexpected error",
                    error_message=str(exc),
                    source_transport="REST_CONTINUOUS_V20",
                    local_receive_time=t_err,
                    sync_state="FAILED",
                    session_id=self.session_id,
                )

        if book is None:
            t_err = self._now()
            return MarketObservation(
                observation_id=obs_id,
                ticker=ticker,
                observed_at=t_err,
                source_timestamp=None,
                market=market,
                order_book=None,
                is_success=False,
                is_stale=True,
                staleness_reason="Exceeded maximum retries",
                error_message="Order book could not be fetched within max_retries",
                source_transport="REST_CONTINUOUS_V20",
                local_receive_time=t_err,
                sync_state="FAILED",
                session_id=self.session_id,
            )

        t_recv = self._now()
        source_ts = book.source_timestamp
        is_stale, stale_reason = self.check_order_book_freshness(book, source_ts, t_recv)

        return MarketObservation(
            observation_id=obs_id,
            ticker=ticker,
            observed_at=t_recv,
            source_timestamp=source_ts,
            market=market,
            order_book=book,
            is_success=True,
            is_stale=is_stale,
            staleness_reason=stale_reason,
            error_message=None,
            source_transport="REST_CONTINUOUS_V20",
            local_receive_time=t_recv,
            sync_state="SYNCHRONIZED" if not is_stale else "UNSYNCHRONIZED",
            session_id=self.session_id,
        )

    def discover_markets(self, force: bool = False) -> Tuple[str, ...]:
        """Discover active open markets from Kalshi catalog if target_tickers was not fixed."""
        if self.config.target_tickers:
            self._monitored_tickers = tuple(self.config.target_tickers)
            return self._monitored_tickers

        now_mono = time.monotonic()
        if (
            not force
            and self._monitored_tickers
            and (now_mono - self._last_discovery_time) < self.config.discovery_refresh_interval_seconds
        ):
            return self._monitored_tickers

        try:
            markets = self.client.list_markets(
                status="open",
                max_markets=self.config.max_markets_to_poll,
            )
            tickers = tuple(m.ticker for m in markets)
            if tickers:
                self._monitored_tickers = tickers
                self._last_discovery_time = now_mono
                logger.info("Discovered %d active markets for continuous collection", len(tickers))
        except Exception as exc:
            logger.warning("Market discovery query failed: %s", exc)
            if not self._monitored_tickers:
                self._last_error = str(exc)
                self._last_error_code = "DISCOVERY_ERROR"

        return self._monitored_tickers

    def run_cycle(self) -> Tuple[List[MarketObservation], CollectorCycleSummary]:
        """Execute a single, thread-safe, non-overlapping collection cycle."""
        # Enforce non-overlapping single-flight execution
        acquired = self._cycle_lock.acquire(blocking=False)
        if not acquired:
            logger.warning("Previous collection cycle is still running. Skipping overlapping trigger.")
            now = self._now()
            summary = CollectorCycleSummary(
                cycle_index=self._cycle_index,
                started_at=now,
                completed_at=now,
                duration_seconds=0.0,
                markets_polled=0,
                markets_succeeded=0,
                markets_failed=0,
                markets_stale=0,
                is_success=False,
                error_message="Skipped overlapping cycle",
            )
            return [], summary

        try:
            self._cycle_index += 1
            cycle_idx = self._cycle_index
            cycle_start = self._now()
            cycle_mono_start = time.monotonic()
            self._last_attempted_collection_time = cycle_start

            # 1. Ensure target universe is active
            tickers = self.discover_markets()
            if not tickers:
                elapsed = time.monotonic() - cycle_mono_start
                cycle_end = self._now()
                self._consecutive_failures += 1
                self._failed_cycles_count += 1
                self._last_error = "No active market tickers available"
                self._last_error_code = "EMPTY_UNIVERSE"
                self._last_cycle_duration = elapsed
                self._last_cycle_polled = 0
                self._last_cycle_succeeded = 0
                self._last_cycle_failed = 0
                self._save_state()

                summary = CollectorCycleSummary(
                    cycle_index=cycle_idx,
                    started_at=cycle_start,
                    completed_at=cycle_end,
                    duration_seconds=elapsed,
                    markets_polled=0,
                    markets_succeeded=0,
                    markets_failed=0,
                    markets_stale=0,
                    is_success=False,
                    error_message=self._last_error,
                )
                self._recent_summaries.append(summary)
                return [], summary

            # Bounded targets per cycle
            target_tickers = list(tickers[: self.config.max_markets_to_poll])
            observations: List[MarketObservation] = []

            # 2. Concurrently fetch observations using bounded worker pool
            workers = min(len(target_tickers), self.config.concurrency)
            with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                future_to_ticker = {
                    executor.submit(self.fetch_single_ticker, t): t
                    for t in target_tickers
                }
                for fut in as_completed(future_to_ticker):
                    if self._stop_event.is_set():
                        break
                    obs = fut.result()
                    observations.append(obs)

            # Sort observations deterministically by ticker order
            ticker_order = {t: idx for idx, t in enumerate(target_tickers)}
            observations.sort(key=lambda o: ticker_order.get(o.ticker, 999999))

            # 3. Analyze cycle results
            succeeded = sum(1 for o in observations if o.is_success)
            failed = sum(1 for o in observations if not o.is_success)
            stale = sum(1 for o in observations if o.is_stale)
            elapsed = time.monotonic() - cycle_mono_start
            cycle_end = self._now()

            cycle_is_success = succeeded > 0

            if cycle_is_success:
                self._last_successful_collection_time = cycle_end
                self._consecutive_failures = 0
                self._successful_cycles_count += 1
                self._last_error = None
                self._last_error_code = None

                # Find latest source timestamp among successful fresh observations
                valid_src_timestamps = [
                    o.source_timestamp for o in observations
                    if o.is_success and o.source_timestamp is not None and not o.is_stale
                ]
                if valid_src_timestamps:
                    self._latest_snapshot_timestamp = max(valid_src_timestamps)
            else:
                self._consecutive_failures += 1
                self._failed_cycles_count += 1
                self._last_error = f"Cycle {cycle_idx} failed: 0 of {len(observations)} succeeded"
                self._last_error_code = "ALL_MARKETS_FAILED"

            self._total_observations_count += len(observations)
            self._last_cycle_duration = elapsed
            self._last_cycle_polled = len(observations)
            self._last_cycle_succeeded = succeeded
            self._last_cycle_failed = failed

            # 4. Memory buffer management (strictly bounded deque)
            for o in observations:
                self._recent_observations.append(o)

            # 5. Persist evidence to disk
            if self._evidence_file_path is not None and observations:
                with self._file_lock:
                    with open(self._evidence_file_path, "a", encoding="utf-8") as f:
                        for o in observations:
                            f.write(json.dumps(observation_to_dict(o)) + "\n")

            # Periodic manifest update
            if self._manifest_file_path is not None and (cycle_idx % self.config.manifest_interval_cycles == 0 or not cycle_is_success):
                self._update_manifest()

            # Save state
            self._save_state()

            summary = CollectorCycleSummary(
                cycle_index=cycle_idx,
                started_at=cycle_start,
                completed_at=cycle_end,
                duration_seconds=elapsed,
                markets_polled=len(observations),
                markets_succeeded=succeeded,
                markets_failed=failed,
                markets_stale=stale,
                is_success=cycle_is_success,
                error_message=self._last_error,
            )
            self._recent_summaries.append(summary)
            self._notify_cycle_listeners(observations, summary)
            return observations, summary

        finally:
            self._cycle_lock.release()

    def run(self, max_cycles: Optional[int] = None) -> None:
        """Execute the continuous live collection loop until stopped or max_cycles reached."""
        self._status = CollectorStatus.RUNNING
        self._started_at = self._now()
        self._stop_event.clear()
        self._save_state()

        cycle_limit = max_cycles if max_cycles is not None else self.config.max_cycles
        logger.info(
            "Continuous live collector started (session: %s, interval: %.2fs, concurrency: %d)",
            self.session_id,
            self.config.poll_interval_seconds,
            self.config.concurrency,
        )

        try:
            while not self._stop_event.is_set():
                if cycle_limit is not None and self._cycle_index >= cycle_limit:
                    logger.info("Reached maximum cycles limit (%d). Terminating continuous loop.", cycle_limit)
                    break

                t_cycle_start = time.monotonic()
                _, summary = self.run_cycle()

                if cycle_limit is not None and self._cycle_index >= cycle_limit:
                    break

                # Pacing sleep with adaptive duration
                cycle_elapsed = time.monotonic() - t_cycle_start
                remaining_sleep = max(0.0, self.config.poll_interval_seconds - cycle_elapsed)

                if remaining_sleep > 0:
                    self._stop_event.wait(remaining_sleep)
                else:
                    logger.debug(
                        "Cycle duration (%.2fs) exceeded poll interval (%.2fs); starting next cycle immediately.",
                        cycle_elapsed,
                        self.config.poll_interval_seconds,
                    )

        except KeyboardInterrupt:
            logger.info("Continuous collector received KeyboardInterrupt. Initiating clean shutdown...")
        except Exception as exc:
            self._status = CollectorStatus.FAILED
            self._last_error = f"Fatal collector exception: {exc}"
            self._last_error_code = "FATAL_EXCEPTION"
            logger.exception("Fatal exception in continuous collector: %s", exc)
        finally:
            self._cleanup_on_stop()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Signal the continuous collector to gracefully shut down."""
        if self._status in (CollectorStatus.STOPPED, CollectorStatus.STOPPING):
            return

        self._status = CollectorStatus.STOPPING
        self._stop_event.set()
        logger.info("Stopping continuous collector...")

        # Wait briefly for in-flight cycle lock if active
        acquired = self._cycle_lock.acquire(timeout=timeout_seconds)
        if acquired:
            try:
                pass
            finally:
                self._cycle_lock.release()

        self._cleanup_on_stop()

    def _cleanup_on_stop(self) -> None:
        """Perform final evidence flushing, manifest updates, and state persistence on shutdown."""
        with self._cycle_lock:
            if self._status == CollectorStatus.STOPPED:
                return
            self._status = CollectorStatus.STOPPED
            self._stopped_at = self._now()
            self._update_manifest()
            self._save_state()
            logger.info(
                "Continuous collector stopped cleanly. Total cycles: %d, Total observations: %d",
                self._cycle_index,
                self._total_observations_count,
            )

    def get_recent_observations(self) -> List[MarketObservation]:
        """Return a copy of the recent in-memory observations."""
        return list(self._recent_observations)

    def get_recent_summaries(self) -> List[CollectorCycleSummary]:
        """Return a copy of recent cycle summaries."""
        return list(self._recent_summaries)
