"""Kalshi Arbitrage Engine V16 — Streaming, Universe Selection & Episode Tracking Test Suite.

Verifies:
1. WebSocket snapshot ingestion and L2 order book initialization.
2. Incremental delta application (absolute quantity, overwrites, removals).
3. Out-of-order and duplicate sequence handling.
4. Sequence gap detection, book invalidation, and snapshot resync.
5. Strict read-only safety guardrails (rejection of trading channels/commands).
6. Evidence schema backward compatibility across v1.0 and v1.1 records.
7. Universe selection criteria (spread, depth, active status, outcome completeness).
8. Deterministic opportunity episode lifecycle (start, update, gap timeout, closure).
9. Repeated-state vs unique fingerprint tracking.
10. Bounded collection termination and SHA-256 manifest integrity.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import pytest

from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.episode_tracker import (
    CLOSURE_REASON_NO_EDGE,
    CLOSURE_REASON_TIMEOUT,
    EPISODE_STATE_CLOSED,
    EPISODE_STATE_OPEN,
    EPISODE_STATE_UPDATED,
    EpisodeTrackerConfig,
    OpportunityEpisodeTracker,
)
from kalshi_arbitrage.evidence_storage import (
    EVIDENCE_SCHEMA_VERSION,
    observation_from_dict,
    observation_to_dict,
)
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
    STATUS_REJECTED_NO_GROSS_EDGE,
)
from kalshi_arbitrage.universe_selector import (
    MeceUniverseSelector,
    UniverseSelectorConfig,
)
from kalshi_arbitrage.v16_synchronized_collector import (
    CollectionMode,
    SynchronizedBasketCollector,
    SynchronizedCollectorConfig,
)
from kalshi_arbitrage.ws_streaming import (
    CHANNEL_ORDERBOOK_DELTA,
    LocalL2OrderBook,
    MockStreamingTransport,
    OrderBookDeltaMsg,
    OrderBookSnapshotMsg,
    OrderBookStreamSynchronizer,
    SequenceGapError,
    StreamingProtocolError,
    StreamingSafetyError,
    SYNC_STATE_INVALIDATED,
    SYNC_STATE_RESYNCING,
    SYNC_STATE_SYNCHRONIZED,
    SYNC_STATE_UNSYNCHRONIZED,
)


def _make_sample_market(ticker: str, event_ticker: str, status: str = "active") -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status=status,
        title=f"Sample Market {ticker}",
        subtitle=None,
        yes_subtitle=None,
        no_subtitle=None,
        rules_primary="Mutually exclusive test rule.",
        rules_secondary=None,
        expiration_value=None,
        result=None,
        created_time=None,
        updated_time=None,
        open_time=None,
        close_time=None,
        expiration_time=None,
        settlement_ts=None,
        yes_bid_dollars=Decimal("0.40"),
        yes_ask_dollars=Decimal("0.45"),
        no_bid_dollars=Decimal("0.55"),
        no_ask_dollars=Decimal("0.60"),
        last_price_dollars=Decimal("0.42"),
        settlement_value_dollars=None,
        yes_bid_size=Decimal("100"),
        yes_ask_size=Decimal("100"),
        volume=Decimal("1000"),
        volume_24h=Decimal("500"),
        open_interest=Decimal("2000"),
        price_ranges=(),
        is_provisional=None,
    )


# ---------------------------------------------------------------------------
# 1. WebSocket L2 Order Book & Snapshot Processing
# ---------------------------------------------------------------------------

def test_ws_snapshot_processing():
    book = LocalL2OrderBook("TEST-TICKER")
    assert not book.is_initialized

    snap = OrderBookSnapshotMsg(
        market_ticker="TEST-TICKER",
        seq=10,
        sid=1,
        sending_ts_ms=1669149841000,
        yes_bids=((Decimal("0.40"), Decimal("100")), (Decimal("0.35"), Decimal("50"))),
        no_bids=((Decimal("0.55"), Decimal("80")),),
    )
    book.apply_snapshot(snap)
    assert book.is_initialized
    assert book.last_seq == 10

    normalized = book.to_normalized_order_book()
    assert normalized.market_ticker == "TEST-TICKER"
    assert len(normalized.yes_bids) == 2
    # Verify sorted ascending
    assert normalized.yes_bids[0].price_dollars == Decimal("0.35")
    assert normalized.yes_bids[1].price_dollars == Decimal("0.40")
    assert len(normalized.no_bids) == 1


def test_ws_incremental_delta_add_and_remove():
    book = LocalL2OrderBook("TEST-TICKER")
    snap = OrderBookSnapshotMsg(
        market_ticker="TEST-TICKER",
        seq=1,
        sid=1,
        sending_ts_ms=1669149841000,
        yes_bids=((Decimal("0.40"), Decimal("100")),),
        no_bids=(),
    )
    book.apply_snapshot(snap)

    # 1. Add new level
    delta_add = OrderBookDeltaMsg(
        market_ticker="TEST-TICKER",
        seq=2,
        sid=1,
        price=Decimal("0.42"),
        delta_quantity=Decimal("150"),
        side="yes",
        sending_ts_ms=1669149842000,
        ts_ms=1669149842000,
    )
    book.apply_delta(delta_add)
    norm = book.to_normalized_order_book()
    assert len(norm.yes_bids) == 2
    assert norm.yes_bids[0].price_dollars == Decimal("0.40")
    assert norm.yes_bids[1].price_dollars == Decimal("0.42")
    assert norm.yes_bids[1].quantity == Decimal("150")

    # 2. Overwrite existing level
    delta_update = OrderBookDeltaMsg(
        market_ticker="TEST-TICKER",
        seq=3,
        sid=1,
        price=Decimal("0.40"),
        delta_quantity=Decimal("75"),
        side="yes",
        sending_ts_ms=1669149843000,
        ts_ms=1669149843000,
    )
    book.apply_delta(delta_update)
    norm = book.to_normalized_order_book()
    assert norm.yes_bids[0].quantity == Decimal("75")

    # 3. Remove level when delta_quantity == 0
    delta_remove = OrderBookDeltaMsg(
        market_ticker="TEST-TICKER",
        seq=4,
        sid=1,
        price=Decimal("0.40"),
        delta_quantity=Decimal("0"),
        side="yes",
        sending_ts_ms=1669149844000,
        ts_ms=1669149844000,
    )
    book.apply_delta(delta_remove)
    norm = book.to_normalized_order_book()
    assert len(norm.yes_bids) == 1
    assert norm.yes_bids[0].price_dollars == Decimal("0.42")


# ---------------------------------------------------------------------------
# 2. Sequence Continuity, Duplicates & Gap Invalidation
# ---------------------------------------------------------------------------

def test_ws_duplicate_or_old_seq_ignored():
    sync = OrderBookStreamSynchronizer()
    sync.subscribe_market("M1")

    # Initial snapshot seq=10
    sync.process_message({
        "type": "orderbook_snapshot",
        "seq": 10,
        "sid": 1,
        "sending_ts_ms": 1669149841000,
        "msg": {"market_ticker": "M1", "yes": [["0.40", 100]], "no": []},
    })
    assert sync.is_synchronized("M1")

    # Inbound duplicate delta with seq=10 (less than expected_seq=11)
    obs = sync.process_message({
        "type": "orderbook_delta",
        "seq": 10,
        "sid": 1,
        "sending_ts_ms": 1669149842000,
        "msg": {"market_ticker": "M1", "price": "0.45", "delta": "50", "side": "yes"},
    })
    assert obs is None  # Dropped without corrupting book
    state = sync._states["M1"]
    assert state.duplicate_count == 1
    assert "DUPLICATE_OR_OLD_SEQ" in state.data_quality_flags


def test_ws_sequence_gap_invalidates_and_triggers_resync():
    sync = OrderBookStreamSynchronizer()
    sync.subscribe_market("M1")
    resync_tickers = []
    sync.register_resync_callback(lambda t: resync_tickers.append(t))

    sync.process_message({
        "type": "orderbook_snapshot",
        "seq": 1,
        "sid": 1,
        "msg": {"market_ticker": "M1", "yes": [["0.40", 100]], "no": []},
    })
    assert sync.is_synchronized("M1")

    # Inbound delta with gap: expected seq=2, received seq=5
    obs = sync.process_message({
        "type": "orderbook_delta",
        "seq": 5,
        "sid": 1,
        "msg": {"market_ticker": "M1", "price": "0.45", "delta": "50", "side": "yes"},
    })
    assert obs is None
    assert not sync.is_synchronized("M1")
    assert sync.get_sync_state("M1") == SYNC_STATE_RESYNCING
    assert resync_tickers == ["M1"]


def test_ws_resynchronization_from_snapshot():
    sync = OrderBookStreamSynchronizer()
    sync.subscribe_market("M1")

    # Snapshot 1
    sync.process_message({
        "type": "orderbook_snapshot",
        "seq": 1,
        "msg": {"market_ticker": "M1", "yes": [["0.40", 100]], "no": []},
    })
    # Invalidate
    sync.invalidate_market("M1")
    assert not sync.is_synchronized("M1")

    # Resync snapshot
    obs = sync.process_message({
        "type": "orderbook_snapshot",
        "seq": 20,
        "msg": {"market_ticker": "M1", "yes": [["0.41", 200]], "no": []},
    })
    assert obs is not None
    assert sync.is_synchronized("M1")
    assert obs.order_book.yes_bids[0].price_dollars == Decimal("0.41")


# ---------------------------------------------------------------------------
# 3. Read-Only Safety Guardrails
# ---------------------------------------------------------------------------

def test_ws_read_only_safety_prohibits_trading():
    sync = OrderBookStreamSynchronizer()

    # Attempting to subscribe to private channels must raise StreamingSafetyError
    with pytest.raises(StreamingSafetyError):
        sync.subscribe_market("M1", channel="fill")

    with pytest.raises(StreamingSafetyError):
        sync.subscribe_market("M1", channel="orders")

    # Mock transport prohibits trading commands
    transport = MockStreamingTransport()
    import asyncio
    with pytest.raises(StreamingSafetyError):
        asyncio.run(transport.send_subscription({"cmd": "place_order"}))


def test_ws_heartbeats_and_pongs():
    sync = OrderBookStreamSynchronizer()
    sync.subscribe_market("M1")
    res = sync.process_message({"type": "heartbeat"})
    assert res is None
    res_pong = sync.process_message({"type": "pong"})
    assert res_pong is None


# ---------------------------------------------------------------------------
# 4. Evidence Schema Backward Compatibility (v1.0 & v1.1)
# ---------------------------------------------------------------------------

def test_schema_backward_compatibility_v10_and_v11():
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    obs = MarketObservation(
        observation_id="obs-compat-1",
        ticker="M-COMPAT",
        observed_at=t0,
        source_timestamp=t0,
        market=None,
        order_book=NormalizedOrderBook("M-COMPAT", (), (), source_timestamp=t0),
        is_success=True,
        is_stale=False,
        source_transport="WEBSOCKET",
        stream_seq=42,
        local_receive_time=t0,
        sync_state="SYNCHRONIZED",
        data_quality_flags=("HIGH_QUALITY",),
        session_id="sess-123",
    )

    d = observation_to_dict(obs)
    assert d["source_transport"] == "WEBSOCKET"
    assert d["stream_seq"] == 42
    assert d["session_id"] == "sess-123"

    # Deserialize back
    recovered = observation_from_dict(d)
    assert recovered.source_transport == "WEBSOCKET"
    assert recovered.stream_seq == 42
    assert recovered.data_quality_flags == ("HIGH_QUALITY",)

    # Legacy v1.0 record without new fields
    legacy_dict = {
        "schema_version": "1.0",
        "observation_id": "legacy-obs",
        "ticker": "M-LEGACY",
        "observed_at": t0.isoformat(),
        "source_timestamp": t0.isoformat(),
        "market": None,
        "order_book": None,
        "is_success": True,
        "is_stale": False,
    }
    legacy_rec = observation_from_dict(legacy_dict)
    assert legacy_rec.source_transport == "REST"
    assert legacy_rec.stream_seq is None
    assert legacy_rec.sync_state == "SYNCHRONIZED"


# ---------------------------------------------------------------------------
# 5. Universe Selection Verification
# ---------------------------------------------------------------------------

def test_universe_selector_accepts_valid_liquid_basket():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-VALID",
        basket_id="B-VALID",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
        basket_side=YES,
    )
    m_a = _make_sample_market("M-A", "EVT-VALID", status="active")
    m_b = _make_sample_market("M-B", "EVT-VALID", status="active")

    # Books with depth: NO bid at 0.55 provides YES ask at 0.45
    b_a = NormalizedOrderBook("M-A", (), (OrderBookLevel(Decimal("0.55"), Decimal("100")),))
    b_b = NormalizedOrderBook("M-B", (), (OrderBookLevel(Decimal("0.55"), Decimal("100")),))

    selector = MeceUniverseSelector(UniverseSelectorConfig(max_spread_cents=15, min_depth_contracts=10))
    prof = selector.evaluate_basket(basket, {"M-A": m_a, "M-B": m_b}, {"M-A": b_a, "M-B": b_b})
    assert prof.is_accepted
    assert len(prof.rejection_reasons) == 0
    assert prof.combined_yes_ask_cents == 90  # 45c + 45c
    assert prof.min_depth_contracts == 100


def test_universe_selector_excludes_inactive_or_empty_markets():
    basket = DeclaredMeceBasket(
        event_ticker="EVT-INACTIVE",
        basket_id="B-INACTIVE",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
    )
    m_a = _make_sample_market("M-A", "EVT-INACTIVE", status="inactive")  # Inactive
    m_b = _make_sample_market("M-B", "EVT-INACTIVE", status="active")
    b_a = NormalizedOrderBook("M-A", (), ())  # Empty
    b_b = NormalizedOrderBook("M-B", (), (OrderBookLevel(Decimal("0.55"), Decimal("10")),))

    selector = MeceUniverseSelector(UniverseSelectorConfig(require_active_status=True))
    prof = selector.evaluate_basket(basket, {"M-A": m_a, "M-B": m_b}, {"M-A": b_a, "M-B": b_b})
    assert not prof.is_accepted
    assert any("INACTIVE" in r for r in prof.rejection_reasons)
    assert any("EMPTY" in r for r in prof.rejection_reasons)


# ---------------------------------------------------------------------------
# 6. Opportunity Episode Tracking
# ---------------------------------------------------------------------------

def test_opportunity_episode_lifecycle_and_inactivity_timeout():
    tracker = OpportunityEpisodeTracker(EpisodeTrackerConfig(inactivity_timeout_seconds=30.0))
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Start episode
    ep1 = tracker.record_candidate(
        basket_id="B1",
        strategy_type="LONG_YES",
        observed_at=t0,
        is_qualified=True,
        gross_edge_cents=5,
        net_edge_cents=2,
        supported_quantity=10,
        market_state_fingerprint="fp-1",
    )
    assert ep1 is not None
    assert ep1.state == EPISODE_STATE_OPEN
    assert ep1.observation_count == 1

    # 2. Update episode (within timeout)
    t1 = t0 + timedelta(seconds=10)
    ep1_updated = tracker.record_candidate(
        basket_id="B1",
        strategy_type="LONG_YES",
        observed_at=t1,
        is_qualified=True,
        gross_edge_cents=6,
        net_edge_cents=3,
        supported_quantity=15,
        market_state_fingerprint="fp-2",
    )
    assert ep1_updated.episode_id == ep1.episode_id
    assert ep1_updated.state == EPISODE_STATE_UPDATED
    assert ep1_updated.observation_count == 2
    assert ep1_updated.max_gross_edge_cents == 6
    assert ep1_updated.duration_seconds == 10.0

    # 3. Inactivity timeout triggers closure and starts new episode
    t2 = t1 + timedelta(seconds=45)  # > 30s timeout
    ep2 = tracker.record_candidate(
        basket_id="B1",
        strategy_type="LONG_YES",
        observed_at=t2,
        is_qualified=True,
        gross_edge_cents=4,
        net_edge_cents=1,
        supported_quantity=5,
        market_state_fingerprint="fp-3",
    )
    assert ep2.episode_id != ep1.episode_id
    assert len(tracker.completed_episodes) == 1
    assert tracker.completed_episodes[0].closure_reason == CLOSURE_REASON_TIMEOUT


def test_opportunity_episode_closure_on_non_qualified_state():
    tracker = OpportunityEpisodeTracker()
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    # Qualified observation opens episode
    tracker.record_candidate(
        basket_id="B1",
        strategy_type="LONG_YES",
        observed_at=t0,
        is_qualified=True,
        gross_edge_cents=5,
        net_edge_cents=2,
        supported_quantity=10,
    )
    assert len(tracker.active_episodes) == 1

    # Next observation rejected due to NO_GROSS_EDGE
    t1 = t0 + timedelta(seconds=5)
    tracker.record_candidate(
        basket_id="B1",
        strategy_type="LONG_YES",
        observed_at=t1,
        is_qualified=False,
        gross_edge_cents=-2,
        net_edge_cents=-5,
        supported_quantity=10,
        rejection_reason=STATUS_REJECTED_NO_GROSS_EDGE,
    )
    assert len(tracker.active_episodes) == 0
    assert len(tracker.completed_episodes) == 1
    assert tracker.completed_episodes[0].closure_reason == CLOSURE_REASON_NO_EDGE


def test_opportunity_episode_fingerprint_and_repeated_state_tracking():
    tracker = OpportunityEpisodeTracker()
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    for i in range(4):
        tracker.record_candidate(
            basket_id="B1",
            strategy_type="LONG_YES",
            observed_at=t0 + timedelta(seconds=i),
            is_qualified=True,
            gross_edge_cents=5,
            net_edge_cents=2,
            supported_quantity=10,
            market_state_fingerprint="identical-resting-state-fp",
        )

    ep = list(tracker.active_episodes.values())[0]
    assert ep.observation_count == 4
    # 3 repetitions of the initial state
    assert ep.repeated_state_count == 3


# ---------------------------------------------------------------------------
# 7. Synchronized Collector Skew Comparison & Manifest
# ---------------------------------------------------------------------------

def test_synchronized_collector_manifest_and_bounded_termination(tmp_path):
    class MockClient:
        def __init__(self):
            self.call_count = 0

        def get_market(self, ticker):
            return _make_sample_market(ticker, "EVT-TEST")

        def get_order_book(self, ticker):
            self.call_count += 1
            return NormalizedOrderBook(
                ticker,
                (),
                (OrderBookLevel(Decimal("0.50"), Decimal("10")),),
                source_timestamp=datetime.now(timezone.utc),
            )

    basket = DeclaredMeceBasket(
        event_ticker="EVT-TEST",
        basket_id="B-TEST",
        outcomes=("A", "B"),
        market_outcome_map={"M-A": "A", "M-B": "B"},
    )

    client = MockClient()
    cfg = SynchronizedCollectorConfig(
        mode=CollectionMode.CONCURRENT_REST,
        cycle_count=2,
        cycle_interval_seconds=0.01,
        target_quantity=1,
    )
    collector = SynchronizedBasketCollector(client=client, config=cfg)
    obs_list, metrics = collector.run_collection([basket], tmp_path)

    # 2 cycles * 2 tickers = 4 observations
    assert len(obs_list) == 4
    assert metrics.total_observations == 4
    assert metrics.mode == "CONCURRENT_REST"

    manifest_file = tmp_path / "manifest.json"
    assert manifest_file.exists()
    manifest_data = json.loads(manifest_file.read_text())
    assert manifest_data["mode"] == "CONCURRENT_REST"
    assert "observations.jsonl" in manifest_data["files"]
    assert len(manifest_data["files"]["observations.jsonl"]["sha256"]) == 64
