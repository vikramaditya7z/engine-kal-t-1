"""Unit tests for V5 ObserverMetrics calculation and ObserverReport formatting."""

from datetime import datetime, timezone
from decimal import Decimal

from kalshi_arbitrage import (
    ArbitrageOpportunity,
    EvaluatedOpportunity,
    MarketObservation,
    OPPORTUNITY_BINARY_PARITY,
    ObserverMetrics,
    ObserverReport,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_STALE,
    compute_observer_metrics,
    create_proposed_trade,
    evaluate_binary_parity_execution,
    make_binary_parity_opportunity_id,
    NormalizedOrderBook,
    OrderBookLevel,
    TradeState,
    YES,
)


def make_dummy_book(ticker: str = "KX-1") -> NormalizedOrderBook:
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(Decimal("0.45"), Decimal("10")),),
        no_bids=(OrderBookLevel(Decimal("0.60"), Decimal("10")),),
    )


def test_metrics_calculation_accurate_statistics():
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    book = make_dummy_book()

    # 3 observations: 2 successful (1 fresh, 1 stale), 1 failed
    obs1 = MarketObservation("obs-1", "KX-1", now, now, None, book, is_success=True, is_stale=False)
    obs2 = MarketObservation("obs-2", "KX-2", now, now, None, book, is_success=True, is_stale=True, staleness_reason="stale")
    obs3 = MarketObservation("obs-3", "KX-3", now, None, None, None, is_success=False, is_stale=True, error_message="HTTP 500")

    # Opportunities
    pricing = evaluate_binary_parity_execution(book, 1, fee_per_contract_cents=1)
    arb = ArbitrageOpportunity(
        opportunity_type=OPPORTUNITY_BINARY_PARITY,
        is_arbitrage=True,
        guaranteed_payout_cents=100,
        total_cost_cents=95,
        gross_edge_cents=5,
        payouts_by_outcome={YES: 100, "NO": 100},
        profits_by_outcome={YES: 5, "NO": 5},
        guaranteed_payout_dollars=Decimal("1.00"),
        total_cost_dollars=Decimal("0.95"),
        gross_edge_dollars=Decimal("0.05"),
        is_executable=True,
        is_net_profitable=True,
        net_edge_cents=3,
        net_edge_dollars=Decimal("0.03"),
        estimated_fees_cents=2,
    )

    opp1 = EvaluatedOpportunity(
        observation_id="obs-1",
        opportunity_id="BP:KX-1",
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        observed_at=now,
        source_timestamps={"KX-1": now},
        leg_timestamp_skew_seconds=0.0,
        market_tickers=("KX-1",),
        candidate_opportunity=arb,
        pricing_result=pricing,
        status=STATUS_QUALIFIED,
        rejection_reason=None,
        fee_per_contract_cents=1,
        is_qualified=True,
        is_recurrent=False,
    )

    opp2 = EvaluatedOpportunity(
        observation_id="obs-2",
        opportunity_id="BP:KX-1",
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        observed_at=now,
        source_timestamps={"KX-1": now},
        leg_timestamp_skew_seconds=0.0,
        market_tickers=("KX-1",),
        candidate_opportunity=arb,
        pricing_result=None,
        status=STATUS_REJECTED_STALE,
        rejection_reason="stale",
        fee_per_contract_cents=1,
        is_qualified=False,
        is_recurrent=True,
    )

    metrics = compute_observer_metrics(
        observations=[obs1, obs2, obs3],
        evaluations=[opp1, opp2],
        lifetimes=[5.0, 15.0],
    )

    assert metrics.markets_observed_total == 3
    assert metrics.markets_refreshed_success == 2
    assert metrics.markets_refreshed_failed == 1
    assert metrics.observations_stale == 2
    assert metrics.api_failure_rate == Decimal("1") / Decimal("3")

    assert metrics.candidates_detected_total == 2
    assert metrics.unique_opportunities_count == 1
    assert metrics.repeated_observations_count == 1
    assert metrics.qualified_opportunities_count == 1

    assert "stale" in metrics.rejections_by_reason
    assert metrics.rejections_by_reason["stale"] == 1

    assert metrics.supported_quantities_min == 1
    assert metrics.gross_edge_cents_min == 5
    assert metrics.net_edge_cents_min == 3

    assert metrics.opportunity_lifetimes_min_seconds == 5.0
    assert metrics.opportunity_lifetimes_max_seconds == 15.0
    assert metrics.opportunity_lifetimes_mean_seconds == 10.0


def test_observer_report_summary_formatting():
    metrics = ObserverMetrics(
        markets_observed_total=10,
        markets_refreshed_success=9,
        markets_refreshed_failed=1,
        observations_stale=2,
        candidates_detected_total=4,
        unique_opportunities_count=2,
        repeated_observations_count=2,
        qualified_opportunities_count=2,
        rejections_by_reason={"stale": 1, "depth": 1},
        supported_quantities_min=1,
        supported_quantities_max=5,
        supported_quantities_mean=Decimal("3.0"),
        gross_edge_cents_min=4,
        gross_edge_cents_max=8,
        gross_edge_cents_mean=Decimal("6.0"),
        net_edge_cents_min=2,
        net_edge_cents_max=6,
        net_edge_cents_mean=Decimal("4.0"),
        opportunity_lifetimes_min_seconds=2.0,
        opportunity_lifetimes_max_seconds=10.0,
        opportunity_lifetimes_mean_seconds=6.0,
        paper_trades_submitted=1,
        paper_trades_accepted=1,
        paper_trades_rejected=0,
        paper_trades_filled=1,
        paper_trades_partially_filled=0,
        paper_fees_total_cents=2,
        api_failure_rate=Decimal("0.1"),
    )

    report = ObserverReport(
        metrics=metrics,
        observations_count=10,
        evaluations_count=4,
        paper_portfolio_equity_cents=100_004,
        paper_portfolio_cash_cents=99_905,
    )

    text = report.summary()
    assert "KALSHI ARBITRAGE ENGINE — V5 OBSERVER EVALUATION REPORT" in text
    assert "Total Market Observations:         10" in text
    assert "Successfully Refreshed:            9" in text
    assert "API Failure Rate:                  10.0%" in text
    assert "Depth-Supported Quantity (Min/Mean/Max): 1 / 3.0 / 5" in text
    assert "Gross Edge (Min/Mean/Max):             4¢ / 6.0¢ / 8¢" in text
    assert "Ending Portfolio Book Equity:      100004¢ ($1000.04)" in text
    assert "DISCLAIMER" in text
