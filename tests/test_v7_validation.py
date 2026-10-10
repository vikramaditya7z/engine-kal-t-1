"""Tests for V7 Historical Validation, Execution Realism & Strategy Performance.

Adversarial test suite covering:
1. Evidence-quality auditing (ingestion, cadence, gaps, liquidity, freshness, provenance).
2. Temporal correctness & zero look-ahead bias (chronological ordering, MECE desynchronization, inactivity timeout).
3. Multi-stage qualification & execution realism funnel (Theoretical -> Qualified -> Execution-Model -> Paper -> Realized).
4. Distribution statistics (percentiles, edge distributions, quantity, duration).
5. Paper-trading accounting, partial fills, haircut separation, and ledger reconciliation.
6. Sensitivity analysis and parameter sweeps.
7. Ten-section auditable reporting and structured JSON serialization.
8. Real trial dataset validation (data/v5_trial/observations.jsonl).
9. CLI execution integration.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest

from kalshi_arbitrage import (
    DeclaredMeceBasket,
    DistributionStats,
    EvaluationConfig,
    EvaluationError,
    EvidenceAuditReport,
    EvidenceAuditor,
    HistoricalDataset,
    HistoricalEvaluator,
    MarketObservation,
    NormalizedMarket,
    NormalizedOrderBook,
    ObservationGap,
    OrderBookLevel,
    PaperPortfolio,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    TradeState,
    V7HistoricalValidator,
    V7ValidationConfig,
    V7ValidationReport,
    validate_historical_dataset,
    NO,
    YES,
)
from kalshi_arbitrage.evidence_storage import observation_to_dict

_SENTINEL = object()


def make_test_obs(
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
    event_ticker: str = "EV-TEST",
) -> MarketObservation:
    """Generate deterministic synthetic market observation for V7 testing."""
    now = observed_at or datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    src_ts = now if source_timestamp is _SENTINEL else source_timestamp

    market = NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
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
        # Resting bids derive counter-party asks
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
# 1. Distribution Statistics Tests
# ===========================================================================

class TestDistributionStats:
    """Test exact percentile computations and edge cases."""

    def test_empty_distribution(self):
        stats = DistributionStats.from_values([])
        assert stats.count == 0
        assert stats.min_val is None
        assert stats.median_val is None
        assert stats.mean_val is None
        assert stats.max_val is None
        d = stats.to_dict()
        assert d["count"] == 0
        assert d["mean"] is None

    def test_single_value_distribution(self):
        stats = DistributionStats.from_values([Decimal("42.5")])
        assert stats.count == 1
        assert stats.min_val == Decimal("42.5")
        assert stats.median_val == Decimal("42.5")
        assert stats.mean_val == Decimal("42.5")
        assert stats.max_val == Decimal("42.5")
        assert stats.p25 == Decimal("42.5")
        assert stats.p75 == Decimal("42.5")
        assert stats.p90 == Decimal("42.5")

    def test_multi_value_distribution_percentiles(self):
        # 5 values: 10, 20, 30, 40, 50
        stats = DistributionStats.from_values([10, 20, 30, 40, 50])
        assert stats.count == 5
        assert stats.min_val == Decimal("10")
        assert stats.max_val == Decimal("50")
        assert stats.median_val == Decimal("30")
        assert stats.mean_val == Decimal("30")
        # p25: k = 4 * 0.25 = 1 -> index 1 -> 20
        assert stats.p25 == Decimal("20")
        # p75: k = 4 * 0.75 = 3 -> index 3 -> 40
        assert stats.p75 == Decimal("40")
        # p90: k = 4 * 0.90 = 3.6 -> index 3 + 0.6*(index 4 - index 3) = 40 + 6 = 46.0
        assert stats.p90 == Decimal("46.0")

    def test_even_count_median(self):
        # 4 values: 10, 20, 30, 40
        stats = DistributionStats.from_values([10, 20, 30, 40])
        assert stats.count == 4
        # median: k = 3 * 0.5 = 1.5 -> 20 + 0.5 * (30 - 20) = 25.0
        assert stats.median_val == Decimal("25.0")
        assert stats.mean_val == Decimal("25")


# ===========================================================================
# 2. Evidence Quality & Ingestion Auditing Tests
# ===========================================================================

class TestEvidenceAuditor:
    """Test deep evidence auditing (cadence, gaps, liquidity health, freshness)."""

    def test_audit_empty_dataset(self):
        auditor = EvidenceAuditor()
        dataset = HistoricalDataset.from_observations([])
        report = auditor.audit(dataset)
        assert report.total_raw_records == 0
        assert report.valid_records_count == 0
        assert report.time_span_seconds == 0.0
        assert report.mean_interval_seconds is None
        assert report.median_interval_seconds is None
        assert len(report.gaps) == 0

    def test_audit_cadence_and_gaps(self):
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        # Sequence: t0, t0+5s, t0+10s, t0+80s (gap of 70s), t0+85s
        obs = [
            make_test_obs("KX-1", observed_at=t0),
            make_test_obs("KX-1", observed_at=t0 + timedelta(seconds=5)),
            make_test_obs("KX-1", observed_at=t0 + timedelta(seconds=10)),
            make_test_obs("KX-1", observed_at=t0 + timedelta(seconds=80)),
            make_test_obs("KX-1", observed_at=t0 + timedelta(seconds=85)),
        ]
        dataset = HistoricalDataset.from_observations(obs)
        auditor = EvidenceAuditor(gap_threshold_seconds=60.0)
        report = auditor.audit(dataset)

        assert report.total_raw_records == 5
        assert report.valid_records_count == 5
        assert report.time_span_seconds == 85.0
        assert len(report.gaps) == 1
        gap = report.gaps[0]
        assert gap.duration_seconds == 70.0
        assert gap.from_timestamp == t0 + timedelta(seconds=10)
        assert gap.to_timestamp == t0 + timedelta(seconds=80)

    def test_audit_liquidity_empty_vs_populated(self):
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        obs_empty = make_test_obs("KX-EMPTY", observed_at=t0, empty_book=True)
        obs_pop = make_test_obs("KX-POP", observed_at=t0 + timedelta(seconds=5), empty_book=False, yes_qty="25.0", no_qty="35.0")

        dataset = HistoricalDataset.from_observations([obs_empty, obs_pop])
        auditor = EvidenceAuditor()
        report = auditor.audit(dataset)

        assert report.empty_books_count == 1
        assert report.populated_books_count == 1
        # In obs_pop: 25 YES ask (derived from NO bid 25) + 35 NO ask (derived from YES bid 35) = 60 bids total
        assert report.total_bid_depth_contracts == Decimal("60.0")

    def test_audit_stale_and_future_drift(self):
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        obs_fresh = make_test_obs("KX-FRESH", observed_at=t0, source_timestamp=t0)
        obs_stale = make_test_obs("KX-STALE", observed_at=t0, source_timestamp=t0 - timedelta(seconds=120))
        obs_future = make_test_obs("KX-FUTURE", observed_at=t0, source_timestamp=t0 + timedelta(seconds=20))
        obs_missing = make_test_obs("KX-MISS", observed_at=t0, source_timestamp=None)

        dataset = HistoricalDataset.from_observations([obs_fresh, obs_stale, obs_future, obs_missing])
        auditor = EvidenceAuditor(max_stale_seconds=60.0, max_future_seconds=10.0)
        report = auditor.audit(dataset)

        assert report.fresh_records_count == 1
        assert report.stale_records_count == 1
        assert report.future_drift_records_count == 1
        assert report.missing_source_ts_count == 1

    def test_audit_out_of_order_and_duplicates(self):
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        obs_1 = make_test_obs("KX-1", observed_at=t0 + timedelta(seconds=10))
        obs_2 = make_test_obs("KX-1", observed_at=t0 + timedelta(seconds=5))
        obs_3 = make_test_obs("KX-1", observed_at=t0 + timedelta(seconds=5))

        dataset = HistoricalDataset.from_observations([obs_1, obs_2, obs_3], deduplicate=False)
        auditor = EvidenceAuditor()
        report = auditor.audit(dataset)

        assert report.out_of_order_records_count == 2
        assert report.duplicate_records_count == 1


# ===========================================================================
# 3. Temporal Correctness & Zero Look-Ahead Bias Tests
# ===========================================================================

class TestTemporalCorrectness:
    """Verify strictly causal chronological replay without look-ahead bias."""

    def test_strictly_chronological_evaluation_order(self):
        """Observations must be evaluated strictly by UTC time."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        obs_late = make_test_obs("KX-TEST", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=10))
        obs_early = make_test_obs("KX-TEST", yes_ask="0.40", no_ask="0.55", observed_at=t0)

        dataset = HistoricalDataset.from_observations([obs_late, obs_early])
        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        assert report.audit_report.valid_records_count == 2
        assert report.evaluation_report is not None
        recs = report.evaluation_report.evaluated_records
        assert len(recs) == 2
        assert recs[0].observed_at == t0
        assert recs[1].observed_at == t0 + timedelta(seconds=10)

    def test_intermediate_mece_ticks_do_not_evict_active_episode(self):
        """Intermediate desynchronized leg update must not falsely evict active episode."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        basket = DeclaredMeceBasket(
            event_ticker="EV-MECE",
            outcomes=("A", "B"),
            market_outcome_map={"KX-A": "A", "KX-B": "B"},
            basket_side=YES,
        )

        obs_a0 = make_test_obs("KX-A", yes_ask="0.40", no_ask="0.60", observed_at=t0, source_timestamp=t0, event_ticker="EV-MECE")
        obs_b0 = make_test_obs("KX-B", yes_ask="0.45", no_ask="0.55", observed_at=t0, source_timestamp=t0, event_ticker="EV-MECE")

        t_int = t0 + timedelta(seconds=30)
        obs_a_int = make_test_obs("KX-A", yes_ask="0.40", no_ask="0.60", observed_at=t_int, source_timestamp=t_int, event_ticker="EV-MECE")

        t_reapp = t0 + timedelta(seconds=40)
        obs_a_re = make_test_obs("KX-A", yes_ask="0.40", no_ask="0.60", observed_at=t_reapp, source_timestamp=t_reapp, event_ticker="EV-MECE")
        obs_b_re = make_test_obs("KX-B", yes_ask="0.45", no_ask="0.55", observed_at=t_reapp, source_timestamp=t_reapp, event_ticker="EV-MECE")

        dataset = HistoricalDataset.from_observations(
            [obs_a0, obs_b0, obs_a_int, obs_a_re, obs_b_re],
        )
        cfg = EvaluationConfig(
            declared_baskets=(basket,),
            default_fee_per_contract_cents=1,
            max_leg_timestamp_skew_seconds=5.0,
        )
        v7_cfg = V7ValidationConfig(evaluation_config=cfg)
        validator = V7HistoricalValidator(v7_cfg)
        report = validator.validate(dataset)

        # The episode should remain active across the intermediate desynchronized update
        # Initial episodes count should be 1
        assert report.funnel_initial_episodes == 1


# ===========================================================================
# 4. Multi-Stage Qualification Funnel Tests
# ===========================================================================

class TestMultiStageFunnel:
    """Verify strict stage-by-stage filtering and accounting."""

    def test_funnel_stale_rejection(self):
        """Stale snapshot rejected before theoretical edge evaluation."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        obs_stale = make_test_obs(
            "KX-STALE",
            yes_ask="0.40",
            no_ask="0.55",
            observed_at=t0,
            source_timestamp=t0 - timedelta(seconds=100),  # > 60s stale
        )
        dataset = HistoricalDataset.from_observations([obs_stale])
        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        assert report.funnel_raw_observations == 1
        assert report.funnel_fresh_observations == 0
        # Theoretical gross edge exists mathematically in quotes (5¢)
        assert report.funnel_theoretical_candidates == 1
        # But fails freshness qualification
        assert report.funnel_qualified_candidates == 0

    def test_funnel_depth_rejection(self):
        """Candidate with positive theoretical edge but empty order book fails execution-model."""
        obs = make_test_obs(
            "KX-NODEPTH",
            yes_ask="0.40",
            no_ask="0.55",
            empty_book=True,  # No counter-party bids in order book
        )
        dataset = HistoricalDataset.from_observations([obs])
        cfg = EvaluationConfig(target_quantity=5, default_fee_per_contract_cents=1)
        validator = V7HistoricalValidator(V7ValidationConfig(evaluation_config=cfg))
        report = validator.validate(dataset)

        assert report.funnel_raw_observations == 1
        assert report.funnel_fresh_observations == 1
        # Theoretical gross edge: 100 - (40 + 55) = 5¢ > 0
        assert report.funnel_theoretical_candidates == 1
        # Insufficient depth: 0 supported quantity in empty order book
        assert report.funnel_execution_model_candidates == 0
        assert report.funnel_qualified_candidates == 0

    def test_funnel_fee_rejection(self):
        """Candidate with positive gross edge and adequate depth fails fee constraint."""
        obs = make_test_obs(
            "KX-FEES",
            yes_ask="0.49",
            no_ask="0.50",  # Cost: 99¢, Gross edge: 1¢
            yes_qty="10.0",
            no_qty="10.0",
        )
        dataset = HistoricalDataset.from_observations([obs])
        # Fee 1¢ per contract * 2 legs = 2¢ fee > 1¢ gross edge -> Net edge -1¢
        cfg = EvaluationConfig(target_quantity=1, default_fee_per_contract_cents=1, enforce_net_profitability=True)
        validator = V7HistoricalValidator(V7ValidationConfig(evaluation_config=cfg))
        report = validator.validate(dataset)

        assert report.funnel_raw_observations == 1
        assert report.funnel_fresh_observations == 1
        assert report.funnel_theoretical_candidates == 1
        assert report.funnel_execution_model_candidates == 1
        assert report.funnel_net_profitable_candidates == 0
        assert report.funnel_qualified_candidates == 0

    def test_funnel_qualified_and_episode_recurrence(self):
        """Candidate qualifies, but repeated snapshot in same episode does not duplicate."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        obs1 = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0)
        obs2 = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=10))

        dataset = HistoricalDataset.from_observations([obs1, obs2])
        cfg = EvaluationConfig(
            target_quantity=1,
            default_fee_per_contract_cents=1,
            enforce_net_profitability=True,
            enable_paper_trading=True,
        )
        validator = V7HistoricalValidator(V7ValidationConfig(evaluation_config=cfg))
        report = validator.validate(dataset)

        assert report.funnel_raw_observations == 2
        assert report.funnel_fresh_observations == 2
        assert report.funnel_theoretical_candidates == 2
        assert report.funnel_execution_model_candidates == 2
        assert report.funnel_net_profitable_candidates == 2
        assert report.funnel_qualified_candidates == 2
        # Only 1 initial episode! Second snapshot is recurrence-suppressed
        assert report.funnel_initial_episodes == 1
        assert report.funnel_paper_trades_submitted == 1
        assert report.funnel_paper_trades_filled == 1
        assert report.funnel_realized_trades == 0  # Zero prior to official settlement


# ===========================================================================
# 5. Execution Realism, Accounting & Reconciliation Tests
# ===========================================================================

class TestExecutionRealismAndAccounting:
    """Verify conservative execution pricing, haircut isolation, and ledger balance."""

    def test_slippage_haircut_not_booked_as_exchange_fee(self):
        """Haircut qualifies conservatively without contaminating exchange fee accounting."""
        obs = make_test_obs("KX-HAIR", yes_ask="0.40", no_ask="0.55")
        dataset = HistoricalDataset.from_observations([obs])

        # Gross edge 5¢. Fee: 1¢*2 = 2¢. Haircut: 1¢*2 = 2¢. Net edge: 5¢ - 2¢ - 2¢ = 1¢
        cfg = EvaluationConfig(
            target_quantity=1,
            default_fee_per_contract_cents=1,
            conservative_haircut_cents=1,
            enable_paper_trading=True,
        )
        validator = V7HistoricalValidator(V7ValidationConfig(evaluation_config=cfg))
        report = validator.validate(dataset)

        assert report.funnel_qualified_candidates == 1
        eval_rep = report.evaluation_report
        assert eval_rep is not None
        rec = eval_rep.evaluated_records[0]
        # Configured fee is 1¢ per contract, haircut is 1¢ per contract, net edge is 1¢
        assert rec.configured_fee_cents == 1
        assert rec.conservative_haircut_cents == 1
        assert rec.net_edge_cents == 1

        # Ledger verification: total exchange fees charged in paper portfolio must be 2¢ (1¢ * 2 legs)
        assert eval_rep.metrics.paper_total_fees_cents == 2
        assert eval_rep.metrics.ledger_is_reconciled is True

    def test_ledger_reconciliation_and_drawdown(self):
        """Simulated portfolio reconciles cash, holdings, and tracks drawdown."""
        obs = make_test_obs("KX-RECON", yes_ask="0.40", no_ask="0.55")
        dataset = HistoricalDataset.from_observations([obs])

        cfg = EvaluationConfig(
            initial_cash_cents=50_000,
            target_quantity=10,
            default_fee_per_contract_cents=1,
            enable_paper_trading=True,
        )
        validator = V7HistoricalValidator(V7ValidationConfig(evaluation_config=cfg))
        report = validator.validate(dataset)

        eval_rep = report.evaluation_report
        assert eval_rep is not None
        m = eval_rep.metrics
        # Cost: 10 * 95¢ = 950¢. Fees: 10 * 2¢ = 20¢. Total cash outflow: 970¢
        # Remaining cash: 50,000 - 970 = 49,030¢
        # Holdings book value: 950¢. Total equity: 49,030 + 950 = 49,980¢ (-20¢ fees)
        assert m.paper_final_cash_cents == 49_030
        assert m.paper_final_equity_cents == 49_980
        assert m.paper_total_fees_cents == 20
        assert m.ledger_is_reconciled is True
        assert m.paper_max_drawdown_cents == 20


# ===========================================================================
# 6. Sensitivity Analysis Integration Tests
# ===========================================================================

class TestSensitivityAnalysis:
    """Verify sensitivity sweeps across fees, freshness, and haircuts."""

    def test_sensitivity_sweep_results(self):
        obs = make_test_obs("KX-SENS", yes_ask="0.48", no_ask="0.50")  # Gross edge 2¢
        dataset = HistoricalDataset.from_observations([obs])

        v7_cfg = V7ValidationConfig(run_sensitivity=True)
        validator = V7HistoricalValidator(v7_cfg)
        report = validator.validate(dataset)

        assert report.sensitivity_report is not None
        rows = report.sensitivity_report.rows
        assert len(rows) >= 7

        # Zero fees (0¢) -> gross edge 2¢ qualifies
        zero_fee = next(r for r in rows if "Zero Fees" in r.name)
        assert zero_fee.qualified_count == 1

        # High fees (2¢) -> gross edge 2¢ - 4¢ fee = -2¢ rejected
        high_fee = next(r for r in rows if "High Fees" in r.name)
        assert high_fee.qualified_count == 0


# ===========================================================================
# 7. Auditable Reporting & JSON Serialization Tests
# ===========================================================================

class TestReportingAndSerialization:
    """Verify complete 10-section report generation and JSON fidelity."""

    def test_ten_section_summary_structure(self):
        obs = make_test_obs("KX-REP", yes_ask="0.40", no_ask="0.55")
        dataset = HistoricalDataset.from_observations([obs])

        validator = V7HistoricalValidator()
        report = validator.validate(dataset)
        summary = report.summary()

        assert "SECTION 1: DATASET IDENTITY & COVERAGE" in summary
        assert "SECTION 2: EVIDENCE QUALITY & DATA LIMITATIONS" in summary
        assert "SECTION 3: EVALUATION CONFIGURATION & EXECUTION ASSUMPTIONS" in summary
        assert "SECTION 4: CANDIDATE QUALIFICATION & MULTI-STAGE REJECTION BREAKDOWN" in summary
        assert "SECTION 5: GROSS AND MODELED NET EDGE DISTRIBUTIONS" in summary
        assert "SECTION 6: LIQUIDITY AND SUPPORTED QUANTITY ANALYSIS" in summary
        assert "SECTION 7: PAPER TRADING PERFORMANCE (SIMULATED LEDGER)" in summary
        assert "SECTION 8: PARAMETER SENSITIVITY ANALYSIS" in summary
        assert "SECTION 9: DATA & MODEL LIMITATIONS" in summary
        assert "SECTION 10: FINAL EVIDENCE-BACKED VERDICT" in summary

    def test_json_serialization_fidelity(self):
        obs = make_test_obs("KX-JSON", yes_ask="0.40", no_ask="0.55")
        dataset = HistoricalDataset.from_observations([obs])

        v7_cfg = V7ValidationConfig(run_sensitivity=True)
        validator = V7HistoricalValidator(v7_cfg)
        report = validator.validate(dataset)

        d = report.to_dict()
        assert "section_1_dataset_identity" in d
        assert "section_2_evidence_quality" in d
        assert "section_3_evaluation_configuration" in d
        assert "section_4_qualification_funnel" in d
        assert "section_5_edge_distributions" in d
        assert "section_6_liquidity_analysis" in d
        assert "section_7_paper_trading_performance" in d
        assert "section_8_sensitivity_analysis" in d
        assert "section_9_limitations" in d
        assert "section_10_final_verdict" in d

        # Ensure json.dumps succeeds without serialization error
        serialized = json.dumps(d, indent=2)
        deserialized = json.loads(serialized)
        assert deserialized["section_1_dataset_identity"]["total_raw_records"] == 1
        assert deserialized["section_4_qualification_funnel"]["raw_observations"] == 1

    def test_provenance_records_capture(self):
        """Representative qualified and rejected cases include prices, fees, and reasons."""
        obs_qual = make_test_obs("KX-QUAL", yes_ask="0.40", no_ask="0.55")
        obs_rej = make_test_obs("KX-REJ", yes_ask="0.49", no_ask="0.50")  # Cost 99¢, gross edge 1¢, fee 2¢ -> REJECTED
        dataset = HistoricalDataset.from_observations([obs_qual, obs_rej])

        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        assert len(report.representative_qualified) == 1
        assert len(report.representative_rejected) == 1

        qual_case = report.representative_qualified[0]
        assert qual_case["tickers"] == ["KX-QUAL"]
        assert qual_case["net_edge_cents"] == 3

        rej_case = report.representative_rejected[0]
        assert rej_case["tickers"] == ["KX-REJ"]
        assert "rejection_reason" in rej_case


# ===========================================================================
# 8. Real Trial Dataset End-to-End Validation
# ===========================================================================

class TestRealTrialDatasetValidation:
    """Validate data/v5_trial/observations.jsonl using V7HistoricalValidator."""

    def test_trial_dataset_evaluation(self):
        trial_path = Path("data/v5_trial/observations.jsonl")
        if not trial_path.exists():
            pytest.skip("data/v5_trial/observations.jsonl does not exist")

        dataset = HistoricalDataset.from_file(trial_path)
        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        assert report.audit_report.total_raw_records == 21
        assert report.audit_report.valid_records_count == 21
        assert report.audit_report.unique_tickers_count == 7
        assert report.audit_report.empty_books_count == 15
        assert report.audit_report.stale_records_count == 8
        assert report.audit_report.fresh_records_count == 13

        # Zero candidates in trial sample
        assert report.funnel_theoretical_candidates == 0
        assert report.funnel_qualified_candidates == 0

        # Verdict must acknowledge trial dataset sample size limitations
        assert "INSUFFICIENT HISTORICAL EVIDENCE" in report.final_verdict


# ===========================================================================
# 9. CLI Tool Integration Tests
# ===========================================================================

class TestV7CLI:
    """Verify scripts/validate_historical_v7.py execution."""

    def test_cli_full_run(self, tmp_path: Path):
        from scripts.validate_historical_v7 import main as cli_main

        obs = make_test_obs("KX-CLI", yes_ask="0.40", no_ask="0.55")
        obs_file = tmp_path / "observations.jsonl"
        with open(obs_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(observation_to_dict(obs)) + "\n")

        json_out = tmp_path / "v7_report.json"
        exit_code = cli_main([
            "--file", str(obs_file),
            "--fee-cents", "1",
            "--enable-paper-trading",
            "--sensitivity",
            "--output-json", str(json_out),
        ])

        assert exit_code == 0
        assert json_out.exists()
        with open(json_out, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["section_4_qualification_funnel"]["qualified_candidates"] == 1
            assert data["section_7_paper_trading_performance"]["paper_trades_filled"] == 1
            assert len(data["section_8_sensitivity_analysis"]) > 0

    def test_cli_audit_only(self, tmp_path: Path):
        from scripts.validate_historical_v7 import main as cli_main

        obs = make_test_obs("KX-AUDIT", yes_ask="0.40", no_ask="0.55")
        obs_file = tmp_path / "observations.jsonl"
        with open(obs_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(observation_to_dict(obs)) + "\n")

        json_out = tmp_path / "audit_report.json"
        exit_code = cli_main([
            "--file", str(obs_file),
            "--audit-only",
            "--output-json", str(json_out),
        ])

        assert exit_code == 0
        assert json_out.exists()
        with open(json_out, "r", encoding="utf-8") as f:
            data = json.load(f)
            assert data["section_2_evidence_quality"]["valid_records_count"] == 1
            # Paper trading and evaluation were skipped
            assert data["section_7_paper_trading_performance"]["paper_trades_submitted"] == 0
            assert "AUDIT ONLY" in data["section_10_final_verdict"]


# ===========================================================================
# 10. V7 Remediation Adversarial Regression Tests (F-01, F-02, F-03, F-05)
# ===========================================================================

class TestV7Remediation:
    """Rigorous adversarial tests for episode lifecycle, empty distributions, and reporting."""

    def test_separate_episodes_produce_separate_duration_samples(self):
        """F-01: Distinct opportunity episodes hours apart must yield separate duration samples."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        # Episode 1: 10s span
        obs_ep1_start = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0)
        obs_ep1_end = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=10))

        # Episode 2: 1 hour later, 5s span
        t1 = t0 + timedelta(hours=1)
        obs_ep2_start = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t1)
        obs_ep2_end = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t1 + timedelta(seconds=5))

        dataset = HistoricalDataset.from_observations([obs_ep1_start, obs_ep1_end, obs_ep2_start, obs_ep2_end])
        cfg = EvaluationConfig(
            target_quantity=1,
            default_fee_per_contract_cents=1,
            episode_timeout_seconds=60.0,
            enforce_net_profitability=True,
        )
        validator = V7HistoricalValidator(V7ValidationConfig(evaluation_config=cfg))
        report = validator.validate(dataset)

        assert report.funnel_qualified_candidates == 4
        # Two distinct initial episodes
        assert report.funnel_initial_episodes == 2

        # Duration distribution must contain exactly 2 samples: 10.0s and 5.0s (NOT 3605.0s!)
        dur_dist = report.episode_duration_distribution
        assert dur_dist.count == 2
        assert dur_dist.min_val == Decimal("5.0")
        assert dur_dist.max_val == Decimal("10.0")
        assert dur_dist.mean_val == Decimal("7.5")
        assert dur_dist.median_val == Decimal("7.5")

    def test_unqualified_rejected_snapshots_never_contribute_to_episode_duration(self):
        """F-01: Unqualified and rejected snapshots must not generate fake duration samples."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        # Three candidate observations with theoretical edge but zero depth
        obs1 = make_test_obs("KX-EMPTY", yes_ask="0.40", no_ask="0.55", observed_at=t0, empty_book=True)
        obs2 = make_test_obs("KX-EMPTY", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=10), empty_book=True)
        obs3 = make_test_obs("KX-EMPTY", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=20), empty_book=True)

        dataset = HistoricalDataset.from_observations([obs1, obs2, obs3])
        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        assert report.funnel_theoretical_candidates == 3
        assert report.funnel_qualified_candidates == 0

        # Duration distribution must be cleanly empty (count=0)
        dur_dist = report.episode_duration_distribution
        assert dur_dist.count == 0
        assert dur_dist.min_val is None
        assert dur_dist.max_val is None

        # Summary must render explicit unobserved indicator
        summary = report.summary()
        assert "Episode Duration (seconds):        (No candidate opportunities observed)" in summary

    def test_intervening_rejection_cleanly_terminates_episode(self):
        """F-01: Intervening rejection terminates active episode and does not bridge to later opportunities."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        # Episode 1: Qualified for 10s
        obs1 = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0)
        obs2 = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=10))

        # Intervening rejection at t0 + 15s (empty order book)
        obs_rej = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=15), empty_book=True)

        # Episode 2: Qualified again at t0 + 20s and t0 + 26s (6s span)
        obs3 = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=20))
        obs4 = make_test_obs("KX-ARB", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=26))

        dataset = HistoricalDataset.from_observations([obs1, obs2, obs_rej, obs3, obs4])
        cfg = EvaluationConfig(
            target_quantity=1,
            default_fee_per_contract_cents=1,
            episode_timeout_seconds=60.0,
            enforce_net_profitability=True,
        )
        validator = V7HistoricalValidator(V7ValidationConfig(evaluation_config=cfg))
        report = validator.validate(dataset)

        assert report.funnel_qualified_candidates == 4

        dur_dist = report.episode_duration_distribution
        # Two distinct episodes because intervening rejection broke the active episode
        assert dur_dist.count == 2
        # Episode 1 was 10.0s (not 15s or 20s), Episode 2 was 6.0s
        assert dur_dist.min_val == Decimal("6.0")
        assert dur_dist.max_val == Decimal("10.0")

    def test_zero_candidate_dataset_empty_distribution_formatting(self):
        """F-02: Zero-candidate dataset renders explicit unobserved messages, not zero statistics."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        # Non-arbitrage market: cost 1.10 > 1.00 -> Gross edge -10¢
        obs = make_test_obs("KX-NOARB", yes_ask="0.55", no_ask="0.55", observed_at=t0)

        dataset = HistoricalDataset.from_observations([obs])
        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        summary = report.summary()
        # Verify all four distributions display the explicit unobserved text
        assert "Theoretical Gross Edge (cents):    (No candidate opportunities observed)" in summary
        assert "Modeled Net Edge (cents):          (No candidate opportunities observed)" in summary
        assert "Episode Duration (seconds):        (No candidate opportunities observed)" in summary
        assert "Executable Quantity Supported:     (No candidate opportunities observed)" in summary

        # Must NOT display synthesized zero statistics
        assert "Min: 0¢ | Median: 0.0¢" not in summary
        assert "Min: 0 | Median: 0.0" not in summary

        # Structured dictionary export must be well-formed with count=0 and None percentiles
        d = report.to_dict()
        gross_d = d["section_5_edge_distributions"]["theoretical_gross_edge"]
        net_d = d["section_5_edge_distributions"]["modeled_net_edge"]
        dur_d = d["section_5_edge_distributions"]["episode_duration_seconds"]
        qty_d = d["section_6_liquidity_analysis"]["supported_quantity"]

        for dist_dict in (gross_d, net_d, dur_d, qty_d):
            assert dist_dict["count"] == 0
            assert dist_dict["min"] is None
            assert dist_dict["median"] is None
            assert dist_dict["mean"] is None
            assert dist_dict["max"] is None

    def test_qualification_funnel_distinguishes_raw_ingestion_and_valid_parsed(self, tmp_path: Path):
        """F-03: Funnel distinguishes raw file lines from valid parsed observations."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        obs1 = make_test_obs("KX-1", yes_ask="0.40", no_ask="0.55", observed_at=t0)
        obs2 = make_test_obs("KX-2", yes_ask="0.40", no_ask="0.55", observed_at=t0 + timedelta(seconds=5))

        file_path = tmp_path / "raw_stream.jsonl"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(observation_to_dict(obs1)) + "\n")
            f.write("CORRUPTED_JSON_NOT_VALID\n")  # Malformed line
            f.write(json.dumps(observation_to_dict(obs1)) + "\n")  # Duplicate line
            f.write(json.dumps(observation_to_dict(obs2)) + "\n")

        dataset = HistoricalDataset.from_file(file_path)
        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        # Audit report metrics
        assert report.audit_report.total_raw_records == 4
        assert report.audit_report.malformed_records_count == 1
        assert report.audit_report.duplicate_records_count == 1
        assert report.audit_report.valid_records_count == 2

        # Funnel metrics
        assert report.funnel_raw_ingestion_records == 4
        assert report.funnel_valid_parsed_observations == 2

        # Text summary check
        summary = report.summary()
        assert "Total Raw Ingestion Records:       4" in summary
        assert "Valid Parsed Observations:         2" in summary

        # JSON dictionary export check
        d = report.to_dict()
        assert d["section_4_qualification_funnel"]["total_raw_ingestion_records"] == 4
        assert d["section_4_qualification_funnel"]["raw_ingestion_records"] == 4
        assert d["section_4_qualification_funnel"]["valid_parsed_observations"] == 2

    def test_cumulative_snapshot_depth_explicit_labeling(self):
        """F-05: Depth clearly identified as cumulative across repeated snapshots, not unique liquidity."""
        t0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        # Three repeated polls of same market with 20 contracts depth each
        obs1 = make_test_obs("KX-DEPTH", yes_qty="10.0", no_qty="10.0", observed_at=t0)
        obs2 = make_test_obs("KX-DEPTH", yes_qty="10.0", no_qty="10.0", observed_at=t0 + timedelta(seconds=5))
        obs3 = make_test_obs("KX-DEPTH", yes_qty="10.0", no_qty="10.0", observed_at=t0 + timedelta(seconds=10))

        dataset = HistoricalDataset.from_observations([obs1, obs2, obs3])
        validator = V7HistoricalValidator()
        report = validator.validate(dataset)

        summary = report.summary()
        # Section 6 label
        expected_sec6 = (
            "Total Observable Counterparty Bids:60.0 contracts "
            "(cumulative across repeated snapshot polls; not unique simultaneous liquidity)"
        )
        assert expected_sec6 in summary

        # Section 2 label
        expected_sec2 = (
            "Total Cumulative Bid Depth:        60.0 contracts "
            "(cumulative across repeated snapshot polls; not unique simultaneous liquidity)"
        )
        assert expected_sec2 in summary

        # Audit report summary label
        expected_audit = (
            "Total Observable Depth:        60.0 contracts "
            "(cumulative across repeated snapshot polls; not unique simultaneous liquidity)"
        )
        assert expected_audit in report.audit_report.summary()

        # JSON exports must contain the explanatory cumulative note
        d = report.to_dict()
        assert d["section_6_liquidity_analysis"]["total_bid_depth_cumulative_note"] == (
            "Cumulative across repeated snapshot polls; not unique simultaneous liquidity"
        )
        assert d["section_2_evidence_quality"]["total_bid_depth_cumulative_note"] == (
            "Cumulative across repeated snapshot polls; not unique simultaneous liquidity"
        )
