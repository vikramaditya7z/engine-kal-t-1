"""Comprehensive automated test suite for Kalshi Arbitrage Engine V22 Execution-Aware Qualification.

Verifies:
1. ExecutionQualifierConfig parameter validation and boundaries.
2. Binary parity depth traversal, VWAP, and exact integer cents calculations.
3. Multi-leg liquidity bottleneck matching and depth re-traversal.
4. Exact official CFTC quadratic taker fee modeling per leg and multi-tier.
5. Qualification outcome classification:
   - QUALIFIED (positive net edge, sufficient depth, within latency/freshness bounds)
   - REJECTED_INSUFFICIENT_DEPTH (zero depth or partial fill when require_full=True)
   - REJECTED_UNPROFITABLE_FEES (positive gross edge swallowed by transaction fees)
   - REJECTED_NO_GROSS_EDGE (gross cost >= guaranteed payout)
   - REJECTED_STALE (observation age exceeds max_stale_seconds)
   - REJECTED_LEG_SKEW (multi-leg timestamp skew exceeds max_leg_skew_seconds)
   - REJECTED_MISSING_MARKET (missing leg observation in declared MECE basket)
   - REJECTED_MISSING_TIMESTAMP (missing source timestamp when required)
6. MECE event basket execution qualification for both LONG_YES and LONG_NO strategies.
7. Multi-level slippage (VWAP) calculation and quadratic fee tiering.
8. Full round-trip serialization and deserialization of CandidateQualification records.
9. End-to-end integration with ContinuousEvaluator, state persistence, and transition history.
10. Dashboard DataService and HTTP server API endpoint (/api/evaluator/qualified).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from typing import Dict, List, Optional, Sequence
import urllib.request
import uuid

import pytest

from kalshi_arbitrage import (
    ArbitrageCandidate,
    CandidateQualification,
    CandidateState,
    CandidateTransition,
    ContinuousEvaluator,
    ContinuousEvaluatorConfig,
    ExecutionAwareArbitrageQualifier,
    ExecutionQualifierConfig,
    FeeScheduleType,
    LegExecutionDetail,
    QualificationOutcome,
)
from kalshi_arbitrage.arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
)
from kalshi_arbitrage.dashboard.server import create_dashboard_server
from kalshi_arbitrage.dashboard.service import DashboardDataService
from kalshi_arbitrage.evidence_storage import observation_to_dict
from kalshi_arbitrage.fee_model import FeeModelConfig
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import DeclaredMeceBasket, MarketObservation


# -----------------------------------------------------------------------------
# Test Fixture Helpers
# -----------------------------------------------------------------------------

def make_market(ticker: str, event_ticker: str = "KXTEST") -> NormalizedMarket:
    now = datetime.now(timezone.utc)
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status="open",
        title=f"Test Market {ticker}",
        created_time=now,
        open_time=now,
        close_time=now + timedelta(days=1),
        expiration_time=now + timedelta(days=1),
    )


def make_single_level_book(
    ticker: str,
    yes_bid_dollars: str,
    no_bid_dollars: str,
    yes_bid_qty: str = "10",
    no_bid_qty: str = "10",
    source_timestamp: Optional[datetime] = None,
) -> NormalizedOrderBook:
    """Builds a test order book with 1 level of bids.
    Remember Kalshi binary convention:
    - Resting YES bid at P_yes implies available NO ask at (1.00 - P_yes) with YES bid quantity.
    - Resting NO bid at P_no implies available YES ask at (1.00 - P_no) with NO bid quantity.
    """
    ts = source_timestamp or datetime.now(timezone.utc)
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(
            OrderBookLevel(
                price_dollars=Decimal(yes_bid_dollars),
                quantity=Decimal(yes_bid_qty),
            ),
        ),
        no_bids=(
            OrderBookLevel(
                price_dollars=Decimal(no_bid_dollars),
                quantity=Decimal(no_bid_qty),
            ),
        ),
        source_timestamp=ts,
    )


def make_multilevel_book(
    ticker: str,
    yes_bids: Sequence[tuple],  # (price_dollars, qty)
    no_bids: Sequence[tuple],   # (price_dollars, qty)
    source_timestamp: Optional[datetime] = None,
) -> NormalizedOrderBook:
    ts = source_timestamp or datetime.now(timezone.utc)
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=tuple(
            OrderBookLevel(price_dollars=Decimal(p), quantity=Decimal(q))
            for p, q in yes_bids
        ),
        no_bids=tuple(
            OrderBookLevel(price_dollars=Decimal(p), quantity=Decimal(q))
            for p, q in no_bids
        ),
        source_timestamp=ts,
    )


def make_obs(
    ticker: str,
    book: Optional[NormalizedOrderBook],
    observed_at: Optional[datetime] = None,
    source_timestamp: Optional[datetime] = None,
    is_stale: bool = False,
    staleness_reason: Optional[str] = None,
    event_ticker: str = "KXTEST",
) -> MarketObservation:
    now = observed_at or datetime.now(timezone.utc)
    src_ts = source_timestamp or (book.source_timestamp if book else now)
    return MarketObservation(
        observation_id=f"obs-{ticker}-{uuid.uuid4().hex[:6]}",
        ticker=ticker,
        observed_at=now,
        source_timestamp=src_ts,
        market=make_market(ticker, event_ticker=event_ticker),
        order_book=book,
        is_success=book is not None,
        is_stale=is_stale,
        staleness_reason=staleness_reason,
    )


# -----------------------------------------------------------------------------
# Unit Tests: Qualifier Configuration & Initialization
# -----------------------------------------------------------------------------

def test_qualifier_config_defaults():
    config = ExecutionQualifierConfig()
    assert config.target_quantity == 1
    assert config.max_stale_seconds == 60.0
    assert config.max_leg_skew_seconds == 5.0
    assert config.require_full_quantity is True
    assert config.min_net_profit_cents == 1
    assert config.require_source_timestamp is True
    assert config.fee_model_config.schedule_type == FeeScheduleType.KALSHI_OFFICIAL_TAKER


def test_qualifier_config_validation():
    with pytest.raises(ValueError, match="target_quantity must be a positive integer"):
        ExecutionQualifierConfig(target_quantity=0)

    with pytest.raises(ValueError, match="max_stale_seconds must be positive"):
        ExecutionQualifierConfig(max_stale_seconds=-1.0)

    with pytest.raises(ValueError, match="max_leg_skew_seconds must be positive"):
        ExecutionQualifierConfig(max_leg_skew_seconds=-0.5)


# -----------------------------------------------------------------------------
# Unit Tests: Binary Parity Execution Qualification
# -----------------------------------------------------------------------------

def test_binary_parity_qualified_deep_liquidity():
    """Binary parity with clear positive net edge and deep liquidity qualifies."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=5, min_net_profit_cents=1)
    )
    now = datetime.now(timezone.utc)
    # YES ask = 1.00 - 0.58 = 0.42 (42c) with depth 10
    # NO ask  = 1.00 - 0.55 = 0.45 (45c) with depth 10
    # Total pair cost = 87c. Gross edge = 100 - 87 = 13c per unit.
    book = make_single_level_book("MKT1", yes_bid_dollars="0.55", no_bid_dollars="0.58", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED
    assert qual.rejection_reason is None
    assert qual.requested_quantity == 5
    assert qual.executable_quantity == 5
    assert qual.is_partial_fill is False

    # Financial arithmetic: 5 units @ 87c = 435c total cost. Guaranteed payout = 5 * 100 = 500c.
    assert qual.total_cost_cents == 435
    assert qual.guaranteed_payout_cents == 500
    assert qual.gross_edge_cents == 65
    assert qual.gross_edge_per_unit_cents == Decimal("13")

    # Fees: CFTC taker fee per leg
    # Leg YES: p=42c, C=5 -> fee = ceil(0.07 * 5 * 0.42 * 0.58 * 100) = ceil(8.526) = 9c
    # Leg NO:  p=45c, C=5 -> fee = ceil(0.07 * 5 * 0.45 * 0.55 * 100) = ceil(8.6625) = 9c
    assert qual.fees_per_leg_cents["MKT1:YES"] == 9
    assert qual.fees_per_leg_cents["MKT1:NO"] == 9
    assert qual.total_fees_cents == 18

    # Net edge = 65 - 18 = 47c
    assert qual.net_edge_cents == 47
    assert qual.net_edge_per_unit_cents == Decimal("9.4")
    # Return on capital = (47 / 435) * 100 = 10.8045977%
    assert qual.return_on_capital_pct is not None
    assert qual.return_on_capital_pct > Decimal("10.0")

    # Leg details
    assert len(qual.leg_details) == 2
    leg_yes = next(l for l in qual.leg_details if l.side == "YES")
    assert leg_yes.executable_quantity == 5
    assert leg_yes.available_depth_contracts == 10
    assert leg_yes.top_of_book_price_cents == 42
    assert leg_yes.average_price_cents == Decimal("42.0")
    assert leg_yes.total_cost_cents == 210
    assert leg_yes.fee_cents == 9


def test_binary_parity_rejected_unprofitable_fees():
    """Candidates with gross edge swallowed by transaction fees are rejected."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=1, min_net_profit_cents=1)
    )
    now = datetime.now(timezone.utc)
    # YES ask = 1.00 - 0.51 = 0.49 (49c)
    # NO ask  = 1.00 - 0.50 = 0.50 (50c)
    # Total pair cost = 99c. Gross edge = 100 - 99 = 1c for 1 contract.
    # Official taker fee:
    # YES leg (49c): ceil(0.07 * 1 * 0.49 * 0.51 * 100) = ceil(1.7493) = 2c
    # NO leg (50c):  ceil(0.07 * 1 * 0.50 * 0.50 * 100) = ceil(1.75) = 2c
    # Total fees = 4c.
    # Net edge = 1 - 4 = -3c <= 0.
    book = make_single_level_book("MKT1", yes_bid_dollars="0.50", no_bid_dollars="0.51", source_timestamp=now)
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_UNPROFITABLE_FEES
    assert "Unprofitable after fees" in qual.rejection_reason
    assert qual.gross_edge_cents == 1
    assert qual.total_fees_cents == 4
    assert qual.net_edge_cents == -3


def test_binary_parity_rejected_no_gross_edge():
    """Markets without gross edge (cost >= payout) are rejected as REJECTED_NO_GROSS_EDGE."""
    qualifier = ExecutionAwareArbitrageQualifier()
    now = datetime.now(timezone.utc)
    # YES ask = 47c, NO ask = 55c -> Total cost = 102c >= 100c
    book = make_single_level_book("MKT1", yes_bid_dollars="0.45", no_bid_dollars="0.53", source_timestamp=now)
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_NO_GROSS_EDGE
    assert "No gross edge" in qual.rejection_reason
    assert qual.gross_edge_cents <= 0


def test_binary_parity_bottleneck_depth_partial_fill_allowed():
    """When legs have asymmetric depth, bottleneck quantity is chosen and re-traversed."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=10, require_full_quantity=False)
    )
    now = datetime.now(timezone.utc)
    # YES ask has depth 10 (NO bid qty 10)
    # NO ask has depth 4 (YES bid qty 4)
    # Bottleneck quantity = min(10, 4) = 4
    book = make_single_level_book("MKT1", yes_bid_dollars="0.55", no_bid_dollars="0.58", yes_bid_qty="4", no_bid_qty="10", source_timestamp=now)
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED
    assert qual.requested_quantity == 10
    assert qual.executable_quantity == 4
    assert qual.is_partial_fill is True
    assert qual.total_cost_cents == 4 * 87


def test_binary_parity_bottleneck_depth_partial_fill_rejected():
    """When require_full_quantity=True and depth is partial, rejection occurs."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=10, require_full_quantity=True)
    )
    now = datetime.now(timezone.utc)
    # Depth = 4 < 10
    book = make_single_level_book("MKT1", yes_bid_dollars="0.55", no_bid_dollars="0.58", yes_bid_qty="4", no_bid_qty="10", source_timestamp=now)
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH
    assert "Insufficient depth for full requested quantity" in qual.rejection_reason
    assert qual.requested_quantity == 10
    assert qual.executable_quantity == 4


def test_binary_parity_rejected_empty_book():
    """When one leg has 0 resting depth, rejection occurs."""
    qualifier = ExecutionAwareArbitrageQualifier()
    now = datetime.now(timezone.utc)
    # Empty book
    book = NormalizedOrderBook(
        market_ticker="MKT1",
        yes_bids=(),
        no_bids=(),
        source_timestamp=now,
    )
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_INSUFFICIENT_DEPTH
    assert qual.executable_quantity == 0


def test_binary_parity_rejected_stale_observation():
    """Stale observations exceeding max_stale_seconds fail closed as REJECTED_STALE."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(max_stale_seconds=30.0)
    )
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(seconds=45)
    book = make_single_level_book("MKT1", yes_bid_dollars="0.55", no_bid_dollars="0.58", source_timestamp=stale_time)
    obs = make_obs("MKT1", book, observed_at=stale_time, source_timestamp=stale_time)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_STALE
    assert "exceeds limit" in qual.rejection_reason
    assert qual.data_freshness_seconds >= 45.0


def test_binary_parity_rejected_missing_source_timestamp():
    """When require_source_timestamp=True and timestamp is absent, rejected."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(require_source_timestamp=True)
    )
    now = datetime.now(timezone.utc)
    book = NormalizedOrderBook(
        market_ticker="MKT1",
        yes_bids=(OrderBookLevel(price_dollars=Decimal("0.55"), quantity=Decimal("10")),),
        no_bids=(OrderBookLevel(price_dollars=Decimal("0.58"), quantity=Decimal("10")),),
        source_timestamp=None,
    )
    obs = make_obs("MKT1", book, observed_at=now, source_timestamp=None)
    # Ensure source_timestamp is explicitly None on observation
    obs = MarketObservation(
        observation_id=obs.observation_id,
        ticker=obs.ticker,
        observed_at=obs.observed_at,
        source_timestamp=None,
        market=obs.market,
        order_book=obs.order_book,
        is_success=obs.is_success,
        is_stale=False,
    )

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_MISSING_TIMESTAMP
    assert "source_timestamp" in qual.rejection_reason.lower()


# -----------------------------------------------------------------------------
# Unit Tests: MECE Event Basket Qualification
# -----------------------------------------------------------------------------

def test_mece_basket_qualified_long_yes():
    """3-outcome MECE basket LONG_YES with sum(ask_yes) = 85c qualifies."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=5, min_net_profit_cents=1)
    )
    now = datetime.now(timezone.utc)
    basket = DeclaredMeceBasket(
        event_ticker="KXEVENT1",
        basket_id="BASKET_TEST_1",
        outcomes=("OUT1", "OUT2", "OUT3"),
        market_outcome_map={"MKT1": "OUT1", "MKT2": "OUT2", "MKT3": "OUT3"},
        basket_side="YES",
    )
    # YES ask = 1.00 - NO bid
    # MKT1: NO bid = 0.72 -> YES ask = 0.28 (28c), depth 10
    # MKT2: NO bid = 0.70 -> YES ask = 0.30 (30c), depth 10
    # MKT3: NO bid = 0.73 -> YES ask = 0.27 (27c), depth 10
    # Sum of YES asks = 28 + 30 + 27 = 85c. Payout = 100c.
    # Gross edge = 15c per unit. 5 units -> Gross edge = 75c.
    obs_map = {
        "MKT1": make_obs("MKT1", make_single_level_book("MKT1", "0.20", "0.72", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)),
        "MKT2": make_obs("MKT2", make_single_level_book("MKT2", "0.20", "0.70", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)),
        "MKT3": make_obs("MKT3", make_single_level_book("MKT3", "0.20", "0.73", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)),
    }

    qual = qualifier.qualify_mece_basket(basket, obs_map, now=now)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED
    assert qual.strategy_type == OPPORTUNITY_MECE_BASKET_LONG_YES
    assert qual.requested_quantity == 5
    assert qual.executable_quantity == 5
    assert qual.total_cost_cents == 5 * 85  # 425c
    assert qual.guaranteed_payout_cents == 500
    assert qual.gross_edge_cents == 75
    assert qual.total_fees_cents > 0
    assert qual.net_edge_cents > 0
    assert len(qual.leg_details) == 3


def test_mece_basket_qualified_long_no():
    """3-outcome MECE basket LONG_NO with sum(ask_no) = 175c qualifies against 200c payout."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=2, min_net_profit_cents=1)
    )
    now = datetime.now(timezone.utc)
    basket = DeclaredMeceBasket(
        event_ticker="KXEVENT1",
        basket_id="BASKET_TEST_2",
        outcomes=("OUT1", "OUT2", "OUT3"),
        market_outcome_map={"MKT1": "OUT1", "MKT2": "OUT2", "MKT3": "OUT3"},
        basket_side="NO",
    )
    # NO ask = 1.00 - YES bid
    # MKT1: YES bid = 0.42 -> NO ask = 0.58, depth 10
    # MKT2: YES bid = 0.42 -> NO ask = 0.58, depth 10
    # MKT3: YES bid = 0.41 -> NO ask = 0.59, depth 10
    # Sum of NO asks = 58 + 58 + 59 = 175c.
    # Guaranteed payout = (3 - 1) * 100c = 200c per unit.
    # Gross edge = 200 - 175 = 25c per unit. 2 units -> Gross edge = 50c.
    obs_map = {
        "MKT1": make_obs("MKT1", make_single_level_book("MKT1", "0.42", "0.30", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)),
        "MKT2": make_obs("MKT2", make_single_level_book("MKT2", "0.42", "0.30", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)),
        "MKT3": make_obs("MKT3", make_single_level_book("MKT3", "0.41", "0.30", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)),
    }

    qual = qualifier.qualify_mece_basket(basket, obs_map, now=now)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED
    assert qual.strategy_type == OPPORTUNITY_MECE_BASKET_LONG_NO
    assert qual.requested_quantity == 2
    assert qual.executable_quantity == 2
    assert qual.total_cost_cents == 2 * 175  # 350c
    assert qual.guaranteed_payout_cents == 400  # 2 * 200c
    assert qual.gross_edge_cents == 50
    assert qual.net_edge_cents > 0


def test_mece_basket_rejected_missing_market():
    """If a declared outcome is missing from snapshot, fails as REJECTED_MISSING_MARKET."""
    qualifier = ExecutionAwareArbitrageQualifier()
    now = datetime.now(timezone.utc)
    basket = DeclaredMeceBasket(
        event_ticker="KXEVENT1",
        basket_id="BASKET_TEST_3",
        outcomes=("OUT1", "OUT2", "OUT3"),
        market_outcome_map={"MKT1": "OUT1", "MKT2": "OUT2", "MKT3": "OUT3"},
        basket_side="YES",
    )
    # Only MKT1 and MKT2 provided
    obs_map = {
        "MKT1": make_obs("MKT1", make_single_level_book("MKT1", "0.20", "0.72", source_timestamp=now)),
        "MKT2": make_obs("MKT2", make_single_level_book("MKT2", "0.20", "0.70", source_timestamp=now)),
    }

    qual = qualifier.qualify_mece_basket(basket, obs_map, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_MISSING_MARKET
    assert "missing" in qual.rejection_reason.lower()


def test_mece_basket_rejected_leg_skew():
    """If leg timestamps skew exceeds max_leg_skew_seconds, fails as REJECTED_LEG_SKEW."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(max_leg_skew_seconds=5.0)
    )
    now = datetime.now(timezone.utc)
    t1 = now
    t2 = now - timedelta(seconds=8)  # 8 second skew > 5.0s
    basket = DeclaredMeceBasket(
        event_ticker="KXEVENT1",
        basket_id="BASKET_TEST_4",
        outcomes=("OUT1", "OUT2"),
        market_outcome_map={"MKT1": "OUT1", "MKT2": "OUT2"},
        basket_side="YES",
    )
    obs_map = {
        "MKT1": make_obs("MKT1", make_single_level_book("MKT1", "0.20", "0.72", source_timestamp=t1)),
        "MKT2": make_obs("MKT2", make_single_level_book("MKT2", "0.20", "0.70", source_timestamp=t2)),
    }

    qual = qualifier.qualify_mece_basket(basket, obs_map, now=now)

    assert qual.is_qualified is False
    assert qual.outcome == QualificationOutcome.REJECTED_LEG_SKEW
    assert "skew" in qual.rejection_reason.lower()
    assert qual.max_observed_skew_seconds >= 8.0


# -----------------------------------------------------------------------------
# Unit Tests: Multi-Level Depth Slippage (VWAP) & Fee Calculation
# -----------------------------------------------------------------------------

def test_multilevel_slippage_and_vwap_qualification():
    """Multi-level depth consumption calculates accurate VWAP and tier-based fees."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=10, min_net_profit_cents=1)
    )
    now = datetime.now(timezone.utc)
    # YES ask derives from NO bids:
    # NO bids: 5 @ 0.60 (implies YES ask 40c qty 5), 5 @ 0.58 (implies YES ask 42c qty 5)
    # Traversing 10 YES contracts:
    # 5 @ 40c = 200c
    # 5 @ 42c = 210c
    # Total YES cost = 410c. VWAP = 41.0c.
    # NO ask derives from YES bids:
    # YES bids: 10 @ 0.55 (implies NO ask 45c qty 10)
    # Traversing 10 NO contracts: 10 @ 45c = 450c. VWAP = 45.0c.
    # Total pair cost = 410 + 450 = 860c. Payout = 1000c. Gross edge = 140c.
    book = make_multilevel_book(
        "MKT1",
        yes_bids=(("0.55", "10"),),
        no_bids=(("0.58", "5"), ("0.60", "5")),
        source_timestamp=now,
    )
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)

    assert qual.is_qualified is True
    assert qual.outcome == QualificationOutcome.QUALIFIED
    assert qual.requested_quantity == 10
    assert qual.executable_quantity == 10
    assert qual.total_cost_cents == 860
    assert qual.guaranteed_payout_cents == 1000
    assert qual.gross_edge_cents == 140
    assert qual.net_edge_cents > 0

    leg_yes = next(l for l in qual.leg_details if l.side == "YES")
    assert leg_yes.top_of_book_price_cents == 40
    assert leg_yes.average_price_cents == Decimal("41.0")
    assert leg_yes.total_cost_cents == 410
    assert len(leg_yes.consumed_levels) == 2


# -----------------------------------------------------------------------------
# Unit Tests: CandidateQualification Serialization Round-Trip
# -----------------------------------------------------------------------------

def test_qualification_serialization_roundtrip():
    """CandidateQualification serializes to dict and reconstructs identically."""
    qualifier = ExecutionAwareArbitrageQualifier(
        config=ExecutionQualifierConfig(target_quantity=5)
    )
    now = datetime.now(timezone.utc)
    book = make_single_level_book("MKT1", "0.55", "0.58", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)
    obs = make_obs("MKT1", book, observed_at=now)

    qual = qualifier.qualify_binary_parity(obs, now=now)
    d = qual.to_dict()

    # JSON serialization safety
    json_str = json.dumps(d)
    d_loaded = json.loads(json_str)

    qual_reconstructed = CandidateQualification.from_dict(d_loaded)

    assert qual_reconstructed.qualification_id == qual.qualification_id
    assert qual_reconstructed.candidate_id == qual.candidate_id
    assert qual_reconstructed.outcome == qual.outcome
    assert qual_reconstructed.is_qualified == qual.is_qualified
    assert qual_reconstructed.gross_edge_cents == qual.gross_edge_cents
    assert qual_reconstructed.gross_edge_per_unit_cents == qual.gross_edge_per_unit_cents
    assert qual_reconstructed.total_cost_cents == qual.total_cost_cents
    assert qual_reconstructed.total_fees_cents == qual.total_fees_cents
    assert qual_reconstructed.net_edge_cents == qual.net_edge_cents
    assert qual_reconstructed.net_edge_per_unit_cents == qual.net_edge_per_unit_cents
    assert qual_reconstructed.return_on_capital_pct == qual.return_on_capital_pct
    assert len(qual_reconstructed.leg_details) == len(qual.leg_details)
    assert qual_reconstructed.leg_details[0].market_ticker == qual.leg_details[0].market_ticker


# -----------------------------------------------------------------------------
# Integration Tests: ContinuousEvaluator Integration
# -----------------------------------------------------------------------------

def test_continuous_evaluator_integration_with_qualification(tmp_path: Path):
    """ContinuousEvaluator executes qualification on each cycle and populates state and health."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    obs_file = data_dir / "observations.jsonl"
    state_file = data_dir / "evaluator_state.json"
    history_file = data_dir / "candidate_history.jsonl"

    # Pre-populate observation stream with qualifying arbitrage book
    now = datetime.now(timezone.utc)
    book = make_single_level_book("MKT_QUAL", "0.55", "0.58", yes_bid_qty="10", no_bid_qty="10", source_timestamp=now)
    obs = make_obs("MKT_QUAL", book, observed_at=now)

    with open(obs_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(observation_to_dict(obs)) + "\n")

    config = ContinuousEvaluatorConfig(
        poll_interval_seconds=0.05,
        max_stale_seconds=60.0,
        target_quantity=5,
        require_full_quantity=True,
        min_net_profit_cents=1,
        observations_file=obs_file,
        state_file=state_file,
        history_file=history_file,
        max_cycles=3,
    )

    evaluator = ContinuousEvaluator(config=config)
    evaluator.run()

    health = evaluator.get_health()
    assert health.evaluations_count >= 1
    assert health.processed_observations_count >= 1
    assert health.qualified_candidates_count >= 1

    # Verify qualified candidates accessor
    qualified_candidates = evaluator.get_qualified_candidates()
    assert len(qualified_candidates) >= 1
    candidate = qualified_candidates[0]
    assert candidate["is_qualified"] is True
    assert candidate["qualification"] is not None
    assert candidate["qualification"]["outcome"] == QualificationOutcome.QUALIFIED.value
    assert candidate["qualification"]["executable_quantity"] == 5

    # Verify summary
    summary = evaluator.get_qualification_summary()
    assert summary["qualified_count"] >= 1
    assert summary["outcomes_breakdown"].get("QUALIFIED", 0) >= 1

    # Verify saved state file
    assert state_file.exists()
    with open(state_file, "r", encoding="utf-8") as f:
        state_data = json.load(f)
    assert state_data["qualified_candidates_count"] >= 1
    assert len(state_data["active_candidates"]) >= 1
    active_cand = state_data["active_candidates"][0]
    assert "qualification" in active_cand
    assert active_cand["qualification"]["outcome"] == "QUALIFIED"

    # Verify history file has qualification_outcome
    assert history_file.exists()
    with open(history_file, "r", encoding="utf-8") as f:
        transitions = [json.loads(line) for line in f if line.strip()]
    assert len(transitions) >= 1
    assert transitions[0]["qualification_outcome"] == "QUALIFIED"


def test_continuous_evaluator_unprofitable_fees_candidate(tmp_path: Path):
    """ContinuousEvaluator flags candidates with positive gross edge but unprofitable fees."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    obs_file = data_dir / "observations.jsonl"
    state_file = data_dir / "evaluator_state.json"
    history_file = data_dir / "candidate_history.jsonl"

    now = datetime.now(timezone.utc)
    # YES ask = 49c, NO ask = 50c -> Cost = 99c, Gross edge = 1c, Fees = 4c -> Net edge = -3c
    book = make_single_level_book("MKT_FEE_REJECT", "0.50", "0.51", yes_bid_qty="1", no_bid_qty="1", source_timestamp=now)
    obs = make_obs("MKT_FEE_REJECT", book, observed_at=now)

    with open(obs_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(observation_to_dict(obs)) + "\n")

    config = ContinuousEvaluatorConfig(
        poll_interval_seconds=0.05,
        target_quantity=1,
        min_net_profit_cents=1,
        observations_file=obs_file,
        state_file=state_file,
        history_file=history_file,
        max_cycles=2,
    )

    evaluator = ContinuousEvaluator(config=config)
    evaluator.run()

    health = evaluator.get_health()
    assert health.rejected_candidates_count >= 1
    assert health.qualified_candidates_count == 0

    summary = evaluator.get_qualification_summary()
    assert summary["outcomes_breakdown"].get("REJECTED_UNPROFITABLE_FEES", 0) >= 1


# -----------------------------------------------------------------------------
# Integration Tests: Dashboard DataService & HTTP Server APIs
# -----------------------------------------------------------------------------

def test_dashboard_api_qualified_candidates(tmp_path: Path):
    """Dashboard DataService and HTTP server route /api/evaluator/qualified correctly."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_file = data_dir / "live_evaluator_state.json"

    # Write mock state with 1 qualified and 1 non-qualified candidate
    mock_state = {
        "status": "HEALTHY",
        "session_id": "test-session",
        "evaluations_count": 10,
        "processed_observations_count": 25,
        "active_candidates_count": 2,
        "total_candidates_detected": 2,
        "candidate_transitions_count": 2,
        "qualified_candidates_count": 1,
        "rejected_candidates_count": 1,
        "active_candidates": [
            {
                "candidate_id": "cand-qualified-1",
                "strategy_type": OPPORTUNITY_BINARY_PARITY,
                "market_ticker": "KXQUAL",
                "state": "NEW",
                "is_qualified": True,
                "qualification": {
                    "qualification_id": "qual-1",
                    "candidate_id": "cand-qualified-1",
                    "strategy_type": OPPORTUNITY_BINARY_PARITY,
                    "outcome": "QUALIFIED",
                    "is_qualified": True,
                    "rejection_reason": None,
                    "requested_quantity": 5,
                    "executable_quantity": 5,
                    "net_edge_cents": 45,
                },
            },
            {
                "candidate_id": "cand-rejected-1",
                "strategy_type": OPPORTUNITY_BINARY_PARITY,
                "market_ticker": "KXREJECT",
                "state": "NEW",
                "is_qualified": False,
                "qualification": {
                    "qualification_id": "qual-2",
                    "candidate_id": "cand-rejected-1",
                    "strategy_type": OPPORTUNITY_BINARY_PARITY,
                    "outcome": "REJECTED_UNPROFITABLE_FEES",
                    "is_qualified": False,
                    "rejection_reason": "Net edge -2 <= 0",
                    "requested_quantity": 5,
                    "executable_quantity": 5,
                    "net_edge_cents": -2,
                },
            },
        ],
    }

    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(mock_state, f)

    # Test DashboardDataService method
    service = DashboardDataService(workspace_root=tmp_path)
    qualified = service.get_continuous_evaluator_qualified_candidates()
    assert len(qualified) == 1
    assert qualified[0]["candidate_id"] == "cand-qualified-1"

    # Test HTTP Server endpoint
    server = create_dashboard_server(host="127.0.0.1", port=0, workspace_root=tmp_path)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        req = urllib.request.Request(f"{base_url}/api/evaluator/qualified")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["count"] == 1
            assert len(data["qualified_candidates"]) == 1
            assert data["qualified_candidates"][0]["candidate_id"] == "cand-qualified-1"
    finally:
        server.stop()
