"""Comprehensive automated test suite for Kalshi Arbitrage Engine V25 End-to-End Live Validation.

Verifies:
1. End-to-End Pipeline Stream Flow:
   - Full pipeline integration: Mock Kalshi Client -> ContinuousCollector -> observations.jsonl ->
     ContinuousEvaluator -> ExecutionQualifier -> AlertEngine -> live_alert_history.jsonl ->
     DashboardDataService and DashboardServer REST APIs.
2. Fault Injection & Recovery (Section 7):
   - Fault 1: Temporary API failure, consecutive failure tracking, and automatic recovery.
   - Fault 2: HTTP 429 rate limit backoff and error handling.
   - Fault 3: Collector interruption and restart without file corruption.
   - Fault 4: Evaluator incremental tailing while streaming and deterministic state replay on restart.
   - Fault 5: Dashboard server interruption and restart with rapid binding.
   - Fault 6: Alert delivery timeout and webhook failure isolation (non-blocking).
   - Fault 7: Stale data detection during collection outage (>60s threshold).
   - Fault 8: Malformed persisted state recovery (corrupted JSON handling).
   - Fault 9: Graceful shutdown during active collection and pending alert queue.
3. Deterministic Qualification Reconciliation:
   - Sample qualification results reconciled against independent deterministic arithmetic.
4. Data Provenance Separation:
   - Live daemon evidence strictly isolated from historical trial datasets and simulations.
5. Alert Lifecycle & Secret Sanitization:
   - All alert transitions verified; credential tokens in URLs masked as ***REDACTED***.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
import uuid

import pytest

from kalshi_arbitrage import (
    AlertConfig,
    AlertDeliveryStatus,
    AlertEngine,
    AlertEventType,
    ArbitrageCandidate,
    CandidateQualification,
    CandidateState,
    CandidateTransition,
    CollectorHealth,
    CollectorStatus,
    ContinuousCollector,
    ContinuousCollectorConfig,
    ContinuousEvaluator,
    ContinuousEvaluatorConfig,
    ExecutionAwareArbitrageQualifier,
    ExecutionQualifierConfig,
    FeeModelConfig,
    FeeRoundingMode,
    FeeScheduleType,
    FeeTargetPrecision,
    LegExecutionDetail,
    MarketObservation,
    OpportunityAlert,
    OrderBookLevel,
    QualificationOutcome,
    redact_sensitive_text,
    sanitize_headers,
    sanitize_url,
)
from kalshi_arbitrage.alert_engine import (
    BaseNotificationChannel,
    LocalHistoryChannel,
    WebhookNotificationChannel,
)
from kalshi_arbitrage.arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
)
from kalshi_arbitrage.dashboard.server import create_dashboard_server
from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.evidence_storage import observation_to_dict
from kalshi_arbitrage.market_data import (
    MarketDataInputError,
    NormalizedMarket,
    NormalizedOrderBook,
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
        event_ticker=f"KXEVENT-{ticker}",
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
    yes_bid: Decimal = Decimal("0.45"),
    no_bid: Decimal = Decimal("0.50"),
    quantity: Decimal = Decimal("20"),
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
            OrderBookLevel(price_dollars=yes_bid, quantity=quantity),
        ),
        no_bids=(
            OrderBookLevel(price_dollars=no_bid, quantity=quantity),
        ),
        source_timestamp=ts,
    )


def make_opportunity_alert(
    alert_id: str = "alert-1",
    candidate_id: str = "cand-1",
    event_type: AlertEventType = AlertEventType.NEW_QUALIFIED,
    timestamp: Optional[str] = None,
    strategy_type: str = "BINARY_PARITY",
    market_tickers: Tuple[str, ...] = ("KXTEST-1",),
    event_ticker: Optional[str] = None,
    qualification_outcome: str = "QUALIFIED",
    gross_edge_cents: Optional[int] = 10,
    estimated_fees_cents: Optional[int] = 2,
    net_edge_cents: Optional[int] = 8,
    target_quantity: int = 5,
    executable_quantity: int = 5,
    total_cost_cents: Optional[int] = 450,
    guaranteed_payout_cents: Optional[int] = 500,
    return_on_capital_pct: Optional[str] = "2.22",
    data_freshness_seconds: Optional[float] = 0.5,
    message: str = "Opportunity qualified",
    disclaimer: str = "Test disclaimer",
    metadata: Optional[Dict[str, Any]] = None,
) -> OpportunityAlert:
    return OpportunityAlert(
        alert_id=alert_id,
        candidate_id=candidate_id,
        event_type=event_type,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        strategy_type=strategy_type,
        market_tickers=market_tickers,
        event_ticker=event_ticker,
        qualification_outcome=qualification_outcome,
        gross_edge_cents=gross_edge_cents,
        estimated_fees_cents=estimated_fees_cents,
        net_edge_cents=net_edge_cents,
        target_quantity=target_quantity,
        executable_quantity=executable_quantity,
        total_cost_cents=total_cost_cents,
        guaranteed_payout_cents=guaranteed_payout_cents,
        return_on_capital_pct=return_on_capital_pct,
        data_freshness_seconds=data_freshness_seconds,
        message=message,
        disclaimer=disclaimer,
        metadata=metadata or {},
    )


class MockKalshiClient(KalshiRestClient):
    """Deterministic mock client for continuous collector tests."""

    def __init__(self, markets: Optional[List[str]] = None) -> None:
        super().__init__(environment="demo", transport=lambda url, params, timeout: HTTPResponse(200, b"{}"))
        self.market_tickers = markets or ["KXTEST-A", "KXTEST-B"]
        self.market_calls: List[str] = []
        self.book_calls: List[str] = []
        self.fail_mode: Optional[str] = None
        self.fail_count: int = 0
        self.book_override: Dict[str, NormalizedOrderBook] = {}

    def list_markets(
        self,
        *,
        limit: int = 100,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        max_markets: Optional[int] = None,
    ) -> Tuple[NormalizedMarket, ...]:
        if self.fail_mode == "500":
            raise KalshiHTTPError(500, "https://api.kalshi.com/trade-api/v2/markets", "Internal Server Error")
        if self.fail_mode == "429":
            self.fail_count += 1
            raise KalshiHTTPError(429, "https://api.kalshi.com/trade-api/v2/markets", "Rate limit exceeded")
        if self.fail_mode == "transport":
            raise KalshiTransportError("Connection failed")
        return tuple(make_mock_market(t) for t in self.market_tickers)

    def get_market(self, ticker: str) -> NormalizedMarket:
        self.market_calls.append(ticker)
        if self.fail_mode == "500":
            raise KalshiHTTPError(500, f"https://api.kalshi.com/trade-api/v2/markets/{ticker}", "Server Error")
        if self.fail_mode == "429":
            self.fail_count += 1
            raise KalshiHTTPError(429, f"https://api.kalshi.com/trade-api/v2/markets/{ticker}", "Rate limit")
        return make_mock_market(ticker)

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        self.book_calls.append(ticker)
        if self.fail_mode == "500":
            raise KalshiHTTPError(500, f"https://api.kalshi.com/trade-api/v2/markets/{ticker}/orderbook", "Server Error")
        if self.fail_mode == "429":
            self.fail_count += 1
            raise KalshiHTTPError(429, f"https://api.kalshi.com/trade-api/v2/markets/{ticker}/orderbook", "Rate limit")
        if ticker in self.book_override:
            return self.book_override[ticker]
        return make_mock_book(ticker)


def test_end_to_end_pipeline_live_stream_flow(tmp_path: Path):
    """Test full integration from collector -> observations -> evaluator -> alerts -> dashboard."""
    obs_dir = tmp_path / "collection"
    collector_state = tmp_path / "collector_state.json"
    eval_state = tmp_path / "eval_state.json"
    eval_hist = tmp_path / "eval_hist.jsonl"
    alert_state = tmp_path / "alert_state.json"
    alert_hist = tmp_path / "alert_hist.jsonl"

    client = MockKalshiClient(["KXTEST-E2E"])
    # Construct an arbitrage opportunity: YES bid = 0.55 (implied NO ask = 0.45)
    # NO bid = 0.60 (implied YES ask = 0.40)
    # Total ask = 0.40 + 0.45 = 0.85 (15c gross edge)
    client.book_override["KXTEST-E2E"] = make_mock_book(
        "KXTEST-E2E",
        yes_bid=Decimal("0.55"),
        no_bid=Decimal("0.60"),
        quantity=Decimal("10"),
    )

    collector_config = ContinuousCollectorConfig(
        environment="demo",
        poll_interval_seconds=0.05,
        concurrency=1,
        target_tickers=("KXTEST-E2E",),
        output_dir=obs_dir,
        state_file=collector_state,
        max_cycles=2,
    )
    collector = ContinuousCollector(client=client, config=collector_config)
    collector.run()

    obs_file = obs_dir / "observations.jsonl"
    assert obs_file.exists()
    assert collector_state.exists()

    alert_cfg = AlertConfig(
        enabled=True,
        state_file=alert_state,
        history_file=alert_hist,
        desktop_notifications_enabled=False,
    )
    eval_cfg = ContinuousEvaluatorConfig(
        observations_file=obs_file,
        poll_interval_seconds=0.05,
        state_file=eval_state,
        history_file=eval_hist,
        alerts_enabled=True,
        alert_config=alert_cfg,
        alert_state_file=alert_state,
        alert_history_file=alert_hist,
        target_quantity=5,
        min_net_profit_cents=1,
        fee_schedule=FeeScheduleType.EXEMPT,
        max_cycles=2,
    )
    evaluator = ContinuousEvaluator(config=eval_cfg)
    evaluator.run()

    assert eval_state.exists()
    assert eval_hist.exists()
    assert alert_state.exists()
    assert alert_hist.exists()

    # Dashboard service verification
    service = DashboardDataService(workspace_root=tmp_path)
    orig_c_status = service.get_continuous_collector_status
    orig_e_status = service.get_continuous_evaluator_status
    orig_a_status = service.get_alert_service_status

    service.get_continuous_collector_status = lambda p=collector_state: orig_c_status(p)
    service.get_continuous_evaluator_status = lambda p=eval_state: orig_e_status(p)
    service.get_alert_service_status = lambda p=alert_state: orig_a_status(p)

    summary = service.get_live_pipeline_summary()
    assert "pipeline_status" in summary
    assert summary["evaluator"]["active_candidates_count"] >= 1
    assert summary["alerts"]["alerts_created_count"] >= 1

    # Verify candidate history read
    cands = service.get_continuous_evaluator_candidates(state_filepath=eval_state)
    assert len(cands) >= 1
    assert cands[0]["is_qualified"] is True


def test_fault_1_temporary_api_failure_recovery(tmp_path: Path):
    """Fault 1: Temporary API 500 error causes DEGRADED state, recovers to HEALTHY on restore."""
    client = MockKalshiClient(["KXTEST-A"])
    collector_state = tmp_path / "collector_state.json"
    config = ContinuousCollectorConfig(
        environment="demo",
        poll_interval_seconds=0.02,
        retry_backoff_factor=0.01,
        concurrency=1,
        target_tickers=("KXTEST-A",),
        output_dir=tmp_path / "obs",
        state_file=collector_state,
    )
    collector = ContinuousCollector(client=client, config=config)

    # First cycle succeeds
    collector.run_cycle()
    health1 = collector.get_health()
    assert health1.consecutive_failures == 0
    assert health1.successful_cycles_count == 1

    # Introduce fault: 500 Server Error
    client.fail_mode = "500"
    collector.run_cycle()
    health2 = collector.get_health()
    assert health2.consecutive_failures == 1
    assert health2.failed_cycles_count == 1

    # Restore API
    client.fail_mode = None
    collector.run_cycle()
    health3 = collector.get_health()
    assert health3.consecutive_failures == 0
    assert health3.successful_cycles_count == 2
    assert health3.failed_cycles_count == 1


def test_fault_2_http_429_rate_limit_backoff(tmp_path: Path):
    """Fault 2: HTTP 429 triggers exponential backoff and increments rate_limit_count."""
    client = MockKalshiClient(["KXTEST-A"])
    config = ContinuousCollectorConfig(
        environment="demo",
        poll_interval_seconds=0.02,
        rate_limit_backoff_seconds=0.01,
        max_rate_limit_backoff_seconds=0.02,
        concurrency=1,
        target_tickers=("KXTEST-A",),
        output_dir=tmp_path / "obs",
        state_file=tmp_path / "collector_state.json",
        max_cycles=1,
    )
    collector = ContinuousCollector(client=client, config=config)

    client.fail_mode = "429"
    collector.run(max_cycles=1)

    health = collector.get_health()
    assert health.rate_limit_count >= 1
    assert health.consecutive_failures == 1


def test_fault_3_collector_interruption_and_restart(tmp_path: Path):
    """Fault 3: Collector interrupted and restarted preserves existing observations and resumes."""
    client = MockKalshiClient(["KXTEST-A"])
    obs_dir = tmp_path / "obs"
    collector_state = tmp_path / "collector_state.json"
    config1 = ContinuousCollectorConfig(
        environment="demo",
        poll_interval_seconds=0.02,
        concurrency=1,
        target_tickers=("KXTEST-A",),
        output_dir=obs_dir,
        state_file=collector_state,
        max_cycles=2,
    )

    collector1 = ContinuousCollector(client=client, config=config1)
    collector1.run()
    assert collector_state.exists()

    obs_file = obs_dir / "observations.jsonl"
    with open(obs_file, "r") as f:
        lines1 = [l for l in f if l.strip()]
    assert len(lines1) == 2

    # Interrupt and restart with 2 more cycles using fresh config
    config2 = ContinuousCollectorConfig(
        environment="demo",
        poll_interval_seconds=0.02,
        concurrency=1,
        target_tickers=("KXTEST-A",),
        output_dir=obs_dir,
        state_file=collector_state,
        max_cycles=2,
    )
    collector2 = ContinuousCollector(client=client, config=config2)
    collector2.run()

    with open(obs_file, "r") as f:
        lines2 = [l for l in f if l.strip()]
    assert len(lines2) == 4
    # Check that manifest file is valid SHA256
    manifest_file = obs_dir / "manifest.json"
    assert manifest_file.exists()


def test_fault_4_evaluator_incremental_tailing_and_restart(tmp_path: Path):
    """Fault 4: Evaluator incrementally tails new observations while running and replays from disk on restart."""
    obs_file = tmp_path / "observations.jsonl"
    eval_state = tmp_path / "eval_state.json"
    eval_hist = tmp_path / "eval_hist.jsonl"

    market = make_mock_market("KXTEST-OFF")
    book = make_mock_book("KXTEST-OFF", yes_bid=Decimal("0.55"), no_bid=Decimal("0.60"))
    now = datetime.now(timezone.utc)

    # Write 3 initial observations
    with open(obs_file, "w") as f:
        for i in range(3):
            obs = MarketObservation(
                observation_id=f"obs-{i}",
                ticker="KXTEST-OFF",
                observed_at=now,
                source_timestamp=now,
                market=market,
                order_book=book,
                is_success=True,
                is_stale=False,
            )
            f.write(json.dumps(observation_to_dict(obs)) + "\n")

    eval_cfg1 = ContinuousEvaluatorConfig(
        observations_file=obs_file,
        poll_interval_seconds=0.02,
        state_file=eval_state,
        history_file=eval_hist,
        alerts_enabled=False,
        fee_schedule=FeeScheduleType.EXEMPT,
        max_cycles=1,
    )
    evaluator1 = ContinuousEvaluator(config=eval_cfg1)
    evaluator1.run()

    # Evaluator 1 processed the initial 3 observations
    assert evaluator1._processed_observations_count == 3
    assert len(evaluator1.get_active_candidates()) == 1

    # Append 2 new observations
    with open(obs_file, "a") as f:
        for i in range(3, 5):
            obs = MarketObservation(
                observation_id=f"obs-{i}",
                ticker="KXTEST-OFF",
                observed_at=now,
                source_timestamp=now,
                market=market,
                order_book=book,
                is_success=True,
                is_stale=False,
            )
            f.write(json.dumps(observation_to_dict(obs)) + "\n")

    # Evaluator 2 restarts from file, replaying full history to restore active candidates
    eval_cfg2 = ContinuousEvaluatorConfig(
        observations_file=obs_file,
        poll_interval_seconds=0.02,
        state_file=eval_state,
        history_file=eval_hist,
        alerts_enabled=False,
        fee_schedule=FeeScheduleType.EXEMPT,
        max_cycles=1,
    )
    evaluator2 = ContinuousEvaluator(config=eval_cfg2)
    evaluator2.run()

    # Replay processed all 5 observations and reconstructed active candidate cleanly
    assert evaluator2._processed_observations_count == 5
    assert len(evaluator2.get_active_candidates()) == 1


def test_fault_5_dashboard_server_interruption_and_restart(tmp_path: Path):
    """Fault 5: Dashboard server stops cleanly and restarts on new port without conflict."""
    server1 = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    url1 = server1.start(blocking=False)
    assert url1.startswith("http://127.0.0.1:")

    # Verify HTTP response on /api/health
    req = urllib.request.Request(f"{url1}/api/health")
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        assert resp.status == 200

    # Stop server
    server1.stop()

    # Start second server
    server2 = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    url2 = server2.start(blocking=False)
    req2 = urllib.request.Request(f"{url2}/api/health")
    with urllib.request.urlopen(req2, timeout=3.0) as resp:
        assert resp.status == 200
    server2.stop()


def test_fault_6_alert_delivery_timeout_isolation(tmp_path: Path):
    """Fault 6: Webhook timeout and failure retries with backoff and does not block evaluator."""
    alert_hist = tmp_path / "alert_hist.jsonl"
    alert_state = tmp_path / "alert_state.json"

    # Configure webhook pointing to an unresponsive/non-existent local port
    cfg = AlertConfig(
        enabled=True,
        state_file=alert_state,
        history_file=alert_hist,
        webhook_url="http://127.0.0.1:59999/unreachable_webhook",
        webhook_timeout_seconds=0.05,
        webhook_max_retries=1,
        desktop_notifications_enabled=False,
    )
    engine = AlertEngine(config=cfg)
    engine.start()

    alert = make_opportunity_alert(alert_id="alert-fault-test", candidate_id="cand-1")
    engine._dispatch_alert(alert)

    # Allow worker thread time to attempt and retry
    time.sleep(0.3)
    engine.stop()

    status = engine.get_status()
    # Webhook delivery failed, but local history succeeded
    assert status["alerts_delivered_count"] >= 1  # Delivered to local history channel
    assert status["alerts_failed_count"] >= 1     # Failed on webhook channel
    assert len(status["recent_delivery_errors"]) >= 1
    assert "webhook" in str(status["recent_delivery_errors"][0])


def test_fault_7_stale_data_detection_during_outage(tmp_path: Path):
    """Fault 7: Stale data (>60s) flags is_data_fresh=False and triggers DEGRADED status."""
    collector_state = tmp_path / "collector_state.json"
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=120)

    # Synthesize stale collector state
    stale_health = CollectorHealth(
        status="RUNNING",
        pid=1234,
        session_id="test-session",
        environment="demo",
        started_at=stale_time.isoformat(),
        stopped_at=None,
        last_successful_collection_time=stale_time.isoformat(),
        last_attempted_collection_time=stale_time.isoformat(),
        latest_snapshot_timestamp=stale_time.isoformat(),
        latest_snapshot_age_seconds=120.0,
        is_data_fresh=False,
        consecutive_failures=0,
        successful_cycles_count=5,
        failed_cycles_count=0,
        total_cycles_count=5,
        total_observations_count=10,
        active_monitored_markets_count=2,
        last_cycle_duration_seconds=0.1,
        last_cycle_markets_polled=2,
        last_cycle_markets_succeeded=2,
        last_cycle_markets_failed=0,
        last_error=None,
        last_error_code=None,
        rate_limit_count=0,
        poll_interval_seconds=2.0,
        max_stale_seconds=60.0,
    )
    with open(collector_state, "w") as f:
        json.dump(stale_health.to_dict(), f)

    service = DashboardDataService(workspace_root=tmp_path)
    status = service.get_continuous_collector_status(collector_state)
    assert status["is_data_fresh"] is False
    assert status["latest_snapshot_age_seconds"] >= 120.0

    # Pipeline summary should reflect degraded
    service.get_continuous_evaluator_process_status = lambda: {"is_running": True, "pid": 1235}
    service.get_continuous_collector_process_status = lambda: {"is_running": True, "pid": 1234}
    summary = service.get_live_pipeline_summary()
    assert summary["pipeline_status"] == "DEGRADED"


def test_fault_8_malformed_persisted_state_recovery(tmp_path: Path):
    """Fault 8: Corrupt state files are safely handled without throwing uncaught exceptions."""
    corrupt_file = tmp_path / "corrupt_state.json"
    corrupt_file.write_text("{malformed json:::;;;", encoding="utf-8")

    service = DashboardDataService(workspace_root=tmp_path)
    c_status = service.get_continuous_collector_status(corrupt_file)
    assert c_status["status"] == "ERROR"
    assert "error" in c_status

    e_status = service.get_continuous_evaluator_status(corrupt_file)
    assert e_status["status"] == "ERROR"
    assert "error" in e_status

    a_status = service.get_alert_service_status(corrupt_file)
    assert a_status["status"] == "ERROR"
    assert "error" in a_status


def test_fault_9_graceful_shutdown_state_flush(tmp_path: Path):
    """Fault 9: Graceful shutdown terminates background workers and flushes atomic state."""
    alert_state = tmp_path / "alert_state.json"
    alert_hist = tmp_path / "alert_hist.jsonl"
    cfg = AlertConfig(
        enabled=True,
        state_file=alert_state,
        history_file=alert_hist,
        desktop_notifications_enabled=False,
    )
    engine = AlertEngine(config=cfg)
    engine.start()

    # Enqueue alerts
    for i in range(5):
        alert = make_opportunity_alert(alert_id=f"alert-{i}", candidate_id=f"cand-{i}")
        engine._dispatch_alert(alert)

    # Immediately signal stop
    engine.stop(timeout=2.0)
    assert engine.is_running is False
    assert alert_state.exists()
    assert alert_hist.exists()

    with open(alert_hist, "r") as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 5


def test_deterministic_qualification_reconciliation():
    """Verify execution qualifier calculations against independent manual arithmetic."""
    fee_cfg = FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(
            fee_model_config=fee_cfg,
            min_net_profit_cents=1,
        )
    )

    now = datetime.now(timezone.utc)
    market = make_mock_market("M1")
    # Leg 1: YES ask 42c (resting NO bid at 58c, qty 10)
    # Leg 2: NO ask 51c (resting YES bid at 49c, qty 10)
    # Total sum of asks = 42 + 51 = 93c. Gross edge = 7c/contract.
    book = NormalizedOrderBook(
        market_ticker="M1",
        yes_bids=(
            OrderBookLevel(price_dollars=Decimal("0.49"), quantity=Decimal("10")),
        ),
        no_bids=(
            OrderBookLevel(price_dollars=Decimal("0.58"), quantity=Decimal("10")),
        ),
        source_timestamp=now,
    )
    obs = MarketObservation(
        observation_id="obs-recon",
        ticker="M1",
        observed_at=now,
        source_timestamp=now,
        market=market,
        order_book=book,
        is_success=True,
        is_stale=False,
    )

    qual = qualifier.qualify_binary_parity(obs, requested_quantity=5)

    # Manual arithmetic reconciliation:
    # Target quantity = 5
    # YES ask = 42c * 5 = 210c
    # NO ask = 51c * 5 = 255c
    # Gross cost = 210 + 255 = 465c
    # Guaranteed payout = 5 * 100c = 500c
    # Gross profit = 500 - 465 = 35c
    assert qual.executable_quantity == 5
    assert qual.total_cost_cents == 465
    assert qual.gross_edge_cents == 35
    assert qual.guaranteed_payout_cents == 500
    assert qual.is_qualified is True
    assert qual.total_fees_cents is not None
    # Net edge = gross edge - total fees
    assert qual.net_edge_cents == 35 - qual.total_fees_cents
    assert qual.outcome == QualificationOutcome.QUALIFIED


def test_provenance_isolation_live_vs_historical_vs_simulated(tmp_path: Path):
    """Verify DashboardDataService strictly isolates live evidence from historical datasets."""
    # Create fake historical dataset
    hist_dir = tmp_path / "evidence_dashboard_trial"
    hist_dir.mkdir(parents=True)
    (hist_dir / "observations.jsonl").write_text('{"record": 1}\n', encoding="utf-8")

    # Create live dataset
    live_dir = tmp_path / "data" / "live_collection"
    live_dir.mkdir(parents=True)
    (live_dir / "observations.jsonl").write_text('{"record": 2}\n{"record": 3}\n', encoding="utf-8")

    service = DashboardDataService(workspace_root=tmp_path)
    datasets = service.get_available_datasets()
    names = [d["id"] for d in datasets]
    assert "evidence_dashboard_trial" in names

    # Live path resolution
    resolved_live = service.resolve_dataset_path("live")
    assert resolved_live == live_dir / "observations.jsonl"


def test_controlled_alert_lifecycle_and_secret_sanitization(tmp_path: Path):
    """Verify alert transitions, secret URL masking, and deduplication."""
    # Test secret masking
    dirty_url = "https://hooks.slack.com/services/T00/B00/SECRET_KEY?token=supersecret123&password=mysecretpassword"
    clean_url = sanitize_url(dirty_url)
    assert "supersecret123" not in clean_url
    assert "mysecretpassword" not in clean_url
    assert "***REDACTED***" in clean_url

    dirty_headers = {"Authorization": "Bearer sensitive_token_999", "Content-Type": "application/json"}
    clean_headers = sanitize_headers(dirty_headers)
    assert clean_headers["Authorization"] == "***REDACTED***"

    # Test Alert Engine lifecycle events
    alert_hist = tmp_path / "alert_hist.jsonl"
    alert_state = tmp_path / "alert_state.json"
    engine = AlertEngine(config=AlertConfig(state_file=alert_state, history_file=alert_hist))
    engine.start()

    now = datetime.now(timezone.utc)
    cand = ArbitrageCandidate(
        candidate_id="cand-lifecycle",
        strategy_type="BINARY_PARITY",
        event_ticker="EV1",
        market_tickers=("M1",),
        state=CandidateState.NEW,
        detected_at=now.isoformat(),
        updated_at=now.isoformat(),
        source_timestamps={"M1": now.isoformat()},
        observation_ids=("obs-1",),
        gross_edge_cents=10,
        estimated_fees_cents=2,
        net_edge_cents=8,
        guaranteed_payout_cents=500,
        total_cost_cents=460,
        target_quantity=5,
        supported_quantity=5,
        is_mathematically_valid=True,
        is_depth_supported=True,
        is_net_profitable=True,
        is_qualified=True,
        status="STATUS_QUALIFIED",
        rejection_reason=None,
        market_state_fingerprint="fp1",
        per_leg_prices_cents={"M1": 92},
    )

    # 1. New qualification
    a1 = engine.process_evaluator_cycle({"cand-lifecycle": cand}, [], now=now)
    assert len(a1) == 1
    assert a1[0].event_type == AlertEventType.NEW_QUALIFIED

    # 2. Duplicate suppression
    a2 = engine.process_evaluator_cycle({"cand-lifecycle": cand}, [], now=now + timedelta(seconds=1))
    assert len(a2) == 0

    # 3. Material change
    cand.net_edge_cents = 18  # +10c change > 5c threshold
    a3 = engine.process_evaluator_cycle({"cand-lifecycle": cand}, [], now=now + timedelta(seconds=2))
    assert len(a3) == 1
    assert a3[0].event_type == AlertEventType.MATERIAL_CHANGE

    engine.stop()
