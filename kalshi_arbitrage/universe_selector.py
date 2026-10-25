"""Kalshi Arbitrage Engine V16 — Research Universe Selection & Verification.

Provides conservative, auditable discovery and verification of eligible MECE baskets
and constituent markets from observable market data and verified rules.

Governing Principles:
1. Conservative Inclusion: No basket is accepted without verified MECE outcome definitions,
   open/active market status, sufficient depth, and tight spreads.
2. Never Infer MECE Relationships: Mutually exclusive and collectively exhaustive relationships
   are never inferred from ticker naming conventions or prices alone; they require verified rules.
3. Explicit Rejection Taxonomy: Every rejected basket or market records exact, categorized reasons.
4. Liquidity Grounding: Measures liquidity from actual order-book levels rather than volume metrics.
"""

from dataclasses import dataclass, field
from decimal import Decimal
import json
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .arbitrage import dollars_to_cents
from .contract import NO, YES
from .execution import derive_ask_levels
from .market_data import NormalizedMarket, NormalizedOrderBook
from .observer import DeclaredMeceBasket, ObserverError


@dataclass(frozen=True)
class UniverseSelectorConfig:
    """Configuration thresholds for eligible MECE research basket selection."""

    min_outcomes: int = 2
    max_outcomes: int = 20
    max_spread_cents: Optional[int] = 15
    min_depth_contracts: int = 1
    require_active_status: bool = True
    require_populated_order_book: bool = True
    require_explicit_mece_notes: bool = True


@dataclass(frozen=True)
class BasketSelectionProfile:
    """Detailed evaluation profile for a candidate MECE basket."""

    basket_id: str
    event_ticker: str
    outcomes: Tuple[str, ...]
    market_tickers: Tuple[str, ...]
    is_accepted: bool
    rejection_reasons: Tuple[str, ...]
    missing_data: Tuple[str, ...]
    combined_yes_ask_cents: Optional[int]
    min_depth_contracts: int
    max_leg_spread_cents: Optional[int]

    @property
    def outcome_count(self) -> int:
        return len(self.outcomes)


@dataclass(frozen=True)
class UniverseSelectionReport:
    """Comprehensive report summarizing universe discovery and screening."""

    discovered_baskets_count: int
    accepted_baskets_count: int
    excluded_baskets_count: int
    rejections_by_reason: Dict[str, int]
    profiles: Tuple[BasketSelectionProfile, ...]
    config: UniverseSelectorConfig

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discovered_baskets_count": self.discovered_baskets_count,
            "accepted_baskets_count": self.accepted_baskets_count,
            "excluded_baskets_count": self.excluded_baskets_count,
            "rejections_by_reason": dict(self.rejections_by_reason),
            "config": {
                "min_outcomes": self.config.min_outcomes,
                "max_outcomes": self.config.max_outcomes,
                "max_spread_cents": self.config.max_spread_cents,
                "min_depth_contracts": self.config.min_depth_contracts,
                "require_active_status": self.config.require_active_status,
                "require_populated_order_book": self.config.require_populated_order_book,
            },
            "profiles": [
                {
                    "basket_id": p.basket_id,
                    "event_ticker": p.event_ticker,
                    "is_accepted": p.is_accepted,
                    "rejection_reasons": list(p.rejection_reasons),
                    "missing_data": list(p.missing_data),
                    "combined_yes_ask_cents": p.combined_yes_ask_cents,
                    "min_depth_contracts": p.min_depth_contracts,
                    "max_leg_spread_cents": p.max_leg_spread_cents,
                }
                for p in self.profiles
            ],
        }

    def generate_markdown(self) -> str:
        lines = [
            "# V16 MECE Research Universe Selection Report",
            "",
            "## Summary",
            f"- **Discovered Baskets:** {self.discovered_baskets_count}",
            f"- **Accepted Baskets:** {self.accepted_baskets_count}",
            f"- **Excluded Baskets:** {self.excluded_baskets_count}",
            "",
            "## Screening Criteria",
            f"- Min/Max Outcomes: [{self.config.min_outcomes}, {self.config.max_outcomes}]",
            f"- Max Allowable Spread: {self.config.max_spread_cents}¢" if self.config.max_spread_cents else "- Max Allowable Spread: None",
            f"- Min Depth Required: {self.config.min_depth_contracts} contract(s)",
            f"- Require Active Status: {self.config.require_active_status}",
            "",
            "## Rejection Breakdown",
        ]
        if not self.rejections_by_reason:
            lines.append("No rejections recorded.")
        else:
            for reason, count in sorted(self.rejections_by_reason.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"- **{reason}**: {count}")

        lines.extend(["", "## Basket Evaluation Details"])
        for p in self.profiles:
            status_badge = "✅ ACCEPTED" if p.is_accepted else "❌ EXCLUDED"
            lines.extend([
                f"### {p.basket_id} ({p.event_ticker}) — {status_badge}",
                f"- Outcomes: {p.outcome_count} legs ({', '.join(p.outcomes)})",
                f"- Combined YES Ask: {p.combined_yes_ask_cents}¢" if p.combined_yes_ask_cents is not None else "- Combined YES Ask: N/A",
                f"- Min Available Depth: {p.min_depth_contracts} contracts",
                f"- Max Leg Spread: {p.max_leg_spread_cents}¢" if p.max_leg_spread_cents is not None else "- Max Leg Spread: N/A",
            ])
            if p.rejection_reasons:
                lines.append(f"- Rejection Reasons: {', '.join(p.rejection_reasons)}")
            if p.missing_data:
                lines.append(f"- Missing Data: {', '.join(p.missing_data)}")
            lines.append("")

        return "\n".join(lines)


class MeceUniverseSelector:
    """Evaluates candidate MECE baskets against market status, rules, and depth."""

    def __init__(self, config: Optional[UniverseSelectorConfig] = None) -> None:
        self.config = config or UniverseSelectorConfig()

    def evaluate_basket(
        self,
        basket: DeclaredMeceBasket,
        markets: Mapping[str, NormalizedMarket],
        order_books: Mapping[str, NormalizedOrderBook],
    ) -> BasketSelectionProfile:
        rejections: List[str] = []
        missing: List[str] = []

        # 1. Outcome count check
        n_outcomes = len(basket.outcomes)
        if n_outcomes < self.config.min_outcomes:
            rejections.append("INSUFFICIENT_OUTCOMES")
        if n_outcomes > self.config.max_outcomes:
            rejections.append("EXCESSIVE_OUTCOMES")

        # 2. Check completeness of market mappings
        for outcome in basket.outcomes:
            mapped = [t for t, o in basket.market_outcome_map.items() if o == outcome]
            if not mapped:
                rejections.append(f"MISSING_MAPPING_FOR_OUTCOME_{outcome}")

        # 3. Check market existence and status
        total_yes_ask_cents = 0
        min_depth = 999999999
        max_spread = 0

        for ticker in basket.ordered_tickers:
            m = markets.get(ticker)
            b = order_books.get(ticker)

            if m is None:
                missing.append(f"MARKET_METADATA_{ticker}")
                rejections.append("MISSING_MARKET_METADATA")
            elif self.config.require_active_status and m.status.lower() != "active":
                rejections.append(f"MARKET_INACTIVE_{ticker}")

            if b is None:
                missing.append(f"ORDER_BOOK_{ticker}")
                rejections.append("MISSING_ORDER_BOOK")
            else:
                # Check ask availability
                if not b.yes_bids and not b.no_bids:
                    if self.config.require_populated_order_book:
                        rejections.append(f"EMPTY_ORDER_BOOK_{ticker}")

                # Check ask availability
                asks = derive_ask_levels(b, basket.basket_side)
                if asks:
                    best_ask = asks[0]
                    total_yes_ask_cents += best_ask.price_cents
                    depth = sum(a.quantity for a in asks)
                    min_depth = min(min_depth, depth)
                    if depth < self.config.min_depth_contracts:
                        rejections.append(f"INSUFFICIENT_DEPTH_{ticker}_{depth}_CONTRACTS")

                    # Spread calculation if direct bids exist
                    bids = b.yes_bids if basket.basket_side == YES else b.no_bids
                    if bids:
                        best_bid_cents = dollars_to_cents(max(bid.price_dollars for bid in bids))
                        spread = best_ask.price_cents - best_bid_cents
                        if spread >= 0:
                            max_spread = max(max_spread, spread)
                            if self.config.max_spread_cents is not None and spread > self.config.max_spread_cents:
                                rejections.append(f"WIDE_SPREAD_{ticker}_{spread}c")
                else:
                    rejections.append(f"NO_YES_ASK_{ticker}")

        is_accepted = len(rejections) == 0
        final_depth = min_depth if min_depth != 999999999 else 0
        final_ask_sum = total_yes_ask_cents if is_accepted or total_yes_ask_cents > 0 else None

        return BasketSelectionProfile(
            basket_id=basket.basket_id or f"basket-{basket.event_ticker}",
            event_ticker=basket.event_ticker,
            outcomes=basket.outcomes,
            market_tickers=basket.ordered_tickers,
            is_accepted=is_accepted,
            rejection_reasons=tuple(rejections),
            missing_data=tuple(missing),
            combined_yes_ask_cents=final_ask_sum,
            min_depth_contracts=final_depth,
            max_leg_spread_cents=max_spread if max_spread > 0 else None,
        )

    def select_universe(
        self,
        baskets: Sequence[DeclaredMeceBasket],
        markets: Mapping[str, NormalizedMarket],
        order_books: Mapping[str, NormalizedOrderBook],
    ) -> UniverseSelectionReport:
        profiles: List[BasketSelectionProfile] = []
        rejection_counts: Dict[str, int] = {}

        for b in baskets:
            prof = self.evaluate_basket(b, markets, order_books)
            profiles.append(prof)
            for r in prof.rejection_reasons:
                rejection_counts[r] = rejection_counts.get(r, 0) + 1

        accepted = sum(1 for p in profiles if p.is_accepted)
        excluded = len(profiles) - accepted

        return UniverseSelectionReport(
            discovered_baskets_count=len(baskets),
            accepted_baskets_count=accepted,
            excluded_baskets_count=excluded,
            rejections_by_reason=rejection_counts,
            profiles=tuple(profiles),
            config=self.config,
        )
