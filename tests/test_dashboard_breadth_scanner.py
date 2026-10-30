"""Comprehensive unit and integration test suite for Breadth Scanner Dashboard Integration (Phase 2E).

Tests verify:
1. Loading valid breadth scanner state, cycle summaries, candidate history, and qualification history.
2. Distinction between cumulative unique markets and cycle-specific markets.
3. Separation of preliminary candidates from execution-qualified opportunities.
4. Correct labeling of crossed book candidates as preliminary signals (never executable arbitrage).
5. Breakdown of actual recorded rejection reasons.
6. Detection of catalog incompleteness with appropriate warning flags.
7. Graceful degradation on missing files (returns clean STOPPED state).
8. Graceful handling of corrupted state or malformed history lines.
9. Staleness detection (> 60s threshold sets is_stale=True and is_data_fresh=False).
10. Filtering by strategy, qualification outcome, qualification status, and cycle number.
11. Full HTTP API endpoints (/api/scanner/* and /api/breadth/*).
12. Static frontend template verification (HTML, JS, CSS elements and bindings).
13. Strict isolation from legacy collector/evaluator paths.
"""

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import urllib.request
import urllib.error
import pytest

from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.dashboard.server import create_dashboard_server


@pytest.fixture
def temp_scanner_dir(tmp_path):
    """Create a temporary breadth scanner directory with valid test fixtures."""
    scanner_dir = tmp_path / "breadth_scanner"
    scanner_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    t_fresh = now.isoformat()
    t_quote = (now - timedelta(seconds=10)).isoformat()

    # 1. State JSON
    state_data = {
        "session_id": "bscan-test-001",
        "status": "RUNNING",
        "started_at": t_fresh,
        "updated_at": t_fresh,
        "current_cycle": 3,
        "total_cycles_count": 3,
        "successful_cycles_count": 2,
        "failed_cycles_count": 1,
        "last_successful_discovery_time": t_fresh,
        "last_catalog_complete": False,
        "total_unique_markets_discovered": 150,
        "total_eligible_markets": 140,
        "total_unique_markets_screened": 138,
        "total_events_evaluated": 45,
        "total_strategies_evaluated": 180,
        "total_preliminary_candidates_generated": 12,
        "candidates_enqueued": 12,
        "candidates_pending": 1,
        "candidates_deduplicated": 3,
        "qualification_counts": {
            "QUALIFIED": 2,
            "REJECTED_STALE": 4,
            "REJECTED_UNPROFITABLE_FEES": 3,
            "REJECTED_INSUFFICIENT_DEPTH": 2,
            "REJECTED_UNSUPPORTED": 1,
        },
        "qualified_candidates_count": 2,
        "api_requests_count": 15,
        "api_retries_count": 1,
        "api_failures_count": 0,
        "rate_limit_events_count": 0,
        "queue_overflow_count": 0,
        "last_error": None,
        "last_error_time": None,
        "output_dir": str(scanner_dir),
        "state_file": str(scanner_dir / "scanner_state.json"),
        "last_persistence_time": t_fresh,
    }
    with open(scanner_dir / "scanner_state.json", "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2)

    # 2. Cycle Summaries JSONL
    summaries = [
        {
            "cycle_number": 1,
            "session_id": "bscan-test-001",
            "started_at": t_fresh,
            "completed_at": t_fresh,
            "duration_seconds": 1.25,
            "is_catalog_complete": True,
            "markets_discovered": 100,
            "new_markets_count": 100,
            "changed_markets_count": 0,
            "closed_or_settled_markets_count": 0,
            "eligible_markets": 95,
            "markets_screened": 95,
            "events_screened": 30,
            "strategies_evaluated": 120,
            "preliminary_candidates_count": 5,
            "new_preliminary_candidates_count": 5,
            "deduplicated_preliminary_candidates_count": 0,
            "candidates_enqueued": 5,
            "candidates_evaluated": 5,
            "qualified_count": 1,
            "rejected_count": 4,
            "stale_count": 2,
            "error_count": 0,
            "unsupported_count": 0,
            "queue_overflow_count": 0,
            "requests_made": 6,
            "requests_failed": 0,
            "retries_count": 0,
            "rate_limit_events_count": 0,
            "error_message": None,
        },
        {
            "cycle_number": 2,
            "session_id": "bscan-test-001",
            "started_at": t_fresh,
            "completed_at": t_fresh,
            "duration_seconds": 1.45,
            "is_catalog_complete": True,
            "markets_discovered": 120,
            "new_markets_count": 20,
            "changed_markets_count": 15,
            "closed_or_settled_markets_count": 2,
            "eligible_markets": 115,
            "markets_screened": 115,
            "events_screened": 38,
            "strategies_evaluated": 152,
            "preliminary_candidates_count": 4,
            "new_preliminary_candidates_count": 2,
            "deduplicated_preliminary_candidates_count": 2,
            "candidates_enqueued": 2,
            "candidates_evaluated": 2,
            "qualified_count": 1,
            "rejected_count": 1,
            "stale_count": 1,
            "error_count": 0,
            "unsupported_count": 0,
            "queue_overflow_count": 0,
            "requests_made": 4,
            "requests_failed": 0,
            "retries_count": 0,
            "rate_limit_events_count": 0,
            "error_message": None,
        },
        {
            "cycle_number": 3,
            "session_id": "bscan-test-001",
            "started_at": t_fresh,
            "completed_at": t_fresh,
            "duration_seconds": 0.85,
            "is_catalog_complete": False,
            "markets_discovered": 150,
            "new_markets_count": 30,
            "changed_markets_count": 5,
            "closed_or_settled_markets_count": 1,
            "eligible_markets": 140,
            "markets_screened": 138,
            "events_screened": 45,
            "strategies_evaluated": 180,
            "preliminary_candidates_count": 3,
            "new_preliminary_candidates_count": 3,
            "deduplicated_preliminary_candidates_count": 1,
            "candidates_enqueued": 3,
            "candidates_evaluated": 3,
            "qualified_count": 0,
            "rejected_count": 3,
            "stale_count": 1,
            "error_count": 0,
            "unsupported_count": 1,
            "queue_overflow_count": 0,
            "requests_made": 5,
            "requests_failed": 0,
            "retries_count": 1,
            "rate_limit_events_count": 0,
            "error_message": None,
        },
    ]
    with open(scanner_dir / "scan_summaries.jsonl", "w", encoding="utf-8") as f:
        for s in summaries:
            f.write(json.dumps(s) + "\n")

    # 3. Candidate History JSONL (Preliminary signals across strategies)
    cand_records = [
        {
            "recorded_at": t_fresh,
            "cycle_number": 1,
            "candidate": {
                "candidate_id": "PRE-BP:M1",
                "strategy_type": "binary_parity",
                "event_ticker": "EV1",
                "market_tickers": ["M1"],
                "gross_edge_dollars": "0.1000",
                "gross_edge_cents": 10,
                "estimated_entry_cost_dollars": "0.9000",
                "guaranteed_payout_dollars": "1.0000",
                "constituent_quotes": {"M1": {"yes_ask": "0.4500", "no_ask": "0.4500"}},
                "screening_timestamp": t_fresh,
                "quote_timestamp": t_quote,
                "stable_key": "BP:M1",
                "is_execution_qualified": False,
            },
        },
        {
            "recorded_at": t_fresh,
            "cycle_number": 2,
            "candidate": {
                "candidate_id": "PRE-LY:EV2",
                "strategy_type": "mece_long_yes",
                "event_ticker": "EV2",
                "market_tickers": ["M2A", "M2B"],
                "gross_edge_dollars": "0.0800",
                "gross_edge_cents": 8,
                "estimated_entry_cost_dollars": "0.9200",
                "guaranteed_payout_dollars": "1.0000",
                "constituent_quotes": {},
                "screening_timestamp": t_fresh,
                "quote_timestamp": t_quote,
                "stable_key": "LY:EV2",
                "is_execution_qualified": False,
            },
        },
        {
            "recorded_at": t_fresh,
            "cycle_number": 3,
            "candidate": {
                "candidate_id": "PRE-CB:M3",
                "strategy_type": "crossed_book",
                "event_ticker": "EV3",
                "market_tickers": ["M3"],
                "gross_edge_dollars": "0.0500",
                "gross_edge_cents": 5,
                "estimated_entry_cost_dollars": "0.4500",
                "guaranteed_payout_dollars": "0.5000",
                "constituent_quotes": {"M3": {"yes_bid": "0.5000", "yes_ask": "0.4500"}},
                "screening_timestamp": t_fresh,
                "quote_timestamp": t_quote,
                "stable_key": "CB:M3",
                "is_execution_qualified": False,
            },
        },
    ]
    with open(scanner_dir / "candidate_history.jsonl", "w", encoding="utf-8") as f:
        for c in cand_records:
            f.write(json.dumps(c) + "\n")

    # 4. Qualification History JSONL
    qual_records = [
        {
            "recorded_at": t_fresh,
            "cycle_number": 1,
            "qualification": {
                "qualification_id": "tqual-succ-001",
                "candidate_id": "PRE-BP:M1",
                "strategy_type": "binary_parity",
                "stable_key": "BP:M1",
                "evaluated_at": t_fresh,
                "outcome": "QUALIFIED",
                "is_qualified": True,
                "rejection_reason": None,
                "requested_quantity": 5,
                "executable_quantity": 5,
                "gross_edge_cents": 10,
                "net_edge_cents": 6,
                "total_fees_cents": 4,
                "return_on_capital_pct": "6.67",
                "requests_made": 1,
                "requests_failed": 0,
                "preliminary_candidate": cand_records[0]["candidate"],
            },
        },
        {
            "recorded_at": t_fresh,
            "cycle_number": 2,
            "qualification": {
                "qualification_id": "tqual-rej-002",
                "candidate_id": "PRE-LY:EV2",
                "strategy_type": "mece_long_yes",
                "stable_key": "LY:EV2",
                "evaluated_at": t_fresh,
                "outcome": "REJECTED_UNPROFITABLE_FEES",
                "is_qualified": False,
                "rejection_reason": "Order book fees ($0.10) exceed gross edge ($0.08)",
                "requested_quantity": 10,
                "executable_quantity": 0,
                "gross_edge_cents": 8,
                "net_edge_cents": -2,
                "total_fees_cents": 10,
                "return_on_capital_pct": None,
                "requests_made": 2,
                "requests_failed": 0,
                "preliminary_candidate": cand_records[1]["candidate"],
            },
        },
        {
            "recorded_at": t_fresh,
            "cycle_number": 3,
            "qualification": {
                "qualification_id": "tqual-rej-003",
                "candidate_id": "PRE-CB:M3",
                "strategy_type": "crossed_book",
                "stable_key": "CB:M3",
                "evaluated_at": t_fresh,
                "outcome": "REJECTED_UNSUPPORTED",
                "is_qualified": False,
                "rejection_reason": "Crossed book candidates are preliminary signals only and unsupported for execution qualification",
                "requested_quantity": 1,
                "executable_quantity": 0,
                "gross_edge_cents": 5,
                "net_edge_cents": None,
                "total_fees_cents": None,
                "return_on_capital_pct": None,
                "requests_made": 0,
                "requests_failed": 0,
                "preliminary_candidate": cand_records[2]["candidate"],
            },
        },
    ]
    with open(scanner_dir / "qualification_history.jsonl", "w", encoding="utf-8") as f:
        for q in qual_records:
            f.write(json.dumps(q) + "\n")

    return scanner_dir


# ==============================================================================
# Service Unit Tests
# ==============================================================================

def test_load_valid_breadth_scanner_state(temp_scanner_dir):
    """Verify loading valid scanner state from disk."""
    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    status = service.get_breadth_scanner_status(scanner_dir=temp_scanner_dir)

    assert status["session_id"] == "bscan-test-001"
    assert status["current_cycle"] == 3
    assert status["total_cycles_count"] == 3
    assert status["total_unique_markets_discovered"] == 150
    assert status["total_unique_markets_screened"] == 138
    assert status["qualified_candidates_count"] == 2
    assert status["is_stale"] is False
    assert status["last_catalog_complete"] is False
    assert any("partial or incomplete" in w.lower() for w in status["warnings"])


def test_unique_versus_cycle_counters(temp_scanner_dir):
    """Verify cumulative unique counters remain distinct from per-cycle counters."""
    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    status = service.get_breadth_scanner_status(scanner_dir=temp_scanner_dir)
    summaries = service.get_breadth_scanner_summaries(scanner_dir=temp_scanner_dir, limit=5)

    # Cumulative counters
    assert status["total_unique_markets_discovered"] == 150
    assert status["total_unique_markets_screened"] == 138

    # Cycle 3 summary
    latest_cycle = summaries[0]  # Newest first
    assert latest_cycle["cycle_number"] == 3
    assert latest_cycle["markets_discovered"] == 150
    assert latest_cycle["new_markets_count"] == 30  # New in this cycle
    assert latest_cycle["markets_screened"] == 138


def test_preliminary_versus_qualified_separation(temp_scanner_dir):
    """Verify preliminary candidates are never reported as execution qualified."""
    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    candidates = service.get_breadth_scanner_candidates(scanner_dir=temp_scanner_dir)

    for cand in candidates:
        assert cand.get("is_execution_qualified") is False

    qualifications = service.get_breadth_scanner_qualifications(scanner_dir=temp_scanner_dir)
    qualified = [q for q in qualifications if q["is_qualified"]]
    rejected = [q for q in qualifications if not q["is_qualified"]]

    assert len(qualified) == 1
    assert len(rejected) == 2
    assert qualified[0]["outcome"] == "QUALIFIED"
    assert qualified[0]["net_edge_cents"] == 6


def test_crossed_book_labeled_preliminary_signal_only(temp_scanner_dir):
    """Verify crossed book strategy is explicitly marked as preliminary signal only."""
    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    cb_cands = service.get_breadth_scanner_candidates(
        scanner_dir=temp_scanner_dir, strategy="crossed_book"
    )
    assert len(cb_cands) == 1
    assert cb_cands[0]["is_crossed_book_signal"] is True
    assert cb_cands[0]["is_execution_qualified"] is False

    cb_quals = service.get_breadth_scanner_qualifications(
        scanner_dir=temp_scanner_dir, strategy="crossed_book"
    )
    assert len(cb_quals) == 1
    assert cb_quals[0]["is_qualified"] is False
    assert cb_quals[0]["outcome"] == "REJECTED_UNSUPPORTED"


def test_rejection_reason_breakdown(temp_scanner_dir):
    """Verify qualification counts correctly expose actual recorded rejection reasons."""
    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    status = service.get_breadth_scanner_status(scanner_dir=temp_scanner_dir)

    counts = status["qualification_counts"]
    assert counts.get("REJECTED_STALE") == 4
    assert counts.get("REJECTED_UNPROFITABLE_FEES") == 3
    assert counts.get("REJECTED_INSUFFICIENT_DEPTH") == 2
    assert counts.get("REJECTED_UNSUPPORTED") == 1
    assert counts.get("QUALIFIED") == 2


def test_staleness_detection(temp_scanner_dir):
    """Verify telemetry older than max_stale_seconds sets is_stale=True."""
    state_file = temp_scanner_dir / "scanner_state.json"
    with open(state_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Set updated_at to 120 seconds ago
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    data["updated_at"] = old_time
    data["last_persistence_time"] = old_time
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    status = service.get_breadth_scanner_status(scanner_dir=temp_scanner_dir, max_stale_seconds=60.0)

    assert status["is_stale"] is True
    assert status["is_data_fresh"] is False
    assert status["staleness_seconds"] >= 119.0
    assert any("stale" in w.lower() for w in status["warnings"])


def test_missing_files_handling(tmp_path):
    """Verify clean, crash-free degradation when breadth scanner has not run."""
    empty_dir = tmp_path / "empty_scanner"
    service = DashboardDataService(workspace_root=tmp_path)

    status = service.get_breadth_scanner_status(scanner_dir=empty_dir)
    assert status["status"] == "STOPPED"
    assert status["is_data_fresh"] is False
    assert status["total_cycles_count"] == 0
    assert status["total_unique_markets_discovered"] == 0

    summaries = service.get_breadth_scanner_summaries(scanner_dir=empty_dir)
    assert summaries == []

    candidates = service.get_breadth_scanner_candidates(scanner_dir=empty_dir)
    assert candidates == []

    quals = service.get_breadth_scanner_qualifications(scanner_dir=empty_dir)
    assert quals == []


def test_corrupt_files_handling(temp_scanner_dir):
    """Verify graceful error reporting when scanner_state.json is corrupted."""
    state_file = temp_scanner_dir / "scanner_state.json"
    with open(state_file, "w", encoding="utf-8") as f:
        f.write("{ invalid json corrupted content ...")

    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    status = service.get_breadth_scanner_status(scanner_dir=temp_scanner_dir)

    assert status["status"] == "ERROR"
    assert "error" in status
    assert status["is_stale"] is True
    assert status["is_data_fresh"] is False
    assert len(status["warnings"]) > 0


def test_qualification_and_candidate_filtering(temp_scanner_dir):
    """Verify filtering by strategy, outcome, qualification status, and cycle."""
    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)

    # Filter candidate by strategy
    bp_cands = service.get_breadth_scanner_candidates(
        scanner_dir=temp_scanner_dir, strategy="binary_parity"
    )
    assert len(bp_cands) == 1
    assert bp_cands[0]["candidate_id"] == "PRE-BP:M1"

    # Filter candidate by cycle
    cycle2_cands = service.get_breadth_scanner_candidates(
        scanner_dir=temp_scanner_dir, cycle_number=2
    )
    assert len(cycle2_cands) == 1
    assert cycle2_cands[0]["strategy_type"] == "mece_long_yes"

    # Filter qualification by outcome
    fees_quals = service.get_breadth_scanner_qualifications(
        scanner_dir=temp_scanner_dir, outcome="REJECTED_UNPROFITABLE_FEES"
    )
    assert len(fees_quals) == 1
    assert fees_quals[0]["candidate_id"] == "PRE-LY:EV2"

    # Filter qualification by is_qualified
    qual_only = service.get_breadth_scanner_qualifications(
        scanner_dir=temp_scanner_dir, is_qualified=True
    )
    assert len(qual_only) == 1
    assert qual_only[0]["is_qualified"] is True


def test_system_health_integration(temp_scanner_dir):
    """Verify get_system_health includes breadth_scanner telemetry."""
    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    # Point workspace root so it locates temp_scanner_dir
    health = service.get_system_health()

    assert "breadth_scanner" in health
    assert isinstance(health["breadth_scanner"], dict)


def test_strict_isolation_from_legacy_paths(temp_scanner_dir):
    """Verify breadth scanner queries never write to or alter legacy collector/evaluator paths."""
    legacy_coll = temp_scanner_dir.parent / "data" / "live_collector_state.json"
    legacy_eval = temp_scanner_dir.parent / "data" / "live_evaluator_state.json"

    service = DashboardDataService(workspace_root=temp_scanner_dir.parent)
    service.get_breadth_scanner_status(scanner_dir=temp_scanner_dir)
    service.get_breadth_scanner_summaries(scanner_dir=temp_scanner_dir)
    service.get_breadth_scanner_candidates(scanner_dir=temp_scanner_dir)
    service.get_breadth_scanner_qualifications(scanner_dir=temp_scanner_dir)

    # Legacy files must not have been created or touched
    assert not legacy_coll.exists()
    assert not legacy_eval.exists()


# ==============================================================================
# Server HTTP API Integration Tests
# ==============================================================================

def test_server_breadth_scanner_api_endpoints(temp_scanner_dir):
    """Verify HTTP endpoints for breadth scanner return correct JSON responses."""
    # Create server with workspace_root pointing to parent of temp_scanner_dir
    # Symlink temp_scanner_dir to workspace_root/data/breadth_scanner
    data_dir = temp_scanner_dir.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target_link = data_dir / "breadth_scanner"
    if not target_link.exists():
        target_link.symlink_to(temp_scanner_dir)

    server = create_dashboard_server(
        host="127.0.0.1", port=8105, workspace_root=temp_scanner_dir.parent
    )
    url = server.start(blocking=False)

    try:
        # 1. /api/scanner/status and /api/breadth/status
        for endpoint in ("/api/scanner/status", "/api/breadth/status"):
            with urllib.request.urlopen(f"{url}{endpoint}", timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["session_id"] == "bscan-test-001"
                assert data["total_unique_markets_discovered"] == 150
                assert data["current_cycle"] == 3

        # 2. /api/scanner/summaries and /api/breadth/summaries
        for endpoint in ("/api/scanner/summaries", "/api/breadth/summaries?limit=2"):
            with urllib.request.urlopen(f"{url}{endpoint}", timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert "summaries" in data
                assert len(data["summaries"]) > 0

        # 3. /api/scanner/candidates with strategy filter
        cand_url = f"{url}/api/scanner/candidates?strategy=binary_parity"
        with urllib.request.urlopen(cand_url, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["count"] == 1
            assert data["candidates"][0]["strategy_type"] == "binary_parity"

        # 4. /api/scanner/qualifications with outcome filter
        qual_url = f"{url}/api/scanner/qualifications?outcome=QUALIFIED"
        with urllib.request.urlopen(qual_url, timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["count"] == 1
            assert data["qualifications"][0]["is_qualified"] is True
            assert data["qualifications"][0]["outcome"] == "QUALIFIED"

    finally:
        server.stop()


# ==============================================================================
# Frontend Static Asset Verification
# ==============================================================================

def test_frontend_breadth_scanner_template_elements():
    """Verify that index.html, dashboard.js, and dashboard.css include all Phase 2E elements."""
    static_dir = Path(__file__).resolve().parents[1] / "kalshi_arbitrage" / "dashboard" / "static"

    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    dashboard_js = (static_dir / "dashboard.js").read_text(encoding="utf-8")
    dashboard_css = (static_dir / "dashboard.css").read_text(encoding="utf-8")

    # 1. Navigation Tab & Header Status Pill
    assert 'data-tab="breadth-scanner"' in index_html
    assert 'id="scanner-status-pill"' in index_html
    assert 'id="scanner-status-text"' in index_html

    # 2. Breadth Scanner View Panel & ID
    assert 'id="tab-breadth-scanner"' in index_html

    # 3. Warning Banners
    assert 'id="bscan-stale-banner"' in index_html
    assert 'id="bscan-incomplete-banner"' in index_html
    assert 'id="bscan-error-banner"' in index_html

    # 4. KPI Cards
    assert 'id="bscan-kpi-disc"' in index_html
    assert 'id="bscan-kpi-screen"' in index_html
    assert 'id="bscan-catalog-status"' in index_html
    assert 'id="bscan-health-status"' in index_html
    assert 'id="bscan-kpi-prelim"' in index_html
    assert 'id="bscan-kpi-qual"' in index_html

    # 5. Breakdown Panels
    assert 'id="bscan-strat-bp"' in index_html
    assert 'id="bscan-strat-cb"' in index_html
    assert 'id="bscan-rej-stale"' in index_html
    assert 'id="bscan-rej-fees"' in index_html
    assert 'id="bscan-rej-depth"' in index_html

    # 6. Tables & Filters
    assert 'id="bscan-filter-strat"' in index_html
    assert 'id="bscan-filter-outcome"' in index_html
    assert 'id="bscan-candidates-table"' in index_html
    assert 'id="bscan-cycles-table"' in index_html

    # 7. JavaScript Functions
    assert "loadBreadthScannerData" in dashboard_js
    assert "renderBreadthCandidatesTable" in dashboard_js
    assert "renderBreadthCyclesTable" in dashboard_js
    assert "/api/scanner/status" in dashboard_js
    assert "Crossed Book (Signal Only)" in dashboard_js

    # 8. CSS Classes
    assert ".badge-signal-only" in dashboard_css
    assert ".badge-preliminary-signal" in dashboard_css
    assert ".badge-catalog-complete" in dashboard_css


def test_dashboard_server_configured_scanner_dir(temp_scanner_dir):
    """Verify DashboardServer and DashboardDataService resolve a configured scanner directory."""
    server = create_dashboard_server(
        host="127.0.0.1",
        port=8115,
        workspace_root=temp_scanner_dir.parent,
        scanner_dir=temp_scanner_dir,
    )
    url = server.start(blocking=False)
    try:
        with urllib.request.urlopen(f"{url}/api/scanner/status", timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["session_id"] == "bscan-test-001"
            assert data["total_unique_markets_discovered"] == 150
    finally:
        server.stop()


# ==============================================================================
# Phase 2F-B: Strategy Breakdown, Deduplication, & Candidate UX Tests
# ==============================================================================

def test_strategy_breakdown_smoke_test_schema():
    """Verify strategy breakdown calculation from the actual smoke-test dataset."""
    smoke_dir = Path(__file__).resolve().parents[1] / "data" / "breadth_scanner" / "smoke_test_20261028"
    if not smoke_dir.exists():
        pytest.skip("Smoke-test dataset not found on disk.")

    service = DashboardDataService(
        workspace_root=Path(__file__).resolve().parents[1],
        default_scanner_dir=smoke_dir,
    )

    status = service.get_breadth_scanner_status(scanner_dir=smoke_dir)
    assert status["status"] == "STOPPED"
    assert status["total_unique_markets_discovered"] == 157581
    assert status["total_preliminary_candidates_generated"] == 5

    # Strategy breakdown assertions
    strat_counts = status.get("screening_strategy_counts")
    assert strat_counts is not None
    assert strat_counts["mece_basket_long_yes"] == 3
    assert strat_counts["mece_long_yes"] == 3  # alias
    assert strat_counts["mece_basket_long_no"] == 2
    assert strat_counts["mece_long_no"] == 2  # alias
    assert strat_counts["binary_parity"] == 0
    assert strat_counts["crossed_book"] == 0
    assert strat_counts["total_unique"] == 5

    # Targeted qualification breakdown by strategy
    qual_strat = status.get("qualification_strategy_counts")
    assert qual_strat is not None
    assert qual_strat["mece_basket_long_yes"]["qualified"] == 0
    assert qual_strat["mece_basket_long_yes"]["rejected"] == 3
    assert qual_strat["mece_basket_long_yes"]["outcomes"]["REJECTED_LEG_SKEW"] == 2
    assert qual_strat["mece_basket_long_yes"]["outcomes"]["REJECTED_UNPROFITABLE_FEES"] == 1

    assert qual_strat["mece_basket_long_no"]["qualified"] == 0
    assert qual_strat["mece_basket_long_no"]["rejected"] == 2
    assert qual_strat["mece_basket_long_no"]["outcomes"]["REJECTED_LEG_SKEW"] == 2


def test_strategy_deduplication_across_history_updates(tmp_path):
    """Verify deduplication avoids double-counting when candidates are re-recorded."""
    test_dir = tmp_path / "dedup_test"
    test_dir.mkdir(parents=True, exist_ok=True)

    # State
    state = {
        "session_id": "bscan-dedup-01",
        "status": "STOPPED",
        "total_preliminary_candidates_generated": 2,
    }
    with open(test_dir / "scanner_state.json", "w") as f:
        json.dump(state, f)

    # 4 lines in candidate_history, but only 2 unique candidates
    records = [
        {"cycle_number": 1, "candidate": {"candidate_id": "CAND-1", "strategy_type": "mece_basket_long_yes", "gross_edge_cents": 2}},
        {"cycle_number": 1, "candidate": {"candidate_id": "CAND-2", "strategy_type": "mece_basket_long_no", "gross_edge_cents": 3}},
        # Re-recorded in cycle 2 (e.g. quote snapshot or repeated signal)
        {"cycle_number": 2, "candidate": {"candidate_id": "CAND-1", "strategy_type": "mece_basket_long_yes", "gross_edge_cents": 4}},
        {"cycle_number": 2, "candidate": {"candidate_id": "CAND-2", "strategy_type": "mece_basket_long_no", "gross_edge_cents": 5}},
    ]
    with open(test_dir / "candidate_history.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    service = DashboardDataService(workspace_root=tmp_path)
    breakdown = service.get_breadth_scanner_strategy_breakdown(scanner_dir=test_dir)
    prelim = breakdown["preliminary_by_strategy"]

    assert prelim["total_unique"] == 2
    assert prelim["mece_basket_long_yes"] == 1
    assert prelim["mece_basket_long_no"] == 1
    assert prelim["binary_parity"] == 0


def test_missing_or_unsupported_strategy_records(tmp_path):
    """Verify behavior when candidate records are absent or unsupported."""
    test_dir = tmp_path / "missing_records"
    test_dir.mkdir(parents=True, exist_ok=True)

    # Case A: Candidates generated > 0, but no history file exists
    state_a = {
        "session_id": "bscan-missing-01",
        "status": "STOPPED",
        "total_preliminary_candidates_generated": 5,
    }
    with open(test_dir / "scanner_state.json", "w") as f:
        json.dump(state_a, f)

    service = DashboardDataService(workspace_root=tmp_path)
    status_a = service.get_breadth_scanner_status(scanner_dir=test_dir)
    assert status_a["screening_strategy_counts"] is None  # Signals N/A to the UI

    # Case B: Zero candidates generated
    state_b = {
        "session_id": "bscan-zero-01",
        "status": "STOPPED",
        "total_preliminary_candidates_generated": 0,
    }
    with open(test_dir / "scanner_state.json", "w") as f:
        json.dump(state_b, f)

    status_b = service.get_breadth_scanner_status(scanner_dir=test_dir)
    assert status_b["screening_strategy_counts"] is not None
    assert status_b["screening_strategy_counts"]["total_unique"] == 0
    assert status_b["screening_strategy_counts"]["mece_basket_long_yes"] == 0


def test_strategy_and_outcome_filtering():
    """Verify that candidate and qualification queries support normalized strategy and outcome filters."""
    smoke_dir = Path(__file__).resolve().parents[1] / "data" / "breadth_scanner" / "smoke_test_20261028"
    if not smoke_dir.exists():
        pytest.skip("Smoke-test dataset not found on disk.")

    service = DashboardDataService(workspace_root=Path(__file__).resolve().parents[1])

    # 1. Candidate filtering with normalized names
    c_yes_short = service.get_breadth_scanner_candidates(scanner_dir=smoke_dir, strategy="mece_long_yes")
    c_yes_full = service.get_breadth_scanner_candidates(scanner_dir=smoke_dir, strategy="mece_basket_long_yes")
    assert len(c_yes_short) == 3
    assert len(c_yes_full) == 3

    c_no_short = service.get_breadth_scanner_candidates(scanner_dir=smoke_dir, strategy="mece_long_no")
    c_no_full = service.get_breadth_scanner_candidates(scanner_dir=smoke_dir, strategy="mece_basket_long_no")
    assert len(c_no_short) == 2
    assert len(c_no_full) == 2

    c_bp = service.get_breadth_scanner_candidates(scanner_dir=smoke_dir, strategy="binary_parity")
    assert len(c_bp) == 0

    # 2. Qualification filtering with normalized names and outcomes
    q_skew = service.get_breadth_scanner_qualifications(scanner_dir=smoke_dir, outcome="REJECTED_LEG_SKEW")
    assert len(q_skew) == 4

    q_fees = service.get_breadth_scanner_qualifications(scanner_dir=smoke_dir, outcome="REJECTED_UNPROFITABLE_FEES")
    assert len(q_fees) == 1

    q_qual = service.get_breadth_scanner_qualifications(scanner_dir=smoke_dir, is_qualified=True)
    assert len(q_qual) == 0


def test_frontend_multi_leg_presentation_elements():
    """Verify that static assets include all Phase 2F-B UX improvements and styles."""
    static_dir = Path(__file__).resolve().parents[1] / "kalshi_arbitrage" / "dashboard" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    dashboard_js = (static_dir / "dashboard.js").read_text(encoding="utf-8")
    dashboard_css = (static_dir / "dashboard.css").read_text(encoding="utf-8")

    # CSS classes for multi-leg compact rendering
    assert ".badge-legs-count" in dashboard_css
    assert ".mkt-cell-container" in dashboard_css
    assert ".mkt-event-header" in dashboard_css
    assert ".mkt-sample-preview" in dashboard_css
    assert ".legs-disclosure" in dashboard_css
    assert ".legs-summary" in dashboard_css
    assert ".legs-expanded-box" in dashboard_css

    # HTML subtext spans and filter values
    assert 'id="bscan-strat-ly-sub"' in index_html
    assert 'id="bscan-strat-ln-sub"' in index_html
    assert 'value="mece_basket_long_yes"' in index_html
    assert 'value="REJECTED_LEG_SKEW"' in index_html

    # JS rendering logic
    assert "badge-legs-count" in dashboard_js
    assert "legs-disclosure" in dashboard_js
    assert "screening_strategy_counts" in dashboard_js
    assert "qualification_strategy_counts" in dashboard_js


def test_catalog_completeness_vs_telemetry_freshness():
    """Verify that catalog completeness is clearly distinguished from telemetry freshness."""
    smoke_dir = Path(__file__).resolve().parents[1] / "data" / "breadth_scanner" / "smoke_test_20261028"
    if not smoke_dir.exists():
        pytest.skip("Smoke-test dataset not found on disk.")

    service = DashboardDataService(workspace_root=Path(__file__).resolve().parents[1])
    status = service.get_breadth_scanner_status(scanner_dir=smoke_dir)

    # Stopped status is preserved and explicit
    assert status["status"] == "STOPPED"
    assert status["process_running"] is False

    # Catalog was complete during discovery
    assert status["last_catalog_complete"] is True

    # But telemetry is stale and data is not live
    assert status["is_stale"] is True
    assert status["is_data_fresh"] is False
    assert status["staleness_seconds"] > 60.0

    # Warning list includes both stopped process and stale telemetry warnings
    warnings = status["warnings"]
    assert any("stopped" in w.lower() for w in warnings)
    assert any("stale" in w.lower() for w in warnings)


