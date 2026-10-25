"""Kalshi Arbitrage Engine V13 — Historical Event Snapshot Reconstruction.

Provides deterministic reconstruction of event-level multi-market MECE basket
snapshots from historical observations recorded via sequential REST polling.

Governing Principles:
1. Determinism: Observations are sorted chronologically and keyed by ticker;
   identical inputs yield identical reconstructed snapshots.
2. Cycle Boundary Integrity: Snapshots are bounded by constituent ticker repetition
   and maximum cycle window duration. Observations from unrelated sampling cycles
   are never combined to artificially complete a basket.
3. Strict Fail-Closed Validation: Incomplete snapshots, stale legs, transport errors,
   missing source timestamps, and excessive timestamp skew fail closed with explicit
   diagnostic rejection codes.
4. Non-Contamination: Multiple declared baskets evaluate independently without
   cross-contaminating constituent observations.
5. Deduplication: Duplicate observation IDs are tracked, pruned, and reported.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
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

from .contract import YES
from .evaluation import _normalize_timestamp
from .observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
    STATUS_REJECTED_INSUFFICIENT_DEPTH,
    STATUS_REJECTED_LEG_SKEW,
    STATUS_REJECTED_MISSING_MARKET,
    STATUS_REJECTED_MISSING_TIMESTAMP,
    STATUS_REJECTED_NO_GROSS_EDGE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_UNPROFITABLE_FEES,
)

STATUS_VALID_SNAPSHOT = "VALID_SNAPSHOT"


@dataclass(frozen=True)
class SnapshotReconstructionConfig:
    """Configuration for historical basket snapshot reconstruction."""

    max_leg_timestamp_skew_seconds: float = 5.0
    cycle_window_seconds: float = 15.0
    max_stale_seconds: float = 60.0
    require_source_timestamp: bool = True

    def __post_init__(self) -> None:
        if self.max_leg_timestamp_skew_seconds <= 0:
            raise ValueError("max_leg_timestamp_skew_seconds must be positive")
        if self.cycle_window_seconds <= 0:
            raise ValueError("cycle_window_seconds must be positive")
        if self.max_stale_seconds <= 0:
            raise ValueError("max_stale_seconds must be positive")


@dataclass(frozen=True)
class ReconstructedBasketSnapshot:
    """An event-level reconstructed snapshot across constituent basket legs."""

    snapshot_id: str
    basket: DeclaredMeceBasket
    observations: Mapping[str, MarketObservation]  # ticker -> MarketObservation
    observation_ids: Tuple[str, ...]
    source_timestamps: Mapping[str, Optional[str]]
    min_timestamp: Optional[datetime]
    max_timestamp: Optional[datetime]
    leg_timestamp_skew_seconds: float
    is_complete: bool
    status: str
    rejection_reason: Optional[str] = None
    missing_tickers: Tuple[str, ...] = ()
    stale_tickers: Tuple[str, ...] = ()
    errored_tickers: Tuple[str, ...] = ()
    market_state_fingerprint: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """True if snapshot is complete and meets all synchronization/freshness criteria."""
        return self.status == STATUS_VALID_SNAPSHOT


def _effective_timestamp(obs: MarketObservation) -> Optional[datetime]:
    """Extract normalized effective timestamp (preferring source_timestamp)."""
    if obs.source_timestamp is not None:
        return _normalize_timestamp(obs.source_timestamp)
    if obs.order_book and obs.order_book.source_timestamp is not None:
        return _normalize_timestamp(obs.order_book.source_timestamp)
    return _normalize_timestamp(obs.observed_at)


def _compute_market_state_fingerprint(
    group: Dict[str, MarketObservation],
    ordered_tickers: Sequence[str],
) -> str:
    """Compute deterministic cryptographic fingerprint of constituent order books."""
    h = hashlib.sha256()
    for t in ordered_tickers:
        obs = group.get(t)
        if obs and obs.order_book:
            yes_repr = ",".join(f"{b.price_dollars}:{b.quantity}" for b in obs.order_book.yes_bids)
            no_repr = ",".join(f"{b.price_dollars}:{b.quantity}" for b in obs.order_book.no_bids)
            h.update(f"{t}|yes:{yes_repr}|no:{no_repr}\n".encode("utf-8"))
        else:
            h.update(f"{t}|none\n".encode("utf-8"))
    return h.hexdigest()


def _finalize_group(
    group: Dict[str, MarketObservation],
    basket: DeclaredMeceBasket,
    config: SnapshotReconstructionConfig,
    snapshot_index: int,
) -> ReconstructedBasketSnapshot:
    """Finalize a grouped candidate window into a validated or rejected snapshot."""
    target_tickers = set(basket.ordered_tickers)
    observed_tickers = set(group.keys())
    missing = tuple(sorted(target_tickers - observed_tickers))

    errored = tuple(
        sorted(
            t
            for t, o in group.items()
            if not o.is_success or o.market is None or o.order_book is None
        )
    )
    stale = tuple(
        sorted(
            t
            for t, o in group.items()
            if o.is_stale
        )
    )

    source_ts_map: Dict[str, Optional[str]] = {}
    ts_list: List[datetime] = []
    missing_source_ts: List[str] = []

    for t in basket.ordered_tickers:
        obs = group.get(t)
        if obs is None:
            source_ts_map[t] = None
            continue
        raw_src = obs.source_timestamp if obs.source_timestamp is not None else (obs.order_book.source_timestamp if obs.order_book else None)
        if raw_src is not None:
            norm_src = _normalize_timestamp(raw_src)
            source_ts_map[t] = norm_src.isoformat() if norm_src else None
        else:
            source_ts_map[t] = None
            missing_source_ts.append(t)

        ts = _effective_timestamp(obs)
        if ts is not None:
            ts_list.append(ts)

    min_ts = min(ts_list) if ts_list else None
    max_ts = max(ts_list) if ts_list else None
    skew_s = (max_ts - min_ts).total_seconds() if (min_ts and max_ts) else 0.0

    obs_ids = tuple(group[t].observation_id for t in basket.ordered_tickers if t in group)
    snap_id = f"snap-{basket.event_ticker}-{snapshot_index:05d}"

    # Funnel validation
    if missing:
        return ReconstructedBasketSnapshot(
            snapshot_id=snap_id,
            basket=basket,
            observations=dict(group),
            observation_ids=obs_ids,
            source_timestamps=source_ts_map,
            min_timestamp=min_ts,
            max_timestamp=max_ts,
            leg_timestamp_skew_seconds=skew_s,
            is_complete=False,
            status=STATUS_REJECTED_MISSING_MARKET,
            rejection_reason=f"Incomplete basket snapshot: missing leg(s) {list(missing)}",
            missing_tickers=missing,
            stale_tickers=stale,
            errored_tickers=errored,
        )

    if errored:
        return ReconstructedBasketSnapshot(
            snapshot_id=snap_id,
            basket=basket,
            observations=dict(group),
            observation_ids=obs_ids,
            source_timestamps=source_ts_map,
            min_timestamp=min_ts,
            max_timestamp=max_ts,
            leg_timestamp_skew_seconds=skew_s,
            is_complete=True,
            status=STATUS_REJECTED_MISSING_MARKET,
            rejection_reason=f"Failed observation on leg(s): {list(errored)}",
            missing_tickers=(),
            stale_tickers=stale,
            errored_tickers=errored,
        )

    if stale:
        return ReconstructedBasketSnapshot(
            snapshot_id=snap_id,
            basket=basket,
            observations=dict(group),
            observation_ids=obs_ids,
            source_timestamps=source_ts_map,
            min_timestamp=min_ts,
            max_timestamp=max_ts,
            leg_timestamp_skew_seconds=skew_s,
            is_complete=True,
            status=STATUS_REJECTED_STALE,
            rejection_reason=f"Stale leg(s): {list(stale)}",
            missing_tickers=(),
            stale_tickers=stale,
            errored_tickers=(),
        )

    if config.require_source_timestamp and missing_source_ts:
        return ReconstructedBasketSnapshot(
            snapshot_id=snap_id,
            basket=basket,
            observations=dict(group),
            observation_ids=obs_ids,
            source_timestamps=source_ts_map,
            min_timestamp=min_ts,
            max_timestamp=max_ts,
            leg_timestamp_skew_seconds=skew_s,
            is_complete=True,
            status=STATUS_REJECTED_MISSING_TIMESTAMP,
            rejection_reason=f"Legs missing source_timestamp: {missing_source_ts}",
            missing_tickers=(),
            stale_tickers=(),
            errored_tickers=(),
        )

    if skew_s > config.max_leg_timestamp_skew_seconds:
        return ReconstructedBasketSnapshot(
            snapshot_id=snap_id,
            basket=basket,
            observations=dict(group),
            observation_ids=obs_ids,
            source_timestamps=source_ts_map,
            min_timestamp=min_ts,
            max_timestamp=max_ts,
            leg_timestamp_skew_seconds=skew_s,
            is_complete=True,
            status=STATUS_REJECTED_LEG_SKEW,
            rejection_reason=(
                f"Excessive leg timestamp skew: {skew_s:.2f}s > max {config.max_leg_timestamp_skew_seconds:.2f}s"
            ),
            missing_tickers=(),
            stale_tickers=(),
            errored_tickers=(),
        )

    fingerprint = _compute_market_state_fingerprint(group, basket.ordered_tickers)

    return ReconstructedBasketSnapshot(
        snapshot_id=snap_id,
        basket=basket,
        observations=dict(group),
        observation_ids=obs_ids,
        source_timestamps=source_ts_map,
        min_timestamp=min_ts,
        max_timestamp=max_ts,
        leg_timestamp_skew_seconds=skew_s,
        is_complete=True,
        status=STATUS_VALID_SNAPSHOT,
        rejection_reason=None,
        missing_tickers=(),
        stale_tickers=(),
        errored_tickers=(),
        market_state_fingerprint=fingerprint,
    )


class EventSnapshotReconstructor:
    """Deterministic event-level basket snapshot reconstruction engine."""

    def __init__(self, config: Optional[SnapshotReconstructionConfig] = None) -> None:
        self.config = config or SnapshotReconstructionConfig()
        self.last_unmatched_baskets: Tuple[str, ...] = ()

    def reconstruct(
        self,
        observations: Sequence[MarketObservation],
        baskets: Sequence[DeclaredMeceBasket],
    ) -> Tuple[List[ReconstructedBasketSnapshot], int]:
        """Reconstruct event snapshots from recorded observations for each basket.

        Returns:
            Tuple of (reconstructed_snapshots, duplicate_observations_count)
        """
        if not baskets:
            self.last_unmatched_baskets = ()
            return [], 0

        all_snapshots: List[ReconstructedBasketSnapshot] = []
        total_duplicates = 0
        unmatched_baskets: List[str] = []

        for basket in baskets:
            target_tickers = set(basket.ordered_tickers)

            # 1. Filter observations for this basket
            relevant_obs: List[MarketObservation] = []
            seen_obs_ids: Set[str] = set()

            for obs in observations:
                if obs is None or not obs.ticker:
                    continue
                if obs.ticker in target_tickers:
                    # Enforce event ticker match if both observation market and basket specify it
                    if (
                        obs.market
                        and obs.market.event_ticker
                        and basket.event_ticker
                        and obs.market.event_ticker != basket.event_ticker
                    ):
                        continue
                    if obs.observation_id in seen_obs_ids:
                        total_duplicates += 1
                        continue
                    seen_obs_ids.add(obs.observation_id)
                    relevant_obs.append(obs)

            if not relevant_obs:
                unmatched_baskets.append(basket.basket_id or basket.event_ticker)

            # 2. Sort chronologically by (effective_ts, obs_id)
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            sorted_obs = sorted(
                relevant_obs,
                key=lambda o: (_effective_timestamp(o) or epoch, o.observation_id),
            )

            # 3. Group chronologically into snapshot cycles
            current_group: Dict[str, MarketObservation] = {}
            basket_snap_index = 0

            for obs in sorted_obs:
                t = obs.ticker
                obs_ts = _effective_timestamp(obs)

                if not current_group:
                    current_group[t] = obs
                    continue

                # Check window break condition
                group_timestamps = [
                    _effective_timestamp(o)
                    for o in current_group.values()
                    if _effective_timestamp(o) is not None
                ]
                min_group_ts = min(group_timestamps) if group_timestamps else None
                time_delta = (
                    (obs_ts - min_group_ts).total_seconds()
                    if (obs_ts and min_group_ts)
                    else 0.0
                )

                if t in current_group or time_delta > self.config.cycle_window_seconds:
                    basket_snap_index += 1
                    all_snapshots.append(
                        _finalize_group(current_group, basket, self.config, basket_snap_index)
                    )
                    current_group = {t: obs}
                else:
                    current_group[t] = obs
                    if len(current_group) == len(target_tickers):
                        basket_snap_index += 1
                        all_snapshots.append(
                            _finalize_group(current_group, basket, self.config, basket_snap_index)
                        )
                        current_group = {}

            if current_group:
                basket_snap_index += 1
                all_snapshots.append(
                    _finalize_group(current_group, basket, self.config, basket_snap_index)
                )

        self.last_unmatched_baskets = tuple(unmatched_baskets)
        return all_snapshots, total_duplicates
