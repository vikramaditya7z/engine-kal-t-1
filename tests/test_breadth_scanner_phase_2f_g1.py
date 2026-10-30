"""Phase 2F-G1 deterministic offline tests: Pre-qualification leg gating and candidate prioritization.

Covers:
- Basket with 8 legs is not rejected by the size gate.
- Basket with 9 legs is rejected before qualification.
- Oversized candidate generates zero order-book requests.
- Smaller candidates reach normal qualification path.
- Missing or malformed leg data is handled safely without crashing.
- Higher edge density is prioritized over lower edge density.
- Equal edge densities use deterministic tie-breaking.
- Invalid denominators and malformed edge values cannot crash the queue.
- Bounded queue eviction uses edge density.
- Rejection outcome is correctly persisted to history and telemetry.
- CLI argument validation for --max-candidate-legs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence
import pytest

from kalshi_arbitrage import (
    BreadthArbitrageScreener,
    BreadthScannerConfig,
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    FeeScheduleType,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PreliminaryCandidate,
    QualificationOutcome,
    ScreeningOutcome,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
    TargetedQualificationResult,
)
from kalshi_arbitrage.breadth_scanner import (
    CandidatePriority,
    compute_candidate_edge_density,
    compute_candidate_priority,
    extract_constituent_leg_count,
)
from scripts.run_breadth_scanner import parse_args


class MockOrderBookClient:
    """Mock KalshiRestClient tracking order-book requests offline."""

    def __init__(self, books: Optional[Mapping[str, NormalizedOrderBook]] = None) -> None:
        self.books: Dict[str, NormalizedOrderBook] = dict(books or {})
        self.requested_tickers: List[str] = []
        self.call_count: int = 0

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        self.call_count += 1
        self.requested_tickers.append(ticker)
        if ticker in self.books:
            return self.books[ticker]
        now = datetime.now(timezone.utc)
        return NormalizedOrderBook(
            market_ticker=ticker,
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.45"), quantity=100),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.50"), quantity=100),),
            source_timestamp=now,
        )


def make_test_candidate(
    candidate_id: str = "TEST-CAND",
    strategy_type: str = ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
    event_ticker: str = "EV-TEST",
    market_tickers: Sequence[str] = ("LEG-1", "LEG-2"),
    gross_edge_dollars: str = "0.10",
    quote_timestamp: Optional[str] = None,
) -> PreliminaryCandidate:
    now_iso = quote_timestamp or datetime.now(timezone.utc).isoformat()
    return PreliminaryCandidate(
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        event_ticker=event_ticker,
        market_tickers=tuple(market_tickers),
        gross_edge_dollars=Decimal(gross_edge_dollars),
        gross_edge_cents=int(Decimal(gross_edge_dollars) * 100),
        estimated_entry_cost_dollars=Decimal("0.90"),
        guaranteed_payout_dollars=Decimal("1.00"),
        constituent_quotes={t: {"yes_ask": "0.4500", "no_ask": "0.5000"} for t in market_tickers},
        screening_timestamp=now_iso,
        candidate_rationale="Test candidate",
        validation_limitations=(),
        stable_key=f"{strategy_type}:{':'.join(market_tickers)}",
        is_execution_qualified=False,
        quote_timestamp=now_iso,
    )


# =============================================================================
# 1. Size Gating Tests
# =============================================================================

def test_basket_with_8_legs_not_rejected_by_size_gate():
    """A basket with exactly 8 legs is within default max_candidate_legs=8 and is not rejected."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    tickers = [f"LEG-{i}" for i in range(8)]
    cand = make_test_candidate(market_tickers=tickers, gross_edge_dollars="0.20")

    enqueued = qualifier.enqueue_candidate(cand)
    assert enqueued is True
    assert qualifier.queue_size == 1

    summary = qualifier.process_queue()
    assert summary.candidates_evaluated == 1
    # Candidate reached order-book qualification (fetched 8 order books)
    assert summary.requests_made == 8
    assert client.call_count == 8
    res = summary.results[0]
    assert res.outcome != QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value


def test_basket_with_9_legs_rejected_before_qualification():
    """A basket with 9 legs exceeds default max_candidate_legs=8 and is rejected before qualification."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    tickers = [f"LEG-{i}" for i in range(9)]
    cand = make_test_candidate(market_tickers=tickers, gross_edge_dollars="0.25")

    # Enqueue should intercept and gate the candidate
    enqueued = qualifier.enqueue_candidate(cand)
    assert enqueued is False
    assert qualifier.queue_size == 0  # Never entered request queue

    summary = qualifier.process_queue()
    assert summary.candidates_evaluated == 1
    assert summary.requests_made == 0
    assert client.call_count == 0  # Zero network requests!

    res = summary.results[0]
    assert res.outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert res.is_qualified is False
    assert "9" in (res.rejection_reason or "")
    assert "8" in (res.rejection_reason or "")
    assert summary.outcomes_breakdown.get(QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value) == 1


def test_oversized_candidate_qualify_candidate_direct_call_zero_requests():
    """Direct invocation of qualify_candidate() for an oversized basket rejects with zero requests."""
    client = MockOrderBookClient()
    config = TargetedOrderBookQualifierConfig(max_candidate_legs=5)
    qualifier = TargetedOrderBookQualifier(client=client, config=config)

    tickers = [f"LEG-{i}" for i in range(6)]
    cand = make_test_candidate(market_tickers=tickers)

    res = qualifier.qualify_candidate(cand)
    assert res.outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert res.is_qualified is False
    assert res.requests_made == 0
    assert client.call_count == 0
    assert "6" in (res.rejection_reason or "")
    assert "5" in (res.rejection_reason or "")


def test_smaller_candidates_reach_normal_qualification_path():
    """Existing smaller candidates (binary parity: 1 leg, small MECE: 2 legs) reach normal qualification."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    c1 = make_test_candidate(candidate_id="PARITY-1", strategy_type="binary_parity", market_tickers=("M-1",))
    c2 = make_test_candidate(candidate_id="MECE-2", market_tickers=("M-2A", "M-2B"))

    assert qualifier.enqueue_candidate(c1) is True
    assert qualifier.enqueue_candidate(c2) is True
    assert qualifier.queue_size == 2

    summary = qualifier.process_queue()
    assert summary.candidates_evaluated == 2
    # 1 leg for parity + 2 legs for MECE = 3 requests
    assert summary.requests_made == 3
    assert client.call_count == 3


def test_missing_or_malformed_leg_data_handled_safely():
    """Missing or malformed leg data is handled safely without crashing and rejected."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    # 1. None market_tickers
    c_none = make_test_candidate(candidate_id="C-NONE")
    object.__setattr__(c_none, "market_tickers", None)
    res_none = qualifier.qualify_candidate(c_none)
    assert res_none.outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert "missing" in (res_none.rejection_reason or "").lower()
    assert res_none.requests_made == 0

    # 2. Empty market_tickers
    c_empty = make_test_candidate(candidate_id="C-EMPTY")
    object.__setattr__(c_empty, "market_tickers", ())
    res_empty = qualifier.qualify_candidate(c_empty)
    assert res_empty.outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert "empty" in (res_empty.rejection_reason or "").lower()
    assert res_empty.requests_made == 0

    # 3. Non-string market ticker item
    c_bad_item = make_test_candidate(candidate_id="C-BAD-ITEM")
    object.__setattr__(c_bad_item, "market_tickers", (12345,))
    res_bad = qualifier.qualify_candidate(c_bad_item)
    assert res_bad.outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert "not a valid non-empty string" in (res_bad.rejection_reason or "").lower()
    assert res_bad.requests_made == 0


# =============================================================================
# 2. Prioritization & Edge Density Tests
# =============================================================================

def test_higher_edge_density_prioritized_over_lower_edge_density():
    """Candidate with higher edge density is prioritized even if gross edge is lower."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    # Cand A: 8 legs, gross edge $0.16 -> edge density = 0.16 / 8 = 0.02
    cand_a = make_test_candidate(candidate_id="C-A", market_tickers=[f"A-{i}" for i in range(8)], gross_edge_dollars="0.16")
    # Cand B: 2 legs, gross edge $0.10 -> edge density = 0.10 / 2 = 0.05
    cand_b = make_test_candidate(candidate_id="C-B", market_tickers=["B-0", "B-1"], gross_edge_dollars="0.10")

    # Enqueue Cand A first, then Cand B
    qualifier.enqueue_candidate(cand_a)
    qualifier.enqueue_candidate(cand_b)

    # Queue must prioritize Cand B first because density 0.05 > 0.02
    assert qualifier._queue[0].candidate_id == "C-B"
    assert qualifier._queue[1].candidate_id == "C-A"

    # Enqueue via enqueue_candidates (batch) must produce the exact same order
    qualifier._queue.clear()
    qualifier._queue_keys.clear()
    qualifier.enqueue_candidates([cand_a, cand_b])
    assert qualifier._queue[0].candidate_id == "C-B"
    assert qualifier._queue[1].candidate_id == "C-A"


def test_equal_edge_densities_deterministic_tie_breaking():
    """Equal edge densities with equal gross edge use deterministic tie-breaking."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    # Both have 2 legs and gross edge $0.10 (density 0.05)
    cand_alpha = make_test_candidate(candidate_id="C-ALPHA", market_tickers=["A-0", "A-1"], gross_edge_dollars="0.10")
    cand_beta = make_test_candidate(candidate_id="C-BETA", market_tickers=["B-0", "B-1"], gross_edge_dollars="0.10")

    # Order 1: Beta then Alpha
    qualifier.enqueue_candidate(cand_beta)
    qualifier.enqueue_candidate(cand_alpha)
    order1 = [c.candidate_id for c in qualifier._queue]

    # Order 2: Alpha then Beta
    qualifier._queue.clear()
    qualifier._queue_keys.clear()
    qualifier.enqueue_candidate(cand_alpha)
    qualifier.enqueue_candidate(cand_beta)
    order2 = [c.candidate_id for c in qualifier._queue]

    # Both must be deterministically identical: earlier alphabetical ID first
    assert order1 == ["C-ALPHA", "C-BETA"]
    assert order2 == ["C-ALPHA", "C-BETA"]


def test_equal_edge_densities_higher_gross_edge_preferred():
    """Equal edge densities prefer candidate with higher total gross dollar edge."""
    client = MockOrderBookClient()
    qualifier = TargetedOrderBookQualifier(client=client)

    # Cand A: 2 legs, gross edge $0.10 -> density 0.05
    cand_a = make_test_candidate(candidate_id="C-A", market_tickers=["A-0", "A-1"], gross_edge_dollars="0.10")
    # Cand B: 4 legs, gross edge $0.20 -> density 0.05
    cand_b = make_test_candidate(candidate_id="C-B", market_tickers=["B-0", "B-1", "B-2", "B-3"], gross_edge_dollars="0.20")

    qualifier.enqueue_candidate(cand_a)
    qualifier.enqueue_candidate(cand_b)

    # Density is equal (0.05), so higher gross edge ($0.20 > $0.10) wins
    assert qualifier._queue[0].candidate_id == "C-B"
    assert qualifier._queue[1].candidate_id == "C-A"


def test_invalid_denominators_and_malformed_edges_cannot_crash_queue():
    """Malformed edges or zero-leg candidates evaluate to zero density without crashing."""
    c_zero_legs = make_test_candidate(candidate_id="C-ZERO")
    object.__setattr__(c_zero_legs, "market_tickers", ())
    density_zero = compute_candidate_edge_density(c_zero_legs)
    assert density_zero == Decimal("0")

    c_neg_edge = make_test_candidate(candidate_id="C-NEG")
    object.__setattr__(c_neg_edge, "gross_edge_dollars", Decimal("-0.10"))
    density_neg = compute_candidate_edge_density(c_neg_edge)
    assert density_neg == Decimal("0")

    prio = compute_candidate_priority(c_neg_edge)
    assert prio.edge_density == Decimal("0")
    assert prio.candidate_id == "C-NEG"


def test_bounded_queue_eviction_uses_edge_density():
    """When bounded queue is full, eviction compares edge density, not raw gross edge."""
    client = MockOrderBookClient()
    config = TargetedOrderBookQualifierConfig(max_queue_size=2)
    qualifier = TargetedOrderBookQualifier(client=client, config=config)

    # Candidate 1: 2 legs, gross $0.10 (density = 0.05)
    c1 = make_test_candidate(candidate_id="C1", market_tickers=["M1", "M2"], gross_edge_dollars="0.10")
    # Candidate 2: 4 legs, gross $0.12 (density = 0.03)
    c2 = make_test_candidate(candidate_id="C2", market_tickers=["M3", "M4", "M5", "M6"], gross_edge_dollars="0.12")

    assert qualifier.enqueue_candidate(c1) is True
    assert qualifier.enqueue_candidate(c2) is True
    assert qualifier.queue_size == 2
    assert qualifier.queue_overflow_count == 0

    # Candidate 3 arrives: 1 leg, gross $0.06 (density = 0.06)
    # Notice: C3 has lower gross edge ($0.06 < $0.12), BUT higher edge density (0.06 > 0.03)!
    c3 = make_test_candidate(candidate_id="C3", market_tickers=["M7"], gross_edge_dollars="0.06")
    assert qualifier.enqueue_candidate(c3) is True
    assert qualifier.queue_size == 2
    assert qualifier.queue_overflow_count == 1

    # Remaining in queue must be C3 (0.06) and C1 (0.05). C2 (0.03) must be evicted!
    queue_ids = [c.candidate_id for c in qualifier._queue]
    assert queue_ids == ["C3", "C1"]


# =============================================================================
# 3. Persistence & Telemetry Integration Tests
# =============================================================================

def test_rejection_outcome_persisted_to_history_and_telemetry(tmp_path: Path):
    """REJECTED_BASKET_TOO_LARGE is persisted to qualification_history.jsonl and scanner_state.json."""
    output_dir = tmp_path / "breadth_scanner_test"
    output_dir.mkdir(parents=True, exist_ok=True)

    config = BreadthScannerRunnerConfig(
        max_candidate_legs=5,
        output_dir=output_dir,
        single_pass=True,
    )
    runner = BreadthScannerRunner(config=config)

    # Fabricate an oversized candidate (10 legs) in screener output
    ten_legs = [f"T-{i}" for i in range(10)]
    cand = make_test_candidate(candidate_id="C-OVERSIZE", market_tickers=ten_legs)

    # Inject into runner candidate queue
    enqueued = runner._qualifier.enqueue_candidate(cand)
    assert enqueued is False  # Leg gate intercepted

    # Run qualification pass via process_queue
    summary = runner._qualifier.process_queue()
    assert summary.candidates_evaluated == 1
    assert summary.requests_made == 0

    # Persist state
    runner._persist_state(new_qual_results=summary.results)

    # 1. Verify qualification_history.jsonl
    qual_file = output_dir / "qualification_history.jsonl"
    assert qual_file.exists()
    lines = qual_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    qual = record["qualification"]
    assert qual["outcome"] == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert qual["is_qualified"] is False
    assert "10" in qual["rejection_reason"]
    assert "5" in qual["rejection_reason"]
    assert qual["requests_made"] == 0

    # 2. Update health counts and verify scanner_state.json
    for r in summary.results:
        runner._health.qualification_counts[r.outcome] = runner._health.qualification_counts.get(r.outcome, 0) + 1
    runner._persist_state()

    state_file = output_dir / "scanner_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["qualification_counts"].get(QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value) == 1


# =============================================================================
# 4. CLI Argument Validation Tests
# =============================================================================

def test_cli_argument_max_candidate_legs():
    """CLI properly parses --max-candidate-legs and rejects non-positive values."""
    args = parse_args(["--max-candidate-legs", "12"])
    assert args.max_candidate_legs == 12

    # Default value is 8
    args_def = parse_args([])
    assert args_def.max_candidate_legs == 8

    # Rejects 0 or negative
    with pytest.raises(SystemExit):
        parse_args(["--max-candidate-legs", "0"])

    with pytest.raises(SystemExit):
        parse_args(["--max-candidate-legs", "-5"])
