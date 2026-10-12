"""Tests for the Kalshi Arbitrage Engine Dashboard (Frontend V1).

Verifies:
- Data ingestion and dataset discovery
- Empty, corrupted, and malformed evidence handling
- Order book status classification (EMPTY, POPULATED, STALE, INVALID)
- Strict separation of real evidence from simulated demonstration rows
- V7 Historical Validation report integration
- Read-only HTTP REST server endpoints and static assets
"""

import json
from pathlib import Path
import tempfile
import threading
import urllib.request
import pytest

from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.dashboard.server import (
    DashboardRequestHandler,
    DashboardServer,
    create_dashboard_server,
)


@pytest.fixture
def workspace_root():
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def service(workspace_root):
    return DashboardDataService(workspace_root=workspace_root)


def test_discover_available_datasets(service):
    datasets = service.get_available_datasets()
    assert len(datasets) >= 1
    ids = [d["id"] for d in datasets]
    assert "evidence_dashboard_trial" in ids

    trial = next(d for d in datasets if d["id"] == "evidence_dashboard_trial")
    assert trial["record_count"] == 44
    assert trial["is_default"] is True
    assert trial["size_bytes"] > 0


def test_resolve_dataset_path(service, workspace_root):
    # Default
    default_p = service.resolve_dataset_path()
    assert default_p.exists()
    assert default_p.name == "observations.jsonl"
    assert "evidence_dashboard_trial" in str(default_p)

    # Named
    named_p = service.resolve_dataset_path("evidence_dashboard_trial")
    assert named_p.exists()
    assert "evidence_dashboard_trial" in str(named_p)

    # Non-existent
    non_existent = service.resolve_dataset_path("non_existent_dir")
    assert not non_existent.exists()


def test_load_real_trial_dataset(service):
    trial_path = service.resolve_dataset_path("evidence_dashboard_trial")
    observations, malformed, total_raw = service.load_observations(trial_path)

    assert total_raw == 44
    assert len(observations) == 44
    assert malformed == 0

    first = observations[0]
    assert first.ticker.startswith("KX")
    assert first.observed_at is not None
    assert first.source_timestamp is not None
    assert first.is_success is True


def test_load_empty_and_malformed_evidence(service, tmp_path):
    # 1. Non-existent file
    empty_file = tmp_path / "missing.jsonl"
    obs, malformed, raw = service.load_observations(empty_file)
    assert obs == []
    assert malformed == 0
    assert raw == 0

    # 2. Empty file
    empty_file.write_text("")
    obs, malformed, raw = service.load_observations(empty_file)
    assert obs == []
    assert malformed == 0
    assert raw == 0

    # 3. Malformed records
    corrupt_file = tmp_path / "corrupt.jsonl"
    corrupt_file.write_text(
        "INVALID JSON LINE\n"
        '{"schema_version": "1.0", "observation_id": "test"}\n'  # missing required fields
        "{bad json\n"
    )
    obs, malformed, raw = service.load_observations(corrupt_file)
    assert len(obs) == 0
    assert malformed == 3
    assert raw == 3


def test_overview_metrics_on_real_trial(service):
    overview = service.get_overview("evidence_dashboard_trial")
    assert overview["valid_observations_count"] == 44
    assert overview["total_raw_records"] == 44
    assert overview["malformed_records_count"] == 0
    assert overview["unique_markets_count"] == 15
    assert overview["unique_events_count"] == 15
    assert overview["api_success_count"] == 44
    assert overview["api_failure_count"] == 0
    assert overview["api_reliability_pct"] == 100.0
    assert overview["empty_books_count"] == 44
    assert overview["populated_books_count"] == 0
    assert overview["stale_observations_count"] == 0
    assert overview["theoretical_candidates_count"] == 0
    assert overview["qualified_opportunities_count"] == 0
    assert overview["is_data_historical"] is True
    assert overview["earliest_observation_at"] is not None
    assert overview["latest_observation_at"] is not None


def test_markets_table_classification(service):
    markets = service.get_markets_table("evidence_dashboard_trial")
    assert len(markets) == 15

    for m in markets:
        assert m["ticker"].startswith("KX")
        assert m["event_ticker"].startswith("KX")
        assert m["status"] in ("active", "open", "closed")
        assert m["order_book_status"] == "EMPTY"
        assert m["yes_bids_levels_count"] == 0
        assert m["no_bids_levels_count"] == 0
        assert m["observation_count"] >= 1
        assert m["first_observed_at"] is not None
        assert m["latest_observed_at"] is not None


def test_opportunities_real_versus_simulated(service):
    # 1. Real evidence: must show honest 0 opportunities
    real_opps = service.get_opportunities("evidence_dashboard_trial", include_simulated=False)
    assert real_opps["is_simulated"] is False
    assert real_opps["count"] == 0
    assert real_opps["qualified_count"] == 0
    assert real_opps["opportunities"] == []
    assert "No arbitrage opportunities detected" in real_opps["message"]

    # 2. Simulated demonstration data: must be visibly labeled SIMULATED
    sim_opps = service.get_opportunities("evidence_dashboard_trial", include_simulated=True)
    assert sim_opps["is_simulated"] is True
    assert "ATTENTION: VISIBLY LABELED SIMULATION DATA" in sim_opps["disclaimer"]
    assert sim_opps["count"] == 2
    assert sim_opps["qualified_count"] == 1
    assert len(sim_opps["opportunities"]) == 2

    # Check fields of simulated opportunities
    op1 = sim_opps["opportunities"][0]
    assert op1["is_simulated"] is True
    assert op1["is_qualified"] is True
    assert op1["gross_edge_cents"] == 4
    assert op1["net_edge_cents"] == 2
    assert op1["total_cost_cents"] == 96

    op2 = sim_opps["opportunities"][1]
    assert op2["is_simulated"] is True
    assert op2["is_qualified"] is False
    assert "Unprofitable after estimated fees" in op2["rejection_reason"]


def test_historical_validation_integration(service):
    v7_report = service.get_historical_validation("evidence_dashboard_trial")
    assert v7_report["exists"] is True
    assert "section_1_dataset_identity" in v7_report
    assert "section_2_evidence_quality" in v7_report
    assert "section_10_final_verdict" in v7_report

    s1 = v7_report["section_1_dataset_identity"]
    assert s1["valid_parsed_observations"] == 44
    assert s1["unique_tickers_count"] == 15

    s2 = v7_report["section_2_evidence_quality"]
    assert s2["fresh_records_count"] == 44
    assert s2["stale_records_count"] == 0

    assert "NO LIQUIDITY IN EVIDENCE" in v7_report["section_10_final_verdict"]


def test_system_health(service):
    health = service.get_system_health()
    assert health["server_time"] is not None
    assert health["observer_process"]["status"] in ("STOPPED", "RUNNING", "UNKNOWN")
    assert health["datasets_count"] >= 1
    assert health["total_records_stored"] >= 44
    assert health["total_storage_bytes"] > 0
    assert isinstance(health["warnings"], list)


def test_http_api_endpoints_integration(workspace_root):
    server = create_dashboard_server(host="127.0.0.1", port=8095, workspace_root=workspace_root)
    url = server.start(blocking=False)
    assert url.startswith("http://127.0.0.1:")

    try:
        # Test Static Assets
        for path, mime in [
            ("/", "text/html; charset=utf-8"),
            ("/dashboard.css", "text/css; charset=utf-8"),
            ("/dashboard.js", "application/javascript; charset=utf-8"),
        ]:
            resp = urllib.request.urlopen(f"{url}{path}", timeout=3)
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == mime
            assert len(resp.read()) > 0

        # Test API Endpoints
        endpoints = [
            "/api/status",
            "/api/overview",
            "/api/markets",
            "/api/opportunities",
            "/api/opportunities?simulated=true",
            "/api/historical",
            "/api/v8/simulation",
            "/api/datasets",
        ]
        for ep in endpoints:
            resp = urllib.request.urlopen(f"{url}{ep}", timeout=3)
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "application/json; charset=utf-8"
            payload = json.loads(resp.read().decode("utf-8"))
            assert isinstance(payload, dict)

        # Test 404
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{url}/api/non_existent_route", timeout=3)
        assert exc_info.value.code == 404

    finally:
        server.stop()


def test_frontend_v1_1_static_refinements(workspace_root):
    """Verify that Frontend V1.1 contains refined, calm elements and 4 primary metrics."""
    static_dir = workspace_root / "kalshi_arbitrage" / "dashboard" / "static"
    html_content = (static_dir / "index.html").read_text(encoding="utf-8")
    css_content = (static_dir / "dashboard.css").read_text(encoding="utf-8")

    # 4 Primary Metrics are present in HTML
    assert 'id="kpi-markets-count"' in html_content
    assert 'id="kpi-total-obs"' in html_content
    assert 'id="kpi-freshness-age"' in html_content
    assert 'id="kpi-opps-qualified"' in html_content

    # Compact header observer indicator
    assert 'id="observer-status-pill"' in html_content
    assert 'id="observer-status-text"' in html_content
    assert 'id="env-pill"' in html_content

    # Simplified breakdown
    assert 'id="toggle-funnel-btn"' in html_content
    assert 'id="funnel-breakdown-body"' in html_content

    # Clean styling variables in CSS
    assert "--bg-page" in css_content
    assert "--bg-surface" in css_content
    assert "--status-green" in css_content
    assert "--status-amber" in css_content


def test_v8_simulation_service(service):
    """Verify DashboardDataService executes V8 simulation on authentic trial data."""
    sim = service.get_v8_simulation("evidence_dashboard_trial")
    assert sim["exists"] is True
    assert "execution_lifecycle" in sim
    assert "financial_accounting" in sim
    assert sim["financial_accounting"]["ledger_is_reconciled"] is True
    assert "human_summary" in sim


def test_get_paper_trading_status_missing_file(service, tmp_path):
    missing_file = tmp_path / "does_not_exist.json"
    status = service.get_paper_trading_status(state_filepath=missing_file)
    assert status["is_active"] is False
    assert status["status"] == "STOPPED"
    assert status["process_running"] is False
    assert "No active paper trading session file found" in status["message"]


def test_get_paper_trading_status_stopped_session(service, tmp_path, monkeypatch):
    # Mock process table to show no running paper trader process
    monkeypatch.setattr(
        service, "get_paper_trader_process_status", lambda: {"is_running": False, "pid": None}
    )
    state_file = tmp_path / "stopped_state.json"
    state_data = {
        "schema_version": "2.0",
        "session_id": "test-sess-stopped",
        "execution_mode": "paper",
        "status": "STOPPED",
        "initial_cash_cents": 1_000_000,
        "last_completed_cycle": 5,
        "portfolio": {
            "available_cash_cents": 1_000_000,
            "reserved_cash_cents": 0,
            "positions": [],
        },
        "trades": [],
        "cycle_history": [],
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")

    status = service.get_paper_trading_status(state_filepath=state_file)
    assert status["is_active"] is False
    assert status["status"] == "STOPPED"
    assert status["process_running"] is False
    assert status["session_id"] == "test-sess-stopped"
    assert status["last_completed_cycle"] == 5
    assert status["available_cash_cents"] == 1_000_000


def test_get_paper_trading_status_running_process(service, tmp_path, monkeypatch):
    # Mock process table to show an active paper trader process
    monkeypatch.setattr(
        service,
        "get_paper_trader_process_status",
        lambda: {"is_running": True, "pid": 12345, "command": "python scripts/run_paper_trader.py"},
    )
    state_file = tmp_path / "running_state.json"
    state_data = {
        "schema_version": "2.0",
        "session_id": "test-sess-running",
        "execution_mode": "paper",
        "status": "RUNNING",
        "initial_cash_cents": 1_000_000,
        "last_completed_cycle": 10,
        "portfolio": {
            "available_cash_cents": 1_000_000,
            "reserved_cash_cents": 0,
            "positions": [],
        },
        "trades": [],
        "cycle_history": [],
    }
    state_file.write_text(json.dumps(state_data), encoding="utf-8")

    status = service.get_paper_trading_status(state_filepath=state_file)
    assert status["is_active"] is True
    assert status["status"] == "RUNNING"
    assert status["process_running"] is True
    assert status["process_pid"] == 12345
    assert status["session_id"] == "test-sess-running"


def test_get_paper_trading_status_corrupted_file(service, tmp_path):
    corrupt_file = tmp_path / "corrupt_state.json"
    corrupt_file.write_text("bad json content", encoding="utf-8")

    status = service.get_paper_trading_status(state_filepath=corrupt_file)
    assert status["is_active"] is False
    assert status["status"] == "ERROR"
    assert "error" in status
