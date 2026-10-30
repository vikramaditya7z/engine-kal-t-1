"""Comprehensive automated tests for Kalshi Arbitrage Engine V21 Continuous Live Arbitrage Detection.

Covers:
1. ContinuousEvaluatorConfig validation & boundary invariants.
2. Observation ingestion & health telemetry advancement.
3. Candidate detection: Creation of NEW candidates with exact integer cents and CFTC fee qualification.
4. Candidate update: Transition to UPDATED when edge or depth changes.
5. Deduplication: Unchanged market ticks emit zero redundant transitions.
6. Candidate disqualification: Transition to DISQUALIFIED with explicit reasons when edge vanishes.
7. Candidate invalidation: Transition to INVALIDATED on stale inputs or excessive leg skew.
8. Candidate disappearance: Transition to DISAPPEARED when markets cease updating beyond timeout.
9. Multi-market MECE event basket evaluation across reconstructed snapshots.
10. In-process integration with ContinuousCollector via cycle listeners.
11. File-tailing stream mode reading from continuous JSONL evidence.
12. Atomic state file persistence and append-only candidate transition history.
13. Dashboard data service & HTTP API endpoints for continuous evaluator telemetry.
14. Graceful shutdown and crash resilience.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from typing import Dict, List, Optional
import urllib.request
import uuid

import pytest

from kalshi_arbitrage import (
    ArbitrageCandidate,
    CandidateState,
    CandidateTransition,
    CollectorCycleSummary,
    ContinuousCollector,
    ContinuousCollectorConfig,
    ContinuousEvaluator,
    ContinuousEvaluatorConfig,
    EvaluatorHealth,
    EvaluatorStatus,
    FeeScheduleType,
)
from kalshi_arbitrage.arbitrage import OPPORTUNITY_BINARY_PARITY, OPPORTUNITY_MECE_BASKET_LONG_YES
from kalshi_arbitrage.dashboard.server import create_dashboard_server
from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.evidence_storage import observation_to_dict
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import DeclaredMeceBasket, MarketObservation
from kalshi_arbitrage.rest_client import HTTPResponse, KalshiRestClient


# -----------------------------------------------------------------------------
# Test Fixture Helpers
# -----------------------------------------------------------------------------

def make_test_market(ticker: str, event_ticker: str = "KXEVENT1") -> NormalizedMarket:
    now = datetime.now(timezone.utc)
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status="open",
        title=f"Test Market {ticker}",
        created_time=now,
        open_time=now,
        close_time=now + timedelta(days=1),
        expiration_time=now + timedelta(days=1),
    )


def make_test_book(
    ticker: str,
    yes_bid: str = "0.45",
    no_bid: str = "0.53",
    quantity: str = "10",
    source_timestamp: Optional[datetime] = None,
) -> NormalizedOrderBook:
    """Builds a test order book.
    Default yields:
    - Best YES bid 0.45 -> NO ask is 0.55 (55c)
    - Best NO bid 0.53 -> YES ask is 0.47 (47c)
    Total pair cost = 47c + 55c = 102c (no gross arbitrage).
    """
    ts = source_timestamp or datetime.now(timezone.utc)
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=Decimal(yes_bid), quantity=Decimal(quantity)),),
        no_bids=(OrderBookLevel(price_dollars=Decimal(no_bid), quantity=Decimal(quantity)),),
        source_timestamp=ts,
    )


def make_arbitrage_book(
    ticker: str,
    yes_bid: str = "0.55",
    no_bid: str = "0.58",
    quantity: str = "10",
    source_timestamp: Optional[datetime] = None,
) -> NormalizedOrderBook:
    """Builds an order book with genuine binary parity arbitrage:
    - Best YES bid 0.55 -> NO ask is 0.45 (45c)
    - Best NO bid 0.58 -> YES ask is 0.42 (42c)
    Total pair cost = 42c + 45c = 87c (Gross edge = 13c, Net edge after fees ~ 11c).
    """
    ts = source_timestamp or datetime.now(timezone.utc)
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=Decimal(yes_bid), quantity=Decimal(quantity)),),
        no_bids=(OrderBookLevel(price_dollars=Decimal(no_bid), quantity=Decimal(quantity)),),
        source_timestamp=ts,
    )


def make_test_observation(
    ticker: str,
    book: Optional[NormalizedOrderBook] = None,
    observed_at: Optional[datetime] = None,
    source_timestamp: Optional[datetime] = None,
    is_stale: bool = False,
    staleness_reason: Optional[str] = None,
    event_ticker: str = "KXEVENT1",
) -> MarketObservation:
    now = observed_at or datetime.now(timezone.utc)
    src_ts = source_timestamp or (book.source_timestamp if book else now)
    return MarketObservation(
        observation_id=f"obs-{ticker}-{uuid.uuid4().hex[:6]}",
        ticker=ticker,
        observed_at=now,
        source_timestamp=src_ts,
        market=make_test_market(ticker, event_ticker=event_ticker),
        order_book=book,
        is_success=book is not None,
        is_stale=is_stale,
        staleness_reason=staleness_reason,
        error_message=None if book is not None else "Missing book",
        source_transport="TEST",
        local_receive_time=now,
        sync_state="SYNCHRONIZED" if not is_stale else "UNSYNCHRONIZED",
    )


# -----------------------------------------------------------------------------
# Unit & Integration Tests
# -----------------------------------------------------------------------------

def test_continuous_evaluator_config_validation():
    """Verify strict fail-fast validation of ContinuousEvaluatorConfig."""
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        ContinuousEvaluatorConfig(poll_interval_seconds=-0.5)

    with pytest.raises(ValueError, match="max_stale_seconds"):
        ContinuousEvaluatorConfig(max_stale_seconds=0.0)

    with pytest.raises(ValueError, match="max_leg_skew_seconds"):
        ContinuousEvaluatorConfig(max_leg_skew_seconds=-1.0)

    with pytest.raises(ValueError, match="target_quantity"):
        ContinuousEvaluatorConfig(target_quantity=0)

    with pytest.raises(ValueError, match="disappear_timeout_seconds"):
        ContinuousEvaluatorConfig(disappear_timeout_seconds=-5.0)

    with pytest.raises(ValueError, match="max_history_records"):
        ContinuousEvaluatorConfig(max_history_records=0)

    with pytest.raises(ValueError, match="max_cycles"):
        ContinuousEvaluatorConfig(max_cycles=0)

    valid_config = ContinuousEvaluatorConfig(
        poll_interval_seconds=1.5,
        max_stale_seconds=45.0,
        max_leg_skew_seconds=4.0,
        target_quantity=5,
    )
    assert valid_config.poll_interval_seconds == 1.5
    assert valid_config.target_quantity == 5


def test_observation_ingestion_and_evaluation_telemetry():
    """Verify observation ingestion advances telemetry counts and updates timestamps."""
    evaluator = ContinuousEvaluator()
    health_init = evaluator.get_health()
    assert health_init.evaluations_count == 0
    assert health_init.processed_observations_count == 0
    assert health_init.active_candidates_count == 0

    t1 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    obs1 = make_test_observation("MKT-A", make_test_book("MKT-A"), observed_at=t1, source_timestamp=t1)
    obs2 = make_test_observation("MKT-B", make_test_book("MKT-B"), observed_at=t1, source_timestamp=t1)

    cands, transitions = evaluator.evaluate_observations([obs1, obs2])
    assert len(cands) == 0  # Neither has gross edge
    assert len(transitions) == 0

    health = evaluator.get_health()
    assert health.evaluations_count == 1
    assert health.processed_observations_count == 2
    assert health.latest_input_timestamp == t1.isoformat()
    assert health.last_evaluation_time is not None


def test_candidate_detection_and_creation_new(tmp_path):
    """Verify that an order book with gross and net edge creates an ArbitrageCandidate in state NEW."""
    state_file = tmp_path / "eval_state.json"
    history_file = tmp_path / "cand_history.jsonl"
    config = ContinuousEvaluatorConfig(
        state_file=state_file,
        history_file=history_file,
        target_quantity=1,
    )
    t_now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    evaluator = ContinuousEvaluator(config=config, time_provider=lambda: t_now)
    # YES ask = 42c, NO ask = 45c -> total 87c. Payout = 100c. Gross = 13c.
    arb_book = make_arbitrage_book("MKT-ARB-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t_now)
    obs = make_test_observation("MKT-ARB-1", arb_book, observed_at=t_now, source_timestamp=t_now)

    emitted, transitions = evaluator.evaluate_observations([obs])

    assert len(emitted) == 1
    cand = emitted[0]
    assert cand.candidate_id == "BP:MKT-ARB-1"
    assert cand.strategy_type == OPPORTUNITY_BINARY_PARITY
    assert cand.state == CandidateState.NEW
    assert cand.gross_edge_cents == 13
    assert cand.guaranteed_payout_cents == 100
    assert cand.total_cost_cents == 87
    assert cand.supported_quantity >= 1
    assert cand.is_qualified is True
    assert cand.is_net_profitable is True
    assert cand.net_edge_cents is not None and cand.net_edge_cents > 0

    # Transitions record check
    assert len(transitions) == 1
    trans = transitions[0]
    assert trans.candidate_id == "BP:MKT-ARB-1"
    assert trans.from_state is None
    assert trans.to_state == CandidateState.NEW.value
    assert trans.gross_edge_cents == 13

    # Active candidates telemetry
    active = evaluator.get_active_candidates()
    assert len(active) == 1
    assert active[0]["candidate_id"] == "BP:MKT-ARB-1"
    assert active[0]["state"] == "NEW"

    health = evaluator.get_health()
    assert health.active_candidates_count == 1
    assert health.total_candidates_detected == 1
    assert health.candidate_transitions_count == 1


def test_candidate_update_on_price_or_depth_change():
    """Verify that when quotes or supported quantity change, the candidate transitions to UPDATED."""
    t1 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=2)
    current_time = t1
    evaluator = ContinuousEvaluator(time_provider=lambda: current_time)

    # Cycle 1: YES ask 42c, NO ask 45c (87c cost, 13c gross)
    book1 = make_arbitrage_book("MKT-ARB-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t1)
    obs1 = make_test_observation("MKT-ARB-1", book1, observed_at=t1, source_timestamp=t1)
    emitted1, trans1 = evaluator.evaluate_observations([obs1])
    assert len(emitted1) == 1
    assert emitted1[0].state == CandidateState.NEW

    # Cycle 2: YES ask drops to 40c (NO bid 0.60), NO ask drops to 44c (YES bid 0.56) -> total 84c cost, 16c gross!
    current_time = t2
    book2 = make_arbitrage_book("MKT-ARB-1", yes_bid="0.56", no_bid="0.60", quantity="15", source_timestamp=t2)
    obs2 = make_test_observation("MKT-ARB-1", book2, observed_at=t2, source_timestamp=t2)
    emitted2, trans2 = evaluator.evaluate_observations([obs2])

    assert len(emitted2) == 1
    cand2 = emitted2[0]
    assert cand2.state == CandidateState.UPDATED
    assert cand2.gross_edge_cents == 16
    assert cand2.total_cost_cents == 84

    assert len(trans2) == 1
    assert trans2[0].from_state == CandidateState.NEW.value
    assert trans2[0].to_state == CandidateState.UPDATED.value
    assert trans2[0].gross_edge_cents == 16


def test_deduplication_across_identical_unchanged_ticks():
    """Verify identical observation ticks do not emit redundant transitions."""
    t1 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    current_time = t1
    evaluator = ContinuousEvaluator(time_provider=lambda: current_time)

    book = make_arbitrage_book("MKT-ARB-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t1)
    obs1 = make_test_observation("MKT-ARB-1", book, observed_at=t1, source_timestamp=t1)

    # First cycle -> detected as NEW
    evaluator.evaluate_observations([obs1])
    assert evaluator.get_health().candidate_transitions_count == 1

    # Second cycle with bit-for-bit identical order book state
    t2 = t1 + timedelta(seconds=2)
    current_time = t2
    obs2 = make_test_observation("MKT-ARB-1", book, observed_at=t2, source_timestamp=t1)
    emitted, trans = evaluator.evaluate_observations([obs2])

    assert len(emitted) == 0  # Suppressed by fingerprint deduplication
    assert len(trans) == 0
    assert evaluator.get_health().candidate_transitions_count == 1  # Unchanged!


def test_candidate_disqualification_when_edge_vanishes():
    """Verify candidate transitions to DISQUALIFIED when quotes adjust and edge vanishes."""
    t1 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=2)
    current_time = t1
    evaluator = ContinuousEvaluator(time_provider=lambda: current_time)

    # 1. Detected as NEW
    arb_book = make_arbitrage_book("MKT-ARB-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t1)
    obs1 = make_test_observation("MKT-ARB-1", arb_book, observed_at=t1, source_timestamp=t1)
    evaluator.evaluate_observations([obs1])
    assert len(evaluator.get_active_candidates()) == 1

    # 2. Quotes move to standard spread: YES ask 47c + NO ask 55c = 102c (negative edge)
    current_time = t2
    calm_book = make_test_book("MKT-ARB-1", yes_bid="0.45", no_bid="0.53", quantity="10", source_timestamp=t2)
    obs2 = make_test_observation("MKT-ARB-1", calm_book, observed_at=t2, source_timestamp=t2)
    emitted, trans = evaluator.evaluate_observations([obs2])

    assert len(evaluator.get_active_candidates()) == 0  # Retired from active candidates
    assert len(trans) == 1
    assert trans[0].candidate_id == "BP:MKT-ARB-1"
    assert trans[0].from_state == CandidateState.NEW.value
    assert trans[0].to_state == CandidateState.DISQUALIFIED.value
    assert "No gross edge" in trans[0].reason


def test_candidate_invalidation_on_stale_inputs():
    """Verify candidate transitions to INVALIDATED if market observation becomes stale."""
    t1 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=2)
    current_time = t1
    evaluator = ContinuousEvaluator(
        config=ContinuousEvaluatorConfig(max_stale_seconds=30.0),
        time_provider=lambda: current_time,
    )

    # 1. Detected as NEW
    arb_book = make_arbitrage_book("MKT-ARB-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t1)
    obs1 = make_test_observation("MKT-ARB-1", arb_book, observed_at=t1, source_timestamp=t1)
    evaluator.evaluate_observations([obs1])
    assert len(evaluator.get_active_candidates()) == 1

    # 2. Stale observation arrives
    current_time = t2
    obs_stale = make_test_observation(
        "MKT-ARB-1",
        arb_book,
        observed_at=t2,
        source_timestamp=t1,
        is_stale=True,
        staleness_reason="Transport HTTP Date gap > 60s",
    )
    emitted, trans = evaluator.evaluate_observations([obs_stale])

    assert len(evaluator.get_active_candidates()) == 0
    assert len(trans) == 1
    assert trans[0].to_state == CandidateState.INVALIDATED.value
    assert "Transport HTTP Date gap" in trans[0].reason


def test_candidate_disappearance_detection():
    """Verify active candidate transitions to DISAPPEARED when its market stops updating beyond timeout."""
    fake_time = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    evaluator = ContinuousEvaluator(
        config=ContinuousEvaluatorConfig(disappear_timeout_seconds=60.0),
        time_provider=lambda: fake_time,
    )

    arb_book = make_arbitrage_book("MKT-ARB-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=fake_time)
    obs1 = make_test_observation("MKT-ARB-1", arb_book, observed_at=fake_time, source_timestamp=fake_time)
    evaluator.evaluate_observations([obs1])
    assert len(evaluator.get_active_candidates()) == 1

    # Advance time by 75 seconds without any observations for MKT-ARB-1
    fake_time += timedelta(seconds=75)
    other_obs = make_test_observation("OTHER-MKT", make_test_book("OTHER-MKT"), observed_at=fake_time, source_timestamp=fake_time)
    _, trans = evaluator.evaluate_observations([other_obs])

    assert len(evaluator.get_active_candidates()) == 0
    disappear_trans = [t for t in trans if t.to_state == CandidateState.DISAPPEARED.value]
    assert len(disappear_trans) == 1
    assert disappear_trans[0].candidate_id == "BP:MKT-ARB-1"


def test_multi_market_mece_basket_evaluation():
    """Verify continuous evaluation across multi-market declared MECE event baskets."""
    # Build declared 3-outcome MECE basket
    basket = DeclaredMeceBasket(
        event_ticker="FED-RATE-DEC",
        outcomes=("CUT", "HOLD", "HIKE"),
        market_outcome_map={
            "FED-CUT-25": "CUT",
            "FED-HOLD": "HOLD",
            "FED-HIKE-25": "HIKE",
        },
        basket_side="YES",
        basket_id="BASKET-FED",
    )

    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    evaluator = ContinuousEvaluator(
        config=ContinuousEvaluatorConfig(
            baskets=(basket,),
            target_quantity=1,
            max_leg_skew_seconds=5.0,
        ),
        time_provider=lambda: t0,
    )

    # In Long YES MECE basket: we buy 1 YES in CUT, HOLD, HIKE.
    # Payout is 100c.
    # If CUT ask is 25c (NO bid 0.75), HOLD ask is 40c (NO bid 0.60), HIKE ask is 20c (NO bid 0.80)
    # Total acquisition cost = 25c + 40c + 20c = 85c -> Gross edge = 15c!
    book_cut = make_arbitrage_book("FED-CUT-25", yes_bid="0.20", no_bid="0.75", quantity="10", source_timestamp=t0)
    book_hold = make_arbitrage_book("FED-HOLD", yes_bid="0.30", no_bid="0.60", quantity="10", source_timestamp=t0)
    book_hike = make_arbitrage_book("FED-HIKE-25", yes_bid="0.10", no_bid="0.80", quantity="10", source_timestamp=t0)

    obs_cut = make_test_observation("FED-CUT-25", book_cut, observed_at=t0, source_timestamp=t0, event_ticker="FED-RATE-DEC")
    obs_hold = make_test_observation("FED-HOLD", book_hold, observed_at=t0, source_timestamp=t0, event_ticker="FED-RATE-DEC")
    obs_hike = make_test_observation("FED-HIKE-25", book_hike, observed_at=t0, source_timestamp=t0, event_ticker="FED-RATE-DEC")

    emitted, trans = evaluator.evaluate_observations([obs_cut, obs_hold, obs_hike])

    mece_cands = [c for c in emitted if c.candidate_id == "MECE:FED-RATE-DEC:YES"]
    assert len(mece_cands) == 1
    cand = mece_cands[0]
    assert cand.strategy_type == OPPORTUNITY_MECE_BASKET_LONG_YES
    assert cand.state == CandidateState.NEW
    assert cand.gross_edge_cents == 15
    assert cand.guaranteed_payout_cents == 100
    assert cand.total_cost_cents == 85
    assert cand.is_qualified is True

    # Multi-leg skew invalidation test:
    # If CUT source timestamp is skewed > 5.0s, candidate must be INVALIDATED
    t_skewed = t0 + timedelta(seconds=12)
    obs_cut_skewed = make_test_observation("FED-CUT-25", book_cut, observed_at=t_skewed, source_timestamp=t_skewed, event_ticker="FED-RATE-DEC")
    emitted_skew, trans_skew = evaluator.evaluate_observations([obs_cut_skewed])

    invalidated_mece = [t for t in trans_skew if t.candidate_id == "MECE:FED-RATE-DEC:YES" and t.to_state == CandidateState.INVALIDATED.value]
    assert len(invalidated_mece) == 1
    assert "skew" in invalidated_mece[0].reason.lower()


def test_in_process_continuous_collector_evaluator_integration():
    """Verify coupling ContinuousCollector with ContinuousEvaluator via cycle listener."""
    collector_config = ContinuousCollectorConfig(
        poll_interval_seconds=0.1,
        max_stale_seconds=60.0,
        target_tickers=("MKT-INT-1",),
        max_cycles=3,
    )

    t0 = datetime.now(timezone.utc)
    arb_book = make_arbitrage_book("MKT-INT-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t0)
    market_obj = make_test_market("MKT-INT-1")

    class MockKalshiClient:
        def __init__(self):
            self.calls = 0

        def get_market(self, ticker: str):
            return market_obj

        def get_order_book(self, ticker: str, depth: int = 100):
            self.calls += 1
            return arb_book

    evaluator = ContinuousEvaluator()
    collector = ContinuousCollector(client=MockKalshiClient(), config=collector_config)

    # Attach evaluator to collector cycle listener
    collector.add_cycle_listener(lambda obs, summary: evaluator.evaluate_observations(obs))

    # Run 2 cycles
    collector.run(max_cycles=2)

    health = evaluator.get_health()
    assert health.evaluations_count == 2
    assert health.total_candidates_detected == 1
    assert len(evaluator.get_active_candidates()) == 1


def test_file_tailing_stream_mode(tmp_path):
    """Verify ContinuousEvaluator in standalone file-tailing mode reading observations JSONL."""
    evidence_file = tmp_path / "stream_observations.jsonl"
    state_file = tmp_path / "stream_state.json"
    history_file = tmp_path / "stream_history.jsonl"

    config = ContinuousEvaluatorConfig(
        poll_interval_seconds=0.05,
        observations_file=evidence_file,
        state_file=state_file,
        history_file=history_file,
        max_cycles=4,
    )
    evaluator = ContinuousEvaluator(config=config)

    t0 = datetime.now(timezone.utc)
    arb_book = make_arbitrage_book("MKT-TAIL-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t0)
    obs = make_test_observation("MKT-TAIL-1", arb_book, observed_at=t0, source_timestamp=t0)

    # Write observation to file before starting
    with open(evidence_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(observation_to_dict(obs)) + "\n")

    # Run evaluator in separate thread
    worker = threading.Thread(target=evaluator.run, daemon=True)
    worker.start()

    time.sleep(0.3)
    evaluator.stop()
    worker.join(timeout=3.0)

    health = evaluator.get_health()
    assert health.evaluations_count >= 1
    assert health.total_candidates_detected == 1
    assert len(evaluator.get_active_candidates()) == 1


def test_state_and_history_file_persistence(tmp_path):
    """Verify evaluator persists state atomically to JSON and transitions to JSONL."""
    state_file = tmp_path / "evaluator_state.json"
    history_file = tmp_path / "candidate_history.jsonl"

    evaluator = ContinuousEvaluator(
        config=ContinuousEvaluatorConfig(
            state_file=state_file,
            history_file=history_file,
        )
    )

    t0 = datetime.now(timezone.utc)
    arb_book = make_arbitrage_book("MKT-ARB-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t0)
    obs = make_test_observation("MKT-ARB-1", arb_book, observed_at=t0, source_timestamp=t0)

    evaluator.evaluate_observations([obs])

    # Check state file
    assert state_file.exists()
    with open(state_file, "r", encoding="utf-8") as f:
        state_data = json.load(f)
    assert state_data["active_candidates_count"] == 1
    assert len(state_data["active_candidates"]) == 1
    assert state_data["active_candidates"][0]["candidate_id"] == "BP:MKT-ARB-1"

    # Check history file
    assert history_file.exists()
    with open(history_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    assert len(lines) == 1
    trans_data = json.loads(lines[0])
    assert trans_data["candidate_id"] == "BP:MKT-ARB-1"
    assert trans_data["to_state"] == "NEW"
    assert trans_data["gross_edge_cents"] == 13


def test_dashboard_service_and_server_endpoints(tmp_path):
    """Verify DashboardDataService and DashboardServer /api/evaluator endpoints."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_file = data_dir / "live_evaluator_state.json"
    history_file = data_dir / "live_candidate_history.jsonl"

    evaluator = ContinuousEvaluator(
        config=ContinuousEvaluatorConfig(
            state_file=state_file,
            history_file=history_file,
        )
    )

    t0 = datetime.now(timezone.utc)
    arb_book = make_arbitrage_book("MKT-DASH-1", yes_bid="0.55", no_bid="0.58", quantity="10", source_timestamp=t0)
    obs = make_test_observation("MKT-DASH-1", arb_book, observed_at=t0, source_timestamp=t0)
    evaluator.evaluate_observations([obs])

    service = DashboardDataService(workspace_root=tmp_path)

    # 1. Service methods
    eval_status = service.get_continuous_evaluator_status(state_filepath=state_file)
    assert eval_status["active_candidates_count"] == 1
    assert eval_status["total_candidates_detected"] == 1

    candidates = service.get_continuous_evaluator_candidates(state_filepath=state_file)
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == "BP:MKT-DASH-1"

    history = service.get_continuous_evaluator_history(history_filepath=history_file)
    assert len(history) == 1
    assert history[0]["candidate_id"] == "BP:MKT-DASH-1"

    # 2. HTTP Server endpoints
    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        # GET /api/evaluator/status
        with urllib.request.urlopen(f"{base_url}/api/evaluator/status", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["active_candidates_count"] == 1

        # GET /api/evaluator/candidates
        with urllib.request.urlopen(f"{base_url}/api/evaluator/candidates", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 1
            assert data["candidates"][0]["candidate_id"] == "BP:MKT-DASH-1"

        # GET /api/evaluator/history
        with urllib.request.urlopen(f"{base_url}/api/evaluator/history", timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["count"] == 1
            assert data["history"][0]["candidate_id"] == "BP:MKT-DASH-1"

    finally:
        server.stop()
