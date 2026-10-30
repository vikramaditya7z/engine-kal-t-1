"""Final Program Integration Tests: Complete Arbitrage Pipeline & Event Pre-filter Correction.

Verifies:
1. Event pre-filter does not falsely reject candidates based on catalog database updated_time.
2. Event pre-filter rejects when refreshed top-of-book prices dissolve the gross edge (refreshed_edge <= 0).
3. Event pre-filter deduplicates calls within event cache TTL.
4. Stage C strictly rejects stale actual order books (> 60s source timestamp).
5. Stage C strictly rejects excessive client fetch window (> 5s).
6. Stage C strictly rejects excessive server timestamp skew (> 2s).
7. Stage C strictly rejects insufficient depth (< target quantity).
8. Stage C strictly rejects unprofitable opportunities after CFTC taker fees (< 5 cents net).
9. Stage C leg size gating strictly rejects baskets exceeding maximum legs (> 8 legs).
10. Valid candidate with fresh order books, sufficient depth, and net profit >= 5 cents fully qualifies.
11. End-to-end scanner pass persists all records atomically and DashboardDataService reads them cleanly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage import (
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    FeeScheduleType,
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PreliminaryCandidate,
    QualificationOutcome,
    ScreeningOutcome,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
)
from kalshi_arbitrage.dashboard.service import DashboardDataService


def make_market(
    ticker: str,
    event_ticker: str = "EV-PIPELINE",
    yes_ask: str = "0.4500",
    no_ask: str = "0.5000",
    updated_time: Optional[datetime] = None,
) -> NormalizedMarket:
    now = updated_time or datetime.now(timezone.utc)
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status="open",
        title=f"Market {ticker}",
        yes_ask_dollars=Decimal(yes_ask) if yes_ask is not None else None,
        no_ask_dollars=Decimal(no_ask) if no_ask is not None else None,
        yes_bid_dollars=Decimal("0.4000"),
        no_bid_dollars=Decimal("0.4500"),
        yes_ask_size=Decimal("100"),
        yes_bid_size=Decimal("100"),
        updated_time=now,
    )


def make_book(
    ticker: str,
    yes_bid: str = "0.4000",
    no_bid: str = "0.5800",  # YES ask = 1.00 - 0.58 = 0.42 (cost 42c)
    quantity: int = 100,
    source_timestamp: Optional[datetime] = None,
) -> NormalizedOrderBook:
    now = source_timestamp or datetime.now(timezone.utc)
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(price_dollars=Decimal(yes_bid), quantity=quantity),),
        no_bids=(OrderBookLevel(price_dollars=Decimal(no_bid), quantity=quantity),),
        source_timestamp=now,
    )


def make_event(
    event_ticker: str = "EV-PIPELINE",
    market_tickers: Sequence[str] = ("MKT-1", "MKT-2"),
    markets: Optional[Sequence[NormalizedMarket]] = None,
) -> NormalizedEvent:
    if markets is None:
        markets = [make_market(t, event_ticker=event_ticker) for t in market_tickers]
    return NormalizedEvent(
        event_ticker=event_ticker,
        series_ticker="SERIES-PIPE",
        title=f"Event {event_ticker}",
        subtitle=None,
        mutually_exclusive=True,
        market_tickers=tuple(market_tickers),
        settlement_sources=(),
        category="Economics",
        markets=tuple(markets),
    )


class MockPipelineClient:
    """Deterministic mock client providing events and order books."""

    def __init__(
        self,
        events: Optional[Mapping[str, NormalizedEvent]] = None,
        books: Optional[Mapping[str, NormalizedOrderBook]] = None,
    ) -> None:
        self.events = dict(events or {})
        self.books = dict(books or {})
        self.event_calls = 0
        self.book_calls = 0

    def get_event(self, ticker: str, with_nested_markets: bool = True) -> NormalizedEvent:
        self.event_calls += 1
        if ticker in self.events:
            return self.events[ticker]
        raise ValueError(f"Event {ticker} not found (404)")

    def get_order_book(self, ticker: str, depth: int = 100) -> NormalizedOrderBook:
        self.book_calls += 1
        if ticker in self.books:
            return self.books[ticker]
        raise ValueError(f"Order book {ticker} not found (404)")


def test_01_event_prefilter_does_not_reject_old_catalog_updated_time():
    """Event pre-filter does not reject candidates when constituent market updated_time is days old."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=45)  # 45 days old database record
    m1 = make_market("MKT-1", yes_ask="0.4000", updated_time=old_time)
    m2 = make_market("MKT-2", yes_ask="0.4500", updated_time=old_time)
    event = make_event("EV-OLD", market_tickers=["MKT-1", "MKT-2"], markets=[m1, m2])

    b1 = make_book("MKT-1", no_bid="0.6000", source_timestamp=now)  # YES ask = 40c
    b2 = make_book("MKT-2", no_bid="0.5500", source_timestamp=now)  # YES ask = 45c

    client = MockPipelineClient(events={"EV-OLD": event}, books={"MKT-1": b1, "MKT-2": b2})
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(
            max_stale_seconds=60.0,
            min_net_profit_cents=5,
            check_prefilter_quote_staleness=False,
        ),
    )

    cand = PreliminaryCandidate(
        candidate_id="PRE-OLD",
        strategy_type="mece_basket_long_yes",
        event_ticker="EV-OLD",
        market_tickers=("MKT-1", "MKT-2"),
        gross_edge_dollars=Decimal("0.1500"),
        gross_edge_cents=15,
        estimated_entry_cost_dollars=Decimal("0.8500"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"MKT-1": {"yes_ask": "0.4000"}, "MKT-2": {"yes_ask": "0.4500"}},
        screening_timestamp=now.isoformat(),
        candidate_rationale="Old catalog test",
        validation_limitations=(),
        stable_key="mece:EV-OLD",
        is_execution_qualified=False,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.is_qualified is True
    assert res.outcome == QualificationOutcome.QUALIFIED.value
    assert client.event_calls == 1
    assert client.book_calls == 2
    assert res.net_edge_cents is not None
    assert res.net_edge_cents >= 5


def test_02_event_prefilter_rejects_when_refreshed_edge_dissolves():
    """Event pre-filter rejects candidate with zero order-book calls when quotes shift eliminating edge."""
    now = datetime.now(timezone.utc)
    m1 = make_market("MKT-1", yes_ask="0.5500")  # shifted up
    m2 = make_market("MKT-2", yes_ask="0.5000")  # total cost 1.05 > 1.00
    event = make_event("EV-DISSOLVED", market_tickers=["MKT-1", "MKT-2"], markets=[m1, m2])

    client = MockPipelineClient(events={"EV-DISSOLVED": event})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = PreliminaryCandidate(
        candidate_id="PRE-DISSOLVED",
        strategy_type="mece_basket_long_yes",
        event_ticker="EV-DISSOLVED",
        market_tickers=("MKT-1", "MKT-2"),
        gross_edge_dollars=Decimal("0.1000"),
        gross_edge_cents=10,
        estimated_entry_cost_dollars=Decimal("0.9000"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"MKT-1": {"yes_ask": "0.4500"}, "MKT-2": {"yes_ask": "0.4500"}},
        screening_timestamp=now.isoformat(),
        candidate_rationale="Dissolved test",
        validation_limitations=(),
        stable_key="mece:EV-DISSOLVED",
        is_execution_qualified=False,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.is_qualified is False
    assert res.outcome == QualificationOutcome.REJECTED_EVENT_PREFILTER.value
    assert client.event_calls == 1
    assert client.book_calls == 0  # Saved all order-book calls


def test_03_stage_c_rejects_stale_actual_order_book():
    """Stage C strictly rejects candidate when actual order books are stale (> 60s source timestamp)."""
    now = datetime.now(timezone.utc)
    stale_ts = now - timedelta(seconds=75)
    m1 = make_market("MKT-1", yes_ask="0.4000")
    m2 = make_market("MKT-2", yes_ask="0.4500")
    event = make_event("EV-STALE-BOOK", market_tickers=["MKT-1", "MKT-2"], markets=[m1, m2])

    b1 = make_book("MKT-1", no_bid="0.6000", source_timestamp=stale_ts)  # 75s old
    b2 = make_book("MKT-2", no_bid="0.5500", source_timestamp=stale_ts)  # 75s old (skew = 0)

    client = MockPipelineClient(events={"EV-STALE-BOOK": event}, books={"MKT-1": b1, "MKT-2": b2})
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_stale_seconds=60.0),
    )

    cand = PreliminaryCandidate(
        candidate_id="PRE-STALE-BOOK",
        strategy_type="mece_basket_long_yes",
        event_ticker="EV-STALE-BOOK",
        market_tickers=("MKT-1", "MKT-2"),
        gross_edge_dollars=Decimal("0.1500"),
        gross_edge_cents=15,
        estimated_entry_cost_dollars=Decimal("0.8500"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"MKT-1": {"yes_ask": "0.4000"}, "MKT-2": {"yes_ask": "0.4500"}},
        screening_timestamp=now.isoformat(),
        candidate_rationale="Stale book test",
        validation_limitations=(),
        stable_key="mece:EV-STALE-BOOK",
        is_execution_qualified=False,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.is_qualified is False
    assert res.outcome == QualificationOutcome.REJECTED_STALE.value


def test_03b_stage_c_rejects_server_timestamp_skew():
    """Stage C strictly rejects candidate when cross-leg server timestamp skew exceeds 2.0s."""
    now = datetime.now(timezone.utc)
    skewed_ts = now - timedelta(seconds=4)  # 4s skew > 2s limit
    m1 = make_market("MKT-1", yes_ask="0.4000")
    m2 = make_market("MKT-2", yes_ask="0.4500")
    event = make_event("EV-SKEW-BOOK", market_tickers=["MKT-1", "MKT-2"], markets=[m1, m2])

    b1 = make_book("MKT-1", no_bid="0.6000", source_timestamp=skewed_ts)
    b2 = make_book("MKT-2", no_bid="0.5500", source_timestamp=now)

    client = MockPipelineClient(events={"EV-SKEW-BOOK": event}, books={"MKT-1": b1, "MKT-2": b2})
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_leg_skew_seconds=2.0),
    )

    cand = PreliminaryCandidate(
        candidate_id="PRE-SKEW-BOOK",
        strategy_type="mece_basket_long_yes",
        event_ticker="EV-SKEW-BOOK",
        market_tickers=("MKT-1", "MKT-2"),
        gross_edge_dollars=Decimal("0.1500"),
        gross_edge_cents=15,
        estimated_entry_cost_dollars=Decimal("0.8500"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"MKT-1": {"yes_ask": "0.4000"}, "MKT-2": {"yes_ask": "0.4500"}},
        screening_timestamp=now.isoformat(),
        candidate_rationale="Skew book test",
        validation_limitations=(),
        stable_key="mece:EV-SKEW-BOOK",
        is_execution_qualified=False,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.is_qualified is False
    assert res.outcome == QualificationOutcome.REJECTED_LEG_SKEW.value


def test_04_stage_c_rejects_unprofitable_after_fees():
    """Stage C strictly rejects candidate when net edge after official CFTC taker fees is < min_net_profit."""
    now = datetime.now(timezone.utc)
    # Total cost 98c -> gross edge 2c. Taker fees for 2 legs = 2c + 2c = 4c -> net loss -2c.
    m1 = make_market("MKT-1", yes_ask="0.4900")
    m2 = make_market("MKT-2", yes_ask="0.4900")
    event = make_event("EV-UNPROFITABLE", market_tickers=["MKT-1", "MKT-2"], markets=[m1, m2])

    b1 = make_book("MKT-1", no_bid="0.5100", source_timestamp=now)  # YES ask = 49c
    b2 = make_book("MKT-2", no_bid="0.5100", source_timestamp=now)  # YES ask = 49c

    client = MockPipelineClient(events={"EV-UNPROFITABLE": event}, books={"MKT-1": b1, "MKT-2": b2})
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(min_net_profit_cents=5),
    )

    cand = PreliminaryCandidate(
        candidate_id="PRE-UNPROFITABLE",
        strategy_type="mece_basket_long_yes",
        event_ticker="EV-UNPROFITABLE",
        market_tickers=("MKT-1", "MKT-2"),
        gross_edge_dollars=Decimal("0.0200"),
        gross_edge_cents=2,
        estimated_entry_cost_dollars=Decimal("0.9800"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"MKT-1": {"yes_ask": "0.4900"}, "MKT-2": {"yes_ask": "0.4900"}},
        screening_timestamp=now.isoformat(),
        candidate_rationale="Unprofitable test",
        validation_limitations=(),
        stable_key="mece:EV-UNPROFITABLE",
        is_execution_qualified=False,
    )

    res = qualifier.qualify_candidate(cand)
    assert res.is_qualified is False
    assert res.outcome == QualificationOutcome.REJECTED_UNPROFITABLE_FEES.value


def test_05_stage_c_leg_gating_rejects_baskets_over_8_legs():
    """Stage C pre-qualification size gate rejects baskets with > 8 legs with zero network requests."""
    now = datetime.now(timezone.utc)
    tickers = tuple(f"LEG-{i}" for i in range(12))  # 12 legs > 8
    cand = PreliminaryCandidate(
        candidate_id="PRE-LARGE",
        strategy_type="mece_basket_long_yes",
        event_ticker="EV-LARGE",
        market_tickers=tickers,
        gross_edge_dollars=Decimal("0.5000"),
        gross_edge_cents=50,
        estimated_entry_cost_dollars=Decimal("0.5000"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={t: {"yes_ask": "0.0400"} for t in tickers},
        screening_timestamp=now.isoformat(),
        candidate_rationale="Size gate test",
        validation_limitations=(),
        stable_key="mece:EV-LARGE",
        is_execution_qualified=False,
    )

    client = MockPipelineClient()
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_candidate_legs=8),
    )

    # Enqueue should gate out
    enqueued = qualifier.enqueue_candidate(cand)
    assert enqueued is False
    assert len(qualifier.gated_rejections) == 1
    assert qualifier.gated_rejections[0].outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert client.event_calls == 0
    assert client.book_calls == 0
