"""Kalshi Arbitrage Engine V17 — Broad-Market Discovery & Multi-Event Test Suite.

Verifies:
1. Paginated event discovery and market ingestion.
2. Explicit MECE eligibility classification:
   - Pairwise choice competitions (VALIDATED_CATEGORICAL_MECE)
   - Continuous numeric range partitions (VALIDATED_CATEGORICAL_MECE)
   - Open-ended candidate lists with residual 'Other' (VALIDATED_CATEGORICAL_MECE)
   - Open-ended candidate lists without residual 'Other' (MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE)
   - Cumulative/threshold contracts (INELIGIBLE_NON_MECE_OR_CUMULATIVE)
   - Inactive/closed markets (INELIGIBLE_INACTIVE_OR_CLOSED)
3. Multi-event snapshot reconstruction without cross-event contamination.
4. Multi-event opportunity episode tracking isolation.
5. Observable liquidity filtering (spread, depth, non-empty books).
6. Deterministic financial replay and evidence manifest integrity.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import pytest

from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.discovery import (
    BroadMarketDiscoveryEngine,
    CandidateBasketProfile,
    CATEGORY_EXCLUDED_INSUFFICIENT_DATA,
    CATEGORY_EXCLUDED_UNOBSERVABLE_LIQUIDITY,
    CATEGORY_INELIGIBLE_INACTIVE_OR_CLOSED,
    CATEGORY_INELIGIBLE_NON_MECE_OR_CUMULATIVE,
    CATEGORY_MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE,
    CATEGORY_VALIDATED_CATEGORICAL_MECE,
    DiscoveryConfig,
    DiscoveryReport,
)
from kalshi_arbitrage.episode_tracker import (
    CLOSURE_REASON_NO_EDGE,
    EPISODE_STATE_OPEN,
    OpportunityEpisodeTracker,
)
from kalshi_arbitrage.evidence_storage import (
    observation_from_dict,
    observation_to_dict,
)
from kalshi_arbitrage.market_data import (
    NormalizedEvent,
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
    SettlementSource,
)
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
    STATUS_REJECTED_NO_GROSS_EDGE,
)
from kalshi_arbitrage.rest_client import HTTPResponse, KalshiRestClient
from kalshi_arbitrage.snapshot_reconstruction import (
    EventSnapshotReconstructor,
    SnapshotReconstructionConfig,
)
from kalshi_arbitrage.v13_basket_replay import (
    HistoricalBasketReplayConfig,
    HistoricalBasketReplayEngine,
)
from kalshi_arbitrage.v16_synchronized_collector import (
    CollectionMode,
    SynchronizedBasketCollector,
    SynchronizedCollectorConfig,
)


def _make_mock_market(ticker: str, event_ticker: str, subtitle: str, status: str = "active") -> NormalizedMarket:
    return NormalizedMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type="binary",
        status=status,
        title=f"Market {ticker}",
        subtitle=subtitle,
        yes_subtitle=subtitle,
        no_subtitle=subtitle,
        rules_primary="Settlement rules.",
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
# 1. Event Listing & Pagination
# ---------------------------------------------------------------------------

def test_discovery_pagination_and_event_listing():
    class MockTransport:
        def __init__(self):
            self.calls = 0

        def get(self, url, params, timeout):
            self.calls += 1
            cursor = params.get("cursor")
            if not cursor:
                # Page 1
                return HTTPResponse(200, json.dumps({
                    "events": [
                        {"event_ticker": "EVT-1", "series_ticker": "KX1", "title": "Evt 1", "mutually_exclusive": True, "markets": []},
                        {"event_ticker": "EVT-2", "series_ticker": "KX1", "title": "Evt 2", "mutually_exclusive": False, "markets": []},
                    ],
                    "cursor": "cur-page-2",
                }).encode("utf-8"))
            else:
                # Page 2
                return HTTPResponse(200, json.dumps({
                    "events": [
                        {"event_ticker": "EVT-3", "series_ticker": "KX1", "title": "Evt 3", "mutually_exclusive": True, "markets": []},
                    ],
                    "cursor": None,
                }).encode("utf-8"))

    client = KalshiRestClient(transport=MockTransport())
    events = client.list_events(limit=2, with_nested_markets=True)
    assert len(events) == 3
    assert events[0].event_ticker == "EVT-1"
    assert events[2].event_ticker == "EVT-3"


# ---------------------------------------------------------------------------
# 2. MECE Classification Taxonomy
# ---------------------------------------------------------------------------

def test_discovery_classifies_pairwise_mece():
    engine = BroadMarketDiscoveryEngine()
    raw_e = {
        "event_ticker": "KXDEELRIP-40",
        "series_ticker": "KXDEELRIP",
        "title": "Will Deel or Rippling IPO first?",
        "category": "Corporate Finance",
        "mutually_exclusive": True,
    }
    markets = [
        _make_mock_market("KXDEELRIP-DEEL", "KXDEELRIP-40", "Deel"),
        _make_mock_market("KXDEELRIP-RIP", "KXDEELRIP-40", "Rippling"),
    ]
    prof, is_candidate = engine._classify_event_semantics(raw_e, markets)
    assert is_candidate
    assert prof.classification == CATEGORY_VALIDATED_CATEGORICAL_MECE
    assert prof.outcome_count == 2


def test_discovery_classifies_range_partition_mece():
    engine = BroadMarketDiscoveryEngine()
    raw_e = {
        "event_ticker": "KXFEDDECISION-26OCT",
        "series_ticker": "KXFEDDECISION",
        "title": "Fed rate decision October 2026",
        "category": "Economics",
        "mutually_exclusive": True,
    }
    markets = [
        _make_mock_market("M-C26", "KXFEDDECISION-26OCT", "Cut >25"),
        _make_mock_market("M-C25", "KXFEDDECISION-26OCT", "Cut 25"),
        _make_mock_market("M-H0", "KXFEDDECISION-26OCT", "Maintain"),
        _make_mock_market("M-H25", "KXFEDDECISION-26OCT", "Hike 25"),
        _make_mock_market("M-H26", "KXFEDDECISION-26OCT", "Hike >25"),
    ]
    prof, is_candidate = engine._classify_event_semantics(raw_e, markets)
    assert is_candidate
    assert prof.classification == CATEGORY_VALIDATED_CATEGORICAL_MECE
    assert prof.outcome_count == 5


def test_discovery_classifies_residual_market_mece():
    engine = BroadMarketDiscoveryEngine()
    raw_e = {
        "event_ticker": "KXNEXTDNCCHAIR-45",
        "series_ticker": "KXNEXTDNCCHAIR",
        "title": "Who will be the next DNC Chair?",
        "category": "Politics",
        "mutually_exclusive": True,
    }
    markets = [
        _make_mock_market("M-A", "KXNEXTDNCCHAIR-45", "Candidate A"),
        _make_mock_market("M-B", "KXNEXTDNCCHAIR-45", "Candidate B"),
        _make_mock_market("M-OTHER", "KXNEXTDNCCHAIR-45", "All Other Candidates"),
    ]
    prof, is_candidate = engine._classify_event_semantics(raw_e, markets)
    assert is_candidate
    assert prof.classification == CATEGORY_VALIDATED_CATEGORICAL_MECE
    assert prof.residual_market_ticker == "M-OTHER"


def test_discovery_rejects_open_ended_without_residual():
    engine = BroadMarketDiscoveryEngine()
    raw_e = {
        "event_ticker": "KXNEWPOPE-70",
        "series_ticker": "KXNEWPOPE",
        "title": "Who will the next Pope be?",
        "category": "World",
        "mutually_exclusive": True,
    }
    # Open-ended list of candidates without 'Other'
    markets = [
        _make_mock_market("M-PIZ", "KXNEWPOPE-70", "Pizzaballa"),
        _make_mock_market("M-PAR", "KXNEWPOPE-70", "Parolin"),
        _make_mock_market("M-TAG", "KXNEWPOPE-70", "Tagle"),
    ]
    prof, is_candidate = engine._classify_event_semantics(raw_e, markets)
    assert not is_candidate
    assert prof.classification == CATEGORY_MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE
    assert "lacks verified 'Other'" in prof.classification_reason


def test_discovery_rejects_cumulative_and_non_mutually_exclusive():
    engine = BroadMarketDiscoveryEngine()
    raw_e = {
        "event_ticker": "KXNFLCAREERPASSYDS-MAHOMES",
        "series_ticker": "KXNFLCAREERPASSYDS",
        "title": "Patrick Mahomes Total Career Passing Yards",
        "category": "Sports",
        "mutually_exclusive": False,  # Cumulative strikes
    }
    markets = [
        _make_mock_market("M-40K", "KXNFLCAREERPASSYDS-MAHOMES", ">40,000 yards"),
        _make_mock_market("M-50K", "KXNFLCAREERPASSYDS-MAHOMES", ">50,000 yards"),
        _make_mock_market("M-60K", "KXNFLCAREERPASSYDS-MAHOMES", ">60,000 yards"),
    ]
    prof, is_candidate = engine._classify_event_semantics(raw_e, markets)
    assert not is_candidate
    assert prof.classification == CATEGORY_INELIGIBLE_NON_MECE_OR_CUMULATIVE


def test_discovery_rejects_inactive_or_insufficient_legs():
    engine = BroadMarketDiscoveryEngine(DiscoveryConfig(require_active_markets=True))
    raw_e = {
        "event_ticker": "EVT-INACTIVE",
        "series_ticker": "KXTEST",
        "title": "Test Inactive",
        "mutually_exclusive": True,
    }
    markets = [
        _make_mock_market("M-1", "EVT-INACTIVE", "Choice 1", status="active"),
        _make_mock_market("M-2", "EVT-INACTIVE", "Choice 2", status="closed"),  # Closed
    ]
    prof, is_candidate = engine._classify_event_semantics(raw_e, markets)
    assert not is_candidate
    assert prof.classification == CATEGORY_INELIGIBLE_INACTIVE_OR_CLOSED


# ---------------------------------------------------------------------------
# 3. Multi-Event Isolation & Snapshot Reconstruction
# ---------------------------------------------------------------------------

def test_multi_event_collection_isolation_prevents_contamination():
    # Basket 1: Event FED (2 legs)
    b1 = DeclaredMeceBasket(
        event_ticker="EVT-FED",
        basket_id="B1",
        outcomes=("CUT", "HIKE"),
        market_outcome_map={"M-FED-1": "CUT", "M-FED-2": "HIKE"},
    )
    # Basket 2: Event IPO (2 legs)
    b2 = DeclaredMeceBasket(
        event_ticker="EVT-IPO",
        basket_id="B2",
        outcomes=("DEEL", "RIP"),
        market_outcome_map={"M-IPO-1": "DEEL", "M-IPO-2": "RIP"},
    )

    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    def _make_dummy_market(ticker: str, event_ticker: str) -> NormalizedMarket:
        return NormalizedMarket(
            ticker=ticker,
            event_ticker=event_ticker,
            market_type="binary",
            status="active",
            title=f"Market {ticker}",
            subtitle="sub",
            yes_subtitle="yes_sub",
            no_subtitle="no_sub",
            rules_primary="rule",
            rules_secondary="sec_rule",
            open_time=t0,
            close_time=t0,
            expiration_time=t0,
            yes_bid_dollars=Decimal("0.49"),
            yes_ask_dollars=Decimal("0.51"),
            no_bid_dollars=Decimal("0.49"),
            no_ask_dollars=Decimal("0.51"),
            last_price_dollars=Decimal("0.50"),
            volume_24h=100,
            open_interest=100,
        )

    # Interleaved observations from both events
    observations = [
        MarketObservation("obs-fed-1", "M-FED-1", t0, t0, _make_dummy_market("M-FED-1", "EVT-FED"), NormalizedOrderBook("M-FED-1", (), (), t0), True, False),
        MarketObservation("obs-ipo-1", "M-IPO-1", t0, t0, _make_dummy_market("M-IPO-1", "EVT-IPO"), NormalizedOrderBook("M-IPO-1", (), (), t0), True, False),
        MarketObservation("obs-fed-2", "M-FED-2", t0, t0, _make_dummy_market("M-FED-2", "EVT-FED"), NormalizedOrderBook("M-FED-2", (), (), t0), True, False),
        MarketObservation("obs-ipo-2", "M-IPO-2", t0, t0, _make_dummy_market("M-IPO-2", "EVT-IPO"), NormalizedOrderBook("M-IPO-2", (), (), t0), True, False),
    ]

    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig())
    snapshots, _ = reconstructor.reconstruct(observations, [b1, b2])

    assert len(snapshots) == 2
    # Verify strict isolation: FED snapshot only contains FED tickers; IPO only IPO tickers
    fed_snap = next(s for s in snapshots if s.basket.event_ticker == "EVT-FED")
    ipo_snap = next(s for s in snapshots if s.basket.event_ticker == "EVT-IPO")

    assert set(fed_snap.observations.keys()) == {"M-FED-1", "M-FED-2"}
    assert set(ipo_snap.observations.keys()) == {"M-IPO-1", "M-IPO-2"}
    assert fed_snap.is_valid
    assert ipo_snap.is_valid


# ---------------------------------------------------------------------------
# 4. Multi-Event Opportunity Episode Tracking Isolation
# ---------------------------------------------------------------------------

def test_multi_event_episode_tracking_isolation():
    tracker = OpportunityEpisodeTracker()
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    # Qualified observation on Event A
    tracker.record_candidate(
        basket_id="BASKET-EVENT-A",
        strategy_type="LONG_YES",
        observed_at=t0,
        is_qualified=True,
        gross_edge_cents=5,
        net_edge_cents=2,
        supported_quantity=10,
    )
    # Qualified observation on Event B
    tracker.record_candidate(
        basket_id="BASKET-EVENT-B",
        strategy_type="LONG_YES",
        observed_at=t0,
        is_qualified=True,
        gross_edge_cents=8,
        net_edge_cents=4,
        supported_quantity=20,
    )

    # Must produce 2 distinct active episodes
    assert len(tracker.active_episodes) == 2
    ep_a = tracker.active_episodes["BASKET-EVENT-A:LONG_YES"]
    ep_b = tracker.active_episodes["BASKET-EVENT-B:LONG_YES"]
    assert ep_a.episode_id != ep_b.episode_id
    assert ep_a.min_gross_edge_cents == 5
    assert ep_b.min_gross_edge_cents == 8

    # Closure on Event A does NOT close Event B
    tracker.record_candidate(
        basket_id="BASKET-EVENT-A",
        strategy_type="LONG_YES",
        observed_at=t0,
        is_qualified=False,
        gross_edge_cents=-2,
        net_edge_cents=-5,
        supported_quantity=0,
        rejection_reason=STATUS_REJECTED_NO_GROSS_EDGE,
    )
    assert len(tracker.active_episodes) == 1
    assert "BASKET-EVENT-B:LONG_YES" in tracker.active_episodes
    assert len(tracker.completed_episodes) == 1
    assert tracker.completed_episodes[0].basket_id == "BASKET-EVENT-A"


# ---------------------------------------------------------------------------
# 5. Financial Evaluation & Multi-Event Replay
# ---------------------------------------------------------------------------

def test_replay_multi_event_evaluates_exact_financial_invariants():
    b_fed = DeclaredMeceBasket(
        event_ticker="EVT-FED",
        basket_id="FED-MECE",
        outcomes=("CUT", "HIKE"),
        market_outcome_map={"M-F1": "CUT", "M-F2": "HIKE"},
    )
    b_ipo = DeclaredMeceBasket(
        event_ticker="EVT-IPO",
        basket_id="IPO-MECE",
        outcomes=("DEEL", "RIP"),
        market_outcome_map={"M-I1": "DEEL", "M-I2": "RIP"},
    )

    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    # FED: asks at 52c + 52c = 104c (-4c gross edge)
    # NO bids at 48c create YES asks at 52c
    bk_f1 = NormalizedOrderBook("M-F1", (), (OrderBookLevel(Decimal("0.48"), Decimal("10")),), t0)
    bk_f2 = NormalizedOrderBook("M-F2", (), (OrderBookLevel(Decimal("0.48"), Decimal("10")),), t0)
    m_f1 = _make_mock_market("M-F1", "EVT-FED", "CUT")
    m_f2 = _make_mock_market("M-F2", "EVT-FED", "HIKE")

    # IPO: asks at 45c + 45c = 90c (+10c gross edge)
    # NO bids at 55c create YES asks at 45c
    bk_i1 = NormalizedOrderBook("M-I1", (), (OrderBookLevel(Decimal("0.55"), Decimal("10")),), t0)
    bk_i2 = NormalizedOrderBook("M-I2", (), (OrderBookLevel(Decimal("0.55"), Decimal("10")),), t0)
    m_i1 = _make_mock_market("M-I1", "EVT-IPO", "DEEL")
    m_i2 = _make_mock_market("M-I2", "EVT-IPO", "RIP")

    observations = [
        MarketObservation("obs-f1", "M-F1", t0, t0, m_f1, bk_f1, True, False),
        MarketObservation("obs-f2", "M-F2", t0, t0, m_f2, bk_f2, True, False),
        MarketObservation("obs-i1", "M-I1", t0, t0, m_i1, bk_i1, True, False),
        MarketObservation("obs-i2", "M-I2", t0, t0, m_i2, bk_i2, True, False),
    ]

    replayer = HistoricalBasketReplayEngine(HistoricalBasketReplayConfig(target_quantity=1))
    summary = replayer.replay(observations, [b_fed, b_ipo])
    records = summary.records

    assert len(records) == 2
    fed_rec = next(r for r in records if r.event_ticker == "EVT-FED")
    ipo_rec = next(r for r in records if r.event_ticker == "EVT-IPO")

    # FED: Gross cost 104c, payout 100c, gross edge -4c -> rejected
    assert fed_rec.gross_acquisition_cost_cents == 104
    assert fed_rec.guaranteed_payout_cents == 100
    assert fed_rec.gross_edge_cents == -4
    assert fed_rec.status == STATUS_REJECTED_NO_GROSS_EDGE

    # IPO: Gross cost 90c, payout 100c, gross edge +10c -> qualified
    assert ipo_rec.gross_acquisition_cost_cents == 90
    assert ipo_rec.guaranteed_payout_cents == 100
    assert ipo_rec.gross_edge_cents == 10
    assert ipo_rec.net_edge_cents > 0
    assert ipo_rec.status == STATUS_QUALIFIED


# ---------------------------------------------------------------------------
# 6. Backward Compatibility with V12–V16 Evidence
# ---------------------------------------------------------------------------

def test_backward_compatibility_v12_v16_evidence_parsing():
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    v16_dict = {
        "schema_version": "1.0",
        "observation_id": "v16-obs-test",
        "ticker": "KXFEDDECISION-26DEC-C25",
        "observed_at": t0.isoformat(),
        "source_timestamp": t0.isoformat(),
        "market": None,
        "order_book": None,
        "is_success": True,
        "is_stale": False,
        "source_transport": "REST_CONCURRENT",
        "stream_seq": None,
        "local_receive_time": t0.isoformat(),
        "sync_state": "SYNCHRONIZED",
        "data_quality_flags": ["SYNCHRONIZED_OK"],
        "session_id": "sess-v16",
    }

    obs = observation_from_dict(v16_dict)
    assert obs.observation_id == "v16-obs-test"
    assert obs.source_transport == "REST_CONCURRENT"
    assert obs.session_id == "sess-v16"

    # Reserialize and confirm identity
    reserialized = observation_to_dict(obs)
    assert reserialized["source_transport"] == "REST_CONCURRENT"
    assert reserialized["session_id"] == "sess-v16"
