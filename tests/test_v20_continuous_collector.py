"""Tests for Kalshi Arbitrage Engine V20 — Continuous Live Market Data Infrastructure.

Verifies:
- Config validation and lifecycle states.
- Repeated successful collection cycles with bounded concurrency.
- Configurable collection intervals and pacing.
- Graceful startup and shutdown.
- HTTP 429 rate-limit handling and exponential backoff.
- Transient transport and 5xx errors with retries and recovery.
- Malformed, incomplete, and empty market data handling.
- Stale and future timestamp detection against configurable thresholds.
- Single-flight cycle lock preventing overlapping collection cycles.
- Bounded memory growth (deque capping) preventing resource exhaustion.
- Persistence integrity: observations.jsonl and SHA-256 manifest.json.
- Collector health telemetry and dashboard API endpoint integration.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from typing import List, Optional, Tuple
import urllib.request
import pytest

from kalshi_arbitrage.continuous_collector import (
    CollectorCycleSummary,
    CollectorHealth,
    CollectorStatus,
    ContinuousCollector,
    ContinuousCollectorConfig,
)
from kalshi_arbitrage.dashboard.server import create_dashboard_server
from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.evidence_storage import observation_from_dict
from kalshi_arbitrage.market_data import (
    MarketDataInputError,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.rest_client import (
    HTTPResponse,
    KalshiClientError,
    KalshiHTTPError,
    KalshiRestClient,
    KalshiTransportError,
)


def make_mock_market(ticker: str, status: str = "open") -> NormalizedMarket:
    now = datetime.now(timezone.utc)
    return NormalizedMarket(
        ticker=ticker,
        event_ticker="KXEVENT1",
        market_type="binary",
        status=status,
        title=f"Test Market {ticker}",
        created_time=now,
        open_time=now,
        close_time=now + timedelta(days=1),
        expiration_time=now + timedelta(days=1),
    )


def make_mock_book(
    ticker: str,
    source_timestamp: Optional[datetime] = None,
    empty: bool = False,
) -> NormalizedOrderBook:
    ts = source_timestamp or datetime.now(timezone.utc)
    if empty:
        return NormalizedOrderBook(
            market_ticker=ticker,
            yes_bids=(),
            no_bids=(),
            source_timestamp=ts,
        )
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(
            OrderBookLevel(price_dollars=Decimal("0.44"), quantity=Decimal("20")),
            OrderBookLevel(price_dollars=Decimal("0.45"), quantity=Decimal("10")),
        ),
        no_bids=(
            OrderBookLevel(price_dollars=Decimal("0.53"), quantity=Decimal("15")),
        ),
        source_timestamp=ts,
    )


class MockKalshiClient(KalshiRestClient):
    """Deterministic mock client for continuous collector tests."""

    def __init__(self, markets: Optional[List[str]] = None) -> None:
        super().__init__(environment="demo", transport=lambda url, params, timeout: HTTPResponse(200, b"{}"))
        self.market_tickers = markets or ["KXTEST-A", "KXTEST-B", "KXTEST-C"]
        self.market_calls: List[str] = []
        self.book_calls: List[str] = []
        self.time_offset = timedelta(0)
        self.book_failures: dict = {}  # ticker -> Exception or list of Exceptions
        self.market_failures: dict = {}
        self.custom_timestamps: dict = {}  # ticker -> datetime
        self.empty_books: set = set()

    def list_markets(self, *, status: Optional[str] = None, max_markets: Optional[int] = None, **kwargs) -> Tuple[NormalizedMarket, ...]:
        limit = max_markets or len(self.market_tickers)
        return tuple(make_mock_market(t) for t in self.market_tickers[:limit])

    def get_market(self, ticker: str) -> NormalizedMarket:
        self.market_calls.append(ticker)
        if ticker in self.market_failures:
            fail = self.market_failures[ticker]
            if isinstance(fail, list) and fail:
                exc = fail.pop(0)
                raise exc
            elif isinstance(fail, Exception):
                raise fail
        return make_mock_market(ticker)

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        self.book_calls.append(ticker)
        if ticker in self.book_failures:
            fail = self.book_failures[ticker]
            if isinstance(fail, list) and fail:
                exc = fail.pop(0)
                raise exc
            elif isinstance(fail, Exception):
                raise fail
        ts = self.custom_timestamps.get(ticker, datetime.now(timezone.utc) + self.time_offset)
        is_empty = ticker in self.empty_books
        return make_mock_book(ticker, source_timestamp=ts, empty=is_empty)


# ============================================================================
# 1. Configuration & Validation Tests
# ============================================================================

def test_continuous_collector_config_validation():
    # Valid default config
    cfg = ContinuousCollectorConfig()
    assert cfg.poll_interval_seconds == 2.0
    assert cfg.concurrency == 4
    assert cfg.max_stale_seconds == 60.0

    # Invalid poll interval
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        ContinuousCollectorConfig(poll_interval_seconds=-1.0)

    # Invalid max_stale_seconds
    with pytest.raises(ValueError, match="max_stale_seconds"):
        ContinuousCollectorConfig(max_stale_seconds=0.0)

    # Invalid concurrency
    with pytest.raises(ValueError, match="concurrency"):
        ContinuousCollectorConfig(concurrency=0)

    # Invalid rate_limit_backoff
    with pytest.raises(ValueError, match="rate_limit_backoff_seconds"):
        ContinuousCollectorConfig(rate_limit_backoff_seconds=0.0)

    # Invalid max_rate_limit_backoff
    with pytest.raises(ValueError, match="max_rate_limit_backoff_seconds"):
        ContinuousCollectorConfig(rate_limit_backoff_seconds=10.0, max_rate_limit_backoff_seconds=5.0)


# ============================================================================
# 2. Repeated Cycles & Freshness Tests
# ============================================================================

def test_repeated_successful_collection_cycles(tmp_path):
    client = MockKalshiClient(["M1", "M2", "M3"])
    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        concurrency=2,
        output_dir=tmp_path / "evidence",
        state_file=tmp_path / "state.json",
        max_cycles=3,
        target_tickers=("M1", "M2", "M3"),
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    health = collector.get_health()
    assert health.status == CollectorStatus.STOPPED.value
    assert health.total_cycles_count == 3
    assert health.successful_cycles_count == 3
    assert health.failed_cycles_count == 0
    assert health.consecutive_failures == 0
    assert health.total_observations_count == 9
    assert health.active_monitored_markets_count == 3
    assert health.last_successful_collection_time is not None

    # Check state file persisted
    assert (tmp_path / "state.json").exists()
    state_data = json.loads((tmp_path / "state.json").read_text())
    assert state_data["total_cycles_count"] == 3
    assert state_data["successful_cycles_count"] == 3


def test_configurable_poll_interval_pacing():
    client = MockKalshiClient(["M1"])
    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.05,
        target_tickers=("M1",),
        max_cycles=2,
    )
    collector = ContinuousCollector(client=client, config=config)

    t0 = time.monotonic()
    collector.run()
    elapsed = time.monotonic() - t0

    # Two cycles with 0.05s pacing should take at least ~0.05s
    assert elapsed >= 0.04
    assert collector.get_health().total_cycles_count == 2


# ============================================================================
# 3. Graceful Startup & Shutdown Tests
# ============================================================================

def test_graceful_shutdown():
    client = MockKalshiClient(["M1", "M2"])
    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.05,
        target_tickers=("M1", "M2"),
        max_cycles=None,  # Infinite
    )
    collector = ContinuousCollector(client=client, config=config)

    # Start collector in thread
    t = threading.Thread(target=collector.run, daemon=True)
    t.start()

    # Wait until running
    for _ in range(50):
        if collector.status == CollectorStatus.RUNNING and collector.get_health().total_cycles_count >= 1:
            break
        time.sleep(0.01)

    assert collector.is_running is True

    # Issue clean stop
    collector.stop(timeout_seconds=2.0)
    t.join(timeout=2.0)

    assert not t.is_alive()
    assert collector.status == CollectorStatus.STOPPED
    health = collector.get_health()
    assert health.stopped_at is not None
    assert health.total_cycles_count >= 1


# ============================================================================
# 4. HTTP 429 Rate-Limit & Backoff Tests
# ============================================================================

def test_rate_limit_429_handling_and_backoff():
    client = MockKalshiClient(["M1"])
    # First 2 attempts raise 429, 3rd attempt succeeds
    client.book_failures["M1"] = [
        KalshiHTTPError(429, "https://api.kalshi.com/markets/M1/orderbook", body="Rate limited"),
        KalshiHTTPError(429, "https://api.kalshi.com/markets/M1/orderbook", body="Rate limited"),
    ]

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        max_retries=3,
        rate_limit_backoff_seconds=0.02,
        target_tickers=("M1",),
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    health = collector.get_health()
    assert health.successful_cycles_count == 1
    assert health.rate_limit_count == 2  # Handled both 429s

    obs = collector.get_recent_observations()
    assert len(obs) == 1
    assert obs[0].is_success is True


def test_rate_limit_429_exhaustion_marks_failure():
    client = MockKalshiClient(["M1"])
    # 429 persists beyond max_retries
    client.book_failures["M1"] = [
        KalshiHTTPError(429, "https://api.kalshi.com/markets/M1/orderbook", body="Rate limited"),
        KalshiHTTPError(429, "https://api.kalshi.com/markets/M1/orderbook", body="Rate limited"),
        KalshiHTTPError(429, "https://api.kalshi.com/markets/M1/orderbook", body="Rate limited"),
    ]

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        max_retries=2,
        rate_limit_backoff_seconds=0.01,
        target_tickers=("M1",),
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    health = collector.get_health()
    assert health.failed_cycles_count == 1
    assert health.rate_limit_count >= 2

    obs = collector.get_recent_observations()
    assert len(obs) == 1
    assert obs[0].is_success is False
    assert obs[0].is_stale is True
    assert "Rate Limit" in obs[0].staleness_reason


# ============================================================================
# 5. Transient Transport & Server Error Recovery Tests
# ============================================================================

def test_transient_transport_error_retries_and_succeeds():
    client = MockKalshiClient(["M1"])
    # 1st attempt transport error, 2nd succeeds
    client.book_failures["M1"] = [
        KalshiTransportError("Connection reset by peer"),
    ]

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        max_retries=2,
        retry_backoff_factor=0.01,
        target_tickers=("M1",),
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    health = collector.get_health()
    assert health.successful_cycles_count == 1
    obs = collector.get_recent_observations()
    assert len(obs) == 1
    assert obs[0].is_success is True


def test_recovery_after_temporary_cycle_failure():
    client = MockKalshiClient(["M1"])
    # Cycle 1: Fatal 500 error
    # Cycle 2: Success
    client.book_failures["M1"] = [
        KalshiHTTPError(500, "url", "Internal Error"),
        KalshiHTTPError(500, "url", "Internal Error"),
    ]

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        max_retries=1,
        retry_backoff_factor=0.01,
        target_tickers=("M1",),
        max_cycles=2,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    health = collector.get_health()
    assert health.total_cycles_count == 2
    assert health.failed_cycles_count == 1
    assert health.successful_cycles_count == 1
    # Consecutive failures must be reset to 0 after Cycle 2 succeeds!
    assert health.consecutive_failures == 0


# ============================================================================
# 6. Malformed & Incomplete Market Data Tests
# ============================================================================

def test_malformed_market_payload_fails_closed():
    client = MockKalshiClient(["M1"])
    client.market_failures["M1"] = MarketDataInputError("Malformed JSON response")

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        target_tickers=("M1",),
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    obs = collector.get_recent_observations()
    assert len(obs) == 1
    assert obs[0].is_success is False
    assert obs[0].is_stale is True
    assert "Malformed" in obs[0].staleness_reason


def test_empty_order_books_handled_without_fabrication():
    client = MockKalshiClient(["M1"])
    client.empty_books.add("M1")

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        target_tickers=("M1",),
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    obs = collector.get_recent_observations()
    assert len(obs) == 1
    assert obs[0].is_success is True
    assert obs[0].order_book is not None
    assert len(obs[0].order_book.yes_bids) == 0
    assert len(obs[0].order_book.no_bids) == 0


# ============================================================================
# 7. Stale & Future Timestamp Detection Tests
# ============================================================================

def test_stale_order_book_detected_against_threshold():
    client = MockKalshiClient(["M1"])
    # 100 seconds old, threshold is 60s
    client.custom_timestamps["M1"] = datetime.now(timezone.utc) - timedelta(seconds=100)

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        max_stale_seconds=60.0,
        target_tickers=("M1",),
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    obs = collector.get_recent_observations()
    assert len(obs) == 1
    assert obs[0].is_success is True
    assert obs[0].is_stale is True
    assert "stale" in obs[0].staleness_reason


def test_future_timestamp_detected_and_rejected():
    client = MockKalshiClient(["M1"])
    # 30 seconds into the future
    client.custom_timestamps["M1"] = datetime.now(timezone.utc) + timedelta(seconds=30)

    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        max_stale_seconds=60.0,
        target_tickers=("M1",),
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    obs = collector.get_recent_observations()
    assert len(obs) == 1
    assert obs[0].is_success is True
    assert obs[0].is_stale is True
    assert "future" in obs[0].staleness_reason


# ============================================================================
# 8. Single-Flight Lock & Overlap Prevention
# ============================================================================

def test_prevention_of_overlapping_cycles():
    client = MockKalshiClient(["M1"])
    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        target_tickers=("M1",),
    )
    collector = ContinuousCollector(client=client, config=config)

    # Acquire lock externally simulating in-flight cycle
    acquired = collector._cycle_lock.acquire(blocking=False)
    assert acquired is True

    try:
        # Direct call to run_cycle must not block or crash; returns skipped cycle
        obs, summary = collector.run_cycle()
        assert obs == []
        assert summary.is_success is False
        assert "Skipped overlapping cycle" in summary.error_message
    finally:
        collector._cycle_lock.release()


# ============================================================================
# 9. Bounded Memory Growth (Resource Safety)
# ============================================================================

def test_bounded_memory_growth():
    client = MockKalshiClient(["M1", "M2"])
    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.005,
        target_tickers=("M1", "M2"),
        max_memory_observations=6,  # Cap memory buffer at 6 items
        max_cycles=10,  # 10 cycles * 2 = 20 observations total
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    health = collector.get_health()
    assert health.total_cycles_count == 10
    assert health.total_observations_count == 20

    # Memory buffer MUST be strictly capped at max_memory_observations
    recent = collector.get_recent_observations()
    assert len(recent) == 6


# ============================================================================
# 10. Persistence & Manifest Verification
# ============================================================================

def test_persistence_and_manifest_integrity(tmp_path):
    out_dir = tmp_path / "continuous_evidence"
    client = MockKalshiClient(["M1", "M2"])
    config = ContinuousCollectorConfig(
        poll_interval_seconds=0.01,
        target_tickers=("M1", "M2"),
        output_dir=out_dir,
        max_cycles=2,
        manifest_interval_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    collector.run()

    obs_file = out_dir / "observations.jsonl"
    manifest_file = out_dir / "manifest.json"

    assert obs_file.exists()
    assert manifest_file.exists()

    # Verify JSONL lines can be deserialized back into MarketObservation
    lines = [line.strip() for line in obs_file.read_text().split("\n") if line.strip()]
    assert len(lines) == 4
    for line in lines:
        obs = observation_from_dict(json.loads(line))
        assert obs.ticker in ("M1", "M2")
        assert obs.is_success is True

    # Verify SHA-256 in manifest matches actual bytes
    manifest = json.loads(manifest_file.read_text())
    assert manifest["total_records"] == 4
    expected_hash = manifest["files"]["observations.jsonl"]["sha256"]

    import hashlib
    actual_hash = hashlib.sha256(obs_file.read_bytes()).hexdigest()
    assert actual_hash == expected_hash


# ============================================================================
# 11. Dashboard Health & REST API Integration
# ============================================================================

def test_dashboard_continuous_collector_status(tmp_path):
    state_file = tmp_path / "live_collector_state.json"
    state_payload = {
        "status": "RUNNING",
        "pid": 99999,
        "session_id": "v20-test-session",
        "environment": "production",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_successful_collection_time": datetime.now(timezone.utc).isoformat(),
        "latest_snapshot_timestamp": datetime.now(timezone.utc).isoformat(),
        "consecutive_failures": 0,
        "successful_cycles_count": 42,
        "failed_cycles_count": 0,
        "total_cycles_count": 42,
        "total_observations_count": 126,
        "active_monitored_markets_count": 3,
        "max_stale_seconds": 60.0,
    }
    state_file.write_text(json.dumps(state_payload))

    service = DashboardDataService(workspace_root=tmp_path)
    status = service.get_continuous_collector_status(state_filepath=state_file)

    assert status["session_id"] == "v20-test-session"
    assert status["successful_cycles_count"] == 42
    assert status["total_observations_count"] == 126


def test_dashboard_server_api_collector_endpoint(tmp_path):
    state_file = tmp_path / "data" / "live_collector_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "status": "STOPPED",
        "session_id": "v20-api-test",
        "successful_cycles_count": 10,
        "total_observations_count": 50,
        "consecutive_failures": 0,
        "active_monitored_markets_count": 5,
    }))

    # Start dashboard server on ephemeral port
    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    actual_port = server.port

    try:
        # Query /api/collector/status
        url = f"http://127.0.0.1:{actual_port}/api/collector/status"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["session_id"] == "v20-api-test"
            assert data["successful_cycles_count"] == 10

        # Query /api/status to verify continuous_collector block is included
        url_status = f"http://127.0.0.1:{actual_port}/api/status"
        req_status = urllib.request.Request(url_status, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req_status, timeout=3.0) as resp:
            assert resp.status == 200
            data_status = json.loads(resp.read().decode("utf-8"))
            assert "continuous_collector" in data_status
            assert data_status["continuous_collector"]["session_id"] == "v20-api-test"

    finally:
        server.stop()
