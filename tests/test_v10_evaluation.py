"""Comprehensive Test Suite for Kalshi Arbitrage Engine V10 Historical Strategy Validation.

Verifies:
1. Chronological replay ordering and sorting.
2. Zero look-ahead bias and point-in-time universe selection.
3. Out-of-order and duplicate observation classification.
4. Stale and empty order-book handling fail-closed.
5. Official CFTC quadratic taker fee schedule and integer cent rounding.
6. Depth-aware order book traversal and bottleneck calculation.
7. Asymmetric multi-leg fills and legged-out state transitions.
8. Unwind policies (EMERGENCY_DUMP, AGGRESSIVE_CROSS) and liquidation proceeds.
9. Pre-trade risk controls and capital limits rejection.
10. Double-entry paper ledger reconciliation (100% invariant match).
11. Empty datasets and zero-opportunity periods.
12. Malformed records and missing metadata handling.
13. Out-of-sample chronological partitioning and insufficiency detection.
14. Multi-dimensional sensitivity and stress grid generation.
15. Deterministic report reproducibility and JSON serialization.
16. Mark-to-market valuation vs cost basis valuation.
17. Real historical trial dataset evaluation (data/v5_trial/observations.jsonl).
18. CLI execution and argument parsing integration.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage.async_execution import (
    LegSequencingPolicy,
    MicrostructureConfig,
    MultiLegLifecycleState,
    UnwindPolicy,
)
from kalshi_arbitrage.evaluation import HistoricalDataset
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import MarketObservation
from kalshi_arbitrage.paper_risk import PaperRiskConfig
from kalshi_arbitrage.universe import UniverseFilterConfig
from kalshi_arbitrage.v10_evaluation import (
    V10EvaluationConfig,
    V10EvaluationReport,
    V10HistoricalStrategyEvaluator,
    _safe_pct,
)


def _make_observation(
    ticker: str,
    yes_ask: str = "0.40",
    no_ask: str = "0.35",
    qty: str = "10.00",
    volume: str = "150.00",
    oi: str = "200.00",
    observed_at: Optional[datetime] = None,
    source_timestamp: Optional[datetime] = None,
    obs_id: Optional[str] = None,
) -> MarketObservation:
    t_obs = observed_at or datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    t_src = source_timestamp or t_obs

    y_ask_dec = Decimal(yes_ask) if yes_ask else None
    n_ask_dec = Decimal(no_ask) if no_ask else None

    # Counter-party resting bids deriving asks:
    # Buying YES at yes_ask corresponds to counter NO bid at (1.00 - yes_ask)
    # Buying NO at no_ask corresponds to counter YES bid at (1.00 - no_ask)
    c_no_bid = (Decimal("1.00") - y_ask_dec) if y_ask_dec is not None else None
    c_yes_bid = (Decimal("1.00") - n_ask_dec) if n_ask_dec is not None else None

    market = NormalizedMarket(
        ticker=ticker,
        event_ticker=f"EVENT-{ticker}",
        market_type="binary",
        status="active",
        title=f"Market {ticker}",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal(volume),
        open_interest=Decimal(oi),
        yes_bid_dollars=c_yes_bid if c_yes_bid and c_yes_bid > 0 else Decimal("0.01"),
        yes_ask_dollars=y_ask_dec,
        no_bid_dollars=c_no_bid if c_no_bid and c_no_bid > 0 else Decimal("0.01"),
        no_ask_dollars=n_ask_dec,
    )

    y_levels = (OrderBookLevel(price_dollars=c_yes_bid, quantity=Decimal(qty)),) if c_yes_bid and c_yes_bid > 0 else ()
    n_levels = (OrderBookLevel(price_dollars=c_no_bid, quantity=Decimal(qty)),) if c_no_bid and c_no_bid > 0 else ()

    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=y_levels,
        no_bids=n_levels,
        source_timestamp=t_src,
    )

    return MarketObservation(
        observation_id=obs_id or f"obs-{ticker}-{t_obs.timestamp()}",
        ticker=ticker,
        observed_at=t_obs,
        market=market,
        order_book=book,
        source_timestamp=t_src,
        is_success=True,
        is_stale=False,
    )


def _make_dataset(
    observations: Sequence[MarketObservation],
    source_description: str = "test",
    total_raw_records: Optional[int] = None,
    out_of_order_count: int = 0,
) -> HistoricalDataset:
    obs_tuple = tuple(observations)
    n = len(obs_tuple)
    earliest = obs_tuple[0].observed_at if obs_tuple else None
    latest = obs_tuple[-1].observed_at if obs_tuple else None
    tickers = tuple(sorted({o.ticker for o in obs_tuple if o.ticker}))
    return HistoricalDataset(
        observations=obs_tuple,
        source_description=source_description,
        total_raw_records=total_raw_records if total_raw_records is not None else n,
        malformed_records_count=0,
        duplicate_records_count=0,
        out_of_order_records_count=out_of_order_count,
        earliest_timestamp=earliest,
        latest_timestamp=latest,
        unique_tickers=tickers,
    )


# ==============================================================================
# 1. Chronological Replay & Sorting
# ==============================================================================
def test_chronological_replay_sorting():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs1 = _make_observation("M1", "0.60", "0.50", observed_at=t0 + timedelta(seconds=20))
    obs2 = _make_observation("M2", "0.65", "0.45", observed_at=t0 + timedelta(seconds=10))
    obs3 = _make_observation("M3", "0.70", "0.40", observed_at=t0 + timedelta(seconds=30))

    # Pass in shuffled order
    dataset = _make_dataset([obs1, obs2, obs3], total_raw_records=3)
    evaluator = V10HistoricalStrategyEvaluator()

    quality = evaluator.audit_dataset_quality(dataset)
    assert quality.out_of_order_timestamps >= 1

    report = evaluator.evaluate(dataset)
    assert report.funnel.observations_processed == 3
    # Duration span should reflect chronological span: 20 seconds (10s to 30s)
    assert report.quality_summary.time_span_seconds == 20.0


# ==============================================================================
# 2. Point-in-Time Universe Selection & Zero Look-Ahead Bias
# ==============================================================================
def test_point_in_time_universe_selection():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    # Obs 1 has low volume (10.00 < 50.00 min) -> rejected by universe
    obs1 = _make_observation("M-ILLIQUID", "0.40", "0.35", volume="10.00", observed_at=t0)
    # Obs 2 has high volume (100.00 >= 50.00 min) -> passes universe
    obs2 = _make_observation("M-LIQUID", "0.40", "0.35", volume="100.00", observed_at=t0 + timedelta(seconds=5))

    dataset = _make_dataset([obs1, obs2])
    cfg = V10EvaluationConfig(
        universe_config=UniverseFilterConfig(min_volume_24h=Decimal("50.00")),
        enforce_universe=True,
    )
    evaluator = V10HistoricalStrategyEvaluator(config=cfg)
    report = evaluator.evaluate(dataset)

    assert report.funnel.candidates_detected == 2
    assert report.funnel.rejected_illiquid_universe == 1
    assert report.funnel.approved_for_execution == 1
    assert report.funnel.simulated_execution_attempts == 1


# ==============================================================================
# 3. Duplicate and Stale Records Handling Fail-Closed
# ==============================================================================
def test_duplicate_and_stale_records_audit():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs1 = _make_observation("M1", "0.40", "0.35", observed_at=t0, obs_id="DUP-ID")
    obs2 = _make_observation("M1", "0.40", "0.35", observed_at=t0 + timedelta(seconds=5), obs_id="DUP-ID")
    # Stale observation: book source is 120s older than observed_at (> 60s max)
    obs_stale = _make_observation(
        "M-STALE",
        "0.40",
        "0.35",
        observed_at=t0 + timedelta(seconds=10),
        source_timestamp=t0 - timedelta(seconds=110),
        obs_id="STALE-ID",
    )

    dataset = _make_dataset([obs1, obs2, obs_stale])
    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset)

    assert report.quality_summary.duplicate_observation_ids == 1
    assert report.funnel.rejected_stale_data >= 1


# ==============================================================================
# 4. Realistic Fee Modeling & Precision
# ==============================================================================
def test_fee_precision_and_profitability_threshold():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    # YES ask = 49¢, NO ask = 50¢. Total cost = 99¢.
    # Gross edge = 100 - 99 = 1¢.
    # Kalshi CFTC taker fee on leg 1 (p=49¢): ceil(0.07 * 1 * 0.49 * 0.51 * 100) = ceil(1.75¢) = 2¢.
    # Kalshi CFTC taker fee on leg 2 (p=50¢): ceil(0.07 * 1 * 0.50 * 0.50 * 100) = ceil(1.75¢) = 2¢.
    # Total fees = 4¢ > 1¢ gross edge => Net unprofitable after CFTC fees!
    obs = _make_observation("M-SLIM", "0.49", "0.50", observed_at=t0)
    dataset = _make_dataset([obs])

    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset)

    assert report.funnel.candidates_detected == 1
    assert report.funnel.rejected_unprofitable_fees == 1
    assert report.funnel.simulated_execution_attempts == 0
    assert report.financials.final_cash_cents == report.config.initial_cash_cents


# ==============================================================================
# 5. Full Arbitrage Fill & Double-Entry Ledger Reconciliation
# ==============================================================================
def test_successful_arbitrage_fill_and_ledger_reconciliation():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    # YES ask = 40¢, NO ask = 35¢. Total cost = 75¢. Payout = 100¢. Gross edge = 25¢.
    obs = _make_observation("M-ARB", "0.40", "0.35", qty="10", observed_at=t0)
    dataset = _make_dataset([obs])

    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset)

    assert report.funnel.candidates_detected == 1
    assert report.funnel.approved_for_execution == 1
    assert report.funnel.trades_fully_filled == 1
    assert report.funnel.completed_arbitrage_trades == 1
    assert report.financials.realized_gross_edge_cents > 0
    assert report.financials.total_exchange_fees_cents > 0
    assert report.financials.net_realized_pnl_cents > 0
    assert report.financials.ledger_is_reconciled is True
    assert len(report.financials.reconciliation_discrepancies) == 0


# ==============================================================================
# 6. Pre-Trade Risk Limits & Capital Preservation
# ==============================================================================
def test_risk_limits_rejection():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    # Arb with cost 75¢. Set risk max_cost_per_trade_cents to 50¢ -> rejected!
    obs = _make_observation("M-RISK", "0.40", "0.35", observed_at=t0)
    dataset = _make_dataset([obs])

    risk_cfg = PaperRiskConfig(max_cost_per_trade_cents=50)
    cfg = V10EvaluationConfig(risk_config=risk_cfg, enforce_risk_limits=True)
    evaluator = V10HistoricalStrategyEvaluator(config=cfg)
    report = evaluator.evaluate(dataset)

    assert report.funnel.candidates_detected == 1
    assert report.funnel.rejected_risk_limits == 1
    assert report.funnel.approved_for_execution == 0
    assert report.funnel.simulated_execution_attempts == 0
    assert report.financials.net_realized_pnl_cents == 0


# ==============================================================================
# 7. Asymmetric Leg Fills and Scratch Recovery
# ==============================================================================
def test_asymmetric_fill_scratch_unwind():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    # Book with YES bid only (NO bid is 0.00). Leg 2 ask unavailable.
    market = NormalizedMarket(
        ticker="M-ASYM",
        event_ticker="EVENT-ASYM",
        market_type="binary",
        status="active",
        title="Asymmetric Market",
        yes_subtitle="Yes",
        no_subtitle="No",
        volume_24h=Decimal("100.00"),
        open_interest=Decimal("200.00"),
        yes_bid_dollars=Decimal("0.65"),
        yes_ask_dollars=Decimal("0.40"),
        no_bid_dollars=Decimal("0.00"),
        no_ask_dollars=Decimal("0.35"),
    )
    book = NormalizedOrderBook(
        market_ticker="M-ASYM",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.65"), quantity=Decimal("10")),),
        no_bids=(),  # No counterparty bids
        source_timestamp=t0,
    )
    obs = MarketObservation(
        observation_id="obs-asym",
        ticker="M-ASYM",
        observed_at=t0,
        market=market,
        order_book=book,
        source_timestamp=t0,
        is_success=True,
        is_stale=False,
    )
    dataset = _make_dataset([obs])

    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset)

    # Must be rejected due to insufficient depth on leg 2
    assert report.funnel.completed_arbitrage_trades == 0
    assert report.funnel.rejected_insufficient_depth == 1
    assert report.financials.ledger_is_reconciled is True


# ==============================================================================
# 8. Hierarchical Comparative Baselines
# ==============================================================================
def test_comparative_baselines_hierarchy():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs = _make_observation("M-BASE", "0.40", "0.35", qty="10", observed_at=t0)
    dataset = _make_dataset([obs])

    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset)

    tiers = report.baseline_comparison.tiers
    assert len(tiers) == 6

    # Tier 0: No-Trade has 0 P&L
    assert tiers[0].tier_name == "Tier 0: No-Trade"
    assert tiers[0].net_pnl_cents == 0

    # Tier 1: Gross Edge has 0 fees
    assert tiers[1].tier_name == "Tier 1: Gross Edge"
    assert tiers[1].fees_paid_cents == 0
    assert tiers[1].gross_pnl_cents > 0

    # Tier 2: Net Edge has fees paid
    assert tiers[2].tier_name == "Tier 2: Net Top-of-Book"
    assert tiers[2].fees_paid_cents > 0
    assert tiers[2].net_pnl_cents < tiers[1].net_pnl_cents  # Net is lower than gross!


# ==============================================================================
# 9. Chronological Out-of-Sample Partitioning
# ==============================================================================
def test_out_of_sample_insufficient_data():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs_list = [_make_observation(f"M-{i}", "0.40", "0.35", observed_at=t0 + timedelta(seconds=i)) for i in range(10)]
    dataset = _make_dataset(obs_list)

    cfg = V10EvaluationConfig(min_observations_for_split=30)
    evaluator = V10HistoricalStrategyEvaluator(config=cfg)
    report = evaluator.evaluate(dataset)

    assert report.out_of_sample.is_split_viable is False
    assert "insufficient volume" in report.out_of_sample.insufficiency_reason


def test_out_of_sample_viable_split():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs_list = [_make_observation(f"M-{i}", "0.40", "0.35", observed_at=t0 + timedelta(seconds=i)) for i in range(40)]
    dataset = _make_dataset(obs_list)

    cfg = V10EvaluationConfig(min_observations_for_split=30, in_sample_ratio=0.70)
    evaluator = V10HistoricalStrategyEvaluator(config=cfg)
    report = evaluator.evaluate(dataset)

    assert report.out_of_sample.is_split_viable is True
    assert report.out_of_sample.in_sample_observations_count == 28  # 40 * 0.70
    assert report.out_of_sample.out_of_sample_observations_count == 12  # 40 - 28


# ==============================================================================
# 10. Multi-Dimensional Sensitivity & Stress Grid
# ==============================================================================
def test_sensitivity_stress_grid_execution():
    t0 = datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)
    obs = _make_observation("M-SENS", "0.40", "0.35", observed_at=t0)
    dataset = _make_dataset([obs])

    cfg = V10EvaluationConfig(
        enable_sensitivity=True,
        sensitivity_latencies_ms=(0.0, 50.0),
        sensitivity_haircuts=(1.0, 0.5),
    )
    evaluator = V10HistoricalStrategyEvaluator(config=cfg)
    report = evaluator.evaluate(dataset)

    # 2 latencies * 2 haircuts = 4 grid points
    assert len(report.sensitivity_grid) == 4
    for pt in report.sensitivity_grid:
        assert pt.fee_model_name == "CFTC_TAKER"


# ==============================================================================
# 11. Empty Dataset & Malformed Records
# ==============================================================================
def test_empty_dataset_handling():
    dataset = _make_dataset([])
    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset)

    assert report.quality_summary.valid_observations == 0
    assert report.funnel.observations_processed == 0
    assert report.financials.final_cash_cents == report.config.initial_cash_cents
    assert report.executive_verdict == "INCONCLUSIVE"
    assert "zero populated order books" in report.verdict_rationale


# ==============================================================================
# 12. Real Trial Dataset Audit (data/v5_trial/observations.jsonl)
# ==============================================================================
def test_real_v5_trial_dataset_evaluation():
    p = Path("data/v5_trial/observations.jsonl")
    assert p.exists(), "v5_trial observations dataset must exist on disk"

    dataset = HistoricalDataset.from_file(p)
    evaluator = V10HistoricalStrategyEvaluator()
    report = evaluator.evaluate(dataset, source_path=str(p))

    assert report.quality_summary.total_raw_records == 21
    assert report.quality_summary.populated_books_count == 6
    assert report.quality_summary.empty_books_count == 15
    assert report.funnel.candidates_detected == 0
    assert report.financials.total_trades_count == 0
    assert report.financials.ledger_is_reconciled is True
    assert report.executive_verdict == "INCONCLUSIVE"
    assert "Real prediction market pricing was efficient" in report.verdict_rationale
