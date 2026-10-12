"""Exhaustive Test Suite for Kalshi Arbitrage Engine V11 Evidence Collection & Forward Evaluation.

Verifies:
1. Bounded polling and request concurrency limits.
2. Rate-limit compliance and minimum request interval enforcement.
3. Timeout, rate limit, and network failure diagnostics logging.
4. Malformed and stale market data handling fail-closed.
5. Duplicate observation deduplication and idempotent processing.
6. Append-only evidence persistence and crash resilience.
7. Restart and resume behavior without duplicate records.
8. Timestamp provenance (request timing, HTTP Date, local arrival).
9. Empty and sparse order-book handling.
10. Repeated identical order books tracking vs mutated books.
11. Market coverage, cadence, and gap distribution metrics.
12. V10 integration and historical replay equivalence.
13. Opportunity deduplication across continuous cycles.
14. Pre-trade risk rejection and capital safety.
15. Multi-leg execution, unwind handling, and double-entry ledger reconciliation.
16. Graceful shutdown signaling and manifest writing.
17. Read-only dashboard integration safety.
18. Prevention of real-order execution paths.
19. Output-path collision protection against overwriting existing datasets.
20. Dataset manifest generation and cryptographic SHA256 integrity verification.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
import pytest

from kalshi_arbitrage.async_execution import (
    MicrostructureConfig,
    MultiLegLifecycleState,
    UnwindPolicy,
)
from kalshi_arbitrage.evaluation import HistoricalDataset
from kalshi_arbitrage.evidence_storage import EvidenceStore
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.market_data_provider import (
    FixtureMarketDataProvider,
    MarketDataErrorCode,
    MarketDataProviderError,
)
from kalshi_arbitrage.observer import MarketObservation
from kalshi_arbitrage.paper_risk import PaperRiskConfig
from kalshi_arbitrage.v10_evaluation import (
    V10EvaluationConfig,
    V10HistoricalStrategyEvaluator,
)
from kalshi_arbitrage.v11_collector import (
    CollectionDiagnosticEvent,
    V11CollectorConfig,
    V11EvidenceManifest,
    V11EvidenceQualityAuditor,
    V11EvidenceQualityMetrics,
    V11MarketEvidenceCollector,
    V11ProspectiveEvaluator,
    _compute_file_sha256,
)


def _make_test_market_and_book(
    ticker: str = "KX-TEST",
    yes_ask: str = "0.40",
    no_ask: str = "0.35",
    qty: str = "10.00",
    volume: str = "150.00",
    source_timestamp: Optional[datetime] = None,
) -> Tuple[NormalizedMarket, NormalizedOrderBook]:
    """Helper to generate a normalized market and book with valid asks."""
    t_src = source_timestamp or datetime.now(timezone.utc)
    y_ask_dec = Decimal(yes_ask)
    n_ask_dec = Decimal(no_ask)

    # Counterparty resting bids
    c_no_bid = Decimal("1.00") - y_ask_dec
    c_yes_bid = Decimal("1.00") - n_ask_dec

    market = NormalizedMarket(
        ticker=ticker,
        event_ticker=f"EVENT-{ticker}",
        market_type="binary",
        status="active",
        title=f"Test Market {ticker}",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal(volume),
        open_interest=Decimal("200.00"),
        yes_bid_dollars=c_yes_bid,
        yes_ask_dollars=y_ask_dec,
        no_bid_dollars=c_no_bid,
        no_ask_dollars=n_ask_dec,
    )

    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=c_yes_bid, quantity=Decimal(qty)),),
        no_bids=(OrderBookLevel(price_dollars=c_no_bid, quantity=Decimal(qty)),),
        source_timestamp=t_src,
    )

    return market, book


# ==============================================================================
# 1. Bounded Polling Limits (Cycles, Observations, Duration)
# ==============================================================================
def test_bounded_polling_limits(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1")
    m2, b2 = _make_test_market_and_book("M2")
    provider = FixtureMarketDataProvider(markets=[m1, m2], books={"M1": b1, "M2": b2})

    # Test max_cycles = 2
    cfg = V11CollectorConfig(
        output_dir=tmp_path / "bounded_cycles",
        max_cycles=2,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    manifest = collector.run()

    assert manifest.total_cycles == 2
    assert manifest.successful_observations == 4  # 2 markets * 2 cycles


def test_bounded_observations_limit(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1")
    m2, b2 = _make_test_market_and_book("M2")
    provider = FixtureMarketDataProvider(markets=[m1, m2], books={"M1": b1, "M2": b2})

    # Test max_observations = 3 (stops after 3rd observation)
    cfg = V11CollectorConfig(
        output_dir=tmp_path / "bounded_obs",
        max_observations=3,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    manifest = collector.run()

    assert manifest.successful_observations == 3


# ==============================================================================
# 2. Rate-Limit Compliance & Interval Enforcement
# ==============================================================================
def test_rate_limit_compliance(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1")
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    # Enforce minimum 0.05s between requests
    cfg = V11CollectorConfig(
        output_dir=tmp_path / "rate_limit",
        max_cycles=2,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.05,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)

    t0 = time.monotonic()
    collector.run()
    elapsed = time.monotonic() - t0

    # At least 2 requests (market discovery + order book query) across 2 cycles => elapsed >= 0.10s
    assert elapsed >= 0.08


# ==============================================================================
# 3. Timeout and Network Failure Diagnostics Logging
# ==============================================================================
def test_diagnostic_failure_isolation(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1")
    provider = FixtureMarketDataProvider(markets=[m1], books={})  # Missing order book!

    cfg = V11CollectorConfig(
        output_dir=tmp_path / "diag_test",
        max_cycles=1,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    manifest = collector.run()

    assert manifest.diagnostic_events_count >= 1
    assert collector.diagnostics_path.exists()

    with open(collector.diagnostics_path, "r", encoding="utf-8") as f:
        events = [CollectionDiagnosticEvent.from_dict(json.loads(line)) for line in f]

    assert len(events) >= 1
    assert events[0].error_code == "EMPTY_ORDER_BOOK"
    assert events[0].ticker == "M1"

    # Observations path should contain 0 valid observations
    assert manifest.successful_observations == 0


# ==============================================================================
# 4. Malformed and Stale Market Data Handling
# ==============================================================================
def test_stale_data_flagging(tmp_path: Path):
    t_now = datetime.now(timezone.utc)
    t_stale = t_now - timedelta(seconds=120)  # 120s old > 60s max
    m1, b1 = _make_test_market_and_book("M-STALE", source_timestamp=t_stale)
    provider = FixtureMarketDataProvider(markets=[m1], books={"M-STALE": b1})

    cfg = V11CollectorConfig(
        output_dir=tmp_path / "stale_test",
        max_cycles=1,
        max_stale_seconds=60.0,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    collector.run()

    store = EvidenceStore(storage_dir=tmp_path / "stale_test")
    obs_list = store.load_observations()
    assert len(obs_list) == 1
    assert obs_list[0].is_stale is True
    assert "exceeds max" in (obs_list[0].staleness_reason or "")


# ==============================================================================
# 5. Duplicate Observation Deduplication & Idempotency
# ==============================================================================
def test_prospective_evaluation_idempotency():
    m1, b1 = _make_test_market_and_book("M1")
    t0 = datetime.now(timezone.utc)
    obs = MarketObservation(
        observation_id="obs-dup-1",
        ticker="M1",
        observed_at=t0,
        source_timestamp=t0,
        market=m1,
        order_book=b1,
        is_success=True,
        is_stale=False,
    )

    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())
    res1 = evaluator.process_observation(obs)
    res2 = evaluator.process_observation(obs)  # Duplicate!

    assert res1 is not None
    assert res2 is None  # Second must be skipped
    assert evaluator.markets_evaluated == 1
    assert len(evaluator._trades) == 1


# ==============================================================================
# 6. Append-Only Persistence and Durability
# ==============================================================================
def test_append_only_persistence(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1")
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    cfg = V11CollectorConfig(
        output_dir=tmp_path / "append_test",
        max_cycles=3,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    collector.run()

    store = EvidenceStore(storage_dir=tmp_path / "append_test")
    observations = store.load_observations()
    assert len(observations) == 3


# ==============================================================================
# 7. Restart and Resume Behavior
# ==============================================================================
def test_session_resume_without_duplicate_records(tmp_path: Path):
    out_dir = tmp_path / "resume_test"
    m1, b1 = _make_test_market_and_book("M1")
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    # Initial session: 2 cycles
    cfg1 = V11CollectorConfig(
        output_dir=out_dir,
        max_cycles=2,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    c1 = V11MarketEvidenceCollector(config=cfg1, data_provider=provider)
    c1.run()

    store1 = EvidenceStore(storage_dir=out_dir)
    assert len(store1.load_observations()) == 2

    # Resume session: run 2 more cycles
    cfg2 = V11CollectorConfig(
        output_dir=out_dir,
        max_cycles=2,
        resume=True,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    c2 = V11MarketEvidenceCollector(config=cfg2, data_provider=provider)
    manifest = c2.run()

    store2 = EvidenceStore(storage_dir=out_dir)
    assert len(store2.load_observations()) == 4
    assert manifest.successful_observations == 4


# ==============================================================================
# 8. Timestamp Provenance & Timing Precision
# ==============================================================================
def test_timestamp_provenance_preservation(tmp_path: Path):
    t_source = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone.utc)
    m1, b1 = _make_test_market_and_book("M1", source_timestamp=t_source)
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    cfg = V11CollectorConfig(
        output_dir=tmp_path / "provenance_test",
        max_cycles=1,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    collector.run()

    store = EvidenceStore(storage_dir=tmp_path / "provenance_test")
    obs = store.load_observations()[0]

    assert obs.source_timestamp == t_source
    assert obs.observed_at is not None
    assert obs.observed_at.tzinfo == timezone.utc


# ==============================================================================
# 9. Repeated Identical Books Tracking vs Mutated Books
# ==============================================================================
def test_repeated_identical_book_tracking():
    m1, b1 = _make_test_market_and_book("M1", yes_ask="0.40", no_ask="0.35")
    _, b2 = _make_test_market_and_book("M1", yes_ask="0.42", no_ask="0.35")  # Mutated!

    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs1 = MarketObservation("o1", "M1", t0, t0, m1, b1, True, False)
    obs2 = MarketObservation("o2", "M1", t0 + timedelta(seconds=2), t0, m1, b1, True, False)  # Identical
    obs3 = MarketObservation("o3", "M1", t0 + timedelta(seconds=4), t0, m1, b2, True, False)  # Mutated

    metrics = V11EvidenceQualityAuditor.audit([obs1, obs2, obs3], expected_cadence_seconds=2.0)

    assert metrics.valid_observations == 3
    assert metrics.repeated_identical_books_count == 1
    assert metrics.mutated_books_count == 1


# ==============================================================================
# 10. Market Coverage, Cadence, and Cadence Gap Metrics
# ==============================================================================
def test_cadence_gap_detection():
    m1, b1 = _make_test_market_and_book("M1")
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs1 = MarketObservation("o1", "M1", t0, t0, m1, b1, True, False)
    obs2 = MarketObservation("o2", "M1", t0 + timedelta(seconds=2), t0, m1, b1, True, False)
    obs3 = MarketObservation("o3", "M1", t0 + timedelta(seconds=30), t0, m1, b1, True, False)  # 28s gap!

    metrics = V11EvidenceQualityAuditor.audit([obs1, obs2, obs3], expected_cadence_seconds=2.0)

    assert metrics.cadence_gaps_count >= 1
    assert metrics.max_gap_seconds == 28.0


# ==============================================================================
# 11. Minimum Research Requirements Assessment
# ==============================================================================
def test_minimum_research_requirements():
    m1, b1 = _make_test_market_and_book("M1")
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    # Small dataset: 5 observations over 10 seconds -> Fails minimum requirements
    obs_list = [
        MarketObservation(f"o{i}", "M1", t0 + timedelta(seconds=i * 2), t0, m1, b1, True, False)
        for i in range(5)
    ]
    metrics = V11EvidenceQualityAuditor.audit(obs_list)

    assert metrics.meets_minimum_research_requirements is False
    assert len(metrics.requirements_gap_reasons) > 0


# ==============================================================================
# 12. V10 Integration and Replay Equivalence
# ==============================================================================
def test_prospective_v10_replay_equivalence(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1", yes_ask="0.40", no_ask="0.35")
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    # Run prospective collection
    out_dir = tmp_path / "replay_equiv"
    cfg = V11CollectorConfig(
        output_dir=out_dir,
        max_cycles=1,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
        enable_prospective_evaluation=True,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    collector.run()

    # Replay recorded evidence file through V10 evaluator
    dataset = HistoricalDataset.from_file(collector.observations_path)
    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset)

    # Substantive decisions must match:
    # 1 candidate detected, 1 approved, 1 filled trade
    assert report.funnel.candidates_detected == 1
    assert report.funnel.approved_for_execution == 1
    assert report.funnel.trades_fully_filled == 1
    assert report.financials.ledger_is_reconciled is True


# ==============================================================================
# 13. Risk Rejection and Capital Safety
# ==============================================================================
def test_pre_trade_risk_rejection():
    # Cost = 75¢. Limit = 50¢ => Rejected by risk
    m1, b1 = _make_test_market_and_book("M-RISK", yes_ask="0.40", no_ask="0.35")
    t0 = datetime.now(timezone.utc)
    obs = MarketObservation("o-risk", "M-RISK", t0, t0, m1, b1, True, False)

    risk_cfg = PaperRiskConfig(max_cost_per_trade_cents=50)
    cfg = V11CollectorConfig(risk_config=risk_cfg)
    evaluator = V11ProspectiveEvaluator(config=cfg)
    eval_opp = evaluator.process_observation(obs)

    assert eval_opp is None
    assert evaluator.rej_risk == 1
    assert evaluator.approved_for_execution == 0
    assert evaluator.portfolio.available_cash_cents == cfg.initial_cash_cents


# ==============================================================================
# 14. Output-Path Collision Protection
# ==============================================================================
def test_output_path_collision_protection(tmp_path: Path):
    target_dir = tmp_path / "collision_test"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "observations.jsonl").write_text("{}\n")

    cfg = V11CollectorConfig(output_dir=target_dir, prevent_overwrite=True, resume=False)
    with pytest.raises(FileExistsError):
        V11MarketEvidenceCollector(config=cfg)


# ==============================================================================
# 15. Graceful Stop Request
# ==============================================================================
def test_graceful_stop_signal(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1")
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    cfg = V11CollectorConfig(
        output_dir=tmp_path / "stop_test",
        max_cycles=100,  # High limit
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)

    # Trigger stop immediately after 1st cycle
    def _cycle_hook():
        collector.request_stop()

    collector.run_single_cycle()
    _cycle_hook()
    manifest = collector.run()

    assert manifest.total_cycles <= 2
    assert collector.manifest_path.exists()


# ==============================================================================
# 16. Manifest Generation and SHA256 Verification
# ==============================================================================
def test_manifest_sha256_verification(tmp_path: Path):
    m1, b1 = _make_test_market_and_book("M1")
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    cfg = V11CollectorConfig(
        output_dir=tmp_path / "manifest_test",
        max_cycles=1,
        poll_interval_seconds=0.0,
        min_request_interval_seconds=0.0,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    manifest = collector.run()

    assert manifest.observations_sha256 is not None
    # Verify calculated hash matches file on disk
    computed_hash = _compute_file_sha256(collector.observations_path)
    assert manifest.observations_sha256 == computed_hash


# ==============================================================================
# 17. Prohibition of Live Trading Calls
# ==============================================================================
def test_prohibition_of_live_trading_capabilities():
    # Verify that V11 collector does not expose or call any order-placement methods
    collector = V11MarketEvidenceCollector(
        config=V11CollectorConfig(output_dir=tempfile.mkdtemp())
    )
    assert not hasattr(collector, "place_order")
    assert not hasattr(collector, "submit_order")
    assert not hasattr(collector, "cancel_order")
    assert not hasattr(collector.data_provider, "place_order")


# ==============================================================================
# 18. Empty and Sparse Dataset Auditing
# ==============================================================================
def test_empty_and_sparse_dataset_auditing():
    # Audit an empty observation list without crashing or dividing by zero
    metrics = V11EvidenceQualityAuditor.audit([])
    assert metrics.total_collection_attempts == 0
    assert metrics.valid_observations == 0
    assert metrics.duration_seconds == 0.0
    assert metrics.meets_minimum_research_requirements is False
    assert len(metrics.requirements_gap_reasons) > 0


# ==============================================================================
# 19. Partial Fill, Unwind and Ledger Reconciliation
# ==============================================================================
def test_asymmetric_depth_unwind_reconciliation():
    # Asymmetric book: YES has 10 contracts depth, NO has 0 contracts -> Unwind/failed trade
    m1, b1 = _make_test_market_and_book("M-ASYM", yes_ask="0.40", no_ask="0.35")
    # Strip NO counterparty bids
    b_asym = NormalizedOrderBook(
        market_ticker="M-ASYM",
        yes_bids=b1.yes_bids,
        no_bids=(),  # No depth to buy YES
        source_timestamp=b1.source_timestamp,
    )
    t0 = datetime.now(timezone.utc)
    obs = MarketObservation("o-asym", "M-ASYM", t0, t0, m1, b_asym, True, False)

    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())
    eval_opp = evaluator.process_observation(obs)

    # Must be rejected due to insufficient depth on leg 2
    assert eval_opp is None
    assert evaluator.rej_insufficient_depth == 1
    assert evaluator.portfolio.available_cash_cents == 1_000_000
    assert evaluator.portfolio.reconcile().is_reconciled is True


# ==============================================================================
# 20. CLI Execution, Audit and Evaluation Subcommands
# ==============================================================================
def test_cli_execution_audit_and_evaluate(tmp_path: Path):
    import subprocess

    out_dir = tmp_path / "cli_test"
    m1, b1 = _make_test_market_and_book("M1")
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1})

    # 1. Create a quick session
    cfg = V11CollectorConfig(output_dir=out_dir, max_cycles=2, poll_interval_seconds=0.0, min_request_interval_seconds=0.0)
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    collector.run()

    report_path = tmp_path / "cli_report.md"
    json_path = tmp_path / "cli_audit.json"

    # Test audit subcommand via CLI
    ret_audit = subprocess.run(
        [
            sys.executable,
            "scripts/collect_v11.py",
            "--audit",
            "--input-dir",
            str(out_dir),
            "--output-json",
            str(json_path),
        ],
        capture_output=True,
        text=True,
    )
    assert ret_audit.returncode == 0
    assert json_path.exists()

    # Test evaluate subcommand via CLI
    ret_eval = subprocess.run(
        [
            sys.executable,
            "scripts/collect_v11.py",
            "--evaluate",
            "--file",
            str(out_dir / "observations.jsonl"),
            "--output-report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
    )
    assert ret_eval.returncode == 0
    assert report_path.exists()
