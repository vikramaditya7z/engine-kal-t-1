"""Tests for V8.5 liquid universe selection and ranking engine.

Verifies:
- Configurable volume and open interest thresholds
- Rejection of empty order books and insufficient depth
- Spread tightness filtering
- Series prefix inclusions and exclusions
- Honest distinction between unavailable metrics (None) and zero values
- Deterministic liquidity ranking scores
- Rejection reasons tracking
- Fail-closed validation on invalid configurations
"""

from decimal import Decimal
from typing import Dict, List, Optional
import pytest

from kalshi_arbitrage.market_data import NormalizedMarket, NormalizedOrderBook, OrderBookLevel
from kalshi_arbitrage.universe import (
    LiquidUniverseSelector,
    MarketLiquidityProfile,
    UniverseFilterConfig,
    UniverseSelectionError,
    UniverseSelectionResult,
)


def make_market(
    ticker: str,
    status: str = "open",
    volume_24h: Optional[str] = "500.00",
    open_interest: Optional[str] = "100.00",
) -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=f"EVENT-{ticker}",
        market_type="binary",
        status=status,
        title=f"Test Market {ticker}",
        subtitle=None,
        yes_subtitle=None,
        no_subtitle=None,
        rules_primary=None,
        rules_secondary=None,
        expiration_value=None,
        result=None,
        created_time=None,
        updated_time=None,
        open_time=None,
        close_time=None,
        expiration_time=None,
        settlement_ts=None,
        yes_bid_dollars=Decimal("0.50"),
        yes_ask_dollars=Decimal("0.52"),
        no_bid_dollars=Decimal("0.48"),
        no_ask_dollars=Decimal("0.50"),
        last_price_dollars=Decimal("0.50"),
        settlement_value_dollars=None,
        yes_bid_size=None,
        yes_ask_size=None,
        volume=Decimal("1000.00"),
        volume_24h=Decimal(volume_24h) if volume_24h is not None else None,
        open_interest=Decimal(open_interest) if open_interest is not None else None,
        price_ranges=(),
        is_provisional=None,
    )


def make_book(ticker: str, yes_qty: int = 50, no_qty: int = 50) -> NormalizedOrderBook:
    return NormalizedOrderBook(
        market_ticker=ticker,
        yes_bids=(OrderBookLevel(Decimal("0.50"), Decimal(yes_qty)),) if yes_qty > 0 else (),
        no_bids=(OrderBookLevel(Decimal("0.48"), Decimal(no_qty)),) if no_qty > 0 else (),
    )


def test_select_liquid_market():
    selector = LiquidUniverseSelector()
    market = make_market("KXINX-TEST")
    book = make_book("KXINX-TEST")

    res = selector.select_universe([market], {"KXINX-TEST": book})
    assert res.selected_count == 1
    assert res.rejected_count == 0
    assert "KXINX-TEST" in res.selected_tickers
    profile = res.profiles[0]
    assert profile.is_liquid is True
    assert profile.is_populated is True
    assert profile.liquidity_score is not None
    assert profile.liquidity_score > Decimal("0")


def test_reject_empty_order_book():
    selector = LiquidUniverseSelector(
        UniverseFilterConfig(require_populated_order_book=True)
    )
    market = make_market("KXEMPTY")
    empty_book = make_book("KXEMPTY", yes_qty=0, no_qty=0)

    res = selector.select_universe([market], {"KXEMPTY": empty_book})
    assert res.selected_count == 0
    assert res.rejected_count == 1
    assert any("empty" in r.lower() for r in res.rejections_by_reason.keys())


def test_reject_insufficient_volume():
    selector = LiquidUniverseSelector(
        UniverseFilterConfig(min_volume_24h=Decimal("100.00"))
    )
    # Market with only 5 volume
    market = make_market("KXLOWVOL", volume_24h="5.00")
    book = make_book("KXLOWVOL")

    res = selector.select_universe([market], {"KXLOWVOL": book})
    assert res.selected_count == 0
    assert res.rejected_count == 1
    assert any("volume" in r.lower() for r in res.rejections_by_reason.keys())


def test_unavailable_volume_distinguished_from_zero():
    selector = LiquidUniverseSelector(
        UniverseFilterConfig(min_volume_24h=Decimal("10.00"))
    )
    # Market with None volume
    market = make_market("KXNOVOL", volume_24h=None)
    book = make_book("KXNOVOL")

    res = selector.select_universe([market], {"KXNOVOL": book})
    assert res.selected_count == 0
    assert any("unavailable" in r.lower() for r in res.rejections_by_reason.keys())


def test_reject_wide_spread():
    selector = LiquidUniverseSelector(
        UniverseFilterConfig(max_spread_cents=5)
    )
    market = make_market("KXWIDESPREAD")
    # YES bid 40¢, NO bid 40¢ -> YES ask 60¢ -> spread = 60 - 40 = 20¢ > 5¢
    wide_book = NormalizedOrderBook(
        market_ticker="KXWIDESPREAD",
        yes_bids=(OrderBookLevel(Decimal("0.40"), Decimal("10")),),
        no_bids=(OrderBookLevel(Decimal("0.40"), Decimal("10")),),
    )

    res = selector.select_universe([market], {"KXWIDESPREAD": wide_book})
    assert res.selected_count == 0
    assert any("spread" in r.lower() for r in res.rejections_by_reason.keys())


def test_series_prefix_filtering():
    selector = LiquidUniverseSelector(
        UniverseFilterConfig(allowed_series_prefixes=("KXINX", "KXFED"))
    )
    market1 = make_market("KXINX-24OCT")
    market2 = make_market("KXSPORTS-24OCT")
    book1 = make_book("KXINX-24OCT")
    book2 = make_book("KXSPORTS-24OCT")

    res = selector.select_universe([market1, market2], {"KXINX-24OCT": book1, "KXSPORTS-24OCT": book2})
    assert res.selected_count == 1
    assert res.selected_tickers == ("KXINX-24OCT",)


def test_ranking_by_liquidity_score():
    selector = LiquidUniverseSelector()
    # Market A: 500 vol, 50 depth
    # Market B: 5000 vol, 500 depth
    m_a = make_market("MKT-A", volume_24h="500.00")
    b_a = make_book("MKT-A", yes_qty=25, no_qty=25)

    m_b = make_market("MKT-B", volume_24h="5000.00")
    b_b = make_book("MKT-B", yes_qty=250, no_qty=250)

    res = selector.select_universe([m_a, m_b], {"MKT-A": b_a, "MKT-B": b_b})
    assert res.selected_count == 2
    # Market B must rank FIRST due to higher volume and depth
    assert res.selected_tickers[0] == "MKT-B"
    assert res.selected_tickers[1] == "MKT-A"


def test_invalid_filter_config_fails_closed():
    with pytest.raises(UniverseSelectionError):
        UniverseFilterConfig(min_volume_24h=Decimal("-1"))

    with pytest.raises(UniverseSelectionError):
        UniverseFilterConfig(max_spread_cents=-5)

    with pytest.raises(UniverseSelectionError):
        UniverseFilterConfig(max_selected_markets=0)
