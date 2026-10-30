"""Phase 2F-G4 Unit Tests: Strict Runtime & Duration Enforcement.

Verifies:
1. Lookahead cycle gate preventing cycle overrun when remaining duration is insufficient.
2. Monotonic deadline propagation halting catalog discovery mid-pass with partial retention.
3. Mid-cycle qualification deadline halting queue processing and marking remaining items SKIPPED_TIMEOUT.
4. Clean final persistence on stop/interruption with stopped_reason, final_elapsed_seconds, and shutdown_cleanly.
5. Max cycles boundary stopping with stopped_reason MAX_CYCLES_REACHED.
"""

from decimal import Decimal
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage.breadth_scanner import (
    BreadthScannerConfig,
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    CatalogDiscoveryEngine,
    CatalogDiscoveryResult,
    PreliminaryCandidate,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
    TargetedQualificationResult,
)
from kalshi_arbitrage.market_data import (
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.rest_client import HTTPResponse, KalshiRestClient


# -----------------------------------------------------------------------------
# Test Fixtures & Mocks
# -----------------------------------------------------------------------------

class G4MockTransport:
    """Mock transport capable of delayed page responses and tracking calls."""

    def __init__(self, pages: Sequence[Any], page_delay_seconds: float = 0.0):
        self.pages: List[Any] = list(pages)
        self.calls: List[Tuple[str, Dict[str, str], float]] = []
        self.page_delay = page_delay_seconds

    def get(self, url: str, params: Mapping[str, str], timeout_seconds: float) -> HTTPResponse:
        self.calls.append((url, dict(params), timeout_seconds))
        if self.page_delay > 0:
            time.sleep(self.page_delay)
        if not self.pages:
            raise RuntimeError(f"No mock page available for {url}")
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        if isinstance(page, HTTPResponse):
            return page
        body = json.dumps(page).encode("utf-8")
        return HTTPResponse(200, body, {"date": "Wed, 28 Oct 2026 12:00:00 GMT"})


def make_raw_market(ticker: str, yes_ask: str = "0.4500", no_ask: str = "0.5000", event_ticker: str = "EV-1") -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "market_type": "binary",
        "status": "open",
        "title": f"Will {ticker} settle yes?",
        "yes_bid_dollars": "0.4000",
        "yes_ask_dollars": str(yes_ask),
        "no_bid_dollars": "0.4500",
        "no_ask_dollars": str(no_ask),
        "close_time": "2026-12-31T00:00:00Z",
    }


def make_raw_event(event_ticker: str, markets: List[Dict[str, Any]], category: str = "Economics") -> Dict[str, Any]:
    return {
        "event_ticker": event_ticker,
        "category": category,
        "markets": markets,
    }


def make_preliminary_candidate(cand_id: str, market_tickers: Tuple[str, ...]) -> PreliminaryCandidate:
    now_iso = datetime.now(timezone.utc).isoformat()
    return PreliminaryCandidate(
        candidate_id=cand_id,
        strategy_type=ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
        event_ticker="EV-TEST",
        market_tickers=market_tickers,
        gross_edge_dollars=Decimal("0.1000"),
        gross_edge_cents=10,
        estimated_entry_cost_dollars=Decimal("0.9000"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={t: {"yes_ask": "0.4500", "no_ask": "0.5000"} for t in market_tickers},
        screening_timestamp=now_iso,
        candidate_rationale="Test candidate",
        validation_limitations=(),
        stable_key=f"test:{cand_id}",
        is_execution_qualified=False,
        quote_timestamp=now_iso,
    )


# -----------------------------------------------------------------------------
# Milestone G4 Unit Tests
# -----------------------------------------------------------------------------

def test_runner_lookahead_gate_prevents_overrun(tmp_path: Path):
    """Runner with finite duration stops before next cycle if remaining time is below threshold."""
    # Create pages for multiple cycles
    pages = []
    for c in range(10):
        ev = make_raw_event(f"EV-{c}", [make_raw_market(f"MKT-{c}-1"), make_raw_market(f"MKT-{c}-2")])
        pages.append({"events": [ev], "cursor": None})

    transport = G4MockTransport(pages)
    client = KalshiRestClient(environment="production", transport=transport)

    # Configure duration = 0.2s, lookahead threshold = 0.15s
    # Cycle 1 takes ~0.02s. Remaining time (~0.18s) will be below lookahead threshold if threshold is 0.25s
    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        duration_seconds=0.20,
        refresh_interval_seconds=0.01,
        enable_lookahead_gate=True,
        lookahead_threshold_seconds=0.15,
        max_requests_per_second=1000.0,
    )

    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: time.sleep(0.01))

    t0 = time.monotonic()
    runner.run()
    elapsed = time.monotonic() - t0

    health = runner.get_health()
    assert health.status == "STOPPED"
    assert health.stopped_reason == "DURATION_EXPIRED"
    assert health.shutdown_cleanly is True
    assert health.final_elapsed_seconds is not None
    assert elapsed <= 0.25  # Strictly enforced bounded run duration
    assert health.current_cycle >= 1


def test_runner_mid_cycle_discovery_deadline_partial_pass():
    """Discovery deadline halts pagination cleanly mid-pass with partial catalog retention."""
    # Create 5 pages of events
    pages = []
    for p in range(1, 6):
        ev = make_raw_event(f"EV-P{p}", [make_raw_market(f"M-P{p}")])
        pages.append({"events": [ev], "cursor": f"c_{p+1}" if p < 5 else None})

    # Add delay of 30ms per page
    transport = G4MockTransport(pages, page_delay_seconds=0.03)
    client = KalshiRestClient(environment="production", transport=transport)
    engine = CatalogDiscoveryEngine(client=client)

    # Set deadline for 50ms from now (should fetch page 1 and 2, but halt before page 3 completes)
    deadline = time.monotonic() + 0.05
    result = engine.discover_catalog(deadline=deadline)

    assert result.is_complete is False
    assert result.error_message == "HALTED_BY_RUN_DURATION_DEADLINE"
    assert result.pages_fetched < 5
    assert result.pages_fetched >= 1
    # Partial markets are retained in discovered_markets
    assert len(result.discovered_markets) >= 1
    # State active_tickers is merged, not wiped out
    assert len(engine.active_tickers) >= 1


def test_runner_mid_cycle_qualification_deadline_skips_queue():
    """Targeted qualification halts processing when deadline fires, marking remaining SKIPPED_TIMEOUT."""
    client = KalshiRestClient(environment="production", transport=G4MockTransport([]))
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(enable_event_prefilter=False),
    )

    # Mock qualify_candidate to simulate taking 30ms per candidate
    def slow_qualify(candidate: PreliminaryCandidate, market: Optional[NormalizedMarket] = None):
        time.sleep(0.03)
        return TargetedQualificationResult(
            qualification_id="tqual-1",
            candidate_id=candidate.candidate_id,
            strategy_type=candidate.strategy_type,
            stable_key=candidate.stable_key,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            outcome="QUALIFIED",
            is_qualified=True,
            rejection_reason=None,
            preliminary_candidate=candidate,
            requested_quantity=1,
            executable_quantity=1,
            gross_edge_cents=10,
            net_edge_cents=8,
            total_fees_cents=2,
            return_on_capital_pct=Decimal("10.0"),
            candidate_leg_count=len(candidate.market_tickers),
        )

    qualifier.qualify_candidate = slow_qualify

    # Enqueue 5 candidates
    for i in range(5):
        cand = make_preliminary_candidate(f"CAND-{i}", ("M1", "M2"))
        qualifier.enqueue_candidate(cand)

    # Deadline 45ms from now: Candidate 0 completes (~30ms), Candidate 1 completes (~60ms, expired),
    # Candidates 2, 3, 4 skipped with SKIPPED_TIMEOUT
    deadline = time.monotonic() + 0.045
    summary = qualifier.process_queue(deadline=deadline)

    assert summary.candidates_enqueued == 5
    assert len(summary.results) == 5

    outcomes = [r.outcome for r in summary.results]
    assert "SKIPPED_TIMEOUT" in outcomes
    # Check that skipped results carry correct rejection reason
    skipped_results = [r for r in summary.results if r.outcome == "SKIPPED_TIMEOUT"]
    for s in skipped_results:
        assert s.is_qualified is False
        assert "Qualification halted by run duration deadline" in s.rejection_reason


def test_runner_clean_persistence_on_user_stop(tmp_path: Path):
    """Cooperative stop persists clean final state file with stopped_reason USER_REQUEST."""
    ev = make_raw_event("EV1", [make_raw_market("M1")])
    transport = G4MockTransport([{"events": [ev], "cursor": None}])
    client = KalshiRestClient(environment="production", transport=transport)

    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        refresh_interval_seconds=10.0,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    # Run in background or single cycle and invoke stop()
    def stop_during_cycle(*args, **kwargs):
        runner.stop()
        return CatalogDiscoveryResult(
            session_id="disc-test",
            started_at=datetime.now(timezone.utc).isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            is_complete=True,
        )

    runner._discovery_engine.discover_catalog = stop_during_cycle

    runner.run()

    health = runner.get_health()
    assert health.status == "STOPPED"
    assert health.stopped_reason == "USER_REQUEST"
    assert health.shutdown_cleanly is True
    assert health.final_elapsed_seconds is not None

    # Verify atomic state file was written and is valid JSON
    state_file = tmp_path / "scanner_state.json"
    assert state_file.exists()
    state_data = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_data["status"] == "STOPPED"
    assert state_data["stopped_reason"] == "USER_REQUEST"
    assert state_data["shutdown_cleanly"] is True


def test_runner_max_cycles_reached_sets_stopped_reason(tmp_path: Path):
    """Reaching max_cycles halts runner and sets stopped_reason MAX_CYCLES_REACHED."""
    pages = []
    for c in range(5):
        ev = make_raw_event(f"EV-{c}", [make_raw_market(f"MKT-{c}")])
        pages.append({"events": [ev], "cursor": None})

    transport = G4MockTransport(pages)
    client = KalshiRestClient(environment="production", transport=transport)

    config = BreadthScannerRunnerConfig(
        output_dir=tmp_path,
        max_cycles=2,
        refresh_interval_seconds=0.01,
        max_requests_per_second=1000.0,
    )
    runner = BreadthScannerRunner(client=client, config=config, sleep_func=lambda d: None)

    runner.run()

    health = runner.get_health()
    assert health.current_cycle == 2
    assert health.status == "STOPPED"
    assert health.stopped_reason == "MAX_CYCLES_REACHED"
    assert health.shutdown_cleanly is True
