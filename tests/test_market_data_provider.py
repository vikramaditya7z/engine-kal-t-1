"""Tests for V9 Market Data Provider, retries, and data freshness validation."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock
import pytest

from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.market_data_provider import (
    FixtureMarketDataProvider,
    MarketDataErrorCode,
    MarketDataProviderError,
    ProviderStats,
    RestMarketDataProvider,
    validate_order_book_freshness,
)


def _make_dummy_market(ticker: str = "KXTEST-26OCT-T1", is_active: bool = True) -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker="KXTEST-EVENT",
        market_type="binary",
        title="Test Market",
        subtitle=None,
        status="active" if is_active else "closed",
        yes_subtitle="Yes",
        no_subtitle="No",
    )


def _make_dummy_book(ticker: str = "KXTEST-26OCT-T1", timestamp: Optional[datetime] = None) -> NormalizedOrderBook:
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(
            OrderBookLevel(price_dollars=Decimal("0.45"), quantity=Decimal("10")),
        ),
        no_bids=(
            OrderBookLevel(price_dollars=Decimal("0.50"), quantity=Decimal("10")),
        ),
        source_timestamp=timestamp or datetime.now(timezone.utc),
    )


def test_order_book_freshness_validation_success():
    now = datetime.now(timezone.utc)
    book = _make_dummy_book(timestamp=now - timedelta(seconds=15))
    res = validate_order_book_freshness(book, reference_time=now, max_stale_seconds=60.0)
    assert res.is_valid is True
    assert res.error_code is None


def test_order_book_freshness_rejects_missing_timestamp():
    book = NormalizedOrderBook(
        market_ticker="KXTEST",
        yes_bids=(),
        no_bids=(),
        source_timestamp=None,
    )
    res = validate_order_book_freshness(book, reference_time=datetime.now(timezone.utc))
    assert res.is_valid is False
    assert res.error_code == MarketDataErrorCode.STALE_DATA
    assert "no source_timestamp" in str(res.reason)


def test_order_book_freshness_rejects_future_drift():
    now = datetime.now(timezone.utc)
    book = _make_dummy_book(timestamp=now + timedelta(seconds=25))
    res = validate_order_book_freshness(book, reference_time=now, max_future_seconds=10.0)
    assert res.is_valid is False
    assert res.error_code == MarketDataErrorCode.FUTURE_TIMESTAMP
    assert "in the future" in str(res.reason)


def test_order_book_freshness_rejects_stale_quote():
    now = datetime.now(timezone.utc)
    book = _make_dummy_book(timestamp=now - timedelta(seconds=120))
    res = validate_order_book_freshness(book, reference_time=now, max_stale_seconds=60.0)
    assert res.is_valid is False
    assert res.error_code == MarketDataErrorCode.STALE_DATA
    assert "stale" in str(res.reason)


def test_fixture_market_data_provider():
    now = datetime.now(timezone.utc)
    m1 = _make_dummy_market("TICK-1")
    m2 = _make_dummy_market("TICK-2")
    b1 = _make_dummy_book("TICK-1", timestamp=now)

    provider = FixtureMarketDataProvider(markets=[m1, m2], books={"TICK-1": b1}, current_time=now)

    markets = provider.discover_markets()
    assert len(markets) == 2
    assert provider.get_market("TICK-1").ticker == "TICK-1"

    book, ts = provider.get_order_book("TICK-1")
    assert book.market_ticker == "TICK-1"
    assert ts == now

    with pytest.raises(MarketDataProviderError) as exc:
        provider.get_order_book("NON-EXISTENT")
    assert exc.value.code == MarketDataErrorCode.EMPTY_ORDER_BOOK


def test_rest_provider_retries_transient_failures_and_succeeds():
    mock_client = MagicMock()
    now = datetime.now(timezone.utc)
    book = _make_dummy_book(timestamp=now)

    # Fail twice with timeout, succeed on 3rd attempt
    mock_client.get_order_book.side_effect = [
        Exception("Connection timed out"),
        Exception("HTTP 429 Rate limit exceeded"),
        book,
    ]

    provider = RestMarketDataProvider(
        client=mock_client, max_retries=3, backoff_factor=0.01
    )
    result_book, ts = provider.get_order_book("KXTEST-26OCT-T1")
    assert result_book.market_ticker == "KXTEST-26OCT-T1"

    stats = provider.get_stats()
    assert stats.total_requests == 1
    assert stats.successful_requests == 1
    assert stats.retry_attempts == 2
    assert stats.consecutive_failures == 0


def test_rest_provider_fails_closed_on_permanent_error():
    mock_client = MagicMock()
    mock_client.get_order_book.side_effect = Exception("HTTP 401 Unauthorized")

    provider = RestMarketDataProvider(
        client=mock_client, max_retries=3, backoff_factor=0.01
    )
    with pytest.raises(MarketDataProviderError) as exc_info:
        provider.get_order_book("KXTEST-26OCT-T1")

    assert exc_info.value.code == MarketDataErrorCode.AUTH_ERROR
    stats = provider.get_stats()
    assert stats.total_requests == 1
    assert stats.failed_requests == 1
    assert stats.consecutive_failures == 1
