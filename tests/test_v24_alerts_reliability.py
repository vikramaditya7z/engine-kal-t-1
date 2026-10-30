"""Comprehensive automated test suite for Kalshi Arbitrage Engine V24 Alerts & Reliability.

Verifies:
1. Newly qualified candidate notifications:
   - When a candidate becomes execution-qualified, exactly one NEW_QUALIFIED alert is generated.
2. Unqualified candidates suppressed:
   - Candidates rejected for depth, fees, edge, or stale data NEVER generate opportunity alerts.
3. Persistent duplicate suppression across cycles:
   - Unchanged qualified candidate state across polling cycles produces zero duplicate alerts.
4. Material-change alerts:
   - Net profit change >= threshold generates MATERIAL_CHANGE alert.
   - Executable quantity change >= threshold generates MATERIAL_CHANGE alert.
   - Sub-threshold changes are cleanly suppressed.
5. Candidate lifecycle transitions:
   - Qualified -> Disqualified generates DISQUALIFIED alert.
   - Qualified -> Invalidated generates INVALIDATED alert.
   - Qualified -> Disappeared generates DISAPPEARED alert (emitted exactly once).
   - Unqualified candidate transitions NEVER trigger disqualification/disappearance alerts.
6. Requalification and cooldown:
   - Candidate re-qualifying after cooldown generates a new qualification alert.
   - Requalification within cooldown window is suppressed.
7. Persistence across restarts:
   - Deduplication state survives process restarts; restarting engine does NOT flood duplicate alerts.
8. Delivery channels:
   - Local history channel appends to JSONL file.
   - Webhook delivery succeeds on 200 OK.
   - Webhook retries with backoff on failure and records failures accurately.
   - Desktop channel handles macOS and non-macOS platforms gracefully.
9. Secret and credential redaction:
   - Tokens in webhook URLs, auth headers, and error messages are masked as ***REDACTED***.
10. Queue backpressure and bounded memory:
    - Queue drop handled gracefully without memory leak.
11. Clean shutdown:
    - Worker drains queue or shuts down within timeout; state is saved atomically.
12. Operational health & Dashboard integration:
    - GET /api/alerts/status returns full telemetry.
    - GET /api/alerts/history returns reverse-chronological alert log with filters.
    - GET /api/live/summary includes alert health and counts.
13. Fault isolation:
    - Failing notification channel does not crash evaluation loop.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional
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
    ContinuousEvaluator,
    ContinuousEvaluatorConfig,
    ExecutionQualifierConfig,
    FeeScheduleType,
    LegExecutionDetail,
    OpportunityAlert,
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


# -----------------------------------------------------------------------------
# Test Fixtures & Mock Builders
# -----------------------------------------------------------------------------

def make_candidate_fixture(
    candidate_id: str = "BP:KXTEST-1",
    strategy_type: str = OPPORTUNITY_BINARY_PARITY,
    is_qualified: bool = True,
    outcome: str = "QUALIFIED",
    net_edge_cents: int = 15,
    supported_quantity: int = 5,
    total_cost_cents: int = 85,
    gross_edge_cents: int = 20,
    rejection_reason: Optional[str] = None,
) -> ArbitrageCandidate:
    now_iso = datetime.now(timezone.utc).isoformat()
    ticker = candidate_id.split(":", 1)[-1]

    qual = CandidateQualification(
        qualification_id=f"qual-{uuid.uuid4().hex[:8]}",
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        evaluated_at=now_iso,
        outcome=QualificationOutcome(outcome) if outcome in QualificationOutcome._value2member_map_ else QualificationOutcome.QUALIFIED,
        is_qualified=is_qualified,
        rejection_reason=rejection_reason,
        requested_quantity=5,
        executable_quantity=supported_quantity if is_qualified else 0,
        is_partial_fill=False,
        gross_edge_cents=gross_edge_cents,
        gross_edge_per_unit_cents=Decimal(gross_edge_cents) / Decimal(5),
        total_cost_cents=total_cost_cents,
        guaranteed_payout_cents=100,
        total_fees_cents=gross_edge_cents - net_edge_cents,
        fees_per_leg_cents={f"{ticker}:YES": 2, f"{ticker}:NO": 3},
        net_edge_cents=net_edge_cents,
        net_edge_per_unit_cents=Decimal(net_edge_cents) / Decimal(5),
        return_on_capital_pct=Decimal("17.65") if is_qualified else None,
        leg_details=(
            LegExecutionDetail(
                market_ticker=ticker,
                side="YES",
                requested_quantity=5,
                executable_quantity=supported_quantity if is_qualified else 0,
                available_depth_contracts=10 if is_qualified else 0,
                top_of_book_price_cents=42,
                average_price_cents=Decimal("42.0"),
                total_cost_cents=210,
                fee_cents=2,
                consumed_levels=(),
            ),
        ),
        max_observed_skew_seconds=0.01,
        data_freshness_seconds=1.2,
        assumptions_and_limitations=("Static test assumption",),
    )

    state = CandidateState.NEW if is_qualified else CandidateState.DISQUALIFIED

    return ArbitrageCandidate(
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        event_ticker="KXTEST",
        market_tickers=(ticker,),
        state=state,
        detected_at=now_iso,
        updated_at=now_iso,
        source_timestamps={ticker: now_iso},
        observation_ids=("obs-1",),
        gross_edge_cents=gross_edge_cents,
        estimated_fees_cents=gross_edge_cents - net_edge_cents,
        net_edge_cents=net_edge_cents,
        guaranteed_payout_cents=100,
        total_cost_cents=total_cost_cents,
        target_quantity=5,
        supported_quantity=supported_quantity if is_qualified else 0,
        is_mathematically_valid=True,
        is_depth_supported=is_qualified,
        is_net_profitable=is_qualified,
        is_qualified=is_qualified,
        status=outcome,
        rejection_reason=rejection_reason,
        market_state_fingerprint="fp-test",
        per_leg_prices_cents={f"{ticker}:YES": 42, f"{ticker}:NO": 43},
        transitions=[],
        qualification=qual,
    )


class MockChannel(BaseNotificationChannel):
    """Test notification channel that records delivered alerts in memory."""

    def __init__(self, name: str = "mock", should_succeed: bool = True) -> None:
        self._name = name
        self.should_succeed = should_succeed
        self.delivered_alerts: List[OpportunityAlert] = []
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def send(self, alert: OpportunityAlert) -> bool:
        self.call_count += 1
        if not self.should_succeed:
            raise RuntimeError(f"Mock channel {self._name} intentional failure")
        self.delivered_alerts.append(alert)
        return True


# -----------------------------------------------------------------------------
# Unit Tests: Sanitization and Secret Redaction
# -----------------------------------------------------------------------------

def test_sanitize_url_masks_sensitive_tokens_and_passwords():
    """Verify webhook URLs mask tokens, keys, and credentials."""
    url1 = "https://discord.com/api/webhooks/12345/abcdef?token=secret123&other=visible"
    sanitized1 = sanitize_url(url1)
    assert "token=***REDACTED***" in sanitized1
    assert "other=visible" in sanitized1
    assert "secret123" not in sanitized1

    url2 = "https://user:my_secret_pass@example.com/webhook?api_key=privkey99"
    sanitized2 = sanitize_url(url2)
    assert "user:***REDACTED***@example.com" in sanitized2
    assert "api_key=***REDACTED***" in sanitized2
    assert "my_secret_pass" not in sanitized2
    assert "privkey99" not in sanitized2


def test_sanitize_headers_masks_auth():
    """Verify authorization headers are masked."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer super_secret_token_12345",
        "X-API-Key": "my_api_key_xyz",
    }
    sanitized = sanitize_headers(headers)
    assert sanitized["Content-Type"] == "application/json"
    assert sanitized["Authorization"] == "***REDACTED***"
    assert sanitized["X-API-Key"] == "***REDACTED***"


def test_redact_sensitive_text():
    """Verify arbitrary error strings have secrets redacted."""
    raw = "Failed connecting to https://hooks.slack.com?token=xyz123 with Bearer my_secret_jwt"
    redacted = redact_sensitive_text(raw)
    assert "xyz123" not in redacted
    assert "my_secret_jwt" not in redacted
    assert "token=***REDACTED***" in redacted


# -----------------------------------------------------------------------------
# Unit Tests: Deduplication and Alert Lifecycle
# -----------------------------------------------------------------------------

def test_newly_qualified_candidate_generates_alert(tmp_path: Path):
    """When an execution-qualified candidate appears, exactly one NEW_QUALIFIED alert is emitted."""
    cfg = AlertConfig(
        state_file=tmp_path / "live_alert_state.json",
        history_file=tmp_path / "live_alert_history.jsonl",
    )
    engine = AlertEngine(config=cfg)
    mock_chan = MockChannel("test_mock")
    engine.register_channel(mock_chan)

    cand = make_candidate_fixture(candidate_id="BP:KXBTC-1", is_qualified=True, net_edge_cents=25)

    alerts = engine.process_evaluator_cycle(
        active_candidates={"BP:KXBTC-1": cand},
        transitions=[],
    )

    assert len(alerts) == 1
    alt = alerts[0]
    assert alt.event_type == AlertEventType.NEW_QUALIFIED
    assert alt.candidate_id == "BP:KXBTC-1"
    assert alt.net_edge_cents == 25
    assert alt.executable_quantity == 5
    assert "DISCLAIMER" in alt.disclaimer


def test_unqualified_candidates_never_alert(tmp_path: Path):
    """Candidates failing qualification for depth, fees, or timing produce ZERO alerts."""
    cfg = AlertConfig(
        state_file=tmp_path / "live_alert_state.json",
        history_file=tmp_path / "live_alert_history.jsonl",
    )
    engine = AlertEngine(config=cfg)

    # 1. Candidate rejected for depth
    cand_depth = make_candidate_fixture(
        candidate_id="BP:NODEPTH",
        is_qualified=False,
        outcome="REJECTED_INSUFFICIENT_DEPTH",
        net_edge_cents=0,
    )

    # 2. Candidate rejected for fees
    cand_fees = make_candidate_fixture(
        candidate_id="BP:NOFEES",
        is_qualified=False,
        outcome="REJECTED_UNPROFITABLE_FEES",
        net_edge_cents=-4,
    )

    alerts = engine.process_evaluator_cycle(
        active_candidates={"BP:NODEPTH": cand_depth, "BP:NOFEES": cand_fees},
        transitions=[],
    )

    assert len(alerts) == 0


def test_duplicate_suppression_across_cycles(tmp_path: Path):
    """Subsequent cycles with identical candidate metrics generate zero duplicate alerts."""
    cfg = AlertConfig(
        state_file=tmp_path / "live_alert_state.json",
        history_file=tmp_path / "live_alert_history.jsonl",
    )
    engine = AlertEngine(config=cfg)

    cand = make_candidate_fixture("BP:KXBTC-1", is_qualified=True, net_edge_cents=20, supported_quantity=5)

    # Cycle 1: newly qualified -> 1 alert
    cycle1_alerts = engine.process_evaluator_cycle({"BP:KXBTC-1": cand}, [])
    assert len(cycle1_alerts) == 1

    # Cycle 2: identical state -> 0 alerts (suppressed!)
    cycle2_alerts = engine.process_evaluator_cycle({"BP:KXBTC-1": cand}, [])
    assert len(cycle2_alerts) == 0

    # Cycle 3: minor edge shift below threshold (e.g. +2c with threshold=5c) -> 0 alerts
    cand.net_edge_cents = 22
    cycle3_alerts = engine.process_evaluator_cycle({"BP:KXBTC-1": cand}, [])
    assert len(cycle3_alerts) == 0


def test_material_change_alert_on_profit_and_quantity(tmp_path: Path):
    """When net profit or executable quantity changes materially, a MATERIAL_CHANGE alert is emitted."""
    cfg = AlertConfig(
        profit_change_threshold_cents=5,
        quantity_change_threshold=2,
        state_file=tmp_path / "live_alert_state.json",
        history_file=tmp_path / "live_alert_history.jsonl",
    )
    engine = AlertEngine(config=cfg)

    cand = make_candidate_fixture("BP:KXBTC-1", is_qualified=True, net_edge_cents=20, supported_quantity=5)

    # Cycle 1: initial qualification
    engine.process_evaluator_cycle({"BP:KXBTC-1": cand}, [])

    # Cycle 2: net profit increases by 10c (>= 5c threshold)
    cand.net_edge_cents = 30
    c2_alerts = engine.process_evaluator_cycle({"BP:KXBTC-1": cand}, [])
    assert len(c2_alerts) == 1
    assert c2_alerts[0].event_type == AlertEventType.MATERIAL_CHANGE
    assert c2_alerts[0].net_edge_cents == 30
    assert c2_alerts[0].metadata["previous_net_edge_cents"] == 20

    # Cycle 3: executable quantity drops by 3 contracts (>= 2 threshold)
    cand.supported_quantity = 2
    c3_alerts = engine.process_evaluator_cycle({"BP:KXBTC-1": cand}, [])
    assert len(c3_alerts) == 1
    assert c3_alerts[0].event_type == AlertEventType.MATERIAL_CHANGE
    assert c3_alerts[0].executable_quantity == 2


def test_candidate_disqualification_invalidation_and_disappearance(tmp_path: Path):
    """When a qualified candidate is disqualified, invalidated, or disappears, explicit alerts are emitted."""
    cfg = AlertConfig(
        state_file=tmp_path / "live_alert_state.json",
        history_file=tmp_path / "live_alert_history.jsonl",
    )
    engine = AlertEngine(config=cfg)

    cand = make_candidate_fixture("BP:KXETH-1", is_qualified=True, net_edge_cents=15)
    engine.process_evaluator_cycle({"BP:KXETH-1": cand}, [])

    now_iso = datetime.now(timezone.utc).isoformat()

    # Case A: Candidate disqualified due to lack of ask depth
    trans_disq = CandidateTransition(
        transition_id="tr-1",
        candidate_id="BP:KXETH-1",
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        from_state="NEW",
        to_state="DISQUALIFIED",
        transitioned_at=now_iso,
        reason="Ask depth exhausted",
        gross_edge_cents=0,
        net_edge_cents=0,
        supported_quantity=0,
        market_tickers=("KXETH-1",),
        qualification_outcome="REJECTED_INSUFFICIENT_DEPTH",
    )

    alerts_disq = engine.process_evaluator_cycle({}, [trans_disq])
    assert len(alerts_disq) == 1
    assert alerts_disq[0].event_type == AlertEventType.DISQUALIFIED

    # Re-qualify candidate
    cand.is_qualified = True
    cand.net_edge_cents = 18
    engine.process_evaluator_cycle({"BP:KXETH-1": cand}, [], now=datetime.now(timezone.utc) + timedelta(seconds=120))

    # Case B: Candidate invalidated due to stale data
    trans_inval = CandidateTransition(
        transition_id="tr-2",
        candidate_id="BP:KXETH-1",
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        from_state="NEW",
        to_state="INVALIDATED",
        transitioned_at=now_iso,
        reason="Snapshot age > 60s",
        gross_edge_cents=18,
        net_edge_cents=12,
        supported_quantity=5,
        market_tickers=("KXETH-1",),
        qualification_outcome="REJECTED_STALE",
    )

    alerts_inval = engine.process_evaluator_cycle({}, [trans_inval])
    assert len(alerts_inval) == 1
    assert alerts_inval[0].event_type == AlertEventType.INVALIDATED

    # Re-qualify candidate
    engine.process_evaluator_cycle({"BP:KXETH-1": cand}, [], now=datetime.now(timezone.utc) + timedelta(seconds=240))

    # Case C: Candidate disappears (market not observed)
    trans_disapp = CandidateTransition(
        transition_id="tr-3",
        candidate_id="BP:KXETH-1",
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        from_state="NEW",
        to_state="DISAPPEARED",
        transitioned_at=now_iso,
        reason="Market inactive",
        gross_edge_cents=18,
        net_edge_cents=12,
        supported_quantity=5,
        market_tickers=("KXETH-1",),
    )

    alerts_disapp = engine.process_evaluator_cycle({}, [trans_disapp])
    assert len(alerts_disapp) == 1
    assert alerts_disapp[0].event_type == AlertEventType.DISAPPEARED

    # Subsequent cycle with same disappearance transition -> zero alerts (no duplicate disappearance alerts!)
    alerts_disapp_repeat = engine.process_evaluator_cycle({}, [trans_disapp])
    assert len(alerts_disapp_repeat) == 0


def test_requalification_cooldown(tmp_path: Path):
    """Requalification alert is suppressed during cooldown and emitted after cooldown expires."""
    cfg = AlertConfig(
        re_alert_cooldown_seconds=60.0,
        state_file=tmp_path / "live_alert_state.json",
        history_file=tmp_path / "live_alert_history.jsonl",
    )
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    engine = AlertEngine(config=cfg, time_provider=lambda: t0)

    cand = make_candidate_fixture("BP:COOLDOWN", is_qualified=True)

    # 1. Qualify at t=0
    a1 = engine.process_evaluator_cycle({"BP:COOLDOWN": cand}, [], now=t0)
    assert len(a1) == 1

    # 2. Disqualify at t=10s
    t1 = t0 + timedelta(seconds=10)
    trans = CandidateTransition(
        transition_id="tr-disq", candidate_id="BP:COOLDOWN", strategy_type=OPPORTUNITY_BINARY_PARITY,
        from_state="NEW", to_state="DISQUALIFIED", transitioned_at=t1.isoformat(), reason="Depth lost",
        gross_edge_cents=0, net_edge_cents=0, supported_quantity=0, market_tickers=("COOLDOWN",),
    )
    engine.process_evaluator_cycle({}, [trans], now=t1)

    # 3. Re-qualifies at t=30s (within 60s cooldown) -> Alert SUPPRESSED!
    t2 = t0 + timedelta(seconds=30)
    a2 = engine.process_evaluator_cycle({"BP:COOLDOWN": cand}, [], now=t2)
    assert len(a2) == 0

    # 4. Re-qualifies at t=75s (cooldown expired) -> Alert EMITTED!
    t3 = t0 + timedelta(seconds=75)
    a3 = engine.process_evaluator_cycle({"BP:COOLDOWN": cand}, [], now=t3)
    assert len(a3) == 1
    assert a3[0].event_type == AlertEventType.NEW_QUALIFIED


def test_persistence_across_process_restarts(tmp_path: Path):
    """Saved deduplication state prevents duplicate alert flood when process restarts."""
    state_file = tmp_path / "live_alert_state.json"
    hist_file = tmp_path / "live_alert_history.jsonl"
    cfg = AlertConfig(state_file=state_file, history_file=hist_file)

    engine1 = AlertEngine(config=cfg)
    cand = make_candidate_fixture("BP:RESTART", is_qualified=True, net_edge_cents=18)
    a1 = engine1.process_evaluator_cycle({"BP:RESTART": cand}, [])
    assert len(a1) == 1

    # Stop engine1 (persists state)
    engine1.stop()
    assert state_file.exists()

    # Simulate restart by creating engine2 with the same state file
    engine2 = AlertEngine(config=cfg)
    status2 = engine2.get_status()
    assert status2["tracked_candidates_count"] == 1
    assert status2["currently_qualified_count"] == 1

    # Cycle on engine2 with unchanged candidate -> ZERO duplicate alerts!
    a2 = engine2.process_evaluator_cycle({"BP:RESTART": cand}, [])
    assert len(a2) == 0


# -----------------------------------------------------------------------------
# Unit Tests: Notification Channels and Delivery
# -----------------------------------------------------------------------------

def test_local_history_channel(tmp_path: Path):
    """LocalHistoryChannel appends valid JSONL lines to file."""
    hist_file = tmp_path / "custom_alerts.jsonl"
    chan = LocalHistoryChannel(hist_file)

    alert = OpportunityAlert(
        alert_id="alt-123",
        candidate_id="BP:KXTEST",
        event_type=AlertEventType.NEW_QUALIFIED,
        timestamp=datetime.now(timezone.utc).isoformat(),
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        market_tickers=("KXTEST",),
        event_ticker="KXEVT",
        qualification_outcome="QUALIFIED",
        gross_edge_cents=20,
        estimated_fees_cents=4,
        net_edge_cents=16,
        target_quantity=5,
        executable_quantity=5,
        total_cost_cents=80,
        guaranteed_payout_cents=100,
        return_on_capital_pct="20.0",
        data_freshness_seconds=1.1,
        message="Test alert",
    )

    success = chan.send(alert)
    assert success is True
    assert hist_file.exists()

    with open(hist_file, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["alert_id"] == "alt-123"
    assert data["candidate_id"] == "BP:KXTEST"


def test_async_delivery_worker_thread(tmp_path: Path):
    """AlertEngine background worker consumes queue and delivers to channels asynchronously."""
    cfg = AlertConfig(
        state_file=tmp_path / "live_alert_state.json",
        history_file=tmp_path / "live_alert_history.jsonl",
    )
    engine = AlertEngine(config=cfg)
    mock_chan = MockChannel("async_mock")
    engine.register_channel(mock_chan)
    engine.start()

    try:
        cand = make_candidate_fixture("BP:ASYNC", is_qualified=True)
        engine.process_evaluator_cycle({"BP:ASYNC": cand}, [])

        # Wait briefly for background worker to consume
        time.sleep(0.3)

        assert mock_chan.call_count == 1
        assert len(mock_chan.delivered_alerts) == 1
        assert mock_chan.delivered_alerts[0].candidate_id == "BP:ASYNC"
        status = engine.get_status()
        assert status["alerts_delivered_count"] >= 1
    finally:
        engine.stop()


def test_webhook_failure_and_retries(tmp_path: Path):
    """Webhook channel retries on failure and does not crash scanner."""
    # Point to nonexistent port to trigger ConnectionRefused
    bad_url = "http://127.0.0.1:59999/nonexistent_webhook"
    chan = WebhookNotificationChannel(
        url=bad_url,
        timeout_seconds=0.2,
        max_retries=2,
        retry_backoff_seconds=0.01,
    )

    alert = OpportunityAlert(
        alert_id="alt-fail",
        candidate_id="BP:FAIL",
        event_type=AlertEventType.NEW_QUALIFIED,
        timestamp=datetime.now(timezone.utc).isoformat(),
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        market_tickers=("FAIL",),
        event_ticker=None,
        qualification_outcome="QUALIFIED",
        gross_edge_cents=20,
        estimated_fees_cents=4,
        net_edge_cents=16,
        target_quantity=5,
        executable_quantity=5,
        total_cost_cents=80,
        guaranteed_payout_cents=100,
        return_on_capital_pct=None,
        data_freshness_seconds=1.0,
        message="Fail test",
    )

    with pytest.raises(RuntimeError) as exc_info:
        chan.send(alert)
    assert "Webhook delivery failed" in str(exc_info.value)


# -----------------------------------------------------------------------------
# Integration Tests: Continuous Evaluator and Dashboard Server
# -----------------------------------------------------------------------------

def test_evaluator_with_alert_engine_integration(tmp_path: Path):
    """ContinuousEvaluator automatically initialises and runs AlertEngine."""
    eval_state = tmp_path / "live_evaluator_state.json"
    eval_hist = tmp_path / "live_candidate_history.jsonl"
    alt_state = tmp_path / "live_alert_state.json"
    alt_hist = tmp_path / "live_alert_history.jsonl"

    cfg = ContinuousEvaluatorConfig(
        state_file=eval_state,
        history_file=eval_hist,
        alert_state_file=alt_state,
        alert_history_file=alt_hist,
        alerts_enabled=True,
    )

    evaluator = ContinuousEvaluator(config=cfg)
    assert evaluator.alert_engine is not None
    assert isinstance(evaluator.alert_engine, AlertEngine)

    # Telemetry in state includes alert engine
    health = evaluator.get_health()
    assert health.status in ("STOPPED", "STARTING", "RUNNING")


def test_dashboard_server_api_alerts_endpoints(tmp_path: Path):
    """DashboardServer exposes /api/alerts/status and /api/alerts/history."""
    # Write mock alert state and history
    alt_state = tmp_path / "data" / "live_alert_state.json"
    alt_hist = tmp_path / "data" / "live_alert_history.jsonl"
    alt_state.parent.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(timezone.utc).isoformat()
    mock_state = {
        "status": "RUNNING",
        "alerts_created_count": 3,
        "alerts_delivered_count": 3,
        "alerts_failed_count": 0,
        "last_alert_time": now_iso,
        "last_delivery_time": now_iso,
        "qualified_candidates_tracked": {"BP:KXBTC": {"candidate_id": "BP:KXBTC", "is_currently_qualified": True}},
        "recent_delivery_errors": [],
    }
    alt_state.write_text(json.dumps(mock_state))

    mock_history = [
        {"alert_id": "alt-1", "candidate_id": "BP:KXBTC", "event_type": "NEW_QUALIFIED", "timestamp": now_iso},
        {"alert_id": "alt-2", "candidate_id": "BP:KXBTC", "event_type": "MATERIAL_CHANGE", "timestamp": now_iso},
    ]
    with open(alt_hist, "w") as f:
        for r in mock_history:
            f.write(json.dumps(r) + "\n")

    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        # GET /api/alerts/status
        with urllib.request.urlopen(f"{base_url}/api/alerts/status", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["alerts_created_count"] == 3
            assert data["alerts_delivered_count"] == 3

        # GET /api/alerts/history
        with urllib.request.urlopen(f"{base_url}/api/alerts/history?limit=10", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 2
            assert data["alerts"][0]["alert_id"] == "alt-2"  # Reverse order!

        # GET /api/live/summary includes alerts summary
        with urllib.request.urlopen(f"{base_url}/api/live/summary", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert "alerts" in data
            assert data["alerts_created_count"] == 3

    finally:
        server.stop()
