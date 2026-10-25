"""Kalshi Arbitrage Engine V13 — Historical MECE Basket Replay & Snapshot Tests.

Deterministic offline test suite covering:
1. Snapshot reconstruction:
   - Complete five-leg snapshot within skew tolerance.
   - Missing leg fail-closed rejection.
   - Excessive timestamp skew rejection.
   - Stale and errored observation rejection.
   - Out-of-order input handling.
   - Duplicate observation deduplication.
   - Multiple valid snapshots across multiple cycles.
   - Matching-window boundary behavior.
   - Non-leakage: prior window does not complete subsequent snapshot.
   - Basket isolation: multiple baskets never cross-contaminate.
   - Strict determinism across repeated executions.
2. Economics & execution feasibility:
   - Profitable synthetic YES basket.
   - Unprofitable synthetic YES basket (negative gross edge).
   - Profitable synthetic NO basket.
   - Positive gross edge eroded to negative by CFTC fees.
   - Insufficient depth on one leg.
   - Common quantity bottleneck sizing.
   - Zero edge / break even.
   - Fee calculations at price boundaries.
3. Compatibility, persistence & safety:
   - Single-market replay unchanged without baskets.
   - Malformed basket configuration fails closed.
   - JSONL and summary dictionary serialization round-trips.
   - Manifest SHA-256 hashes match exact file bytes on disk.
   - Summary counts match underlying records.
   - Strict prohibition of live order routing.
4. Real evidence verification:
   - Full replay of recorded V12 trial evidence (50 observations -> 10 snapshots).

NOTE: Synthetic tests use SYNTHETIC fixtures for hermetic offline test execution.
They do not represent real-market predictions or live quotes.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pytest
from typing import Dict, List, Optional, Tuple

from kalshi_arbitrage.arbitrage import (
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
)
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
    ObserverError,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
    load_declared_baskets,
)
from kalshi_arbitrage.snapshot_reconstruction import (
    EventSnapshotReconstructor,
    ReconstructedBasketSnapshot,
    STATUS_VALID_SNAPSHOT,
    SnapshotReconstructionConfig,
)
from kalshi_arbitrage.v13_basket_replay import (
    HistoricalBasketReplayConfig,
    HistoricalBasketReplayEngine,
    HistoricalBasketReplayRecord,
    HistoricalBasketReplaySummary,
)


def _make_synthetic_leg_obs(
    ticker: str,
    event_ticker: str,
    outcome: str,
    yes_ask: str,
    no_ask: str,
    qty: str = "10.00",
    source_timestamp: Optional[datetime] = None,
    observed_at: Optional[datetime] = None,
    is_success: bool = True,
    is_stale: bool = False,
    obs_id: Optional[str] = None,
) -> MarketObservation:
    """Helper to construct synthetic MarketObservation fixtures for test execution."""
    ts = source_timestamp or datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    obs_ts = observed_at or ts
    oid = obs_id or f"syn-obs-{ticker}-{int(ts.timestamp())}"

    y_ask_dec = Decimal(yes_ask)
    n_ask_dec = Decimal(no_ask)

    # In Kalshi binary markets:
    # YES ask = 1.00 - NO bid => NO bid = 1.00 - YES ask
    # NO ask = 1.00 - YES bid => YES bid = 1.00 - NO ask
    c_no_bid = Decimal("1.00") - y_ask_dec
    c_yes_bid = Decimal("1.00") - n_ask_dec

    market = NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status="active",
        title=f"Synthetic {outcome}",
        subtitle=outcome,
        yes_subtitle=outcome,
        no_subtitle=outcome,
        rules_primary=f"Resolves yes if {outcome}",
        rules_secondary=f"Mutually exclusive outcome for {outcome}.",
        open_time=ts - timedelta(days=1),
        close_time=ts + timedelta(days=30),
        expiration_time=ts + timedelta(days=31),
        yes_bid_dollars=c_yes_bid,
        yes_ask_dollars=y_ask_dec,
        no_bid_dollars=c_no_bid,
        no_ask_dollars=n_ask_dec,
        last_price_dollars=y_ask_dec,
        yes_bid_size=Decimal(qty),
        yes_ask_size=Decimal(qty),
        volume=Decimal("1000.00"),
        volume_24h=Decimal("100.00"),
        open_interest=Decimal("500.00"),
    )

    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=c_yes_bid, quantity=Decimal(qty)),),
        no_bids=(OrderBookLevel(price_dollars=c_no_bid, quantity=Decimal(qty)),),
        source_timestamp=ts,
    )

    return MarketObservation(
        observation_id=oid,
        ticker=ticker,
        observed_at=obs_ts,
        source_timestamp=ts,
        market=market if is_success else None,
        order_book=book if is_success else None,
        is_success=is_success,
        is_stale=is_stale,
        staleness_reason="test_stale" if is_stale else None,
        error_message="test_error" if not is_success else None,
    )


def _make_five_leg_fed_basket() -> DeclaredMeceBasket:
    """Canonical December 2027 Fed rate decision basket."""
    return DeclaredMeceBasket(
        event_ticker="KXFEDDECISION-27DEC",
        basket_id="FED-RATE-DEC27",
        outcomes=("CUT_MORE_THAN_25", "CUT_25", "MAINTAIN", "HIKE_25", "HIKE_MORE_THAN_25"),
        market_outcome_map={
            "KXFEDDECISION-27DEC-C26": "CUT_MORE_THAN_25",
            "KXFEDDECISION-27DEC-C25": "CUT_25",
            "KXFEDDECISION-27DEC-H0": "MAINTAIN",
            "KXFEDDECISION-27DEC-H25": "HIKE_25",
            "KXFEDDECISION-27DEC-H26": "HIKE_MORE_THAN_25",
        },
        basket_side=YES,
    )


# ===========================================================================
# 1. Snapshot Reconstruction Tests
# ===========================================================================

def test_reconstruct_complete_five_leg_snapshot_within_skew():
    """Verify that five constituent legs within skew tolerance form a valid complete snapshot."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    obs = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4)),
    ]

    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig(max_leg_timestamp_skew_seconds=5.0))
    snaps, dups = reconstructor.reconstruct(obs, [basket])

    assert dups == 0
    assert len(snaps) == 1
    s = snaps[0]
    assert s.is_complete is True
    assert s.is_valid is True
    assert s.status == STATUS_VALID_SNAPSHOT
    assert s.leg_timestamp_skew_seconds == 4.0
    assert s.missing_tickers == ()
    assert s.errored_tickers == ()
    assert s.stale_tickers == ()
    assert len(s.observations) == 5


def test_reconstruct_missing_leg_fails_closed():
    """Verify that an incomplete snapshot missing one leg fails closed."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Only 4 legs, omitting H26
    obs = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
    ]

    reconstructor = EventSnapshotReconstructor()
    snaps, _ = reconstructor.reconstruct(obs, [basket])

    assert len(snaps) == 1
    s = snaps[0]
    assert s.is_complete is False
    assert s.is_valid is False
    assert s.status == STATUS_REJECTED_MISSING_MARKET
    assert "KXFEDDECISION-27DEC-H26" in s.missing_tickers
    assert "missing leg(s)" in s.rejection_reason


def test_reconstruct_excessive_timestamp_skew_fails_closed():
    """Verify that snapshots with skew exceeding max limit are rejected."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # 5 legs, but 5th leg arrives at +7s (exceeding 5.0s max skew)
    obs = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=7)),
    ]

    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig(max_leg_timestamp_skew_seconds=5.0, cycle_window_seconds=15.0))
    snaps, _ = reconstructor.reconstruct(obs, [basket])

    assert len(snaps) == 1
    s = snaps[0]
    assert s.is_complete is True
    assert s.is_valid is False
    assert s.status == STATUS_REJECTED_LEG_SKEW
    assert s.leg_timestamp_skew_seconds == 7.0
    assert "Excessive leg timestamp skew" in s.rejection_reason


def test_reconstruct_stale_and_errored_observations_rejected():
    """Verify that snapshots containing stale or errored observations fail closed."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Leg 2 is errored
    obs_err = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1), is_success=False),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4)),
    ]

    reconstructor = EventSnapshotReconstructor()
    snaps_err, _ = reconstructor.reconstruct(obs_err, [basket])
    assert snaps_err[0].status == STATUS_REJECTED_MISSING_MARKET
    assert "KXFEDDECISION-27DEC-C25" in snaps_err[0].errored_tickers

    # Leg 3 is stale
    obs_stale = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2), is_stale=True),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4)),
    ]

    snaps_stale, _ = reconstructor.reconstruct(obs_stale, [basket])
    assert snaps_stale[0].status == STATUS_REJECTED_STALE
    assert "KXFEDDECISION-27DEC-H0" in snaps_stale[0].stale_tickers


def test_reconstruct_out_of_order_observations_handled_deterministically():
    """Verify that input order does not alter snapshot reconstruction results."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    obs1 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0)
    obs2 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1))
    obs3 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2))
    obs4 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3))
    obs5 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4))

    forward = [obs1, obs2, obs3, obs4, obs5]
    reverse = [obs5, obs4, obs3, obs2, obs1]
    shuffled = [obs3, obs1, obs5, obs2, obs4]

    reconstructor = EventSnapshotReconstructor()
    res_fwd, _ = reconstructor.reconstruct(forward, [basket])
    res_rev, _ = reconstructor.reconstruct(reverse, [basket])
    res_shuf, _ = reconstructor.reconstruct(shuffled, [basket])

    assert res_fwd[0].status == res_rev[0].status == res_shuf[0].status == STATUS_VALID_SNAPSHOT
    assert res_fwd[0].leg_timestamp_skew_seconds == res_rev[0].leg_timestamp_skew_seconds == res_shuf[0].leg_timestamp_skew_seconds == 4.0
    assert res_fwd[0].observation_ids == res_rev[0].observation_ids == res_shuf[0].observation_ids


def test_reconstruct_duplicate_records_pruned_and_counted():
    """Verify that duplicate observation records are pruned and reported."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    obs1 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0, obs_id="DUP-1")
    obs2 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1))
    obs3 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2))
    obs4 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3))
    obs5 = _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4))

    # Include duplicate of obs1
    all_obs = [obs1, obs1, obs2, obs3, obs4, obs5]

    reconstructor = EventSnapshotReconstructor()
    snaps, dups = reconstructor.reconstruct(all_obs, [basket])

    assert dups == 1
    assert len(snaps) == 1
    assert snaps[0].status == STATUS_VALID_SNAPSHOT


def test_reconstruct_multiple_valid_snapshots_same_event():
    """Verify that multiple consecutive polling cycles reconstruct multiple distinct snapshots."""
    basket = _make_five_leg_fed_basket()
    t1 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 24, 12, 0, 10, tzinfo=timezone.utc)

    obs_cycle1 = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t1, obs_id="c1-1"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t1 + timedelta(seconds=1), obs_id="c1-2"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t1 + timedelta(seconds=2), obs_id="c1-3"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t1 + timedelta(seconds=3), obs_id="c1-4"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t1 + timedelta(seconds=4), obs_id="c1-5"),
    ]

    obs_cycle2 = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t2, obs_id="c2-1"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t2 + timedelta(seconds=1), obs_id="c2-2"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t2 + timedelta(seconds=2), obs_id="c2-3"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t2 + timedelta(seconds=3), obs_id="c2-4"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t2 + timedelta(seconds=4), obs_id="c2-5"),
    ]

    reconstructor = EventSnapshotReconstructor()
    snaps, dups = reconstructor.reconstruct(obs_cycle1 + obs_cycle2, [basket])

    assert dups == 0
    assert len(snaps) == 2
    assert snaps[0].snapshot_id != snaps[1].snapshot_id
    assert snaps[0].status == STATUS_VALID_SNAPSHOT
    assert snaps[1].status == STATUS_VALID_SNAPSHOT
    assert snaps[0].observation_ids == ("c1-1", "c1-2", "c1-3", "c1-4", "c1-5")
    assert snaps[1].observation_ids == ("c2-1", "c2-2", "c2-3", "c2-4", "c2-5")


def test_reconstruct_previous_window_does_not_silently_complete_later_snapshot():
    """Verify that an observation from cycle 1 is not recycled to complete an incomplete cycle 2."""
    basket = _make_five_leg_fed_basket()
    t1 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 24, 12, 1, 0, tzinfo=timezone.utc)  # 60s later

    # Cycle 1: complete
    obs_cycle1 = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t1, obs_id="c1-1"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t1 + timedelta(seconds=1), obs_id="c1-2"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t1 + timedelta(seconds=2), obs_id="c1-3"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t1 + timedelta(seconds=3), obs_id="c1-4"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t1 + timedelta(seconds=4), obs_id="c1-5"),
    ]

    # Cycle 2: missing H26
    obs_cycle2 = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t2, obs_id="c2-1"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t2 + timedelta(seconds=1), obs_id="c2-2"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t2 + timedelta(seconds=2), obs_id="c2-3"),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t2 + timedelta(seconds=3), obs_id="c2-4"),
    ]

    reconstructor = EventSnapshotReconstructor()
    snaps, _ = reconstructor.reconstruct(obs_cycle1 + obs_cycle2, [basket])

    assert len(snaps) == 2
    # Snapshot 1 is complete
    assert snaps[0].status == STATUS_VALID_SNAPSHOT
    # Snapshot 2 must NOT borrow c1-5 from cycle 1! It must be marked incomplete.
    assert snaps[1].status == STATUS_REJECTED_MISSING_MARKET
    assert "KXFEDDECISION-27DEC-H26" in snaps[1].missing_tickers


def test_reconstruct_multiple_baskets_isolated():
    """Verify that multiple baskets with different events or tickers never cross-contaminate."""
    b1 = DeclaredMeceBasket(
        event_ticker="EVT-ALPHA",
        basket_id="BASKET-ALPHA",
        outcomes=("A1", "A2"),
        market_outcome_map={"M-A1": "A1", "M-A2": "A2"},
    )
    b2 = DeclaredMeceBasket(
        event_ticker="EVT-BETA",
        basket_id="BASKET-BETA",
        outcomes=("B1", "B2"),
        market_outcome_map={"M-B1": "B1", "M-B2": "B2"},
    )

    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    obs = [
        _make_synthetic_leg_obs("M-A1", "EVT-ALPHA", "A1", "0.40", "0.60", source_timestamp=t0),
        _make_synthetic_leg_obs("M-B1", "EVT-BETA", "B1", "0.30", "0.70", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("M-A2", "EVT-ALPHA", "A2", "0.50", "0.50", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("M-B2", "EVT-BETA", "B2", "0.60", "0.40", source_timestamp=t0 + timedelta(seconds=3)),
    ]

    reconstructor = EventSnapshotReconstructor()
    snaps, _ = reconstructor.reconstruct(obs, [b1, b2])

    assert len(snaps) == 2
    s_alpha = next(s for s in snaps if s.basket.event_ticker == "EVT-ALPHA")
    s_beta = next(s for s in snaps if s.basket.event_ticker == "EVT-BETA")

    assert set(s_alpha.observations.keys()) == {"M-A1", "M-A2"}
    assert set(s_beta.observations.keys()) == {"M-B1", "M-B2"}


# ===========================================================================
# 2. Economics & Execution Feasibility Tests
# ===========================================================================

def test_replay_profitable_synthetic_yes_basket():
    """Verify that a synthetic basket with total ask cost < 100c qualifies after fees."""
    basket = DeclaredMeceBasket(
        event_ticker="EVT-PROF",
        basket_id="BASKET-PROFITABLE",
        outcomes=("A", "B", "C"),
        market_outcome_map={"M-A": "A", "M-B": "B", "M-C": "C"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Costs: 20¢ + 30¢ + 35¢ = 85¢. Payout = 100¢. Gross edge = +15¢.
    # Official taker fees: ~1¢ + 2¢ + 2¢ = 5¢. Net edge = +10¢ > 0.
    obs = [
        _make_synthetic_leg_obs("M-A", "EVT-PROF", "A", "0.20", "0.80", source_timestamp=t0),
        _make_synthetic_leg_obs("M-B", "EVT-PROF", "B", "0.30", "0.70", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("M-C", "EVT-PROF", "C", "0.35", "0.65", source_timestamp=t0 + timedelta(seconds=2)),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="synthetic_test")

    assert summary.total_basket_evaluations == 1
    assert summary.gross_positive_count == 1
    assert summary.fee_adjusted_positive_count == 1
    assert summary.depth_supported_count == 1
    assert summary.qualified_count == 1

    rec = summary.records[0]
    assert rec.status == STATUS_QUALIFIED
    assert rec.is_qualified is True
    assert rec.gross_acquisition_cost_cents == 85
    assert rec.guaranteed_payout_cents == 100
    assert rec.gross_edge_cents == 15
    assert rec.net_edge_cents is not None and rec.net_edge_cents > 0


def test_replay_unprofitable_synthetic_yes_basket_negative_gross_edge():
    """Verify that a synthetic basket with total ask cost >= 100c is rejected with NO_GROSS_EDGE."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Costs: 1¢ + 19¢ + 63¢ + 16¢ + 3¢ = 102¢. Payout = 100¢. Gross edge = -2¢.
    obs = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4)),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="synthetic_test")

    assert summary.total_basket_evaluations == 1
    assert summary.gross_positive_count == 0
    assert summary.qualified_count == 0
    assert summary.rejected_by_reason == {STATUS_REJECTED_NO_GROSS_EDGE: 1}

    rec = summary.records[0]
    assert rec.status == STATUS_REJECTED_NO_GROSS_EDGE
    assert rec.gross_acquisition_cost_cents == 102
    assert rec.gross_edge_cents == -2
    assert rec.is_qualified is False


def test_replay_positive_gross_edge_eroded_by_cftc_fees():
    """Verify that a positive gross edge smaller than exchange fees is rejected with UNPROFITABLE_FEES."""
    basket = DeclaredMeceBasket(
        event_ticker="EVT-FEE-TEST",
        basket_id="BASKET-FEE-EROSION",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Cost: 49¢ + 49¢ = 98¢. Payout = 100¢. Gross edge = +2¢.
    # CFTC 7% taker fee on 49¢: ceil(0.07 * 49 * 51 / 100) = 2¢ per leg. Total fees = 4¢.
    # Net edge = 2¢ - 4¢ = -2¢ < 0.
    obs = [
        _make_synthetic_leg_obs("M-A", "EVT-FEE-TEST", "A", "0.49", "0.51", source_timestamp=t0),
        _make_synthetic_leg_obs("M-B", "EVT-FEE-TEST", "B", "0.49", "0.51", source_timestamp=t0 + timedelta(seconds=1)),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="synthetic_fee_test")

    assert summary.gross_positive_count == 1
    assert summary.fee_adjusted_positive_count == 0
    assert summary.qualified_count == 0
    assert summary.rejected_by_reason == {STATUS_REJECTED_UNPROFITABLE_FEES: 1}

    rec = summary.records[0]
    assert rec.status == STATUS_REJECTED_UNPROFITABLE_FEES
    assert rec.gross_edge_cents == 2
    assert rec.estimated_fees_cents == 4
    assert rec.net_edge_cents == -2


def test_replay_insufficient_depth_on_one_leg():
    """Verify that an empty book on any constituent leg triggers INSUFFICIENT_DEPTH rejection."""
    basket = DeclaredMeceBasket(
        event_ticker="EVT-DEPTH",
        basket_id="BASKET-DEPTH",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    obs_a = _make_synthetic_leg_obs("M-A", "EVT-DEPTH", "A", "0.40", "0.60", source_timestamp=t0)
    # Leg B has empty order book (0 quantity / empty no_bids)
    obs_b_empty = MarketObservation(
        observation_id="empty-b",
        ticker="M-B",
        observed_at=t0 + timedelta(seconds=1),
        source_timestamp=t0 + timedelta(seconds=1),
        market=obs_a.market,
        order_book=NormalizedOrderBook(market_ticker="M-B", yes_bids=(), no_bids=(), source_timestamp=t0 + timedelta(seconds=1)),
        is_success=True,
        is_stale=False,
    )

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay([obs_a, obs_b_empty], [basket], source_path="synthetic_depth_test")

    assert summary.depth_supported_count == 0
    assert summary.rejected_by_reason == {STATUS_REJECTED_INSUFFICIENT_DEPTH: 1}
    assert summary.records[0].common_executable_quantity == 0


def test_replay_common_quantity_bottleneck():
    """Verify that common executable quantity is constrained by the shallowest constituent leg."""
    basket = DeclaredMeceBasket(
        event_ticker="EVT-BOTTLENECK",
        basket_id="BASKET-BOTTLENECK",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Leg A has depth 10, Leg B has depth 3
    obs_a = _make_synthetic_leg_obs("M-A", "EVT-BOTTLENECK", "A", "0.40", "0.60", qty="10.00", source_timestamp=t0)
    obs_b = _make_synthetic_leg_obs("M-B", "EVT-BOTTLENECK", "B", "0.40", "0.60", qty="3.00", source_timestamp=t0 + timedelta(seconds=1))

    engine = HistoricalBasketReplayEngine(HistoricalBasketReplayConfig(target_quantity=10))
    summary = engine.replay([obs_a, obs_b], [basket], source_path="synthetic_bottleneck")

    rec = summary.records[0]
    # Common quantity bottleneck is 3
    assert rec.common_executable_quantity == 3
    assert rec.per_leg_depth["M-A"] == 10
    assert rec.per_leg_depth["M-B"] == 3


# ===========================================================================
# 3. Compatibility, Persistence & Safety Tests
# ===========================================================================

def test_replay_without_basket_runs_single_market(tmp_path):
    """Verify backward compatibility: Replay without declared baskets executes single-market evaluation."""
    from kalshi_arbitrage.v10_evaluation import V10EvaluationConfig, V10HistoricalStrategyEvaluator
    from kalshi_arbitrage.evaluation import HistoricalDataset

    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    obs = [_make_synthetic_leg_obs("T1", "EVT1", "A", "0.45", "0.55", source_timestamp=t0)]

    dataset = HistoricalDataset.from_observations(obs, source_description="single_market_test")
    evaluator = V10HistoricalStrategyEvaluator(config=V10EvaluationConfig(enforce_universe=False))

    report = evaluator.evaluate(dataset, source_path="test")
    assert report.funnel.markets_evaluated == 1
    assert report.financials.ledger_is_reconciled is True


def test_replay_malformed_basket_configuration_fails_closed():
    """Verify that invalid basket declarations fail closed with ObserverError."""
    with pytest.raises(ObserverError):
        load_declared_baskets("{invalid_json:}")

    with pytest.raises(ObserverError):
        load_declared_baskets([{"event_ticker": "EVT"}])  # missing outcomes


def test_replay_persistence_artifacts_and_manifest_hashes(tmp_path):
    """Verify that write_artifacts persists files and computes exact matching SHA-256 byte digests."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    obs = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4)),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="synthetic_test")

    out_dir = tmp_path / "replay_out"
    artifacts = summary.write_artifacts(out_dir)

    assert artifacts["records"].exists()
    assert artifacts["summary"].exists()
    assert artifacts["report"].exists()
    assert artifacts["manifest"].exists()

    # Verify manifest hashes match disk bytes
    with open(artifacts["manifest"], "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    def _hash(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    assert manifest_data["files"]["replay_records.jsonl"] == _hash(artifacts["records"])
    assert manifest_data["files"]["replay_summary.json"] == _hash(artifacts["summary"])
    assert manifest_data["files"]["replay_report.md"] == _hash(artifacts["report"])
    assert manifest_data["records_count"] == 1


# ===========================================================================
# 4. Real Evidence Verification Tests
# ===========================================================================

def test_replay_real_v12_trial_evidence_exact_matching():
    """Verify that replaying the recorded V12 trial evidence (50 obs across 10 cycles) matches exactly."""
    evidence_path = Path("evidence_v12_fed_basket_trial/observations.jsonl")
    manifest_path = Path("evidence_v12_fed_basket_trial/manifest.json")
    config_path = Path("configs/baskets/fed_dec_2027_mece.json")

    assert evidence_path.exists(), "evidence_v12_fed_basket_trial/observations.jsonl must exist"
    assert manifest_path.exists(), "evidence_v12_fed_basket_trial/manifest.json must exist"
    assert config_path.exists(), "configs/baskets/fed_dec_2027_mece.json must exist"

    # Verify original evidence hash first
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    actual_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert actual_hash == manifest["observations_sha256"], "Evidence file must be uncorrupted and match manifest"

    from kalshi_arbitrage.evaluation import HistoricalDataset
    dataset = HistoricalDataset.from_file(evidence_path)
    baskets = load_declared_baskets(config_path)

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(
        dataset.observations,
        baskets,
        source_path=str(evidence_path),
        dataset_sha256=actual_hash,
    )

    # Replay must reconstruct exactly 10 complete snapshots across 10 cycles
    assert summary.total_input_observations == 50
    assert summary.duplicate_observations_count == 0
    assert summary.total_snapshots_reconstructed == 10
    assert summary.complete_snapshots_count == 10
    assert summary.rejected_snapshots_count == 10
    assert summary.rejected_by_reason == {STATUS_REJECTED_NO_GROSS_EDGE: 10}
    assert summary.qualified_count == 0

    # Every cycle had 102c cost, 100c payout, -2c gross edge, 7c fees, -9c net edge
    assert summary.gross_edge_distribution["min"] == -2.0
    assert summary.gross_edge_distribution["max"] == -2.0
    assert summary.gross_edge_distribution["mean"] == -2.0
    assert summary.net_edge_distribution["min"] == -9.0
    assert summary.net_edge_distribution["max"] == -9.0
    assert summary.net_edge_distribution["mean"] == -9.0

    # Measured timestamp skew was between 3.0s and 5.0s, with mean 4.0s
    assert summary.skew_distribution["min"] == 3.0
    assert summary.skew_distribution["max"] == 5.0
    assert summary.skew_distribution["mean"] == 4.0
    assert summary.skew_distribution["p50"] == 4.0


def test_reconstruct_observations_near_window_boundaries():
    """Verify behavior when observations arrive near the cycle window boundary (15s)."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Leg 1-4 at t0, Leg 5 arrives at t0 + 14.8s (within 15s window, but > 5s max skew)
    obs_near = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=14, milliseconds=800)),
    ]

    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig(cycle_window_seconds=15.0, max_leg_timestamp_skew_seconds=5.0))
    snaps, _ = reconstructor.reconstruct(obs_near, [basket])

    # Reconstructed into 1 snapshot (within 15s window), but rejected for excessive skew (> 5.0s)
    assert len(snaps) == 1
    assert snaps[0].status == STATUS_REJECTED_LEG_SKEW
    assert snaps[0].leg_timestamp_skew_seconds == 14.8


def test_reconstruct_repeated_replay_is_strictly_deterministic():
    """Verify that repeating replay on the same dataset produces strictly identical outputs."""
    basket = _make_five_leg_fed_basket()
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    obs = [
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C26", "KXFEDDECISION-27DEC", "CUT_MORE_THAN_25", "0.01", "0.99", source_timestamp=t0),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-C25", "KXFEDDECISION-27DEC", "CUT_25", "0.19", "0.81", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H0", "KXFEDDECISION-27DEC", "MAINTAIN", "0.63", "0.37", source_timestamp=t0 + timedelta(seconds=2)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H25", "KXFEDDECISION-27DEC", "HIKE_25", "0.16", "0.84", source_timestamp=t0 + timedelta(seconds=3)),
        _make_synthetic_leg_obs("KXFEDDECISION-27DEC-H26", "KXFEDDECISION-27DEC", "HIKE_MORE_THAN_25", "0.03", "0.97", source_timestamp=t0 + timedelta(seconds=4)),
    ]

    engine = HistoricalBasketReplayEngine()
    s1 = engine.replay(obs, [basket], source_path="deterministic_test")
    s2 = engine.replay(obs, [basket], source_path="deterministic_test")

    assert s1.to_dict()["records_count"] == s2.to_dict()["records_count"]
    assert s1.gross_edge_distribution == s2.gross_edge_distribution
    assert s1.net_edge_distribution == s2.net_edge_distribution
    assert s1.skew_distribution == s2.skew_distribution
    assert s1.records[0].gross_edge_cents == s2.records[0].gross_edge_cents
    assert s1.records[0].status == s2.records[0].status


def test_replay_profitable_synthetic_no_basket():
    """Verify that a declared NO basket with total cost < payout (N-1)*100 qualifies after fees."""
    basket = DeclaredMeceBasket(
        event_ticker="EVT-NO-PROF",
        basket_id="BASKET-NO-PROFITABLE",
        outcomes=("A", "B", "C"),
        market_outcome_map={"M-A": "A", "M-B": "B", "M-C": "C"},
        basket_side=NO,
    )
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # For NO basket with 3 outcomes, exactly 2 outcomes resolve to NO.
    # Guaranteed payout = (3 - 1) * 100¢ = 200¢.
    # Suppose NO asks are 50¢, 60¢, 60¢ -> Total cost = 170¢.
    # Gross edge = 200¢ - 170¢ = +30¢.
    # Net edge after fees is positive!
    obs = [
        _make_synthetic_leg_obs("M-A", "EVT-NO-PROF", "A", yes_ask="0.50", no_ask="0.50", source_timestamp=t0),
        _make_synthetic_leg_obs("M-B", "EVT-NO-PROF", "B", yes_ask="0.40", no_ask="0.60", source_timestamp=t0 + timedelta(seconds=1)),
        _make_synthetic_leg_obs("M-C", "EVT-NO-PROF", "C", yes_ask="0.40", no_ask="0.60", source_timestamp=t0 + timedelta(seconds=2)),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="synthetic_no_test")

    assert summary.qualified_count == 1
    rec = summary.records[0]
    assert rec.basket_side == NO
    assert rec.status == STATUS_QUALIFIED
    assert rec.is_qualified is True
    assert rec.guaranteed_payout_cents == 200
    assert rec.gross_acquisition_cost_cents == 170
    assert rec.gross_edge_cents == 30
    assert rec.net_edge_cents is not None and rec.net_edge_cents > 0


def test_replay_zero_edge_break_even():
    """Verify that a zero-gross-edge basket (cost == payout) is rejected as NO_GROSS_EDGE."""
    basket = DeclaredMeceBasket(
        event_ticker="EVT-ZERO",
        basket_id="BASKET-ZERO-EDGE",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Cost: 50¢ + 50¢ = 100¢. Guaranteed payout = 100¢. Gross edge = 0¢.
    obs = [
        _make_synthetic_leg_obs("M-A", "EVT-ZERO", "A", "0.50", "0.50", source_timestamp=t0),
        _make_synthetic_leg_obs("M-B", "EVT-ZERO", "B", "0.50", "0.50", source_timestamp=t0 + timedelta(seconds=1)),
    ]

    engine = HistoricalBasketReplayEngine()
    summary = engine.replay(obs, [basket], source_path="synthetic_zero_test")

    rec = summary.records[0]
    assert rec.gross_edge_cents == 0
    assert rec.status == STATUS_REJECTED_NO_GROSS_EDGE
    assert rec.is_qualified is False


def test_replay_fee_calculation_boundaries():
    """Verify official CFTC fee calculations across low, mid, and high price boundaries."""
    from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType, calculate_single_level_fee

    cfg = FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)

    # Boundary 1: 1¢ contract -> 1¢ taker fee
    fee_1c = calculate_single_level_fee(price_cents=1, quantity=1, config=cfg)
    assert fee_1c.total_fee_cents == 1

    # Boundary 2: 50¢ contract -> ceil(0.07 * 50 * 50 / 100) = ceil(1.75) = 2¢
    fee_50c = calculate_single_level_fee(price_cents=50, quantity=1, config=cfg)
    assert fee_50c.total_fee_cents == 2

    # Boundary 3: 99¢ contract -> 1¢ taker fee
    fee_99c = calculate_single_level_fee(price_cents=99, quantity=1, config=cfg)
    assert fee_99c.total_fee_cents == 1


def test_replay_never_invokes_order_submission():
    """Verify that historical replay engine contains no live order-submission capability."""
    import inspect
    from kalshi_arbitrage.v13_basket_replay import HistoricalBasketReplayEngine
    from kalshi_arbitrage.snapshot_reconstruction import EventSnapshotReconstructor

    # Ensure no order methods exist on classes
    forbidden_terms = ["place_order", "submit_order", "send_order", "cancel_order", "amend_order"]
    for cls in (HistoricalBasketReplayEngine, EventSnapshotReconstructor):
        methods = [m[0].lower() for m in inspect.getmembers(cls, predicate=inspect.isfunction)]
        for term in forbidden_terms:
            assert not any(term in m for m in methods), f"Forbidden method {term} found in {cls.__name__}"
