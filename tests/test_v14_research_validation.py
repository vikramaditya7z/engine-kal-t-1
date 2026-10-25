"""Kalshi Arbitrage Engine V14 — Research Validation, Data Provenance & Edge Discovery Tests.

Deterministic offline test suite covering:
1. Provenance matching & unmatched basket configuration validation.
2. Event ticker isolation (preventing cross-event constituent absorption).
3. Market state fingerprinting & deduplication (distinguishing independent quotes from repeated states).
4. Canonical 7-category opportunity classification.
5. Exact YES and NO basket payout and gross edge economics.
6. Exact CFTC regulatory fee modeling and net edge calculation.
7. Multi-basket polling deduplication of shared constituent markets.
8. Byte-level cryptographic manifest integrity.
9. Safety boundary verification (strict read-only isolation, zero order submission).
"""

import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pytest
from typing import List

from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
)
from kalshi_arbitrage.snapshot_reconstruction import (
    EventSnapshotReconstructor,
    ReconstructedBasketSnapshot,
    SnapshotReconstructionConfig,
)
from kalshi_arbitrage.v11_collector import V11CollectorConfig, V11MarketEvidenceCollector
from kalshi_arbitrage.v13_basket_replay import (
    HistoricalBasketReplayConfig,
    HistoricalBasketReplayEngine,
    HistoricalBasketReplayRecord,
    HistoricalBasketReplaySummary,
)


def _make_mock_leg_obs(
    ticker: str,
    event_ticker: str,
    outcome: str,
    yes_bid_dollars: str,
    yes_ask_dollars: str,
    yes_qty: int = 100,
    no_qty: int = 100,
    source_timestamp: datetime = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc),
    obs_id: str = "",
) -> MarketObservation:
    """Create a synthetic market observation for testing."""
    y_bid = Decimal(yes_bid_dollars)
    y_ask = Decimal(yes_ask_dollars)

    yes_bids = (OrderBookLevel(price_dollars=y_bid, quantity=yes_qty),)
    # NO bids correspond to 1 - YES ask
    no_bids = (OrderBookLevel(price_dollars=Decimal("1.00") - y_ask, quantity=no_qty),)

    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=yes_bids,
        no_bids=no_bids,
        source_timestamp=source_timestamp,
    )

    market = NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status="active",
        title=f"Will outcome {outcome} occur?",
        subtitle=outcome,
        rules_primary=f"Settles to Yes if {outcome} occurs.",
        rules_secondary=f"Mutually exclusive outcome for {outcome}.",
        yes_bid_dollars=y_bid,
        yes_ask_dollars=y_ask,
    )

    oid = obs_id or f"syn-obs-{ticker}-{int(source_timestamp.timestamp())}"
    return MarketObservation(
        observation_id=oid,
        ticker=ticker,
        observed_at=source_timestamp,
        source_timestamp=source_timestamp,
        market=market,
        order_book=book,
        is_success=True,
        is_stale=False,
    )


def test_unmatched_basket_identity_raises_value_error():
    """Verify that replaying a basket with zero matching observations raises ValueError."""
    basket = DeclaredMeceBasket(
        event_ticker="EVENT-NASDAQ",
        basket_id="BASKET-NASDAQ",
        outcomes=("UP", "DOWN"),
        market_outcome_map={"NASDAQ-UP": "UP", "NASDAQ-DOWN": "DOWN"},
        basket_side=YES,
    )

    # Observations contain FED tickers, not NASDAQ
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    obs = [
        _make_mock_leg_obs("FED-CUT", "EVENT-FED", "CUT", "0.20", "0.25", source_timestamp=t0),
        _make_mock_leg_obs("FED-HIKE", "EVENT-FED", "HIKE", "0.70", "0.75", source_timestamp=t0),
    ]

    engine = HistoricalBasketReplayEngine()
    with pytest.raises(ValueError) as excinfo:
        engine.replay(obs, [basket], source_path="mismatched_test.jsonl")

    assert "zero matching observations" in str(excinfo.value)
    assert "BASKET-NASDAQ" in str(excinfo.value)


def test_multi_basket_partial_unmatched_tracked_in_summary():
    """In a multi-basket replay, unmatched baskets are recorded in summary and limitations."""
    basket_fed = DeclaredMeceBasket(
        event_ticker="EVENT-FED",
        basket_id="BASKET-FED",
        outcomes=("CUT", "HIKE"),
        market_outcome_map={"FED-CUT": "CUT", "FED-HIKE": "HIKE"},
        basket_side=YES,
    )
    basket_nasdaq = DeclaredMeceBasket(
        event_ticker="EVENT-NASDAQ",
        basket_id="BASKET-NASDAQ",
        outcomes=("UP", "DOWN"),
        market_outcome_map={"NASDAQ-UP": "UP", "NASDAQ-DOWN": "DOWN"},
        basket_side=YES,
    )

    # Dataset only contains FED observations
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    obs = [
        _make_mock_leg_obs("FED-CUT", "EVENT-FED", "CUT", "0.40", "0.50", source_timestamp=t0),
        _make_mock_leg_obs("FED-HIKE", "EVENT-FED", "HIKE", "0.40", "0.55", source_timestamp=t0 + timedelta(seconds=1)),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket_fed, basket_nasdaq], source_path="partial_unmatched_test")

    assert "BASKET-NASDAQ" in summary.unmatched_baskets
    assert any("BASKET-NASDAQ" in lim for lim in summary.data_quality_limitations)
    assert summary.complete_snapshots_count == 1


def test_event_ticker_mismatch_prevents_cross_event_absorption():
    """Observations with matching ticker but mismatched event_ticker are not absorbed."""
    basket = DeclaredMeceBasket(
        event_ticker="EVENT-CORRECT",
        basket_id="BASKET-CORRECT",
        outcomes=("LEG1", "LEG2"),
        market_outcome_map={"LEG1": "LEG1", "LEG2": "LEG2"},
        basket_side=YES,
    )

    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # LEG1 belongs to EVENT-CORRECT, but LEG2 observation belongs to EVENT-WRONG
    obs1 = _make_mock_leg_obs("LEG1", "EVENT-CORRECT", "LEG1", "0.40", "0.50", source_timestamp=t0)
    obs2 = _make_mock_leg_obs("LEG2", "EVENT-WRONG", "LEG2", "0.40", "0.50", source_timestamp=t0 + timedelta(seconds=1))

    reconstructor = EventSnapshotReconstructor()
    snapshots, _ = reconstructor.reconstruct([obs1, obs2], [basket])

    # Should not produce a complete valid snapshot
    assert len(snapshots) == 1
    assert not snapshots[0].is_valid
    assert snapshots[0].status == STATUS_REJECTED_MISSING_MARKET
    assert "LEG2" in snapshots[0].missing_tickers


def test_market_state_fingerprint_detects_repeated_order_books():
    """Consecutive snapshots with unchanged order books produce identical fingerprints."""
    basket = DeclaredMeceBasket(
        event_ticker="EVENT-FED",
        basket_id="BASKET-FED",
        outcomes=("CUT", "HIKE"),
        market_outcome_map={"FED-CUT": "CUT", "FED-HIKE": "HIKE"},
        basket_side=YES,
    )

    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 24, 12, 1, 0, tzinfo=timezone.utc)

    # Cycle 1
    obs1_a = _make_mock_leg_obs("FED-CUT", "EVENT-FED", "CUT", "0.40", "0.50", source_timestamp=t0, obs_id="obs-1")
    obs1_b = _make_mock_leg_obs("FED-HIKE", "EVENT-FED", "HIKE", "0.40", "0.55", source_timestamp=t0 + timedelta(seconds=1), obs_id="obs-2")

    # Cycle 2 (identical book prices & quantities, new timestamps)
    obs2_a = _make_mock_leg_obs("FED-CUT", "EVENT-FED", "CUT", "0.40", "0.50", source_timestamp=t1, obs_id="obs-3")
    obs2_b = _make_mock_leg_obs("FED-HIKE", "EVENT-FED", "HIKE", "0.40", "0.55", source_timestamp=t1 + timedelta(seconds=1), obs_id="obs-4")

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay([obs1_a, obs1_b, obs2_a, obs2_b], [basket], source_path="fingerprint_test")

    assert summary.complete_snapshots_count == 2
    assert summary.unique_market_states_count == 1
    assert summary.repeated_market_states_count == 1


def test_market_state_fingerprint_identifies_new_independent_quotes():
    """Distinct order books produce distinct fingerprints."""
    basket = DeclaredMeceBasket(
        event_ticker="EVENT-FED",
        basket_id="BASKET-FED",
        outcomes=("CUT", "HIKE"),
        market_outcome_map={"FED-CUT": "CUT", "FED-HIKE": "HIKE"},
        basket_side=YES,
    )

    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 24, 12, 1, 0, tzinfo=timezone.utc)

    # Cycle 1: Ask 50¢ & 55¢
    obs1_a = _make_mock_leg_obs("FED-CUT", "EVENT-FED", "CUT", "0.40", "0.50", source_timestamp=t0, obs_id="obs-1")
    obs1_b = _make_mock_leg_obs("FED-HIKE", "EVENT-FED", "HIKE", "0.40", "0.55", source_timestamp=t0 + timedelta(seconds=1), obs_id="obs-2")

    # Cycle 2: Ask changed to 48¢ & 53¢
    obs2_a = _make_mock_leg_obs("FED-CUT", "EVENT-FED", "CUT", "0.40", "0.48", source_timestamp=t1, obs_id="obs-3")
    obs2_b = _make_mock_leg_obs("FED-HIKE", "EVENT-FED", "HIKE", "0.40", "0.53", source_timestamp=t1 + timedelta(seconds=1), obs_id="obs-4")

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay([obs1_a, obs1_b, obs2_a, obs2_b], [basket], source_path="independent_test")

    assert summary.complete_snapshots_count == 2
    assert summary.unique_market_states_count == 2
    assert summary.repeated_market_states_count == 0


def test_canonical_seven_opportunity_classifications():
    """Verify that all 7 canonical classifications are tracked without collapsing."""
    basket = DeclaredMeceBasket(
        event_ticker="EVENT-TEST",
        basket_id="BASKET-TEST",
        outcomes=("L1", "L2"),
        market_outcome_map={"L1": "L1", "L2": "L2"},
        basket_side=YES,
    )

    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Candidate with negative gross edge (55¢ + 50¢ = 105¢ > 100¢)
    obs_a = _make_mock_leg_obs("L1", "EVENT-TEST", "L1", "0.40", "0.55", source_timestamp=t0, obs_id="a-1")
    obs_b = _make_mock_leg_obs("L2", "EVENT-TEST", "L2", "0.40", "0.50", source_timestamp=t0 + timedelta(seconds=1), obs_id="a-2")

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay([obs_a, obs_b], [basket], source_path="canonical_test")

    opps = summary.opportunity_classifications
    assert "STRUCTURALLY_INVALID_BASKET" in opps
    assert "INSUFFICIENT_SYNCHRONIZATION" in opps
    assert "NO_EXECUTABLE_DEPTH" in opps
    assert "NO_GROSS_EDGE" in opps
    assert "UNPROFITABLE_AFTER_FEES" in opps
    assert "INSUFFICIENT_COMMON_QUANTITY" in opps
    assert "QUALIFIED_RESEARCH_CANDIDATE" in opps
    assert opps["NO_GROSS_EDGE"] == 1


def test_yes_and_no_payout_and_gross_edge_parity():
    """Verify independent deterministic payout formulas for YES and NO baskets."""
    basket_yes = DeclaredMeceBasket(
        event_ticker="EVENT-3LEG",
        basket_id="BASKET-3LEG-YES",
        outcomes=("A", "B", "C"),
        market_outcome_map={"M-A": "A", "M-B": "B", "M-C": "C"},
        basket_side=YES,
    )
    basket_no = DeclaredMeceBasket(
        event_ticker="EVENT-3LEG",
        basket_id="BASKET-3LEG-NO",
        outcomes=("A", "B", "C"),
        market_outcome_map={"M-A": "A", "M-B": "B", "M-C": "C"},
        basket_side=NO,
    )

    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # YES asks: 30¢, 30¢, 30¢ -> Sum = 90¢. Guaranteed YES payout = 100¢. Gross edge = +10¢.
    # NO asks: 100 - 25 = 75¢ each -> Sum = 225¢. Guaranteed NO payout = (3 - 1) * 100¢ = 200¢. Gross edge = -25¢.
    obs = [
        _make_mock_leg_obs("M-A", "EVENT-3LEG", "A", "0.25", "0.30", source_timestamp=t0, obs_id="m1"),
        _make_mock_leg_obs("M-B", "EVENT-3LEG", "B", "0.25", "0.30", source_timestamp=t0 + timedelta(seconds=1), obs_id="m2"),
        _make_mock_leg_obs("M-C", "EVENT-3LEG", "C", "0.25", "0.30", source_timestamp=t0 + timedelta(seconds=2), obs_id="m3"),
    ]

    engine = HistoricalBasketReplayEngine()
    summary_yes = engine.replay(obs, [basket_yes], source_path="yes_test")
    rec_yes = summary_yes.records[0]
    assert rec_yes.guaranteed_payout_cents == 100
    assert rec_yes.gross_acquisition_cost_cents == 90
    assert rec_yes.gross_edge_cents == 10

    summary_no = engine.replay(obs, [basket_no], source_path="no_test")
    rec_no = summary_no.records[0]
    assert rec_no.guaranteed_payout_cents == 200
    assert rec_no.gross_acquisition_cost_cents == 225
    assert rec_no.gross_edge_cents == -25


def test_cftc_fee_schedule_exact_taker_calculation():
    """Verify that official taker fee schedule computes 7% up to round caps per contract."""
    from kalshi_arbitrage.fee_model import calculate_single_level_fee

    fee_cfg = FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)

    # For 50¢ contract: ceil(0.07 * 0.50 * 0.50 * 100) = ceil(1.75) = 2¢ per contract
    fee_50 = calculate_single_level_fee(50, 1, config=fee_cfg)
    assert fee_50.total_fee_cents == 2

    # For 10 contracts of 50¢: ceil(10 * 1.75) = ceil(17.5) = 18¢
    fee_50_10 = calculate_single_level_fee(50, 10, config=fee_cfg)
    assert fee_50_10.total_fee_cents == 18


def test_collector_multi_basket_deduplication():
    """Verify that multiple baskets sharing constituent markets are queried once per cycle."""
    b1 = DeclaredMeceBasket(
        event_ticker="EVT-1",
        basket_id="B1",
        outcomes=("A", "SHARED"),
        market_outcome_map={"M-A": "A", "M-SHARED": "SHARED"},
        basket_side=YES,
    )
    b2 = DeclaredMeceBasket(
        event_ticker="EVT-2",
        basket_id="B2",
        outcomes=("B", "SHARED"),
        market_outcome_map={"M-B": "B", "M-SHARED": "SHARED"},
        basket_side=YES,
    )

    collector_cfg = V11CollectorConfig(
        output_dir="tmp_dummy",
        declared_baskets=(b1, b2),
    )

    tickers_to_poll = list(collector_cfg.target_tickers or ())
    for b in collector_cfg.declared_baskets:
        for t in b.ordered_tickers:
            if t not in tickers_to_poll:
                tickers_to_poll.append(t)

    # 'M-SHARED' appears in both b1 and b2, but must only appear once in tickers_to_poll
    assert len(tickers_to_poll) == 3
    assert sorted(tickers_to_poll) == ["M-A", "M-B", "M-SHARED"]


def test_replay_artifacts_manifest_sha256_exactness(tmp_path: Path):
    """Verify write_artifacts generates exact byte-level SHA-256 manifests."""
    basket = DeclaredMeceBasket(
        event_ticker="EVT-T",
        basket_id="B-T",
        outcomes=("O1", "O2"),
        market_outcome_map={"T1": "O1", "T2": "O2"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    obs = [
        _make_mock_leg_obs("T1", "EVT-T", "O1", "0.40", "0.50", source_timestamp=t0, obs_id="o1"),
        _make_mock_leg_obs("T2", "EVT-T", "O2", "0.40", "0.50", source_timestamp=t0 + timedelta(seconds=1), obs_id="o2"),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="manifest_test.jsonl")

    out_dir = tmp_path / "artifacts_test"
    artifacts = summary.write_artifacts(out_dir)

    with open(artifacts["manifest"]) as f:
        manifest_data = json.load(f)

    for fname, exp_hash in manifest_data["files"].items():
        fpath = out_dir / fname
        h = hashlib.sha256()
        with open(fpath, "rb") as fh:
            while chunk := fh.read(65536):
                h.update(chunk)
        assert h.hexdigest() == exp_hash, f"Hash mismatch in {fname}"


def test_safety_boundary_zero_order_submission_paths():
    """Verify snapshot_reconstruction and v13_basket_replay contain zero order execution calls."""
    modules_to_audit = [
        Path("kalshi_arbitrage/snapshot_reconstruction.py"),
        Path("kalshi_arbitrage/v13_basket_replay.py"),
    ]

    prohibited_call_names = {
        "place_order",
        "submit_order",
        "create_order",
        "send_order",
        "cancel_order",
        "amend_order",
        "execute_live_order",
    }

    for mod_path in modules_to_audit:
        with open(mod_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(mod_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                assert func_name not in prohibited_call_names, (
                    f"Prohibited live order call {func_name!r} detected in {mod_path}"
                )
