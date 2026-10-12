"""Liquid-market universe selection, ranking, and liquidity profiling.

V8.5 Liquid Universe Selection:
- Replaces blind market discovery with defensible, configurable liquidity filtering.
- Inspects verified exchange metadata (volume, 24h volume, open interest) and order-book observations (spread, executable depth).
- Ranks markets by depth, activity, and spread tightness.
- Provides explicit inclusion/exclusion reasons for every evaluated market.
- Distinguishes unavailable metrics (None) from measured zero values.
- Prohibits bypassing data freshness, fee, depth, or execution-risk checks.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .arbitrage import dollars_to_cents
from .market_data import NormalizedMarket, NormalizedOrderBook


class UniverseSelectionError(ValueError):
    """Raised when universe filter parameters or market inputs are invalid."""


@dataclass(frozen=True)
class MarketLiquidityProfile:
    """Structured assessment of an individual market's executable liquidity."""

    ticker: str
    event_ticker: str
    status: str
    volume_24h: Optional[Decimal]
    open_interest: Optional[Decimal]
    best_yes_bid_cents: Optional[int]
    best_yes_ask_cents: Optional[int]
    best_no_bid_cents: Optional[int]
    best_no_ask_cents: Optional[int]
    bid_ask_spread_cents: Optional[int]
    total_bid_depth_contracts: int
    is_populated: bool
    is_liquid: bool
    exclusion_reasons: Tuple[str, ...]
    liquidity_score: Optional[Decimal] = None  # Higher is more liquid

    @property
    def is_selected(self) -> bool:
        return self.is_liquid

    @property
    def rejection_reasons(self) -> Tuple[str, ...]:
        return self.exclusion_reasons


@dataclass(frozen=True)
class UniverseFilterConfig:
    """Configurable thresholds for liquid market selection."""

    min_volume_24h: Optional[Decimal] = Decimal("10.00")
    min_open_interest: Optional[Decimal] = Decimal("10.00")
    max_spread_cents: Optional[int] = 10
    min_bid_depth_contracts: int = 1
    require_populated_order_book: bool = True
    allowed_series_prefixes: Optional[Tuple[str, ...]] = None
    excluded_series_prefixes: Optional[Tuple[str, ...]] = None
    max_selected_markets: Optional[int] = 50

    def __post_init__(self) -> None:
        if self.min_volume_24h is not None and self.min_volume_24h < Decimal("0"):
            raise UniverseSelectionError("min_volume_24h cannot be negative")
        if self.min_open_interest is not None and self.min_open_interest < Decimal("0"):
            raise UniverseSelectionError("min_open_interest cannot be negative")
        if self.max_spread_cents is not None and self.max_spread_cents < 0:
            raise UniverseSelectionError("max_spread_cents cannot be negative")
        if self.min_bid_depth_contracts < 0:
            raise UniverseSelectionError("min_bid_depth_contracts cannot be negative")
        if self.max_selected_markets is not None and self.max_selected_markets <= 0:
            raise UniverseSelectionError("max_selected_markets must be positive")


@dataclass(frozen=True)
class UniverseSelectionResult:
    """Result of universe evaluation across a catalog of candidate markets."""

    selected_tickers: Tuple[str, ...]
    evaluated_markets_count: int
    selected_count: int
    rejected_count: int
    rejections_by_reason: Dict[str, int]
    profiles: Tuple[MarketLiquidityProfile, ...]
    explanation: str

    @property
    def qualified_markets(self) -> Tuple[str, ...]:
        return self.selected_tickers

    @property
    def total_markets_evaluated(self) -> int:
        return self.evaluated_markets_count

    @property
    def qualified_markets_count(self) -> int:
        return self.selected_count

    @property
    def rejected_markets_count(self) -> int:
        return self.rejected_count

    @property
    def market_profiles(self) -> Dict[str, MarketLiquidityProfile]:
        return {p.ticker: p for p in self.profiles}


class LiquidUniverseSelector:
    """Filters and ranks markets based on verified observable liquidity."""

    def __init__(self, config: Optional[UniverseFilterConfig] = None):
        self.config = config or UniverseFilterConfig()

    def profile_market(
        self,
        market: NormalizedMarket,
        order_book: Optional[NormalizedOrderBook] = None,
    ) -> MarketLiquidityProfile:
        """Construct a rigorous liquidity profile for a single market."""
        if not isinstance(market, NormalizedMarket):
            raise UniverseSelectionError("market must be a NormalizedOrderBook instance")

        reasons: List[str] = []
        is_populated = False
        tot_depth = 0
        best_yb_cents: Optional[int] = None
        best_ya_cents: Optional[int] = None
        best_nb_cents: Optional[int] = None
        best_na_cents: Optional[int] = None
        spread_cents: Optional[int] = None

        # 1. Market Status Check
        if market.status not in ("open", "active"):
            reasons.append(f"Market status is {market.status!r} (expected 'open' or 'active')")

        # 2. Volume & Open Interest Checks
        if self.config.min_volume_24h is not None:
            if market.volume_24h is None:
                reasons.append("24h volume data unavailable from exchange API")
            elif market.volume_24h < self.config.min_volume_24h:
                reasons.append(
                    f"24h volume {market.volume_24h} < minimum threshold {self.config.min_volume_24h}"
                )

        if self.config.min_open_interest is not None:
            if market.open_interest is None:
                reasons.append("Open interest data unavailable from exchange API")
            elif market.open_interest < self.config.min_open_interest:
                reasons.append(
                    f"Open interest {market.open_interest} < minimum threshold {self.config.min_open_interest}"
                )

        # 3. Series Filtering
        if self.config.allowed_series_prefixes:
            if not any(market.ticker.startswith(p) for p in self.config.allowed_series_prefixes):
                reasons.append(
                    f"Ticker {market.ticker} does not match allowed series prefixes {self.config.allowed_series_prefixes}"
                )

        if self.config.excluded_series_prefixes:
            if any(market.ticker.startswith(p) for p in self.config.excluded_series_prefixes):
                reasons.append(
                    f"Ticker {market.ticker} matches excluded series prefixes {self.config.excluded_series_prefixes}"
                )

        # 4. Order Book Analysis (if available)
        if order_book is not None:
            if not isinstance(order_book, NormalizedOrderBook):
                raise UniverseSelectionError("order_book must be a NormalizedOrderBook instance")

            yb_levels = order_book.yes_bids
            nb_levels = order_book.no_bids
            is_populated = len(yb_levels) > 0 or len(nb_levels) > 0
            tot_depth = sum(int(lvl.quantity) for lvl in yb_levels) + sum(int(lvl.quantity) for lvl in nb_levels)

            if yb_levels:
                best_yb_cents = dollars_to_cents(max(lvl.price_dollars for lvl in yb_levels))
                # NO ask derived from YES bid
                best_na_cents = 100 - best_yb_cents

            if nb_levels:
                best_nb_cents = dollars_to_cents(max(lvl.price_dollars for lvl in nb_levels))
                # YES ask derived from NO bid
                best_ya_cents = 100 - best_nb_cents

            # Calculate spread on YES side if both bid and derived ask exist
            if best_yb_cents is not None and best_ya_cents is not None:
                spread_cents = best_ya_cents - best_yb_cents

            if self.config.require_populated_order_book and not is_populated:
                reasons.append("Order book is completely empty (zero resting bids)")

            if tot_depth < self.config.min_bid_depth_contracts:
                reasons.append(
                    f"Total bid depth {tot_depth} contracts < required minimum {self.config.min_bid_depth_contracts}"
                )

            if self.config.max_spread_cents is not None and spread_cents is not None:
                if spread_cents > self.config.max_spread_cents:
                    reasons.append(
                        f"Spread {spread_cents}¢ exceeds maximum allowed spread {self.config.max_spread_cents}¢"
                    )
        elif self.config.require_populated_order_book:
            reasons.append("Order book observation was not provided for evaluation")

        is_liquid = len(reasons) == 0

        # Calculate composite score for ranking (higher is better)
        score: Optional[Decimal] = None
        if is_liquid:
            v_term = (market.volume_24h or Decimal("0")) * Decimal("0.4")
            d_term = Decimal(tot_depth) * Decimal("0.4")
            # Tighter spread adds up to 20 points
            s_val = spread_cents if spread_cents is not None else 100
            s_term = max(Decimal("0"), Decimal("20") - Decimal(s_val))
            score = (v_term + d_term + s_term).quantize(Decimal("0.01"))

        return MarketLiquidityProfile(
            ticker=market.ticker,
            event_ticker=market.event_ticker,
            status=market.status,
            volume_24h=market.volume_24h,
            open_interest=market.open_interest,
            best_yes_bid_cents=best_yb_cents,
            best_yes_ask_cents=best_ya_cents,
            best_no_bid_cents=best_nb_cents,
            best_no_ask_cents=best_na_cents,
            bid_ask_spread_cents=spread_cents,
            total_bid_depth_contracts=tot_depth,
            is_populated=is_populated,
            is_liquid=is_liquid,
            exclusion_reasons=tuple(reasons),
            liquidity_score=score,
        )

    def select_universe(
        self,
        markets: Sequence[NormalizedMarket],
        order_books: Optional[Mapping[str, NormalizedOrderBook]] = None,
    ) -> UniverseSelectionResult:
        """Filter and rank candidate markets into a qualified trading universe."""
        books = order_books or {}
        profiles: List[MarketLiquidityProfile] = []
        rejections: Dict[str, int] = {}

        for m in markets:
            b = books.get(m.ticker)
            prof = self.profile_market(m, b)
            profiles.append(prof)
            if not prof.is_liquid:
                for r in prof.exclusion_reasons:
                    rejections[r] = rejections.get(r, 0) + 1

        # Sort liquid markets by liquidity score descending
        liquid_profiles = [p for p in profiles if p.is_liquid]
        liquid_profiles.sort(key=lambda p: p.liquidity_score or Decimal("0"), reverse=True)

        # Apply maximum selected limit
        if self.config.max_selected_markets is not None:
            liquid_profiles = liquid_profiles[: self.config.max_selected_markets]

        selected_tickers = tuple(p.ticker for p in liquid_profiles)

        explanation = (
            f"Evaluated {len(markets)} markets: {len(selected_tickers)} selected as liquid, "
            f"{len(markets) - len(selected_tickers)} rejected."
        )

        return UniverseSelectionResult(
            selected_tickers=selected_tickers,
            evaluated_markets_count=len(markets),
            selected_count=len(selected_tickers),
            rejected_count=len(markets) - len(selected_tickers),
            rejections_by_reason=rejections,
            profiles=tuple(profiles),
            explanation=explanation,
        )

    def select_universe_from_dataset(
        self,
        dataset: Any,
    ) -> UniverseSelectionResult:
        """Extract unique markets and latest order books from a HistoricalDataset and select universe."""
        latest_markets: Dict[str, NormalizedMarket] = {}
        latest_books: Dict[str, NormalizedOrderBook] = {}

        for obs in dataset.observations:
            if obs.market is not None:
                latest_markets[obs.ticker] = obs.market
            if obs.order_book is not None:
                latest_books[obs.ticker] = obs.order_book

        markets = list(latest_markets.values())
        return self.select_universe(markets, latest_books)
