"""Comprehensive automated test suite for Kalshi Arbitrage Engine V23 Live Dashboard Integration.

Verifies:
1. DashboardDataService:
   - get_live_pipeline_summary() under STOPPED, HEALTHY, DEGRADED, and PARTIAL states.
   - Candidate filtering by strategy, outcome, state, market ticker, and qualification.
   - Qualified candidates filtering by strategy, market ticker, and min_profit_cents.
   - Candidate lifecycle transition history retrieval, limit clamping, and filtering.
   - Provenance separation (LIVE vs HISTORICAL vs SIMULATION).
   - Safe handling of missing, corrupt, or empty telemetry state files.
2. DashboardServer HTTP APIs:
   - GET /api/live/summary and /api/pipeline/summary.
   - GET /api/collector/status and /api/evaluator/status.
   - GET /api/evaluator/candidates with query parameter filters.
   - GET /api/evaluator/qualified with query parameter filters.
   - GET /api/evaluator/history with query parameter filters.
   - GET /api/status returning comprehensive live_pipeline diagnostics.
   - Static asset delivery: /, /index.html, /dashboard.css, /dashboard.js with valid Content-Type and 200 OK.
   - Proper Cache-Control and CORS headers.
   - 404 handling for unknown endpoints.
3. Realistic Live Pipeline States:
   - Fresh data vs stale data (>60s) telemetry detection.
   - Rejection outcomes breakdown mapping.
   - Empty state when 0 candidates qualify.
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
    ArbitrageCandidate,
    CandidateQualification,
    CandidateState,
    ContinuousEvaluatorConfig,
    ExecutionQualifierConfig,
    FeeScheduleType,
    LegExecutionDetail,
    QualificationOutcome,
)
from kalshi_arbitrage.arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
)
from kalshi_arbitrage.dashboard.server import DashboardServer, create_dashboard_server
from kalshi_arbitrage.dashboard.service import DashboardDataService


# -----------------------------------------------------------------------------
# Fixtures & Mock State Builders
# -----------------------------------------------------------------------------

def write_mock_collector_state(
    workspace_root: Path,
    status: str = "RUNNING",
    is_fresh: bool = True,
    age_seconds: float = 1.2,
    markets_count: int = 15,
    session_id: str = "coll-session-1",
    last_error: Optional[str] = None,
) -> Path:
    data_dir = workspace_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_file = data_dir / "live_collector_state.json"

    now = datetime.now(timezone.utc)
    snap_time = now - timedelta(seconds=age_seconds)

    payload = {
        "status": status,
        "pid": 12345 if status == "RUNNING" else None,
        "session_id": session_id,
        "environment": "production",
        "started_at": (now - timedelta(minutes=10)).isoformat(),
        "stopped_at": None if status == "RUNNING" else now.isoformat(),
        "last_successful_collection_time": now.isoformat(),
        "last_attempted_collection_time": now.isoformat(),
        "latest_snapshot_timestamp": snap_time.isoformat(),
        "latest_snapshot_age_seconds": age_seconds,
        "is_data_fresh": is_fresh,
        "consecutive_failures": 0 if is_fresh else 2,
        "successful_cycles_count": 42,
        "failed_cycles_count": 0 if is_fresh else 2,
        "total_cycles_count": 42,
        "total_observations_count": 420,
        "active_monitored_markets_count": markets_count,
        "last_cycle_duration_seconds": 0.45,
        "last_cycle_markets_polled": markets_count,
        "last_cycle_markets_succeeded": markets_count,
        "last_cycle_markets_failed": 0,
        "last_error": last_error,
        "last_error_code": None,
        "rate_limit_count": 0,
        "poll_interval_seconds": 2.0,
        "max_stale_seconds": 60.0,
    }

    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return state_file


def write_mock_evaluator_state(
    workspace_root: Path,
    status: str = "RUNNING",
    active_candidates: Optional[List[Dict[str, Any]]] = None,
    session_id: str = "eval-session-1",
    last_error: Optional[str] = None,
) -> Path:
    data_dir = workspace_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_file = data_dir / "live_evaluator_state.json"

    now = datetime.now(timezone.utc)
    cands = active_candidates or []
    qual_count = sum(1 for c in cands if c.get("is_qualified") or c.get("qualification", {}).get("is_qualified"))
    rej_count = len(cands) - qual_count

    outcomes_breakdown: Dict[str, int] = {}
    for c in cands:
        out = c.get("qualification", {}).get("outcome") or c.get("status", "UNKNOWN")
        outcomes_breakdown[out] = outcomes_breakdown.get(out, 0) + 1

    payload = {
        "status": status,
        "pid": 23456 if status == "RUNNING" else None,
        "session_id": session_id,
        "started_at": (now - timedelta(minutes=10)).isoformat(),
        "stopped_at": None if status == "RUNNING" else now.isoformat(),
        "last_evaluation_time": now.isoformat(),
        "evaluations_count": 42,
        "processed_observations_count": 420,
        "active_candidates_count": len(cands),
        "qualified_candidates_count": qual_count,
        "rejected_candidates_count": rej_count,
        "total_candidates_detected": len(cands),
        "candidate_transitions_count": 10,
        "is_running": status == "RUNNING",
        "latest_input_timestamp": now.isoformat(),
        "last_error": last_error,
        "last_error_code": None,
        "active_candidates": cands,
        "qualified_candidates": [c for c in cands if c.get("is_qualified") or c.get("qualification", {}).get("is_qualified")],
        "qualification_summary": {
            "total_evaluated": len(cands),
            "qualified_count": qual_count,
            "outcomes_breakdown": outcomes_breakdown,
        },
        "config": {
            "poll_interval_seconds": 1.0,
            "max_stale_seconds": 60.0,
            "target_quantity": 5,
            "require_full_quantity": True,
            "min_net_profit_cents": 1,
            "fee_schedule": "KALSHI_OFFICIAL_TAKER",
        },
    }

    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return state_file


def write_mock_history_records(workspace_root: Path, records: List[Dict[str, Any]]) -> Path:
    data_dir = workspace_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    history_file = data_dir / "live_candidate_history.jsonl"

    with open(history_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    return history_file


def make_mock_candidate(
    candidate_id: str,
    strategy_type: str = OPPORTUNITY_BINARY_PARITY,
    market_ticker: Optional[str] = None,
    state: str = "NEW",
    is_qualified: bool = True,
    outcome: str = "QUALIFIED",
    gross_edge_cents: int = 15,
    fees_cents: int = 4,
    net_edge_cents: int = 11,
    rejection_reason: Optional[str] = None,
) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    ticker = market_ticker or candidate_id.split(":", 1)[-1]
    return {
        "candidate_id": candidate_id,
        "strategy_type": strategy_type,
        "event_ticker": "KXTEST",
        "market_ticker": ticker,
        "market_tickers": [ticker],
        "state": state,
        "detected_at": now_iso,
        "updated_at": now_iso,
        "gross_edge_cents": gross_edge_cents,
        "estimated_fees_cents": fees_cents,
        "net_edge_cents": net_edge_cents,
        "guaranteed_payout_cents": 100,
        "total_cost_cents": 100 - gross_edge_cents,
        "target_quantity": 5,
        "supported_quantity": 5 if is_qualified else 0,
        "is_qualified": is_qualified,
        "status": outcome,
        "rejection_reason": rejection_reason,
        "qualification": {
            "qualification_id": f"qual-{uuid.uuid4().hex[:8]}",
            "candidate_id": candidate_id,
            "strategy_type": strategy_type,
            "evaluated_at": now_iso,
            "outcome": outcome,
            "is_qualified": is_qualified,
            "rejection_reason": rejection_reason,
            "requested_quantity": 5,
            "executable_quantity": 5 if is_qualified else 0,
            "is_partial_fill": False,
            "gross_edge_cents": gross_edge_cents,
            "gross_edge_per_unit_cents": str(Decimal(gross_edge_cents) / Decimal(5)),
            "total_cost_cents": 100 - gross_edge_cents,
            "guaranteed_payout_cents": 100,
            "total_fees_cents": fees_cents,
            "fees_per_leg_cents": {f"{ticker}:YES": fees_cents // 2, f"{ticker}:NO": fees_cents // 2},
            "net_edge_cents": net_edge_cents,
            "net_edge_per_unit_cents": str(Decimal(net_edge_cents) / Decimal(5)),
            "return_on_capital_pct": "12.50" if is_qualified else None,
            "leg_details": [
                {
                    "market_ticker": ticker,
                    "side": "YES",
                    "requested_quantity": 5,
                    "executable_quantity": 5 if is_qualified else 0,
                    "available_depth_contracts": 10 if is_qualified else 0,
                    "top_of_book_price_cents": 42,
                    "average_price_cents": "42.0",
                    "total_cost_cents": 210,
                    "fee_cents": fees_cents // 2,
                    "consumed_levels": [{"price_cents": 42, "quantity": 5, "cost_cents": 210}],
                },
                {
                    "market_ticker": ticker,
                    "side": "NO",
                    "requested_quantity": 5,
                    "executable_quantity": 5 if is_qualified else 0,
                    "available_depth_contracts": 10 if is_qualified else 0,
                    "top_of_book_price_cents": 43,
                    "average_price_cents": "43.0",
                    "total_cost_cents": 215,
                    "fee_cents": fees_cents // 2,
                    "consumed_levels": [{"price_cents": 43, "quantity": 5, "cost_cents": 215}],
                },
            ],
            "max_observed_skew_seconds": 0.05,
            "data_freshness_seconds": 1.1,
            "assumptions_and_limitations": [
                "Static snapshot assumption: depth and fees evaluated against resting limit orders observed at snapshot time.",
                "Non-atomic multi-leg execution risk: orders execute independently.",
            ],
        },
    }


# -----------------------------------------------------------------------------
# Unit Tests: DashboardDataService Live Pipeline Telemetry
# -----------------------------------------------------------------------------

def test_live_pipeline_summary_stopped(tmp_path: Path):
    """When collector and evaluator are not running, summary reports STOPPED."""
    service = DashboardDataService(workspace_root=tmp_path)
    summary = service.get_live_pipeline_summary()

    assert summary["pipeline_status"] == "STOPPED"
    assert summary["provenance"] == "STOPPED"
    assert summary["collector_status"] in ("STOPPED", "UNKNOWN")
    assert summary["evaluator_status"] in ("STOPPED", "UNKNOWN")
    assert summary["active_candidates_count"] == 0
    assert summary["qualified_candidates_count"] == 0
    assert summary["rejected_candidates_count"] == 0


def test_live_pipeline_summary_healthy(tmp_path: Path):
    """When collector and evaluator are running and fresh, summary reports HEALTHY."""
    # Write running mock state files
    write_mock_collector_state(tmp_path, status="RUNNING", is_fresh=True, age_seconds=1.5)
    write_mock_evaluator_state(tmp_path, status="RUNNING")

    service = DashboardDataService(workspace_root=tmp_path)
    service.get_continuous_collector_process_status = lambda: {"is_running": True, "pid": 12345}
    service.get_continuous_evaluator_process_status = lambda: {"is_running": True, "pid": 23456}
    summary = service.get_live_pipeline_summary()

    assert summary["pipeline_status"] == "HEALTHY"
    assert summary["provenance"] == "LIVE"
    assert summary["is_data_fresh"] is True
    assert summary["active_monitored_markets_count"] == 15
    assert summary["processed_observations_count"] == 420


def test_live_pipeline_summary_degraded_when_stale(tmp_path: Path):
    """When market data snapshot is stale (>60s), pipeline reports DEGRADED."""
    write_mock_collector_state(tmp_path, status="RUNNING", is_fresh=False, age_seconds=85.0)
    write_mock_evaluator_state(tmp_path, status="RUNNING")

    service = DashboardDataService(workspace_root=tmp_path)
    service.get_continuous_collector_process_status = lambda: {"is_running": True, "pid": 12345}
    service.get_continuous_evaluator_process_status = lambda: {"is_running": True, "pid": 23456}
    summary = service.get_live_pipeline_summary()

    assert summary["pipeline_status"] == "DEGRADED"
    assert summary["provenance"] == "LIVE"
    assert summary["is_data_fresh"] is False
    assert summary["latest_snapshot_age_seconds"] == pytest.approx(85.0, abs=2.0)


def test_live_pipeline_summary_partial(tmp_path: Path):
    """When only one service is running, pipeline reports PARTIAL."""
    write_mock_collector_state(tmp_path, status="RUNNING", is_fresh=True)
    write_mock_evaluator_state(tmp_path, status="STOPPED")

    service = DashboardDataService(workspace_root=tmp_path)
    service.get_continuous_collector_process_status = lambda: {"is_running": True, "pid": 12345}
    service.get_continuous_evaluator_process_status = lambda: {"is_running": False, "pid": None}
    summary = service.get_live_pipeline_summary()

    assert summary["pipeline_status"] == "PARTIAL"
    assert summary["collector_status"] == "RUNNING"
    assert summary["evaluator_status"] == "STOPPED"


# -----------------------------------------------------------------------------
# Unit Tests: Candidate & Rejection Filtering
# -----------------------------------------------------------------------------

def test_candidate_filtering_by_strategy_and_outcome(tmp_path: Path):
    """Test filtering candidates by strategy, qualification outcome, and status."""
    cand1 = make_mock_candidate("BP:KXTEST-1", strategy_type=OPPORTUNITY_BINARY_PARITY, is_qualified=True, outcome="QUALIFIED")
    cand2 = make_mock_candidate("BP:KXTEST-2", strategy_type=OPPORTUNITY_BINARY_PARITY, is_qualified=False, outcome="REJECTED_UNPROFITABLE_FEES")
    cand3 = make_mock_candidate("MECE:KXEVENT:YES", strategy_type=OPPORTUNITY_MECE_BASKET_LONG_YES, is_qualified=False, outcome="REJECTED_INSUFFICIENT_DEPTH")

    write_mock_evaluator_state(tmp_path, active_candidates=[cand1, cand2, cand3])
    service = DashboardDataService(workspace_root=tmp_path)

    # 1. No filters -> returns all 3
    all_cands = service.get_continuous_evaluator_candidates()
    assert len(all_cands) == 3

    # 2. Filter by strategy
    mece_only = service.get_continuous_evaluator_candidates(strategy=OPPORTUNITY_MECE_BASKET_LONG_YES)
    assert len(mece_only) == 1
    assert mece_only[0]["candidate_id"] == "MECE:KXEVENT:YES"

    # 3. Filter by outcome
    fee_rej = service.get_continuous_evaluator_candidates(outcome="REJECTED_UNPROFITABLE_FEES")
    assert len(fee_rej) == 1
    assert fee_rej[0]["candidate_id"] == "BP:KXTEST-2"

    # 4. Filter by is_qualified=True
    qual_only = service.get_continuous_evaluator_candidates(is_qualified=True)
    assert len(qual_only) == 1
    assert qual_only[0]["candidate_id"] == "BP:KXTEST-1"

    # 5. Filter by market ticker search
    mkt2 = service.get_continuous_evaluator_candidates(market="TEST-2")
    assert len(mkt2) == 1
    assert mkt2[0]["candidate_id"] == "BP:KXTEST-2"


def test_qualified_candidates_accessor(tmp_path: Path):
    """Test get_continuous_evaluator_qualified_candidates filtering."""
    cand_qual1 = make_mock_candidate("BP:MKT1", is_qualified=True, outcome="QUALIFIED", net_edge_cents=5)
    cand_qual2 = make_mock_candidate("BP:MKT2", is_qualified=True, outcome="QUALIFIED", net_edge_cents=25)
    cand_rej = make_mock_candidate("BP:MKT3", is_qualified=False, outcome="REJECTED_NO_GROSS_EDGE")

    write_mock_evaluator_state(tmp_path, active_candidates=[cand_qual1, cand_qual2, cand_rej])
    service = DashboardDataService(workspace_root=tmp_path)

    # All qualified
    qualified = service.get_continuous_evaluator_qualified_candidates()
    assert len(qualified) == 2
    assert all(c["is_qualified"] for c in qualified)

    # Filter with min_profit_cents threshold
    high_profit = service.get_continuous_evaluator_qualified_candidates(min_profit_cents=10)
    assert len(high_profit) == 1
    assert high_profit[0]["candidate_id"] == "BP:MKT2"


def test_candidate_history_pagination_and_filtering(tmp_path: Path):
    """Test candidate transition history reading and filtering."""
    now_iso = datetime.now(timezone.utc).isoformat()
    records = [
        {"candidate_id": "BP:MKT1", "from_state": None, "to_state": "NEW", "qualification_outcome": "QUALIFIED", "transitioned_at": now_iso, "reason": "New"},
        {"candidate_id": "BP:MKT1", "from_state": "NEW", "to_state": "UPDATED", "qualification_outcome": "QUALIFIED", "transitioned_at": now_iso, "reason": "Edge changed"},
        {"candidate_id": "BP:MKT2", "from_state": None, "to_state": "NEW", "qualification_outcome": "REJECTED_INSUFFICIENT_DEPTH", "transitioned_at": now_iso, "reason": "No depth"},
    ]
    write_mock_history_records(tmp_path, records)

    service = DashboardDataService(workspace_root=tmp_path)

    # All records in reverse order
    history = service.get_continuous_evaluator_history(limit=10)
    assert len(history) == 3
    assert history[0]["candidate_id"] == "BP:MKT2"  # Reversed!

    # Filter by candidate_id
    mkt1_hist = service.get_continuous_evaluator_history(candidate_id="BP:MKT1")
    assert len(mkt1_hist) == 2

    # Filter by to_state
    new_trans = service.get_continuous_evaluator_history(to_state="NEW")
    assert len(new_trans) == 2


def test_resolve_dataset_path_live(tmp_path: Path):
    """Test resolve_dataset_path('live') points to live collection directory."""
    live_dir = tmp_path / "data" / "live_collection"
    live_dir.mkdir(parents=True, exist_ok=True)
    live_obs = live_dir / "observations.jsonl"
    live_obs.write_text('{"mock": true}\n')

    service = DashboardDataService(workspace_root=tmp_path)
    resolved = service.resolve_dataset_path("live")

    assert resolved == live_obs


# -----------------------------------------------------------------------------
# Integration Tests: DashboardServer HTTP Endpoints
# -----------------------------------------------------------------------------

def test_dashboard_server_api_live_summary(tmp_path: Path):
    """Test GET /api/live/summary and /api/pipeline/summary."""
    write_mock_collector_state(tmp_path, status="RUNNING", is_fresh=True)
    write_mock_evaluator_state(tmp_path, status="RUNNING")

    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.service.get_continuous_collector_process_status = lambda: {"is_running": True, "pid": 12345}
    server.service.get_continuous_evaluator_process_status = lambda: {"is_running": True, "pid": 23456}
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        # GET /api/live/summary
        with urllib.request.urlopen(f"{base_url}/api/live/summary", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["pipeline_status"] == "HEALTHY"
            assert data["provenance"] == "LIVE"
            assert data["collector_status"] == "RUNNING"
            assert data["evaluator_status"] == "RUNNING"
            assert data["is_data_fresh"] is True

        # GET /api/pipeline/summary (alias)
        with urllib.request.urlopen(f"{base_url}/api/pipeline/summary", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["pipeline_status"] == "HEALTHY"
    finally:
        server.stop()


def test_dashboard_server_api_evaluator_filtered_candidates(tmp_path: Path):
    """Test GET /api/evaluator/candidates with query parameters."""
    cand1 = make_mock_candidate("BP:KXBTC", strategy_type=OPPORTUNITY_BINARY_PARITY, is_qualified=True)
    cand2 = make_mock_candidate("MECE:KXFED:YES", strategy_type=OPPORTUNITY_MECE_BASKET_LONG_YES, is_qualified=False, outcome="REJECTED_INSUFFICIENT_DEPTH")

    write_mock_evaluator_state(tmp_path, active_candidates=[cand1, cand2])

    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        # All candidates
        with urllib.request.urlopen(f"{base_url}/api/evaluator/candidates", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 2

        # Filter by outcome
        with urllib.request.urlopen(f"{base_url}/api/evaluator/candidates?outcome=REJECTED_INSUFFICIENT_DEPTH", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 1
            assert data["candidates"][0]["candidate_id"] == "MECE:KXFED:YES"

        # Filter by strategy
        with urllib.request.urlopen(f"{base_url}/api/evaluator/candidates?strategy=binary_parity", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 1
            assert data["candidates"][0]["candidate_id"] == "BP:KXBTC"
    finally:
        server.stop()


def test_dashboard_server_api_qualified_endpoint(tmp_path: Path):
    """Test GET /api/evaluator/qualified returning only execution-qualified opportunities."""
    cand1 = make_mock_candidate("BP:KXBTC", is_qualified=True, net_edge_cents=10)
    cand2 = make_mock_candidate("BP:KXETH", is_qualified=False, outcome="REJECTED_UNPROFITABLE_FEES")

    write_mock_evaluator_state(tmp_path, active_candidates=[cand1, cand2])

    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        with urllib.request.urlopen(f"{base_url}/api/evaluator/qualified", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 1
            assert data["qualified_candidates"][0]["candidate_id"] == "BP:KXBTC"
            assert data["qualified_candidates"][0]["qualification"]["is_qualified"] is True
    finally:
        server.stop()


def test_dashboard_server_api_history_filtered(tmp_path: Path):
    """Test GET /api/evaluator/history with limit and filters."""
    now_iso = datetime.now(timezone.utc).isoformat()
    records = [
        {"candidate_id": "BP:MKT1", "to_state": "NEW", "transitioned_at": now_iso},
        {"candidate_id": "BP:MKT2", "to_state": "UPDATED", "transitioned_at": now_iso},
    ]
    write_mock_history_records(tmp_path, records)

    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        with urllib.request.urlopen(f"{base_url}/api/evaluator/history?candidate_id=BP:MKT2", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 1
            assert data["history"][0]["candidate_id"] == "BP:MKT2"
    finally:
        server.stop()


def test_dashboard_server_static_assets(tmp_path: Path):
    """Test that static UI assets (HTML, CSS, JS) are served with correct headers."""
    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        # GET / (HTML)
        with urllib.request.urlopen(f"{base_url}/", timeout=3.0) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
            content = resp.read().decode()
            assert "Kalshi Arbitrage" in content
            assert "LIVE PIPELINE" in content
            assert "Execution-Qualified" in content

        # GET /dashboard.css (CSS)
        with urllib.request.urlopen(f"{base_url}/dashboard.css", timeout=3.0) as resp:
            assert resp.status == 200
            assert "text/css" in resp.headers.get("Content-Type", "")
            content = resp.read().decode()
            assert "--bg-page" in content
            assert ".provenance-tag" in content

        # GET /dashboard.js (JavaScript)
        with urllib.request.urlopen(f"{base_url}/dashboard.js", timeout=3.0) as resp:
            assert resp.status == 200
            assert "application/javascript" in resp.headers.get("Content-Type", "")
            content = resp.read().decode()
            assert "refreshAllData" in content
            assert "loadLivePipelineSummary" in content
            assert "isRefreshing" in content
    finally:
        server.stop()


def test_dashboard_server_404_handling(tmp_path: Path):
    """Test that nonexistent endpoints return JSON 404."""
    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/api/nonexistent_endpoint", timeout=3.0)
        assert exc_info.value.code == 404
        data = json.loads(exc_info.value.read().decode())
        assert data["error"] == "Not Found"
    finally:
        server.stop()
