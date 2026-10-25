"""Kalshi Arbitrage Engine V16 — Read-Only WebSocket Streaming & Order Book Synchronization.

Provides a robust, deterministic, read-only streaming client and local L2 order-book
synchronizer based on official Kalshi WebSocket v2 protocol specifications.

Governing Principles:
1. Strict Read-Only Safety: Exchange interaction is strictly read-only. Order placement,
   amendment, cancellation, or private execution channels are prohibited and fail closed.
2. Canonical L2 Delta Semantics: Adheres to Kalshi orderbook_delta protocol where 'delta'
   represents the absolute resting quantity at a price level (delta > 0 overwrites level,
   delta == 0 removes level).
3. Sequence Continuity & Invalidation: Tracks stream sequence numbers ('seq'). Any sequence
   gap or out-of-order message immediately invalidates the book and triggers resynchronization.
4. Transport & Evaluator Separation: Streaming transport and synchronization are fully
   decoupled from financial arbitrage evaluation.
5. Zero Private Credentials in Logs or State: Never requests, stores, or logs private keys.
"""

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import logging
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

from .contract import NO, YES
from .market_data import (
    MarketDataInputError,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    PriceRange,
)
from .observer import (
    MarketObservation,
    ObserverError,
)

logger = logging.getLogger(__name__)

# Protocol Constants
WS_PRODUCTION_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_DEMO_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

CHANNEL_ORDERBOOK_DELTA = "orderbook_delta"
CHANNEL_TICKER = "ticker"
CHANNEL_TRADE = "trade"

PUBLIC_CHANNELS = frozenset({CHANNEL_ORDERBOOK_DELTA, CHANNEL_TICKER, CHANNEL_TRADE})
PROHIBITED_PRIVATE_CHANNELS = frozenset({
    "fill",
    "market_positions",
    "communications",
    "order_group_updates",
    "orders",
    "balance",
})

# Synchronization States
SYNC_STATE_UNSYNCHRONIZED = "UNSYNCHRONIZED"
SYNC_STATE_SYNCHRONIZED = "SYNCHRONIZED"
SYNC_STATE_RESYNCING = "RESYNCING"
SYNC_STATE_INVALIDATED = "INVALIDATED"


class StreamingSafetyError(Exception):
    """Raised when an operation violates read-only or security invariants."""


class StreamingProtocolError(Exception):
    """Raised when a WebSocket message violates Kalshi protocol semantics."""


class SequenceGapError(StreamingProtocolError):
    """Raised when an unrecoverable sequence gap is detected in the stream."""


def _parse_timestamp_ms(val: Optional[Union[int, float]]) -> Optional[datetime]:
    """Parse millisecond Unix timestamp to UTC datetime."""
    if val is None:
        return None
    try:
        ts_sec = float(val) / 1000.0
        return datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _parse_decimal_price(val: Any) -> Decimal:
    """Safely convert price string/float/int to Decimal."""
    if isinstance(val, (int, str)):
        return Decimal(str(val))
    if isinstance(val, float):
        return Decimal(str(round(val, 4)))
    if isinstance(val, Decimal):
        return val
    raise MarketDataInputError(f"Invalid price value: {val!r}")


def _parse_decimal_qty(val: Any) -> Decimal:
    """Safely convert quantity to Decimal."""
    if isinstance(val, (int, str)):
        return Decimal(str(val))
    if isinstance(val, float):
        return Decimal(str(round(val, 2)))
    if isinstance(val, Decimal):
        return val
    raise MarketDataInputError(f"Invalid quantity value: {val!r}")


@dataclass(frozen=True)
class OrderBookSnapshotMsg:
    """Initial order book snapshot emitted upon subscription."""

    market_ticker: str
    seq: int
    sid: int
    sending_ts_ms: Optional[int]
    yes_bids: Tuple[Tuple[Decimal, Decimal], ...]  # ((price, qty), ...)
    no_bids: Tuple[Tuple[Decimal, Decimal], ...]
    raw_payload: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class OrderBookDeltaMsg:
    """Incremental delta update for a specific price level."""

    market_ticker: str
    seq: int
    sid: int
    price: Decimal
    delta_quantity: Decimal  # Absolute quantity (0 = remove level)
    side: str  # "yes" or "no"
    sending_ts_ms: Optional[int]
    ts_ms: Optional[int]
    raw_payload: Optional[Dict[str, Any]] = None


class LocalL2OrderBook:
    """In-memory local copy of an L2 order book maintained via snapshot and delta stream."""

    def __init__(self, market_ticker: str) -> None:
        self.market_ticker = market_ticker
        self._yes_bids: Dict[Decimal, Decimal] = {}  # price -> qty
        self._no_bids: Dict[Decimal, Decimal] = {}   # price -> qty
        self.last_seq: Optional[int] = None
        self.last_update_ts: Optional[datetime] = None
        self.is_initialized: bool = False

    def apply_snapshot(self, snapshot: OrderBookSnapshotMsg) -> None:
        """Initialize or reset book state from a full snapshot."""
        if snapshot.market_ticker != self.market_ticker:
            raise StreamingProtocolError(
                f"Snapshot ticker {snapshot.market_ticker} does not match {self.market_ticker}"
            )
        self._yes_bids.clear()
        self._no_bids.clear()

        for price, qty in snapshot.yes_bids:
            if qty > Decimal("0"):
                self._yes_bids[price] = qty

        for price, qty in snapshot.no_bids:
            if qty > Decimal("0"):
                self._no_bids[price] = qty

        self.last_seq = snapshot.seq
        self.last_update_ts = _parse_timestamp_ms(snapshot.sending_ts_ms)
        self.is_initialized = True

    def apply_delta(self, delta: OrderBookDeltaMsg) -> None:
        """Apply an incremental delta update (absolute quantity at price level)."""
        if not self.is_initialized:
            raise StreamingProtocolError("Cannot apply delta to uninitialized order book")
        if delta.market_ticker != self.market_ticker:
            raise StreamingProtocolError(
                f"Delta ticker {delta.market_ticker} does not match {self.market_ticker}"
            )

        target_side = self._yes_bids if delta.side.lower() == YES.lower() else self._no_bids
        if delta.delta_quantity <= Decimal("0"):
            target_side.pop(delta.price, None)
        else:
            target_side[delta.price] = delta.delta_quantity

        self.last_seq = delta.seq
        self.last_update_ts = _parse_timestamp_ms(delta.ts_ms or delta.sending_ts_ms)

    def to_normalized_order_book(
        self,
        source_timestamp: Optional[datetime] = None,
    ) -> NormalizedOrderBook:
        """Export local state to canonical NormalizedOrderBook with bids sorted descending."""
        if not self.is_initialized:
            raise StreamingProtocolError("Cannot export uninitialized order book")

        # Sort price ascending as required by NormalizedOrderBook
        yes_levels = tuple(
            OrderBookLevel(price_dollars=p, quantity=q)
            for p, q in sorted(self._yes_bids.items(), key=lambda x: x[0], reverse=False)
        )
        no_levels = tuple(
            OrderBookLevel(price_dollars=p, quantity=q)
            for p, q in sorted(self._no_bids.items(), key=lambda x: x[0], reverse=False)
        )

        return NormalizedOrderBook(
            market_ticker=self.market_ticker,
            yes_bids=yes_levels,
            no_bids=no_levels,
            source_timestamp=source_timestamp or self.last_update_ts,
        )

    def clear(self) -> None:
        """Clear book state upon invalidation."""
        self._yes_bids.clear()
        self._no_bids.clear()
        self.last_seq = None
        self.last_update_ts = None
        self.is_initialized = False


@dataclass
class MarketStreamState:
    """Internal synchronization state for an active subscription."""

    ticker: str
    channel: str
    sid: Optional[int] = None
    expected_seq: Optional[int] = None
    sync_state: str = SYNC_STATE_UNSYNCHRONIZED
    local_book: LocalL2OrderBook = field(default_factory=lambda: LocalL2OrderBook(""))
    last_received_at: Optional[datetime] = None
    total_messages_received: int = 0
    duplicate_count: int = 0
    gap_count: int = 0
    resync_count: int = 0
    data_quality_flags: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.local_book.market_ticker:
            self.local_book = LocalL2OrderBook(self.ticker)


class OrderBookStreamSynchronizer:
    """Validates sequence continuity, applies book updates, and emits observations."""

    def __init__(self, session_id: Optional[str] = None) -> None:
        self.session_id = session_id or f"ws-session-{int(datetime.now(timezone.utc).timestamp())}"
        self._states: Dict[str, MarketStreamState] = {}
        self._resync_callbacks: List[Callable[[str], None]] = []

    def register_resync_callback(self, cb: Callable[[str], None]) -> None:
        """Register a callback invoked when a market requires snapshot resync."""
        self._resync_callbacks.append(cb)

    def subscribe_market(self, ticker: str, channel: str = CHANNEL_ORDERBOOK_DELTA) -> None:
        """Register a subscription for a market ticker with read-only validation."""
        if channel in PROHIBITED_PRIVATE_CHANNELS:
            raise StreamingSafetyError(f"Prohibited private channel {channel!r} cannot be subscribed")
        if channel not in PUBLIC_CHANNELS:
            raise StreamingProtocolError(f"Unsupported channel {channel!r}")

        if ticker not in self._states:
            self._states[ticker] = MarketStreamState(
                ticker=ticker,
                channel=channel,
                sync_state=SYNC_STATE_UNSYNCHRONIZED,
            )

    def is_synchronized(self, ticker: str) -> bool:
        """Return True if the market book is initialized and synchronized."""
        state = self._states.get(ticker)
        return bool(state and state.sync_state == SYNC_STATE_SYNCHRONIZED and state.local_book.is_initialized)

    def get_sync_state(self, ticker: str) -> str:
        state = self._states.get(ticker)
        return state.sync_state if state else SYNC_STATE_UNSYNCHRONIZED

    def process_message(
        self,
        payload: Mapping[str, Any],
        local_receive_time: Optional[datetime] = None,
    ) -> Optional[MarketObservation]:
        """Ingest, validate, and apply an inbound WebSocket frame."""
        now = local_receive_time or datetime.now(timezone.utc)
        msg_type = payload.get("type")

        # Heartbeat / ping handling
        if msg_type in ("heartbeat", "pong", "ping"):
            return None

        # Protocol error response
        if msg_type == "error":
            logger.warning("Kalshi stream error: %s", payload)
            return None

        if msg_type == "orderbook_snapshot":
            return self._handle_snapshot(payload, now)
        elif msg_type == "orderbook_delta":
            return self._handle_delta(payload, now)
        else:
            logger.debug("Ignoring unhandled stream message type: %s", msg_type)
            return None

    def _handle_snapshot(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> MarketObservation:
        msg = payload.get("msg", {})
        ticker = msg.get("market_ticker")
        if not ticker or ticker not in self._states:
            raise StreamingProtocolError(f"Snapshot for unknown or unregistered ticker: {ticker}")

        state = self._states[ticker]
        state.total_messages_received += 1
        state.last_received_at = received_at
        state.sid = payload.get("sid")
        seq = payload.get("seq", 0)

        # Parse snapshot bids
        yes_raw = msg.get("yes", [])
        no_raw = msg.get("no", [])

        yes_bids = tuple(
            (_parse_decimal_price(item[0]), _parse_decimal_qty(item[1]))
            for item in yes_raw
        )
        no_bids = tuple(
            (_parse_decimal_price(item[0]), _parse_decimal_qty(item[1]))
            for item in no_raw
        )

        snap = OrderBookSnapshotMsg(
            market_ticker=ticker,
            seq=seq,
            sid=state.sid or 0,
            sending_ts_ms=payload.get("sending_ts_ms"),
            yes_bids=yes_bids,
            no_bids=no_bids,
            raw_payload=dict(payload),
        )

        state.local_book.apply_snapshot(snap)
        state.expected_seq = seq + 1
        state.sync_state = SYNC_STATE_SYNCHRONIZED
        state.data_quality_flags.clear()

        source_ts = _parse_timestamp_ms(snap.sending_ts_ms) or received_at
        normalized_book = state.local_book.to_normalized_order_book(source_ts)

        obs = MarketObservation(
            observation_id=f"ws-obs-{ticker}-{seq}-{int(received_at.timestamp())}",
            ticker=ticker,
            observed_at=received_at,
            source_timestamp=source_ts,
            market=None,
            order_book=normalized_book,
            is_success=True,
            is_stale=False,
            source_transport="WEBSOCKET",
            stream_seq=seq,
            local_receive_time=received_at,
            sync_state=SYNC_STATE_SYNCHRONIZED,
            data_quality_flags=tuple(state.data_quality_flags),
            session_id=self.session_id,
        )
        return obs

    def _handle_delta(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> Optional[MarketObservation]:
        msg = payload.get("msg", {})
        ticker = msg.get("market_ticker")
        if not ticker or ticker not in self._states:
            raise StreamingProtocolError(f"Delta for unknown or unregistered ticker: {ticker}")

        state = self._states[ticker]
        state.total_messages_received += 1
        state.last_received_at = received_at
        seq = payload.get("seq", 0)

        # If not synchronized, delta cannot be applied until snapshot arrives
        if state.sync_state != SYNC_STATE_SYNCHRONIZED or not state.local_book.is_initialized:
            logger.debug("Dropping delta on unsynchronized book for %s", ticker)
            return None

        # Sequence checking
        if state.expected_seq is not None:
            if seq < state.expected_seq:
                # Duplicate or old message
                state.duplicate_count += 1
                state.data_quality_flags.append("DUPLICATE_OR_OLD_SEQ")
                logger.debug("Duplicate or old seq %d on %s (expected %d)", seq, ticker, state.expected_seq)
                return None
            elif seq > state.expected_seq:
                # Sequence gap detected! State is compromised
                state.gap_count += 1
                state.sync_state = SYNC_STATE_INVALIDATED
                state.local_book.clear()
                state.data_quality_flags.append(f"SEQUENCE_GAP_{state.expected_seq}_TO_{seq}")
                logger.warning("Sequence gap detected on %s: expected %d, got %d. Invalidating book.", ticker, state.expected_seq, seq)
                self._trigger_resync(ticker)
                return None

        # Parse delta fields
        side = msg.get("side", "").lower()
        if side not in (YES.lower(), NO.lower()):
            raise StreamingProtocolError(f"Invalid side {side!r} in delta message")

        price = _parse_decimal_price(msg.get("price"))
        delta_qty = _parse_decimal_qty(msg.get("delta", "0"))

        delta_msg = OrderBookDeltaMsg(
            market_ticker=ticker,
            seq=seq,
            sid=payload.get("sid", 0),
            price=price,
            delta_quantity=delta_qty,
            side=side,
            sending_ts_ms=payload.get("sending_ts_ms"),
            ts_ms=msg.get("ts_ms"),
            raw_payload=dict(payload),
        )

        state.local_book.apply_delta(delta_msg)
        state.expected_seq = seq + 1

        source_ts = _parse_timestamp_ms(delta_msg.ts_ms or delta_msg.sending_ts_ms) or received_at
        normalized_book = state.local_book.to_normalized_order_book(source_ts)

        obs = MarketObservation(
            observation_id=f"ws-obs-{ticker}-{seq}-{int(received_at.timestamp())}",
            ticker=ticker,
            observed_at=received_at,
            source_timestamp=source_ts,
            market=None,
            order_book=normalized_book,
            is_success=True,
            is_stale=False,
            source_transport="WEBSOCKET",
            stream_seq=seq,
            local_receive_time=received_at,
            sync_state=SYNC_STATE_SYNCHRONIZED,
            data_quality_flags=tuple(state.data_quality_flags),
            session_id=self.session_id,
        )
        return obs

    def invalidate_market(self, ticker: str, reason: str = "MANUAL_INVALIDATION") -> None:
        """Explicitly invalidate a market book (e.g. timeout or disconnect)."""
        state = self._states.get(ticker)
        if state:
            state.sync_state = SYNC_STATE_INVALIDATED
            state.local_book.clear()
            state.data_quality_flags.append(reason)
            self._trigger_resync(ticker)

    def _trigger_resync(self, ticker: str) -> None:
        state = self._states.get(ticker)
        if state:
            state.sync_state = SYNC_STATE_RESYNCING
            state.resync_count += 1
            for cb in self._resync_callbacks:
                try:
                    cb(ticker)
                except Exception as exc:
                    logger.error("Error in resync callback for %s: %s", ticker, exc)


class StreamingTransport(ABC):
    """Abstract interface for WebSocket streaming transport."""

    @abstractmethod
    async def connect(self, url: str) -> None:
        """Establish connection."""

    @abstractmethod
    async def send_subscription(self, command: Mapping[str, Any]) -> None:
        """Transmit a subscription or resync command."""

    @abstractmethod
    async def receive_message(self) -> Optional[Mapping[str, Any]]:
        """Receive the next message frame."""

    @abstractmethod
    async def close(self) -> None:
        """Close connection cleanly."""


class MockStreamingTransport(StreamingTransport):
    """Deterministic scriptable in-memory transport for unit tests and replay."""

    def __init__(self, message_sequence: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
        self.messages: List[Mapping[str, Any]] = list(message_sequence or [])
        self.sent_commands: List[Mapping[str, Any]] = []
        self.is_connected: bool = False
        self.close_called: bool = False

    async def connect(self, url: str) -> None:
        self.is_connected = True

    async def send_subscription(self, command: Mapping[str, Any]) -> None:
        # Enforce safety rule
        params = command.get("params", {})
        channels = params.get("channels", [])
        for ch in channels:
            if ch in PROHIBITED_PRIVATE_CHANNELS:
                raise StreamingSafetyError(f"Cannot subscribe to prohibited private channel: {ch}")
        if command.get("cmd") not in ("subscribe", "update_subscription", "get_snapshot"):
            raise StreamingSafetyError(f"Prohibited streaming command: {command.get('cmd')}")
        self.sent_commands.append(dict(command))

    async def receive_message(self) -> Optional[Mapping[str, Any]]:
        if not self.messages:
            return None
        return self.messages.pop(0)

    async def close(self) -> None:
        self.is_connected = False
        self.close_called = True
