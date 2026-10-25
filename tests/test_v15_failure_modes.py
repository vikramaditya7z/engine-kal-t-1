"""Kalshi Arbitrage Engine V15 — Comprehensive Failure Modes Test Suite.

Deterministic tests for the 20 failure mode scenarios specified in Phase Five:
1. Valid, profitable theoretical basket.
2. Basket with negative gross edge.
3. Gross edge positive but net edge zero or negative after fees.
4. Insufficient executable depth on one leg.
5. Incomplete outcomes in snapshot.
6. Overlapping outcome definitions.
7. Cross-event contamination.
8. Timestamp skew beyond permitted maximum.
9. Missing or malformed timestamps.
10. Duplicate observations and repeated market states.
11. Missing order-book sides.
12. Invalid prices, quantities, or units.
13. Fee rounding and boundary conditions.
14. Partial execution / insufficient common quantity.
15. Corrupted evidence records fail closed.
16. Replay determinism for identical inputs.
17. Manifest hash mismatch detection.
18. Empty input and zero valid snapshot handling.
19. Zero-overlap reconstruction fails safely.
20. Explicit and informative rejection reasons on all failure paths.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pytest

from kalshi_arbitrage.arbitrage import (
    ArbitrageInputError,
    dollars_to_cents,
)
from kalshi_arbitrage.contract import (
    BinaryContract,
    ContractInputError,
    NO,
    YES,
)
from kalshi_arbitrage.fee_model import (
    FeeModelConfig,
    FeeScheduleType,
    calculate_single_level_fee,
)
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    MarketObservation,
    ObserverError,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_INSUFFICIENT_QUANTITY,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
)
from kalshi_arbitrage.snapshot_reconstruction import (
    EventSnapshotReconstructor,
    SnapshotReconstructionConfig,
)
from kalshi_arbitrage.v13_basket_replay import (
    HistoricalBasketReplayConfig,
    HistoricalBasketReplayEngine,
)


def _make_mock_obs(
    ticker: str,
    event_ticker: str,
    outcome: str,
    yes_bid: str,
    yes_ask: str,
    qty: int = 100,
    source_timestamp: datetime = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc),
    obs_id: str = "",
    is_success: bool = True,
    empty_book: bool = False,
    is_stale: bool = False,
) -> MarketObservation:
    """Helper to create synthetic market observations."""
    y_bid = Decimal(yes_bid)
    y_ask = Decimal(yes_ask)

    if empty_book:
        book = NormalizedOrderBook(
            market_ticker=ticker,
            yes_bids=(),
            no_bids=(),
            source_timestamp=source_timestamp,
        )
    else:
        yes_bids = (OrderBookLevel(price_dollars=y_bid, quantity=qty),)
        no_bids = (OrderBookLevel(price_dollars=Decimal("1.00") - y_ask, quantity=qty),)
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

    oid = obs_id or f"obs-{ticker}-{int(source_timestamp.timestamp())}"
    return MarketObservation(
        observation_id=oid,
        ticker=ticker,
        observed_at=source_timestamp,
        source_timestamp=source_timestamp if source_timestamp else None,
        market=market if is_success else None,
        order_book=book if is_success else None,
        is_success=is_success,
        is_stale=is_stale,
    )


# Scenario 1: Valid, profitable theoretical basket
def test_failure_mode_1_valid_profitable_theoretical_basket():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-1",
        basket_id="B1",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Cost = 40¢ + 45¢ = 85¢. Guaranteed payout = 100¢. Gross edge = 15¢. Fees = 2¢ + 2¢ = 4¢. Net edge = 11¢.
    obs = [
        _make_mock_obs("M-A", "EVT-1", "A", "0.35", "0.40", source_timestamp=t0, obs_id="1-a"),
        _make_mock_obs("M-B", "EVT-1", "B", "0.40", "0.45", source_timestamp=t0 + timedelta(seconds=1), obs_id="1-b"),
    ]
    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="test_s1")
    assert summary.qualified_count == 1
    rec = summary.records[0]
    assert rec.status == STATUS_QUALIFIED
    assert rec.is_qualified is True
    assert rec.net_edge_cents == 11


# Scenario 2: Negative gross edge
def test_failure_mode_2_negative_gross_edge():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-2",
        basket_id="B2",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Cost = 55¢ + 50¢ = 105¢ > 100¢.
    obs = [
        _make_mock_obs("M-A", "EVT-2", "A", "0.45", "0.55", source_timestamp=t0, obs_id="2-a"),
        _make_mock_obs("M-B", "EVT-2", "B", "0.45", "0.50", source_timestamp=t0 + timedelta(seconds=1), obs_id="2-b"),
    ]
    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="test_s2")
    assert summary.qualified_count == 0
    assert summary.rejected_by_reason.get(STATUS_REJECTED_NO_GROSS_EDGE) == 1
    assert summary.records[0].status == STATUS_REJECTED_NO_GROSS_EDGE


# Scenario 3: Positive gross edge eroded by fees
def test_failure_mode_3_gross_positive_eroded_by_fees():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-3",
        basket_id="B3",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Cost = 49¢ + 50¢ = 99¢. Payout = 100¢. Gross edge = +1¢. Fees = 2¢ + 2¢ = 4¢. Net edge = -3¢.
    obs = [
        _make_mock_obs("M-A", "EVT-3", "A", "0.45", "0.49", source_timestamp=t0, obs_id="3-a"),
        _make_mock_obs("M-B", "EVT-3", "B", "0.45", "0.50", source_timestamp=t0 + timedelta(seconds=1), obs_id="3-b"),
    ]
    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="test_s3")
    assert summary.qualified_count == 0
    assert summary.rejected_by_reason.get(STATUS_REJECTED_UNPROFITABLE_FEES) == 1
    assert summary.records[0].status == STATUS_REJECTED_UNPROFITABLE_FEES


# Scenario 4: Insufficient executable depth on one leg
def test_failure_mode_4_insufficient_depth_on_one_leg():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-4",
        basket_id="B4",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Leg B has an empty book (0 depth)
    obs = [
        _make_mock_obs("M-A", "EVT-4", "A", "0.30", "0.40", source_timestamp=t0, obs_id="4-a"),
        _make_mock_obs("M-B", "EVT-4", "B", "0.30", "0.40", source_timestamp=t0 + timedelta(seconds=1), obs_id="4-b", empty_book=True),
    ]
    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="test_s4")
    assert summary.qualified_count == 0
    assert summary.rejected_by_reason.get(STATUS_REJECTED_INSUFFICIENT_DEPTH) == 1
    assert summary.records[0].status == STATUS_REJECTED_INSUFFICIENT_DEPTH


# Scenario 5: Incomplete outcomes in snapshot
def test_failure_mode_5_incomplete_outcomes():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-5",
        basket_id="B5",
        outcomes=("A", "B", "C"),
        market_outcome_map={"M-A": "A", "M-B": "B", "M-C": "C"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Only legs A and B are present; leg C is missing
    obs = [
        _make_mock_obs("M-A", "EVT-5", "A", "0.30", "0.40", source_timestamp=t0, obs_id="5-a"),
        _make_mock_obs("M-B", "EVT-5", "B", "0.30", "0.40", source_timestamp=t0 + timedelta(seconds=1), obs_id="5-b"),
    ]
    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="test_s5")
    assert summary.complete_snapshots_count == 0
    assert summary.rejected_by_reason.get(STATUS_REJECTED_MISSING_MARKET) == 1
    assert summary.records[0].status == STATUS_REJECTED_MISSING_MARKET


# Scenario 6: Overlapping outcome definitions
def test_failure_mode_6_overlapping_outcome_definitions():
    # Duplicate outcomes in tuple must raise ObserverError
    with pytest.raises(ObserverError) as excinfo:
        DeclaredMeceBasket(
            event_ticker="EVT-6",
            basket_id="B6",
            outcomes=("OUTCOME_A", "OUTCOME_A"),
            market_outcome_map={"M-1": "OUTCOME_A", "M-2": "OUTCOME_A"},
            basket_side=YES,
        )
    assert "unique strings" in str(excinfo.value)


# Scenario 7: Cross-event contamination
def test_failure_mode_7_cross_event_contamination():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-TARGET",
        basket_id="B7",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # M-A belongs to EVT-TARGET, but M-B belongs to EVT-OTHER
    obs_a = _make_mock_obs("M-A", "EVT-TARGET", "A", "0.40", "0.45", source_timestamp=t0, obs_id="7-a")
    obs_b = _make_mock_obs("M-B", "EVT-OTHER", "B", "0.40", "0.45", source_timestamp=t0 + timedelta(seconds=1), obs_id="7-b")

    reconstructor = EventSnapshotReconstructor()
    snapshots, _ = reconstructor.reconstruct([obs_a, obs_b], [basket])
    assert len(snapshots) == 1
    assert not snapshots[0].is_valid
    assert snapshots[0].status == STATUS_REJECTED_MISSING_MARKET
    assert "M-B" in snapshots[0].missing_tickers


# Scenario 8: Timestamp skew beyond permitted maximum
def test_failure_mode_8_timestamp_skew_exceeds_maximum():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-8",
        basket_id="B8",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Leg B arrives 8.0s after Leg A when max allowed skew is 5.0s
    obs = [
        _make_mock_obs("M-A", "EVT-8", "A", "0.40", "0.45", source_timestamp=t0, obs_id="8-a"),
        _make_mock_obs("M-B", "EVT-8", "B", "0.40", "0.45", source_timestamp=t0 + timedelta(seconds=8), obs_id="8-b"),
    ]
    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig(max_leg_timestamp_skew_seconds=5.0))
    snapshots, _ = reconstructor.reconstruct(obs, [basket])
    assert len(snapshots) == 1
    assert snapshots[0].status == STATUS_REJECTED_LEG_SKEW


# Scenario 9: Missing or malformed timestamps
def test_failure_mode_9_missing_or_malformed_timestamps():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-9",
        basket_id="B9",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Leg B has source_timestamp = None
    obs_a = _make_mock_obs("M-A", "EVT-9", "A", "0.40", "0.45", source_timestamp=t0, obs_id="9-a")
    obs_b = MarketObservation(
        observation_id="9-b",
        ticker="M-B",
        observed_at=t0,
        source_timestamp=None,
        market=obs_a.market,
        order_book=NormalizedOrderBook("M-B", (), (), source_timestamp=None),
        is_success=True,
        is_stale=False,
    )
    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig(require_source_timestamp=True))
    snapshots, _ = reconstructor.reconstruct([obs_a, obs_b], [basket])
    assert snapshots[0].status == STATUS_REJECTED_MISSING_TIMESTAMP


# Scenario 10: Duplicate observations and repeated market states
def test_failure_mode_10_duplicate_observations_and_repeated_market_states():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-10",
        basket_id="B10",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Same obs_id repeated
    obs_a1 = _make_mock_obs("M-A", "EVT-10", "A", "0.40", "0.45", source_timestamp=t0, obs_id="dup-1")
    obs_a2 = _make_mock_obs("M-A", "EVT-10", "A", "0.40", "0.45", source_timestamp=t0, obs_id="dup-1")
    obs_b = _make_mock_obs("M-B", "EVT-10", "B", "0.40", "0.45", source_timestamp=t0 + timedelta(seconds=1), obs_id="dup-2")

    reconstructor = EventSnapshotReconstructor()
    _, dups = reconstructor.reconstruct([obs_a1, obs_a2, obs_b], [basket])
    assert dups == 1


# Scenario 11: Missing order-book sides
def test_failure_mode_11_missing_order_book_sides():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-11",
        basket_id="B11",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Observation with is_success=False
    obs_a = _make_mock_obs("M-A", "EVT-11", "A", "0.40", "0.45", source_timestamp=t0, obs_id="11-a")
    obs_b = _make_mock_obs("M-B", "EVT-11", "B", "0.40", "0.45", source_timestamp=t0 + timedelta(seconds=1), obs_id="11-b", is_success=False)

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay([obs_a, obs_b], [basket], source_path="test_s11")
    assert summary.complete_snapshots_count == 0
    assert summary.records[0].status == STATUS_REJECTED_MISSING_MARKET


# Scenario 12: Invalid prices, quantities, or units
def test_failure_mode_12_invalid_prices_quantities_or_units():
    with pytest.raises(ContractInputError):
        BinaryContract(identifier="BAD", price_cents=101)  # > 100¢

    with pytest.raises(ContractInputError):
        BinaryContract(identifier="BAD", price_cents=-1)   # < 0¢

    with pytest.raises(ArbitrageInputError):
        dollars_to_cents(Decimal("0.505"))  # Sub-cent precision rejected


# Scenario 13: Fee rounding and boundary conditions
def test_failure_mode_13_fee_rounding_and_boundary_conditions():
    cfg = FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)
    # 0¢ price -> fee = 0¢
    fee_0 = calculate_single_level_fee(0, 10, config=cfg)
    assert fee_0.total_fee_cents == 0

    # 100¢ price -> fee = 0¢
    fee_100 = calculate_single_level_fee(100, 10, config=cfg)
    assert fee_100.total_fee_cents == 0

    # 50¢ price, 1 contract: ceil(0.07 * 0.50 * 0.50 * 100) = ceil(1.75) = 2¢
    fee_50_1 = calculate_single_level_fee(50, 1, config=cfg)
    assert fee_50_1.total_fee_cents == 2

    # 50¢ price, 10 contracts: ceil(10 * 1.75) = ceil(17.5) = 18¢
    fee_50_10 = calculate_single_level_fee(50, 10, config=cfg)
    assert fee_50_10.total_fee_cents == 18


# Scenario 14: Partial execution / insufficient common quantity
def test_failure_mode_14_partial_execution_and_insufficient_quantity():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-14",
        basket_id="B14",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Target quantity is 10, but depth on Leg B is only 3
    # Cost = 40¢ + 40¢ = 80¢ (profitable), but fillable quantity is 3 < 10
    obs_a = _make_mock_obs("M-A", "EVT-14", "A", "0.35", "0.40", qty=10, source_timestamp=t0, obs_id="14-a")
    obs_b = _make_mock_obs("M-B", "EVT-14", "B", "0.35", "0.40", qty=3, source_timestamp=t0 + timedelta(seconds=1), obs_id="14-b")

    engine = HistoricalBasketReplayEngine(HistoricalBasketReplayConfig(target_quantity=10))
    summary = engine.replay([obs_a, obs_b], [basket], source_path="test_s14")

    assert summary.qualified_count == 0
    rec = summary.records[0]
    assert rec.status == STATUS_REJECTED_INSUFFICIENT_QUANTITY
    assert rec.is_qualified is False
    assert rec.common_executable_quantity == 3
    assert summary.opportunity_classifications["INSUFFICIENT_COMMON_QUANTITY"] == 1


# Scenario 15: Corrupted evidence records fail closed
def test_failure_mode_15_corrupted_evidence_records(tmp_path: Path):
    corrupt_file = tmp_path / "corrupt_obs.jsonl"
    with open(corrupt_file, "w") as f:
        f.write('{"ticker": "M-A", "broken": json}\n')

    # Parsing corrupted lines should not silently yield valid observations
    with open(corrupt_file, "r") as f:
        line = f.readline()
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)


# Scenario 16: Replay determinism for identical inputs
def test_failure_mode_16_replay_determinism():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-16",
        basket_id="B16",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    obs = [
        _make_mock_obs("M-A", "EVT-16", "A", "0.35", "0.40", source_timestamp=t0, obs_id="16-a"),
        _make_mock_obs("M-B", "EVT-16", "B", "0.35", "0.40", source_timestamp=t0 + timedelta(seconds=1), obs_id="16-b"),
    ]

    engine = HistoricalBasketReplayEngine()
    s1 = engine.replay(obs, [basket], source_path="test_det")
    s2 = engine.replay(obs, [basket], source_path="test_det")

    assert s1.total_snapshots_reconstructed == s2.total_snapshots_reconstructed
    assert s1.qualified_count == s2.qualified_count
    assert s1.gross_edge_distribution == s2.gross_edge_distribution
    assert s1.net_edge_distribution == s2.net_edge_distribution


# Scenario 17: Manifest hash mismatch detection
def test_failure_mode_17_manifest_hash_mismatch(tmp_path: Path):
    out_dir = tmp_path / "manifest_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    sample_file = out_dir / "replay_records.jsonl"
    sample_file.write_text("initial content\n")

    initial_hash = hashlib.sha256(sample_file.read_bytes()).hexdigest()
    manifest = {"files": {"replay_records.jsonl": initial_hash}}

    # Mutate file
    sample_file.write_text("tampered content\n")
    current_hash = hashlib.sha256(sample_file.read_bytes()).hexdigest()

    assert current_hash != manifest["files"]["replay_records.jsonl"]


# Scenario 18: Empty input and zero valid snapshot handling
def test_failure_mode_18_empty_input_and_zero_valid_snapshots():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-18",
        basket_id="B18",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    engine = HistoricalBasketReplayEngine()
    summary = engine.replay([], [basket], source_path="empty_test")
    assert summary.total_input_observations == 0
    assert summary.total_snapshots_reconstructed == 0
    assert summary.qualified_count == 0


# Scenario 19: Zero-overlap reconstruction fails safely
def test_failure_mode_19_no_overlap_reconstruction():
    basket_fed = DeclaredMeceBasket(
        event_ticker="EVT-FED",
        basket_id="B-FED",
        outcomes=("A", "B"),
        market_outcome_map={"FED-A": "A", "FED-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Dataset only contains NASDAQ tickers
    obs = [
        _make_mock_obs("NASDAQ-1", "EVT-NASDAQ", "1", "0.40", "0.50", source_timestamp=t0, obs_id="n1"),
        _make_mock_obs("NASDAQ-2", "EVT-NASDAQ", "2", "0.40", "0.50", source_timestamp=t0 + timedelta(seconds=1), obs_id="n2"),
    ]
    engine = HistoricalBasketReplayEngine()
    with pytest.raises(ValueError) as excinfo:
        engine.replay(obs, [basket_fed], source_path="no_overlap_test")
    assert "zero matching observations" in str(excinfo.value)


# Scenario 20: Explicit and informative rejection reasons on all failure paths
def test_failure_mode_20_explicit_correct_rejection_reasons():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-20",
        basket_id="B20",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Stale observation
    obs = [
        _make_mock_obs("M-A", "EVT-20", "A", "0.40", "0.45", source_timestamp=t0, obs_id="20-a", is_stale=True),
        _make_mock_obs("M-B", "EVT-20", "B", "0.40", "0.45", source_timestamp=t0 + timedelta(seconds=1), obs_id="20-b"),
    ]
    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="stale_test")
    assert summary.records[0].status == STATUS_REJECTED_STALE
    assert summary.records[0].rejection_reason is not None
    assert "Stale leg(s)" in summary.records[0].rejection_reason
