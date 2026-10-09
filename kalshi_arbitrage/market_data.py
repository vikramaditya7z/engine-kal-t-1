"""Validated, network-independent models for normalized Kalshi market data.

Prices remain exact Decimal dollar values in this layer. Conversion to the V0
integer-cent model is permitted only when multiplying by 100 produces an
integer in the 0..100 range; this module deliberately does not perform that
conversion or round sub-cent values.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence, Tuple


class MarketDataInputError(ValueError):
    """Raised when raw or normalized market-data input is invalid."""


_BINARY_MARKET_TYPE = "binary"
_ZERO = Decimal("0")
_ONE = Decimal("1")


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketDataInputError(f"{name} must be a mapping")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MarketDataInputError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: object, name: str) -> Optional[str]:
    if value is None:
        return None
    return _required_text(value, name)


def _optional_text_allow_empty(value: object, name: str) -> Optional[str]:
    """Treat an API-provided empty text field as unspecified metadata."""
    if value == "":
        return None
    return _optional_text(value, name)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise MarketDataInputError(f"{name} must be an exact decimal value")
    if not isinstance(value, (str, int, Decimal)):
        raise MarketDataInputError(f"{name} must be an exact decimal value")
    if isinstance(value, str) and not value.strip():
        raise MarketDataInputError(f"{name} must be an exact decimal value")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataInputError(f"{name} must be an exact decimal value") from exc
    if not parsed.is_finite():
        raise MarketDataInputError(f"{name} must be finite")
    return parsed


def _optional_decimal(
    value: object,
    name: str,
    minimum: Decimal = _ZERO,
    maximum: Optional[Decimal] = None,
    strictly_positive: bool = False,
) -> Optional[Decimal]:
    if value is None:
        return None
    parsed = _decimal(value, name)
    if strictly_positive and parsed <= minimum:
        raise MarketDataInputError(f"{name} must be greater than {minimum}")
    if not strictly_positive and parsed < minimum:
        raise MarketDataInputError(f"{name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise MarketDataInputError(f"{name} must be at most {maximum}")
    return parsed


def parse_price_dollars(value: object, name: str = "price_dollars") -> Decimal:
    """Parse a finite exact dollar price in the inclusive 0.00–1.00 range."""
    parsed = _optional_decimal(value, name, maximum=_ONE)
    if parsed is None:
        raise MarketDataInputError(f"{name} is required")
    return parsed


def parse_quantity(value: object, name: str = "quantity", allow_zero: bool = True) -> Decimal:
    """Parse a finite exact non-negative or positive contract quantity."""
    parsed = _optional_decimal(
        value,
        name,
        strictly_positive=not allow_zero,
    )
    if parsed is None:
        raise MarketDataInputError(f"{name} is required")
    return parsed


def _optional_timestamp(value: object, name: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise MarketDataInputError(f"{name} must be an ISO-8601 timestamp or null")
    for format_string in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(value, format_string)
        except ValueError:
            continue
    raise MarketDataInputError(f"{name} must be an ISO-8601 timestamp with a timezone or null")


def _optional_bool(value: object, name: str) -> Optional[bool]:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise MarketDataInputError(f"{name} must be a boolean or null")
    return value


@dataclass(frozen=True)
class PriceRange:
    """One valid fixed-point price band from a market response."""

    start: Decimal
    end: Decimal
    step: Decimal

    def __post_init__(self) -> None:
        start = parse_price_dollars(self.start, "price range start")
        end = parse_price_dollars(self.end, "price range end")
        step = _optional_decimal(self.step, "price range step", strictly_positive=True)
        if step is None:
            raise MarketDataInputError("price range step is required")
        if start > end:
            raise MarketDataInputError("price range start must not exceed end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "step", step)


@dataclass(frozen=True)
class OrderBookLevel:
    """An exact price/quantity pair for one YES or NO bid level."""

    price_dollars: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price_dollars", parse_price_dollars(self.price_dollars))
        object.__setattr__(
            self,
            "quantity",
            parse_quantity(self.quantity, "order-book quantity", allow_zero=False),
        )


@dataclass(frozen=True)
class NormalizedOrderBook:
    """Normalized bids for a single binary market.

    source_timestamp represents the transport-level exchange timestamp (derived from
    the HTTP Date response header). Limitation: HTTP Date establishes response time,
    not necessarily the exact internal snapshot-generation time or proof that an
    upstream cache was bypassed.
    """

    market_ticker: str
    yes_bids: Tuple[OrderBookLevel, ...]
    no_bids: Tuple[OrderBookLevel, ...]
    source_timestamp: Optional[datetime] = None

    def __post_init__(self) -> None:
        _required_text(self.market_ticker, "market_ticker")
        for name, levels in (("yes_bids", self.yes_bids), ("no_bids", self.no_bids)):
            if not isinstance(levels, tuple):
                raise MarketDataInputError(f"{name} must be a tuple")
            if any(not isinstance(level, OrderBookLevel) for level in levels):
                raise MarketDataInputError(f"{name} must contain OrderBookLevel objects")
            if any(left.price_dollars > right.price_dollars for left, right in zip(levels, levels[1:])):
                raise MarketDataInputError(f"{name} must be sorted by ascending price")
        if self.source_timestamp is not None:
            if not isinstance(self.source_timestamp, datetime):
                raise MarketDataInputError("source_timestamp must be a datetime or null")
            if self.source_timestamp.tzinfo is None:
                raise MarketDataInputError("source_timestamp must include a timezone")


@dataclass(frozen=True)
class SettlementSource:
    name: str
    url: str

    def __post_init__(self) -> None:
        _required_text(self.name, "settlement source name")
        _required_text(self.url, "settlement source url")


@dataclass(frozen=True)
class NormalizedEvent:
    """Event identity and metadata; no exhaustiveness claim is made."""

    event_ticker: str
    series_ticker: str
    title: str
    subtitle: Optional[str]
    mutually_exclusive: bool
    market_tickers: Tuple[str, ...]
    settlement_sources: Tuple[SettlementSource, ...]
    category: Optional[str] = None
    strike_date: Optional[datetime] = None
    strike_period: Optional[str] = None
    collateral_return_type: Optional[str] = None

    def __post_init__(self) -> None:
        _required_text(self.event_ticker, "event_ticker")
        _required_text(self.series_ticker, "series_ticker")
        _required_text(self.title, "event title")
        if self.subtitle is not None:
            _required_text(self.subtitle, "event subtitle")
        if not isinstance(self.mutually_exclusive, bool):
            raise MarketDataInputError("mutually_exclusive must be a boolean")
        if not isinstance(self.market_tickers, tuple):
            raise MarketDataInputError("market_tickers must be a tuple")
        if any(not isinstance(ticker, str) or not ticker for ticker in self.market_tickers):
            raise MarketDataInputError("market_tickers must contain non-empty strings")
        if len(set(self.market_tickers)) != len(self.market_tickers):
            raise MarketDataInputError("market_tickers must contain unique values")
        if not isinstance(self.settlement_sources, tuple) or any(
            not isinstance(source, SettlementSource) for source in self.settlement_sources
        ):
            raise MarketDataInputError(
                "settlement_sources must contain SettlementSource objects"
            )
        if self.category is not None:
            _required_text(self.category, "event category")
        if self.strike_date is not None:
            if not isinstance(self.strike_date, datetime):
                raise MarketDataInputError("strike_date must be a datetime or null")
            if self.strike_date.tzinfo is None:
                raise MarketDataInputError("strike_date must include a timezone")
        if self.strike_period is not None:
            _required_text(self.strike_period, "strike_period")
        if self.collateral_return_type is not None:
            _required_text(self.collateral_return_type, "collateral_return_type")


@dataclass(frozen=True)
class NormalizedMarket:
    """Normalized binary market data independent of the raw API schema."""

    ticker: str
    event_ticker: str
    market_type: str
    status: str
    title: str
    subtitle: Optional[str] = None
    yes_subtitle: Optional[str] = None
    no_subtitle: Optional[str] = None
    rules_primary: Optional[str] = None
    rules_secondary: Optional[str] = None
    expiration_value: Optional[str] = None
    result: Optional[str] = None
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    settlement_ts: Optional[datetime] = None
    yes_bid_dollars: Optional[Decimal] = None
    yes_ask_dollars: Optional[Decimal] = None
    no_bid_dollars: Optional[Decimal] = None
    no_ask_dollars: Optional[Decimal] = None
    last_price_dollars: Optional[Decimal] = None
    settlement_value_dollars: Optional[Decimal] = None
    yes_bid_size: Optional[Decimal] = None
    yes_ask_size: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    volume_24h: Optional[Decimal] = None
    open_interest: Optional[Decimal] = None
    price_ranges: Tuple[PriceRange, ...] = ()
    is_provisional: Optional[bool] = None

    def __post_init__(self) -> None:
        _required_text(self.ticker, "ticker")
        _required_text(self.event_ticker, "event_ticker")
        if self.market_type != _BINARY_MARKET_TYPE:
            raise MarketDataInputError("only binary markets are supported")
        _required_text(self.status, "status")
        _required_text(self.title, "market title")
        for name, value in (
            ("subtitle", self.subtitle),
            ("yes_subtitle", self.yes_subtitle),
            ("no_subtitle", self.no_subtitle),
            ("rules_primary", self.rules_primary),
            ("rules_secondary", self.rules_secondary),
            ("expiration_value", self.expiration_value),
            ("result", self.result),
        ):
            if value is not None:
                _required_text(value, name)
        for name, value in (
            ("yes_bid_dollars", self.yes_bid_dollars),
            ("yes_ask_dollars", self.yes_ask_dollars),
            ("no_bid_dollars", self.no_bid_dollars),
            ("no_ask_dollars", self.no_ask_dollars),
            ("last_price_dollars", self.last_price_dollars),
            ("settlement_value_dollars", self.settlement_value_dollars),
        ):
            _optional_decimal(value, name, maximum=_ONE)
        for name, value in (
            ("yes_bid_size", self.yes_bid_size),
            ("yes_ask_size", self.yes_ask_size),
            ("volume", self.volume),
            ("volume_24h", self.volume_24h),
            ("open_interest", self.open_interest),
        ):
            _optional_decimal(value, name)
        for name, value in (
            ("created_time", self.created_time),
            ("updated_time", self.updated_time),
            ("open_time", self.open_time),
            ("close_time", self.close_time),
            ("expiration_time", self.expiration_time),
            ("settlement_ts", self.settlement_ts),
        ):
            if value is not None:
                if not isinstance(value, datetime):
                    raise MarketDataInputError(f"{name} must be a datetime or null")
                if value.tzinfo is None:
                    raise MarketDataInputError(f"{name} must include a timezone")
        if not isinstance(self.price_ranges, tuple) or any(
            not isinstance(price_range, PriceRange) for price_range in self.price_ranges
        ):
            raise MarketDataInputError("price_ranges must contain PriceRange objects")
        _optional_bool(self.is_provisional, "is_provisional")


def _optional_market_decimal(
    payload: Mapping[str, Any],
    key: str,
    parser: str,
) -> Optional[Decimal]:
    value = payload.get(key)
    if value is None:
        return None
    if parser == "price":
        return parse_price_dollars(value, key)
    return parse_quantity(value, key)


def _normalize_price_ranges(value: object) -> Tuple[PriceRange, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MarketDataInputError("price_ranges must be a sequence")
    ranges = []
    for item in value:
        item = _require_mapping(item, "price range")
        ranges.append(
            PriceRange(
                parse_price_dollars(item.get("start"), "price range start"),
                parse_price_dollars(item.get("end"), "price range end"),
                _optional_decimal(item.get("step"), "price range step", strictly_positive=True),
            )
        )
    return tuple(ranges)


def normalize_market(payload: object) -> NormalizedMarket:
    """Validate a raw market mapping and return a normalized model."""
    raw = _require_mapping(payload, "market payload")
    market = raw.get("market", raw)
    market = _require_mapping(market, "market")
    market_type = _required_text(market.get("market_type"), "market_type")
    if market_type != _BINARY_MARKET_TYPE:
        raise MarketDataInputError(f"unsupported market_type: {market_type}")
    return NormalizedMarket(
        ticker=_required_text(market.get("ticker"), "ticker"),
        event_ticker=_required_text(market.get("event_ticker"), "event_ticker"),
        market_type=market_type,
        status=_required_text(market.get("status"), "status"),
        title=_required_text(market.get("title"), "market title"),
        subtitle=_optional_text(market.get("subtitle"), "subtitle"),
        yes_subtitle=_optional_text(market.get("yes_sub_title"), "yes_sub_title"),
        no_subtitle=_optional_text(market.get("no_sub_title"), "no_sub_title"),
        rules_primary=_optional_text_allow_empty(
            market.get("rules_primary"), "rules_primary"
        ),
        rules_secondary=_optional_text(market.get("rules_secondary"), "rules_secondary"),
        expiration_value=_optional_text_allow_empty(
            market.get("expiration_value"), "expiration_value"
        ),
        result=_optional_text_allow_empty(market.get("result"), "result"),
        created_time=_optional_timestamp(market.get("created_time"), "created_time"),
        updated_time=_optional_timestamp(market.get("updated_time"), "updated_time"),
        open_time=_optional_timestamp(market.get("open_time"), "open_time"),
        close_time=_optional_timestamp(market.get("close_time"), "close_time"),
        expiration_time=_optional_timestamp(market.get("expiration_time"), "expiration_time"),
        settlement_ts=_optional_timestamp(market.get("settlement_ts"), "settlement_ts"),
        yes_bid_dollars=_optional_market_decimal(market, "yes_bid_dollars", "price"),
        yes_ask_dollars=_optional_market_decimal(market, "yes_ask_dollars", "price"),
        no_bid_dollars=_optional_market_decimal(market, "no_bid_dollars", "price"),
        no_ask_dollars=_optional_market_decimal(market, "no_ask_dollars", "price"),
        last_price_dollars=_optional_market_decimal(market, "last_price_dollars", "price"),
        settlement_value_dollars=_optional_market_decimal(
            market, "settlement_value_dollars", "price"
        ),
        yes_bid_size=_optional_market_decimal(market, "yes_bid_size_fp", "quantity"),
        yes_ask_size=_optional_market_decimal(market, "yes_ask_size_fp", "quantity"),
        volume=_optional_market_decimal(market, "volume_fp", "quantity"),
        volume_24h=_optional_market_decimal(market, "volume_24h_fp", "quantity"),
        open_interest=_optional_market_decimal(market, "open_interest_fp", "quantity"),
        price_ranges=_normalize_price_ranges(market.get("price_ranges")),
        is_provisional=_optional_bool(market.get("is_provisional"), "is_provisional"),
    )


def _normalize_settlement_sources(value: object) -> Tuple[SettlementSource, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MarketDataInputError("settlement_sources must be a sequence")
    sources = []
    for item in value:
        item = _require_mapping(item, "settlement source")
        sources.append(
            SettlementSource(
                _required_text(item.get("name"), "settlement source name"),
                _required_text(item.get("url"), "settlement source url"),
            )
        )
    return tuple(sources)


def normalize_event(payload: object) -> NormalizedEvent:
    """Validate a raw event response and preserve its relationship metadata."""
    raw = _require_mapping(payload, "event payload")
    event = _require_mapping(raw.get("event", raw), "event")
    nested_markets = event.get("markets")
    if nested_markets is None:
        nested_markets = raw.get("markets")
    market_tickers = ()
    if nested_markets is not None:
        if not isinstance(nested_markets, Sequence) or isinstance(nested_markets, (str, bytes)):
            raise MarketDataInputError("event markets must be a sequence")
        market_tickers = tuple(
            _required_text(_require_mapping(item, "event market").get("ticker"), "market ticker")
            for item in nested_markets
        )
        if len(set(market_tickers)) != len(market_tickers):
            raise MarketDataInputError("event market tickers must be unique")
    return NormalizedEvent(
        event_ticker=_required_text(event.get("event_ticker"), "event_ticker"),
        series_ticker=_required_text(event.get("series_ticker"), "series_ticker"),
        title=_required_text(event.get("title"), "event title"),
        subtitle=_optional_text(event.get("sub_title"), "sub_title"),
        mutually_exclusive=event.get("mutually_exclusive"),
        market_tickers=market_tickers,
        settlement_sources=_normalize_settlement_sources(event.get("settlement_sources")),
        category=_optional_text(event.get("category"), "category"),
        strike_date=_optional_timestamp(event.get("strike_date"), "strike_date"),
        strike_period=_optional_text(event.get("strike_period"), "strike_period"),
        collateral_return_type=_optional_text(
            event.get("collateral_return_type"), "collateral_return_type"
        ),
    )


def _normalize_orderbook_levels(value: object, name: str) -> Tuple[OrderBookLevel, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MarketDataInputError(f"{name} must be a sequence")
    levels = []
    for item in value:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise MarketDataInputError(f"{name} levels must be [price, quantity] pairs")
        levels.append(
            OrderBookLevel(
                parse_price_dollars(item[0], f"{name} price"),
                parse_quantity(item[1], f"{name} quantity", allow_zero=False),
            )
        )
    return tuple(levels)


def normalize_order_book(
    payload: object,
    market_ticker: str,
    source_timestamp: Optional[datetime] = None,
) -> NormalizedOrderBook:
    """Validate a raw order-book response and return exact bid levels."""
    raw = _require_mapping(payload, "order-book payload")
    orderbook = _require_mapping(raw.get("orderbook_fp"), "orderbook_fp")
    return NormalizedOrderBook(
        market_ticker=_required_text(market_ticker, "market_ticker"),
        yes_bids=_normalize_orderbook_levels(orderbook.get("yes_dollars"), "yes_dollars"),
        no_bids=_normalize_orderbook_levels(orderbook.get("no_dollars"), "no_dollars"),
        source_timestamp=source_timestamp,
    )
