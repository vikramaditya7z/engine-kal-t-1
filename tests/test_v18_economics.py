"""Kalshi Arbitrage Engine V18 — Opportunity Economics & Attribution Tests.

Deterministic offline test suite covering:
1. Payout semantics:
   - Long YES guaranteed payout: Q * 100c
   - Long NO guaranteed payout: Q * (N - 1) * 100c
2. Two-sided microstructure invariant:
   - Gross Edge(YES) + Gross Edge(NO) == -Sum(Spread) <= 0
3. Causal attribution taxonomy:
   - NEGATIVE_GROSS_EDGE
   - FEE_EROSION (positive gross edge consumed by fees)
   - INSUFFICIENT_DEPTH (positive net edge but depth bottleneck)
   - DATA_QUALITY_OR_SKEW (excessive skew or unverified snapshot)
   - QUALIFIED_ARBITRAGE
4. Microstructure extraction:
   - Aggregate spread, average leg spread, limiting bottleneck leg
5. Full V17 evidence snapshot attribution:
   - Replay and attribution of 30 V17 broad-market trial snapshots
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import pytest
from typing import Dict, List, Optional, Tuple

from kalshi_arbitrage.attribution import (
    BasketSideAttribution,
    EconomicAttributionCategory,
    MicrostructureAttribution,
    OpportunityEconomicsAnalyzer,
    SnapshotEconomicAttribution,
)
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.evidence_storage import observation_from_dict
from kalshi_arbitrage.execution import evaluate_mece_basket_execution
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    MarketObservation,
)
from kalshi_arbitrage.portfolio import Event
from kalshi_arbitrage.snapshot_reconstruction import (
    EventSnapshotReconstructor,
    ReconstructedBasketSnapshot,
    STATUS_VALID_SNAPSHOT,
    SnapshotReconstructionConfig,
)


def _make_test_order_book(
    ticker: str,
    yes_bid_cents: int,
    yes_ask_cents: int,
    quantity: int = 100,
    ts: Optional[datetime] = None,
) -> Tuple[NormalizedMarket, NormalizedOrderBook]:
    """Helper to create matching NormalizedMarket and NormalizedOrderBook."""
    now = ts or datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # Binary complements:
    # YES ask = 100 - NO bid => NO bid = 100 - YES ask
    # NO ask = 100 - YES bid => YES bid = 100 - NO ask
    no_bid_cents = 100 - yes_ask_cents
    no_ask_cents = 100 - yes_bid_cents

    m = NormalizedMarket(
        ticker=ticker,
        event_ticker="EVT-TEST",
        market_type="binary",
        status="active",
        title=f"Market {ticker}",
        subtitle=f"Subtitle {ticker}",
        yes_subtitle="Yes",
        no_subtitle="No",
        rules_primary="Rule primary",
        rules_secondary="Rule secondary",
        open_time=now,
        close_time=now,
        expiration_time=now,
        yes_bid_dollars=Decimal(yes_bid_cents) / Decimal("100"),
        yes_ask_dollars=Decimal(yes_ask_cents) / Decimal("100"),
        no_bid_dollars=Decimal(no_bid_cents) / Decimal("100"),
        no_ask_dollars=Decimal(no_ask_cents) / Decimal("100"),
        last_price_dollars=Decimal("0.50"),
        volume_24h=1000,
        open_interest=1000,
    )

    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=Decimal(yes_bid_cents) / Decimal("100"), quantity=Decimal(quantity)),),
        no_bids=(OrderBookLevel(price_dollars=Decimal(no_bid_cents) / Decimal("100"), quantity=Decimal(quantity)),),
        source_timestamp=now,
    )
    return m, book


def test_yes_and_no_payout_semantics():
    """Verify exact payout semantics for N-outcome partitions."""
    event_3 = Event("EVT-3", ("A", "B", "C"), relationship_established=True)
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    # 3-leg event: prices 30c, 30c, 30c (sum asks = 90c < 100c)
    books = {}
    for letter in ("A", "B", "C"):
        _, b = _make_test_order_book(f"M-{letter}", yes_bid_cents=25, yes_ask_cents=30, quantity=10, ts=t0)
        books[letter] = b

    # Long YES: Payout should be Q * 100c
    res_yes = evaluate_mece_basket_execution(event_3, books, basket_side=YES, requested_quantity=2)
    assert res_yes.guaranteed_payout_cents == 2 * 100
    assert res_yes.total_cost_cents == 2 * (30 + 30 + 30)  # 180c
    assert res_yes.gross_profit_cents == 20  # 200 - 180

    # Long NO: Payout should be Q * (N - 1) * 100c = 2 * (3 - 1) * 100c = 400c
    # In 3-leg partition, NO ask = 100 - YES bid = 100 - 25 = 75c
    res_no = evaluate_mece_basket_execution(event_3, books, basket_side=NO, requested_quantity=2)
    assert res_no.guaranteed_payout_cents == 2 * (3 - 1) * 100  # 400c
    assert res_no.total_cost_cents == 2 * (75 + 75 + 75)  # 450c
    assert res_no.gross_profit_cents == -50  # 400 - 450 = -50c


def test_two_sided_microstructure_invariant():
    """Verify the fundamental invariant: GrossEdge(YES) + GrossEdge(NO) == -TotalSpread."""
    analyzer = OpportunityEconomicsAnalyzer()
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    # 2-leg pairwise competition
    m1, b1 = _make_test_order_book("M-1", yes_bid_cents=45, yes_ask_cents=55, quantity=10, ts=t0)
    m2, b2 = _make_test_order_book("M-2", yes_bid_cents=40, yes_ask_cents=53, quantity=10, ts=t0)

    obs1 = MarketObservation("o1", "M-1", t0, t0, m1, b1, True, False)
    obs2 = MarketObservation("o2", "M-2", t0, t0, m2, b2, True, False)

    basket = DeclaredMeceBasket("EVT-TEST", ("O1", "O2"), {"M-1": "O1", "M-2": "O2"})
    snap = ReconstructedBasketSnapshot(
        snapshot_id="s1",
        basket=basket,
        observations={"M-1": obs1, "M-2": obs2},
        observation_ids=("o1", "o2"),
        source_timestamps={"M-1": t0.isoformat(), "M-2": t0.isoformat()},
        min_timestamp=t0,
        max_timestamp=t0,
        leg_timestamp_skew_seconds=0.0,
        is_complete=True,
        status=STATUS_VALID_SNAPSHOT,
    )

    attr = analyzer.analyze_snapshot(snap, requested_quantity=1)

    # Spread leg 1 = 55 - 45 = 10c; Spread leg 2 = 53 - 40 = 13c; Total spread = 23c
    assert attr.microstructure.aggregate_spread_cents == 23
    assert attr.yes_side.gross_edge_cents == 100 - (55 + 53)  # -8c
    # NO gross edge = Sum(YES bid) - 100 = (45 + 40) - 100 = -15c
    assert attr.no_side.gross_edge_cents == -15
    # Combined edge = -8 + (-15) = -23c == -Total spread
    assert attr.yes_side.gross_edge_cents + attr.no_side.gross_edge_cents == -attr.microstructure.aggregate_spread_cents
    assert attr.microstructure.complementarity_invariant_holds is True
    assert attr.dominant_failure_reason == "NORMAL_OVER_ROUND_MARKET_MAKING"


def test_fee_erosion_attribution_category():
    """Verify FEE_EROSION is assigned when positive gross edge is eroded by exchange fees."""
    # Synthetic basket where sum of asks is 98c (gross edge = +2c), but taker fees = 4c -> net edge = -2c
    analyzer = OpportunityEconomicsAnalyzer(FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER))
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    m1, b1 = _make_test_order_book("M-1", yes_bid_cents=45, yes_ask_cents=49, quantity=10, ts=t0)
    m2, b2 = _make_test_order_book("M-2", yes_bid_cents=45, yes_ask_cents=49, quantity=10, ts=t0)

    obs1 = MarketObservation("o1", "M-1", t0, t0, m1, b1, True, False)
    obs2 = MarketObservation("o2", "M-2", t0, t0, m2, b2, True, False)

    basket = DeclaredMeceBasket("EVT-FEE", ("O1", "O2"), {"M-1": "O1", "M-2": "O2"})
    snap = ReconstructedBasketSnapshot(
        snapshot_id="s-fee",
        basket=basket,
        observations={"M-1": obs1, "M-2": obs2},
        observation_ids=("o1", "o2"),
        source_timestamps={"M-1": t0.isoformat(), "M-2": t0.isoformat()},
        min_timestamp=t0,
        max_timestamp=t0,
        leg_timestamp_skew_seconds=0.0,
        is_complete=True,
        status=STATUS_VALID_SNAPSHOT,
    )

    attr = analyzer.analyze_snapshot(snap, requested_quantity=1)
    assert attr.yes_side.gross_edge_cents == 2
    assert attr.yes_side.is_gross_profitable is True
    assert attr.yes_side.net_edge_cents < 0  # Eroded by fees
    assert attr.yes_side.category == EconomicAttributionCategory.FEE_EROSION
    assert "eroded" in attr.yes_side.explanation


def test_insufficient_depth_attribution_category():
    """Verify INSUFFICIENT_DEPTH identifies limiting bottleneck leg."""
    analyzer = OpportunityEconomicsAnalyzer(FeeModelConfig(schedule_type=FeeScheduleType.EXEMPT))
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    # Leg 1 has 10 contracts depth; Leg 2 has only 2 contracts depth
    m1, b1 = _make_test_order_book("M-DEEP", yes_bid_cents=40, yes_ask_cents=45, quantity=10, ts=t0)
    m2, b2 = _make_test_order_book("M-SHALLOW", yes_bid_cents=40, yes_ask_cents=45, quantity=2, ts=t0)

    obs1 = MarketObservation("o1", "M-DEEP", t0, t0, m1, b1, True, False)
    obs2 = MarketObservation("o2", "M-SHALLOW", t0, t0, m2, b2, True, False)

    basket = DeclaredMeceBasket("EVT-DEPTH", ("O1", "O2"), {"M-DEEP": "O1", "M-SHALLOW": "O2"})
    snap = ReconstructedBasketSnapshot(
        snapshot_id="s-depth",
        basket=basket,
        observations={"M-DEEP": obs1, "M-SHALLOW": obs2},
        observation_ids=("o1", "o2"),
        source_timestamps={"M-1": t0.isoformat(), "M-2": t0.isoformat()},
        min_timestamp=t0,
        max_timestamp=t0,
        leg_timestamp_skew_seconds=0.0,
        is_complete=True,
        status=STATUS_VALID_SNAPSHOT,
    )

    # Request 5 contracts (supported is only 2)
    attr = analyzer.analyze_snapshot(snap, requested_quantity=5)
    assert attr.yes_side.category == EconomicAttributionCategory.INSUFFICIENT_DEPTH
    assert attr.yes_side.limiting_leg == "M-SHALLOW"
    assert attr.yes_side.supported_quantity == 2


def test_invalid_snapshot_data_quality_attribution():
    """Verify invalid snapshot (e.g. excessive skew) is categorized cleanly."""
    analyzer = OpportunityEconomicsAnalyzer()
    basket = DeclaredMeceBasket("EVT-INVALID", ("O1", "O2"), {"M-1": "O1", "M-2": "O2"})
    snap = ReconstructedBasketSnapshot(
        snapshot_id="s-invalid",
        basket=basket,
        observations={},
        observation_ids=(),
        source_timestamps={},
        min_timestamp=None,
        max_timestamp=None,
        leg_timestamp_skew_seconds=8.5,
        is_complete=False,
        status="REJECTED_LEG_SKEW",
        rejection_reason="Excessive leg timestamp skew: 8.50s > max 5.00s",
    )

    attr = analyzer.analyze_snapshot(snap)
    assert attr.is_valid_snapshot is False
    assert attr.yes_side.category == EconomicAttributionCategory.DATA_QUALITY_OR_SKEW
    assert attr.dominant_failure_reason == "REJECTED_LEG_SKEW"


def test_v17_recorded_evidence_economic_attribution():
    """Run full two-sided attribution on all 30 V17 trial snapshots."""
    v17_obs_path = Path("evidence_v17_broad_market_trial/observations.jsonl")
    if not v17_obs_path.exists():
        pytest.skip("V17 evidence file not present")

    raw_obs = [json.loads(l) for l in v17_obs_path.read_text().strip().split("\n")]
    observations = [observation_from_dict(d) for d in raw_obs]

    # Reconstruct snapshots
    disc = json.loads(open("evidence_v17_broad_market_trial/universe_discovery_report.json").read())
    baskets = []
    for b_meta in disc["observable_baskets"]:
        evt = b_meta["event_ticker"]
        tickers = sorted(list(set(o.ticker for o in observations if o.ticker.startswith(evt))))
        out_map = {t: f"OUT_{i}" for i, t in enumerate(tickers)}
        baskets.append(DeclaredMeceBasket(
            event_ticker=evt,
            outcomes=tuple(out_map.values()),
            market_outcome_map=out_map,
            basket_id=f"BASKET-{evt}",
        ))

    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig())
    snapshots, _ = reconstructor.reconstruct(observations, baskets)
    assert len(snapshots) == 30

    analyzer = OpportunityEconomicsAnalyzer()
    attributions = analyzer.analyze_snapshots(snapshots)
    assert len(attributions) == 30

    # Verify all 30 snapshots maintain mathematical invariant
    for a in attributions:
        assert a.is_valid_snapshot is True
        assert a.microstructure.complementarity_invariant_holds is True
        assert a.yes_side.gross_edge_cents + a.no_side.gross_edge_cents == -a.microstructure.aggregate_spread_cents
        # In calm market states, all were in normal over-round regime
        assert a.dominant_failure_reason == "NORMAL_OVER_ROUND_MARKET_MAKING"
        assert a.yes_side.category == EconomicAttributionCategory.NEGATIVE_GROSS_EDGE
        assert a.no_side.category == EconomicAttributionCategory.NEGATIVE_GROSS_EDGE

    # Check summary metrics
    summary = analyzer.summarize_attributions(attributions)
    assert summary["total_snapshots_analyzed"] == 30
    assert summary["valid_snapshots"] == 30
    assert summary["complementarity_invariant_pass_rate"] == 1.0
    assert summary["yes_gross_edge_range_cents"] == [-31, -8]
    assert summary["no_gross_edge_range_cents"] == [-51, -8]
