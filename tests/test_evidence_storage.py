"""Unit tests for V5 EvidenceStore, schema versioning, corruption handling, and atomic writes."""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import pytest

from kalshi_arbitrage import (
    ArbitrageOpportunity,
    CorruptedEvidenceError,
    EVIDENCE_SCHEMA_VERSION,
    EvaluatedOpportunity,
    EvidenceStore,
    MarketObservation,
    NormalizedMarket,
    NormalizedOrderBook,
    OPPORTUNITY_BINARY_PARITY,
    OrderBookLevel,
    STATUS_QUALIFIED,
    YES,
    evaluate_binary_parity_execution,
    replay_recorded_evidence,
)


def make_sample_observation(ticker: str = "KX-TEST", price: str = "0.40") -> MarketObservation:
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    market = NormalizedMarket(
        ticker=ticker,
        event_ticker="EV-1",
        market_type="binary",
        status="open",
        title=f"Market {ticker}",
        updated_time=now,
        yes_ask_dollars=Decimal(price),
        no_ask_dollars=Decimal("0.55"),
    )
    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(Decimal("0.45"), Decimal("10")),),
        no_bids=(OrderBookLevel(Decimal("0.60"), Decimal("10")),),
    )
    return MarketObservation(
        observation_id="obs-12345",
        ticker=ticker,
        observed_at=now,
        source_timestamp=now,
        market=market,
        order_book=book,
        is_success=True,
        is_stale=False,
    )


def make_sample_opportunity(obs: MarketObservation) -> EvaluatedOpportunity:
    pricing = evaluate_binary_parity_execution(
        obs.order_book,
        requested_quantity=1,
        fee_per_contract_cents=1,
    )
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
    return EvaluatedOpportunity(
        observation_id=obs.observation_id,
        opportunity_id=f"BP:{obs.ticker}",
        strategy_type=OPPORTUNITY_BINARY_PARITY,
        observed_at=obs.observed_at,
        source_timestamps={obs.ticker: obs.source_timestamp},
        leg_timestamp_skew_seconds=0.0,
        market_tickers=(obs.ticker,),
        candidate_opportunity=arb,
        pricing_result=pricing,
        status=STATUS_QUALIFIED,
        rejection_reason=None,
        fee_per_contract_cents=1,
        is_qualified=True,
    )


def test_record_and_load_observation(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    obs = make_sample_observation()

    store.record_observation(obs)
    loaded = store.load_observations()

    assert len(loaded) == 1
    recovered = loaded[0]
    assert recovered.observation_id == obs.observation_id
    assert recovered.ticker == obs.ticker
    assert recovered.is_success is True
    assert recovered.order_book.market_ticker == "KX-TEST"
    assert len(recovered.order_book.yes_bids) == 1
    assert recovered.order_book.yes_bids[0].price_dollars == Decimal("0.45")


def test_record_and_load_opportunity(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    obs = make_sample_observation()
    opp = make_sample_opportunity(obs)

    store.record_opportunity(opp)
    loaded = store.load_opportunities()

    assert len(loaded) == 1
    recovered = loaded[0]
    assert recovered.opportunity_id == "BP:KX-TEST"
    assert recovered.is_qualified is True
    assert recovered.pricing_result is not None
    assert recovered.pricing_result.gross_profit_cents == 5
    assert recovered.pricing_result.net_profit_cents == 3


def test_corrupted_observation_file_raises_error(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    obs_file = tmp_path / "observations.jsonl"
    with open(obs_file, "w", encoding="utf-8") as f:
        f.write("{invalid json line\n")

    with pytest.raises(CorruptedEvidenceError, match="Corrupted observation"):
        store.load_observations()


def test_schema_version_mismatch_raises_error(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    obs_file = tmp_path / "observations.jsonl"
    bad_payload = {
        "schema_version": "99.0",
        "observation_id": "obs-1",
        "ticker": "KX-1",
        "observed_at": "2026-10-09T12:00:00Z",
        "is_success": True,
        "is_stale": False,
    }
    with open(obs_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(bad_payload) + "\n")

    with pytest.raises(CorruptedEvidenceError, match="Unsupported schema version"):
        store.load_observations()


def test_retention_policy_pruning(tmp_path: Path):
    store = EvidenceStore(tmp_path, retention_max_records=3)
    for i in range(5):
        obs = make_sample_observation(ticker=f"KX-{i}")
        store.record_observation(obs)

    loaded = store.load_observations()
    assert len(loaded) == 3
    # Most recent 3 retained
    tickers = [o.ticker for o in loaded]
    assert tickers == ["KX-2", "KX-3", "KX-4"]


def test_replay_recorded_evidence_offline(tmp_path: Path):
    store = EvidenceStore(tmp_path)
    obs = make_sample_observation("KX-REPLAY")
    store.record_observation(obs)

    report = replay_recorded_evidence(
        store,
        initial_cash_cents=100_000,
        default_fee_per_contract_cents=1,
    )

    assert report is not None
    assert report.metrics.opportunities_evaluated == 1
    assert report.metrics.trades_accepted == 1
    assert report.metrics.trades_filled == 1
    assert report.metrics.total_cost_cents == 95
    assert report.portfolio.reconcile().is_reconciled is True
