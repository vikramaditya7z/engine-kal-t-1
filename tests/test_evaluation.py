"""Tests for V6 Historical Opportunity Evaluation & Paper-Trading Performance."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from kalshi_arbitrage import (
    DeclaredMeceBasket,
    EvaluationConfig,
    EvaluationError,
    HistoricalDataset,
    HistoricalEvaluator,
    MarketObservation,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PaperPortfolio,
    RiskConfig,
    RiskManager,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    TradeState,
    NO,
    YES,
    evaluate_historical_evidence,
    format_http_date,
    run_sensitivity_analysis,
)

_SENTINEL = object()


def make_test_observation(
    ticker: str = "KX-TEST",
    yes_ask: str = "0.40",
    no_ask: str = "0.55",
    yes_qty: str = "10.00",
    no_qty: str = "10.00",
    observed_at: Optional[datetime] = None,
    source_timestamp: Any = _SENTINEL,
    is_stale: bool = False,
    staleness_reason: Optional[str] = None,
    empty_book: bool = False,
) -> MarketObservation:
    """Generate deterministic synthetic market observation for testing."""
    now = observed_at or datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    src_ts = now if source_timestamp is _SENTINEL else source_timestamp

    market = NormalizedMarket(
        ticker=ticker,
        event_ticker="EV-TEST",
        market_type="binary",
        status="open",
        title=f"Test Market {ticker}",
        subtitle=None,
        yes_subtitle="YES",
        no_subtitle="NO",
        rules_primary="Primary rule",
        rules_secondary="Secondary rule",
        expiration_value=None,
        result=None,
        created_time=None,
        updated_time=None,
        open_time=None,
        close_time=None,
        expiration_time=None,
        settlement_ts=None,
        yes_bid_dollars=Decimal("0.45"),
        yes_ask_dollars=Decimal(yes_ask),
        no_bid_dollars=Decimal("0.60"),
        no_ask_dollars=Decimal(no_ask),
        last_price_dollars=Decimal("0.50"),
        settlement_value_dollars=None,
        yes_bid_size=Decimal("10"),
        yes_ask_size=Decimal("10"),
        volume=Decimal("100"),
        volume_24h=Decimal("50"),
        open_interest=Decimal("25"),
        price_ranges=(),
        is_provisional=False,
    )

    if empty_book:
        yes_bids = ()
        no_bids = ()
    else:
        # Counter-party resting bids deriving asks:
        # Buying YES at yes_ask corresponds to counter NO bid at (100 - yes_ask)
        # Buying NO at no_ask corresponds to counter YES bid at (100 - no_ask)
        c_no_bid = Decimal("1.00") - Decimal(yes_ask)
        c_yes_bid = Decimal("1.00") - Decimal(no_ask)
        yes_bids = (OrderBookLevel(price_dollars=c_yes_bid, quantity=Decimal(no_qty)),)
        no_bids = (OrderBookLevel(price_dollars=c_no_bid, quantity=Decimal(yes_qty)),)

    order_book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=yes_bids,
        no_bids=no_bids,
        source_timestamp=src_ts,
    )

    return MarketObservation(
        observation_id=f"obs-{ticker}-{int(now.timestamp())}",
        ticker=ticker,
        observed_at=now,
        source_timestamp=src_ts,
        market=market,
        order_book=order_book,
        is_success=True,
        is_stale=is_stale,
        staleness_reason=staleness_reason,
    )


# ===========================================================================
# 1. Profitable candidate with adequate depth and positive net edge
# ===========================================================================

def test_profitable_candidate_qualification():
    obs = make_test_observation(
        ticker="KX-ARB",
        yes_ask="0.40",
        no_ask="0.55",  # total cost 95¢, gross edge 5¢
    )
    dataset = HistoricalDataset.from_observations([obs])
    config = EvaluationConfig(
        target_quantity=10,
        default_fee_per_contract_cents=1,  # 1¢ * 2 legs = 2¢ fee -> net edge 3¢ per contract
        enforce_net_profitability=True,
    )
    evaluator = HistoricalEvaluator(config)
    report = evaluator.evaluate(dataset)

    assert report.metrics.candidates_detected == 1
    assert report.metrics.qualified_opportunities == 1
    assert report.metrics.rejected_opportunities == 0

    record = report.evaluated_records[0]
    assert record.is_qualified is True
    assert record.status == STATUS_QUALIFIED
    assert record.theoretical_gross_edge_cents == 50
    assert record.net_edge_cents == 30
    assert record.supported_quantity == 10
    assert record.entry_cost_cents == 950


# ===========================================================================
# 2. Candidate rejected after fees eliminate its edge
# ===========================================================================

def test_candidate_rejected_after_fees():
    obs = make_test_observation(
        ticker="KX-FEES",
        yes_ask="0.49",
        no_ask="0.50",  # total cost 99¢, gross edge 1¢
    )
    dataset = HistoricalDataset.from_observations([obs])
    # Fee is 1¢ per contract * 2 legs = 2¢ -> net edge = 1¢ - 2¢ = -1¢
    config = EvaluationConfig(
        default_fee_per_contract_cents=1,
        enforce_net_profitability=True,
    )
    evaluator = HistoricalEvaluator(config)
    report = evaluator.evaluate(dataset)

    assert report.metrics.candidates_detected == 1
    assert report.metrics.qualified_opportunities == 0
    assert report.metrics.rejected_opportunities == 1

    record = report.evaluated_records[0]
    assert record.is_qualified is False
    assert record.status == STATUS_REJECTED_UNPROFITABLE_FEES
    assert "Unprofitable after estimated fees" in record.rejection_reason


# ===========================================================================
# 3. Candidate rejected for insufficient depth
# ===========================================================================

def test_candidate_rejected_insufficient_depth():
    obs = make_test_observation(
        ticker="KX-NODEPTH",
        yes_ask="0.40",
        no_ask="0.55",
        empty_book=True,
    )
    dataset = HistoricalDataset.from_observations([obs])
    config = EvaluationConfig()
    evaluator = HistoricalEvaluator(config)
    report = evaluator.evaluate(dataset)

    assert report.metrics.candidates_detected == 1
    assert report.metrics.qualified_opportunities == 0
    assert report.metrics.rejected_opportunities == 1

    record = report.evaluated_records[0]
    assert record.is_qualified is False
    assert record.status == STATUS_REJECTED_INSUFFICIENT_DEPTH
    assert "no available ask liquidity" in record.rejection_reason


# ===========================================================================
# 4. Stale or invalid source timestamps
# ===========================================================================

def test_stale_and_invalid_timestamp_rejections():
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    # 4a: Stale timestamp (2 hours old)
    stale_ts = now - timedelta(hours=2)
    obs_stale = make_test_observation(
        ticker="KX-STALE",
        observed_at=now,
        source_timestamp=stale_ts,
    )
    ds_stale = HistoricalDataset.from_observations([obs_stale])
    rep_stale = HistoricalEvaluator(EvaluationConfig(max_stale_seconds=60.0)).evaluate(ds_stale)
    assert rep_stale.metrics.qualified_opportunities == 0
    assert rep_stale.evaluated_records[0].status == STATUS_REJECTED_STALE
    assert "stale" in rep_stale.evaluated_records[0].rejection_reason.lower()

    # 4b: Missing source timestamp
    obs_nots = make_test_observation(
        ticker="KX-NOTS",
        observed_at=now,
        source_timestamp=None,
    )
    ds_nots = HistoricalDataset.from_observations([obs_nots])
    rep_nots = HistoricalEvaluator(EvaluationConfig(require_source_timestamp=True)).evaluate(ds_nots)
    assert rep_nots.metrics.qualified_opportunities == 0
    assert rep_nots.evaluated_records[0].status == STATUS_REJECTED_MISSING_TIMESTAMP

    # 4c: Future drifted timestamp (> 10s into future)
    future_ts = now + timedelta(seconds=25)
    obs_future = make_test_observation(
        ticker="KX-FUT",
        observed_at=now,
        source_timestamp=future_ts,
    )
    ds_future = HistoricalDataset.from_observations([obs_future])
    rep_future = HistoricalEvaluator(EvaluationConfig(max_future_seconds=10.0)).evaluate(ds_future)
    assert rep_future.metrics.qualified_opportunities == 0
    assert rep_future.evaluated_records[0].status == STATUS_REJECTED_STALE
    assert "future" in rep_future.evaluated_records[0].rejection_reason.lower()


# ===========================================================================
# 5. Multi-leg timestamp skew rejection
# ===========================================================================

def test_mece_basket_timestamp_skew_rejection():
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    t1 = now
    t2 = now - timedelta(seconds=20)  # 20s skew

    obs_a = make_test_observation(
        ticker="KX-A",
        yes_ask="0.30",
        no_ask="0.70",
        observed_at=now,
        source_timestamp=t1,
    )
    obs_b = make_test_observation(
        ticker="KX-B",
        yes_ask="0.60",
        no_ask="0.40",
        observed_at=now,
        source_timestamp=t2,
    )

    basket = DeclaredMeceBasket(
        event_ticker="EV-SKEW",
        outcomes=("OUT_A", "OUT_B"),
        market_outcome_map={"KX-A": "OUT_A", "KX-B": "OUT_B"},
        basket_side=YES,
    )

    dataset = HistoricalDataset.from_observations([obs_a, obs_b])
    config = EvaluationConfig(
        declared_baskets=(basket,),
        max_leg_timestamp_skew_seconds=5.0,  # Max 5s allowed
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    basket_records = [r for r in report.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_records) == 1
    rec = basket_records[0]
    assert rec.is_qualified is False
    assert rec.status == STATUS_REJECTED_LEG_SKEW
    assert rec.leg_timestamp_skew_seconds >= 19.0


# ===========================================================================
# 6. Correct partial-fill behavior
# ===========================================================================

def test_partial_fill_handling():
    # Book only has depth of 4 contracts
    obs = make_test_observation(
        ticker="KX-PARTIAL",
        yes_ask="0.40",
        no_ask="0.55",
        yes_qty="4.00",
        no_qty="4.00",
    )
    dataset = HistoricalDataset.from_observations([obs])

    # Request quantity 10 -> partial fill of 4
    config = EvaluationConfig(
        target_quantity=10,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    assert len(report.paper_trades) == 1
    trade = report.paper_trades[0]
    assert trade.state == TradeState.PARTIALLY_FILLED
    assert trade.requested_quantity == 10
    assert trade.filled_quantity == 4
    assert trade.total_cost_cents == 4 * 95  # 380¢
    assert trade.estimated_fees_cents == 4 * 2    # 8¢


# ===========================================================================
# 7. Correct fee application without double-counting
# ===========================================================================

def test_fee_application_exactness():
    obs = make_test_observation(
        ticker="KX-FEECALC",
        yes_ask="0.40",
        no_ask="0.55",
    )
    dataset = HistoricalDataset.from_observations([obs])
    # 2¢ per contract fee
    config = EvaluationConfig(
        target_quantity=3,
        default_fee_per_contract_cents=2,
        enable_paper_trading=True,
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    trade = report.paper_trades[0]
    # 3 contracts * 2 legs = 6 leg contracts * 2¢ = 12¢ total fees
    assert trade.estimated_fees_cents == 12

    # Check portfolio ledger
    portfolio = report.paper_portfolio
    assert portfolio is not None
    assert portfolio.total_fees_paid_cents == 12
    # Initial 100_000 - (3 * 95¢ cost) - 12¢ fees = 100_000 - 285 - 12 = 99_703¢
    assert portfolio.available_cash_cents == 100_000 - 285 - 12


# ===========================================================================
# 8. Cash, positions, P&L, and ledger reconciliation
# ===========================================================================

def test_cash_positions_pnl_ledger_reconciliation():
    obs = make_test_observation(
        ticker="KX-LEDGER",
        yes_ask="0.40",
        no_ask="0.55",
    )
    dataset = HistoricalDataset.from_observations([obs])
    config = EvaluationConfig(
        initial_cash_cents=50_000,
        target_quantity=5,
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    portfolio = report.paper_portfolio
    assert portfolio is not None

    # Audit reconciliation
    rec = portfolio.reconcile()
    assert rec.is_reconciled is True
    assert report.metrics.ledger_is_reconciled is True

    # Cash check: 50_000 - (5 * 95¢ = 475¢) - (5 * 2¢ = 10¢) = 49_515¢
    assert portfolio.available_cash_cents == 49_515

    # Positions check: 5 YES and 5 NO
    assert portfolio.positions[("KX-LEDGER", YES)].quantity == 5
    assert portfolio.positions[("KX-LEDGER", NO)].quantity == 5

    # Book equity check: Cash (49_515) + Acquisition Cost Basis (475) = 49_990¢
    assert portfolio.book_value_equity_cents() == 50_000 - 10  # 10¢ fees paid


# ===========================================================================
# 9. Replay determinism on identical evidence and configuration
# ===========================================================================

def test_evaluation_determinism():
    obs1 = make_test_observation(ticker="KX-DET1", yes_ask="0.40", no_ask="0.55")
    obs2 = make_test_observation(ticker="KX-DET2", yes_ask="0.42", no_ask="0.53")
    dataset = HistoricalDataset.from_observations([obs1, obs2])

    config = EvaluationConfig(
        default_fee_per_contract_cents=1,
        enable_paper_trading=True,
    )

    evaluator = HistoricalEvaluator(config)
    report1 = evaluator.evaluate(dataset)
    report2 = evaluator.evaluate(dataset)

    assert report1.metrics.candidates_detected == report2.metrics.candidates_detected
    assert report1.metrics.qualified_opportunities == report2.metrics.qualified_opportunities
    assert report1.metrics.paper_final_cash_cents == report2.metrics.paper_final_cash_cents
    assert report1.metrics.paper_final_equity_cents == report2.metrics.paper_final_equity_cents
    assert len(report1.paper_trades) == len(report2.paper_trades)


# ===========================================================================
# 10. Malformed, incomplete, duplicate, and out-of-order evidence
# ===========================================================================

def test_malformed_duplicate_out_of_order_evidence_handling(tmp_path: Path):
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    t1 = now
    t2 = now + timedelta(seconds=10)
    t3 = now + timedelta(seconds=20)

    obs1 = make_test_observation("KX-1", observed_at=t1)
    obs2 = make_test_observation("KX-2", observed_at=t2)
    obs3 = make_test_observation("KX-3", observed_at=t3)

    # Prepare JSONL file with:
    # Line 1: obs2 (out of order!)
    # Line 2: obs1 (earlier!)
    # Line 3: malformed JSON!
    # Line 4: obs1 duplicate!
    # Line 5: obs3 (latest)
    from kalshi_arbitrage.evidence_storage import observation_to_dict

    jsonl_path = tmp_path / "mixed.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(observation_to_dict(obs2)) + "\n")
        f.write(json.dumps(observation_to_dict(obs1)) + "\n")
        f.write("{NOT_VALID_JSON}\n")
        f.write(json.dumps(observation_to_dict(obs1)) + "\n")  # duplicate!
        f.write(json.dumps(observation_to_dict(obs3)) + "\n")

    dataset = HistoricalDataset.from_file(jsonl_path, deduplicate=True, strict=False)

    assert dataset.total_raw_records == 5
    assert dataset.malformed_records_count == 1
    assert dataset.duplicate_records_count == 1
    assert dataset.out_of_order_records_count >= 1
    assert len(dataset.observations) == 3

    # Ensure chronological order was restored
    assert dataset.observations[0].ticker == "KX-1"
    assert dataset.observations[1].ticker == "KX-2"
    assert dataset.observations[2].ticker == "KX-3"


# ===========================================================================
# 11. No paper-state mutation when paper trading is disabled
# ===========================================================================

def test_paper_trading_disabled_by_default_no_mutation():
    obs = make_test_observation(
        ticker="KX-MUTATION",
        yes_ask="0.40",
        no_ask="0.55",
    )
    dataset = HistoricalDataset.from_observations([obs])

    # Default config has enable_paper_trading = False
    config = EvaluationConfig()
    assert config.enable_paper_trading is False

    report = HistoricalEvaluator(config).evaluate(dataset)

    assert report.paper_portfolio is None
    assert report.paper_trades == ()
    assert report.metrics.paper_trades_submitted == 0
    assert report.metrics.paper_final_cash_cents is None


# ===========================================================================
# 12. No duplicate simulated trades when replaying the same dataset
# ===========================================================================

def test_opportunity_deduplication_prevents_duplicate_trades():
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    t1 = now
    t2 = now + timedelta(seconds=10)

    # 2 consecutive observations of the same active opportunity
    obs1 = make_test_observation("KX-DEDUP", observed_at=t1)
    obs2 = make_test_observation("KX-DEDUP", observed_at=t2)

    dataset = HistoricalDataset.from_observations([obs1, obs2], deduplicate=False)
    config = EvaluationConfig(enable_paper_trading=True)
    report = HistoricalEvaluator(config).evaluate(dataset)

    # Both observations were evaluated
    assert len(report.evaluated_records) == 2
    assert report.evaluated_records[0].is_recurrent is False
    assert report.evaluated_records[1].is_recurrent is True

    # But only 1 paper trade was executed!
    assert len(report.paper_trades) == 1
    assert report.paper_trades[0].state == TradeState.FILLED


# ===========================================================================
# 13. Correct reporting when zero candidates qualify
# ===========================================================================

def test_zero_opportunity_reporting():
    # Prices that have no arbitrage: YES ask 60¢, NO ask 60¢ -> cost 120¢
    obs = make_test_observation(
        ticker="KX-NOARB",
        yes_ask="0.60",
        no_ask="0.60",
    )
    dataset = HistoricalDataset.from_observations([obs])
    config = EvaluationConfig()
    report = HistoricalEvaluator(config).evaluate(dataset)

    assert report.metrics.total_observations == 1
    assert report.metrics.valid_observations == 1
    assert report.metrics.candidates_detected == 0
    assert report.metrics.qualified_opportunities == 0
    assert report.metrics.rejected_opportunities == 0
    assert report.metrics.gross_edge_max_cents == 0

    summary = report.summary()
    assert "Theoretical Candidates Detected:   0" in summary
    assert "Qualified (Depth & Positive Net):  0" in summary


# ===========================================================================
# 14. Sensitivity analysis across fee and freshness profiles
# ===========================================================================

def test_sensitivity_analysis_execution():
    obs = make_test_observation(
        ticker="KX-SENS",
        yes_ask="0.48",
        no_ask="0.50",  # Gross edge: 100 - 98 = 2¢
    )
    dataset = HistoricalDataset.from_observations([obs])

    sens_report = run_sensitivity_analysis(dataset)
    assert len(sens_report.rows) > 0

    # 0¢ fee profile: Gross edge 2¢ -> net edge 2¢ -> QUALIFIES
    zero_fee_row = next(r for r in sens_report.rows if "Zero Fees" in r.name)
    assert zero_fee_row.qualified_count == 1

    # 2¢ fee profile: Gross edge 2¢ - (2¢ * 2 = 4¢) = -2¢ -> REJECTED
    high_fee_row = next(r for r in sens_report.rows if "High Fees" in r.name)
    assert high_fee_row.qualified_count == 0
    assert high_fee_row.rejections_unprofitable_fees == 1

    table_text = sens_report.summary()
    assert "Zero Fees (0¢)" in table_text
    assert "High Fees (2¢)" in table_text


# ===========================================================================
# 15. CLI execution integration test
# ===========================================================================

def test_cli_execution_with_json_export(tmp_path: Path):
    from scripts.evaluate_historical import main as cli_main
    from kalshi_arbitrage.evidence_storage import observation_to_dict

    obs = make_test_observation("KX-CLI", yes_ask="0.40", no_ask="0.55")
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    obs_file = evidence_dir / "observations.jsonl"
    with open(obs_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(observation_to_dict(obs)) + "\n")

    json_out = tmp_path / "output.json"

    exit_code = cli_main([
        "--evidence-dir", str(evidence_dir),
        "--fee-cents", "1",
        "--enable-paper-trading",
        "--sensitivity",
        "--output-json", str(json_out),
    ])

    assert exit_code == 0
    assert json_out.exists()

    with open(json_out, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["metrics"]["qualified_opportunities"] == 1
        assert data["metrics"]["paper_trades_filled"] == 1


# ===========================================================================
# 16. Regression Tests for Audit Findings F-01 through F-09
# ===========================================================================

def test_unconfigured_fee_fails_closed_when_enforcing_net_profitability():
    """F-01: When fees are unconfigured (None), enforce_net_profitability must fail closed."""
    obs = make_test_observation(ticker="KX-F01", yes_ask="0.40", no_ask="0.55")
    dataset = HistoricalDataset.from_observations([obs])

    # 1a. Enforcing net profitability without fee configuration must reject
    cfg_strict = EvaluationConfig(
        default_fee_per_contract_cents=None,
        enforce_net_profitability=True,
    )
    report_strict = HistoricalEvaluator(cfg_strict).evaluate(dataset)
    assert report_strict.metrics.candidates_detected == 1
    assert report_strict.metrics.qualified_opportunities == 0
    assert report_strict.metrics.rejected_opportunities == 1

    rec_strict = report_strict.evaluated_records[0]
    assert rec_strict.is_qualified is False
    assert rec_strict.status == STATUS_REJECTED_UNPROFITABLE_FEES
    assert rec_strict.configured_fee_cents is None
    assert rec_strict.net_edge_cents is None
    assert "Net profitability cannot be verified without fee configuration" in rec_strict.rejection_reason

    # 1b. Disabling enforcement qualifies the candidate while recording fee as None
    cfg_permissive = EvaluationConfig(
        default_fee_per_contract_cents=None,
        enforce_net_profitability=False,
    )
    report_perm = HistoricalEvaluator(cfg_permissive).evaluate(dataset)
    assert report_perm.metrics.qualified_opportunities == 1
    rec_perm = report_perm.evaluated_records[0]
    assert rec_perm.is_qualified is True
    assert rec_perm.configured_fee_cents is None
    assert rec_perm.net_edge_cents is None


def test_mece_basket_rejects_stale_and_future_legs():
    """F-02: MECE basket legs must fail closed when stale or future-drifted."""
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    # 2a. Both legs 2 hours old (skew only 1s) -> must reject as STALE
    t_stale1 = now - timedelta(hours=2)
    t_stale2 = now - timedelta(hours=2) + timedelta(seconds=1)
    obs_a = make_test_observation("KX-MA", yes_ask="0.40", no_ask="0.60", observed_at=now, source_timestamp=t_stale1)
    obs_b = make_test_observation("KX-MB", yes_ask="0.40", no_ask="0.60", observed_at=now, source_timestamp=t_stale2)
    basket = DeclaredMeceBasket(
        event_ticker="EV-STALE",
        outcomes=("A", "B"),
        market_outcome_map={"KX-MA": "A", "KX-MB": "B"},
        basket_side=YES,
    )
    ds_stale = HistoricalDataset.from_observations([obs_a, obs_b])
    rep_stale = HistoricalEvaluator(EvaluationConfig(declared_baskets=(basket,), max_stale_seconds=60.0)).evaluate(ds_stale)
    basket_recs = [r for r in rep_stale.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_recs) == 1
    assert basket_recs[0].is_qualified is False
    assert basket_recs[0].status == STATUS_REJECTED_STALE
    assert "stale" in basket_recs[0].rejection_reason.lower()

    # 2b. One leg marked is_stale=True -> must reject
    obs_stale_flag = make_test_observation("KX-MA", observed_at=now, source_timestamp=now, is_stale=True)
    obs_fresh = make_test_observation("KX-MB", observed_at=now, source_timestamp=now)
    ds_flag = HistoricalDataset.from_observations([obs_stale_flag, obs_fresh])
    rep_flag = HistoricalEvaluator(EvaluationConfig(declared_baskets=(basket,))).evaluate(ds_flag)
    basket_recs_flag = [r for r in rep_flag.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_recs_flag) == 1
    assert basket_recs_flag[0].is_qualified is False
    assert basket_recs_flag[0].status == STATUS_REJECTED_STALE

    # 2c. One leg future drifted (> 10s into future) -> must reject
    obs_future = make_test_observation("KX-MA", observed_at=now, source_timestamp=now + timedelta(seconds=25))
    ds_fut = HistoricalDataset.from_observations([obs_future, obs_fresh])
    rep_fut = HistoricalEvaluator(EvaluationConfig(declared_baskets=(basket,), max_future_seconds=10.0)).evaluate(ds_fut)
    basket_recs_fut = [r for r in rep_fut.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_recs_fut) == 1
    assert basket_recs_fut[0].is_qualified is False
    assert basket_recs_fut[0].status == STATUS_REJECTED_STALE
    assert "future" in basket_recs_fut[0].rejection_reason.lower()


def test_mece_basket_timezone_normalized_skew():
    """F-06: Distinct timezone offsets normalize consistently to UTC for skew calculation."""
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    t1_utc = now
    # t2 in UTC-4 (08:00:02 EDT is 12:00:02 UTC -> 2s skew)
    tz_minus4 = timezone(timedelta(hours=-4))
    t2_offset = datetime(2026, 10, 9, 8, 0, 2, tzinfo=tz_minus4)

    obs_a = make_test_observation("KX-TZA", yes_ask="0.40", no_ask="0.60", observed_at=now, source_timestamp=t1_utc)
    obs_b = make_test_observation("KX-TZB", yes_ask="0.40", no_ask="0.60", observed_at=now, source_timestamp=t2_offset)
    basket = DeclaredMeceBasket(
        event_ticker="EV-TZ",
        outcomes=("A", "B"),
        market_outcome_map={"KX-TZA": "A", "KX-TZB": "B"},
        basket_side=YES,
    )
    dataset = HistoricalDataset.from_observations([obs_a, obs_b])
    report = HistoricalEvaluator(EvaluationConfig(declared_baskets=(basket,), max_leg_timestamp_skew_seconds=5.0)).evaluate(dataset)
    basket_recs = [r for r in report.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_recs) == 1
    assert abs(basket_recs[0].leg_timestamp_skew_seconds - 2.0) < 1e-6
    assert basket_recs[0].is_qualified is True


def test_opportunity_episode_lifecycle_reappearance():
    """F-03: Opportunity recurrence ends on disappearance, allowing new episodes to trade."""
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Obs 1: Arb opportunity (40¢ + 55¢ = 95¢)
    obs1 = make_test_observation("KX-LIFE", yes_ask="0.40", no_ask="0.55", observed_at=now)
    # Obs 2: Lingering arb 5s later
    obs2 = make_test_observation("KX-LIFE", yes_ask="0.40", no_ask="0.55", observed_at=now + timedelta(seconds=5))
    # Obs 3: Arb disappeared (60¢ + 60¢ = 120¢) 10s later
    obs3 = make_test_observation("KX-LIFE", yes_ask="0.60", no_ask="0.60", observed_at=now + timedelta(seconds=10))
    # Obs 4: New arb opportunity reappears 60s later
    obs4 = make_test_observation("KX-LIFE", yes_ask="0.40", no_ask="0.55", observed_at=now + timedelta(seconds=60))

    dataset = HistoricalDataset.from_observations([obs1, obs2, obs3, obs4], deduplicate=False)
    config = EvaluationConfig(enable_paper_trading=True, default_fee_per_contract_cents=1)
    report = HistoricalEvaluator(config).evaluate(dataset)

    # 3 evaluated arb records: obs1 (initial), obs2 (recurrent), obs4 (reappeared new episode)
    assert len(report.evaluated_records) == 3
    assert report.evaluated_records[0].is_recurrent is False
    assert report.evaluated_records[1].is_recurrent is True
    assert report.evaluated_records[2].is_recurrent is False  # Reappeared episode!

    # Exactly 2 paper trades executed (obs1 and obs4)
    assert len(report.paper_trades) == 2
    assert report.paper_trades[0].state == TradeState.FILLED
    assert report.paper_trades[1].state == TradeState.FILLED


def test_stale_initial_observation_does_not_block_subsequent_fresh_trade():
    """F-03: A stale or rejected observation must not suppress subsequent fresh trades."""
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Obs 1: Stale timestamp (2 hours old)
    obs_stale = make_test_observation(
        "KX-STALEFIRST",
        yes_ask="0.40",
        no_ask="0.55",
        observed_at=now,
        source_timestamp=now - timedelta(hours=2),
    )
    # Obs 2: Fresh timestamp 10s later
    obs_fresh = make_test_observation(
        "KX-STALEFIRST",
        yes_ask="0.40",
        no_ask="0.55",
        observed_at=now + timedelta(seconds=10),
        source_timestamp=now + timedelta(seconds=10),
    )

    dataset = HistoricalDataset.from_observations([obs_stale, obs_fresh], deduplicate=False)
    config = EvaluationConfig(enable_paper_trading=True, default_fee_per_contract_cents=1)
    report = HistoricalEvaluator(config).evaluate(dataset)

    assert len(report.evaluated_records) == 2
    assert report.evaluated_records[0].status == STATUS_REJECTED_STALE
    assert report.evaluated_records[0].is_recurrent is False

    assert report.evaluated_records[1].status == STATUS_QUALIFIED
    assert report.evaluated_records[1].is_recurrent is False  # Must not be suppressed!

    # Fresh trade was executed
    assert len(report.paper_trades) == 1
    assert report.paper_trades[0].state == TradeState.FILLED


def test_opportunity_episode_timeout():
    """F-03: Inactivity beyond episode_timeout_seconds resets recurrence."""
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    obs1 = make_test_observation("KX-TIMEOUT", yes_ask="0.40", no_ask="0.55", observed_at=now)
    # 300s later (> 120s timeout)
    obs2 = make_test_observation("KX-TIMEOUT", yes_ask="0.40", no_ask="0.55", observed_at=now + timedelta(seconds=300))

    dataset = HistoricalDataset.from_observations([obs1, obs2], deduplicate=False)
    config = EvaluationConfig(enable_paper_trading=True, episode_timeout_seconds=120.0)
    report = HistoricalEvaluator(config).evaluate(dataset)

    assert len(report.evaluated_records) == 2
    assert report.evaluated_records[0].is_recurrent is False
    assert report.evaluated_records[1].is_recurrent is False  # Timed out -> new episode!
    assert len(report.paper_trades) == 2


def test_mece_basket_paper_trading_execution():
    """F-04: Declared MECE basket simulates paper trading fills and ledger updates."""
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    obs_a = make_test_observation("KX-MECEA", yes_ask="0.30", no_ask="0.70", observed_at=now)
    obs_b = make_test_observation("KX-MECEB", yes_ask="0.60", no_ask="0.40", observed_at=now)
    basket = DeclaredMeceBasket(
        event_ticker="EV-PAPER",
        outcomes=("OUT_A", "OUT_B"),
        market_outcome_map={"KX-MECEA": "OUT_A", "KX-MECEB": "OUT_B"},
        basket_side=YES,
    )
    dataset = HistoricalDataset.from_observations([obs_a, obs_b])
    config = EvaluationConfig(
        target_quantity=5,
        default_fee_per_contract_cents=1,
        declared_baskets=(basket,),
        enable_paper_trading=True,
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    # MECE basket record qualified
    basket_recs = [r for r in report.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_recs) == 1
    rec = basket_recs[0]
    assert rec.is_qualified is True
    assert rec.paper_trade_id is not None

    # Paper trade executed for MECE basket
    assert len(report.paper_trades) == 1
    trade = report.paper_trades[0]
    assert trade.trade_id == rec.paper_trade_id
    assert trade.state == TradeState.FILLED
    assert trade.filled_quantity == 5
    # 5 contracts * (30¢ + 60¢ = 90¢) = 450¢ cost
    assert trade.total_cost_cents == 450
    # 5 contracts * 2 legs * 1¢ fee = 10¢ fee
    assert trade.estimated_fees_cents == 10

    # Ledger consistency
    portfolio = report.paper_portfolio
    assert portfolio is not None
    assert portfolio.reconcile().is_reconciled is True
    assert portfolio.total_fees_paid_cents == 10
    assert portfolio.available_cash_cents == 100_000 - 450 - 10
    assert portfolio.positions[("KX-MECEA", YES)].quantity == 5
    assert portfolio.positions[("KX-MECEB", YES)].quantity == 5


def test_out_of_order_monotonic_high_water_count():
    """F-05: Ingestion counts out-of-order records against monotonic high-water mark."""
    base = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Timestamps: [10s, 5s, 6s, 7s, 8s, 9s, 10s]
    # Against monotonic high water 10s: indices 1, 2, 3, 4, 5 are all < 10s -> 5 out of order!
    offsets = [10, 5, 6, 7, 8, 9, 10]
    obs_list = [
        make_test_observation(f"KX-{i}", observed_at=base + timedelta(seconds=off))
        for i, off in enumerate(offsets)
    ]
    dataset = HistoricalDataset.from_observations(obs_list, deduplicate=False)
    assert dataset.out_of_order_records_count == 5
    # Chronological sort order preserved
    assert [int((o.observed_at - base).total_seconds()) for o in dataset.observations] == [
        5, 6, 7, 8, 9, 10, 10
    ]


def test_sensitivity_analysis_inherits_base_config_fee():
    """F-08: Sensitivity profiles inherit base config fee when not explicitly overridden."""
    obs = make_test_observation("KX-SENSFEE", yes_ask="0.40", no_ask="0.55")
    dataset = HistoricalDataset.from_observations([obs])

    base_config = EvaluationConfig(default_fee_per_contract_cents=3)
    sens_report = run_sensitivity_analysis(dataset, base_config=base_config)

    # Tight freshness profile should inherit the 3¢ fee from base_config
    tight_row = next(r for r in sens_report.rows if "Tight Freshness" in r.name)
    assert tight_row.fee_per_contract_cents == 3

    # Zero fees profile should still explicitly evaluate 0¢ fee
    zero_row = next(r for r in sens_report.rows if "Zero Fees" in r.name)
    assert zero_row.fee_per_contract_cents == 0


def test_haircut_separate_from_exchange_fees():
    """F-09: Modeled slippage haircut adjusts net edge without distorting ledger exchange fees."""
    obs = make_test_observation("KX-HAIRCUT", yes_ask="0.40", no_ask="0.50")  # gross edge 10¢
    dataset = HistoricalDataset.from_observations([obs])
    config = EvaluationConfig(
        target_quantity=5,
        default_fee_per_contract_cents=1,
        conservative_haircut_cents=2,
        enable_paper_trading=True,
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    record = report.evaluated_records[0]
    assert record.is_qualified is True
    # Gross edge: 5 * 10¢ = 50¢
    assert record.theoretical_gross_edge_cents == 50
    # Modeled exchange fee: 5 * 2 legs * 1¢ = 10¢
    assert record.configured_fee_cents == 1
    # Modeled haircut: 5 * 2 legs * 2¢ = 20¢
    assert record.conservative_haircut_cents == 2
    # Net edge after fee AND haircut: 50¢ - 10¢ - 20¢ = 20¢
    assert record.net_edge_cents == 20

    # Trade ledger must ONLY record exchange fees (10¢), not the haircut buffer!
    assert len(report.paper_trades) == 1
    trade = report.paper_trades[0]
    assert trade.estimated_fees_cents == 10
    portfolio = report.paper_portfolio
    assert portfolio is not None
    assert portfolio.total_fees_paid_cents == 10
    assert portfolio.reconcile().is_reconciled is True


def test_stale_observation_does_not_evict_active_episode():
    """Untrusted/stale observations must not evict an active episode or cause duplicate trades."""
    t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    # 1. Fresh arb opportunity arrives -> trades
    obs1 = make_test_observation("KX-ACTIVE", yes_ask="0.40", no_ask="0.55", observed_at=t0, source_timestamp=t0)
    # 2. Stale observation arrives with non-arb prices (must not evict active episode)
    t1 = t0 + timedelta(seconds=5)
    obs_stale = make_test_observation(
        "KX-ACTIVE",
        yes_ask="0.60",
        no_ask="0.60",
        observed_at=t1,
        source_timestamp=t0 - timedelta(hours=1),
    )
    # 3. Fresh observation arrives with continuing arb prices (must remain recurrent)
    t2 = t0 + timedelta(seconds=10)
    obs_fresh = make_test_observation("KX-ACTIVE", yes_ask="0.40", no_ask="0.55", observed_at=t2, source_timestamp=t2)

    dataset = HistoricalDataset.from_observations([obs1, obs_stale, obs_fresh], deduplicate=False)
    config = EvaluationConfig(enable_paper_trading=True, default_fee_per_contract_cents=1)
    report = HistoricalEvaluator(config).evaluate(dataset)

    # obs1 is evaluated and qualified; obs_fresh is evaluated and marked recurrent
    recs = report.evaluated_records
    assert len(recs) == 2
    assert recs[0].is_recurrent is False
    assert recs[0].is_qualified is True
    assert recs[1].is_recurrent is True
    assert recs[1].is_qualified is True

    # Exactly 1 paper trade executed (duplicate suppressed!)
    assert len(report.paper_trades) == 1
    assert report.paper_trades[0].state == TradeState.FILLED


def test_mece_interleaved_leg_updates_do_not_evict_active_episode():
    """Interleaved leg ticks in an MECE basket must not evict an active episode."""
    t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Cycle 1: Leg A and Leg B establish arb -> paper trade executes
    obs_a0 = make_test_observation("KX-LEGA", yes_ask="0.40", no_ask="0.60", observed_at=t0, source_timestamp=t0)
    obs_b0 = make_test_observation("KX-LEGB", yes_ask="0.55", no_ask="0.45", observed_at=t0, source_timestamp=t0)

    # Cycle 2: Leg A updates first to 0.50 (momentarily A=0.50 + B_old=0.55 = 1.05, no gross edge)
    t1 = t0 + timedelta(seconds=10)
    obs_a1 = make_test_observation("KX-LEGA", yes_ask="0.50", no_ask="0.50", observed_at=t1, source_timestamp=t1)

    # Cycle 2: Leg B updates 10ms later to 0.40 (A=0.50 + B=0.40 = 0.90, arb continues!)
    t1_b = t1 + timedelta(milliseconds=10)
    obs_b1 = make_test_observation("KX-LEGB", yes_ask="0.40", no_ask="0.60", observed_at=t1_b, source_timestamp=t1_b)

    basket = DeclaredMeceBasket(
        event_ticker="EV-INTERLEAVE",
        outcomes=("A", "B"),
        market_outcome_map={"KX-LEGA": "A", "KX-LEGB": "B"},
        basket_side=YES,
    )
    dataset = HistoricalDataset.from_observations([obs_a0, obs_b0, obs_a1, obs_b1], deduplicate=False)
    config = EvaluationConfig(
        declared_baskets=(basket,),
        enable_paper_trading=True,
        default_fee_per_contract_cents=1,
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    basket_recs = [r for r in report.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_recs) == 2
    assert basket_recs[0].is_qualified is True
    assert basket_recs[0].is_recurrent is False
    assert basket_recs[0].paper_trade_id is not None

    assert basket_recs[1].is_qualified is True
    assert basket_recs[1].is_recurrent is True  # Intermediate tick did NOT evict episode!

    # Exactly 1 paper trade executed (duplicate suppressed!)
    assert len(report.paper_trades) == 1
    assert report.paper_trades[0].state == TradeState.FILLED


def test_mece_basket_partial_fill_paper_trading():
    """Unequal leg depth results in common filled quantity, partial trade state, and consistent ledger."""
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Leg A has 10 contracts of depth at 30¢
    obs_a = make_test_observation("KX-PARTA", yes_ask="0.30", no_ask="0.70", yes_qty="10.00", observed_at=now)
    # Leg B has only 3 contracts of depth at 60¢ (bottleneck)
    obs_b = make_test_observation("KX-PARTB", yes_ask="0.60", no_ask="0.40", yes_qty="3.00", observed_at=now)

    basket = DeclaredMeceBasket(
        event_ticker="EV-PARTIAL",
        outcomes=("OUT_A", "OUT_B"),
        market_outcome_map={"KX-PARTA": "OUT_A", "KX-PARTB": "OUT_B"},
        basket_side=YES,
    )
    dataset = HistoricalDataset.from_observations([obs_a, obs_b])
    config = EvaluationConfig(
        target_quantity=10,
        default_fee_per_contract_cents=1,
        declared_baskets=(basket,),
        enable_paper_trading=True,
    )
    report = HistoricalEvaluator(config).evaluate(dataset)

    basket_recs = [r for r in report.evaluated_records if "MECE" in r.opportunity_id]
    assert len(basket_recs) == 1
    rec = basket_recs[0]
    assert rec.is_qualified is True
    assert rec.supported_quantity == 3  # Bottleneck depth
    assert rec.paper_trade_id is not None

    # Paper trade executed as partial fill
    assert len(report.paper_trades) == 1
    trade = report.paper_trades[0]
    assert trade.trade_id == rec.paper_trade_id
    assert trade.state == TradeState.PARTIALLY_FILLED
    assert trade.filled_quantity == 3
    # 3 contracts * (30¢ + 60¢ = 90¢) = 270¢ cost
    assert trade.total_cost_cents == 270
    # 3 contracts * 2 legs * 1¢ fee = 6¢ fee
    assert trade.estimated_fees_cents == 6

    # Ledger consistency
    portfolio = report.paper_portfolio
    assert portfolio is not None
    assert portfolio.reconcile().is_reconciled is True
    assert portfolio.total_fees_paid_cents == 6
    assert portfolio.available_cash_cents == 100_000 - 270 - 6
    assert portfolio.positions[("KX-PARTA", YES)].quantity == 3
    assert portfolio.positions[("KX-PARTB", YES)].quantity == 3
