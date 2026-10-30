"""Phase 2F-G2 deterministic offline tests: Event-Level Top-of-Book Pre-Filter.

Covers:
- Opportunity survives and reaches L2 qualification.
- Opportunity disappears and generates zero L2 requests.
- Refreshed edge falls below existing threshold (edge <= 0).
- Missing event data (e.g. 404).
- Missing constituent markets in event response.
- Malformed response (invalid quote data).
- Stale event response.
- Timeout / transport errors.
- Multiple candidates sharing an event (deduplication within cache TTL).
- Correct request accounting.
- G1 leg-count gating occurs before event retrieval.
- Configuration toggle (--no-event-prefilter / enable_event_prefilter=False).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
import pytest

from kalshi_arbitrage import (
    FeeScheduleType,
    KalshiHTTPError,
    KalshiResponseError,
    KalshiTransportError,
    MarketDataInputError,
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PreliminaryCandidate,
    QualificationOutcome,
    ScreeningOutcome,
    ScreeningStrategyType,
    SettlementSource,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
    TargetedQualificationResult,
)


def make_test_market(
    ticker: str,
    event_ticker: str = "EV-TEST",
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
        title=f"Test Market {ticker}",
        yes_ask_dollars=Decimal(yes_ask) if yes_ask is not None else None,
        no_ask_dollars=Decimal(no_ask) if no_ask is not None else None,
        yes_bid_dollars=Decimal("0.4000"),
        no_bid_dollars=Decimal("0.4500"),
        yes_ask_size=Decimal("100"),
        yes_bid_size=Decimal("100"),
        updated_time=now,
    )


def make_test_order_book(
    ticker: str,
    yes_bid: str = "0.4500",
    no_bid: str = "0.5000",
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


def make_test_event(
    event_ticker: str = "EV-TEST",
    market_tickers: Sequence[str] = ("LEG-1", "LEG-2"),
    markets: Optional[Sequence[NormalizedMarket]] = None,
) -> NormalizedEvent:
    if markets is None:
        markets = [make_test_market(t, event_ticker=event_ticker) for t in market_tickers]
    return NormalizedEvent(
        event_ticker=event_ticker,
        series_ticker="SERIES-1",
        title=f"Event {event_ticker}",
        subtitle=None,
        mutually_exclusive=True,
        market_tickers=tuple(market_tickers),
        settlement_sources=(SettlementSource(name="Test", url="https://example.com"),),
        markets=tuple(markets),
    )


def make_test_candidate(
    candidate_id: str = "CAND-1",
    strategy_type: str = ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
    event_ticker: str = "EV-TEST",
    market_tickers: Sequence[str] = ("LEG-1", "LEG-2"),
    gross_edge_dollars: str = "0.1000",
    quote_timestamp: Optional[str] = None,
) -> PreliminaryCandidate:
    now_iso = quote_timestamp or datetime.now(timezone.utc).isoformat()
    return PreliminaryCandidate(
        candidate_id=candidate_id,
        strategy_type=strategy_type,
        event_ticker=event_ticker,
        market_tickers=tuple(market_tickers),
        gross_edge_dollars=Decimal(gross_edge_dollars),
        gross_edge_cents=int(Decimal(gross_edge_dollars) * 100),
        estimated_entry_cost_dollars=Decimal("0.9000"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={t: {"yes_ask": "0.4500", "no_ask": "0.5000"} for t in market_tickers},
        screening_timestamp=now_iso,
        candidate_rationale="Test candidate",
        validation_limitations=(),
        stable_key=f"{strategy_type}:{event_ticker}",
        is_execution_qualified=False,
        quote_timestamp=now_iso,
    )


class MockEventClient:
    """Mock client supporting get_event and get_order_book."""

    def __init__(
        self,
        events: Optional[Mapping[str, NormalizedEvent]] = None,
        books: Optional[Mapping[str, NormalizedOrderBook]] = None,
        event_error: Optional[Exception] = None,
        book_error: Optional[Exception] = None,
    ) -> None:
        self.events: Dict[str, NormalizedEvent] = dict(events or {})
        self.books: Dict[str, NormalizedOrderBook] = dict(books or {})
        self.event_error = event_error
        self.book_error = book_error
        self.requested_events: List[str] = []
        self.requested_books: List[str] = []
        self.event_call_count: int = 0
        self.book_call_count: int = 0

    def get_event(self, event_ticker: str, *, with_nested_markets: bool = False) -> NormalizedEvent:
        self.event_call_count += 1
        self.requested_events.append(event_ticker)
        if self.event_error:
            raise self.event_error
        if event_ticker in self.events:
            return self.events[event_ticker]
        return make_test_event(event_ticker)

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        self.book_call_count += 1
        self.requested_books.append(ticker)
        if self.book_error:
            raise self.book_error
        if ticker in self.books:
            return self.books[ticker]
        return make_test_order_book(ticker)


# =============================================================================
# Milestone G2 Tests
# =============================================================================

def test_g2_event_prefilter_opportunity_survives_and_reaches_l2():
    """Opportunity survives event-level check (positive refreshed edge) and completes L2 qualification."""
    # 2 legs: yes_ask 0.40 and 0.50 -> cost 0.90 < 1.00 (edge = 0.10)
    m1 = make_test_market("LEG-1", yes_ask="0.4000")
    m2 = make_test_market("LEG-2", yes_ask="0.5000")
    event = make_test_event("EV-SURVIVE", market_tickers=["LEG-1", "LEG-2"], markets=[m1, m2])
    
    # Book with deep liquidity: to satisfy target_quantity 1
    # YES ask = 0.40 means opposing NO bid = 0.60
    # YES ask = 0.50 means opposing NO bid = 0.50
    b1 = make_test_order_book("LEG-1", yes_bid="0.4000", no_bid="0.6000", quantity=100)
    b2 = make_test_order_book("LEG-2", yes_bid="0.5000", no_bid="0.5000", quantity=100)

    client = MockEventClient(events={"EV-SURVIVE": event}, books={"LEG-1": b1, "LEG-2": b2})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_candidate(
        candidate_id="CAND-SURVIVE",
        event_ticker="EV-SURVIVE",
        market_tickers=["LEG-1", "LEG-2"],
        gross_edge_dollars="0.1000",
    )

    result = qualifier.qualify_candidate(cand)
    assert result.is_qualified is True
    assert result.outcome == QualificationOutcome.QUALIFIED.value
    # 1 event request + 2 order-book requests = 3 requests made
    assert result.requests_made == 3
    assert client.event_call_count == 1
    assert client.book_call_count == 2
    assert result.refreshed_gross_edge_dollars == Decimal("0.1000")


def test_g2_event_prefilter_opportunity_disappears_zero_l2_requests():
    """When event top-of-book quotes shift eliminating edge, candidate is rejected with 0 order-book requests."""
    # YES asks shifted up: 0.52 + 0.50 = 1.02 >= 1.00 (negative edge)
    m1 = make_test_market("LEG-1", yes_ask="0.5200")
    m2 = make_test_market("LEG-2", yes_ask="0.5000")
    event = make_test_event("EV-DISSOLVED", market_tickers=["LEG-1", "LEG-2"], markets=[m1, m2])

    client = MockEventClient(events={"EV-DISSOLVED": event})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_candidate(
        candidate_id="CAND-DISSOLVED",
        event_ticker="EV-DISSOLVED",
        market_tickers=["LEG-1", "LEG-2"],
        gross_edge_dollars="0.0800",
    )

    result = qualifier.qualify_candidate(cand)
    assert result.is_qualified is False
    assert result.outcome == QualificationOutcome.REJECTED_EVENT_PREFILTER.value
    assert "Event pre-filter rejected: refreshed gross edge $-0.0200 <= $0.0000" in result.rejection_reason
    assert "EV-DISSOLVED" in result.rejection_reason
    assert result.refreshed_gross_edge_dollars == Decimal("-0.0200")
    # Spent exactly 1 event request, saved 2 order-book requests
    assert result.requests_made == 1
    assert client.event_call_count == 1
    assert client.book_call_count == 0


def test_g2_event_prefilter_refreshed_edge_exactly_zero():
    """When refreshed edge is exactly $0.0000 (breakeven), candidate is rejected by event pre-filter."""
    # YES asks = 0.5000 + 0.5000 = 1.0000 -> edge = 0.0000
    m1 = make_test_market("LEG-1", yes_ask="0.5000")
    m2 = make_test_market("LEG-2", yes_ask="0.5000")
    event = make_test_event("EV-ZERO", market_tickers=["LEG-1", "LEG-2"], markets=[m1, m2])

    client = MockEventClient(events={"EV-ZERO": event})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_candidate(event_ticker="EV-ZERO", market_tickers=["LEG-1", "LEG-2"])
    result = qualifier.qualify_candidate(cand)

    assert result.outcome == QualificationOutcome.REJECTED_EVENT_PREFILTER.value
    assert result.refreshed_gross_edge_dollars == Decimal("0.0000")
    assert result.requests_made == 1
    assert client.book_call_count == 0


def test_g2_event_prefilter_missing_event_404():
    """When event returns 404, candidate is rejected conservatively without proceeding to order books."""
    client = MockEventClient(
        event_error=KalshiHTTPError(404, "https://api.kalshi.com/events/EV-404", "Not found")
    )
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_candidate(event_ticker="EV-404")
    result = qualifier.qualify_candidate(cand)

    assert result.is_qualified is False
    assert result.outcome in (QualificationOutcome.REJECTED_MISSING_MARKET.value, "ERROR")
    assert "404" in result.rejection_reason
    assert result.requests_made >= 1
    assert client.book_call_count == 0


def test_g2_event_prefilter_missing_constituent_markets():
    """When event response is missing one of the constituent market tickers, candidate is rejected conservatively."""
    # Event only has LEG-1, missing LEG-2
    m1 = make_test_market("LEG-1")
    event = make_test_event("EV-MISSING-LEG", market_tickers=["LEG-1"], markets=[m1])

    client = MockEventClient(events={"EV-MISSING-LEG": event})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_candidate(event_ticker="EV-MISSING-LEG", market_tickers=["LEG-1", "LEG-2"])
    result = qualifier.qualify_candidate(cand)

    assert result.is_qualified is False
    assert result.outcome == QualificationOutcome.REJECTED_MISSING_MARKET.value
    assert "constituent market(s) missing from event: LEG-2" in result.rejection_reason
    assert result.requests_made == 1
    assert client.book_call_count == 0


def test_g2_event_prefilter_malformed_response_negative_quote():
    """When event contains out-of-range or malformed quote data, candidate is rejected with explicit ERROR."""
    # Market has invalid yes_ask = -0.1000
    m1 = make_test_market("LEG-1")
    m2 = make_test_market("LEG-2")
    # Mutate to invalid Decimal
    object.__setattr__(m2, "yes_ask_dollars", Decimal("-0.1000"))
    event = make_test_event("EV-MALFORMED", market_tickers=["LEG-1", "LEG-2"], markets=[m1, m2])

    client = MockEventClient(events={"EV-MALFORMED": event})
    qualifier = TargetedOrderBookQualifier(client=client)

    cand = make_test_candidate(event_ticker="EV-MALFORMED", market_tickers=["LEG-1", "LEG-2"])
    result = qualifier.qualify_candidate(cand)

    assert result.is_qualified is False
    assert result.outcome == "ERROR"
    assert "invalid yes_ask_dollars" in result.rejection_reason
    assert result.requests_made == 1
    assert client.book_call_count == 0


def test_g2_event_prefilter_stale_event_response():
    """When constituent quotes in event response are stale (> max_stale_seconds), candidate is rejected."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(seconds=120)  # 120s old > 60s limit
    m1 = make_test_market("LEG-1", updated_time=stale_time)
    m2 = make_test_market("LEG-2", updated_time=now)
    event = make_test_event("EV-STALE", market_tickers=["LEG-1", "LEG-2"], markets=[m1, m2])

    client = MockEventClient(events={"EV-STALE": event})
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_stale_seconds=60.0, check_prefilter_quote_staleness=True),
    )

    cand = make_test_candidate(event_ticker="EV-STALE", market_tickers=["LEG-1", "LEG-2"])
    result = qualifier.qualify_candidate(cand)

    assert result.is_qualified is False
    assert result.outcome == QualificationOutcome.REJECTED_STALE.value
    assert "constituent market LEG-1 quote is stale" in result.rejection_reason
    assert result.requests_made == 1
    assert client.book_call_count == 0


def test_g2_event_prefilter_decoupled_metadata_staleness_reaches_l2():
    """Default check_prefilter_quote_staleness=False permits candidate with old catalog updated_time to proceed to L2 order books."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(days=30)  # 30 days old catalog updated_time
    m1 = make_test_market("LEG-1", yes_ask="0.4500", updated_time=stale_time)
    m2 = make_test_market("LEG-2", yes_ask="0.4500", updated_time=stale_time)
    event = make_test_event("EV-OLD-CAT", market_tickers=["LEG-1", "LEG-2"], markets=[m1, m2])

    b1 = make_test_order_book("LEG-1", yes_bid="0.4500", no_bid="0.5500", source_timestamp=now)
    b2 = make_test_order_book("LEG-2", yes_bid="0.4500", no_bid="0.5500", source_timestamp=now)

    client = MockEventClient(events={"EV-OLD-CAT": event}, books={"LEG-1": b1, "LEG-2": b2})
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_stale_seconds=60.0, check_prefilter_quote_staleness=False),
    )

    cand = make_test_candidate(
        candidate_id="CAND-OLD-CAT",
        event_ticker="EV-OLD-CAT",
        market_tickers=["LEG-1", "LEG-2"],
        gross_edge_dollars="0.1000",
    )
    result = qualifier.qualify_candidate(cand)

    # Reaches L2 and qualifies because L2 order books are fresh
    assert result.is_qualified is True
    assert result.outcome == QualificationOutcome.QUALIFIED.value
    assert result.requests_made == 3  # 1 event + 2 order books
    assert client.book_call_count == 2


def test_g2_event_prefilter_timeout_handling():
    """When event fetch encounters a transport timeout, explicit error is recorded and zero L2 calls made."""
    client = MockEventClient(event_error=KalshiTransportError(TimeoutError("Socket timeout on /events")))
    qualifier = TargetedOrderBookQualifier(client=client, config=TargetedOrderBookQualifierConfig(max_retries=1))

    cand = make_test_candidate(event_ticker="EV-TIMEOUT")
    result = qualifier.qualify_candidate(cand)

    assert result.is_qualified is False
    assert result.outcome == "ERROR"
    assert "Socket timeout" in result.rejection_reason or "Failed fetching event" in result.rejection_reason
    assert result.requests_failed >= 1
    assert client.book_call_count == 0


def test_g2_event_prefilter_multiple_candidates_sharing_event_deduplication():
    """When multiple candidates share an event ticker, event fetch is cached within TTL."""
    # Event has valid prices for both Long YES and Long NO (costs sum to 0.90 < 1.00 payout)
    m1 = make_test_market("LEG-1", yes_ask="0.4500", no_ask="0.4500")
    m2 = make_test_market("LEG-2", yes_ask="0.4500", no_ask="0.4500")
    event = make_test_event("EV-SHARED", market_tickers=["LEG-1", "LEG-2"], markets=[m1, m2])

    b1 = make_test_order_book("LEG-1", yes_bid="0.4500", no_bid="0.5500", quantity=100)
    b2 = make_test_order_book("LEG-2", yes_bid="0.4500", no_bid="0.5500", quantity=100)

    client = MockEventClient(events={"EV-SHARED": event}, books={"LEG-1": b1, "LEG-2": b2})
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(event_cache_ttl_seconds=5.0),
    )

    cand_yes = make_test_candidate(
        candidate_id="CAND-YES",
        strategy_type=ScreeningStrategyType.MECE_BASKET_LONG_YES.value,
        event_ticker="EV-SHARED",
        market_tickers=["LEG-1", "LEG-2"],
        gross_edge_dollars="0.1000",
    )
    cand_no = make_test_candidate(
        candidate_id="CAND-NO",
        strategy_type=ScreeningStrategyType.MECE_BASKET_LONG_NO.value,
        event_ticker="EV-SHARED",
        market_tickers=["LEG-1", "LEG-2"],
        gross_edge_dollars="0.0500",
    )

    assert qualifier.enqueue_candidate(cand_yes) is True
    assert qualifier.enqueue_candidate(cand_no) is True

    summary = qualifier.process_queue()
    assert summary.candidates_evaluated == 2
    # Crucial assertion: Exactly 1 event call made for EV-SHARED across both candidates!
    assert client.event_call_count == 1
    # First candidate made 1 event call + 2 book calls = 3 requests
    # Second candidate used cached event (0 event calls) + 2 book calls = 2 requests
    # Total requests across both: 3 + 2 = 5 requests
    assert summary.requests_made == 5


def test_g2_g1_gating_occurs_before_event_retrieval():
    """An oversized candidate (9 legs > 8 limit) is rejected before any event requests are dispatched."""
    client = MockEventClient()
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_candidate_legs=8),
    )

    tickers = [f"LEG-{i}" for i in range(9)]
    cand = make_test_candidate(
        candidate_id="CAND-OVERSIZE",
        event_ticker="EV-OVERSIZE",
        market_tickers=tickers,
    )

    # 1. Enqueue gate blocks candidate
    enqueued = qualifier.enqueue_candidate(cand)
    assert enqueued is False
    assert qualifier.queue_size == 0
    # Exactly zero event calls dispatched during enqueue
    assert client.event_call_count == 0

    # 2. Process queue inspects gated result
    summary = qualifier.process_queue()
    assert summary.candidates_evaluated == 1
    res = summary.results[0]
    assert res.outcome == QualificationOutcome.REJECTED_BASKET_TOO_LARGE.value
    assert res.requests_made == 0
    assert client.event_call_count == 0
    assert client.book_call_count == 0


def test_g2_event_prefilter_disabled_flag():
    """When enable_event_prefilter is set to False, event pre-filter is bypassed completely."""
    client = MockEventClient()
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(enable_event_prefilter=False),
    )

    cand = make_test_candidate(event_ticker="EV-BYPASS", market_tickers=["LEG-1", "LEG-2"])
    res = qualifier.qualify_candidate(cand)

    # Event pre-filter was bypassed: 0 event calls, only order-book calls
    assert client.event_call_count == 0
    assert client.book_call_count == 2
    assert res.requests_made == 2
