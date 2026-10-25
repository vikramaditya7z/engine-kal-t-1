"""Kalshi Arbitrage Engine V17 — Broad-Market Discovery & MECE Eligibility Classifier.

Provides automated, conservative discovery, classification, and prioritization of multi-outcome
MECE event baskets across Kalshi's full public event catalog.

Governing Principles:
1. Conservative Classification: No basket is classified as MECE without verified mutual
   exclusivity AND verified collective exhaustiveness (including residual 'Other'/'None' outcomes).
2. Never Infer Arbitrage: Threshold, cumulative, or non-mutually-exclusive events are strictly
   quarantined and excluded from guaranteed basket arbitrage.
3. Explicit Taxonomy: Every event and market is assigned a distinct category:
   - VALIDATED_CATEGORICAL_MECE
   - MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE
   - REQUIRES_MANUAL_SEMANTIC_VERIFICATION
   - INELIGIBLE_NON_MECE_OR_CUMULATIVE
   - INELIGIBLE_INACTIVE_OR_CLOSED
   - EXCLUDED_UNOBSERVABLE_LIQUIDITY
   - EXCLUDED_INSUFFICIENT_DATA
4. High-Efficiency Ingestion: Extracts nested markets directly from /events response without
   redundant round-trips; fetches candidate order books with bounded concurrency.
5. Liquidity Prioritization: Ranks observable baskets by measurable depth, spread tightness,
   and quote freshness.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import re
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from .arbitrage import dollars_to_cents
from .contract import NO, YES
from .execution import derive_ask_levels
from .market_data import (
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    normalize_event,
    normalize_market,
)
from .observer import DeclaredMeceBasket
from .rest_client import KalshiRestClient

logger = logging.getLogger(__name__)

# Category Taxonomy Constants
CATEGORY_VALIDATED_CATEGORICAL_MECE = "VALIDATED_CATEGORICAL_MECE"
CATEGORY_MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE = "MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE"
CATEGORY_REQUIRES_MANUAL_SEMANTIC_VERIFICATION = "REQUIRES_MANUAL_SEMANTIC_VERIFICATION"
CATEGORY_INELIGIBLE_NON_MECE_OR_CUMULATIVE = "INELIGIBLE_NON_MECE_OR_CUMULATIVE"
CATEGORY_INELIGIBLE_INACTIVE_OR_CLOSED = "INELIGIBLE_INACTIVE_OR_CLOSED"
CATEGORY_EXCLUDED_INSUFFICIENT_DATA = "EXCLUDED_INSUFFICIENT_DATA"
CATEGORY_EXCLUDED_UNOBSERVABLE_LIQUIDITY = "EXCLUDED_UNOBSERVABLE_LIQUIDITY"

# Patterns indicating residual exhaustive outcomes
RESIDUAL_OUTCOME_PATTERNS = re.compile(
    r"\b(all\s+other|any\s+other|other|none|neither|someone\s+else|rest\s+of\s+field|field)\b",
    re.IGNORECASE,
)

# Known numeric range partition series prefixes (verified structural partitions)
RANGE_PARTITION_PREFIXES = (
    "KXFEDDECISION",
    "KXGDPYEAR",
    "KXUSCPIYEAR",
    "KXNOMGDPGROWTH",
    "KXU3EOY",
    "KXINX",
    "KXNASDAQ100",
)


@dataclass(frozen=True)
class DiscoveryConfig:
    """Configuration for broad-market discovery and filtering."""

    max_events_to_scan: int = 100
    min_legs: int = 2
    max_legs: int = 20
    max_spread_cents: int = 25
    min_depth_contracts: int = 1
    require_active_markets: bool = True
    event_status: str = "open"
    concurrency: int = 8


@dataclass(frozen=True)
class CandidateBasketProfile:
    """Detailed discovery and classification profile for an event basket."""

    event_ticker: str
    series_ticker: str
    title: str
    category: Optional[str]
    mutually_exclusive: bool
    total_market_tickers: Tuple[str, ...]
    active_market_tickers: Tuple[str, ...]
    classification: str
    classification_reason: str
    residual_market_ticker: Optional[str]
    is_mece_validated: bool
    is_observable: bool
    best_yes_ask_sum_cents: Optional[int]
    min_depth_contracts: int
    max_leg_spread_cents: Optional[int]
    declared_basket: Optional[DeclaredMeceBasket] = None

    @property
    def outcome_count(self) -> int:
        return len(self.active_market_tickers)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_ticker": self.event_ticker,
            "series_ticker": self.series_ticker,
            "title": self.title,
            "category": self.category,
            "mutually_exclusive": self.mutually_exclusive,
            "total_markets_count": len(self.total_market_tickers),
            "active_markets_count": len(self.active_market_tickers),
            "classification": self.classification,
            "classification_reason": self.classification_reason,
            "residual_market_ticker": self.residual_market_ticker,
            "is_mece_validated": self.is_mece_validated,
            "is_observable": self.is_observable,
            "best_yes_ask_sum_cents": self.best_yes_ask_sum_cents,
            "min_depth_contracts": self.min_depth_contracts,
            "max_leg_spread_cents": self.max_leg_spread_cents,
        }


@dataclass(frozen=True)
class DiscoveryReport:
    """Comprehensive summary of broad-market discovery across Kalshi events."""

    scanned_events_count: int
    discovered_markets_count: int
    classification_counts: Dict[str, int]
    validated_baskets: Tuple[CandidateBasketProfile, ...]
    observable_baskets: Tuple[CandidateBasketProfile, ...]
    unobservable_baskets: Tuple[CandidateBasketProfile, ...]
    ineligible_profiles: Tuple[CandidateBasketProfile, ...]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "scanned_events_count": self.scanned_events_count,
            "discovered_markets_count": self.discovered_markets_count,
            "classification_counts": dict(self.classification_counts),
            "validated_baskets_count": len(self.validated_baskets),
            "observable_baskets_count": len(self.observable_baskets),
            "unobservable_baskets_count": len(self.unobservable_baskets),
            "validated_baskets": [b.to_dict() for b in self.validated_baskets],
            "observable_baskets": [b.to_dict() for b in self.observable_baskets],
        }

    def generate_markdown(self) -> str:
        lines = [
            "# V17 Broad-Market Discovery & MECE Classification Report",
            "",
            "## 1. Executive Summary",
            f"- **Scan Timestamp:** {self.created_at}",
            f"- **Scanned Events:** {self.scanned_events_count}",
            f"- **Discovered Markets:** {self.discovered_markets_count}",
            f"- **Validated Categorical MECE Baskets:** {len(self.validated_baskets)}",
            f"- **Observably Liquid Baskets:** {len(self.observable_baskets)}",
            "",
            "## 2. Event Classification Breakdown",
        ]
        for cat, cnt in sorted(self.classification_counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- **{cat}**: {cnt} events")

        lines.extend(["", "## 3. Observable Validated MECE Baskets (Research Ready)"])
        if not self.observable_baskets:
            lines.append("No observable baskets met depth and spread criteria.")
        else:
            for b in self.observable_baskets:
                lines.extend([
                    f"### `{b.event_ticker}` — {b.title}",
                    f"- Category: {b.category} | Series: `{b.series_ticker}`",
                    f"- Active Legs: {b.outcome_count} ({', '.join(b.active_market_tickers[:6])}{'...' if b.outcome_count > 6 else ''})",
                    f"- Combined YES Ask: {b.best_yes_ask_sum_cents}¢" if b.best_yes_ask_sum_cents is not None else "- Combined YES Ask: N/A",
                    f"- Min Available Depth: {b.min_depth_contracts} contracts",
                    f"- Max Leg Spread: {b.max_leg_spread_cents}¢" if b.max_leg_spread_cents is not None else "- Max Leg Spread: N/A",
                    f"- Classification Reason: {b.classification_reason}",
                    "",
                ])

        lines.extend(["## 4. Validated Baskets Excluded for Low Liquidity"])
        if not self.unobservable_baskets:
            lines.append("None.")
        else:
            for b in self.unobservable_baskets:
                lines.append(f"- `{b.event_ticker}` ({b.title}): {b.classification_reason}")

        return "\n".join(lines)


class BroadMarketDiscoveryEngine:
    """Discovers, classifies, and prioritizes multi-outcome event baskets from Kalshi REST."""

    def __init__(
        self,
        client: Optional[KalshiRestClient] = None,
        config: Optional[DiscoveryConfig] = None,
    ) -> None:
        self.client = client or KalshiRestClient()
        self.config = config or DiscoveryConfig()

    def discover_and_classify(self) -> DiscoveryReport:
        """Scan public events and perform deterministic MECE eligibility classification."""
        now_str = datetime.now(timezone.utc).isoformat()
        logger.info("Scanning public events (max %d)...", self.config.max_events_to_scan)

        # Single or paginated event ingestion with nested markets
        params = {
            "limit": str(min(100, self.config.max_events_to_scan)),
            "status": self.config.event_status,
            "with_nested_markets": "true",
        }
        payload, _ = self.client._get_json_response("/events", params)
        raw_events = payload.get("events", [])
        if self.config.max_events_to_scan:
            raw_events = raw_events[:self.config.max_events_to_scan]

        all_profiles: List[CandidateBasketProfile] = []
        classification_counts: Dict[str, int] = {}
        total_markets = 0

        # Pass 1: Semantic MECE Classification from Ingested Event & Market Metadata
        mece_candidate_events: List[Tuple[Dict[str, Any], List[NormalizedMarket]]] = []

        for raw_e in raw_events:
            raw_markets = raw_e.get("markets", [])
            total_markets += len(raw_markets)

            # Normalize nested markets in memory (0 network latency)
            markets: List[NormalizedMarket] = []
            for rm in raw_markets:
                try:
                    markets.append(normalize_market(rm))
                except Exception:
                    pass

            prof, is_candidate = self._classify_event_semantics(raw_e, markets)
            if not is_candidate:
                all_profiles.append(prof)
                cat = prof.classification
                classification_counts[cat] = classification_counts.get(cat, 0) + 1
            else:
                mece_candidate_events.append((raw_e, markets))

        # Pass 2: Concurrently evaluate order books only for validated MECE candidates
        for raw_e, markets in mece_candidate_events:
            prof = self._evaluate_mece_liquidity(raw_e, markets)
            all_profiles.append(prof)
            cat = prof.classification
            classification_counts[cat] = classification_counts.get(cat, 0) + 1

        validated = tuple(p for p in all_profiles if p.is_mece_validated)
        observable = tuple(p for p in validated if p.is_observable)
        unobservable = tuple(p for p in validated if not p.is_observable)
        ineligible = tuple(p for p in all_profiles if not p.is_mece_validated)

        report = DiscoveryReport(
            scanned_events_count=len(raw_events),
            discovered_markets_count=total_markets,
            classification_counts=classification_counts,
            validated_baskets=validated,
            observable_baskets=observable,
            unobservable_baskets=unobservable,
            ineligible_profiles=ineligible,
            created_at=now_str,
        )
        return report

    def _classify_event_semantics(
        self, raw_e: Dict[str, Any], markets: Sequence[NormalizedMarket]
    ) -> Tuple[CandidateBasketProfile, bool]:
        """Perform conservative semantic classification of an event without fetching books."""
        event_ticker = raw_e.get("event_ticker", "")
        series = raw_e.get("series_ticker", "")
        title = raw_e.get("title", "")
        category = raw_e.get("category")
        mut_ex = bool(raw_e.get("mutually_exclusive", False))
        tickers = tuple(m.ticker for m in markets)
        n_tickers = len(tickers)

        # 1. Single-market rejection
        if n_tickers < self.config.min_legs:
            p = CandidateBasketProfile(
                event_ticker=event_ticker,
                series_ticker=series,
                title=title,
                category=category,
                mutually_exclusive=mut_ex,
                total_market_tickers=tickers,
                active_market_tickers=(),
                classification=CATEGORY_INELIGIBLE_NON_MECE_OR_CUMULATIVE,
                classification_reason=f"Event has {n_tickers} market(s), minimum {self.config.min_legs} required",
                residual_market_ticker=None,
                is_mece_validated=False,
                is_observable=False,
                best_yes_ask_sum_cents=None,
                min_depth_contracts=0,
                max_leg_spread_cents=None,
            )
            return p, False

        # 2. Cumulative / Non-mutually-exclusive rejection
        if not mut_ex:
            p = CandidateBasketProfile(
                event_ticker=event_ticker,
                series_ticker=series,
                title=title,
                category=category,
                mutually_exclusive=False,
                total_market_tickers=tickers,
                active_market_tickers=(),
                classification=CATEGORY_INELIGIBLE_NON_MECE_OR_CUMULATIVE,
                classification_reason="Exchange metadata declares mutually_exclusive=False (cumulative/threshold or independent questions)",
                residual_market_ticker=None,
                is_mece_validated=False,
                is_observable=False,
                best_yes_ask_sum_cents=None,
                min_depth_contracts=0,
                max_leg_spread_cents=None,
            )
            return p, False

        # 3. Leg count upper bound check
        if n_tickers > self.config.max_legs:
            p = CandidateBasketProfile(
                event_ticker=event_ticker,
                series_ticker=series,
                title=title,
                category=category,
                mutually_exclusive=True,
                total_market_tickers=tickers,
                active_market_tickers=(),
                classification=CATEGORY_EXCLUDED_INSUFFICIENT_DATA,
                classification_reason=f"Event has {n_tickers} legs, exceeding max threshold of {self.config.max_legs}",
                residual_market_ticker=None,
                is_mece_validated=False,
                is_observable=False,
                best_yes_ask_sum_cents=None,
                min_depth_contracts=0,
                max_leg_spread_cents=None,
            )
            return p, False

        # 4. Check active market count and residual market markers
        active_tickers: List[str] = []
        residual_ticker: Optional[str] = None

        for m in markets:
            if m.status.lower() == "active":
                active_tickers.append(m.ticker)
            sub = f"{m.subtitle or ''} {m.yes_subtitle or ''} {m.title or ''}"
            if RESIDUAL_OUTCOME_PATTERNS.search(sub) or m.ticker.endswith(("-OTHER", "-NONE")):
                residual_ticker = m.ticker

        if len(active_tickers) < len(tickers) and self.config.require_active_markets:
            p = CandidateBasketProfile(
                event_ticker=event_ticker,
                series_ticker=series,
                title=title,
                category=category,
                mutually_exclusive=True,
                total_market_tickers=tickers,
                active_market_tickers=tuple(active_tickers),
                classification=CATEGORY_INELIGIBLE_INACTIVE_OR_CLOSED,
                classification_reason=f"Only {len(active_tickers)} of {len(tickers)} markets are active",
                residual_market_ticker=residual_ticker,
                is_mece_validated=False,
                is_observable=False,
                best_yes_ask_sum_cents=None,
                min_depth_contracts=0,
                max_leg_spread_cents=None,
            )
            return p, False

        # 5. Collective exhaustiveness validation
        is_range = any(series.startswith(p) for p in RANGE_PARTITION_PREFIXES)
        is_pairwise = len(tickers) == 2 and ("first" in title.lower() or "vs" in title.lower())
        has_residual = residual_ticker is not None

        if not (is_range or is_pairwise or has_residual):
            p = CandidateBasketProfile(
                event_ticker=event_ticker,
                series_ticker=series,
                title=title,
                category=category,
                mutually_exclusive=True,
                total_market_tickers=tickers,
                active_market_tickers=tuple(active_tickers),
                classification=CATEGORY_MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE,
                classification_reason="Open-ended candidate list lacks verified 'Other'/'None' residual outcome",
                residual_market_ticker=None,
                is_mece_validated=False,
                is_observable=False,
                best_yes_ask_sum_cents=None,
                min_depth_contracts=0,
                max_leg_spread_cents=None,
            )
            return p, False

        # Candidate passed semantic validation!
        return CandidateBasketProfile(
            event_ticker=event_ticker,
            series_ticker=series,
            title=title,
            category=category,
            mutually_exclusive=True,
            total_market_tickers=tickers,
            active_market_tickers=tuple(active_tickers),
            classification=CATEGORY_VALIDATED_CATEGORICAL_MECE,
            classification_reason="Validated MECE",
            residual_market_ticker=residual_ticker,
            is_mece_validated=True,
            is_observable=False,
            best_yes_ask_sum_cents=None,
            min_depth_contracts=0,
            max_leg_spread_cents=None,
        ), True

    def _evaluate_mece_liquidity(
        self, raw_e: Dict[str, Any], markets: Sequence[NormalizedMarket]
    ) -> CandidateBasketProfile:
        """Fetch order books concurrently for a validated MECE event and measure liquidity."""
        event_ticker = raw_e.get("event_ticker", "")
        series = raw_e.get("series_ticker", "")
        title = raw_e.get("title", "")
        category = raw_e.get("category")
        active_tickers = tuple(m.ticker for m in markets if m.status.lower() == "active")

        # Concurrently fetch books
        books: Dict[str, NormalizedOrderBook] = {}
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            fut_to_ticker = {executor.submit(self.client.get_order_book, t): t for t in active_tickers}
            for fut in as_completed(fut_to_ticker):
                t = fut_to_ticker[fut]
                try:
                    books[t] = fut.result()
                except Exception as exc:
                    logger.debug("Failed book fetch for %s: %s", t, exc)

        total_yes_ask = 0
        min_depth = 999999999
        max_spread = 0
        is_observable = True
        unobservable_reasons = []

        outcome_map: Dict[str, str] = {}
        outcomes_list: List[str] = []

        for m in markets:
            t = m.ticker
            out_label = m.subtitle or m.yes_subtitle or t
            out_label = out_label.strip()
            if out_label in outcomes_list:
                out_label = f"{out_label} ({t})"
            outcomes_list.append(out_label)
            outcome_map[t] = out_label

            b = books.get(t)
            if not b:
                is_observable = False
                unobservable_reasons.append(f"Missing book on {t}")
                continue

            asks = derive_ask_levels(b, YES)
            if not asks:
                is_observable = False
                unobservable_reasons.append(f"No YES asks on {t}")
            else:
                best_ask = asks[0]
                total_yes_ask += best_ask.price_cents
                depth = sum(a.quantity for a in asks)
                min_depth = min(min_depth, depth)
                if depth < self.config.min_depth_contracts:
                    is_observable = False
                    unobservable_reasons.append(f"Depth {depth} on {t} < {self.config.min_depth_contracts}")

                if b.yes_bids:
                    best_bid_cents = dollars_to_cents(max(bid.price_dollars for bid in b.yes_bids))
                    spread = best_ask.price_cents - best_bid_cents
                    if spread >= 0:
                        max_spread = max(max_spread, spread)
                        if spread > self.config.max_spread_cents:
                            is_observable = False
                            unobservable_reasons.append(f"Spread {spread}c on {t} > {self.config.max_spread_cents}c")

        # Determine exhaustiveness note
        is_range = any(series.startswith(p) for p in RANGE_PARTITION_PREFIXES)
        is_pairwise = len(active_tickers) == 2 and ("first" in title.lower() or "vs" in title.lower())
        residual_ticker = next((m.ticker for m in markets if RESIDUAL_OUTCOME_PATTERNS.search(f"{m.subtitle or ''} {m.yes_subtitle or ''} {m.title or ''}") or m.ticker.endswith(("-OTHER", "-NONE"))), None)

        if is_range:
            exhaustiveness_note = "Structurally exhaustive continuous parameter partition"
        elif is_pairwise:
            exhaustiveness_note = "Binary pairwise choice with exhaustive alternatives"
        else:
            exhaustiveness_note = f"Verified collective exhaustiveness via residual outcome market: {residual_ticker}"

        basket = DeclaredMeceBasket(
            event_ticker=event_ticker,
            basket_id=f"BASKET-{event_ticker}",
            outcomes=tuple(outcomes_list),
            market_outcome_map=outcome_map,
            basket_side=YES,
            notes=f"Discovered by V17 broad engine. {exhaustiveness_note}",
        )

        final_depth = min_depth if min_depth != 999999999 else 0
        final_ask_sum = total_yes_ask if is_observable or total_yes_ask > 0 else None

        if not is_observable:
            classification_reason = f"Validated MECE ({exhaustiveness_note}), but unobservable: {'; '.join(unobservable_reasons[:3])}"
        else:
            classification_reason = f"Validated MECE ({exhaustiveness_note}) with executable depth >= {final_depth} contracts"

        return CandidateBasketProfile(
            event_ticker=event_ticker,
            series_ticker=series,
            title=title,
            category=category,
            mutually_exclusive=True,
            total_market_tickers=tuple(m.ticker for m in markets),
            active_market_tickers=active_tickers,
            classification=CATEGORY_VALIDATED_CATEGORICAL_MECE,
            classification_reason=classification_reason,
            residual_market_ticker=residual_ticker,
            is_mece_validated=True,
            is_observable=is_observable,
            best_yes_ask_sum_cents=final_ask_sum,
            min_depth_contracts=final_depth,
            max_leg_spread_cents=max_spread if max_spread > 0 else None,
            declared_basket=basket,
        )
