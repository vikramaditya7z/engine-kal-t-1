"""Live market observation and deterministic opportunity evaluation engine.

V5 Live Market Observer connects fresh, normalized Kalshi public market data to
V2 candidate detection, V3 execution pricing, optional V4 paper execution,
and durable timestamped evidence recording.

Governing Principles:
1. Public market data only: uses read-only GET endpoints; no private trading APIs.
2. Explicit freshness policy: order books missing source timestamps or exceeding
   configured age thresholds are rejected and never treated as current opportunities.
3. Failed refreshes fail closed: an error during refresh leaves that market without
   current depth; stale data is never silently reused.
4. Snapshot consistency: multi-leg strategies verify that timestamp skew between legs
   does not exceed configured tolerances.
5. Deduplication and identity: distinguishes stable opportunity identity from
   ephemeral point-in-time observation IDs; repeated polls do not create duplicate
   paper trades.
6. Exact arithmetic: preserves integer-cent accounting and exact Decimal models.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import logging
import time
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
import uuid

from .arbitrage import (
    ArbitrageOpportunity,
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    evaluate_binary_parity,
    evaluate_market_parity,
    evaluate_mece_markets,
)
from .contract import NO, YES
from .execution import (
    STATUS_DEPTH_SUPPORTED_GROSS_POSITIVE,
    STATUS_DEPTH_SUPPORTED_NET_PROFITABLE,
    STATUS_DEPTH_SUPPORTED_NET_UNPROFITABLE,
    STATUS_INSUFFICIENT_DEPTH,
    STATUS_INVALID_INPUT,
    STATUS_NO_GROSS_EDGE,
    STATUS_PARTIALLY_SUPPORTED,
    ExecutionPricingResult,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
)
from .market_data import (
    MarketDataInputError,
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
)
from .paper_executor import PaperExecutionEngine
from .paper_trade import PaperTrade, TradeState
from .portfolio import Event
from .rest_client import KalshiClientError, KalshiRestClient

logger = logging.getLogger(__name__)

# Observation and evaluation status constants
STATUS_QUALIFIED = "QUALIFIED"
STATUS_REJECTED_STALE = "REJECTED_STALE"
STATUS_REJECTED_MISSING_TIMESTAMP = "REJECTED_MISSING_TIMESTAMP"
STATUS_REJECTED_LEG_SKEW = "REJECTED_LEG_SKEW"
STATUS_REJECTED_INSUFFICIENT_DEPTH = "REJECTED_INSUFFICIENT_DEPTH"
STATUS_REJECTED_UNPROFITABLE_FEES = "REJECTED_UNPROFITABLE_FEES"
STATUS_REJECTED_NO_GROSS_EDGE = "REJECTED_NO_GROSS_EDGE"
STATUS_REJECTED_MISSING_MARKET = "REJECTED_MISSING_MARKET"
STATUS_REJECTED_RISK = "REJECTED_RISK"
STATUS_REJECTED_INVALID_INPUT = "REJECTED_INVALID_INPUT"


class ObserverError(ValueError):
    """Raised when observer configuration or parameters are invalid."""


@dataclass(frozen=True)
class DeclaredMeceBasket:
    """Explicit declaration of an MECE basket for multi-market arbitrage.

    MECE relationships are never inferred from market text, titles, or series;
    they must be explicitly declared and validated.
    """

    event_ticker: str
    outcomes: Tuple[str, ...]
    market_outcome_map: Mapping[str, str]  # ticker -> outcome
    basket_side: str = YES

    def __post_init__(self) -> None:
        if not isinstance(self.event_ticker, str) or not self.event_ticker:
            raise ObserverError("event_ticker must be a non-empty string")
        if not isinstance(self.outcomes, tuple) or len(self.outcomes) < 2:
            raise ObserverError("outcomes must be a tuple with at least 2 outcomes")
        if len(set(self.outcomes)) != len(self.outcomes):
            raise ObserverError("outcomes must contain unique strings")
        if not isinstance(self.market_outcome_map, Mapping):
            raise ObserverError("market_outcome_map must be a mapping")
        if self.basket_side not in (YES, NO):
            raise ObserverError(f"basket_side must be {YES!r} or {NO!r}")
        for ticker, outcome in self.market_outcome_map.items():
            if not isinstance(ticker, str) or not ticker:
                raise ObserverError("market ticker must be a non-empty string")
            if outcome not in self.outcomes:
                raise ObserverError(f"mapped outcome {outcome!r} not in declared outcomes")
        if len(self.market_outcome_map) != len(self.outcomes):
            raise ObserverError("market_outcome_map must map exactly all declared outcomes")

    @property
    def ordered_tickers(self) -> Tuple[str, ...]:
        """Market tickers ordered strictly by declared outcomes order."""
        outcome_to_ticker = {outcome: ticker for ticker, outcome in self.market_outcome_map.items()}
        return tuple(outcome_to_ticker[o] for o in self.outcomes)


@dataclass(frozen=True)
class MarketObserverConfig:
    """Configuration for public market observation and evaluation."""

    poll_interval_seconds: float = 1.0
    max_stale_seconds: float = 60.0
    max_leg_timestamp_skew_seconds: float = 5.0
    min_request_interval_seconds: float = 0.05
    target_quantity: int = 1
    default_fee_per_contract_cents: Optional[int] = None
    enable_paper_trading: bool = False
    require_source_timestamp: bool = True
    enforce_net_profitability: bool = True
    max_cycles: Optional[int] = None
    max_discovered_markets: int = 50
    max_poll_markets_per_cycle: Optional[int] = 50
    discovery_refresh_interval_cycles: Optional[int] = 20
    verbose_progress: bool = True

    def __post_init__(self) -> None:
        if self.poll_interval_seconds < 0:
            raise ObserverError("poll_interval_seconds must be non-negative")
        if self.max_stale_seconds <= 0:
            raise ObserverError("max_stale_seconds must be positive")
        if self.max_leg_timestamp_skew_seconds <= 0:
            raise ObserverError("max_leg_timestamp_skew_seconds must be positive")
        if self.min_request_interval_seconds < 0:
            raise ObserverError("min_request_interval_seconds must be non-negative")
        if self.target_quantity <= 0:
            raise ObserverError("target_quantity must be positive")
        if (
            self.default_fee_per_contract_cents is not None
            and self.default_fee_per_contract_cents < 0
        ):
            raise ObserverError("default_fee_per_contract_cents must be non-negative")
        if self.max_cycles is not None and self.max_cycles <= 0:
            raise ObserverError("max_cycles must be positive")
        if self.max_discovered_markets <= 0:
            raise ObserverError("max_discovered_markets must be positive")
        if self.max_poll_markets_per_cycle is not None and self.max_poll_markets_per_cycle <= 0:
            raise ObserverError("max_poll_markets_per_cycle must be positive")
        if (
            self.discovery_refresh_interval_cycles is not None
            and self.discovery_refresh_interval_cycles <= 0
        ):
            raise ObserverError("discovery_refresh_interval_cycles must be positive")


@dataclass(frozen=True)
class MarketObservation:
    """Timestamped snapshot of a single market's order book and metadata.

    source_timestamp is the exchange transport timestamp (derived from the HTTP Date
    header of the order-book GET response). Limitation: HTTP Date establishes response
    time, not necessarily the exact matching-engine snapshot generation time or proof
    that an upstream cache was bypassed.
    """

    observation_id: str
    ticker: str
    observed_at: datetime
    source_timestamp: Optional[datetime]
    market: Optional[NormalizedMarket]
    order_book: Optional[NormalizedOrderBook]
    is_success: bool
    is_stale: bool
    staleness_reason: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True)
class EvaluatedOpportunity:
    """Evaluated arbitrage opportunity combining V2 detection and V3 execution."""

    observation_id: str
    opportunity_id: str
    strategy_type: str
    observed_at: datetime
    source_timestamps: Mapping[str, Optional[datetime]]
    leg_timestamp_skew_seconds: float
    market_tickers: Tuple[str, ...]
    candidate_opportunity: ArbitrageOpportunity
    pricing_result: Optional[ExecutionPricingResult]
    status: str
    rejection_reason: Optional[str]
    fee_per_contract_cents: Optional[int]
    is_qualified: bool
    is_recurrent: bool = False
    paper_trade_id: Optional[str] = None


def generate_observation_id() -> str:
    """Generate a unique point-in-time observation ID."""
    return f"obs-{uuid.uuid4().hex[:12]}"


def make_binary_parity_opportunity_id(ticker: str) -> str:
    """Generate a stable strategy ID for single-market binary parity."""
    return f"BP:{ticker}"


def make_mece_opportunity_id(event_ticker: str, side: str) -> str:
    """Generate a stable strategy ID for MECE basket arbitrage."""
    return f"MECE:{event_ticker}:{side}"


class MarketObserver:
    """Continuously observes public Kalshi market data and evaluates arbitrage opportunities."""

    def __init__(
        self,
        client: KalshiRestClient,
        *,
        config: Optional[MarketObserverConfig] = None,
        target_tickers: Optional[Sequence[str]] = None,
        declared_baskets: Optional[Sequence[DeclaredMeceBasket]] = None,
        evidence_store: Optional[Any] = None,
        paper_portfolio: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        time_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not isinstance(client, KalshiRestClient):
            raise ObserverError("client must be a KalshiRestClient instance")
        self.client = client
        self.target_tickers: Tuple[str, ...] = tuple(target_tickers or ())
        if config is not None:
            self.config = config
        elif self.target_tickers:
            self.config = MarketObserverConfig(
                max_poll_markets_per_cycle=len(self.target_tickers),
                max_discovered_markets=len(self.target_tickers),
            )
        else:
            self.config = MarketObserverConfig()
        self.declared_baskets: Tuple[DeclaredMeceBasket, ...] = tuple(declared_baskets or ())
        self.evidence_store = evidence_store
        self.paper_portfolio = paper_portfolio
        self.risk_manager = risk_manager
        self.progress_callback = progress_callback
        self._time_provider = time_provider or (lambda: datetime.now(timezone.utc))
        self._discovered_tickers: Optional[Tuple[str, ...]] = None

        if self.config.enable_paper_trading:
            self.paper_executor = PaperExecutionEngine(
                default_fee_per_contract_cents=self.config.default_fee_per_contract_cents
            )
        else:
            self.paper_executor = None

        self._stop_requested: bool = False
        self._cycle_count: int = 0
        self._last_request_time: float = 0.0

        # Opportunity tracking & deduplication
        self._active_opportunities: Dict[str, EvaluatedOpportunity] = {}
        self._submitted_paper_opportunities: Set[str] = set()

        # Metrics state
        self._opportunity_first_seen: Dict[str, datetime] = {}
        self._opportunity_last_seen: Dict[str, datetime] = {}
        self._opportunity_lifetimes: List[float] = []

        self._observations_history: List[MarketObservation] = []
        self._evaluations_history: List[EvaluatedOpportunity] = []
        self._paper_trades_history: List[PaperTrade] = []

    def stop(self) -> None:
        """Signal the observer polling loop to cleanly stop."""
        self._stop_requested = True

    def _report_progress(self, message: str) -> None:
        """Report progress message to caller callback or stdout."""
        if self.progress_callback is not None:
            self.progress_callback(message)
        elif self.config.verbose_progress:
            print(message, flush=True)

    def _now(self) -> datetime:
        """Return current timestamp from configured time provider."""
        return self._time_provider()

    def _sleep_with_stop_check(self, duration_seconds: float) -> None:
        """Sleep in small steps so stop() or interrupts break promptly."""
        end_time = time.monotonic() + duration_seconds
        while not self._stop_requested:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))

    def _pace_request(self) -> None:
        """Enforce minimum delay between outgoing REST requests."""
        if self.config.min_request_interval_seconds > 0 and self._last_request_time > 0:
            elapsed = time.monotonic() - self._last_request_time
            remaining = self.config.min_request_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_time = time.monotonic()

    def discover_markets(self, force_refresh: bool = False) -> Tuple[str, ...]:
        """Discover active market tickers with an enforced upper bound."""
        if self.target_tickers:
            return self.target_tickers
        if not force_refresh and self._discovered_tickers is not None:
            return self._discovered_tickers
        if self._stop_requested:
            return ()

        self._report_progress(
            f"Discovering open markets (limit: {self.config.max_discovered_markets})..."
        )
        self._pace_request()
        try:
            markets = self.client.list_markets(
                status="open",
                max_markets=self.config.max_discovered_markets,
            )
            self._discovered_tickers = tuple(m.ticker for m in markets)
            self._report_progress(
                f"Discovered {len(self._discovered_tickers)} open markets to observe."
            )
            return self._discovered_tickers
        except KalshiClientError as exc:
            logger.warning(f"Failed to discover active markets: {exc}")
            self._report_progress(f"Failed to discover active markets: {exc}")
            return ()


    def check_order_book_freshness(
        self,
        book: Optional[NormalizedOrderBook],
        source_timestamp: Optional[datetime],
        observed_at: datetime,
    ) -> Tuple[bool, Optional[str]]:
        """Validate order book freshness against the documented policy.

        Checks the transport-level exchange timestamp (derived from the HTTP Date header).
        Limitation: HTTP Date establishes response time, not necessarily the exact internal
        snapshot-generation time or proof that an upstream cache was bypassed.

        Returns (is_stale, reason).
        """
        if book is None:
            return True, "No order book available"

        if source_timestamp is None:
            if self.config.require_source_timestamp:
                return True, "Missing exchange source timestamp"
            return False, None

        if not isinstance(source_timestamp, datetime):
            return True, "Malformed exchange source timestamp"

        # Ensure timezone compatibility for age comparison
        obs_tz = observed_at.tzinfo
        if source_timestamp.tzinfo is None and obs_tz is not None:
            source_ts = source_timestamp.replace(tzinfo=obs_tz)
        elif source_timestamp.tzinfo is not None and obs_tz is None:
            source_ts = source_timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            source_ts = source_timestamp

        age_seconds = (observed_at - source_ts).total_seconds()

        # Future timestamp beyond reasonable clock skew (e.g. 10s)
        if age_seconds < -10.0:
            return True, f"Source timestamp is from the future ({age_seconds:.1f}s)"

        if age_seconds > self.config.max_stale_seconds:
            return (
                True,
                f"Order book is stale (age: {age_seconds:.1f}s > max: {self.config.max_stale_seconds:.1f}s)",
            )

        return False, None

    def check_leg_skew(
        self,
        source_timestamps: Mapping[str, Optional[datetime]],
        fallback_timestamp: datetime,
    ) -> Tuple[bool, float, Optional[str]]:
        """Check multi-leg snapshot consistency across related markets.

        Returns (is_skewed, skew_seconds, rejection_reason).
        """
        if len(source_timestamps) <= 1:
            return False, 0.0, None

        times: List[float] = []
        for ticker, ts in source_timestamps.items():
            if ts is None:
                if self.config.require_source_timestamp:
                    return True, 0.0, f"Missing source timestamp for leg {ticker!r}"
                times.append(fallback_timestamp.timestamp())
            else:
                times.append(ts.timestamp())

        skew = max(times) - min(times)
        if skew > self.config.max_leg_timestamp_skew_seconds:
            return (
                True,
                skew,
                f"Excessive leg timestamp skew: {skew:.2f}s > max {self.config.max_leg_timestamp_skew_seconds:.2f}s",
            )

        return False, skew, None

    def poll_market(self, ticker: str) -> MarketObservation:
        """Fetch fresh market metadata and order book with explicit error handling."""
        obs_id = generate_observation_id()
        now = self._now()

        market: Optional[NormalizedMarket] = None
        book: Optional[NormalizedOrderBook] = None
        error_msg: Optional[str] = None

        self._pace_request()
        try:
            market = self.client.get_market(ticker)
        except KalshiClientError as exc:
            error_msg = f"Failed to fetch market {ticker}: {exc}"
            return MarketObservation(
                observation_id=obs_id,
                ticker=ticker,
                observed_at=now,
                source_timestamp=None,
                market=None,
                order_book=None,
                is_success=False,
                is_stale=True,
                staleness_reason="Market fetch failed",
                error_message=error_msg,
            )
        except MarketDataInputError as exc:
            return MarketObservation(
                observation_id=obs_id,
                ticker=ticker,
                observed_at=now,
                source_timestamp=None,
                market=None,
                order_book=None,
                is_success=False,
                is_stale=True,
                staleness_reason="Malformed market payload",
                error_message=str(exc),
            )

        self._pace_request()
        try:
            book = self.client.get_order_book(ticker)
        except KalshiClientError as exc:
            error_msg = f"Failed to fetch order book {ticker}: {exc}"
            return MarketObservation(
                observation_id=obs_id,
                ticker=ticker,
                observed_at=now,
                source_timestamp=None,
                market=market,
                order_book=None,
                is_success=False,
                is_stale=True,
                staleness_reason="Order book fetch failed",
                error_message=error_msg,
            )
        except MarketDataInputError as exc:
            reason = (
                "Malformed exchange source timestamp"
                if "HTTP Date" in str(exc)
                else "Malformed order book payload"
            )
            return MarketObservation(
                observation_id=obs_id,
                ticker=ticker,
                observed_at=now,
                source_timestamp=None,
                market=market,
                order_book=None,
                is_success=False,
                is_stale=True,
                staleness_reason=reason,
                error_message=str(exc),
            )

        book_source_ts = book.source_timestamp
        is_stale, stale_reason = self.check_order_book_freshness(book, book_source_ts, now)

        obs = MarketObservation(
            observation_id=obs_id,
            ticker=ticker,
            observed_at=now,
            source_timestamp=book_source_ts,
            market=market,
            order_book=book,
            is_success=True,
            is_stale=is_stale,
            staleness_reason=stale_reason,
            error_message=None,
        )

        if self.evidence_store is not None:
            self.evidence_store.record_observation(obs)

        return obs

    def evaluate_binary_parity_candidate(
        self,
        observation: MarketObservation,
    ) -> Optional[EvaluatedOpportunity]:
        """Evaluate single-market binary parity across V2 detection and V3 execution."""
        ticker = observation.ticker
        opp_id = make_binary_parity_opportunity_id(ticker)
        now = observation.observed_at
        source_ts_map = {ticker: observation.source_timestamp}

        # 1. Verify market quote presence & freshness
        if not observation.is_success or observation.market is None or observation.order_book is None:
            return None

        # V2 Candidate Detection
        try:
            candidate = evaluate_market_parity(
                observation.market,
                quantity=self.config.target_quantity,
                fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            )
        except Exception as exc:
            return None

        # If not a gross theoretical candidate and not break-even, skip or record rejection
        if not candidate.is_arbitrage and candidate.gross_edge_cents <= 0:
            return None

        # Check freshness
        if observation.is_stale:
            status = (
                STATUS_REJECTED_MISSING_TIMESTAMP
                if observation.staleness_reason == "Missing exchange source timestamp"
                else STATUS_REJECTED_STALE
            )
            return EvaluatedOpportunity(
                observation_id=observation.observation_id,
                opportunity_id=opp_id,
                strategy_type=OPPORTUNITY_BINARY_PARITY,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=0.0,
                market_tickers=(ticker,),
                candidate_opportunity=candidate,
                pricing_result=None,
                status=status,
                rejection_reason=observation.staleness_reason,
                fee_per_contract_cents=self.config.default_fee_per_contract_cents,
                is_qualified=False,
            )

        # 2. V3 Execution Pricing
        pricing = evaluate_binary_parity_execution(
            observation.order_book,
            requested_quantity=self.config.target_quantity,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
        )

        # Evaluate qualification
        is_qualified = False
        status = pricing.status
        rejection_reason = pricing.rejection_reason

        if pricing.supported_quantity == 0:
            status = STATUS_REJECTED_INSUFFICIENT_DEPTH
            rejection_reason = "Order book has no available ask liquidity"
        elif not pricing.is_gross_profitable:
            status = STATUS_REJECTED_NO_GROSS_EDGE
            rejection_reason = "No gross edge when traversing order book depth"
        elif self.config.enforce_net_profitability and pricing.is_net_profitable is False:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Unprofitable after estimated fees"
        elif self.config.enforce_net_profitability and pricing.is_net_profitable is None:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Net profitability cannot be verified without fee configuration"
        else:
            status = STATUS_QUALIFIED
            is_qualified = True
            rejection_reason = None

        return EvaluatedOpportunity(
            observation_id=observation.observation_id,
            opportunity_id=opp_id,
            strategy_type=OPPORTUNITY_BINARY_PARITY,
            observed_at=now,
            source_timestamps=source_ts_map,
            leg_timestamp_skew_seconds=0.0,
            market_tickers=(ticker,),
            candidate_opportunity=candidate,
            pricing_result=pricing,
            status=status,
            rejection_reason=rejection_reason,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            is_qualified=is_qualified,
        )

    def evaluate_mece_basket_candidate(
        self,
        basket: DeclaredMeceBasket,
        observations: Mapping[str, MarketObservation],
    ) -> Optional[EvaluatedOpportunity]:
        """Evaluate an explicitly declared MECE event basket candidate."""
        opp_id = make_mece_opportunity_id(basket.event_ticker, basket.basket_side)
        now = self._now()
        strat_type = (
            OPPORTUNITY_MECE_BASKET_LONG_YES
            if basket.basket_side == YES
            else OPPORTUNITY_MECE_BASKET_LONG_NO
        )

        # Check all required markets exist in current observations
        missing_tickers = [t for t in basket.market_outcome_map if t not in observations]
        if missing_tickers:
            return None

        basket_obs = [observations[t] for t in basket.market_outcome_map]
        failed_obs = [o for o in basket_obs if not o.is_success or o.market is None or o.order_book is None]
        if failed_obs:
            return None

        markets = [o.market for o in basket_obs]
        books_by_outcome = {
            basket.market_outcome_map[o.ticker]: o.order_book for o in basket_obs
        }
        source_ts_map = {o.ticker: o.source_timestamp for o in basket_obs}

        # Build Event with explicitly established relationship
        event = Event(
            identifier=basket.event_ticker,
            outcomes=basket.outcomes,
            relationship_established=True,
        )

        # V2 Candidate Detection
        try:
            candidate = evaluate_mece_markets(
                event=event,
                markets=markets,
                market_outcome_map=basket.market_outcome_map,
                basket_side=basket.basket_side,
                quantity=self.config.target_quantity,
                fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            )
        except Exception:
            return None

        if not candidate.is_arbitrage and candidate.gross_edge_cents <= 0:
            return None

        # Check freshness on all legs
        stale_legs = [o for o in basket_obs if o.is_stale]
        if stale_legs:
            reasons = [f"{o.ticker}: {o.staleness_reason}" for o in stale_legs]
            return EvaluatedOpportunity(
                observation_id=generate_observation_id(),
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=0.0,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=candidate,
                pricing_result=None,
                status=STATUS_REJECTED_STALE,
                rejection_reason=f"Stale legs: {'; '.join(reasons)}",
                fee_per_contract_cents=self.config.default_fee_per_contract_cents,
                is_qualified=False,
            )

        # Check multi-leg snapshot consistency / skew
        is_skewed, skew_sec, skew_reason = self.check_leg_skew(source_ts_map, now)
        if is_skewed:
            return EvaluatedOpportunity(
                observation_id=generate_observation_id(),
                opportunity_id=opp_id,
                strategy_type=strat_type,
                observed_at=now,
                source_timestamps=source_ts_map,
                leg_timestamp_skew_seconds=skew_sec,
                market_tickers=basket.ordered_tickers,
                candidate_opportunity=candidate,
                pricing_result=None,
                status=STATUS_REJECTED_LEG_SKEW,
                rejection_reason=skew_reason,
                fee_per_contract_cents=self.config.default_fee_per_contract_cents,
                is_qualified=False,
            )

        # V3 Execution Pricing
        pricing = evaluate_mece_basket_execution(
            event=event,
            books_by_outcome=books_by_outcome,
            basket_side=basket.basket_side,
            requested_quantity=self.config.target_quantity,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
        )

        is_qualified = False
        status = pricing.status
        rejection_reason = pricing.rejection_reason

        if pricing.supported_quantity == 0:
            status = STATUS_REJECTED_INSUFFICIENT_DEPTH
            rejection_reason = "Order books have insufficient depth for common basket execution"
        elif not pricing.is_gross_profitable:
            status = STATUS_REJECTED_NO_GROSS_EDGE
            rejection_reason = "No gross edge across basket order book depth"
        elif self.config.enforce_net_profitability and pricing.is_net_profitable is False:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Unprofitable after estimated fees"
        elif self.config.enforce_net_profitability and pricing.is_net_profitable is None:
            status = STATUS_REJECTED_UNPROFITABLE_FEES
            rejection_reason = "Net profitability cannot be verified without fee configuration"
        else:
            status = STATUS_QUALIFIED
            is_qualified = True
            rejection_reason = None

        return EvaluatedOpportunity(
            observation_id=generate_observation_id(),
            opportunity_id=opp_id,
            strategy_type=strat_type,
            observed_at=now,
            source_timestamps=source_ts_map,
            leg_timestamp_skew_seconds=skew_sec,
            market_tickers=basket.ordered_tickers,
            candidate_opportunity=candidate,
            pricing_result=pricing,
            status=status,
            rejection_reason=rejection_reason,
            fee_per_contract_cents=self.config.default_fee_per_contract_cents,
            is_qualified=is_qualified,
        )

    def _submit_paper_trade(
        self,
        opp: EvaluatedOpportunity,
    ) -> Optional[PaperTrade]:
        """Submit a qualified opportunity to V4 paper execution with pre-trade risk checks."""
        if not self.config.enable_paper_trading or self.paper_executor is None:
            return None
        if self.paper_portfolio is None or opp.pricing_result is None:
            return None

        # Build trade via PaperExecutionEngine
        trade = self.paper_executor.execute_from_pricing_result(
            opp.pricing_result,
            timestamp=opp.observed_at.isoformat(),
            contract_ids=opp.market_tickers,
            enforce_net_profitability=self.config.enforce_net_profitability,
            portfolio=self.paper_portfolio,
            risk_manager=self.risk_manager,
        )

        # If accepted/filled, apply to portfolio
        if trade.state in (TradeState.FILLED, TradeState.PARTIALLY_FILLED):
            self.paper_portfolio.reserve_for_trade(trade, timestamp=trade.evaluated_at)
            self.paper_portfolio.apply_trade_fill(trade, timestamp=trade.evaluated_at)

        self._paper_trades_history.append(trade)
        return trade

    def run_cycle(
        self,
    ) -> Tuple[Tuple[MarketObservation, ...], Tuple[EvaluatedOpportunity, ...]]:
        """Run one full market observation and opportunity evaluation cycle."""
        self._cycle_count += 1
        cycle_label = f"Cycle {self._cycle_count}"
        if self.config.max_cycles:
            cycle_label += f"/{self.config.max_cycles}"
        cycle_start = self._now()
        cycle_t0 = time.monotonic()

        # 1. Determine targets to observe
        tickers_to_poll: Set[str] = set()
        if self.target_tickers:
            tickers_to_poll.update(self.target_tickers)
        for basket in self.declared_baskets:
            tickers_to_poll.update(basket.market_outcome_map.keys())

        if not tickers_to_poll:
            refresh_due = (
                self.config.discovery_refresh_interval_cycles is not None
                and self._discovered_tickers is not None
                and (self._cycle_count - 1) > 0
                and (self._cycle_count - 1) % self.config.discovery_refresh_interval_cycles == 0
            )
            if refresh_due:
                self._report_progress(
                    f"[{cycle_label}] Refreshing open market discovery cache..."
                )
            discovered = self.discover_markets(force_refresh=refresh_due)
            tickers_to_poll.update(discovered)

        # Apply cycle polling limit if configured
        target_list = sorted(tickers_to_poll)
        if (
            self.config.max_poll_markets_per_cycle is not None
            and len(target_list) > self.config.max_poll_markets_per_cycle
        ):
            self._report_progress(
                f"Bounding cycle polling to {self.config.max_poll_markets_per_cycle} markets "
                f"(out of {len(target_list)} available)."
            )
            target_list = target_list[: self.config.max_poll_markets_per_cycle]

        self._report_progress(f"[{cycle_label}] Polling {len(target_list)} markets...")

        # 2. Poll all target markets
        observations_by_ticker: Dict[str, MarketObservation] = {}
        for i, ticker in enumerate(target_list, 1):
            if self._stop_requested:
                break
            if len(target_list) > 1 and (i == 1 or i % 10 == 0 or i == len(target_list)):
                self._report_progress(f"[{cycle_label}] Polling market {i}/{len(target_list)}: {ticker}...")
            obs = self.poll_market(ticker)
            observations_by_ticker[ticker] = obs
            self._observations_history.append(obs)

        # 3. Detect and evaluate candidate opportunities
        evaluated_in_cycle: List[EvaluatedOpportunity] = []

        # (a) Single-market parity
        for ticker, obs in observations_by_ticker.items():
            if self._stop_requested:
                break
            opp = self.evaluate_binary_parity_candidate(obs)
            if opp is not None:
                evaluated_in_cycle.append(opp)

        # (b) Declared MECE baskets
        for basket in self.declared_baskets:
            if self._stop_requested:
                break
            opp = self.evaluate_mece_basket_candidate(basket, observations_by_ticker)
            if opp is not None:
                evaluated_in_cycle.append(opp)

        # 4. Deduplicate, track lifetimes, and handle paper execution
        current_opp_ids = {opp.opportunity_id for opp in evaluated_in_cycle}
        final_evaluated: List[EvaluatedOpportunity] = []

        for opp in evaluated_in_cycle:
            opp_id = opp.opportunity_id
            is_recurrent = opp_id in self._active_opportunities

            if opp_id not in self._opportunity_first_seen:
                self._opportunity_first_seen[opp_id] = opp.observed_at
            self._opportunity_last_seen[opp_id] = opp.observed_at

            paper_trade_id = None

            # Paper execution integration
            if self.config.enable_paper_trading and opp.is_qualified:
                # Deduplication: do not open duplicate trade if already submitted
                if opp_id not in self._submitted_paper_opportunities:
                    trade = self._submit_paper_trade(opp)
                    if trade is not None:
                        paper_trade_id = trade.trade_id
                        self._submitted_paper_opportunities.add(opp_id)

            updated_opp = EvaluatedOpportunity(
                observation_id=opp.observation_id,
                opportunity_id=opp.opportunity_id,
                strategy_type=opp.strategy_type,
                observed_at=opp.observed_at,
                source_timestamps=opp.source_timestamps,
                leg_timestamp_skew_seconds=opp.leg_timestamp_skew_seconds,
                market_tickers=opp.market_tickers,
                candidate_opportunity=opp.candidate_opportunity,
                pricing_result=opp.pricing_result,
                status=opp.status,
                rejection_reason=opp.rejection_reason,
                fee_per_contract_cents=opp.fee_per_contract_cents,
                is_qualified=opp.is_qualified,
                is_recurrent=is_recurrent,
                paper_trade_id=paper_trade_id,
            )

            final_evaluated.append(updated_opp)
            self._active_opportunities[opp_id] = updated_opp
            self._evaluations_history.append(updated_opp)

            if self.evidence_store is not None:
                self.evidence_store.record_opportunity(updated_opp)

        # Check for disappeared opportunities
        disappeared_ids = set(self._active_opportunities.keys()) - current_opp_ids
        for dis_id in disappeared_ids:
            first_seen = self._opportunity_first_seen.pop(dis_id, None)
            last_seen = self._opportunity_last_seen.pop(dis_id, None)
            if first_seen and last_seen:
                lifetime = (last_seen - first_seen).total_seconds()
                self._opportunity_lifetimes.append(lifetime)
            del self._active_opportunities[dis_id]
            self._submitted_paper_opportunities.discard(dis_id)

        cycle_elapsed = time.monotonic() - cycle_t0
        qualified_count = sum(1 for o in final_evaluated if o.is_qualified)
        self._report_progress(
            f"[{cycle_label}] Complete in {cycle_elapsed:.1f}s: {len(observations_by_ticker)} markets polled, "
            f"{len(final_evaluated)} candidates ({qualified_count} qualified)."
        )

        return tuple(observations_by_ticker.values()), tuple(final_evaluated)

    def run(self, max_cycles: Optional[int] = None) -> None:
        """Run the continuous observation loop until stopped or max_cycles reached."""
        limit = max_cycles or self.config.max_cycles
        cycles_completed = 0

        try:
            while not self._stop_requested:
                if limit is not None and cycles_completed >= limit:
                    break

                self.run_cycle()
                cycles_completed += 1

                if limit is not None and cycles_completed >= limit:
                    break

                if not self._stop_requested and self.config.poll_interval_seconds > 0:
                    self._sleep_with_stop_check(self.config.poll_interval_seconds)
        except KeyboardInterrupt:
            self.stop()
            self._report_progress("\n[KeyboardInterrupt] Observer stopped by user.")


    def get_metrics(self) -> Any:
        """Compute structured session metrics across observations and evaluations."""
        from .metrics import compute_observer_metrics

        return compute_observer_metrics(
            self._observations_history,
            self._evaluations_history,
            paper_trades=self._paper_trades_history,
            lifetimes=self._opportunity_lifetimes,
        )

    def get_report(self) -> Any:
        """Generate a complete evaluation report with portfolio equity if paper trading."""
        from .metrics import ObserverReport

        metrics = self.get_metrics()
        equity_cents = (
            self.paper_portfolio.book_value_equity_cents()
            if self.paper_portfolio is not None
            else None
        )
        cash_cents = (
            self.paper_portfolio.available_cash_cents
            if self.paper_portfolio is not None
            else None
        )
        return ObserverReport(
            metrics=metrics,
            observations_count=len(self._observations_history),
            evaluations_count=len(self._evaluations_history),
            paper_portfolio_equity_cents=equity_cents,
            paper_portfolio_cash_cents=cash_cents,
        )

