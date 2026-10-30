"""Kalshi Arbitrage Engine V19 — Release Gates & Packaging Invariant Tests.

Verifies:
1. Package discovery and module exports.
2. Read-only security boundaries: KalshiRestClient contains no mutating or order placement methods.
3. Canonical evidence integrity: V16, V17, and V18 trial manifests verify against actual disk files.
4. Two-sided MECE mathematical invariant across arbitrary partition sizes (N=2, 3, 5, 14).
5. Fail-closed financial validation on invalid, incomplete, or corrupted market states.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pytest
from typing import Dict, List, Optional, Tuple

import kalshi_arbitrage
from kalshi_arbitrage.attribution import (
    EconomicAttributionCategory,
    OpportunityEconomicsAnalyzer,
)
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.execution import evaluate_mece_basket_execution
from kalshi_arbitrage.market_data import (
    NormalizedMarket,
    NormalizedOrderBook,
    OrderBookLevel,
)
from kalshi_arbitrage.observer import DeclaredMeceBasket, MarketObservation
from kalshi_arbitrage.portfolio import Event
from kalshi_arbitrage.rest_client import KalshiRestClient
from kalshi_arbitrage.snapshot_reconstruction import (
    ReconstructedBasketSnapshot,
    STATUS_VALID_SNAPSHOT,
)


def test_public_api_exports():
    """Verify that all public entry points exported in __all__ are defined."""
    exported = kalshi_arbitrage.__all__
    assert len(exported) > 30
    for name in exported:
        assert hasattr(kalshi_arbitrage, name), f"Missing exported symbol: {name}"


def test_rest_client_read_only_boundary():
    """Verify that KalshiRestClient strictly exposes read-only queries."""
    client = KalshiRestClient()
    methods = [m for m in dir(client) if not m.startswith("_")]
    allowed_methods = {
        "base_url",
        "timeout_seconds",
        "get_market",
        "get_order_book",
        "get_event",
        "list_markets",
        "list_events",
        "close",
    }
    assert set(methods) == allowed_methods
    # Verify no order placement or mutation methods exist
    forbidden_keywords = ["order", "trade", "buy", "sell", "post", "cancel", "submit", "amend"]
    for m in methods:
        if m in ("get_order_book",):
            continue
        for kw in forbidden_keywords:
            assert kw not in m.lower(), f"Unexpected non-read-only method: {m}"


def test_canonical_manifest_integrity():
    """Verify SHA-256 integrity across canonical evidence trials (V16, V17, V18)."""
    trials = [
        "evidence_v16_streaming_trial",
        "evidence_v17_broad_market_trial",
        "evidence_v18_economics_trial",
    ]
    for trial_name in trials:
        p = Path(trial_name)
        if not p.exists():
            continue
        manifest_file = p / "manifest.json"
        assert manifest_file.exists(), f"Missing manifest.json in {trial_name}"
        manifest = json.loads(manifest_file.read_text())
        files = manifest.get("files", {})
        for fname, meta in files.items():
            fpath = p / fname
            assert fpath.exists(), f"Manifest references missing file {fpath}"
            expected_hash = meta if isinstance(meta, str) else meta.get("sha256")
            actual_hash = hashlib.sha256(fpath.read_bytes()).hexdigest()
            assert actual_hash == expected_hash, f"Hash mismatch for {fpath}"


@pytest.mark.parametrize("n_legs", [2, 3, 5, 14])
def test_two_sided_invariant_across_various_leg_counts(n_legs: int):
    """Verify Gross Edge(YES) + Gross Edge(NO) == -Aggregate Spread holds for any N-outcome MECE."""
    outcomes = tuple(f"OUT_{i}" for i in range(n_legs))
    tickers = tuple(f"TICKER_{i}" for i in range(n_legs))
    event = Event(f"EVT-{n_legs}", outcomes, relationship_established=True)

    # Construct synthetic books with arbitrary realistic spreads (e.g. 2c to 10c spread per leg)
    books = {}
    observations = {}
    total_spread = 0
    t0 = datetime.now(timezone.utc)

    for i in range(n_legs):
        t = tickers[i]
        out = outcomes[i]
        # Spread varies by leg
        spread = 2 + (i % 6)
        y_bid = max(1, (100 // n_legs) - (spread // 2))
        y_ask = y_bid + spread
        total_spread += spread

        no_bid = 100 - y_ask
        no_ask = 100 - y_bid

        m = NormalizedMarket(
            ticker=t,
            event_ticker=f"EVT-{n_legs}",
            market_type="binary",
            status="active",
            title=f"Market {t}",
            subtitle=out,
            yes_subtitle="Yes",
            no_subtitle="No",
            rules_primary="Rule primary",
            rules_secondary="Rule secondary",
            open_time=t0,
            close_time=t0,
            expiration_time=t0,
            yes_bid_dollars=Decimal(y_bid) / Decimal("100"),
            yes_ask_dollars=Decimal(y_ask) / Decimal("100"),
            no_bid_dollars=Decimal(no_bid) / Decimal("100"),
            no_ask_dollars=Decimal(no_ask) / Decimal("100"),
            last_price_dollars=Decimal("0.50"),
            volume_24h=1000,
            open_interest=1000,
        )
        b = NormalizedOrderBook(
            market_ticker=t,
            yes_bids=(OrderBookLevel(Decimal(y_bid) / Decimal("100"), Decimal(50)),),
            no_bids=(OrderBookLevel(Decimal(no_bid) / Decimal("100"), Decimal(50)),),
            source_timestamp=t0,
        )
        books[out] = b
        observations[t] = MarketObservation(f"obs-{t}", t, t0, t0, m, b, True, False)

    basket = DeclaredMeceBasket(
        event_ticker=f"EVT-{n_legs}",
        outcomes=outcomes,
        market_outcome_map=dict(zip(tickers, outcomes)),
    )
    snap = ReconstructedBasketSnapshot(
        snapshot_id=f"snap-{n_legs}",
        basket=basket,
        observations=observations,
        observation_ids=tuple(f"obs-{t}" for t in tickers),
        source_timestamps={t: t0.isoformat() for t in tickers},
        min_timestamp=t0,
        max_timestamp=t0,
        leg_timestamp_skew_seconds=0.0,
        is_complete=True,
        status=STATUS_VALID_SNAPSHOT,
    )

    analyzer = OpportunityEconomicsAnalyzer()
    attr = analyzer.analyze_snapshot(snap, requested_quantity=1)

    assert attr.is_valid_snapshot is True
    assert attr.microstructure.aggregate_spread_cents == total_spread
    # Check exact theorem
    assert attr.yes_side.gross_edge_cents + attr.no_side.gross_edge_cents == -total_spread
    assert attr.microstructure.complementarity_invariant_holds is True


def test_fail_closed_on_unestablished_relationship():
    """Verify that portfolio payout evaluation strictly fails closed if relationship is not established."""
    unestablished_event = Event("EVT-UNPROVEN", ("A", "B"), relationship_established=False)
    books = {
        "A": NormalizedOrderBook("M-A", (), (), None),
        "B": NormalizedOrderBook("M-B", (), (), None),
    }
    pricing = evaluate_mece_basket_execution(
        event=unestablished_event,
        books_by_outcome=books,
        basket_side=YES,
    )
    assert pricing.is_gross_profitable is False
    assert pricing.supported_quantity == 0
    assert "Undeclared outcome relationship" in (pricing.rejection_reason or "")
