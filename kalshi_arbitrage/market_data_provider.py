"""Market Data Provider abstraction, validation, retries, and failure classification.

V9 Market Data Provider:
- Clean abstraction over read-only Kalshi REST market data or deterministic offline fixtures.
- Structural validation of markets and order books.
- Point-in-time data freshness and timestamp verification.
- Explicit failure classification (timeout, rate limit, server error, stale, malformed).
- Bounded retries with exponential backoff for transient transport failures.
- Zero live trading order capability.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .market_data import (
    MarketDataInputError,
    NormalizedMarket,
    NormalizedOrderBook,
    normalize_market,
    normalize_order_book,
)
from .rest_client import KalshiClientError, KalshiRestClient

logger = logging.getLogger(__name__)


class MarketDataErrorCode(str, Enum):
    """Explicit taxonomy of market-data ingestion and validation failures."""

    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER_ERROR = "SERVER_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    MALFORMED_DATA = "MALFORMED_DATA"
    STALE_DATA = "STALE_DATA"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    MARKET_INACTIVE = "MARKET_INACTIVE"
    EMPTY_ORDER_BOOK = "EMPTY_ORDER_BOOK"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN = "UNKNOWN"


class MarketDataProviderError(Exception):
    """Base exception for market-data retrieval or validation failure."""

    def __init__(self, message: str, code: MarketDataErrorCode = MarketDataErrorCode.UNKNOWN):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MarketDataValidationResult:
    """Detailed verdict of market and order-book data validation."""

    is_valid: bool
    error_code: Optional[MarketDataErrorCode] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class ProviderStats:
    """Operational metrics for market-data queries."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    retry_attempts: int = 0
    consecutive_failures: int = 0
    failures_by_code: Mapping[str, int] = field(default_factory=dict)


def validate_order_book_freshness(
    order_book: NormalizedOrderBook,
    reference_time: datetime,
    max_stale_seconds: float = 60.0,
    max_future_seconds: float = 10.0,
) -> MarketDataValidationResult:
    """Enforces strict freshness and timestamp sanity on order-book data.

    Fails closed if the book has no timestamp, has drifted into the future,
    or is older than max_stale_seconds.
    """
    if order_book.source_timestamp is None:
        return MarketDataValidationResult(
            is_valid=False,
            error_code=MarketDataErrorCode.STALE_DATA,
            reason=f"Order book for {order_book.market_ticker} has no source_timestamp",
        )

    book_time = order_book.source_timestamp
    if book_time.tzinfo is None:
        book_time = book_time.replace(tzinfo=timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    age_seconds = (reference_time - book_time).total_seconds()

    if age_seconds < -max_future_seconds:
        return MarketDataValidationResult(
            is_valid=False,
            error_code=MarketDataErrorCode.FUTURE_TIMESTAMP,
            reason=(
                f"Order book timestamp for {order_book.market_ticker} is in the future by "
                f"{abs(age_seconds):.1f}s (> {max_future_seconds}s tolerance)"
            ),
        )

    if age_seconds > max_stale_seconds:
        return MarketDataValidationResult(
            is_valid=False,
            error_code=MarketDataErrorCode.STALE_DATA,
            reason=(
                f"Order book timestamp for {order_book.market_ticker} is stale "
                f"({age_seconds:.1f}s old > {max_stale_seconds}s threshold)"
            ),
        )

    return MarketDataValidationResult(is_valid=True)


class MarketDataProvider(ABC):
    """Abstract interface for retrieving and validating market data."""

    @abstractmethod
    def discover_markets(self, limit: Optional[int] = None) -> Sequence[NormalizedMarket]:
        """Discover active binary markets."""
        pass

    @abstractmethod
    def get_market(self, ticker: str) -> NormalizedMarket:
        """Fetch normalized market metadata."""
        pass

    @abstractmethod
    def get_order_book(self, ticker: str) -> Tuple[NormalizedOrderBook, datetime]:
        """Fetch order book and retrieval timestamp."""
        pass

    @abstractmethod
    def get_stats(self) -> ProviderStats:
        """Get operational request and failure statistics."""
        pass

    def validate_market_and_book(
        self,
        market: NormalizedMarket,
        order_book: NormalizedOrderBook,
        reference_time: Optional[datetime] = None,
        max_stale_seconds: float = 60.0,
    ) -> MarketDataValidationResult:
        """Validate structural integrity, status, and freshness."""
        if market.status.lower() not in ("active", "open"):
            return MarketDataValidationResult(
                is_valid=False,
                error_code=MarketDataErrorCode.MARKET_INACTIVE,
                reason=f"Market {market.ticker} is inactive (status={market.status!r})",
            )

        ref_time = reference_time or datetime.now(timezone.utc)
        return validate_order_book_freshness(
            order_book,
            reference_time=ref_time,
            max_stale_seconds=max_stale_seconds,
        )


class RestMarketDataProvider(MarketDataProvider):
    """Production read-only REST provider with bounded retries and failure classification."""

    def __init__(
        self,
        client: Optional[KalshiRestClient] = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        max_stale_seconds: float = 60.0,
    ) -> None:
        self.client = client or KalshiRestClient()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_stale_seconds = max_stale_seconds

        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
        self._retry_attempts = 0
        self._consecutive_failures = 0
        self._failures_by_code: Dict[str, int] = {}

    def _record_success(self) -> None:
        self._total_requests += 1
        self._successful_requests += 1
        self._consecutive_failures = 0

    def _record_failure(self, code: MarketDataErrorCode) -> None:
        self._total_requests += 1
        self._failed_requests += 1
        self._consecutive_failures += 1
        code_str = code.value
        self._failures_by_code[code_str] = self._failures_by_code.get(code_str, 0) + 1

    def _classify_client_error(self, exc: Exception) -> MarketDataErrorCode:
        err_msg = str(exc).lower()
        if "timeout" in err_msg or "timed out" in err_msg:
            return MarketDataErrorCode.TIMEOUT
        if "429" in err_msg or "rate limit" in err_msg:
            return MarketDataErrorCode.RATE_LIMIT
        if "500" in err_msg or "502" in err_msg or "503" in err_msg or "504" in err_msg or "server error" in err_msg:
            return MarketDataErrorCode.SERVER_ERROR
        if "401" in err_msg or "403" in err_msg or "unauthorized" in err_msg:
            return MarketDataErrorCode.AUTH_ERROR
        if "malformed" in err_msg or "invalid" in err_msg:
            return MarketDataErrorCode.MALFORMED_DATA
        if "connection" in err_msg or "refused" in err_msg or "network" in err_msg:
            return MarketDataErrorCode.NETWORK_ERROR
        return MarketDataErrorCode.UNKNOWN

    def _execute_with_retry(self, action_name: str, func, *args, **kwargs):
        attempts = 0
        while True:
            attempts += 1
            try:
                result = func(*args, **kwargs)
                self._record_success()
                return result
            except Exception as exc:
                code = self._classify_client_error(exc)
                is_transient = code in (
                    MarketDataErrorCode.TIMEOUT,
                    MarketDataErrorCode.RATE_LIMIT,
                    MarketDataErrorCode.SERVER_ERROR,
                    MarketDataErrorCode.NETWORK_ERROR,
                )

                if is_transient and attempts <= self.max_retries:
                    self._retry_attempts += 1
                    sleep_time = self.backoff_factor * (2 ** (attempts - 1))
                    logger.warning(
                        f"Transient error in {action_name} ({code.value}): {exc}. "
                        f"Retrying in {sleep_time:.2f}s (attempt {attempts}/{self.max_retries})"
                    )
                    time.sleep(sleep_time)
                else:
                    self._record_failure(code)
                    logger.error(f"Failed {action_name} after {attempts} attempts ({code.value}): {exc}")
                    raise MarketDataProviderError(
                        f"Failed {action_name}: {exc}", code=code
                    ) from exc

    def discover_markets(self, limit: Optional[int] = None) -> Sequence[NormalizedMarket]:
        """Fetch list of open binary markets using REST client."""
        def _fetch():
            max_limit = limit or 200
            return self.client.list_markets(
                limit=min(100, max_limit),
                status="open",
                max_markets=max_limit,
            )

        return self._execute_with_retry("discover_markets", _fetch)

    def get_market(self, ticker: str) -> NormalizedMarket:
        """Fetch single market metadata."""
        return self._execute_with_retry(
            f"get_market({ticker})", self.client.get_market, ticker
        )

    def get_order_book(self, ticker: str) -> Tuple[NormalizedOrderBook, datetime]:
        """Fetch order book and record arrival timestamp."""
        book = self._execute_with_retry(
            f"get_order_book({ticker})", self.client.get_order_book, ticker
        )
        observed_at = book.source_timestamp or datetime.now(timezone.utc)
        return book, observed_at

    def get_stats(self) -> ProviderStats:
        return ProviderStats(
            total_requests=self._total_requests,
            successful_requests=self._successful_requests,
            failed_requests=self._failed_requests,
            retry_attempts=self._retry_attempts,
            consecutive_failures=self._consecutive_failures,
            failures_by_code=dict(self._failures_by_code),
        )


class FixtureMarketDataProvider(MarketDataProvider):
    """Deterministic offline provider backed by static fixtures for hermetic testing."""

    def __init__(
        self,
        markets: Optional[Sequence[NormalizedMarket]] = None,
        books: Optional[Mapping[str, NormalizedOrderBook]] = None,
        current_time: Optional[datetime] = None,
    ) -> None:
        self._markets_by_ticker: Dict[str, NormalizedMarket] = {}
        if markets:
            for m in markets:
                self._markets_by_ticker[m.ticker] = m
        self._books_by_ticker: Dict[str, NormalizedOrderBook] = dict(books or {})
        self.current_time = current_time or datetime.now(timezone.utc)
        self._stats = ProviderStats()

    def set_market(self, market: NormalizedMarket) -> None:
        self._markets_by_ticker[market.ticker] = market

    def set_order_book(self, ticker: str, book: NormalizedOrderBook) -> None:
        self._books_by_ticker[ticker] = book

    def set_current_time(self, dt: datetime) -> None:
        self.current_time = dt

    def discover_markets(self, limit: Optional[int] = None) -> Sequence[NormalizedMarket]:
        markets = list(self._markets_by_ticker.values())
        if limit is not None:
            markets = markets[:limit]
        return tuple(markets)

    def get_market(self, ticker: str) -> NormalizedMarket:
        if ticker not in self._markets_by_ticker:
            raise MarketDataProviderError(
                f"Market {ticker} not found in fixtures",
                code=MarketDataErrorCode.INVALID_RESPONSE,
            )
        return self._markets_by_ticker[ticker]

    def get_order_book(self, ticker: str) -> Tuple[NormalizedOrderBook, datetime]:
        if ticker not in self._books_by_ticker:
            raise MarketDataProviderError(
                f"Order book for {ticker} not found in fixtures",
                code=MarketDataErrorCode.EMPTY_ORDER_BOOK,
            )
        book = self._books_by_ticker[ticker]
        ts = book.source_timestamp or self.current_time
        return book, ts

    def get_stats(self) -> ProviderStats:
        return self._stats
