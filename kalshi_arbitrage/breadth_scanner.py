"""Broad Market Catalog Discovery and Cursor Pagination Engine for Kalshi Arbitrage.

Phase 2A implementation of the Breadth-First Scanner architecture:
- Paginates Kalshi public event and market catalogs using API continuation cursors.
- Discovers hundreds to thousands of markets across all economic categories.
- Normalizes events and nested markets with zero L2 order-book overhead.
- Classifies markets into eligible vs excluded with detailed breakdown reasons.
- Reconciles catalog changes across refresh cycles (new, updated, unchanged, closed).
- Fail-soft retry policies, pagination loop detection, and isolation guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
import copy
import hashlib
import json
import logging
from math import isfinite
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union
import uuid

from .contract import NO, YES
from .execution_qualifier import (
    CandidateQualification,
    ExecutionAwareArbitrageQualifier,
    ExecutionQualifierConfig,
    LegExecutionDetail,
    QualificationOutcome,
)
from .fee_model import FeeModelConfig, FeeScheduleType
from .market_data import (
    MarketDataInputError,
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    _optional_market_decimal,
    _required_text,
    _require_mapping,
    normalize_event,
    normalize_market,
)
from .observer import DeclaredMeceBasket, MarketObservation
from .rest_client import (
    DEFAULT_MARKET_PAGE_SIZE,
    MAX_MARKET_PAGE_SIZE,
    HTTPResponse,
    KalshiClientError,
    KalshiHTTPError,
    KalshiJSONError,
    KalshiPaginationError,
    KalshiResponseError,
    KalshiRestClient,
    KalshiTransportError,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Exclusion Reasons & Eligibility Models
# -----------------------------------------------------------------------------

class MarketExclusionReason(str, Enum):
    """Reason a discovered market is excluded from arbitrage evaluation."""

    STATUS_NOT_OPEN = "STATUS_NOT_OPEN"
    NON_BINARY_TYPE = "NON_BINARY_TYPE"
    MISSING_TICKER = "MISSING_TICKER"
    MISSING_PRICES = "MISSING_PRICES"
    PAST_CLOSE_TIME = "PAST_CLOSE_TIME"
    CATEGORY_FILTERED = "CATEGORY_FILTERED"
    SERIES_FILTERED = "SERIES_FILTERED"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"


@dataclass(frozen=True)
class DiscoveredMarketQuote(NormalizedMarket):
    """Lightweight discovered market quote model for high-throughput Stage A discovery.

    Retains only essential fields needed for Stage B arbitrage screening, avoiding
    expensive serialization and timestamp parsing of unused contract metadata.
    Fully compatible with NormalizedMarket.
    """

    def __post_init__(self) -> None:
        if not self.ticker:
            raise MarketDataInputError("ticker must be a non-empty string")
        if self.market_type != "binary":
            raise MarketDataInputError("only binary markets are supported")
        if not self.status:
            raise MarketDataInputError("status must be a non-empty string")


def fast_parse_iso_timestamp(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 timestamp with C-accelerated fast path."""
    if not ts_str:
        return None
    try:
        s = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
    return None


def fast_normalize_discovered_market(
    raw_market: Mapping[str, Any],
    close_dt: Optional[datetime] = None,
) -> DiscoveredMarketQuote:
    """Normalize a raw market mapping into a lightweight DiscoveredMarketQuote."""
    raw = _require_mapping(raw_market, "market payload")
    market = raw.get("market", raw)
    market = _require_mapping(market, "market")

    ticker = _required_text(market.get("ticker"), "ticker")
    event_ticker = _required_text(market.get("event_ticker"), "event_ticker")
    market_type = _required_text(market.get("market_type"), "market_type")
    if market_type != "binary":
        raise MarketDataInputError(f"unsupported market_type: {market_type}")
    status = _required_text(market.get("status"), "status")
    title = str(market.get("title") or ticker)

    if close_dt is None:
        close_dt = fast_parse_iso_timestamp(market.get("close_time"))
    updated_dt = fast_parse_iso_timestamp(market.get("updated_time"))

    yes_bid = _optional_market_decimal(market, "yes_bid_dollars", "price")
    yes_ask = _optional_market_decimal(market, "yes_ask_dollars", "price")
    no_bid = _optional_market_decimal(market, "no_bid_dollars", "price")
    no_ask = _optional_market_decimal(market, "no_ask_dollars", "price")
    last_price = _optional_market_decimal(market, "last_price_dollars", "price")
    yes_bid_size = _optional_market_decimal(market, "yes_bid_size_fp", "quantity")
    yes_ask_size = _optional_market_decimal(market, "yes_ask_size_fp", "quantity")

    return DiscoveredMarketQuote(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type=market_type,
        status=status,
        title=title,
        close_time=close_dt,
        updated_time=updated_dt,
        yes_bid_dollars=yes_bid,
        yes_ask_dollars=yes_ask,
        no_bid_dollars=no_bid,
        no_ask_dollars=no_ask,
        last_price_dollars=last_price,
        yes_bid_size=yes_bid_size,
        yes_ask_size=yes_ask_size,
    )


# -----------------------------------------------------------------------------
# Scanner Configuration
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class BreadthScannerConfig:
    """Configuration for breadth-first catalog discovery and pagination."""

    environment: str = "production"
    discovery_page_size: int = 100
    max_pages_per_pass: Optional[int] = None
    status_filter: Optional[str] = "open"
    category_filter: Optional[Tuple[str, ...]] = None
    series_ticker_filter: Optional[Tuple[str, ...]] = None
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    request_timeout_seconds: float = 10.0
    with_nested_markets: bool = True
    catalog_source: str = "events"  # "events" (recommended) or "markets"
    catalog_refresh_interval_seconds: float = 300.0
    require_top_of_book_quotes: bool = False
    max_stale_seconds: float = 60.0
    max_leg_skew_seconds: float = 5.0
    raise_on_error: bool = False
    state_file: Optional[Path] = None
    candidates_file: Optional[Path] = None
    target_quantity: int = 1
    fee_schedule: FeeScheduleType = FeeScheduleType.KALSHI_OFFICIAL_TAKER
    enable_alerts: bool = False
    max_requests_per_second: Optional[float] = None

    def __post_init__(self) -> None:
        if self.max_requests_per_second is not None and self.max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be positive")
        if self.environment not in ("production", "demo"):
            raise ValueError("environment must be 'production' or 'demo'")
        if isinstance(self.discovery_page_size, bool) or not isinstance(self.discovery_page_size, int):
            raise ValueError("discovery_page_size must be an integer")
        if not 1 <= self.discovery_page_size <= MAX_MARKET_PAGE_SIZE:
            raise ValueError(f"discovery_page_size must be between 1 and {MAX_MARKET_PAGE_SIZE}")
        if self.max_pages_per_pass is not None:
            if isinstance(self.max_pages_per_pass, bool) or not isinstance(self.max_pages_per_pass, int):
                raise ValueError("max_pages_per_pass must be an integer")
            if self.max_pages_per_pass <= 0:
                raise ValueError("max_pages_per_pass must be positive")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if isinstance(self.retry_backoff_seconds, bool) or not isinstance(self.retry_backoff_seconds, (int, float)):
            raise ValueError("retry_backoff_seconds must be a number")
        if self.retry_backoff_seconds < 0 or not isfinite(float(self.retry_backoff_seconds)):
            raise ValueError("retry_backoff_seconds must be non-negative")
        if self.catalog_source not in ("events", "markets"):
            raise ValueError("catalog_source must be 'events' or 'markets'")
        if isinstance(self.max_stale_seconds, bool) or not isinstance(self.max_stale_seconds, (int, float)) or self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if isinstance(self.max_leg_skew_seconds, bool) or not isinstance(self.max_leg_skew_seconds, (int, float)) or self.max_leg_skew_seconds <= 0:
            raise ValueError("max_leg_skew_seconds must be positive")
        if self.category_filter is not None and not isinstance(self.category_filter, tuple):
            object.__setattr__(self, "category_filter", tuple(self.category_filter))
        if self.series_ticker_filter is not None and not isinstance(self.series_ticker_filter, tuple):
            object.__setattr__(self, "series_ticker_filter", tuple(self.series_ticker_filter))


# -----------------------------------------------------------------------------
# Discovery Result & Telemetry Models
# -----------------------------------------------------------------------------

@dataclass
class CatalogDiscoveryResult:
    """Result and telemetry of a catalog discovery execution pass."""

    session_id: str
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0
    is_complete: bool = False
    error_message: Optional[str] = None
    pages_fetched: int = 0
    events_discovered_count: int = 0
    unique_events_count: int = 0
    markets_discovered_count: int = 0
    unique_markets_count: int = 0
    eligible_markets_count: int = 0
    excluded_markets_count: int = 0
    exclusion_breakdown: Dict[str, int] = field(default_factory=dict)
    category_distribution: Dict[str, int] = field(default_factory=dict)
    request_attempts: int = 0
    request_failures: int = 0
    retries_count: int = 0
    new_markets_count: int = 0
    updated_markets_count: int = 0
    unchanged_markets_count: int = 0
    closed_markets_count: int = 0
    fresh_quotes_count: int = 0
    stale_or_missing_quotes_count: int = 0
    discovered_events: Dict[str, NormalizedEvent] = field(default_factory=dict)
    discovered_markets: Dict[str, NormalizedMarket] = field(default_factory=dict)
    eligible_market_tickers: Tuple[str, ...] = ()
    network_time_seconds: float = 0.0
    json_parse_time_seconds: float = 0.0
    normalization_time_seconds: float = 0.0
    deduplication_time_seconds: float = 0.0
    screening_time_seconds: float = 0.0
    total_discovery_time_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Produce a structured, JSON-serializable telemetry dictionary."""
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 4),
            "is_complete": self.is_complete,
            "error_message": self.error_message,
            "pages_fetched": self.pages_fetched,
            "events_discovered_count": self.events_discovered_count,
            "unique_events_count": self.unique_events_count,
            "markets_discovered_count": self.markets_discovered_count,
            "unique_markets_count": self.unique_markets_count,
            "eligible_markets_count": self.eligible_markets_count,
            "excluded_markets_count": self.excluded_markets_count,
            "exclusion_breakdown": dict(self.exclusion_breakdown),
            "category_distribution": dict(self.category_distribution),
            "request_attempts": self.request_attempts,
            "request_failures": self.request_failures,
            "retries_count": self.retries_count,
            "new_markets_count": self.new_markets_count,
            "updated_markets_count": self.updated_markets_count,
            "unchanged_markets_count": self.unchanged_markets_count,
            "closed_markets_count": self.closed_markets_count,
            "fresh_quotes_count": self.fresh_quotes_count,
            "stale_or_missing_quotes_count": self.stale_or_missing_quotes_count,
            "eligible_market_tickers_count": len(self.eligible_market_tickers),
            "eligible_market_tickers": list(self.eligible_market_tickers),
            "network_time_seconds": round(self.network_time_seconds, 4),
            "json_parse_time_seconds": round(self.json_parse_time_seconds, 4),
            "normalization_time_seconds": round(self.normalization_time_seconds, 4),
            "deduplication_time_seconds": round(self.deduplication_time_seconds, 4),
            "screening_time_seconds": round(self.screening_time_seconds, 4),
            "total_discovery_time_seconds": round(self.total_discovery_time_seconds, 4),
        }

    def get_summary(self) -> Dict[str, Any]:
        """Convenience alias for to_dict()."""
        return self.to_dict()


# -----------------------------------------------------------------------------
# Rate Limiting
# -----------------------------------------------------------------------------

class TokenBucketRateLimiter:
    """Thread-safe rate limiter using atomic ticket scheduling.

    Guarantees that outbound requests never exceed max_requests_per_second
    across any number of concurrent worker threads without burst spikes or 429 rate limit errors.
    """

    def __init__(
        self,
        rate_per_second: float = 5.0,
        time_provider: Optional[Callable[[], float]] = None,
        sleep_func: Optional[Callable[[float], None]] = None,
    ) -> None:
        if rate_per_second < 0:
            raise ValueError("rate_per_second must be non-negative")
        self.rate_per_second = float(rate_per_second)
        self._time = time_provider or time.monotonic
        self._sleep = sleep_func or time.sleep
        self._lock = threading.Lock()
        self._next_available_time: float = 0.0

    def acquire(self) -> float:
        """Acquire permission to send a request, sleeping if necessary.

        Returns the duration in seconds that was slept.
        """
        if self.rate_per_second <= 0:
            return 0.0

        interval = 1.0 / self.rate_per_second
        with self._lock:
            now = self._time()
            if self._next_available_time < now:
                self._next_available_time = now
            ticket_time = self._next_available_time
            self._next_available_time += interval

        now = self._time()
        sleep_duration = ticket_time - now
        if sleep_duration > 0:
            self._sleep(sleep_duration)
            return sleep_duration
        return 0.0


# -----------------------------------------------------------------------------
# Catalog Discovery Engine
# -----------------------------------------------------------------------------

class CatalogDiscoveryEngine:
    """High-throughput catalog discovery engine driving cursor pagination across Kalshi markets."""

    def __init__(
        self,
        client: Optional[KalshiRestClient] = None,
        config: Optional[BreadthScannerConfig] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
        sleep_func: Optional[Callable[[float], None]] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self.config = config or BreadthScannerConfig()
        self.client = client or KalshiRestClient(
            environment=self.config.environment,
            timeout_seconds=self.config.request_timeout_seconds,
        )
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_func or time.sleep
        self._rate_limiter = rate_limiter or (
            TokenBucketRateLimiter(
                rate_per_second=self.config.max_requests_per_second,
                sleep_func=self._sleep,
            )
            if getattr(self.config, "max_requests_per_second", None) is not None
            else None
        )
        self._is_stopped = False

        self.last_result: Optional[CatalogDiscoveryResult] = None
        self.known_markets: Dict[str, NormalizedMarket] = {}
        self.known_events: Dict[str, NormalizedEvent] = {}
        self.active_tickers: Set[str] = set()

    @property
    def rate_limiter(self) -> Optional[TokenBucketRateLimiter]:
        """Active rate limiter instance, if configured."""
        return self._rate_limiter

    def _rate_limit(self) -> float:
        """Enforce configured requests-per-second rate limit before dispatch."""
        if self._rate_limiter is not None:
            return self._rate_limiter.acquire()
        return 0.0

    def stop(self) -> None:
        """Cooperatively request discovery loop termination."""
        self._is_stopped = True

    def reset_stop(self) -> None:
        """Reset stop flag for subsequent execution."""
        self._is_stopped = False

    def _now(self) -> datetime:
        return self._time_provider()

    def _evaluate_market_eligibility(
        self,
        raw_market: Mapping[str, Any],
        event_category: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[Optional[NormalizedMarket], Optional[MarketExclusionReason]]:
        """Evaluate raw market dictionary against eligibility rules."""
        eval_now = now or self._now()

        # 1. Ticker presence
        ticker = raw_market.get("ticker")
        if not ticker or not isinstance(ticker, str) or not ticker.strip():
            return None, MarketExclusionReason.MISSING_TICKER

        # 2. Market type binary check
        raw_type = raw_market.get("market_type")
        if raw_type is not None and str(raw_type).strip().lower() != "binary":
            return None, MarketExclusionReason.NON_BINARY_TYPE

        # 3. Status open check
        status = raw_market.get("status")
        if status is not None and str(status).strip().lower() not in ("open", "active"):
            return None, MarketExclusionReason.STATUS_NOT_OPEN

        # 4. Expiration / close time check
        close_time_str = raw_market.get("close_time")
        close_dt: Optional[datetime] = None
        if close_time_str:
            close_dt = fast_parse_iso_timestamp(close_time_str)
            if close_dt is not None:
                if close_dt < eval_now:
                    return None, MarketExclusionReason.PAST_CLOSE_TIME
            else:
                for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
                    try:
                        close_dt = datetime.strptime(close_time_str, fmt)
                        if close_dt < eval_now:
                            return None, MarketExclusionReason.PAST_CLOSE_TIME
                        break
                    except ValueError:
                        continue

        # 5. Category filter check
        if self.config.category_filter is not None and event_category is not None:
            if event_category not in self.config.category_filter:
                return None, MarketExclusionReason.CATEGORY_FILTERED

        # 6. Series filter check
        if self.config.series_ticker_filter is not None:
            m_series = raw_market.get("series_ticker")
            if m_series and m_series not in self.config.series_ticker_filter:
                return None, MarketExclusionReason.SERIES_FILTERED

        # 7. Quote completeness check (if configured)
        if self.config.require_top_of_book_quotes:
            has_quote = any(
                raw_market.get(q) is not None
                for q in ("yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars")
            )
            if not has_quote:
                return None, MarketExclusionReason.MISSING_PRICES

        # 8. Attempt lightweight normalization
        try:
            norm = fast_normalize_discovered_market(raw_market, close_dt=close_dt)
            return norm, None
        except MarketDataInputError as exc:
            msg = str(exc).lower()
            if "binary" in msg:
                return None, MarketExclusionReason.NON_BINARY_TYPE
            logger.debug("Market %s failed normalization: %s", ticker, exc)
            return None, MarketExclusionReason.NORMALIZATION_ERROR
        except Exception as exc:
            logger.debug("Unexpected error normalizing market %s: %s", ticker, exc)
            return None, MarketExclusionReason.NORMALIZATION_ERROR

    def _fetch_page(self, endpoint: str, params: Dict[str, str]) -> Tuple[Mapping[str, Any], Optional[HTTPResponse]]:
        """Fetch a single JSON page from Kalshi REST API via client transport."""
        if hasattr(self.client, "_get_json_response"):
            return self.client._get_json_response(endpoint, params)
        if hasattr(self.client, "_get_json"):
            payload = self.client._get_json(endpoint, params)
            return payload, None
        raise RuntimeError("Configured client does not support JSON retrieval")

    def discover_catalog(
        self,
        force: bool = False,
        starting_cursor: Optional[str] = None,
        raise_on_error: Optional[bool] = None,
        deadline: Optional[float] = None,
    ) -> CatalogDiscoveryResult:
        """Execute a breadth-first catalog discovery pass using cursor pagination.

        Paginates through events (with nested markets) or markets until all cursors
        are exhausted or max_pages_per_pass is reached. Reconciles discovered markets
        against known state and tracks telemetry.
        """
        start_time = self._now()
        session_id = f"disc-{uuid.uuid4().hex[:8]}"
        should_raise = self.config.raise_on_error if raise_on_error is None else raise_on_error

        result = CatalogDiscoveryResult(
            session_id=session_id,
            started_at=start_time.isoformat(),
        )

        seen_cursors: Set[str] = set()
        current_cursor: Optional[str] = starting_cursor
        is_events_source = self.config.catalog_source == "events"
        endpoint = "/events" if is_events_source else "/markets"

        discovered_events: Dict[str, NormalizedEvent] = {}
        discovered_markets: Dict[str, NormalizedMarket] = {}
        seen_market_tickers: Set[str] = set()
        excluded_market_tickers: Set[str] = set()
        eligible_tickers: List[str] = []
        exclusion_breakdown: Dict[str, int] = {}
        category_distribution: Dict[str, int] = {}

        pages_fetched = 0
        req_attempts = 0
        req_failures = 0
        retries_count = 0
        error_message: Optional[str] = None
        is_complete = False

        total_network_time = 0.0
        total_json_parse_time = 0.0
        total_normalization_time = 0.0
        total_deduplication_time = 0.0

        while True:
            if self._is_stopped:
                logger.info("Discovery aborted cooperatively by stop request.")
                break

            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Catalog discovery halted: run duration deadline reached.")
                is_complete = False
                error_message = "HALTED_BY_RUN_DURATION_DEADLINE"
                break

            if self.config.max_pages_per_pass is not None and pages_fetched >= self.config.max_pages_per_pass:
                logger.debug("Reached max_pages_per_pass limit (%d).", self.config.max_pages_per_pass)
                is_complete = True
                break

            # Construct query params
            params: Dict[str, str] = {"limit": str(self.config.discovery_page_size)}
            if self.config.status_filter:
                params["status"] = self.config.status_filter
            if is_events_source and self.config.with_nested_markets:
                params["with_nested_markets"] = "true"
            if self.config.series_ticker_filter and len(self.config.series_ticker_filter) == 1:
                params["series_ticker"] = self.config.series_ticker_filter[0]
            if current_cursor:
                params["cursor"] = current_cursor

            # Request page with retries
            payload: Optional[Mapping[str, Any]] = None
            attempt = 0
            while attempt <= self.config.max_retries:
                req_attempts += 1
                self._rate_limit()
                t_fetch_start = time.monotonic()
                try:
                    payload, _ = self._fetch_page(endpoint, params)
                    t_fetch_end = time.monotonic()
                    net_dur = getattr(self.client, "_last_network_duration", getattr(self.client, "last_network_duration", 0.0))
                    json_dur = getattr(self.client, "_last_json_duration", getattr(self.client, "last_json_duration", 0.0))
                    if net_dur > 0 or json_dur > 0:
                        total_network_time += net_dur
                        total_json_parse_time += json_dur
                    else:
                        total_network_time += max(0.0, t_fetch_end - t_fetch_start)
                    break
                except (KalshiTransportError, KalshiHTTPError, KalshiJSONError, KalshiResponseError) as exc:
                    t_fetch_end = time.monotonic()
                    total_network_time += max(0.0, t_fetch_end - t_fetch_start)
                    req_failures += 1
                    attempt += 1

                    # Check for invalid or expired cursor or unretryable 4xx client errors
                    if isinstance(exc, KalshiHTTPError):
                        err_body = getattr(exc, "body", "")
                        body_lower = f"{exc} {err_body}".lower()
                        if exc.status_code == 400 and current_cursor and "cursor" in body_lower:
                            logger.warning("Pagination cursor invalid or expired: %s (body=%s)", exc, err_body)
                            error_message = f"Cursor expired or invalid: {exc} - {err_body}"
                            if should_raise:
                                raise
                            break
                        if 400 <= exc.status_code < 500 and exc.status_code != 429:
                            # 4xx client errors (other than rate limits) are non-retryable
                            logger.warning("Non-retryable client error on %s: %s (body=%s)", endpoint, exc, err_body)
                            error_message = f"Client error HTTP {exc.status_code}: {exc} - {err_body}"
                            if should_raise:
                                raise
                            break

                    if attempt > self.config.max_retries:
                        logger.warning("Pagination halted: retry budget exhausted on %s: %s", endpoint, exc)
                        error_message = f"Request failed after {attempt} attempts: {exc}"
                        if should_raise:
                            raise
                        break

                    if deadline is not None and time.monotonic() >= deadline:
                        logger.info("Pagination halted: run duration deadline reached during backoff.")
                        error_message = "HALTED_BY_RUN_DURATION_DEADLINE"
                        break

                    retries_count += 1
                    backoff = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                    logger.debug("Retrying request after %.2fs backoff (attempt %d): %s", backoff, attempt, exc)
                    self._sleep(backoff)
                except Exception as exc:
                    req_failures += 1
                    error_message = f"Unexpected failure: {exc}"
                    logger.error("Unexpected discovery exception: %s", exc, exc_info=True)
                    if should_raise:
                        raise
                    break

            if payload is None:
                # Failed to fetch page; retain partial discovery
                is_complete = False
                break

            pages_fetched += 1

            # Process payload items (instrument normalization time)
            t_norm_start = time.monotonic()
            if is_events_source:
                raw_events = payload.get("events")
                if not isinstance(raw_events, list):
                    err = KalshiResponseError("response events must be an array")
                    req_failures += 1
                    error_message = str(err)
                    if should_raise:
                        raise err
                    break

                for raw_event in raw_events:
                    if not isinstance(raw_event, dict):
                        continue
                    result.events_discovered_count += 1
                    ev_ticker = raw_event.get("event_ticker")
                    if not ev_ticker or not isinstance(ev_ticker, str):
                        continue

                    if ev_ticker not in discovered_events:
                        category = raw_event.get("category") or "Unknown"
                        category_distribution[category] = category_distribution.get(category, 0) + 1

                    try:
                        norm_ev = normalize_event({"event": raw_event})
                        discovered_events[ev_ticker] = norm_ev
                    except Exception:
                        pass

                    nested_mkts = raw_event.get("markets") or []
                    for raw_mkt in nested_mkts:
                        if not isinstance(raw_mkt, dict):
                            continue
                        result.markets_discovered_count += 1
                        m_ticker = raw_mkt.get("ticker")
                        if m_ticker and m_ticker in seen_market_tickers:
                            continue
                        if m_ticker:
                            seen_market_tickers.add(m_ticker)

                        norm_m, excl_reason = self._evaluate_market_eligibility(
                            raw_mkt,
                            event_category=raw_event.get("category"),
                            now=start_time,
                        )

                        if norm_m is not None:
                            discovered_markets[norm_m.ticker] = norm_m
                            if norm_m.ticker not in eligible_tickers:
                                eligible_tickers.append(norm_m.ticker)
                        else:
                            reason_key = excl_reason.value if excl_reason else "EXCLUDED"
                            exclusion_breakdown[reason_key] = exclusion_breakdown.get(reason_key, 0) + 1
            else:
                raw_markets = payload.get("markets")
                if not isinstance(raw_markets, list):
                    err = KalshiResponseError("response markets must be an array")
                    req_failures += 1
                    error_message = str(err)
                    if should_raise:
                        raise err
                    break

                for raw_mkt in raw_markets:
                    if not isinstance(raw_mkt, dict):
                        continue
                    result.markets_discovered_count += 1
                    m_ticker = raw_mkt.get("ticker")
                    if m_ticker and m_ticker in seen_market_tickers:
                        continue
                    if m_ticker:
                        seen_market_tickers.add(m_ticker)
                    norm_m, excl_reason = self._evaluate_market_eligibility(raw_mkt, now=start_time)
                    if norm_m is not None:
                        discovered_markets[norm_m.ticker] = norm_m
                        if norm_m.ticker not in eligible_tickers:
                            eligible_tickers.append(norm_m.ticker)
                    else:
                        reason_key = excl_reason.value if excl_reason else "EXCLUDED"
                        exclusion_breakdown[reason_key] = exclusion_breakdown.get(reason_key, 0) + 1
            total_normalization_time += max(0.0, time.monotonic() - t_norm_start)

            # Handle pagination cursor (instrument deduplication time)
            t_dedup_start = time.monotonic()
            returned_cursor = payload.get("cursor")
            if returned_cursor in (None, ""):
                # Terminal page reached
                is_complete = True
                total_deduplication_time += max(0.0, time.monotonic() - t_dedup_start)
                break

            if not isinstance(returned_cursor, str):
                err = KalshiResponseError("response cursor must be a string or null")
                req_failures += 1
                error_message = str(err)
                if should_raise:
                    raise err
                is_complete = False
                total_deduplication_time += max(0.0, time.monotonic() - t_dedup_start)
                break

            # Cursor loop detection
            if returned_cursor in seen_cursors or returned_cursor == current_cursor:
                err_msg = f"Pagination cursor repeated: {returned_cursor}"
                logger.warning(err_msg)
                error_message = err_msg
                if should_raise:
                    raise KalshiPaginationError("pagination cursor repeated")
                # When fail-soft, abort pagination loop without falsely claiming complete catalog
                is_complete = False
                total_deduplication_time += max(0.0, time.monotonic() - t_dedup_start)
                break

            seen_cursors.add(returned_cursor)
            current_cursor = returned_cursor
            total_deduplication_time += max(0.0, time.monotonic() - t_dedup_start)

        # Reconcile new vs updated vs unchanged markets
        t_recon_start = time.monotonic()
        new_count = 0
        updated_count = 0
        unchanged_count = 0

        for t, mkt in discovered_markets.items():
            prev = self.known_markets.get(t)
            if prev is None:
                new_count += 1
            else:
                is_modified = (
                    prev.yes_bid_dollars != mkt.yes_bid_dollars
                    or prev.yes_ask_dollars != mkt.yes_ask_dollars
                    or prev.no_bid_dollars != mkt.no_bid_dollars
                    or prev.no_ask_dollars != mkt.no_ask_dollars
                    or prev.status != mkt.status
                    or prev.close_time != mkt.close_time
                )
                if is_modified:
                    updated_count += 1
                else:
                    unchanged_count += 1

        # Reconcile quote freshness across discovered markets
        fresh_quotes_count = 0
        stale_or_missing_quotes_count = 0
        start_utc = start_time if start_time.tzinfo else start_time.replace(tzinfo=timezone.utc)

        for mkt in discovered_markets.values():
            has_quotes = (
                mkt.yes_ask_dollars is not None
                or mkt.no_ask_dollars is not None
                or mkt.yes_bid_dollars is not None
                or mkt.no_bid_dollars is not None
            )
            if has_quotes and mkt.updated_time is not None:
                m_ts = mkt.updated_time if mkt.updated_time.tzinfo else mkt.updated_time.replace(tzinfo=timezone.utc)
                age_s = max(0.0, (start_utc - m_ts).total_seconds())
                if age_s <= self.config.max_stale_seconds:
                    fresh_quotes_count += 1
                else:
                    stale_or_missing_quotes_count += 1
            else:
                stale_or_missing_quotes_count += 1

        # Reconcile closed markets only if full pass completed successfully
        closed_count = 0
        if is_complete and not starting_cursor:
            for t, prev_mkt in self.known_markets.items():
                if t not in discovered_markets:
                    closed_count += 1
            # On complete pass, update known_markets and active_tickers
            self.known_markets.update(discovered_markets)
            self.known_events.update(discovered_events)
            self.active_tickers = set(eligible_tickers)
        else:
            # On partial pass, do NOT wipe out existing markets; merge discovered ones
            self.known_markets.update(discovered_markets)
            self.known_events.update(discovered_events)
            self.active_tickers.update(eligible_tickers)

        total_deduplication_time += max(0.0, time.monotonic() - t_recon_start)

        end_time = self._now()
        duration = max(0.0, (end_time - start_time).total_seconds())

        result.completed_at = end_time.isoformat()
        result.duration_seconds = duration
        result.is_complete = is_complete
        result.error_message = error_message
        result.pages_fetched = pages_fetched
        result.unique_events_count = len(discovered_events)
        result.unique_markets_count = len(discovered_markets) + sum(exclusion_breakdown.values())
        result.eligible_markets_count = len(discovered_markets)
        result.excluded_markets_count = sum(exclusion_breakdown.values())
        result.exclusion_breakdown = exclusion_breakdown
        result.category_distribution = category_distribution
        result.request_attempts = req_attempts
        result.request_failures = req_failures
        result.retries_count = retries_count
        result.new_markets_count = new_count
        result.updated_markets_count = updated_count
        result.unchanged_markets_count = unchanged_count
        result.closed_markets_count = closed_count
        result.fresh_quotes_count = fresh_quotes_count
        result.stale_or_missing_quotes_count = stale_or_missing_quotes_count
        result.discovered_events = discovered_events
        result.discovered_markets = discovered_markets
        result.eligible_market_tickers = tuple(eligible_tickers)
        result.network_time_seconds = total_network_time
        result.json_parse_time_seconds = total_json_parse_time
        result.normalization_time_seconds = total_normalization_time
        result.deduplication_time_seconds = total_deduplication_time
        result.total_discovery_time_seconds = duration

        self.last_result = result
        return result

    def refresh_catalog(self, force: bool = True) -> CatalogDiscoveryResult:
        """Run a fresh catalog discovery pass and reconcile state."""
        return self.discover_catalog(force=force)

    def get_summary(self) -> Optional[Dict[str, Any]]:
        """Return structured summary of the most recent catalog discovery pass."""
        return self.last_result.to_dict() if self.last_result else None


# =============================================================================
# Phase 2B: Lightweight Breadth-First Arbitrage Screening
# =============================================================================

# -----------------------------------------------------------------------------
# Screening Outcomes & Strategy Constants
# -----------------------------------------------------------------------------

class ScreeningOutcome(str, Enum):
    """Explicit deterministic outcome for a screened market or contract group."""

    PRELIMINARY_CANDIDATE = "PRELIMINARY_CANDIDATE"
    NO_CANDIDATE = "NO_CANDIDATE"
    SKIPPED_UNSUPPORTED_MARKET = "SKIPPED_UNSUPPORTED_MARKET"
    SKIPPED_INCOMPLETE_QUOTES = "SKIPPED_INCOMPLETE_QUOTES"
    SKIPPED_INVALID_DATA = "SKIPPED_INVALID_DATA"
    SKIPPED_INCOMPLETE_EVENT_GROUP = "SKIPPED_INCOMPLETE_EVENT_GROUP"
    SKIPPED_STALE_QUOTES = "SKIPPED_STALE_QUOTES"


class ScreeningStrategyType(str, Enum):
    """Screening strategy identifiers."""

    BINARY_PARITY = "binary_parity"
    MECE_BASKET_LONG_YES = "mece_basket_long_yes"
    MECE_BASKET_LONG_NO = "mece_basket_long_no"
    CROSSED_BOOK = "crossed_book"


# Patterns indicating residual exhaustive outcomes
RESIDUAL_OUTCOME_PATTERNS = re.compile(
    r"\b(all\s+other|any\s+other|other|none|neither|someone\s+else|rest\s+of\s+field|field)\b",
    re.IGNORECASE,
)

# Known numeric range partition series prefixes (structural economic partitions)
RANGE_PARTITION_PREFIXES = (
    "KXFEDDECISION",
    "KXGDPYEAR",
    "KXUSCPIYEAR",
    "KXNOMGDPGROWTH",
    "KXU3EOY",
    "KXINX",
    "KXNASDAQ100",
)


# -----------------------------------------------------------------------------
# Preliminary Candidate Model
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class PreliminaryCandidate:
    """A mathematically valid preliminary candidate detected from resting quotes.

    NOTE: A preliminary candidate has a theoretical gross edge based on resting top quotes.
    It is NOT an execution-qualified opportunity: depth traversal, slippage, and quadratic
    exchange fees have NOT yet been applied.
    """

    candidate_id: str
    strategy_type: str
    event_ticker: str
    market_tickers: Tuple[str, ...]
    gross_edge_dollars: Decimal
    gross_edge_cents: Optional[int]
    estimated_entry_cost_dollars: Decimal
    guaranteed_payout_dollars: Decimal
    constituent_quotes: Dict[str, Dict[str, Optional[str]]]
    screening_timestamp: str
    candidate_rationale: str
    validation_limitations: Tuple[str, ...]
    stable_key: str
    is_execution_qualified: bool = False
    quote_timestamp: Optional[str] = None
    observation_timestamp: Optional[str] = None
    preliminary_quantity: Optional[Decimal] = None
    estimated_fees_cents: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Produce a JSON-serializable representation of the preliminary candidate."""
        return {
            "candidate_id": self.candidate_id,
            "strategy_type": self.strategy_type,
            "event_ticker": self.event_ticker,
            "market_tickers": list(self.market_tickers),
            "gross_edge_dollars": str(self.gross_edge_dollars),
            "gross_edge_cents": self.gross_edge_cents,
            "estimated_entry_cost_dollars": str(self.estimated_entry_cost_dollars),
            "guaranteed_payout_dollars": str(self.guaranteed_payout_dollars),
            "constituent_quotes": self.constituent_quotes,
            "screening_timestamp": self.screening_timestamp,
            "observation_timestamp": self.observation_timestamp,
            "candidate_rationale": self.candidate_rationale,
            "validation_limitations": list(self.validation_limitations),
            "stable_key": self.stable_key,
            "is_execution_qualified": self.is_execution_qualified,
            "quote_timestamp": self.quote_timestamp,
            "preliminary_quantity": str(self.preliminary_quantity) if self.preliminary_quantity is not None else None,
            "estimated_fees_cents": self.estimated_fees_cents,
        }


# -----------------------------------------------------------------------------
# Detailed Strategy, Market, and Event Screening Results
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategyEvaluationResult:
    """Detailed evaluation result for a single strategy on a contract or group."""

    strategy_type: str
    outcome: ScreeningOutcome
    reason: str
    candidate: Optional[PreliminaryCandidate] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_type": self.strategy_type,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "candidate": self.candidate.to_dict() if self.candidate else None,
        }


@dataclass
class MarketScreeningResult:
    """Aggregated screening result across single-market strategies."""

    ticker: str
    event_ticker: str
    overall_outcome: ScreeningOutcome
    strategy_results: Dict[str, StrategyEvaluationResult]
    candidates: List[PreliminaryCandidate] = field(default_factory=list)
    rejection_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "overall_outcome": self.overall_outcome.value,
            "strategy_results": {k: v.to_dict() for k, v in self.strategy_results.items()},
            "candidates_count": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
            "rejection_reasons": self.rejection_reasons,
        }


@dataclass
class EventScreeningResult:
    """Aggregated screening result across multi-market event basket strategies."""

    event_ticker: str
    overall_outcome: ScreeningOutcome
    strategy_results: Dict[str, StrategyEvaluationResult]
    candidates: List[PreliminaryCandidate] = field(default_factory=list)
    market_tickers: Tuple[str, ...] = ()
    rejection_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_ticker": self.event_ticker,
            "overall_outcome": self.overall_outcome.value,
            "strategy_results": {k: v.to_dict() for k, v in self.strategy_results.items()},
            "candidates_count": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
            "market_tickers": list(self.market_tickers),
            "rejection_reasons": self.rejection_reasons,
        }


@dataclass
class ScreeningSummary:
    """Deterministic summary of a complete breadth screening pass."""

    session_id: str
    screened_at: str
    duration_seconds: float
    total_markets_screened: int
    total_events_screened: int
    preliminary_candidates_count: int
    new_candidates_count: int
    deduplicated_candidates_count: int
    candidates_by_strategy: Dict[str, int]
    outcomes_breakdown: Dict[str, int]
    is_catalog_complete: bool
    upstream_error: Optional[str]
    candidates: List[PreliminaryCandidate]
    market_results: Dict[str, MarketScreeningResult]
    event_results: Dict[str, EventScreeningResult]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "screened_at": self.screened_at,
            "duration_seconds": round(self.duration_seconds, 4),
            "total_markets_screened": self.total_markets_screened,
            "total_events_screened": self.total_events_screened,
            "preliminary_candidates_count": self.preliminary_candidates_count,
            "new_candidates_count": self.new_candidates_count,
            "deduplicated_candidates_count": self.deduplicated_candidates_count,
            "candidates_by_strategy": dict(self.candidates_by_strategy),
            "outcomes_breakdown": dict(self.outcomes_breakdown),
            "is_catalog_complete": self.is_catalog_complete,
            "upstream_error": self.upstream_error,
            "candidates": [c.to_dict() for c in self.candidates],
        }


# -----------------------------------------------------------------------------
# Breadth Arbitrage Screener
# -----------------------------------------------------------------------------

class BreadthArbitrageScreener:
    """Lightweight deterministic screening engine evaluating breadth market catalogs.

    Evaluates top-of-book quotes for:
    1. Single-market binary parity: YES ask + NO ask < $1.00
    2. Multi-market MECE event baskets: Long YES (sum YES asks < $1.00) and Long NO (sum NO asks < (N-1)*$1.00)
    3. Crossed/inverted books: YES bid > YES ask, NO bid > NO ask, or YES bid + NO bid > $1.00

    Guarantees:
    - Zero network HTTP requests during screening.
    - Zero order-book requests.
    - Exact Decimal and integer-cent precision without binary floating-point errors.
    - Explicit explanation and typed outcome for every screened market.
    """

    def __init__(
        self,
        config: Optional[BreadthScannerConfig] = None,
        declared_baskets: Optional[Sequence[DeclaredMeceBasket]] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config or BreadthScannerConfig()
        self._declared_baskets: Dict[str, DeclaredMeceBasket] = {}
        if declared_baskets:
            for b in declared_baskets:
                self._declared_baskets[b.event_ticker] = b
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))
        self._known_candidates: Dict[str, PreliminaryCandidate] = {}

    def _now(self) -> datetime:
        return self._time_provider()

    def get_known_candidates(self) -> Dict[str, PreliminaryCandidate]:
        """Return all known active preliminary candidates indexed by stable_key."""
        return dict(self._known_candidates)

    def screen_market_binary_parity(self, market: NormalizedMarket) -> StrategyEvaluationResult:
        """Screen single market for binary complement parity: YES ask + NO ask < $1.00."""
        strat = ScreeningStrategyType.BINARY_PARITY.value

        # 1. Market type & status validation
        if market.market_type != "binary":
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                reason=f"Only binary markets are supported, got {market.market_type!r}",
            )
        if market.status and market.status.lower() not in ("open", "active"):
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                reason=f"Market status is '{market.status}' (expected 'open')",
            )
        if market.status and market.status.lower() not in ("open", "active"):
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                reason=f"Market status is '{market.status}' (expected 'open')",
            )

        # 2. Quotes presence check
        missing = []
        if market.yes_ask_dollars is None:
            missing.append("yes_ask_dollars")
        if market.no_ask_dollars is None:
            missing.append("no_ask_dollars")
        if missing:
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES,
                reason=f"Incomplete market quotes: missing {', '.join(missing)}",
            )

        yes_ask = market.yes_ask_dollars
        no_ask = market.no_ask_dollars

        # 3. Quote sanity / bounds check
        for name, p in (("yes_ask_dollars", yes_ask), ("no_ask_dollars", no_ask)):
            if not isinstance(p, Decimal) or not p.is_finite() or p < Decimal("0.00") or p > Decimal("1.00"):
                return StrategyEvaluationResult(
                    strategy_type=strat,
                    outcome=ScreeningOutcome.SKIPPED_INVALID_DATA,
                    reason=f"Quote {name} ({p}) out of valid range [0.00, 1.00]",
                )

        # 4. Exact Decimal arithmetic
        total_cost = yes_ask + no_ask
        payout = Decimal("1.0000")
        gross_edge = payout - total_cost

        # Cents conversion if exact
        gross_edge_cents: Optional[int] = None
        cents_val = gross_edge * Decimal("100")
        if cents_val == cents_val.to_integral_value():
            gross_edge_cents = int(cents_val)

        now_iso = self._now().isoformat()

        # 5. Outcome determination
        if gross_edge > Decimal("0"):
            liq_notes = []
            yes_size = market.yes_ask_size
            no_size = getattr(market, "no_ask_size", None)
            if yes_size is not None and no_size is not None:
                min_size = min(yes_size, no_size)
            elif yes_size is not None:
                min_size = yes_size
            else:
                min_size = None

            if min_size is not None:
                if min_size <= Decimal("0"):
                    liq_notes.append("Top-of-book quote size is zero.")
            else:
                liq_notes.append("Top-of-book quote sizes unavailable.")

            limitations = [
                "Preliminary screening candidate only. Executability, order-book depth, and exchange fees not qualified.",
            ]
            limitations.extend(liq_notes)

            cand = PreliminaryCandidate(
                candidate_id=f"PRE-BP:{market.ticker}",
                strategy_type=strat,
                event_ticker=market.event_ticker,
                market_tickers=(market.ticker,),
                gross_edge_dollars=gross_edge,
                gross_edge_cents=gross_edge_cents,
                estimated_entry_cost_dollars=total_cost,
                guaranteed_payout_dollars=payout,
                constituent_quotes={
                    market.ticker: {
                        "yes_ask": str(yes_ask),
                        "no_ask": str(no_ask),
                        "yes_bid": str(market.yes_bid_dollars) if market.yes_bid_dollars is not None else None,
                        "no_bid": str(market.no_bid_dollars) if market.no_bid_dollars is not None else None,
                        "yes_ask_size": str(yes_size) if yes_size is not None else None,
                        "no_ask_size": str(no_size) if no_size is not None else None,
                    }
                },
                screening_timestamp=now_iso,
                observation_timestamp=now_iso,
                candidate_rationale=(
                    f"Binary parity gross edge: YES ask (${yes_ask}) + NO ask (${no_ask}) = "
                    f"${total_cost} < ${payout} (gross edge: ${gross_edge})"
                ),
                validation_limitations=tuple(limitations),
                stable_key=f"BP:{market.ticker}",
                is_execution_qualified=False,
                quote_timestamp=market.updated_time.isoformat() if market.updated_time else None,
                preliminary_quantity=min_size,
            )
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.PRELIMINARY_CANDIDATE,
                reason=f"Positive gross parity edge of ${gross_edge} (cost: ${total_cost}, payout: ${payout})",
                candidate=cand,
            )
        else:
            reason = (
                f"No positive gross parity edge: combined cost ${total_cost} >= guaranteed payout ${payout} "
                f"(edge: ${gross_edge})"
            )
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.NO_CANDIDATE,
                reason=reason,
            )

    def screen_market_crossed_book(self, market: NormalizedMarket) -> StrategyEvaluationResult:
        """Screen single market for suspicious crossed or inverted top-of-book quotes."""
        strat = ScreeningStrategyType.CROSSED_BOOK.value

        if market.market_type != "binary":
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                reason=f"Only binary markets are supported, got {market.market_type!r}",
            )
        if market.status and market.status.lower() not in ("open", "active"):
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                reason=f"Market status is '{market.status}' (expected 'open')",
            )

        yes_bid = market.yes_bid_dollars
        yes_ask = market.yes_ask_dollars
        no_bid = market.no_bid_dollars
        no_ask = market.no_ask_dollars

        # Check invalid data
        for name, p in (("yes_bid", yes_bid), ("yes_ask", yes_ask), ("no_bid", no_bid), ("no_ask", no_ask)):
            if p is not None:
                if not isinstance(p, Decimal) or not p.is_finite() or p < Decimal("0.00") or p > Decimal("1.00"):
                    return StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_INVALID_DATA,
                        reason=f"Quote {name} ({p}) out of bounds [0.00, 1.00]",
                    )

        # Check quote staleness
        # Check crossed conditions
        crossed_reasons = []
        edge_dollars = Decimal("0.0000")

        if yes_bid is not None and yes_ask is not None:
            if yes_bid > yes_ask:
                diff = yes_bid - yes_ask
                crossed_reasons.append(f"Inverted YES spread: yes_bid (${yes_bid}) > yes_ask (${yes_ask})")
                edge_dollars = max(edge_dollars, diff)

        if no_bid is not None and no_ask is not None:
            if no_bid > no_ask:
                diff = no_bid - no_ask
                crossed_reasons.append(f"Inverted NO spread: no_bid (${no_bid}) > no_ask (${no_ask})")
                edge_dollars = max(edge_dollars, diff)

        if yes_bid is not None and no_bid is not None:
            bids_sum = yes_bid + no_bid
            if bids_sum > Decimal("1.0000"):
                diff = bids_sum - Decimal("1.0000")
                crossed_reasons.append(
                    f"Complement bids crossed: yes_bid (${yes_bid}) + no_bid (${no_bid}) = ${bids_sum} > $1.0000"
                )
                edge_dollars = max(edge_dollars, diff)

        if crossed_reasons:
            now_iso = self._now().isoformat()
            limitations = (
                "Suspicious crossed or inverted top-of-book quotes; requires L2 book depth verification to confirm order routing feasibility.",
                "Preliminary screening flag only. Not an execution-qualified opportunity.",
            )
            cand = PreliminaryCandidate(
                candidate_id=f"PRE-XB:{market.ticker}",
                strategy_type=strat,
                event_ticker=market.event_ticker,
                market_tickers=(market.ticker,),
                gross_edge_dollars=edge_dollars,
                gross_edge_cents=int(edge_dollars * 100) if (edge_dollars * 100).to_integral_value() == (edge_dollars * 100) else None,
                estimated_entry_cost_dollars=Decimal("0.0000"),
                guaranteed_payout_dollars=edge_dollars,
                constituent_quotes={
                    market.ticker: {
                        "yes_bid": str(yes_bid) if yes_bid is not None else None,
                        "yes_ask": str(yes_ask) if yes_ask is not None else None,
                        "no_bid": str(no_bid) if no_bid is not None else None,
                        "no_ask": str(no_ask) if no_ask is not None else None,
                    }
                },
                screening_timestamp=now_iso,
                observation_timestamp=now_iso,
                candidate_rationale="; ".join(crossed_reasons),
                validation_limitations=limitations,
                stable_key=f"XB:{market.ticker}",
                is_execution_qualified=False,
                quote_timestamp=market.updated_time.isoformat() if market.updated_time else None,
            )
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.PRELIMINARY_CANDIDATE,
                reason="; ".join(crossed_reasons),
                candidate=cand,
            )

        # Check if quotes were present to evaluate
        has_any_pair = (
            (yes_bid is not None and yes_ask is not None)
            or (no_bid is not None and no_ask is not None)
            or (yes_bid is not None and no_bid is not None)
        )
        if not has_any_pair:
            return StrategyEvaluationResult(
                strategy_type=strat,
                outcome=ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES,
                reason="Insufficient bid/ask quotes to evaluate crossed-book condition",
            )

        return StrategyEvaluationResult(
            strategy_type=strat,
            outcome=ScreeningOutcome.NO_CANDIDATE,
            reason="Non-crossed quotes: yes_bid <= yes_ask, no_bid <= no_ask, yes_bid + no_bid <= $1.00",
        )

    def screen_market(self, market: NormalizedMarket) -> MarketScreeningResult:
        """Screen a single market across single-market strategies (binary parity, crossed book)."""
        bp_res = self.screen_market_binary_parity(market)
        xb_res = self.screen_market_crossed_book(market)

        strategy_results = {
            bp_res.strategy_type: bp_res,
            xb_res.strategy_type: xb_res,
        }

        candidates = []
        if bp_res.candidate:
            candidates.append(bp_res.candidate)
        if xb_res.candidate:
            candidates.append(xb_res.candidate)

        rejections = [
            f"{r.strategy_type}: {r.reason}"
            for r in (bp_res, xb_res)
            if r.outcome != ScreeningOutcome.PRELIMINARY_CANDIDATE
        ]

        if candidates:
            overall = ScreeningOutcome.PRELIMINARY_CANDIDATE
        elif any(r.outcome == ScreeningOutcome.NO_CANDIDATE for r in (bp_res, xb_res)):
            overall = ScreeningOutcome.NO_CANDIDATE
        else:
            overall = bp_res.outcome

        return MarketScreeningResult(
            ticker=market.ticker,
            event_ticker=market.event_ticker,
            overall_outcome=overall,
            strategy_results=strategy_results,
            candidates=candidates,
            rejection_reasons=rejections,
        )

    def screen_event_mece_baskets(
        self, event: NormalizedEvent, markets: Sequence[NormalizedMarket]
    ) -> List[StrategyEvaluationResult]:
        """Screen an event group for Long YES and Long NO MECE basket arbitrage."""
        results: List[StrategyEvaluationResult] = []
        now_iso = self._now().isoformat()

        # 1. Check duplicate tickers
        tickers = [m.ticker for m in markets]
        if len(tickers) != len(set(tickers)):
            for strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, ScreeningStrategyType.MECE_BASKET_LONG_NO.value):
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_INVALID_DATA,
                        reason="Duplicate contract identifier in event group",
                    )
                )
            return results

        # 2. Check for non-binary market structures or non-open status
        if any(m.market_type != "binary" for m in markets):
            for strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, ScreeningStrategyType.MECE_BASKET_LONG_NO.value):
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                        reason="Event contains non-binary or multivariate market structures",
                    )
                )
            return results

        non_open = [m.ticker for m in markets if m.status and m.status.lower() not in ("open", "active")]
        if non_open:
            for strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, ScreeningStrategyType.MECE_BASKET_LONG_NO.value):
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                        reason=f"Constituent market(s) not open: {', '.join(non_open)}",
                    )
                )
            return results

        # 3. Minimum legs check
        if len(markets) < 2:
            for strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, ScreeningStrategyType.MECE_BASKET_LONG_NO.value):
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                        reason=f"Event has {len(markets)} leg(s), minimum 2 required for MECE basket",
                    )
                )
            return results

        # 4. Incomplete event group check
        if event.market_tickers and len(markets) < len(event.market_tickers):
            for strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, ScreeningStrategyType.MECE_BASKET_LONG_NO.value):
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_INCOMPLETE_EVENT_GROUP,
                        reason=(
                            f"Incomplete event group: received {len(markets)} market(s) "
                            f"but event declares {len(event.market_tickers)} tickers"
                        ),
                    )
                )
            return results

        # 5. Mutual exclusivity check
        if not event.mutually_exclusive:
            for strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, ScreeningStrategyType.MECE_BASKET_LONG_NO.value):
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                        reason="Exchange metadata declares mutually_exclusive=False (cumulative/threshold or independent questions)",
                    )
                )
            return results

        # 6. Collective exhaustiveness validation
        is_declared = event.event_ticker in self._declared_baskets
        is_range = any(event.series_ticker.startswith(p) for p in RANGE_PARTITION_PREFIXES)
        has_residual = any(
            RESIDUAL_OUTCOME_PATTERNS.search(f"{m.subtitle or ''} {m.yes_subtitle or ''} {m.title or ''}")
            or m.ticker.endswith(("-OTHER", "-NONE", "-REST", "-FIELD"))
            for m in markets
        )
        is_exhaustive = is_declared or is_range or has_residual
        if not is_exhaustive:
            for strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, ScreeningStrategyType.MECE_BASKET_LONG_NO.value):
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat,
                        outcome=ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET,
                        reason="Event outcomes not verified collectively exhaustive; missing residual 'Other'/'None' or structured partition",
                    )
                )
            return results

        constituent_tickers = tuple(m.ticker for m in markets)
        N = len(markets)

        # 7. Extract exchange quote timestamps if available for telemetry / candidate auditing
        oldest_quote_iso: Optional[str] = None
        leg_timestamps = [
            m.updated_time if m.updated_time.tzinfo else m.updated_time.replace(tzinfo=timezone.utc)
            for m in markets
            if m.updated_time is not None
        ]
        if leg_timestamps:
            oldest_quote_iso = min(leg_timestamps).isoformat()

        # -----------------------------
        # Evaluate Long YES MECE Basket
        # -----------------------------
        strat_yes = ScreeningStrategyType.MECE_BASKET_LONG_YES.value
        missing_yes = [m.ticker for m in markets if m.yes_ask_dollars is None]
        if missing_yes:
            results.append(
                StrategyEvaluationResult(
                    strategy_type=strat_yes,
                    outcome=ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES,
                    reason=f"Missing yes_ask_dollars for constituent market(s): {', '.join(missing_yes)}",
                )
            )
        else:
            invalid_yes = [
                m.ticker for m in markets
                if not isinstance(m.yes_ask_dollars, Decimal)
                or not m.yes_ask_dollars.is_finite()
                or m.yes_ask_dollars < Decimal("0.00")
                or m.yes_ask_dollars > Decimal("1.00")
            ]
            if invalid_yes:
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat_yes,
                        outcome=ScreeningOutcome.SKIPPED_INVALID_DATA,
                        reason=f"Constituent market(s) with out-of-range yes_ask: {', '.join(invalid_yes)}",
                    )
                )
            else:
                total_cost_yes = sum(m.yes_ask_dollars for m in markets)
                payout_yes = Decimal("1.0000")
                edge_yes = payout_yes - total_cost_yes
                edge_cents_yes = int(edge_yes * 100) if (edge_yes * 100).to_integral_value() == (edge_yes * 100) else None

                if edge_yes > Decimal("0"):
                    quotes_map = {
                        m.ticker: {
                            "yes_ask": str(m.yes_ask_dollars),
                            "yes_bid": str(m.yes_bid_dollars) if m.yes_bid_dollars is not None else None,
                            "yes_ask_size": str(m.yes_ask_size) if m.yes_ask_size is not None else None,
                        }
                        for m in markets
                    }
                    cand_yes = PreliminaryCandidate(
                        candidate_id=f"PRE-MECE-YES:{event.event_ticker}",
                        strategy_type=strat_yes,
                        event_ticker=event.event_ticker,
                        market_tickers=constituent_tickers,
                        gross_edge_dollars=edge_yes,
                        gross_edge_cents=edge_cents_yes,
                        estimated_entry_cost_dollars=total_cost_yes,
                        guaranteed_payout_dollars=payout_yes,
                        constituent_quotes=quotes_map,
                        screening_timestamp=now_iso,
                        candidate_rationale=(
                            f"MECE Long YES basket gross edge: sum of {N} YES asks (${total_cost_yes}) "
                            f"< guaranteed payout (${payout_yes}) (gross edge: ${edge_yes})"
                        ),
                        validation_limitations=(
                            "Preliminary screening candidate only. Executability, order-book depth, and exchange fees not qualified.",
                        ),
                        stable_key=f"MECE-YES:{event.event_ticker}",
                        is_execution_qualified=False,
                        quote_timestamp=oldest_quote_iso,
                        observation_timestamp=now_iso,
                    )
                    results.append(
                        StrategyEvaluationResult(
                            strategy_type=strat_yes,
                            outcome=ScreeningOutcome.PRELIMINARY_CANDIDATE,
                            reason=f"Positive gross MECE Long YES edge of ${edge_yes} (cost: ${total_cost_yes}, payout: ${payout_yes})",
                            candidate=cand_yes,
                        )
                    )
                else:
                    results.append(
                        StrategyEvaluationResult(
                            strategy_type=strat_yes,
                            outcome=ScreeningOutcome.NO_CANDIDATE,
                            reason=(
                                f"No positive gross MECE Long YES edge: sum of {N} YES asks (${total_cost_yes}) "
                                f">= guaranteed payout (${payout_yes}) (edge: ${edge_yes})"
                            ),
                        )
                    )

        # ----------------------------
        # Evaluate Long NO MECE Basket
        # ----------------------------
        strat_no = ScreeningStrategyType.MECE_BASKET_LONG_NO.value
        missing_no = [m.ticker for m in markets if m.no_ask_dollars is None]
        if missing_no:
            results.append(
                StrategyEvaluationResult(
                    strategy_type=strat_no,
                    outcome=ScreeningOutcome.SKIPPED_INCOMPLETE_QUOTES,
                    reason=f"Missing no_ask_dollars for constituent market(s): {', '.join(missing_no)}",
                )
            )
        else:
            invalid_no = [
                m.ticker for m in markets
                if not isinstance(m.no_ask_dollars, Decimal)
                or not m.no_ask_dollars.is_finite()
                or m.no_ask_dollars < Decimal("0.00")
                or m.no_ask_dollars > Decimal("1.00")
            ]
            if invalid_no:
                results.append(
                    StrategyEvaluationResult(
                        strategy_type=strat_no,
                        outcome=ScreeningOutcome.SKIPPED_INVALID_DATA,
                        reason=f"Constituent market(s) with out-of-range no_ask: {', '.join(invalid_no)}",
                    )
                )
            else:
                total_cost_no = sum(m.no_ask_dollars for m in markets)
                payout_no = Decimal(str(N - 1)) * Decimal("1.0000")
                edge_no = payout_no - total_cost_no
                edge_cents_no = int(edge_no * 100) if (edge_no * 100).to_integral_value() == (edge_no * 100) else None

                if edge_no > Decimal("0"):
                    quotes_map_no = {
                        m.ticker: {
                            "no_ask": str(m.no_ask_dollars),
                            "no_bid": str(m.no_bid_dollars) if m.no_bid_dollars is not None else None,
                            "no_ask_size": str(getattr(m, "no_ask_size", None)) if getattr(m, "no_ask_size", None) is not None else (str(m.yes_bid_size) if m.yes_bid_size is not None else None),
                        }
                        for m in markets
                    }
                    cand_no = PreliminaryCandidate(
                        candidate_id=f"PRE-MECE-NO:{event.event_ticker}",
                        strategy_type=strat_no,
                        event_ticker=event.event_ticker,
                        market_tickers=constituent_tickers,
                        gross_edge_dollars=edge_no,
                        gross_edge_cents=edge_cents_no,
                        estimated_entry_cost_dollars=total_cost_no,
                        guaranteed_payout_dollars=payout_no,
                        constituent_quotes=quotes_map_no,
                        screening_timestamp=now_iso,
                        candidate_rationale=(
                            f"MECE Long NO basket gross edge: sum of {N} NO asks (${total_cost_no}) "
                            f"< guaranteed payout (${payout_no}) (gross edge: ${edge_no})"
                        ),
                        validation_limitations=(
                            "Preliminary screening candidate only. Executability, order-book depth, and exchange fees not qualified.",
                        ),
                        stable_key=f"MECE-NO:{event.event_ticker}",
                        is_execution_qualified=False,
                        quote_timestamp=oldest_quote_iso,
                        observation_timestamp=now_iso,
                    )
                    results.append(
                        StrategyEvaluationResult(
                            strategy_type=strat_no,
                            outcome=ScreeningOutcome.PRELIMINARY_CANDIDATE,
                            reason=f"Positive gross MECE Long NO edge of ${edge_no} (cost: ${total_cost_no}, payout: ${payout_no})",
                            candidate=cand_no,
                        )
                    )
                else:
                    results.append(
                        StrategyEvaluationResult(
                            strategy_type=strat_no,
                            outcome=ScreeningOutcome.NO_CANDIDATE,
                            reason=(
                                f"No positive gross MECE Long NO edge: sum of {N} NO asks (${total_cost_no}) "
                                f">= guaranteed payout (${payout_no}) (edge: ${edge_no})"
                            ),
                        )
                    )

        return results

    def screen_event_group(
        self, event: NormalizedEvent, markets: Sequence[NormalizedMarket]
    ) -> EventScreeningResult:
        """Screen an event group across multi-market MECE basket strategies."""
        mece_results = self.screen_event_mece_baskets(event, markets)
        strategy_results = {r.strategy_type: r for r in mece_results}

        candidates = [r.candidate for r in mece_results if r.candidate is not None]
        rejections = [
            f"{r.strategy_type}: {r.reason}"
            for r in mece_results
            if r.outcome != ScreeningOutcome.PRELIMINARY_CANDIDATE
        ]

        if candidates:
            overall = ScreeningOutcome.PRELIMINARY_CANDIDATE
        elif any(r.outcome == ScreeningOutcome.NO_CANDIDATE for r in mece_results):
            overall = ScreeningOutcome.NO_CANDIDATE
        else:
            overall = mece_results[0].outcome if mece_results else ScreeningOutcome.NO_CANDIDATE

        return EventScreeningResult(
            event_ticker=event.event_ticker,
            overall_outcome=overall,
            strategy_results=strategy_results,
            candidates=candidates,
            market_tickers=tuple(m.ticker for m in markets),
            rejection_reasons=rejections,
        )

    def screen_catalog(
        self,
        catalog: Union[CatalogDiscoveryResult, Sequence[NormalizedMarket]],
        events: Optional[Mapping[str, NormalizedEvent]] = None,
    ) -> ScreeningSummary:
        """Screen a complete catalog from discovery without making any network requests."""
        start_time = self._now()
        session_id = f"screen-{uuid.uuid4().hex[:8]}"

        if isinstance(catalog, CatalogDiscoveryResult):
            markets_dict = catalog.discovered_markets
            events_dict = catalog.discovered_events
            is_complete = catalog.is_complete
            upstream_error = catalog.error_message
        else:
            markets_dict = {m.ticker: m for m in catalog}
            events_dict = dict(events or {})
            is_complete = True
            upstream_error = None

        # Group markets by event_ticker
        markets_by_event: Dict[str, List[NormalizedMarket]] = {}
        for m in markets_dict.values():
            markets_by_event.setdefault(m.event_ticker, []).append(m)

        all_candidates: List[PreliminaryCandidate] = []
        market_results: Dict[str, MarketScreeningResult] = {}
        event_results: Dict[str, EventScreeningResult] = {}
        candidates_by_strategy: Dict[str, int] = {}
        outcomes_breakdown: Dict[str, int] = {}

        # 1. Screen individual markets
        for ticker, market in markets_dict.items():
            m_res = self.screen_market(market)
            market_results[ticker] = m_res
            outcomes_breakdown[m_res.overall_outcome.value] = (
                outcomes_breakdown.get(m_res.overall_outcome.value, 0) + 1
            )
            for c in m_res.candidates:
                all_candidates.append(c)
                candidates_by_strategy[c.strategy_type] = (
                    candidates_by_strategy.get(c.strategy_type, 0) + 1
                )

        # 2. Screen event groups
        for ev_ticker, ev_mkts in markets_by_event.items():
            ev = events_dict.get(ev_ticker)
            if ev is not None:
                ev_res = self.screen_event_group(ev, ev_mkts)
                event_results[ev_ticker] = ev_res
                outcomes_breakdown[ev_res.overall_outcome.value] = (
                    outcomes_breakdown.get(ev_res.overall_outcome.value, 0) + 1
                )
                for c in ev_res.candidates:
                    all_candidates.append(c)
                    candidates_by_strategy[c.strategy_type] = (
                        candidates_by_strategy.get(c.strategy_type, 0) + 1
                    )

        # 3. Reconcile new vs deduplicated candidates
        new_count = 0
        dedup_count = 0
        current_candidates_map: Dict[str, PreliminaryCandidate] = {}

        for cand in all_candidates:
            current_candidates_map[cand.stable_key] = cand
            prev = self._known_candidates.get(cand.stable_key)
            if (
                prev is not None
                and prev.gross_edge_dollars == cand.gross_edge_dollars
                and prev.constituent_quotes == cand.constituent_quotes
            ):
                dedup_count += 1
            else:
                new_count += 1

        self._known_candidates = dict(current_candidates_map)

        end_time = self._now()
        duration = max(0.0, (end_time - start_time).total_seconds())

        return ScreeningSummary(
            session_id=session_id,
            screened_at=end_time.isoformat(),
            duration_seconds=duration,
            total_markets_screened=len(markets_dict),
            total_events_screened=len(markets_by_event),
            preliminary_candidates_count=len(all_candidates),
            new_candidates_count=new_count,
            deduplicated_candidates_count=dedup_count,
            candidates_by_strategy=candidates_by_strategy,
            outcomes_breakdown=outcomes_breakdown,
            is_catalog_complete=is_complete,
            upstream_error=upstream_error,
            candidates=all_candidates,
            market_results=market_results,
            event_results=event_results,
        )


# =============================================================================
# Phase 2C: Targeted Order-Book Qualification for Preliminary Candidates
# =============================================================================

@dataclass(frozen=True)
class TargetedOrderBookQualifierConfig:
    """Configuration for targeted L2 order-book qualification."""

    target_quantity: int = 1
    max_queue_size: int = 100
    max_concurrency: int = 4
    max_requests_per_second: float = 5.0
    max_stale_seconds: float = 60.0
    max_leg_skew_seconds: float = 5.0
    max_fetch_window_seconds: float = 5.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    fee_schedule: FeeScheduleType = FeeScheduleType.KALSHI_OFFICIAL_TAKER
    require_full_quantity: bool = True
    min_net_profit_cents: int = 1
    require_source_timestamp: bool = True
    max_candidate_legs: int = 8
    enable_event_prefilter: bool = True
    event_cache_ttl_seconds: float = 2.0
    check_prefilter_quote_staleness: bool = False

    def __post_init__(self) -> None:
        if self.target_quantity <= 0:
            raise ValueError("target_quantity must be positive")
        if self.max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be positive")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if self.max_candidate_legs <= 0:
            raise ValueError("max_candidate_legs must be positive")
        if self.max_fetch_window_seconds <= 0:
            raise ValueError("max_fetch_window_seconds must be positive")
        if self.event_cache_ttl_seconds < 0:
            raise ValueError("event_cache_ttl_seconds must be non-negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.event_cache_ttl_seconds < 0:
            raise ValueError("event_cache_ttl_seconds must be non-negative")


@dataclass(frozen=True)
class TargetedQualificationResult:
    """Auditable result of targeted order-book qualification for an individual candidate."""

    qualification_id: str
    candidate_id: str
    strategy_type: str
    stable_key: str
    evaluated_at: str
    outcome: str  # QUALIFIED, REJECTED_*, STALE, ERROR, UNSUPPORTED_STRATEGY, etc.
    is_qualified: bool
    rejection_reason: Optional[str]
    preliminary_candidate: PreliminaryCandidate
    requested_quantity: int
    executable_quantity: int
    gross_edge_cents: Optional[int]
    net_edge_cents: Optional[int]
    total_fees_cents: Optional[int]
    return_on_capital_pct: Optional[Decimal]
    leg_details: Tuple[LegExecutionDetail, ...] = ()
    order_book_hashes: Dict[str, str] = field(default_factory=dict)
    requests_made: int = 0
    requests_failed: int = 0
    retries_count: int = 0
    rate_limit_events_count: int = 0
    refreshed_gross_edge_dollars: Optional[Decimal] = None
    candidate_leg_count: int = 0
    quote_age_seconds: Optional[float] = None
    prefilter_duration_seconds: Optional[float] = None
    order_book_fetch_duration_seconds: Optional[float] = None
    fetch_window_seconds: Optional[float] = None
    server_timestamp_skew_seconds: Optional[float] = None
    cross_leg_skew_seconds: Optional[float] = None
    execution_qualification: Optional[CandidateQualification] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "qualification_id": self.qualification_id,
            "candidate_id": self.candidate_id,
            "strategy_type": self.strategy_type,
            "stable_key": self.stable_key,
            "evaluated_at": self.evaluated_at,
            "outcome": self.outcome,
            "is_qualified": self.is_qualified,
            "rejection_reason": self.rejection_reason,
            "requested_quantity": self.requested_quantity,
            "executable_quantity": self.executable_quantity,
            "gross_edge_cents": self.gross_edge_cents,
            "net_edge_cents": self.net_edge_cents,
            "total_fees_cents": self.total_fees_cents,
            "return_on_capital_pct": str(self.return_on_capital_pct) if self.return_on_capital_pct is not None else None,
            "refreshed_gross_edge_dollars": str(self.refreshed_gross_edge_dollars) if self.refreshed_gross_edge_dollars is not None else None,
            "candidate_leg_count": self.candidate_leg_count,
            "quote_age_seconds": round(self.quote_age_seconds, 4) if self.quote_age_seconds is not None else None,
            "prefilter_duration_seconds": round(self.prefilter_duration_seconds, 4) if self.prefilter_duration_seconds is not None else None,
            "order_book_fetch_duration_seconds": round(self.order_book_fetch_duration_seconds, 4) if self.order_book_fetch_duration_seconds is not None else None,
            "fetch_window_seconds": round(self.fetch_window_seconds, 4) if self.fetch_window_seconds is not None else None,
            "server_timestamp_skew_seconds": round(self.server_timestamp_skew_seconds, 4) if self.server_timestamp_skew_seconds is not None else None,
            "cross_leg_skew_seconds": round(self.cross_leg_skew_seconds, 4) if self.cross_leg_skew_seconds is not None else None,
            "leg_details": [l.to_dict() for l in self.leg_details],
            "order_book_hashes": dict(self.order_book_hashes),
            "requests_made": self.requests_made,
            "requests_failed": self.requests_failed,
            "retries_count": self.retries_count,
            "rate_limit_events_count": self.rate_limit_events_count,
            "preliminary_candidate": self.preliminary_candidate.to_dict(),
        }


@dataclass
class TargetedQualificationSummary:
    """Comprehensive summary of a targeted order-book qualification pass."""

    session_id: str
    started_at: str
    completed_at: str
    duration_seconds: float
    candidates_enqueued: int
    candidates_evaluated: int
    qualified_count: int
    rejected_count: int
    stale_count: int
    error_count: int
    unsupported_count: int
    requests_made: int
    requests_failed: int
    retries_count: int
    rate_limit_events_count: int
    queue_overflow_count: int
    outcomes_breakdown: Dict[str, int]
    results: List[TargetedQualificationResult]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 4),
            "candidates_enqueued": self.candidates_enqueued,
            "candidates_evaluated": self.candidates_evaluated,
            "qualified_count": self.qualified_count,
            "rejected_count": self.rejected_count,
            "stale_count": self.stale_count,
            "error_count": self.error_count,
            "unsupported_count": self.unsupported_count,
            "requests_made": self.requests_made,
            "requests_failed": self.requests_failed,
            "retries_count": self.retries_count,
            "rate_limit_events_count": self.rate_limit_events_count,
            "queue_overflow_count": self.queue_overflow_count,
            "outcomes_breakdown": dict(self.outcomes_breakdown),
            "results_count": len(self.results),
            "qualified_results": [r.to_dict() for r in self.results if r.is_qualified],
        }


def compute_order_book_hash(book: Optional[NormalizedOrderBook]) -> str:
    """Compute a deterministic SHA-256 fingerprint of order-book bids for auditability."""
    if book is None:
        return "none"
    parts = []
    for side, bids in (("YES", book.yes_bids), ("NO", book.no_bids)):
        for lvl in bids:
            parts.append(f"{side}:{lvl.price_dollars}:{lvl.quantity}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def extract_constituent_leg_count(candidate: PreliminaryCandidate) -> Tuple[Optional[int], Optional[str]]:
    """Determine the valid constituent leg count of a preliminary candidate.

    Returns (leg_count, None) if valid.
    Returns (None, error_reason) if missing, malformed, or invalid.
    """
    if not isinstance(candidate, PreliminaryCandidate):
        return None, f"Item is not a PreliminaryCandidate: got {type(candidate).__name__}"
    tickers = getattr(candidate, "market_tickers", None)
    if tickers is None:
        return None, "Candidate market_tickers is missing (None)"
    if not isinstance(tickers, (tuple, list, set)):
        return None, f"Candidate market_tickers must be a sequence, got {type(tickers).__name__}"
    if len(tickers) == 0:
        return None, "Candidate market_tickers is empty"
    for i, t in enumerate(tickers):
        if not isinstance(t, str) or not t.strip():
            return None, f"Candidate market_tickers[{i}] is not a valid non-empty string: {t!r}"
    return len(tickers), None


@dataclass(frozen=True)
class CandidatePriority:
    """Comparable priority key for preliminary candidate evaluation order.

    Priority criteria (descending):
    1. Edge density: gross edge divided by constituent leg count.
    2. Gross edge dollars: total dollar edge if densities match.
    3. Deterministic tie-breaker: earlier candidate_id / stable_key string preferred.
    """

    edge_density: Decimal
    gross_edge_dollars: Decimal
    candidate_id: str

    def __lt__(self, other: "CandidatePriority") -> bool:
        if not isinstance(other, CandidatePriority):
            return NotImplemented
        if self.edge_density != other.edge_density:
            return self.edge_density < other.edge_density
        if self.gross_edge_dollars != other.gross_edge_dollars:
            return self.gross_edge_dollars < other.gross_edge_dollars
        return self.candidate_id > other.candidate_id

    def __gt__(self, other: "CandidatePriority") -> bool:
        if not isinstance(other, CandidatePriority):
            return NotImplemented
        if self.edge_density != other.edge_density:
            return self.edge_density > other.edge_density
        if self.gross_edge_dollars != other.gross_edge_dollars:
            return self.gross_edge_dollars > other.gross_edge_dollars
        return self.candidate_id < other.candidate_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CandidatePriority):
            return False
        return (
            self.edge_density == other.edge_density
            and self.gross_edge_dollars == other.gross_edge_dollars
            and self.candidate_id == other.candidate_id
        )


def compute_candidate_edge_density(candidate: PreliminaryCandidate) -> Decimal:
    """Compute gross edge dollars per constituent market leg.

    Returns Decimal('0') if gross edge is non-positive or leg count is invalid/zero.
    """
    try:
        raw_edge = getattr(candidate, "gross_edge_dollars", None)
        if raw_edge is None:
            return Decimal("0")
        edge = Decimal(str(raw_edge))
        if edge <= Decimal("0") or not isfinite(edge):
            return Decimal("0")

        tickers = getattr(candidate, "market_tickers", None)
        if not isinstance(tickers, (tuple, list, set)) or len(tickers) == 0:
            return Decimal("0")

        leg_count = len(tickers)
        if leg_count <= 0:
            return Decimal("0")
        return edge / Decimal(leg_count)
    except Exception:
        return Decimal("0")


def compute_candidate_priority(candidate: PreliminaryCandidate) -> CandidatePriority:
    """Compute comparable CandidatePriority instance for ordering priority queues."""
    density = compute_candidate_edge_density(candidate)
    try:
        raw_edge = getattr(candidate, "gross_edge_dollars", None)
        gross = Decimal(str(raw_edge)) if raw_edge is not None and isfinite(Decimal(str(raw_edge))) else Decimal("0")
    except Exception:
        gross = Decimal("0")

    cid = str(getattr(candidate, "candidate_id", "") or "")
    if not cid:
        cid = str(getattr(candidate, "stable_key", "") or "")
    return CandidatePriority(
        edge_density=density,
        gross_edge_dollars=gross,
        candidate_id=cid,
    )


class TargetedOrderBookQualifier:
    """Targeted L2 order-book qualification engine for preliminary candidates.

    - Receives preliminary candidates from BreadthArbitrageScreener.
    - Manages a bounded priority queue (highest gross edge evaluated first).
    - Checks staleness before network requests (rejects stale candidates without fetching).
    - Fetches L2 order books ONLY for constituent legs of candidate opportunities.
    - Reuses ExecutionAwareArbitrageQualifier for multi-level VWAP, slippage, and quadratic fees.
    - Distinguishes QUALIFIED, REJECTED, STALE, ERROR, and UNSUPPORTED outcomes.
    - Preserves zero order-book fetching for the full catalog.
    """

    def __init__(
        self,
        client: Optional[KalshiRestClient] = None,
        config: Optional[TargetedOrderBookQualifierConfig] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
        sleep_func: Optional[Callable[[float], None]] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self.config = config or TargetedOrderBookQualifierConfig()
        self.client = client or KalshiRestClient()
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_func or time.sleep
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            rate_per_second=self.config.max_requests_per_second,
            sleep_func=self._sleep,
        )

        # Underlying V22 execution-aware qualifier
        qualifier_cfg = ExecutionQualifierConfig(
            target_quantity=self.config.target_quantity,
            max_stale_seconds=self.config.max_stale_seconds,
            max_leg_skew_seconds=self.config.max_leg_skew_seconds,
            fee_model_config=FeeModelConfig(schedule_type=self.config.fee_schedule),
            require_full_quantity=self.config.require_full_quantity,
            min_net_profit_cents=self.config.min_net_profit_cents,
            require_source_timestamp=self.config.require_source_timestamp,
        )
        self.qualifier = ExecutionAwareArbitrageQualifier(config=qualifier_cfg)

        # Queue and internal state
        self._queue: List[PreliminaryCandidate] = []
        self._queue_keys: Set[str] = set()
        self._queue_overflow_count: int = 0
        self._gated_rejections: List[TargetedQualificationResult] = []
        self._evaluated_candidates: Dict[str, Tuple[Decimal, Dict[str, Any]]] = {}
        self._event_cache: Dict[str, Tuple[NormalizedEvent, float]] = {}
        self._last_summary: Optional[TargetedQualificationSummary] = None

    def _now(self) -> datetime:
        return self._time_provider()

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def gated_rejections(self) -> List[TargetedQualificationResult]:
        """Pending rejections from pre-qualification leg size gating."""
        return list(self._gated_rejections)

    @property
    def queue_overflow_count(self) -> int:
        return self._queue_overflow_count

    @property
    def rate_limiter(self) -> TokenBucketRateLimiter:
        """Active rate limiter instance."""
        return self._rate_limiter

    def _rate_limit(self) -> None:
        """Enforce configured requests-per-second rate limit."""
        self._rate_limiter.acquire()

    def enqueue_candidate(self, candidate: PreliminaryCandidate) -> bool:
        """Enqueue a preliminary candidate for targeted order-book qualification.

        Returns True if enqueued, False if rejected by deduplication, leg gating, or overflow.
        """
        if not isinstance(candidate, PreliminaryCandidate):
            return False

        # In-queue deduplication
        if candidate.stable_key in self._queue_keys:
            return False

        # Evaluated cache deduplication: check if unchanged quotes
        prev = self._evaluated_candidates.get(candidate.stable_key)
        if prev is not None:
            prev_edge, prev_quotes = prev
            if prev_edge == candidate.gross_edge_dollars and prev_quotes == candidate.constituent_quotes:
                return False

        # Leg-count gating before entering request queue
        leg_count, leg_err = extract_constituent_leg_count(candidate)
        if leg_err is not None or (leg_count is not None and leg_count > self.config.max_candidate_legs):
            rejection_reason = (
                f"Candidate constituent leg data is invalid: {leg_err}"
                if leg_err is not None
                else (
                    f"Candidate constituent leg count ({leg_count}) exceeds "
                    f"configured maximum allowable limit ({self.config.max_candidate_legs})"
                )
            )
            res = TargetedQualificationResult(
                qualification_id=f"tqual-{uuid.uuid4().hex[:10]}",
                candidate_id=candidate.candidate_id,
                strategy_type=candidate.strategy_type,
                stable_key=candidate.stable_key,
                evaluated_at=self._now().isoformat(),
                outcome=QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value,
                is_qualified=False,
                rejection_reason=rejection_reason,
                preliminary_candidate=candidate,
                requested_quantity=self.config.target_quantity,
                executable_quantity=0,
                gross_edge_cents=candidate.gross_edge_cents,
                net_edge_cents=None,
                total_fees_cents=None,
                return_on_capital_pct=None,
                requests_made=0,
                requests_failed=0,
                retries_count=0,
                rate_limit_events_count=0,
                candidate_leg_count=leg_count if leg_count is not None else (len(candidate.market_tickers) if candidate.market_tickers is not None else 0),
            )
            self._gated_rejections.append(res)
            # Record in evaluated cache to prevent duplicate re-evaluations
            self._evaluated_candidates[candidate.stable_key] = (
                candidate.gross_edge_dollars,
                candidate.constituent_quotes,
            )
            return False

        # Bounded queue check using edge-density priority
        cand_prio = compute_candidate_priority(candidate)
        if len(self._queue) >= self.config.max_queue_size:
            self._queue_overflow_count += 1
            # Evict candidate with lowest priority if incoming has higher priority
            min_idx = min(range(len(self._queue)), key=lambda i: compute_candidate_priority(self._queue[i]))
            lowest = self._queue[min_idx]
            if cand_prio > compute_candidate_priority(lowest):
                self._queue_keys.remove(lowest.stable_key)
                self._queue.pop(min_idx)
            else:
                return False

        self._queue.append(candidate)
        self._queue_keys.add(candidate.stable_key)
        # Priority sort: highest edge density first, with deterministic tie-breaking
        self._queue.sort(key=compute_candidate_priority, reverse=True)
        return True

    def enqueue_candidates(self, candidates: Sequence[PreliminaryCandidate]) -> int:
        """Enqueue a collection of preliminary candidates. Returns count successfully enqueued.

        Sorts candidates by edge density before enqueuing so highest priority candidates
        are enqueued first without risk of premature eviction by lower priority items.
        """
        count = 0
        sorted_candidates = sorted(candidates, key=compute_candidate_priority, reverse=True)
        for cand in sorted_candidates:
            if self.enqueue_candidate(cand):
                count += 1
        return count

    def _fetch_order_book_with_retry(
        self,
        ticker: str,
        cancellation_token: Optional[threading.Event] = None,
    ) -> Tuple[Optional[NormalizedOrderBook], Optional[str], int, int, int, int]:
        """Fetch an order book for a constituent ticker with retry and backoff.

        Returns: (order_book, error_message, requests_made, requests_failed, retries_count, rate_limit_events)
        """
        attempts = 0
        reqs = 0
        fails = 0
        retries = 0
        rate_limits = 0
        book: Optional[NormalizedOrderBook] = None
        err_msg: Optional[str] = None

        while attempts <= self.config.max_retries:
            if cancellation_token is not None and cancellation_token.is_set():
                err_msg = f"Fetch cancelled for {ticker} due to constituent leg failure"
                break
            attempts += 1
            reqs += 1
            self._rate_limit()
            if cancellation_token is not None and cancellation_token.is_set():
                err_msg = f"Fetch cancelled for {ticker} due to constituent leg failure"
                break
            try:
                book = self.client.get_order_book(ticker)
                err_msg = None
                break
            except (KalshiTransportError, KalshiHTTPError, KalshiJSONError, KalshiResponseError, MarketDataInputError) as exc:
                fails += 1
                if isinstance(exc, KalshiHTTPError) and exc.status_code == 429:
                    rate_limits += 1
                if attempts > self.config.max_retries:
                    err_msg = f"Failed fetching order book for {ticker} after {attempts} attempts: {exc}"
                    break
                if cancellation_token is not None and cancellation_token.is_set():
                    err_msg = f"Fetch cancelled for {ticker} due to constituent leg failure"
                    break
                retries += 1
                backoff = self.config.retry_backoff_seconds * (2 ** (attempts - 1))
                self._sleep(backoff)
            except Exception as exc:
                fails += 1
                err_msg = f"Unexpected error fetching order book for {ticker}: {exc}"
                break

        return book, err_msg, reqs, fails, retries, rate_limits

    def _execute_order_book_fetch(
        self, ticker: str, cancellation_token: Optional[threading.Event] = None
    ) -> Tuple[Optional[NormalizedOrderBook], Optional[str], int, int, int, int]:
        """Dispatch order-book fetch with backward compatibility for 1-argument patched mock methods."""
        try:
            return self._fetch_order_book_with_retry(ticker, cancellation_token=cancellation_token)
        except TypeError:
            return self._fetch_order_book_with_retry(ticker)

    def _fetch_event_with_retry(
        self, event_ticker: str
    ) -> Tuple[Optional[NormalizedEvent], Optional[str], int, int, int, int]:
        """Fetch event with nested markets with retry, backoff, and freshness caching.

        Returns: (event, error_message, requests_made, requests_failed, retries_count, rate_limit_events)
        """
        # Check freshness cache for safe deduplication across candidates sharing an event
        now_mono = time.monotonic()
        if event_ticker in self._event_cache:
            cached_evt, cached_at = self._event_cache[event_ticker]
            if (now_mono - cached_at) <= self.config.event_cache_ttl_seconds:
                return cached_evt, None, 0, 0, 0, 0

        attempts = 0
        reqs = 0
        fails = 0
        retries = 0
        rate_limits = 0
        event: Optional[NormalizedEvent] = None
        err_msg: Optional[str] = None

        while attempts <= self.config.max_retries:
            attempts += 1
            reqs += 1
            self._rate_limit()
            try:
                event = self.client.get_event(event_ticker, with_nested_markets=True)
                err_msg = None
                if event is not None:
                    self._event_cache[event_ticker] = (event, time.monotonic())
                break
            except (KalshiTransportError, KalshiHTTPError, KalshiJSONError, KalshiResponseError, MarketDataInputError) as exc:
                fails += 1
                if isinstance(exc, KalshiHTTPError) and exc.status_code == 429:
                    rate_limits += 1
                if attempts > self.config.max_retries:
                    err_msg = f"Failed fetching event for {event_ticker} after {attempts} attempts: {exc}"
                    break
                retries += 1
                backoff = self.config.retry_backoff_seconds * (2 ** (attempts - 1))
                self._sleep(backoff)
            except Exception as exc:
                fails += 1
                err_msg = f"Unexpected error fetching event for {event_ticker}: {exc}"
                break

        return event, err_msg, reqs, fails, retries, rate_limits

    def _evaluate_event_prefilter(
        self, candidate: PreliminaryCandidate
    ) -> Tuple[bool, Optional[str], Optional[Decimal], int, int, int, int, Optional[str]]:
        """Evaluate top-of-book quotes from latest event to check if preliminary edge survives.

        Returns: (passed, outcome_str, refreshed_edge, reqs, fails, retries, rate_limits, reason)
        """
        if not self.config.enable_event_prefilter:
            return True, None, None, 0, 0, 0, 0, None

        if not hasattr(self.client, "get_event"):
            return True, None, None, 0, 0, 0, 0, None

        if not candidate.event_ticker:
            return False, "ERROR", None, 0, 0, 0, 0, "Candidate missing event_ticker for event prefilter"

        strat = candidate.strategy_type
        if strat not in (
            ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
            ScreeningStrategyType.MECE_BASKET_LONG_NO.value,
            ScreeningStrategyType.BINARY_PARITY.value,
            "mece_basket_long_yes",
            "mece_basket_long_no",
            "binary_parity",
        ):
            return True, None, None, 0, 0, 0, 0, None

        event, err_msg, reqs, fails, retries, rate_limits = self._fetch_event_with_retry(candidate.event_ticker)
        if event is None:
            outcome_str = QualificationOutcome.REJECTED_MISSING_MARKET.value if (err_msg and "404" in err_msg) else "ERROR"
            reason = err_msg or f"Failed fetching event {candidate.event_ticker}"
            return False, outcome_str, None, reqs, fails, retries, rate_limits, reason

        if not event.markets:
            reason = f"Event {candidate.event_ticker} response contains no nested markets"
            return False, "ERROR", None, reqs, fails, retries, rate_limits, reason

        markets_map = {m.ticker: m for m in event.markets}
        missing_tickers = [t for t in candidate.market_tickers if t not in markets_map]
        if missing_tickers:
            reason = f"Event prefilter failed: constituent market(s) missing from event: {', '.join(missing_tickers)}"
            return False, QualificationOutcome.REJECTED_MISSING_MARKET.value, None, reqs, fails, retries, rate_limits, reason

        now_utc = self._now()
        if not now_utc.tzinfo:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        # Quote staleness check on constituent markets (opt-in metadata check)
        if self.config.check_prefilter_quote_staleness:
            constituent_markets = [markets_map[t] for t in candidate.market_tickers]
            for m in constituent_markets:
                if m.updated_time is None:
                    reason = f"Event prefilter failed: constituent market {m.ticker} missing updated_time"
                    return False, QualificationOutcome.REJECTED_STALE.value, None, reqs, fails, retries, rate_limits, reason
                m_ts = m.updated_time if m.updated_time.tzinfo else m.updated_time.replace(tzinfo=timezone.utc)
                age_s = max(0.0, (now_utc - m_ts).total_seconds())
                if age_s > self.config.max_stale_seconds:
                    reason = f"Event prefilter failed: constituent market {m.ticker} quote is stale ({age_s:.1f}s > {self.config.max_stale_seconds}s limit)"
                    return False, QualificationOutcome.REJECTED_STALE.value, None, reqs, fails, retries, rate_limits, reason

        constituent_markets = [markets_map[t] for t in candidate.market_tickers]

        # Recalculate preliminary opportunity
        refreshed_edge: Optional[Decimal] = None

        if strat in (ScreeningStrategyType.MECE_BASKET_LONG_YES.value, "mece_basket_long_yes"):
            for m in constituent_markets:
                if m.yes_ask_dollars is None:
                    return False, "ERROR", None, reqs, fails, retries, rate_limits, f"Event prefilter failed: missing yes_ask_dollars for {m.ticker}"
                if not isinstance(m.yes_ask_dollars, Decimal) or not m.yes_ask_dollars.is_finite() or m.yes_ask_dollars < Decimal("0.00") or m.yes_ask_dollars > Decimal("1.00"):
                    return False, "ERROR", None, reqs, fails, retries, rate_limits, f"Event prefilter failed: invalid yes_ask_dollars for {m.ticker}"
            total_cost = sum(m.yes_ask_dollars for m in constituent_markets)
            payout = Decimal("1.0000")
            refreshed_edge = payout - total_cost

        elif strat in (ScreeningStrategyType.MECE_BASKET_LONG_NO.value, "mece_basket_long_no"):
            for m in constituent_markets:
                if m.no_ask_dollars is None:
                    return False, "ERROR", None, reqs, fails, retries, rate_limits, f"Event prefilter failed: missing no_ask_dollars for {m.ticker}"
                if not isinstance(m.no_ask_dollars, Decimal) or not m.no_ask_dollars.is_finite() or m.no_ask_dollars < Decimal("0.00") or m.no_ask_dollars > Decimal("1.00"):
                    return False, "ERROR", None, reqs, fails, retries, rate_limits, f"Event prefilter failed: invalid no_ask_dollars for {m.ticker}"
            N = len(constituent_markets)
            total_cost = sum(m.no_ask_dollars for m in constituent_markets)
            payout = Decimal(str(N - 1)) * Decimal("1.0000")
            refreshed_edge = payout - total_cost

        elif strat in (ScreeningStrategyType.BINARY_PARITY.value, "binary_parity"):
            m = constituent_markets[0]
            if m.yes_ask_dollars is None or m.no_ask_dollars is None:
                return False, "ERROR", None, reqs, fails, retries, rate_limits, f"Event prefilter failed: missing quotes for {m.ticker}"
            for name, p in (("yes_ask_dollars", m.yes_ask_dollars), ("no_ask_dollars", m.no_ask_dollars)):
                if not isinstance(p, Decimal) or not p.is_finite() or p < Decimal("0.00") or p > Decimal("1.00"):
                    return False, "ERROR", None, reqs, fails, retries, rate_limits, f"Event prefilter failed: invalid {name} for {m.ticker}"
            total_cost = m.yes_ask_dollars + m.no_ask_dollars
            payout = Decimal("1.0000")
            refreshed_edge = payout - total_cost

        if refreshed_edge <= Decimal("0.0000"):
            now_iso = now_utc.isoformat()
            reason = (
                f"Event pre-filter rejected: refreshed gross edge ${refreshed_edge:.4f} <= $0.0000 "
                f"(original edge: ${candidate.gross_edge_dollars}) for event {candidate.event_ticker} at {now_iso}"
            )
            return False, QualificationOutcome.REJECTED_EVENT_PREFILTER.value, refreshed_edge, reqs, fails, retries, rate_limits, reason

        return True, None, refreshed_edge, reqs, fails, retries, rate_limits, None

    def qualify_candidate(
        self,
        candidate: PreliminaryCandidate,
        market: Optional[NormalizedMarket] = None,
    ) -> TargetedQualificationResult:
        """Execute targeted L2 order-book qualification for an individual preliminary candidate."""
        now = self._now()
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        qual_id = f"tqual-{uuid.uuid4().hex[:10]}"

        # 0. Check constituent leg count gating before spending any network requests
        leg_count, leg_err = extract_constituent_leg_count(candidate)
        cand_leg_count = leg_count if leg_count is not None else (len(candidate.market_tickers) if candidate.market_tickers is not None else 0)

        # Compute quote age
        quote_age_s: Optional[float] = None
        ref_ts_str = getattr(candidate, "observation_timestamp", None) or candidate.quote_timestamp
        if ref_ts_str:
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
            ):
                try:
                    q_ts = datetime.strptime(ref_ts_str, fmt)
                    if q_ts.tzinfo is None:
                        q_ts = q_ts.replace(tzinfo=timezone.utc)
                    quote_age_s = max(0.0, (now_utc - q_ts).total_seconds())
                    break
                except ValueError:
                    continue
        elif market and market.updated_time:
            m_ts = market.updated_time if market.updated_time.tzinfo else market.updated_time.replace(tzinfo=timezone.utc)
            quote_age_s = max(0.0, (now_utc - m_ts).total_seconds())

        if leg_err is not None or (leg_count is not None and leg_count > self.config.max_candidate_legs):
            rejection_reason = (
                f"Candidate constituent leg data is invalid: {leg_err}"
                if leg_err is not None
                else (
                    f"Candidate constituent leg count ({leg_count}) exceeds "
                    f"configured maximum allowable limit ({self.config.max_candidate_legs})"
                )
            )
            return TargetedQualificationResult(
                qualification_id=qual_id,
                candidate_id=candidate.candidate_id,
                strategy_type=candidate.strategy_type,
                stable_key=candidate.stable_key,
                evaluated_at=now.isoformat(),
                outcome=QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value,
                is_qualified=False,
                rejection_reason=rejection_reason,
                preliminary_candidate=candidate,
                requested_quantity=self.config.target_quantity,
                executable_quantity=0,
                gross_edge_cents=candidate.gross_edge_cents,
                net_edge_cents=None,
                total_fees_cents=None,
                return_on_capital_pct=None,
                requests_made=0,
                requests_failed=0,
                retries_count=0,
                rate_limit_events_count=0,
                candidate_leg_count=cand_leg_count,
                quote_age_seconds=quote_age_s,
            )

        # 1. Check staleness before spending any network requests
        if quote_age_s is not None and quote_age_s > self.config.max_stale_seconds:
            reason = (
                f"Candidate quote timestamp is stale ({round(quote_age_s, 1)}s old "
                f"> {self.config.max_stale_seconds}s limit)"
            )
            return TargetedQualificationResult(
                qualification_id=qual_id,
                candidate_id=candidate.candidate_id,
                strategy_type=candidate.strategy_type,
                stable_key=candidate.stable_key,
                evaluated_at=now.isoformat(),
                outcome=QualificationOutcome.REJECTED_STALE.value,
                is_qualified=False,
                rejection_reason=reason,
                preliminary_candidate=candidate,
                requested_quantity=self.config.target_quantity,
                executable_quantity=0,
                gross_edge_cents=candidate.gross_edge_cents,
                net_edge_cents=None,
                total_fees_cents=None,
                return_on_capital_pct=None,
                requests_made=0,
                requests_failed=0,
                retries_count=0,
                rate_limit_events_count=0,
                candidate_leg_count=cand_leg_count,
                quote_age_seconds=quote_age_s,
            )

        # 1.5. Event-level top-of-book pre-filter (Milestone G2)
        t_pf_start = time.monotonic()
        passed, outc, ref_edge, pf_reqs, pf_fails, pf_retries, pf_rates, pf_reason = self._evaluate_event_prefilter(candidate)
        pf_duration = max(0.0, time.monotonic() - t_pf_start)
        if not passed:
            refreshed_cents = None
            if ref_edge is not None:
                cents_val = ref_edge * Decimal("100")
                if cents_val == cents_val.to_integral_value():
                    refreshed_cents = int(cents_val)
            return TargetedQualificationResult(
                qualification_id=qual_id,
                candidate_id=candidate.candidate_id,
                strategy_type=candidate.strategy_type,
                stable_key=candidate.stable_key,
                evaluated_at=now.isoformat(),
                outcome=outc or "ERROR",
                is_qualified=False,
                rejection_reason=pf_reason,
                preliminary_candidate=candidate,
                requested_quantity=self.config.target_quantity,
                executable_quantity=0,
                gross_edge_cents=refreshed_cents if refreshed_cents is not None else candidate.gross_edge_cents,
                net_edge_cents=None,
                total_fees_cents=None,
                return_on_capital_pct=None,
                requests_made=pf_reqs,
                requests_failed=pf_fails,
                retries_count=pf_retries,
                rate_limit_events_count=pf_rates,
                refreshed_gross_edge_dollars=ref_edge,
                candidate_leg_count=cand_leg_count,
                quote_age_seconds=quote_age_s,
                prefilter_duration_seconds=pf_duration,
            )

        # 2. Strategy evaluation
        strat = candidate.strategy_type

        # Strategy A: Binary Parity
        if strat in (ScreeningStrategyType.BINARY_PARITY.value, "binary_parity"):
            ticker = candidate.market_tickers[0]
            t_ob_start = time.monotonic()
            book, err_msg, reqs, fails, retries, rate_limits = self._fetch_order_book_with_retry(ticker)
            ob_duration = max(0.0, time.monotonic() - t_ob_start)
            fetch_window = ob_duration
            server_skew = 0.0

            if book is None:
                return TargetedQualificationResult(
                    qualification_id=qual_id,
                    candidate_id=candidate.candidate_id,
                    strategy_type=strat,
                    stable_key=candidate.stable_key,
                    evaluated_at=now.isoformat(),
                    outcome="ERROR",
                    is_qualified=False,
                    rejection_reason=err_msg or "Failed fetching order book",
                    preliminary_candidate=candidate,
                    requested_quantity=self.config.target_quantity,
                    executable_quantity=0,
                    gross_edge_cents=candidate.gross_edge_cents,
                    net_edge_cents=None,
                    total_fees_cents=None,
                    return_on_capital_pct=None,
                    requests_made=pf_reqs + reqs,
                    requests_failed=pf_fails + fails,
                    retries_count=pf_retries + retries,
                    rate_limit_events_count=pf_rates + rate_limits,
                    refreshed_gross_edge_dollars=ref_edge,
                    candidate_leg_count=cand_leg_count,
                    quote_age_seconds=quote_age_s,
                    prefilter_duration_seconds=pf_duration,
                    order_book_fetch_duration_seconds=ob_duration,
                    fetch_window_seconds=fetch_window,
                    server_timestamp_skew_seconds=server_skew,
                    cross_leg_skew_seconds=server_skew,
                )

            # Check client fetch window
            if fetch_window > self.config.max_fetch_window_seconds:
                reason = f"Candidate order-book fetch window ({fetch_window:.3f}s) exceeded limit ({self.config.max_fetch_window_seconds:.3f}s)"
                return TargetedQualificationResult(
                    qualification_id=qual_id,
                    candidate_id=candidate.candidate_id,
                    strategy_type=strat,
                    stable_key=candidate.stable_key,
                    evaluated_at=now.isoformat(),
                    outcome=QualificationOutcome.REJECTED_LEG_SKEW.value,
                    is_qualified=False,
                    rejection_reason=reason,
                    preliminary_candidate=candidate,
                    requested_quantity=self.config.target_quantity,
                    executable_quantity=0,
                    gross_edge_cents=candidate.gross_edge_cents,
                    net_edge_cents=None,
                    total_fees_cents=None,
                    return_on_capital_pct=None,
                    requests_made=pf_reqs + reqs,
                    requests_failed=pf_fails + fails,
                    retries_count=pf_retries + retries,
                    rate_limit_events_count=pf_rates + rate_limits,
                    refreshed_gross_edge_dollars=ref_edge,
                    candidate_leg_count=cand_leg_count,
                    quote_age_seconds=quote_age_s,
                    prefilter_duration_seconds=pf_duration,
                    order_book_fetch_duration_seconds=ob_duration,
                    fetch_window_seconds=fetch_window,
                    server_timestamp_skew_seconds=server_skew,
                    cross_leg_skew_seconds=server_skew,
                )

            is_stale = False
            staleness_reason = None
            if book.source_timestamp is not None:
                src_ts = book.source_timestamp if book.source_timestamp.tzinfo else book.source_timestamp.replace(tzinfo=timezone.utc)
                book_age = max(0.0, (now_utc - src_ts).total_seconds())
                if book_age > self.config.max_stale_seconds:
                    is_stale = True
                    staleness_reason = f"Order book snapshot age ({book_age:.1f}s) exceeds limit ({self.config.max_stale_seconds:.1f}s)"

            book_hash = compute_order_book_hash(book)
            obs = MarketObservation(
                observation_id=f"obs-{ticker}-{uuid.uuid4().hex[:6]}",
                ticker=ticker,
                observed_at=now,
                source_timestamp=book.source_timestamp or now,
                market=market,
                order_book=book,
                is_success=True,
                is_stale=is_stale,
                staleness_reason=staleness_reason,
            )

            qual = self.qualifier.qualify_binary_parity(
                observation=obs,
                requested_quantity=self.config.target_quantity,
                now=now,
            )

            # Record in evaluated cache
            self._evaluated_candidates[candidate.stable_key] = (
                candidate.gross_edge_dollars,
                candidate.constituent_quotes,
            )

            return TargetedQualificationResult(
                qualification_id=qual_id,
                candidate_id=candidate.candidate_id,
                strategy_type=strat,
                stable_key=candidate.stable_key,
                evaluated_at=now.isoformat(),
                outcome=qual.outcome.value,
                is_qualified=qual.is_qualified,
                rejection_reason=qual.rejection_reason,
                preliminary_candidate=candidate,
                requested_quantity=qual.requested_quantity,
                executable_quantity=qual.executable_quantity,
                gross_edge_cents=qual.gross_edge_cents,
                net_edge_cents=qual.net_edge_cents,
                total_fees_cents=qual.total_fees_cents,
                return_on_capital_pct=qual.return_on_capital_pct,
                leg_details=qual.leg_details,
                order_book_hashes={ticker: book_hash},
                requests_made=pf_reqs + reqs,
                requests_failed=pf_fails + fails,
                retries_count=pf_retries + retries,
                rate_limit_events_count=pf_rates + rate_limits,
                refreshed_gross_edge_dollars=ref_edge,
                candidate_leg_count=cand_leg_count,
                quote_age_seconds=quote_age_s,
                prefilter_duration_seconds=pf_duration,
                order_book_fetch_duration_seconds=ob_duration,
                fetch_window_seconds=fetch_window,
                server_timestamp_skew_seconds=server_skew,
                cross_leg_skew_seconds=server_skew,
                execution_qualification=qual,
            )

        # Strategy B: MECE Event Basket (Long YES or Long NO)
        if strat in (
            ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
            ScreeningStrategyType.MECE_BASKET_LONG_NO.value,
            "mece_basket_long_yes",
            "mece_basket_long_no",
        ):
            basket_side = YES if "yes" in strat.lower() else NO
            books: Dict[str, NormalizedOrderBook] = {}
            book_hashes: Dict[str, str] = {}
            total_reqs = pf_reqs
            total_fails = pf_fails
            total_retries = pf_retries
            total_rate_limits = pf_rates

            t_ob_start = time.monotonic()
            failed_leg_err: Optional[str] = None
            tickers = candidate.market_tickers or ()
            workers = min(self.config.max_concurrency, len(tickers))

            if workers > 1:
                cancel_event = threading.Event()
                futures_map: Dict[concurrent.futures.Future, str] = {}
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    for ticker in tickers:
                        fut = executor.submit(self._execute_order_book_fetch, ticker, cancel_event)
                        futures_map[fut] = ticker

                    for fut in as_completed(futures_map):
                        ticker = futures_map[fut]
                        try:
                            book, err_msg, reqs, fails, retries, rate_limits = fut.result()
                            total_reqs += reqs
                            total_fails += fails
                            total_retries += retries
                            total_rate_limits += rate_limits
                            if book is None and failed_leg_err is None:
                                failed_leg_err = err_msg or f"Failed fetching order book for constituent leg {ticker}"
                                cancel_event.set()
                                for f in futures_map:
                                    f.cancel()
                            elif book is not None:
                                books[ticker] = book
                                book_hashes[ticker] = compute_order_book_hash(book)
                        except CancelledError:
                            pass
                        except Exception as exc:
                            if failed_leg_err is None:
                                failed_leg_err = f"Unexpected error in thread fetching order book for {ticker}: {exc}"
                                cancel_event.set()
                                for f in futures_map:
                                    f.cancel()
            else:
                for ticker in tickers:
                    book, err_msg, reqs, fails, retries, rate_limits = self._fetch_order_book_with_retry(ticker)
                    total_reqs += reqs
                    total_fails += fails
                    total_retries += retries
                    total_rate_limits += rate_limits
                    if book is None:
                        failed_leg_err = err_msg or f"Failed fetching order book for constituent leg {ticker}"
                        break
                    books[ticker] = book
                    book_hashes[ticker] = compute_order_book_hash(book)

            ob_duration = max(0.0, time.monotonic() - t_ob_start)
            fetch_window = ob_duration

            if failed_leg_err is not None:
                return TargetedQualificationResult(
                    qualification_id=qual_id,
                    candidate_id=candidate.candidate_id,
                    strategy_type=strat,
                    stable_key=candidate.stable_key,
                    evaluated_at=now.isoformat(),
                    outcome="ERROR",
                    is_qualified=False,
                    rejection_reason=failed_leg_err,
                    preliminary_candidate=candidate,
                    requested_quantity=self.config.target_quantity,
                    executable_quantity=0,
                    gross_edge_cents=candidate.gross_edge_cents,
                    net_edge_cents=None,
                    total_fees_cents=None,
                    return_on_capital_pct=None,
                    requests_made=total_reqs,
                    requests_failed=total_fails,
                    retries_count=total_retries,
                    rate_limit_events_count=total_rate_limits,
                    refreshed_gross_edge_dollars=ref_edge,
                    candidate_leg_count=cand_leg_count,
                    quote_age_seconds=quote_age_s,
                    prefilter_duration_seconds=pf_duration,
                    order_book_fetch_duration_seconds=ob_duration,
                    fetch_window_seconds=fetch_window,
                    server_timestamp_skew_seconds=None,
                    cross_leg_skew_seconds=None,
                )

            # Compute server timestamp skew across constituent order books
            src_ts_list = [b.source_timestamp for b in books.values() if b.source_timestamp is not None]
            if len(src_ts_list) >= 2:
                server_skew = max(0.0, (max(src_ts_list) - min(src_ts_list)).total_seconds())
            elif len(src_ts_list) == 1:
                server_skew = 0.0
            else:
                server_skew = None

            # Enforce dual-skew check
            # 1. Client-side fetch window
            if fetch_window > self.config.max_fetch_window_seconds:
                reason = f"Candidate order-book fetch window ({fetch_window:.3f}s) exceeded limit ({self.config.max_fetch_window_seconds:.3f}s)"
                return TargetedQualificationResult(
                    qualification_id=qual_id,
                    candidate_id=candidate.candidate_id,
                    strategy_type=strat,
                    stable_key=candidate.stable_key,
                    evaluated_at=now.isoformat(),
                    outcome=QualificationOutcome.REJECTED_LEG_SKEW.value,
                    is_qualified=False,
                    rejection_reason=reason,
                    preliminary_candidate=candidate,
                    requested_quantity=self.config.target_quantity,
                    executable_quantity=0,
                    gross_edge_cents=candidate.gross_edge_cents,
                    net_edge_cents=None,
                    total_fees_cents=None,
                    return_on_capital_pct=None,
                    requests_made=total_reqs,
                    requests_failed=total_fails,
                    retries_count=total_retries,
                    rate_limit_events_count=total_rate_limits,
                    refreshed_gross_edge_dollars=ref_edge,
                    candidate_leg_count=cand_leg_count,
                    quote_age_seconds=quote_age_s,
                    prefilter_duration_seconds=pf_duration,
                    order_book_fetch_duration_seconds=ob_duration,
                    fetch_window_seconds=fetch_window,
                    server_timestamp_skew_seconds=server_skew,
                    cross_leg_skew_seconds=server_skew,
                )

            # 2. Server-side timestamp skew
            if server_skew is not None and server_skew > self.config.max_leg_skew_seconds:
                reason = f"Constituent leg timestamp skew ({server_skew:.3f}s) exceeded limit ({self.config.max_leg_skew_seconds:.3f}s)"
                return TargetedQualificationResult(
                    qualification_id=qual_id,
                    candidate_id=candidate.candidate_id,
                    strategy_type=strat,
                    stable_key=candidate.stable_key,
                    evaluated_at=now.isoformat(),
                    outcome=QualificationOutcome.REJECTED_LEG_SKEW.value,
                    is_qualified=False,
                    rejection_reason=reason,
                    preliminary_candidate=candidate,
                    requested_quantity=self.config.target_quantity,
                    executable_quantity=0,
                    gross_edge_cents=candidate.gross_edge_cents,
                    net_edge_cents=None,
                    total_fees_cents=None,
                    return_on_capital_pct=None,
                    requests_made=total_reqs,
                    requests_failed=total_fails,
                    retries_count=total_retries,
                    rate_limit_events_count=total_rate_limits,
                    refreshed_gross_edge_dollars=ref_edge,
                    candidate_leg_count=cand_leg_count,
                    quote_age_seconds=quote_age_s,
                    prefilter_duration_seconds=pf_duration,
                    order_book_fetch_duration_seconds=ob_duration,
                    fetch_window_seconds=fetch_window,
                    server_timestamp_skew_seconds=server_skew,
                    cross_leg_skew_seconds=server_skew,
                )

            # Build constituent observations
            observations: Dict[str, MarketObservation] = {}
            for t, b in books.items():
                is_b_stale = False
                stale_reason = None
                if b.source_timestamp is not None:
                    src_ts = b.source_timestamp if b.source_timestamp.tzinfo else b.source_timestamp.replace(tzinfo=timezone.utc)
                    b_age = max(0.0, (now_utc - src_ts).total_seconds())
                    if b_age > self.config.max_stale_seconds:
                        is_b_stale = True
                        stale_reason = f"Constituent leg {t} order book snapshot age ({b_age:.1f}s) exceeds limit ({self.config.max_stale_seconds:.1f}s)"
                observations[t] = MarketObservation(
                    observation_id=f"obs-{t}-{uuid.uuid4().hex[:6]}",
                    ticker=t,
                    observed_at=now,
                    source_timestamp=b.source_timestamp or now,
                    market=None,
                    order_book=b,
                    is_success=True,
                    is_stale=is_b_stale,
                    staleness_reason=stale_reason,
                )

            declared_basket = DeclaredMeceBasket(
                event_ticker=candidate.event_ticker,
                outcomes=candidate.market_tickers,
                market_outcome_map={t: t for t in candidate.market_tickers},
                basket_side=basket_side,
                basket_id=f"BASKET-{candidate.event_ticker}",
            )

            qual = self.qualifier.qualify_mece_basket(
                basket=declared_basket,
                observations=observations,
                requested_quantity=self.config.target_quantity,
                now=now,
            )

            self._evaluated_candidates[candidate.stable_key] = (
                candidate.gross_edge_dollars,
                candidate.constituent_quotes,
            )

            return TargetedQualificationResult(
                qualification_id=qual_id,
                candidate_id=candidate.candidate_id,
                strategy_type=strat,
                stable_key=candidate.stable_key,
                evaluated_at=now.isoformat(),
                outcome=qual.outcome.value,
                is_qualified=qual.is_qualified,
                rejection_reason=qual.rejection_reason,
                preliminary_candidate=candidate,
                requested_quantity=qual.requested_quantity,
                executable_quantity=qual.executable_quantity,
                gross_edge_cents=qual.gross_edge_cents,
                net_edge_cents=qual.net_edge_cents,
                total_fees_cents=qual.total_fees_cents,
                return_on_capital_pct=qual.return_on_capital_pct,
                leg_details=qual.leg_details,
                order_book_hashes=book_hashes,
                requests_made=total_reqs,
                requests_failed=total_fails,
                retries_count=total_retries,
                rate_limit_events_count=total_rate_limits,
                refreshed_gross_edge_dollars=ref_edge,
                candidate_leg_count=cand_leg_count,
                quote_age_seconds=quote_age_s,
                prefilter_duration_seconds=pf_duration,
                order_book_fetch_duration_seconds=ob_duration,
                fetch_window_seconds=fetch_window,
                server_timestamp_skew_seconds=server_skew,
                cross_leg_skew_seconds=server_skew,
                execution_qualification=qual,
            )

        # Strategy C: Crossed Book
        if strat in (ScreeningStrategyType.CROSSED_BOOK.value, "crossed_book"):
            ticker = candidate.market_tickers[0]
            t_ob_start = time.monotonic()
            book, err_msg, reqs, fails, retries, rate_limits = self._fetch_order_book_with_retry(ticker)
            ob_duration = max(0.0, time.monotonic() - t_ob_start)
            book_hash = compute_order_book_hash(book) if book else "none"

            reason = (
                "Crossed book is an exchange quote anomaly or transient discrepancy; "
                "not an atomic risk-free execution strategy in V22 qualifier"
            )
            return TargetedQualificationResult(
                qualification_id=qual_id,
                candidate_id=candidate.candidate_id,
                strategy_type=strat,
                stable_key=candidate.stable_key,
                evaluated_at=now.isoformat(),
                outcome="UNSUPPORTED_STRATEGY",
                is_qualified=False,
                rejection_reason=reason,
                preliminary_candidate=candidate,
                requested_quantity=self.config.target_quantity,
                executable_quantity=0,
                gross_edge_cents=candidate.gross_edge_cents,
                net_edge_cents=None,
                total_fees_cents=None,
                return_on_capital_pct=None,
                order_book_hashes={ticker: book_hash},
                requests_made=reqs,
                requests_failed=fails,
                retries_count=retries,
                rate_limit_events_count=rate_limits,
                candidate_leg_count=cand_leg_count,
                quote_age_seconds=quote_age_s,
                prefilter_duration_seconds=pf_duration,
                order_book_fetch_duration_seconds=ob_duration,
                fetch_window_seconds=ob_duration,
                server_timestamp_skew_seconds=0.0,
                cross_leg_skew_seconds=0.0,
            )

        # Unsupported Strategy
        return TargetedQualificationResult(
            qualification_id=qual_id,
            candidate_id=candidate.candidate_id,
            strategy_type=strat,
            stable_key=candidate.stable_key,
            evaluated_at=now.isoformat(),
            outcome="UNSUPPORTED_STRATEGY",
            is_qualified=False,
            rejection_reason=f"Strategy {strat!r} is not supported for execution qualification",
            preliminary_candidate=candidate,
            requested_quantity=self.config.target_quantity,
            executable_quantity=0,
            gross_edge_cents=candidate.gross_edge_cents,
            net_edge_cents=None,
            total_fees_cents=None,
            return_on_capital_pct=None,
            requests_made=0,
            requests_failed=0,
            retries_count=0,
            rate_limit_events_count=0,
            candidate_leg_count=cand_leg_count,
            quote_age_seconds=quote_age_s,
            prefilter_duration_seconds=pf_duration,
        )

    def process_queue(
        self,
        markets: Optional[Mapping[str, NormalizedMarket]] = None,
        deadline: Optional[float] = None,
    ) -> TargetedQualificationSummary:
        """Process all enqueued preliminary candidates and return qualification summary."""
        start_time = self._now()
        session_id = f"tqual-pass-{uuid.uuid4().hex[:8]}"

        candidates_to_process = list(self._queue)
        self._queue.clear()
        self._queue_keys.clear()

        # Collect gated pre-qualification rejections (e.g. REJECTED_BASKET_TOO_LARGE)
        results: List[TargetedQualificationResult] = list(self._gated_rejections)
        self._gated_rejections.clear()

        outcomes_breakdown: Dict[str, int] = {}
        for r in results:
            outcomes_breakdown[r.outcome] = outcomes_breakdown.get(r.outcome, 0) + 1

        qualified_cnt = 0
        rejected_cnt = len(results)
        stale_cnt = 0
        error_cnt = 0
        unsupported_cnt = 0
        total_requests = 0
        total_fails = 0
        total_retries = 0
        total_rate_limits = 0

        for i, cand in enumerate(candidates_to_process):
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Qualification queue processing halted: run duration deadline reached.")
                for remaining in candidates_to_process[i:]:
                    leg_cnt = len(remaining.market_tickers) if remaining.market_tickers else 0
                    skip_res = TargetedQualificationResult(
                        qualification_id=f"tqual-skip-{uuid.uuid4().hex[:8]}",
                        candidate_id=remaining.candidate_id,
                        strategy_type=remaining.strategy_type,
                        stable_key=remaining.stable_key,
                        evaluated_at=self._now().isoformat(),
                        outcome="SKIPPED_TIMEOUT",
                        is_qualified=False,
                        rejection_reason="Qualification halted by run duration deadline",
                        preliminary_candidate=remaining,
                        requested_quantity=self.config.target_quantity,
                        executable_quantity=0,
                        gross_edge_cents=remaining.gross_edge_cents,
                        net_edge_cents=None,
                        total_fees_cents=None,
                        return_on_capital_pct=None,
                        candidate_leg_count=leg_cnt,
                    )
                    results.append(skip_res)
                    outcomes_breakdown["SKIPPED_TIMEOUT"] = outcomes_breakdown.get("SKIPPED_TIMEOUT", 0) + 1
                    rejected_cnt += 1
                break

            m = (markets or {}).get(cand.market_tickers[0]) if cand.market_tickers else None
            res = self.qualify_candidate(cand, market=m)
            results.append(res)
            total_requests += res.requests_made
            total_fails += res.requests_failed
            total_retries += res.retries_count
            total_rate_limits += res.rate_limit_events_count

            outcome = res.outcome
            outcomes_breakdown[outcome] = outcomes_breakdown.get(outcome, 0) + 1

            if res.is_qualified:
                qualified_cnt += 1
            elif outcome == QualificationOutcome.REJECTED_STALE.value:
                stale_cnt += 1
                rejected_cnt += 1
            elif outcome == "ERROR":
                error_cnt += 1
            elif outcome == "UNSUPPORTED_STRATEGY":
                unsupported_cnt += 1
            else:
                rejected_cnt += 1

        end_time = self._now()
        duration = max(0.0, (end_time - start_time).total_seconds())

        summary = TargetedQualificationSummary(
            session_id=session_id,
            started_at=start_time.isoformat(),
            completed_at=end_time.isoformat(),
            duration_seconds=duration,
            candidates_enqueued=len(candidates_to_process),
            candidates_evaluated=len(results),
            qualified_count=qualified_cnt,
            rejected_count=rejected_cnt,
            stale_count=stale_cnt,
            error_count=error_cnt,
            unsupported_count=unsupported_cnt,
            requests_made=total_requests,
            requests_failed=total_fails,
            retries_count=total_retries,
            rate_limit_events_count=total_rate_limits,
            queue_overflow_count=self._queue_overflow_count,
            outcomes_breakdown=outcomes_breakdown,
            results=results,
        )

        self._last_summary = summary
        return summary

    def get_summary(self) -> Optional[Dict[str, Any]]:
        """Return serialized summary of the latest qualification pass."""
        return self._last_summary.to_dict() if self._last_summary else None

    def get_qualified_candidates(self) -> List[TargetedQualificationResult]:
        """Return only execution-qualified opportunities from the latest pass."""
        if not self._last_summary:
            return []
        return [r for r in self._last_summary.results if r.is_qualified]


# =============================================================================
# Phase 2D: Standalone Continuous Breadth-First Scanner Runner
# =============================================================================

@dataclass(frozen=True)
class BreadthScannerRunnerConfig:
    """Configuration options for standalone continuous breadth-first scanner."""

    environment: str = "production"
    refresh_interval_seconds: float = 30.0
    min_cycle_cooldown_seconds: float = 2.0
    page_size: int = 100
    category_filter: Optional[Tuple[str, ...]] = None
    series_ticker_filter: Optional[Tuple[str, ...]] = None
    status_filter: str = "open"
    target_quantity: int = 1
    max_queue_size: int = 100
    max_concurrency: int = 4
    max_requests_per_second: float = 5.0
    max_stale_seconds: float = 60.0
    max_leg_skew_seconds: float = 5.0
    max_fetch_window_seconds: float = 5.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5
    fee_schedule: FeeScheduleType = FeeScheduleType.KALSHI_OFFICIAL_TAKER
    require_full_quantity: bool = False
    min_net_profit_cents: int = 1
    require_source_timestamp: bool = False
    max_candidate_legs: int = 8
    enable_event_prefilter: bool = True
    event_cache_ttl_seconds: float = 2.0
    check_prefilter_quote_staleness: bool = False
    output_dir: Optional[Path] = None
    state_file: Optional[Path] = None
    summaries_file: Optional[Path] = None
    candidate_history_file: Optional[Path] = None
    qualification_history_file: Optional[Path] = None
    max_cycles: Optional[int] = None
    duration_seconds: Optional[float] = None
    single_pass: bool = False
    enable_lookahead_gate: bool = True
    lookahead_threshold_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.refresh_interval_seconds < 0:
            raise ValueError("refresh_interval_seconds cannot be negative")
        if self.min_cycle_cooldown_seconds < 0:
            raise ValueError("min_cycle_cooldown_seconds cannot be negative")
        if self.page_size <= 0:
            raise ValueError("page_size must be positive")
        if self.max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be positive")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")
        if self.max_leg_skew_seconds <= 0:
            raise ValueError("max_leg_skew_seconds must be positive")
        if self.max_fetch_window_seconds <= 0:
            raise ValueError("max_fetch_window_seconds must be positive")
        if self.target_quantity <= 0:
            raise ValueError("target_quantity must be positive")
        if self.min_net_profit_cents < 0:
            raise ValueError("min_net_profit_cents must be non-negative")
        if self.max_candidate_legs <= 0:
            raise ValueError("max_candidate_legs must be positive")
        if self.event_cache_ttl_seconds < 0:
            raise ValueError("event_cache_ttl_seconds must be non-negative")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if self.lookahead_threshold_seconds < 0:
            raise ValueError("lookahead_threshold_seconds cannot be negative")
        if self.max_cycles is not None and self.max_cycles <= 0:
            raise ValueError("max_cycles must be positive")


@dataclass
class ScanCycleSummary:
    """Summary record for an individual end-to-end breadth scanner cycle."""

    cycle_number: int
    session_id: str
    started_at: str
    completed_at: str
    duration_seconds: float
    is_catalog_complete: bool
    markets_discovered: int
    new_markets_count: int
    changed_markets_count: int
    closed_or_settled_markets_count: int
    eligible_markets: int
    markets_screened: int
    events_screened: int
    strategies_evaluated: int
    preliminary_candidates_count: int
    new_preliminary_candidates_count: int
    deduplicated_preliminary_candidates_count: int
    candidates_enqueued: int
    candidates_evaluated: int
    qualified_count: int
    rejected_count: int
    stale_count: int
    error_count: int
    unsupported_count: int
    queue_overflow_count: int
    requests_made: int
    requests_failed: int
    retries_count: int
    rate_limit_events_count: int
    error_message: Optional[str] = None
    scheduled_interval_seconds: float = 0.0
    start_to_start_interval_seconds: Optional[float] = None
    is_overrun: bool = False
    overrun_seconds: float = 0.0
    next_cycle_delay_seconds: float = 0.0
    fresh_quote_markets_count: int = 0
    stale_or_missing_quote_markets_count: int = 0
    invalidated_candidates_count: int = 0
    network_time_seconds: float = 0.0
    json_parse_time_seconds: float = 0.0
    normalization_time_seconds: float = 0.0
    deduplication_time_seconds: float = 0.0
    screening_time_seconds: float = 0.0
    total_discovery_time_seconds: float = 0.0
    total_qualification_time_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_number": self.cycle_number,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 4),
            "is_catalog_complete": self.is_catalog_complete,
            "markets_discovered": self.markets_discovered,
            "new_markets_count": self.new_markets_count,
            "changed_markets_count": self.changed_markets_count,
            "closed_or_settled_markets_count": self.closed_or_settled_markets_count,
            "eligible_markets": self.eligible_markets,
            "markets_screened": self.markets_screened,
            "events_screened": self.events_screened,
            "strategies_evaluated": self.strategies_evaluated,
            "preliminary_candidates_count": self.preliminary_candidates_count,
            "new_preliminary_candidates_count": self.new_preliminary_candidates_count,
            "deduplicated_preliminary_candidates_count": self.deduplicated_preliminary_candidates_count,
            "candidates_enqueued": self.candidates_enqueued,
            "candidates_evaluated": self.candidates_evaluated,
            "qualified_count": self.qualified_count,
            "rejected_count": self.rejected_count,
            "stale_count": self.stale_count,
            "error_count": self.error_count,
            "unsupported_count": self.unsupported_count,
            "queue_overflow_count": self.queue_overflow_count,
            "requests_made": self.requests_made,
            "requests_failed": self.requests_failed,
            "retries_count": self.retries_count,
            "rate_limit_events_count": self.rate_limit_events_count,
            "error_message": self.error_message,
            "scheduled_interval_seconds": self.scheduled_interval_seconds,
            "start_to_start_interval_seconds": self.start_to_start_interval_seconds,
            "is_overrun": self.is_overrun,
            "overrun_seconds": round(self.overrun_seconds, 4),
            "next_cycle_delay_seconds": round(self.next_cycle_delay_seconds, 4),
            "fresh_quote_markets_count": self.fresh_quote_markets_count,
            "stale_or_missing_quote_markets_count": self.stale_or_missing_quote_markets_count,
            "invalidated_candidates_count": self.invalidated_candidates_count,
            "network_time_seconds": round(self.network_time_seconds, 4),
            "json_parse_time_seconds": round(self.json_parse_time_seconds, 4),
            "normalization_time_seconds": round(self.normalization_time_seconds, 4),
            "deduplication_time_seconds": round(self.deduplication_time_seconds, 4),
            "screening_time_seconds": round(self.screening_time_seconds, 4),
            "total_discovery_time_seconds": round(self.total_discovery_time_seconds, 4),
            "total_qualification_time_seconds": round(self.total_qualification_time_seconds, 4),
        }


@dataclass
class BreadthScannerHealth:
    """Cumulative state and health telemetry for continuous breadth scanner."""

    session_id: str
    status: str
    started_at: str
    updated_at: str
    current_cycle: int = 0
    total_cycles_count: int = 0
    successful_cycles_count: int = 0
    failed_cycles_count: int = 0
    last_successful_discovery_time: Optional[str] = None
    last_complete_discovery_time: Optional[str] = None
    last_catalog_complete: bool = False
    scheduled_interval_seconds: float = 0.0
    last_cycle_duration_seconds: float = 0.0
    last_start_to_start_interval_seconds: Optional[float] = None
    total_overruns_count: int = 0
    total_overrun_seconds: float = 0.0
    last_overrun_seconds: float = 0.0
    is_currently_overrunning: bool = False
    fresh_quote_markets_count: int = 0
    stale_or_missing_quote_markets_count: int = 0
    total_candidates_invalidated: int = 0
    total_unique_markets_discovered: int = 0
    total_eligible_markets: int = 0
    total_unique_markets_screened: int = 0
    total_events_evaluated: int = 0
    total_strategies_evaluated: int = 0
    total_preliminary_candidates_generated: int = 0
    candidates_enqueued: int = 0
    candidates_pending: int = 0
    candidates_deduplicated: int = 0
    qualification_counts: Dict[str, int] = field(default_factory=dict)
    qualified_candidates_count: int = 0
    api_requests_count: int = 0
    api_retries_count: int = 0
    api_failures_count: int = 0
    rate_limit_events_count: int = 0
    queue_overflow_count: int = 0
    last_error: Optional[str] = None
    last_error_time: Optional[str] = None
    output_dir: str = ""
    state_file: str = ""
    last_persistence_time: Optional[str] = None
    stopped_reason: Optional[str] = None
    final_elapsed_seconds: Optional[float] = None
    shutdown_cleanly: bool = False
    total_cycles_incomplete: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "current_cycle": self.current_cycle,
            "total_cycles_count": self.total_cycles_count,
            "successful_cycles_count": self.successful_cycles_count,
            "failed_cycles_count": self.failed_cycles_count,
            "total_cycles_incomplete": self.total_cycles_incomplete,
            "stopped_reason": self.stopped_reason,
            "final_elapsed_seconds": round(self.final_elapsed_seconds, 4) if self.final_elapsed_seconds is not None else None,
            "shutdown_cleanly": self.shutdown_cleanly,
            "last_successful_discovery_time": self.last_successful_discovery_time,
            "last_complete_discovery_time": self.last_complete_discovery_time,
            "last_catalog_complete": self.last_catalog_complete,
            "scheduled_interval_seconds": self.scheduled_interval_seconds,
            "last_cycle_duration_seconds": round(self.last_cycle_duration_seconds, 4),
            "last_start_to_start_interval_seconds": round(self.last_start_to_start_interval_seconds, 4) if self.last_start_to_start_interval_seconds is not None else None,
            "total_overruns_count": self.total_overruns_count,
            "total_overrun_seconds": round(self.total_overrun_seconds, 4),
            "last_overrun_seconds": round(self.last_overrun_seconds, 4),
            "is_currently_overrunning": self.is_currently_overrunning,
            "fresh_quote_markets_count": self.fresh_quote_markets_count,
            "stale_or_missing_quote_markets_count": self.stale_or_missing_quote_markets_count,
            "total_candidates_invalidated": self.total_candidates_invalidated,
            "total_unique_markets_discovered": self.total_unique_markets_discovered,
            "total_eligible_markets": self.total_eligible_markets,
            "total_unique_markets_screened": self.total_unique_markets_screened,
            "total_events_evaluated": self.total_events_evaluated,
            "total_strategies_evaluated": self.total_strategies_evaluated,
            "total_preliminary_candidates_generated": self.total_preliminary_candidates_generated,
            "candidates_enqueued": self.candidates_enqueued,
            "candidates_pending": self.candidates_pending,
            "candidates_deduplicated": self.candidates_deduplicated,
            "qualification_counts": dict(self.qualification_counts),
            "qualified_candidates_count": self.qualified_candidates_count,
            "api_requests_count": self.api_requests_count,
            "api_retries_count": self.api_retries_count,
            "api_failures_count": self.api_failures_count,
            "rate_limit_events_count": self.rate_limit_events_count,
            "queue_overflow_count": self.queue_overflow_count,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
            "output_dir": self.output_dir,
            "state_file": self.state_file,
            "last_persistence_time": self.last_persistence_time,
        }


class BreadthScannerRunner:
    """Standalone continuous breadth-first scanner coordinating Discovery, Screening, and Qualification.

    - Discovers the broad relevant Kalshi market catalog using cursor pagination.
    - Screens discovered markets using lightweight top-of-book rules without L2 fetching.
    - Submits only eligible preliminary candidates to targeted L2 order-book qualification.
    - Reconciles markets and candidates across cycles, tracking newly discovered, updated, and closed markets.
    - Persists health telemetry, compact summaries, candidate changes, and qualification records.
    - Operates continuously until stopped, or for a bounded duration/cycles.
    - Strictly read-only; never touches private exchange endpoints or order-placement functions.
    """

    def __init__(
        self,
        client: Optional[KalshiRestClient] = None,
        config: Optional[BreadthScannerRunnerConfig] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
        sleep_func: Optional[Callable[[float], None]] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self.config = config or BreadthScannerRunnerConfig()
        self.client = client or KalshiRestClient(environment=self.config.environment)
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_func or time.sleep

        # Single shared rate limiter across all discovery, prefilter, and order-book requests
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            rate_per_second=self.config.max_requests_per_second,
            sleep_func=self._sleep,
        )

        # Resolve output paths
        self.output_dir = Path(self.config.output_dir) if self.config.output_dir else Path.cwd() / "data" / "breadth_scanner"
        self.state_file = Path(self.config.state_file) if self.config.state_file else self.output_dir / "scanner_state.json"
        self.summaries_file = Path(self.config.summaries_file) if self.config.summaries_file else self.output_dir / "scan_summaries.jsonl"
        self.candidate_history_file = Path(self.config.candidate_history_file) if self.config.candidate_history_file else self.output_dir / "candidate_history.jsonl"
        self.qualification_history_file = Path(self.config.qualification_history_file) if self.config.qualification_history_file else self.output_dir / "qualification_history.jsonl"

        # Instantiate Stage A: Discovery Engine
        disc_cfg = BreadthScannerConfig(
            environment=self.config.environment,
            discovery_page_size=self.config.page_size,
            category_filter=self.config.category_filter,
            series_ticker_filter=self.config.series_ticker_filter,
            status_filter=self.config.status_filter,
            max_retries=self.config.max_retries,
            retry_backoff_seconds=self.config.retry_backoff_seconds,
            max_stale_seconds=self.config.max_stale_seconds,
            max_leg_skew_seconds=self.config.max_leg_skew_seconds,
            max_requests_per_second=self.config.max_requests_per_second,
            raise_on_error=False,
        )
        self._discovery_engine = CatalogDiscoveryEngine(
            client=self.client,
            config=disc_cfg,
            time_provider=self._time_provider,
            sleep_func=self._sleep,
            rate_limiter=self._rate_limiter,
        )

        # Instantiate Stage B: Screener
        self._screener = BreadthArbitrageScreener(
            config=disc_cfg,
            time_provider=self._time_provider,
        )

        # Instantiate Stage C: Targeted Qualifier
        qual_cfg = TargetedOrderBookQualifierConfig(
            target_quantity=self.config.target_quantity,
            max_queue_size=self.config.max_queue_size,
            max_concurrency=self.config.max_concurrency,
            max_requests_per_second=self.config.max_requests_per_second,
            max_stale_seconds=self.config.max_stale_seconds,
            max_leg_skew_seconds=self.config.max_leg_skew_seconds,
            max_fetch_window_seconds=self.config.max_fetch_window_seconds,
            max_retries=self.config.max_retries,
            retry_backoff_seconds=self.config.retry_backoff_seconds,
            fee_schedule=self.config.fee_schedule,
            require_full_quantity=self.config.require_full_quantity,
            min_net_profit_cents=self.config.min_net_profit_cents,
            require_source_timestamp=self.config.require_source_timestamp,
            max_candidate_legs=self.config.max_candidate_legs,
            enable_event_prefilter=self.config.enable_event_prefilter,
            event_cache_ttl_seconds=self.config.event_cache_ttl_seconds,
            check_prefilter_quote_staleness=self.config.check_prefilter_quote_staleness,
        )
        self._qualifier = TargetedOrderBookQualifier(
            client=self.client,
            config=qual_cfg,
            time_provider=self._time_provider,
            sleep_func=self._sleep,
            rate_limiter=self._rate_limiter,
        )

        # Internal state & tracking
        self.session_id = f"bscan-{uuid.uuid4().hex[:8]}"
        self._cycle_counter: int = 0
        self._is_running: bool = False
        self._stop_requested: bool = False
        self._is_executing_cycle: bool = False
        self._execution_lock = threading.Lock()
        self._last_cycle_start_monotonic: Optional[float] = None

        self._known_markets: Dict[str, NormalizedMarket] = {}
        self._known_candidates: Dict[str, PreliminaryCandidate] = {}
        self._qualified_results: Dict[str, TargetedQualificationResult] = {}
        self._all_unique_markets: Set[str] = set()
        self._all_unique_screened: Set[str] = set()
        self._all_unique_events: Set[str] = set()
        self._last_good_catalog: Optional[CatalogDiscoveryResult] = None

        now_iso = self._now().isoformat()
        self._health = BreadthScannerHealth(
            session_id=self.session_id,
            status="INITIALIZING",
            started_at=now_iso,
            updated_at=now_iso,
            scheduled_interval_seconds=self.config.refresh_interval_seconds,
            output_dir=str(self.output_dir),
            state_file=str(self.state_file),
        )

        # Safe state reload if existing state file is present
        self._load_persisted_state()

    def _now(self) -> datetime:
        return self._time_provider()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def rate_limiter(self) -> TokenBucketRateLimiter:
        """Active shared rate limiter instance across all engine stages."""
        return self._rate_limiter

    def get_health(self) -> BreadthScannerHealth:
        """Return a copy of the current scanner health and telemetry."""
        return copy.deepcopy(self._health)

    def stop(self) -> None:
        """Signal cooperative stop for the scanner loop."""
        self._stop_requested = True
        self._discovery_engine.stop()

    def _load_persisted_state(self) -> bool:
        """Safely attempt to reload previous scanner state from disk on startup."""
        if not self.state_file.exists():
            return False

        try:
            content = self.state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                logger.warning("Persisted state file %s did not contain a valid JSON object.", self.state_file)
                self._health.last_error = f"Corrupt state file: expected object, got {type(data).__name__}"
                self._health.last_error_time = self._now().isoformat()
                return False

            # Restore previous session metrics without conflating cycles
            prev_unique = data.get("total_unique_markets_discovered", 0)
            if isinstance(prev_unique, int) and prev_unique > 0:
                self._health.total_unique_markets_discovered = prev_unique

            prev_screened = data.get("total_unique_markets_screened", 0)
            if isinstance(prev_screened, int) and prev_screened > 0:
                self._health.total_unique_markets_screened = prev_screened

            prev_candidates = data.get("total_preliminary_candidates_generated", 0)
            if isinstance(prev_candidates, int) and prev_candidates > 0:
                self._health.total_preliminary_candidates_generated = prev_candidates

            prev_qual_counts = data.get("qualification_counts", {})
            if isinstance(prev_qual_counts, dict):
                self._health.qualification_counts.update(prev_qual_counts)

            prev_qualified = data.get("qualified_candidates_count", 0)
            if isinstance(prev_qualified, int):
                self._health.qualified_candidates_count = prev_qualified

            logger.info("Successfully loaded prior state from %s (previous session: %s)", self.state_file, data.get("session_id"))
            return True
        except Exception as exc:
            logger.warning("Failed reading persisted state from %s: %s", self.state_file, exc)
            self._health.last_error = f"Failed loading state: {exc}"
            self._health.last_error_time = self._now().isoformat()
            return False

    def _persist_state(self, cycle_summary: Optional[ScanCycleSummary] = None, new_candidates: Optional[Sequence[PreliminaryCandidate]] = None, new_qual_results: Optional[Sequence[TargetedQualificationResult]] = None) -> None:
        """Persist scanner state, cycle summary, and candidate changes to disk."""
        now_iso = self._now().isoformat()
        self._health.updated_at = now_iso
        self._health.last_persistence_time = now_iso

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)

            # 1. Append candidate discoveries and quote updates
            if new_candidates and self.candidate_history_file:
                with open(self.candidate_history_file, "a", encoding="utf-8") as f:
                    for cand in new_candidates:
                        record = {
                            "recorded_at": now_iso,
                            "cycle_number": self._cycle_counter,
                            "candidate": cand.to_dict(),
                        }
                        f.write(json.dumps(record) + "\n")

            # 2. Append targeted qualification results
            if new_qual_results and self.qualification_history_file:
                with open(self.qualification_history_file, "a", encoding="utf-8") as f:
                    for res in new_qual_results:
                        record = {
                            "recorded_at": now_iso,
                            "cycle_number": self._cycle_counter,
                            "qualification": res.to_dict(),
                        }
                        f.write(json.dumps(record) + "\n")

            # 3. Append compact cycle summary
            if cycle_summary and self.summaries_file:
                with open(self.summaries_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(cycle_summary.to_dict()) + "\n")

            # 4. Atomically write scanner health state
            tmp_path = self.state_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(self._health.to_dict(), indent=2), encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception as exc:
            logger.warning("Failed to persist scanner telemetry to %s: %s", self.output_dir, exc)
            self._health.last_error = f"Persistence error: {exc}"
            self._health.last_error_time = now_iso

    def run_cycle(self, deadline: Optional[float] = None) -> ScanCycleSummary:
        """Execute a single end-to-end breadth scanner cycle."""
        with self._execution_lock:
            if self._is_executing_cycle:
                logger.warning("Attempted concurrent cycle execution while cycle %d is running. Single-flight guard rejecting call.", self._cycle_counter)
                raise RuntimeError("Concurrent cycle execution rejected: a cycle is already actively executing")
            self._is_executing_cycle = True

        try:
            self._cycle_counter += 1
            cycle_num = self._cycle_counter
            start_time = self._now()
            start_mono = time.monotonic()
            now_iso = start_time.isoformat()

            start_to_start: Optional[float] = None
            if self._last_cycle_start_monotonic is not None:
                start_to_start = max(0.0, start_mono - self._last_cycle_start_monotonic)
            self._last_cycle_start_monotonic = start_mono

            self._health.current_cycle = cycle_num
            self._health.status = "RUNNING"
            self._health.updated_at = now_iso

            cycle_error: Optional[str] = None

            # ---------------------------------------------------------------------
            # Stage A: Catalog Discovery
            # ---------------------------------------------------------------------
            try:
                disc_result = self._discovery_engine.discover_catalog(deadline=deadline)
            except Exception as exc:
                cycle_error = f"Discovery unhandled error: {exc}"
                logger.exception("Catalog discovery raised an unexpected exception: %s", exc)
                disc_result = CatalogDiscoveryResult(
                    session_id=f"disc-err-{uuid.uuid4().hex[:6]}",
                    started_at=now_iso,
                    completed_at=self._now().isoformat(),
                    is_complete=False,
                    error_message=str(exc),
                )

            # Handle fallback to last good catalog on partial/failed discovery
            is_catalog_complete = disc_result.is_complete
            if not disc_result.is_complete:
                self._health.total_cycles_incomplete += 1
                if disc_result.error_message:
                    cycle_error = disc_result.error_message
                    self._health.last_error = disc_result.error_message
                    self._health.last_error_time = self._now().isoformat()

            if not disc_result.is_complete and self._last_good_catalog is not None:
                logger.warning(
                    "Discovery was partial/incomplete (pages: %d, markets: %d); merging with last complete catalog for screening.",
                    disc_result.pages_fetched, len(disc_result.discovered_markets)
                )
                merged_markets = dict(self._last_good_catalog.discovered_markets)
                merged_markets.update(disc_result.discovered_markets)
                merged_events = dict(self._last_good_catalog.discovered_events)
                merged_events.update(disc_result.discovered_events)
                catalog_to_screen = CatalogDiscoveryResult(
                    session_id=disc_result.session_id,
                    started_at=disc_result.started_at,
                    completed_at=disc_result.completed_at,
                    duration_seconds=disc_result.duration_seconds,
                    is_complete=False,
                    error_message=disc_result.error_message,
                    pages_fetched=disc_result.pages_fetched,
                    unique_events_count=len(merged_events),
                    unique_markets_count=len(merged_markets),
                    eligible_markets_count=len(merged_markets),
                    fresh_quotes_count=disc_result.fresh_quotes_count,
                    stale_or_missing_quotes_count=disc_result.stale_or_missing_quotes_count,
                    discovered_events=merged_events,
                    discovered_markets=merged_markets,
                    eligible_market_tickers=tuple(merged_markets.keys()),
                )
            else:
                catalog_to_screen = disc_result
                if disc_result.is_complete and len(disc_result.discovered_markets) > 0:
                    self._last_good_catalog = disc_result
                    now_str = self._now().isoformat()
                    self._health.last_successful_discovery_time = now_str
                    self._health.last_complete_discovery_time = now_str

            self._health.last_catalog_complete = is_catalog_complete
            self._health.fresh_quote_markets_count = disc_result.fresh_quotes_count
            self._health.stale_or_missing_quote_markets_count = disc_result.stale_or_missing_quotes_count

            # Update unique market coverage tracking
            discovered_markets = catalog_to_screen.discovered_markets
            new_market_tickers = set(discovered_markets.keys()) - self._all_unique_markets
            self._all_unique_markets.update(discovered_markets.keys())
            self._all_unique_events.update(catalog_to_screen.discovered_events.keys())

            # Reconcile markets: detect changes and closures
            new_markets_count = len(new_market_tickers)
            changed_markets_count = 0
            closed_markets_count = 0

            for ticker, mkt in discovered_markets.items():
                prev = self._known_markets.get(ticker)
                if prev is not None:
                    if (
                        prev.yes_ask_dollars != mkt.yes_ask_dollars
                        or prev.no_ask_dollars != mkt.no_ask_dollars
                        or prev.yes_bid_dollars != mkt.yes_bid_dollars
                        or prev.no_bid_dollars != mkt.no_bid_dollars
                        or prev.status != mkt.status
                    ):
                        changed_markets_count += 1
                    if prev.status == "open" and mkt.status != "open":
                        closed_markets_count += 1

            # Check for previously open markets that disappeared entirely from active discovery (only on complete pass)
            if is_catalog_complete:
                for ticker, prev_mkt in self._known_markets.items():
                    if prev_mkt.status == "open" and ticker not in discovered_markets:
                        closed_markets_count += 1

            # Update known markets
            self._known_markets.update(discovered_markets)

            # ---------------------------------------------------------------------
            # Stage B: Breadth Screening
            # ---------------------------------------------------------------------
            try:
                screen_summary = self._screener.screen_catalog(catalog_to_screen)
            except Exception as exc:
                logger.exception("Breadth screening raised an unexpected exception: %s", exc)
                cycle_error = f"Screening error: {exc}"
                screen_summary = ScreeningSummary(
                    session_id=f"screen-err-{uuid.uuid4().hex[:6]}",
                    screened_at=self._now().isoformat(),
                    duration_seconds=0.0,
                    total_markets_screened=len(discovered_markets),
                    total_events_screened=0,
                    preliminary_candidates_count=0,
                    new_candidates_count=0,
                    deduplicated_candidates_count=0,
                    candidates_by_strategy={},
                    outcomes_breakdown={},
                    is_catalog_complete=is_catalog_complete,
                    upstream_error=str(exc),
                    candidates=[],
                    market_results={},
                    event_results={},
                )

            self._all_unique_screened.update(screen_summary.market_results.keys())

            strategies_evaluated = sum(
                len(r.strategy_results) for r in screen_summary.market_results.values()
            ) + sum(len(r.strategy_results) for r in screen_summary.event_results.values())

            # Candidate invalidation: detect candidates whose edge dissolved or went stale
            current_candidate_keys = {c.stable_key for c in screen_summary.candidates}
            invalidated_keys = set(self._known_candidates.keys()) - current_candidate_keys
            invalidated_count = len(invalidated_keys)
            if invalidated_keys:
                logger.info("Invalidating %d candidate(s) whose edge dissolved or went stale: %s", len(invalidated_keys), list(invalidated_keys)[:5])
                for k in invalidated_keys:
                    inv_cand = self._known_candidates.pop(k, None)
                    self._qualified_results.pop(k, None)
                    if inv_cand and self.candidate_history_file:
                        try:
                            inv_record = {
                                "recorded_at": now_iso,
                                "cycle_number": cycle_num,
                                "action": "INVALIDATED",
                                "candidate": inv_cand.to_dict(),
                                "reason": "Edge dissolved or quotes moved/went stale in subsequent screen",
                            }
                            with open(self.candidate_history_file, "a", encoding="utf-8") as f:
                                f.write(json.dumps(inv_record) + "\n")
                        except Exception as exc:
                            logger.warning("Failed writing candidate invalidation record: %s", exc)

            self._health.total_candidates_invalidated += invalidated_count

            # ---------------------------------------------------------------------
            # Stage C: Targeted Qualification
            # ---------------------------------------------------------------------
            candidates_to_enqueue: List[PreliminaryCandidate] = []
            newly_persisted_candidates: List[PreliminaryCandidate] = []
            deduplicated_candidates_count = 0

            for cand in screen_summary.candidates:
                prev_cand = self._known_candidates.get(cand.stable_key)
                if prev_cand is None:
                    # Newly discovered preliminary candidate
                    candidates_to_enqueue.append(cand)
                    newly_persisted_candidates.append(cand)
                    self._known_candidates[cand.stable_key] = cand
                elif (
                    prev_cand.gross_edge_dollars != cand.gross_edge_dollars
                    or prev_cand.constituent_quotes != cand.constituent_quotes
                ):
                    # Material quote/edge change on existing candidate: requalify
                    candidates_to_enqueue.append(cand)
                    newly_persisted_candidates.append(cand)
                    self._known_candidates[cand.stable_key] = cand
                else:
                    # Unchanged candidate: deduplicated
                    deduplicated_candidates_count += 1

            # Enqueue into TargetedOrderBookQualifier
            enqueued_count = self._qualifier.enqueue_candidates(candidates_to_enqueue)

            # Process queue with constituent order-book fetching
            try:
                qual_summary = self._qualifier.process_queue(markets=discovered_markets, deadline=deadline)
            except Exception as exc:
                logger.exception("Targeted qualification raised an unexpected exception: %s", exc)
                cycle_error = f"Qualification error: {exc}"
                qual_summary = TargetedQualificationSummary(
                    session_id=f"tqual-err-{uuid.uuid4().hex[:6]}",
                    started_at=self._now().isoformat(),
                    completed_at=self._now().isoformat(),
                    duration_seconds=0.0,
                    candidates_enqueued=enqueued_count,
                    candidates_evaluated=0,
                    qualified_count=0,
                    rejected_count=0,
                    stale_count=0,
                    error_count=1,
                    unsupported_count=0,
                    requests_made=0,
                    requests_failed=1,
                    retries_count=0,
                    rate_limit_events_count=0,
                    queue_overflow_count=0,
                    outcomes_breakdown={"ERROR": 1},
                    results=[],
                )

            for res in qual_summary.results:
                self._qualified_results[res.stable_key] = res
                self._health.qualification_counts[res.outcome] = (
                    self._health.qualification_counts.get(res.outcome, 0) + 1
                )

            # Update cumulative qualified candidates count
            self._health.qualified_candidates_count = len(
                [r for r in self._qualified_results.values() if r.is_qualified]
            )

            end_time = self._now()
            end_mono = time.monotonic()
            duration = max(0.0, (end_time - start_time).total_seconds())
            duration_mono = max(0.0, end_mono - start_mono)

            configured_interval = self.config.refresh_interval_seconds
            is_overrun = duration_mono > configured_interval
            overrun_seconds = max(0.0, duration_mono - configured_interval) if is_overrun else 0.0

            if is_overrun:
                self._health.total_overruns_count += 1
                self._health.total_overrun_seconds += overrun_seconds
                self._health.last_overrun_seconds = overrun_seconds
                self._health.is_currently_overrunning = True
                logger.warning(
                    "Cycle %d overran configured interval: duration %.2fs > interval %.2fs (overrun: %.2fs)",
                    cycle_num, duration_mono, configured_interval, overrun_seconds
                )
            else:
                self._health.last_overrun_seconds = 0.0
                self._health.is_currently_overrunning = False

            self._health.last_cycle_duration_seconds = duration_mono
            self._health.last_start_to_start_interval_seconds = start_to_start

            # Calculate delay before next cycle
            if duration_mono < configured_interval:
                next_delay = configured_interval - duration_mono
            else:
                next_delay = min(self.config.min_cycle_cooldown_seconds, configured_interval)

            # ---------------------------------------------------------------------
            # Stage D: Reconciliation & Health Telemetry Updates
            # ---------------------------------------------------------------------
            self._health.total_cycles_count += 1
            if cycle_error is None and is_catalog_complete:
                self._health.successful_cycles_count += 1
            else:
                self._health.failed_cycles_count += 1

            self._health.total_unique_markets_discovered = len(self._all_unique_markets)
            self._health.total_eligible_markets = catalog_to_screen.eligible_markets_count
            self._health.total_unique_markets_screened = len(self._all_unique_screened)
            self._health.total_events_evaluated = len(self._all_unique_events)
            self._health.total_strategies_evaluated += strategies_evaluated
            self._health.total_preliminary_candidates_generated += len(newly_persisted_candidates)
            self._health.candidates_enqueued += enqueued_count
            self._health.candidates_pending = self._qualifier.queue_size
            self._health.candidates_deduplicated += deduplicated_candidates_count
            self._health.queue_overflow_count += qual_summary.queue_overflow_count

            # API Metrics aggregation
            self._health.api_requests_count += disc_result.pages_fetched + qual_summary.requests_made
            self._health.api_failures_count += qual_summary.requests_failed
            self._health.api_retries_count += qual_summary.retries_count
            self._health.rate_limit_events_count += qual_summary.rate_limit_events_count

            cycle_summary = ScanCycleSummary(
                cycle_number=cycle_num,
                session_id=self.session_id,
                started_at=now_iso,
                completed_at=end_time.isoformat(),
                duration_seconds=duration,
                is_catalog_complete=is_catalog_complete,
                markets_discovered=len(discovered_markets),
                new_markets_count=new_markets_count,
                changed_markets_count=changed_markets_count,
                closed_or_settled_markets_count=closed_markets_count,
                eligible_markets=catalog_to_screen.eligible_markets_count,
                markets_screened=screen_summary.total_markets_screened,
                events_screened=screen_summary.total_events_screened,
                strategies_evaluated=strategies_evaluated,
                preliminary_candidates_count=screen_summary.preliminary_candidates_count,
                new_preliminary_candidates_count=len(newly_persisted_candidates),
                deduplicated_preliminary_candidates_count=deduplicated_candidates_count,
                candidates_enqueued=enqueued_count,
                candidates_evaluated=qual_summary.candidates_evaluated,
                qualified_count=qual_summary.qualified_count,
                rejected_count=qual_summary.rejected_count,
                stale_count=qual_summary.stale_count,
                error_count=qual_summary.error_count,
                unsupported_count=qual_summary.unsupported_count,
                queue_overflow_count=qual_summary.queue_overflow_count,
                requests_made=disc_result.pages_fetched + qual_summary.requests_made,
                requests_failed=qual_summary.requests_failed,
                retries_count=qual_summary.retries_count,
                rate_limit_events_count=qual_summary.rate_limit_events_count,
                error_message=cycle_error,
                scheduled_interval_seconds=configured_interval,
                start_to_start_interval_seconds=start_to_start,
                is_overrun=is_overrun,
                overrun_seconds=overrun_seconds,
                next_cycle_delay_seconds=next_delay,
                fresh_quote_markets_count=disc_result.fresh_quotes_count,
                stale_or_missing_quote_markets_count=disc_result.stale_or_missing_quotes_count,
                invalidated_candidates_count=invalidated_count,
                network_time_seconds=disc_result.network_time_seconds,
                json_parse_time_seconds=disc_result.json_parse_time_seconds,
                normalization_time_seconds=disc_result.normalization_time_seconds,
                deduplication_time_seconds=disc_result.deduplication_time_seconds,
                screening_time_seconds=screen_summary.duration_seconds,
                total_discovery_time_seconds=disc_result.duration_seconds,
                total_qualification_time_seconds=qual_summary.duration_seconds,
            )

            # Persist cycle telemetry
            self._persist_state(
                cycle_summary=cycle_summary,
                new_candidates=newly_persisted_candidates,
                new_qual_results=qual_summary.results,
            )

            return cycle_summary
        finally:
            with self._execution_lock:
                self._is_executing_cycle = False

    def run(self) -> None:
        """Run continuous breadth-first scanning loop until interrupted or bounds reached."""
        self._is_running = True
        self._stop_requested = False
        start_time = self._now()
        start_mono = time.monotonic()
        run_deadline = (start_mono + self.config.duration_seconds) if self.config.duration_seconds is not None else None

        logger.info("Starting BreadthScannerRunner (session: %s, env: %s)", self.session_id, self.config.environment)

        try:
            while not self._stop_requested:
                # Check cycle bounds
                if self.config.max_cycles is not None and self._cycle_counter >= self.config.max_cycles:
                    logger.info("Reached configured max_cycles (%d). Stopping.", self.config.max_cycles)
                    self._health.stopped_reason = "MAX_CYCLES_REACHED"
                    break

                # Check duration bounds & Lookahead Gate
                if run_deadline is not None:
                    remaining_duration = run_deadline - time.monotonic()
                    if remaining_duration <= 0:
                        logger.info("Reached configured duration_seconds (%.1fs). Stopping.", self.config.duration_seconds)
                        self._health.stopped_reason = "DURATION_EXPIRED"
                        break

                    # Lookahead cycle initiation gate (Task 3 in design)
                    # If this is not the first cycle, estimate whether next cycle will fit within remaining duration
                    if self.config.enable_lookahead_gate and self._cycle_counter >= 1:
                        if self.config.duration_seconds is not None and self.config.duration_seconds >= self.config.lookahead_threshold_seconds:
                            estimated_cycle_time = max(self._health.last_cycle_duration_seconds, self.config.lookahead_threshold_seconds)
                            if remaining_duration < estimated_cycle_time:
                                logger.info(
                                    "Lookahead gate halted cycle %d: remaining duration %.2fs < estimated cycle duration %.2fs",
                                    self._cycle_counter + 1, remaining_duration, estimated_cycle_time
                                )
                                self._health.stopped_reason = "DURATION_EXPIRED"
                                break

                # Execute scan cycle with monotonic deadline propagation
                cycle_summary = self.run_cycle(deadline=run_deadline)

                # Single pass check
                if self.config.single_pass:
                    logger.info("Single pass complete. Stopping.")
                    if self._health.stopped_reason is None:
                        self._health.stopped_reason = "SINGLE_PASS_COMPLETE"
                    break

                if self._stop_requested:
                    if self._health.stopped_reason is None:
                        self._health.stopped_reason = "USER_REQUEST"
                    break

                # Responsive sleep between cycles based on computed cadence delay
                sleep_target = cycle_summary.next_cycle_delay_seconds
                sleep_slice = 0.2
                slept = 0.0
                while slept < sleep_target and not self._stop_requested:
                    if run_deadline is not None:
                        remaining = run_deadline - time.monotonic()
                        if remaining <= 0:
                            self._health.stopped_reason = "DURATION_EXPIRED"
                            break
                    time_to_sleep = min(sleep_slice, sleep_target - slept)
                    self._sleep(time_to_sleep)
                    slept += time_to_sleep
        finally:
            self._is_running = False
            self._health.status = "STOPPED"
            if self._health.stopped_reason is None:
                if self._stop_requested:
                    self._health.stopped_reason = "USER_REQUEST"
                elif self.config.single_pass:
                    self._health.stopped_reason = "SINGLE_PASS_COMPLETE"
                elif self.config.max_cycles is not None and self._cycle_counter >= self.config.max_cycles:
                    self._health.stopped_reason = "MAX_CYCLES_REACHED"
                elif run_deadline is not None and time.monotonic() >= run_deadline:
                    self._health.stopped_reason = "DURATION_EXPIRED"
                else:
                    self._health.stopped_reason = "STOPPED"
            self._health.final_elapsed_seconds = max(0.0, time.monotonic() - start_mono)
            self._health.shutdown_cleanly = True
            self._persist_state()
            logger.info("BreadthScannerRunner terminated (session: %s, cycles: %d, reason: %s)", self.session_id, self._cycle_counter, self._health.stopped_reason)


