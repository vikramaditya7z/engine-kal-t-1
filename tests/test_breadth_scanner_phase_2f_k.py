"""Phase 2F-K Test Suite: Screening Pipeline Correction.

Verifies:
1. Removal of catalog updated_time-based staleness rejection from Stage B.
2. Removal of catalog cross-leg timestamp skew rejection from Stage B.
3. Calculation of mathematical gross edge using available catalog top-of-book prices.
4. Preservation of market eligibility, status, structural validity, and event grouping checks.
5. Recording discovery observation timestamps separately from actual exchange quote-update timestamps.
6. Preservation of Stage C safeguards (actual quote age, dual skew, leg limits, depth, fees).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List, Dict, Any

from kalshi_arbitrage.breadth_scanner import (
    BreadthArbitrageScreener,
    BreadthScannerConfig,
    NormalizedMarket,
    NormalizedEvent,
    PreliminaryCandidate,
    ScreeningOutcome,
    ScreeningStrategyType,
    TargetedOrderBookQualifier,
    TargetedOrderBookQualifierConfig,
)
from kalshi_arbitrage.market_data import (
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.execution_qualifier import QualificationOutcome


def make_market(
    ticker: str,
    event_ticker: str = "EV-1",
    status: str = "open",
    market_type: str = "binary",
    yes_ask: Optional[str] = None,
    no_ask: Optional[str] = None,
    yes_bid: Optional[str] = None,
    no_bid: Optional[str] = None,
    updated_time: Optional[datetime] = None,
    subtitle: Optional[str] = None,
) -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type=market_type,
        status=status,
        title=f"Market {ticker}",
        subtitle=subtitle,
        yes_ask_dollars=Decimal(yes_ask) if yes_ask is not None else None,
        no_ask_dollars=Decimal(no_ask) if no_ask is not None else None,
        yes_bid_dollars=Decimal(yes_bid) if yes_bid is not None else None,
        no_bid_dollars=Decimal(no_bid) if no_bid is not None else None,
        updated_time=updated_time,
    )


def make_event(
    event_ticker: str,
    markets: List[NormalizedMarket],
    mutually_exclusive: bool = True,
    series_ticker: str = "KXSERIES",
) -> NormalizedEvent:
    return NormalizedEvent(
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=f"Event {event_ticker}",
        subtitle=None,
        mutually_exclusive=mutually_exclusive,
        market_tickers=tuple(m.ticker for m in markets),
        settlement_sources=(),
        category="Economics",
        markets=tuple(markets),
    )


class MockQualifierClient:
    def __init__(self, order_books: Optional[Dict[str, NormalizedOrderBook]] = None):
        self.order_books = order_books or {}
        self.requested_books: List[str] = []

    def get_order_book(self, ticker: str) -> NormalizedOrderBook:
        self.requested_books.append(ticker)
        if ticker in self.order_books:
            return self.order_books[ticker]
        return NormalizedOrderBook(
            market_ticker=ticker,
            yes_bids=(OrderBookLevel(price_dollars=Decimal("0.5000"), quantity=Decimal("10.0")),),
            no_bids=(OrderBookLevel(price_dollars=Decimal("0.5000"), quantity=Decimal("10.0")),),
            source_timestamp=datetime.now(timezone.utc),
        )


def test_k1_catalog_updated_time_staleness_not_rejected_in_stage_b():
    """Binary parity with old catalog updated_time is not rejected as stale in Stage B screening."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=2)  # 2 hours old

    # Mathematical gross edge: $0.40 + $0.55 = $0.95 < $1.00 -> 5c gross edge
    m = make_market("M-OLD-BP", yes_ask="0.4000", no_ask="0.5500", updated_time=old_time)

    screener = BreadthArbitrageScreener(config=BreadthScannerConfig(max_stale_seconds=60.0))
    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert res.candidate is not None
    assert res.candidate.candidate_id == "PRE-BP:M-OLD-BP"
    assert res.candidate.gross_edge_dollars == Decimal("0.0500")
    # Observation timestamp records screening observation time, while quote_timestamp records exchange time
    assert res.candidate.observation_timestamp is not None
    assert res.candidate.quote_timestamp == old_time.isoformat()


def test_k2_mece_basket_catalog_updated_time_staleness_not_rejected_in_stage_b():
    """MECE event basket with old constituent updated_time is not rejected in Stage B."""
    now = datetime.now(timezone.utc)
    old_time = now - timedelta(days=1)  # 24 hours old

    # 3-leg MECE basket with residual OTHER leg
    m1 = make_market("EV-LEG1", yes_ask="0.3000", updated_time=old_time)
    m2 = make_market("EV-LEG2", yes_ask="0.3000", updated_time=old_time)
    m3 = make_market("EV-OTHER", yes_ask="0.3000", updated_time=old_time)
    ev = make_event("EV-MECE-OLD", [m1, m2, m3])

    screener = BreadthArbitrageScreener(config=BreadthScannerConfig(max_stale_seconds=60.0))
    results = screener.screen_event_mece_baskets(ev, [m1, m2, m3])

    yes_res = next(r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_YES.value)
    assert yes_res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert yes_res.candidate is not None
    assert yes_res.candidate.gross_edge_dollars == Decimal("0.1000")
    assert yes_res.candidate.observation_timestamp is not None
    assert yes_res.candidate.quote_timestamp == old_time.isoformat()


def test_k3_mece_basket_cross_leg_skew_not_rejected_in_stage_b():
    """MECE basket with disparate catalog timestamps is not rejected on skew during Stage B screening."""
    now = datetime.now(timezone.utc)
    ts_leg1 = now - timedelta(seconds=10)
    ts_leg2 = now - timedelta(seconds=500)  # 490s skew in catalog metadata

    m1 = make_market("EV-SKEW-1", yes_ask="0.4000", updated_time=ts_leg1)
    m2 = make_market("EV-SKEW-2", yes_ask="0.4500", updated_time=ts_leg2)
    m3 = make_market("EV-OTHER", yes_ask="0.0500", updated_time=ts_leg1)
    ev = make_event("EV-SKEW", [m1, m2, m3])

    screener = BreadthArbitrageScreener(config=BreadthScannerConfig(max_stale_seconds=60.0, max_leg_skew_seconds=5.0))
    results = screener.screen_event_mece_baskets(ev, [m1, m2, m3])

    yes_res = next(r for r in results if r.strategy_type == ScreeningStrategyType.MECE_BASKET_LONG_YES.value)
    assert yes_res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    assert yes_res.candidate is not None
    # Oldest timestamp is preserved for conservative tracking
    assert yes_res.candidate.quote_timestamp == ts_leg2.isoformat()


def test_k4_market_eligibility_and_structure_validation_preserved():
    """Closed markets, non-binary types, and non-MECE structures remain strictly rejected."""
    screener = BreadthArbitrageScreener(config=BreadthScannerConfig())

    # 1. Closed market
    m_closed = make_market("M-CLOSED", status="closed", yes_ask="0.4000", no_ask="0.4000")
    res_closed = screener.screen_market_binary_parity(m_closed)
    assert res_closed.outcome == ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET
    assert "status is 'closed'" in res_closed.reason

    # 2. Non-binary market validation (rejected at model normalization)
    import pytest
    from kalshi_arbitrage.market_data import MarketDataInputError
    with pytest.raises(MarketDataInputError, match="only binary markets are supported"):
        make_market("M-SCALAR", market_type="scalar", yes_ask="0.4000", no_ask="0.4000")

    # 3. Non-mutually exclusive event
    m1 = make_market("EV-LEG1", yes_ask="0.4000")
    m2 = make_market("EV-OTHER", yes_ask="0.4000")
    ev_not_me = make_event("EV-NOT-ME", [m1, m2], mutually_exclusive=False)
    results_not_me = screener.screen_event_mece_baskets(ev_not_me, [m1, m2])
    for r in results_not_me:
        assert r.outcome == ScreeningOutcome.SKIPPED_UNSUPPORTED_MARKET

    # 4. Incomplete event group
    ev_incomplete = NormalizedEvent(
        event_ticker="EV-INCOMP",
        series_ticker="KXSERIES",
        title="Incomplete Event",
        subtitle=None,
        mutually_exclusive=True,
        market_tickers=("M1", "M2", "M3"),
        settlement_sources=(),
        markets=(m1, m2),
    )
    results_incomp = screener.screen_event_mece_baskets(ev_incomplete, [m1, m2])
    for r in results_incomp:
        assert r.outcome == ScreeningOutcome.SKIPPED_INCOMPLETE_EVENT_GROUP


def test_k5_observation_timestamp_recorded_separately_from_quote_timestamp():
    """PreliminaryCandidate records observation_timestamp separately from quote_timestamp."""
    now = datetime.now(timezone.utc)
    exchange_ts = now - timedelta(days=3)

    m = make_market("M-OBS", yes_ask="0.4000", no_ask="0.5000", updated_time=exchange_ts)
    screener = BreadthArbitrageScreener(config=BreadthScannerConfig())
    res = screener.screen_market_binary_parity(m)

    assert res.outcome == ScreeningOutcome.PRELIMINARY_CANDIDATE
    cand = res.candidate
    assert cand is not None
    assert cand.observation_timestamp is not None
    assert cand.quote_timestamp == exchange_ts.isoformat()

    # Serialization preserves both fields
    d = cand.to_dict()
    assert "observation_timestamp" in d
    assert "quote_timestamp" in d
    assert d["quote_timestamp"] == exchange_ts.isoformat()
    assert d["observation_timestamp"] == cand.observation_timestamp


def test_k6_stage_c_safeguards_preserved_against_stale_actual_quotes():
    """Stage C execution qualification preserves strict staleness checking on actual candidate quotes."""
    now = datetime.now(timezone.utc)
    eval_time = now

    # Candidate with stale observation time (> 60s limit)
    stale_obs_time = (now - timedelta(seconds=120)).isoformat()
    cand_stale = PreliminaryCandidate(
        candidate_id="PRE-TEST-STALE",
        strategy_type="binary_parity",
        event_ticker="EV-1",
        market_tickers=("M-1",),
        gross_edge_dollars=Decimal("0.1000"),
        gross_edge_cents=10,
        estimated_entry_cost_dollars=Decimal("0.9000"),
        guaranteed_payout_dollars=Decimal("1.0000"),
        constituent_quotes={"M-1": {"yes_ask": "0.4000", "no_ask": "0.5000"}},
        screening_timestamp=stale_obs_time,
        observation_timestamp=stale_obs_time,
        candidate_rationale="Test",
        validation_limitations=(),
        stable_key="BP:M-1",
    )

    client = MockQualifierClient()
    qualifier = TargetedOrderBookQualifier(
        client=client,
        config=TargetedOrderBookQualifierConfig(max_stale_seconds=60.0, enable_event_prefilter=False),
        time_provider=lambda: eval_time,
    )

    res = qualifier.qualify_candidate(cand_stale)
    assert res.outcome == QualificationOutcome.REJECTED_STALE.value
    assert res.is_qualified is False
    assert "exceeds" in res.rejection_reason or "stale" in res.rejection_reason.lower()
    # Zero order-book requests made because pre-flight staleness check caught it
    assert res.requests_made == 0
    assert len(client.requested_books) == 0
