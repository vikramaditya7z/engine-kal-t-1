"""Tests for HistoricalOpportunityTracker and Offline Replay Analytics Engine."""

from decimal import Decimal
import json
from pathlib import Path
import pytest

from kalshi_arbitrage.historical_opportunity_tracker import (
    HistoricalAnalysisReport,
    HistoricalOpportunityTracker,
    OpportunityTemporalRecord,
)


def test_tracker_empty_or_nonexistent_dir(tmp_path: Path):
    tracker = HistoricalOpportunityTracker(tmp_path)
    report = tracker.analyze()

    assert isinstance(report, HistoricalAnalysisReport)
    assert report.total_cycles == 0
    assert report.total_preliminary_candidates == 0
    assert report.unique_candidates_count == 0
    assert report.total_qualified_candidates == 0
    assert report.rejection_breakdown == {}
    assert report.temporal_opportunities == []
    assert report.average_opportunity_duration_seconds == 0.0


def test_tracker_synthetic_lifecycle(tmp_path: Path):
    # Setup state
    state = {
        "session_id": "test-session-123",
        "total_cycles_count": 3,
        "total_unique_markets_screened": 500,
        "total_events_evaluated": 50,
        "total_strategies_evaluated": 1200,
    }
    (tmp_path / "scanner_state.json").write_text(json.dumps(state), encoding="utf-8")

    # Setup summaries
    summaries = [
        {"cycle_number": 1, "markets_screened": 500, "events_screened": 50, "strategies_evaluated": 400},
        {"cycle_number": 2, "markets_screened": 500, "events_screened": 50, "strategies_evaluated": 400},
        {"cycle_number": 3, "markets_screened": 500, "events_screened": 50, "strategies_evaluated": 400},
    ]
    with open(tmp_path / "scan_summaries.jsonl", "w", encoding="utf-8") as f:
        for s in summaries:
            f.write(json.dumps(s) + "\n")

    # Setup candidates:
    # Candidate A: appears in cycle 1 at 12:00:00 and cycle 2 at 12:05:00 (duration 300s)
    # Candidate B: appears in cycle 2 only at 12:05:00 (duration 0s)
    candidates = [
        {
            "cycle_number": 1,
            "recorded_at": "2026-10-30T12:00:00+00:00",
            "candidate": {
                "stable_key": "cand-A",
                "strategy_type": "binary_complement",
                "event_ticker": "KXFED-26OCT",
                "market_tickers": ["TKR-1", "TKR-2"],
                "gross_edge_dollars": "0.10",
            },
        },
        {
            "cycle_number": 2,
            "recorded_at": "2026-10-30T12:05:00+00:00",
            "candidate": {
                "stable_key": "cand-A",
                "strategy_type": "binary_complement",
                "event_ticker": "KXFED-26OCT",
                "market_tickers": ["TKR-1", "TKR-2"],
                "gross_edge_dollars": "0.10",
            },
        },
        {
            "cycle_number": 2,
            "recorded_at": "2026-10-30T12:05:00+00:00",
            "candidate": {
                "stable_key": "cand-B",
                "strategy_type": "mece_basket_long_yes",
                "event_ticker": "KXNFL-26OCT",
                "market_tickers": ["NFL-1", "NFL-2", "NFL-3"],
                "gross_edge_dollars": "0.25",
            },
        },
    ]
    with open(tmp_path / "candidate_history.jsonl", "w", encoding="utf-8") as f:
        for c in candidates:
            f.write(json.dumps(c) + "\n")

    # Setup qualifications:
    # Candidate A is qualified in cycle 1
    # Candidate B is rejected (negative net edge)
    quals = [
        {
            "qualification": {
                "stable_key": "cand-A",
                "outcome": "QUALIFIED",
                "is_qualified": True,
                "net_edge_cents": 8,
                "total_fees_cents": 2,
            }
        },
        {
            "qualification": {
                "stable_key": "cand-B",
                "outcome": "REJECTED_NEGATIVE_NET_EDGE",
                "is_qualified": False,
                "rejection_reason": "Taker fees exceed gross edge",
            }
        },
    ]
    with open(tmp_path / "qualification_history.jsonl", "w", encoding="utf-8") as f:
        for q in quals:
            f.write(json.dumps(q) + "\n")

    tracker = HistoricalOpportunityTracker(tmp_path)
    report = tracker.analyze()

    assert report.session_id == "test-session-123"
    assert report.total_cycles == 3
    assert report.total_markets_screened == 500
    assert report.total_strategies_evaluated == 1200
    assert report.total_preliminary_candidates == 3
    assert report.unique_candidates_count == 2
    assert report.total_qualified_candidates == 1
    assert report.overall_qualification_rate_pct == 50.0

    assert report.rejection_breakdown == {"QUALIFIED": 1, "REJECTED_NEGATIVE_NET_EDGE": 1}
    assert report.category_concentration == {"KXFED": 1, "KXNFL": 1}

    # Cand A checks
    opp_a = next(o for o in report.temporal_opportunities if o.stable_key == "cand-A")
    assert opp_a.cycles_observed == 2
    assert opp_a.duration_seconds == 300.0
    assert opp_a.is_qualified is True
    assert opp_a.net_edge_cents == 8
    assert opp_a.total_fees_cents == 2

    # Cand B checks
    opp_b = next(o for o in report.temporal_opportunities if o.stable_key == "cand-B")
    assert opp_b.cycles_observed == 1
    assert opp_b.duration_seconds == 0.0
    assert opp_b.is_qualified is False
    assert "Taker fees exceed gross edge" in opp_b.rejection_reasons

    # Check to_dict serialization
    report_dict = report.to_dict()
    assert report_dict["unique_candidates_count"] == 2
    assert report_dict["average_opportunity_duration_seconds"] == 150.0
    assert report_dict["max_opportunity_duration_seconds"] == 300.0


def test_tracker_on_trial_2_dataset():
    trial_path = Path("data/breadth_scanner/trial_short_15min_20261030")
    if not trial_path.exists():
        pytest.skip("Trial 2 dataset not found on disk")

    tracker = HistoricalOpportunityTracker(trial_path)
    report = tracker.analyze()

    assert report.total_cycles == 2
    assert report.total_preliminary_candidates == 10
    assert report.unique_candidates_count == 10
    assert report.total_qualified_candidates == 0
    assert report.rejection_breakdown.get("REJECTED_BASKET_TOO_LARGE") == 2
    assert report.rejection_breakdown.get("REJECTED_STALE") == 8
    assert "KXFEDDECISION" in report.category_concentration
    assert "mece_basket_long_yes" in report.strategy_breakdown
