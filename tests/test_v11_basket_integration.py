"""Kalshi Arbitrage Engine V11 — Multi-Outcome MECE Basket Integration Tests.

Comprehensive regression suite covering:
1. Configuration:
   - Valid complete declared basket.
   - Missing outcome mapping.
   - Duplicate ticker or outcome mapping.
   - Invalid or unknown market mapping.
   - No implicit MECE inference.
   - JSON file and JSON string configuration loading.
2. Synchronization & Timing:
   - Complete basket within permitted timestamp skew.
   - Basket rejected when timestamp skew exceeds max limit.
   - Incomplete snapshot (missing leg in active cycle).
   - Stale leg rejection.
   - Prior cycle observations must not complete newer incomplete cycle.
   - Multiple baskets must not cross-contaminate observations.
3. Economics & Execution:
   - Positive gross edge with positive net edge after fees.
   - Positive gross edge but negative net edge after CFTC fees.
   - Zero-edge and negative-edge baskets.
   - Insufficient depth (empty book on one leg).
   - Partial common quantity sizing across legs.
4. Compatibility, Auditing & Safety:
   - Single-market collection compatibility when no basket configured.
   - Persisted evidence in opportunities.jsonl is complete and auditable.
   - Cryptographic manifest verification.
   - Safe research execution (zero live order submission).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pytest

from kalshi_arbitrage.arbitrage import (
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
)
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.evidence_storage import EvidenceStore
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.market_data_provider import FixtureMarketDataProvider
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    EvaluatedOpportunity,
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
from kalshi_arbitrage.paper_risk import PaperRiskConfig
from kalshi_arbitrage.v11_collector import (
    V11CollectorConfig,
    V11EvidenceManifest,
    V11MarketEvidenceCollector,
    V11ProspectiveEvaluator,
)


def _make_synthetic_leg(
    ticker: str,
    event_ticker: str,
    outcome: str,
    yes_ask: str,
    no_ask: str,
    qty: str = "10.00",
    source_timestamp: Optional[datetime] = None,
    is_stale: bool = False,
) -> Tuple[NormalizedMarket, NormalizedOrderBook]:
    """Helper to generate synthetic market and book with explicit derived asks.

    NOTE: This generates SYNTHETIC research fixtures for hermetic test execution.
    These are not actual market opportunities or live exchange quotes.
    """
    ts = source_timestamp or datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
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
        title=f"Synthetic Market {ticker} ({outcome})",
        subtitle=outcome,
        yes_bid_dollars=c_yes_bid if c_yes_bid >= 0 else Decimal("0.00"),
        yes_ask_dollars=y_ask_dec,
        no_bid_dollars=c_no_bid if c_no_bid >= 0 else Decimal("0.00"),
        no_ask_dollars=n_ask_dec,
        volume_24h=Decimal("500.00"),
        open_interest=Decimal("300.00"),
    )

    y_levels = (
        (OrderBookLevel(price_dollars=c_yes_bid, quantity=Decimal(qty)),)
        if c_yes_bid > 0
        else ()
    )
    n_levels = (
        (OrderBookLevel(price_dollars=c_no_bid, quantity=Decimal(qty)),)
        if c_no_bid > 0
        else ()
    )

    book = NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=y_levels,
        no_bids=n_levels,
        source_timestamp=ts,
    )

    return market, book


def _make_synthetic_obs(
    market: NormalizedMarket,
    book: NormalizedOrderBook,
    observed_at: Optional[datetime] = None,
    is_stale: bool = False,
    staleness_reason: Optional[str] = None,
) -> MarketObservation:
    t_obs = observed_at or (book.source_timestamp or datetime.now(timezone.utc))
    return MarketObservation(
        observation_id=f"obs-{market.ticker}-{int(t_obs.timestamp())}",
        ticker=market.ticker,
        observed_at=t_obs,
        source_timestamp=book.source_timestamp,
        market=market,
        order_book=book,
        is_success=True,
        is_stale=is_stale,
        staleness_reason=staleness_reason if is_stale else None,
    )


# ==============================================================================
# Phase 4 — Section 1: Configuration Validation Tests
# ==============================================================================

def test_declared_basket_valid_complete():
    """Verify construction and properties of a valid complete declared basket."""
    basket = DeclaredMeceBasket(
        event_ticker="KXFED-27DEC",
        outcomes=("CUT", "HOLD", "HIKE"),
        market_outcome_map={
            "M-CUT": "CUT",
            "M-HOLD": "HOLD",
            "M-HIKE": "HIKE",
        },
        basket_side=YES,
    )
    assert basket.event_ticker == "KXFED-27DEC"
    assert basket.outcomes == ("CUT", "HOLD", "HIKE")
    assert basket.ordered_tickers == ("M-CUT", "M-HOLD", "M-HIKE")
    assert basket.basket_side == YES

    # Serialization roundtrip
    d = basket.to_dict()
    assert d["event_ticker"] == "KXFED-27DEC"
    assert d["outcomes"] == ["CUT", "HOLD", "HIKE"]
    assert d["market_outcome_map"] == {"M-CUT": "CUT", "M-HOLD": "HOLD", "M-HIKE": "HIKE"}
    roundtrip = DeclaredMeceBasket.from_dict(d)
    assert roundtrip == basket


def test_declared_basket_missing_outcome():
    """Verify rejection when declared outcomes are not all mapped in market_outcome_map."""
    with pytest.raises(ObserverError, match="must map exactly all declared outcomes"):
        DeclaredMeceBasket(
            event_ticker="KXFED-27DEC",
            outcomes=("CUT", "HOLD", "HIKE"),
            market_outcome_map={
                "M-CUT": "CUT",
                "M-HOLD": "HOLD",
                # "HIKE" missing!
            },
        )


def test_declared_basket_duplicate_outcome_mapping():
    """Verify rejection when two tickers map to the same outcome or duplicate outcomes declared."""
    # Duplicate in declared outcomes tuple
    with pytest.raises(ObserverError, match="outcomes must contain unique strings"):
        DeclaredMeceBasket(
            event_ticker="KXFED-27DEC",
            outcomes=("CUT", "HOLD", "CUT"),
            market_outcome_map={"M1": "CUT", "M2": "HOLD", "M3": "CUT"},
        )

    # Two tickers mapping to same outcome
    with pytest.raises(ObserverError, match="must map each declared outcome exactly once"):
        DeclaredMeceBasket(
            event_ticker="KXFED-27DEC",
            outcomes=("CUT", "HOLD"),
            market_outcome_map={"M1": "CUT", "M2": "CUT"},
        )


def test_declared_basket_unknown_outcome_mapping():
    """Verify rejection when market_outcome_map maps to an undeclared outcome."""
    with pytest.raises(ObserverError, match="not in declared outcomes"):
        DeclaredMeceBasket(
            event_ticker="KXFED-27DEC",
            outcomes=("CUT", "HOLD"),
            market_outcome_map={"M-CUT": "CUT", "M-UNKNOWN": "UNKNOWN"},
        )


def test_declared_basket_no_implicit_mece_inference():
    """Verify fail-closed rejection when inputs are empty, non-tuple, or invalid types."""
    with pytest.raises(ObserverError, match="event_ticker must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
        )

    with pytest.raises(ObserverError, match="at least 2 outcomes"):
        DeclaredMeceBasket(
            event_ticker="EV1",
            outcomes=("SINGLE_OUTCOME",),
            market_outcome_map={"M1": "SINGLE_OUTCOME"},
        )

    with pytest.raises(ObserverError, match="basket_side must be"):
        DeclaredMeceBasket(
            event_ticker="EV1",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
            basket_side="INVALID_SIDE",
        )


def test_load_declared_baskets_from_json_file_and_string(tmp_path: Path):
    """Verify loading declared baskets from JSON file and raw JSON strings."""
    basket_dict = {
        "event_ticker": "KXFED-27DEC",
        "outcomes": ["CUT", "HOLD", "HIKE"],
        "market_outcome_map": {
            "M1": "CUT",
            "M2": "HOLD",
            "M3": "HIKE",
        },
        "basket_side": "YES",
    }

    # 1. Load from file (single object)
    p_single = tmp_path / "single_basket.json"
    p_single.write_text(json.dumps(basket_dict), encoding="utf-8")
    baskets = load_declared_baskets(p_single)
    assert len(baskets) == 1
    assert baskets[0].event_ticker == "KXFED-27DEC"
    assert baskets[0].ordered_tickers == ("M1", "M2", "M3")

    # 2. Load from file (array of objects)
    p_list = tmp_path / "list_basket.json"
    p_list.write_text(json.dumps([basket_dict]), encoding="utf-8")
    baskets_list = load_declared_baskets(p_list)
    assert len(baskets_list) == 1
    assert baskets_list[0].event_ticker == "KXFED-27DEC"

    # 3. Load from JSON string
    baskets_str = load_declared_baskets(json.dumps(basket_dict))
    assert len(baskets_str) == 1
    assert baskets_str[0].event_ticker == "KXFED-27DEC"

    # 4. Malformed JSON must fail closed
    with pytest.raises(ObserverError, match="Invalid basket configuration JSON"):
        load_declared_baskets("not a json string")

    # 5. Non-existent file must fail closed
    with pytest.raises(ObserverError, match="not found"):
        load_declared_baskets(tmp_path / "non_existent.json")


# ==============================================================================
# Phase 4 — Section 2: Synchronization & Timestamp Skew Tests
# ==============================================================================

def test_basket_evaluation_synchronized_within_skew():
    """Verify that basket evaluation succeeds when leg timestamps are within max skew."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # Leg 1 at t0, Leg 2 at t0 + 1s, Leg 3 at t0 + 2s (max skew = 2.0s <= 5.0s)
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", source_timestamp=t0 + timedelta(seconds=1))
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.35", "0.65", source_timestamp=t0 + timedelta(seconds=2))

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
        "M3": _make_synthetic_obs(m3, b3),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig(max_leg_timestamp_skew_seconds=5.0))

    eval_opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0 + timedelta(seconds=2))
    assert eval_opp is not None
    assert eval_opp.status == STATUS_QUALIFIED
    assert eval_opp.is_qualified is True
    assert eval_opp.leg_timestamp_skew_seconds == pytest.approx(2.0, abs=1e-3)


def test_basket_evaluation_rejected_excessive_skew():
    """Verify that basket evaluation is rejected when timestamp skew exceeds limit."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # Leg 1 at t0, Leg 2 at t0, Leg 3 at t0 + 10s (skew = 10.0s > 5.0s max limit)
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", source_timestamp=t0)
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.35", "0.65", source_timestamp=t0 + timedelta(seconds=10))

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
        "M3": _make_synthetic_obs(m3, b3),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig(max_leg_timestamp_skew_seconds=5.0))

    eval_opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0 + timedelta(seconds=10))
    assert eval_opp is not None
    assert eval_opp.status == STATUS_REJECTED_LEG_SKEW
    assert eval_opp.is_qualified is False
    assert "Excessive leg timestamp skew" in eval_opp.rejection_reason
    assert eval_opp.leg_timestamp_skew_seconds == pytest.approx(10.0, abs=1e-3)


def test_basket_evaluation_rejected_missing_leg():
    """Verify that basket evaluation is rejected if any leg is missing from cycle observations."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", source_timestamp=t0)
    # Leg M3 is missing from cycle observations!

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())

    eval_opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0)
    assert eval_opp is not None
    assert eval_opp.status == STATUS_REJECTED_MISSING_MARKET
    assert eval_opp.is_qualified is False
    assert "M3" in eval_opp.rejection_reason


def test_basket_evaluation_stale_leg_rejected():
    """Verify that basket evaluation is rejected if any leg is marked stale."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", source_timestamp=t0)
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.35", "0.65", source_timestamp=t0)

    # Leg M2 is stale
    obs2_stale = _make_synthetic_obs(m2, b2, is_stale=True, staleness_reason="Exceeded 60s max age")

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": obs2_stale,
        "M3": _make_synthetic_obs(m3, b3),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())

    eval_opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0)
    assert eval_opp is not None
    assert eval_opp.status == STATUS_REJECTED_STALE
    assert eval_opp.is_qualified is False
    assert "Stale leg(s)" in eval_opp.rejection_reason


def test_basket_evaluation_does_not_mix_prior_cycle_observations(tmp_path: Path):
    """Verify that observations from a prior cycle are never combined with a newer incomplete cycle."""
    t1 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", source_timestamp=t1)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", source_timestamp=t1)
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.35", "0.65", source_timestamp=t1)

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})

    # Cycle 1: All 3 legs available
    prov1 = FixtureMarketDataProvider(
        markets=[m1, m2, m3],
        books={"M1": b1, "M2": b2, "M3": b3},
        current_time=t1,
    )
    collector = V11MarketEvidenceCollector(
        config=V11CollectorConfig(
            output_dir=tmp_path / "no_mix",
            declared_baskets=(basket,),
            enable_prospective_evaluation=True,
        ),
        data_provider=prov1,
    )
    obs_count_c1 = collector.run_single_cycle()
    assert obs_count_c1 == 3
    assert collector.prospective_evaluator.basket_qualified == 1

    # Cycle 2: M3 fails/disappears! Only M1 and M2 available in cycle 2
    t2 = t1 + timedelta(seconds=10)
    prov2 = FixtureMarketDataProvider(
        markets=[m1, m2],
        books={"M1": b1, "M2": b2},
        current_time=t2,
    )
    collector.data_provider = prov2
    obs_count_c2 = collector.run_single_cycle()
    assert obs_count_c2 == 2
    # Cycle 2 MUST record STATUS_REJECTED_MISSING_MARKET, not reuse cycle 1 M3
    assert collector.prospective_evaluator.basket_rej_missing_market == 1


def test_multiple_baskets_do_not_cross_contaminate():
    """Verify that observations from multiple declared baskets remain strictly segregated."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # Basket A: EV-A (2 legs: A1, A2) -> Profitable (25¢ + 25¢ = 50¢ < 100¢)
    ma1, ba1 = _make_synthetic_leg("A1", "EV-A", "A_YES", "0.25", "0.75", source_timestamp=t0)
    ma2, ba2 = _make_synthetic_leg("A2", "EV-A", "A_NO", "0.25", "0.75", source_timestamp=t0)

    # Basket B: EV-B (2 legs: B1, B2) -> Unprofitable / Negative edge (60¢ + 60¢ = 120¢ > 100¢)
    mb1, bb1 = _make_synthetic_leg("B1", "EV-B", "B_YES", "0.60", "0.40", source_timestamp=t0)
    mb2, bb2 = _make_synthetic_leg("B2", "EV-B", "B_NO", "0.60", "0.40", source_timestamp=t0)

    obs_map = {
        "A1": _make_synthetic_obs(ma1, ba1),
        "A2": _make_synthetic_obs(ma2, ba2),
        "B1": _make_synthetic_obs(mb1, bb1),
        "B2": _make_synthetic_obs(mb2, bb2),
    }

    basket_a = DeclaredMeceBasket("EV-A", ("A_YES", "A_NO"), {"A1": "A_YES", "A2": "A_NO"})
    basket_b = DeclaredMeceBasket("EV-B", ("B_YES", "B_NO"), {"B1": "B_YES", "B2": "B_NO"})

    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())

    opp_a = evaluator.process_basket(basket_a, obs_map, cycle_timestamp=t0)
    opp_b = evaluator.process_basket(basket_b, obs_map, cycle_timestamp=t0)

    assert opp_a.status == STATUS_QUALIFIED
    assert opp_a.is_qualified is True
    assert opp_a.market_tickers == ("A1", "A2")

    assert opp_b.status == STATUS_REJECTED_NO_GROSS_EDGE
    assert opp_b.is_qualified is False
    assert opp_b.market_tickers == ("B1", "B2")


# ==============================================================================
# Phase 4 — Section 3: Economics, Fees & Execution Feasibility Tests
# ==============================================================================

def test_basket_evaluation_positive_gross_and_net_edge():
    """Verify that a genuine profitable basket is qualified and reconciles in ledger."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # 3 legs: YES asks at 25¢, 30¢, 35¢ => Total = 90¢. Guaranteed payout = 100¢. Gross edge = +10¢.
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", source_timestamp=t0)
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.35", "0.65", source_timestamp=t0)

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
        "M3": _make_synthetic_obs(m3, b3),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig(initial_cash_cents=100_000))

    opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0)
    assert opp is not None
    assert opp.status == STATUS_QUALIFIED
    assert opp.is_qualified is True
    assert opp.candidate_opportunity.gross_edge_cents == 10
    assert opp.pricing_result.is_net_profitable is True
    assert opp.paper_trade_id is not None

    summary = evaluator.get_financial_summary()
    assert summary["completed_arbs"] == 1
    assert summary["is_ledger_reconciled"] is True
    assert summary["discrepancies"] == ()


def test_basket_evaluation_positive_gross_but_negative_net_after_fees():
    """Verify that a candidate with positive gross edge but negative net edge is rejected for fees."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # 3 legs: YES asks at 32¢, 33¢, 34¢ => Total = 99¢. Guaranteed payout = 100¢. Gross edge = +1¢.
    # CFTC quadratic fees at ~33¢ are ceil(0.07 * 33 * 67) / 100 ~ 2¢ per contract.
    # Total fees for 3 contracts ~ 6¢ > 1¢ edge => Net edge is negative!
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.32", "0.68", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.33", "0.67", source_timestamp=t0)
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.34", "0.66", source_timestamp=t0)

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
        "M3": _make_synthetic_obs(m3, b3),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())

    opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0)
    assert opp is not None
    assert opp.status == STATUS_REJECTED_UNPROFITABLE_FEES
    assert opp.is_qualified is False
    assert opp.candidate_opportunity.gross_edge_cents == 1
    assert "Unprofitable after estimated fees" in opp.rejection_reason


def test_basket_evaluation_negative_gross_edge():
    """Verify that a candidate with negative gross edge (e.g. sum of asks > 100¢) is rejected."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # 3 legs: YES asks at 40¢, 35¢, 30¢ => Total = 105¢ > 100¢. Gross edge = -5¢.
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.40", "0.60", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.35", "0.65", source_timestamp=t0)
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.30", "0.70", source_timestamp=t0)

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
        "M3": _make_synthetic_obs(m3, b3),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())

    opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0)
    assert opp is not None
    assert opp.status == STATUS_REJECTED_NO_GROSS_EDGE
    assert opp.is_qualified is False
    assert opp.candidate_opportunity.gross_edge_cents == -5
    assert "No gross edge" in opp.rejection_reason


def test_basket_evaluation_insufficient_depth():
    """Verify rejection when one leg has an empty book (0 depth)."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", qty="10.00", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", qty="10.00", source_timestamp=t0)
    # Leg 3 has NO resting bids, meaning 0 YES ask depth!
    m3 = NormalizedMarket("M3", "EV1", "binary", "active", "M3", yes_ask_dollars=Decimal("0.35"))
    b3_empty = NormalizedOrderBook("M3", yes_bids=(), no_bids=(), source_timestamp=t0)

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
        "M3": _make_synthetic_obs(m3, b3_empty),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig())

    opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0)
    assert opp is not None
    assert opp.status == STATUS_REJECTED_INSUFFICIENT_DEPTH
    assert opp.is_qualified is False
    assert opp.pricing_result.supported_quantity == 0


def test_basket_evaluation_bottleneck_common_quantity():
    """Verify depth traversal sizes down to the shallowest leg."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    # M1 has 10 contracts, M2 has 2 contracts (bottleneck!), M3 has 8 contracts
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.25", "0.75", qty="10.00", source_timestamp=t0)
    m2, b2 = _make_synthetic_leg("M2", "EV1", "O2", "0.30", "0.70", qty="2.00", source_timestamp=t0)
    m3, b3 = _make_synthetic_leg("M3", "EV1", "O3", "0.35", "0.65", qty="8.00", source_timestamp=t0)

    obs_map = {
        "M1": _make_synthetic_obs(m1, b1),
        "M2": _make_synthetic_obs(m2, b2),
        "M3": _make_synthetic_obs(m3, b3),
    }

    basket = DeclaredMeceBasket("EV1", ("O1", "O2", "O3"), {"M1": "O1", "M2": "O2", "M3": "O3"})
    # Request 5 units
    evaluator = V11ProspectiveEvaluator(config=V11CollectorConfig(target_quantity=5, initial_cash_cents=100_000))

    opp = evaluator.process_basket(basket, obs_map, cycle_timestamp=t0)
    assert opp is not None
    assert opp.pricing_result.requested_quantity == 5
    # Common quantity MUST be bottlenecked at 2 units!
    assert opp.pricing_result.supported_quantity == 2
    assert opp.pricing_result.total_cost_cents == 2 * 90  # 180¢
    assert opp.pricing_result.guaranteed_payout_cents == 2 * 100  # 200¢
    assert opp.status == STATUS_QUALIFIED


# ==============================================================================
# Phase 5: End-to-End Offline Verification Pipeline
# ==============================================================================

def test_v11_collector_single_market_compatibility_when_no_basket(tmp_path: Path):
    """Verify that single-market collection behavior is 100% backward-compatible when no basket is configured."""
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    m1, b1 = _make_synthetic_leg("M1", "EV1", "O1", "0.40", "0.55", source_timestamp=t0)
    provider = FixtureMarketDataProvider(markets=[m1], books={"M1": b1}, current_time=t0)

    cfg = V11CollectorConfig(
        output_dir=tmp_path / "single_compat",
        target_tickers=("M1",),
        max_cycles=1,
        enable_prospective_evaluation=True,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)
    manifest = collector.run()

    assert manifest.successful_observations == 1
    assert manifest.total_cycles == 1
    assert (tmp_path / "single_compat" / "observations.jsonl").exists()
    assert (tmp_path / "single_compat" / "manifest.json").exists()


def test_v11_collector_e2e_declared_basket_pipeline(tmp_path: Path):
    """Phase 5: Complete end-to-end multi-outcome research pipeline verification.

    Demonstrates:
    1. Configuration loading from JSON file.
    2. Synchronized market observations collected via provider.
    3. Multi-leg timestamp-skew and completeness verification.
    4. Execution analysis with order-book depth and CFTC fee modeling.
    5. Determination of both qualified (profitable) and rejected (negative edge) cycles.
    6. Complete persistence to observations.jsonl, opportunities.jsonl, and manifest.json.
    7. Double-entry paper ledger reconciliation with zero discrepancies.
    """
    out_dir = tmp_path / "e2e_basket_trial"
    basket_file = tmp_path / "fed_basket.json"

    basket_data = {
        "event_ticker": "KXFED-27DEC",
        "outcomes": ["CUT", "HOLD", "HIKE"],
        "market_outcome_map": {
            "KXFED-27DEC-CUT": "CUT",
            "KXFED-27DEC-HOLD": "HOLD",
            "KXFED-27DEC-HIKE": "HIKE",
        },
        "basket_side": "YES",
    }
    basket_file.write_text(json.dumps(basket_data), encoding="utf-8")

    # Cycle 1: Profitable synthetic basket fixture (asks 25¢ + 30¢ + 35¢ = 90¢)
    t1 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    m1, b1 = _make_synthetic_leg("KXFED-27DEC-CUT", "KXFED-27DEC", "CUT", "0.25", "0.75", source_timestamp=t1)
    m2, b2 = _make_synthetic_leg("KXFED-27DEC-HOLD", "KXFED-27DEC", "HOLD", "0.30", "0.70", source_timestamp=t1)
    m3, b3 = _make_synthetic_leg("KXFED-27DEC-HIKE", "KXFED-27DEC", "HIKE", "0.35", "0.65", source_timestamp=t1)

    # Cycle 2: Negative edge synthetic basket fixture (asks 40¢ + 35¢ + 30¢ = 105¢)
    t2 = t1 + timedelta(seconds=10)
    m1_neg, b1_neg = _make_synthetic_leg("KXFED-27DEC-CUT", "KXFED-27DEC", "CUT", "0.40", "0.60", source_timestamp=t2)
    m2_neg, b2_neg = _make_synthetic_leg("KXFED-27DEC-HOLD", "KXFED-27DEC", "HOLD", "0.35", "0.65", source_timestamp=t2)
    m3_neg, b3_neg = _make_synthetic_leg("KXFED-27DEC-HIKE", "KXFED-27DEC", "HIKE", "0.30", "0.70", source_timestamp=t2)

    baskets = load_declared_baskets(basket_file)
    assert len(baskets) == 1

    # Multi-cycle provider
    class MultiCycleProvider(FixtureMarketDataProvider):
        def __init__(self):
            super().__init__(markets=[m1, m2, m3], books={"KXFED-27DEC-CUT": b1, "KXFED-27DEC-HOLD": b2, "KXFED-27DEC-HIKE": b3}, current_time=t1)
            self._cycle = 0

        def get_market(self, ticker: str):
            if self._cycle > 1:
                mapping = {"KXFED-27DEC-CUT": m1_neg, "KXFED-27DEC-HOLD": m2_neg, "KXFED-27DEC-HIKE": m3_neg}
                return mapping[ticker]
            return super().get_market(ticker)

        def get_order_book(self, ticker: str):
            if self._cycle > 1:
                mapping = {"KXFED-27DEC-CUT": b1_neg, "KXFED-27DEC-HOLD": b2_neg, "KXFED-27DEC-HIKE": b3_neg}
                return mapping[ticker], None
            return super().get_order_book(ticker)

    provider = MultiCycleProvider()

    cfg = V11CollectorConfig(
        session_name="e2e_basket_research_test",
        output_dir=out_dir,
        declared_baskets=baskets,
        poll_interval_seconds=0.0,
        enable_prospective_evaluation=True,
    )
    collector = V11MarketEvidenceCollector(config=cfg, data_provider=provider)

    # Run Cycle 1
    provider._cycle = 1
    provider.current_time = t1
    obs_c1 = collector.run_single_cycle()
    assert obs_c1 == 3

    # Run Cycle 2
    provider._cycle = 2
    provider.current_time = t2
    obs_c2 = collector.run_single_cycle()
    assert obs_c2 == 3

    # Finish and write manifest
    manifest = collector._write_manifest(t1, t2)

    # 1. Verify manifest
    assert manifest.successful_observations == 6
    assert manifest.total_cycles == 2
    assert manifest.basket_evaluations_count == 2
    assert manifest.observations_sha256 is not None
    assert manifest.opportunities_sha256 is not None
    assert manifest.collector_config["declared_baskets_count"] == 1

    # 2. Verify opportunities.jsonl
    opps_path = out_dir / "opportunities.jsonl"
    assert opps_path.exists()
    opp_records = []
    with open(opps_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                opp_records.append(json.loads(line))

    # Expect exactly 2 basket evaluations
    assert len(opp_records) == 2

    # Record 1 (Cycle 1): Qualified profitable basket
    rec1 = opp_records[0]
    assert rec1["strategy_type"] == OPPORTUNITY_MECE_BASKET_LONG_YES
    assert rec1["status"] == STATUS_QUALIFIED
    assert rec1["is_qualified"] is True
    assert rec1["candidate_opportunity"]["gross_edge_cents"] == 10
    assert rec1["pricing_result"]["is_net_profitable"] is True
    assert rec1["paper_trade_id"] is not None

    # Record 2 (Cycle 2): Rejected negative edge basket
    rec2 = opp_records[1]
    assert rec2["strategy_type"] == OPPORTUNITY_MECE_BASKET_LONG_YES
    assert rec2["status"] == STATUS_REJECTED_NO_GROSS_EDGE
    assert rec2["is_qualified"] is False
    assert rec2["candidate_opportunity"]["gross_edge_cents"] == -5
    assert "No gross edge" in rec2["rejection_reason"]

    # 3. Verify ledger reconciliation
    fin = collector.prospective_evaluator.get_financial_summary()
    assert fin["completed_arbs"] == 1
    assert fin["is_ledger_reconciled"] is True
    assert fin["discrepancies"] == ()
    assert fin["baskets_evaluated"] == 2
    assert fin["basket_qualified"] == 1
    assert fin["basket_rej_no_gross_edge"] == 1


def test_declared_basket_adversarial_validation():
    """Verify strict adversarial rejection of malformed or invalid basket declarations."""
    # 1. Invalid event_ticker
    with pytest.raises(ObserverError, match="event_ticker must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
        )
    with pytest.raises(ObserverError, match="event_ticker must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="   ",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
        )
    with pytest.raises(ObserverError, match="event_ticker must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker=123,  # type: ignore
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
        )

    # 2. Invalid outcomes (empty or non-string elements)
    with pytest.raises(ObserverError, match="each outcome must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", ""),
            market_outcome_map={"M1": "A", "M2": ""},
        )
    with pytest.raises(ObserverError, match="each outcome must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", "   "),
            market_outcome_map={"M1": "A", "M2": "   "},
        )
    with pytest.raises(ObserverError, match="each outcome must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", None),  # type: ignore
            market_outcome_map={"M1": "A", "M2": None},  # type: ignore
        )

    # 3. Invalid market tickers
    with pytest.raises(ObserverError, match="market ticker must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", "B"),
            market_outcome_map={"": "A", "M2": "B"},
        )
    with pytest.raises(ObserverError, match="market ticker must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", "B"),
            market_outcome_map={"  ": "A", "M2": "B"},
        )

    # 4. Invalid basket_side
    with pytest.raises(ObserverError, match="basket_side must be"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
            basket_side="INVALID_SIDE",
        )

    # 5. Invalid basket_id
    with pytest.raises(ObserverError, match="basket_id must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
            basket_id="",
        )
    with pytest.raises(ObserverError, match="basket_id must be a non-empty string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
            basket_id="   ",
        )

    # 6. Invalid notes
    with pytest.raises(ObserverError, match="notes must be a string"):
        DeclaredMeceBasket(
            event_ticker="EVT",
            outcomes=("A", "B"),
            market_outcome_map={"M1": "A", "M2": "B"},
            notes=123,  # type: ignore
        )


def test_load_declared_baskets_adversarial(tmp_path):
    """Verify strict adversarial error handling for JSON config loading."""
    # 1. Malformed JSON
    with pytest.raises(ObserverError, match="Invalid basket configuration JSON"):
        load_declared_baskets("{not valid json")

    # 2. Wrong top-level types
    with pytest.raises(ObserverError, match="Basket configuration must be a JSON object or array"):
        load_declared_baskets("12345")
    with pytest.raises(ObserverError, match="Basket configuration must be a JSON object or array"):
        load_declared_baskets("true")
    with pytest.raises(ObserverError, match="Basket configuration must be a JSON object or array"):
        load_declared_baskets("null")
    with pytest.raises(ObserverError, match="Basket configuration must be a JSON object or array"):
        load_declared_baskets(42)

    # 3. List of non-mappings
    with pytest.raises(ObserverError, match="Basket at index 0 must be a mapping/object"):
        load_declared_baskets("[1, 2, 3]")

    # 4. Missing required fields in dictionary
    with pytest.raises(ObserverError, match="missing required basket field: 'event_ticker'"):
        load_declared_baskets([{"outcomes": ["A", "B"], "market_outcome_map": {"M1": "A", "M2": "B"}}])
    with pytest.raises(ObserverError, match="missing required basket field: 'outcomes'"):
        load_declared_baskets([{"event_ticker": "EVT", "market_outcome_map": {"M1": "A", "M2": "B"}}])
    with pytest.raises(ObserverError, match="missing required basket field: 'market_outcome_map'"):
        load_declared_baskets([{"event_ticker": "EVT", "outcomes": ["A", "B"]}])

    # 5. Duplicate basket IDs
    duplicate_bids = json.dumps([
        {
            "basket_id": "SAME_ID",
            "event_ticker": "EVT1",
            "outcomes": ["A", "B"],
            "market_outcome_map": {"M1": "A", "M2": "B"},
        },
        {
            "basket_id": "SAME_ID",
            "event_ticker": "EVT2",
            "outcomes": ["A", "B"],
            "market_outcome_map": {"M3": "A", "M4": "B"},
        },
    ])
    with pytest.raises(ObserverError, match="Duplicate basket_id 'SAME_ID'"):
        load_declared_baskets(duplicate_bids)

    # 6. Duplicate (event_ticker, basket_side)
    duplicate_events = json.dumps([
        {
            "basket_id": "ID1",
            "event_ticker": "EVT_DUP",
            "outcomes": ["A", "B"],
            "market_outcome_map": {"M1": "A", "M2": "B"},
            "basket_side": "yes",
        },
        {
            "basket_id": "ID2",
            "event_ticker": "EVT_DUP",
            "outcomes": ["A", "B"],
            "market_outcome_map": {"M1": "A", "M2": "B"},
            "basket_side": "yes",
        },
    ])
    with pytest.raises(ObserverError, match="Duplicate basket definition for event 'EVT_DUP'"):
        load_declared_baskets(duplicate_events)

    # 7. Non-existent file path
    non_existent = tmp_path / "does_not_exist.json"
    with pytest.raises(ObserverError, match="Basket configuration file not found"):
        load_declared_baskets(non_existent)


def test_canonical_fed_dec_2027_basket_configuration():
    """Verify that configs/baskets/fed_dec_2027_mece.json is valid and covers the 5 audited markets."""
    config_path = Path("configs/baskets/fed_dec_2027_mece.json")
    assert config_path.exists(), "configs/baskets/fed_dec_2027_mece.json must exist"

    baskets = load_declared_baskets(config_path)
    assert len(baskets) == 1
    b = baskets[0]

    assert b.basket_id == "FED-RATE-DEC27"
    assert b.event_ticker == "KXFEDDECISION-27DEC"
    assert b.basket_side == YES
    assert len(b.outcomes) == 5

    expected_tickers = {
        "KXFEDDECISION-27DEC-C26",
        "KXFEDDECISION-27DEC-C25",
        "KXFEDDECISION-27DEC-H0",
        "KXFEDDECISION-27DEC-H25",
        "KXFEDDECISION-27DEC-H26",
    }
    assert set(b.market_outcome_map.keys()) == expected_tickers
    assert len(b.ordered_tickers) == 5
    # Every outcome is unique and mapped 1-to-1
    assert set(b.market_outcome_map.values()) == set(b.outcomes)


def test_evaluated_opportunity_serialization_adversarial():
    """Verify that EvaluatedOpportunity serializes and deserializes cleanly with None candidate."""
    from kalshi_arbitrage.evidence_storage import (
        opportunity_from_dict,
        opportunity_to_dict,
    )

    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    opp = EvaluatedOpportunity(
        observation_id="basket-obs-123",
        opportunity_id="v11-opp-456",
        strategy_type=OPPORTUNITY_MECE_BASKET_LONG_YES,
        observed_at=now,
        source_timestamps={"M1": now, "M2": now},
        leg_timestamp_skew_seconds=0.15,
        market_tickers=("M1", "M2"),
        candidate_opportunity=None,
        pricing_result=None,
        status=STATUS_REJECTED_MISSING_MARKET,
        rejection_reason="Incomplete cycle snapshot",
        fee_per_contract_cents=None,
        is_qualified=False,
        is_recurrent=False,
        paper_trade_id=None,
    )

    d = opportunity_to_dict(opp)
    assert d["candidate_opportunity"] is None
    assert d["pricing_result"] is None
    assert d["status"] == STATUS_REJECTED_MISSING_MARKET

    reloaded = opportunity_from_dict(d)
    assert reloaded.observation_id == opp.observation_id
    assert reloaded.opportunity_id == opp.opportunity_id
    assert reloaded.strategy_type == opp.strategy_type
    assert reloaded.observed_at == opp.observed_at
    assert reloaded.leg_timestamp_skew_seconds == opp.leg_timestamp_skew_seconds
    assert reloaded.market_tickers == opp.market_tickers
    assert reloaded.candidate_opportunity is None
    assert reloaded.pricing_result is None
    assert reloaded.status == opp.status
    assert reloaded.rejection_reason == opp.rejection_reason
    assert reloaded.is_qualified is False


def test_manifest_hash_behavior(tmp_path):
    """Verify cryptographic manifest hashing handles missing, empty, and populated files correctly."""
    import hashlib
    from kalshi_arbitrage.v11_collector import _compute_file_sha256

    missing_file = tmp_path / "missing.jsonl"
    assert _compute_file_sha256(missing_file) is None

    empty_file = tmp_path / "empty.jsonl"
    empty_file.touch()
    expected_empty_hash = hashlib.sha256(b"").hexdigest()
    assert _compute_file_sha256(empty_file) == expected_empty_hash

    content = b'{"record": 1}\n{"record": 2}\n'
    populated_file = tmp_path / "populated.jsonl"
    populated_file.write_bytes(content)
    expected_hash = hashlib.sha256(content).hexdigest()
    assert _compute_file_sha256(populated_file) == expected_hash
