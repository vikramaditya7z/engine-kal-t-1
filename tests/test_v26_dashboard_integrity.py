"""Targeted regression tests for Dashboard Process Status Integrity, Provenance, and Session Consistency.

Verifies:
1. Authoritative process status resolution (distinguishing active OS process from stale persisted metadata).
2. Market count reconciliation (Populated + Empty + Stale + Unavailable = Total).
3. Scanner session metric consistency across selected directories.
4. Historical report structure and dataset preservation.
"""

import json
from pathlib import Path
import urllib.request
import pytest

from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.dashboard.server import create_dashboard_server


@pytest.fixture
def workspace_root():
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def service(workspace_root):
    return DashboardDataService(workspace_root=workspace_root)


def test_process_status_authoritative_resolution(service, tmp_path, monkeypatch):
    """Verify that when a process is dead in OS, status and pid are authoritative (STOPPED, None),
    even if the state JSON file contains stale metadata ('is_running': true, 'pid': 19629)."""
    # Create a mock state file mimicking stale data
    stale_eval_file = tmp_path / "live_evaluator_state.json"
    stale_eval_file.write_text(
        json.dumps({
            "status": "RUNNING",
            "pid": 19629,
            "is_running": True,
            "session_id": "stale-session-123",
            "evaluations_count": 2606,
        }),
        encoding="utf-8",
    )

    # Mock process table to show no running evaluator
    monkeypatch.setattr(
        service,
        "get_continuous_evaluator_process_status",
        lambda: {"is_running": False, "pid": None},
    )

    status = service.get_continuous_evaluator_status(state_filepath=stale_eval_file)

    # Must be STOPPED and process_running False
    assert status["status"] == "STOPPED"
    assert status["process_running"] is False
    assert status["is_running"] is False
    assert status["process_pid"] is None
    assert status["pid"] is None
    # Persisted metadata retained for forensic reference
    assert status["persisted_pid"] == 19629


def test_market_count_reconciliation(service):
    """Verify that market counts are completely reconciled:
    All (289) = Populated (1) + Empty (275) + Stale (0) + Invalid/404 (13)."""
    meta = service.get_markets_metadata("live")

    assert meta["total_markets"] == 289
    counts = meta["counts_by_status"]

    assert counts["ALL"] == 289
    assert counts["POPULATED"] == 1
    assert counts["EMPTY"] == 275
    assert counts["STALE"] == 0
    assert counts["INVALID"] == 13

    # Sum of categories must exactly equal ALL
    assert counts["POPULATED"] + counts["EMPTY"] + counts["STALE"] + counts["INVALID"] == counts["ALL"]

    # When collector is stopped, must not claim live stream
    assert meta["is_live_stream"] is False
    assert meta["provenance_label"] == "PERSISTED SNAPSHOT"
    assert meta["latest_observation_at"] is not None


def test_scanner_session_selection_consistency(service):
    """Verify that selecting live_audit_test resolves to exact session metrics,
    and switching to trial_validation_final loads the full-catalog session."""
    # 1. live_audit_test session
    audit_status = service.get_breadth_scanner_status(scanner_dir="live_audit_test")
    assert audit_status["session_id"] == "bscan-d56635ad"
    assert audit_status["total_unique_markets_discovered"] == 50
    assert audit_status["total_unique_markets_screened"] == 50
    assert audit_status["total_strategies_evaluated"] == 120
    assert audit_status["total_preliminary_candidates_generated"] == 1
    assert audit_status["qualified_candidates_count"] == 0
    assert audit_status["api_requests_count"] == 7

    # 2. trial_validation_final session (168k full catalog)
    final_status = service.get_breadth_scanner_status(scanner_dir="trial_validation_final")
    assert final_status["total_unique_markets_discovered"] == 168056
    assert final_status["total_unique_markets_screened"] == 168056
    assert final_status["total_strategies_evaluated"] == 369198
    assert final_status["qualified_candidates_count"] == 1


def test_historical_report_presentation_and_preservation(service):
    """Verify that historical research datasets are preserved and provide both
    concise executive verdicts and complete 10-section reports."""
    hist = service.get_historical_validation("evidence_dashboard_trial")

    assert hist["exists"] is True
    # Executive verdict must be a readable concise sentence
    verdict = hist["section_10_final_verdict"]
    assert isinstance(verdict, str)
    assert "NO LIQUIDITY IN EVIDENCE" in verdict
    assert len(verdict) < 500  # Not a massive 4KB wall of text

    # Full report must be preserved in human_summary
    assert "human_summary" in hist
    assert len(hist["human_summary"]) > 1000
    assert "KALSHI ARBITRAGE ENGINE — V7 HISTORICAL VALIDATION" in hist["human_summary"]


def test_api_markets_metadata_endpoint(workspace_root):
    """Verify that /api/markets serves reconciled count metadata and provenance."""
    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=workspace_root)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        req = urllib.request.Request(f"{base_url}/api/markets?dataset=live")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 289
            assert "counts_by_status" in data
            assert data["counts_by_status"]["ALL"] == 289
            assert data["counts_by_status"]["POPULATED"] == 1
            assert data["counts_by_status"]["EMPTY"] == 275
            assert data["counts_by_status"]["INVALID"] == 13
            assert data["is_live_stream"] is False
            assert data["provenance_label"] == "PERSISTED SNAPSHOT"
    finally:
        server.stop()
