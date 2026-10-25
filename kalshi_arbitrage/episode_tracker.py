"""Kalshi Arbitrage Engine V16 — Deterministic Opportunity Episode Tracker.

Reconstructs, tracks, and analyzes continuous opportunity episodes across repeated
market-state observations and streaming updates.

Governing Principles:
1. Distinct Episodes vs Repeated States: Continuously observed resting candidate states
   belong to one evolving episode; repeated updates do not artificially inflate candidate counts.
2. Explicit Episode Lifecycle: Tracks crisp state transitions (OPEN, UPDATED, CLOSED)
   with deterministic start, gap timeout, and closure rules.
3. Interruption Sensitivity: Disconnects, sequence gaps, stale quotes, or missing depth
   cleanly terminate an active episode rather than bridging across corrupted periods.
4. Range & Depth Invariant Tracking: Tracks min/max gross edge, net edge, and executable depth
   over the lifetime of the episode.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
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

EPISODE_STATE_OPEN = "OPEN"
EPISODE_STATE_UPDATED = "UPDATED"
EPISODE_STATE_CLOSED = "CLOSED"

CLOSURE_REASON_NO_EDGE = "NO_GROSS_EDGE"
CLOSURE_REASON_FEES = "UNPROFITABLE_FEES"
CLOSURE_REASON_INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
CLOSURE_REASON_STALE = "STALE_DATA"
CLOSURE_REASON_TIMEOUT = "INACTIVITY_TIMEOUT"
CLOSURE_REASON_UNSYNCHRONIZED = "UNSYNCHRONIZED_STREAM"
CLOSURE_REASON_SESSION_END = "SESSION_END"


@dataclass(frozen=True)
class EpisodeTrackerConfig:
    """Configuration for episode boundary detection and timeout."""

    inactivity_timeout_seconds: float = 60.0
    require_unbroken_qualification: bool = True

    def __post_init__(self) -> None:
        if self.inactivity_timeout_seconds <= 0:
            raise ValueError("inactivity_timeout_seconds must be positive")


@dataclass
class OpportunityEpisode:
    """An evolving or finalized temporal episode of an arbitrage opportunity."""

    episode_id: str
    basket_id: str
    strategy_type: str
    start_time: datetime
    last_seen_time: datetime
    end_time: Optional[datetime] = None
    state: str = EPISODE_STATE_OPEN
    closure_reason: Optional[str] = None
    observation_count: int = 1
    repeated_state_count: int = 0
    min_gross_edge_cents: int = 0
    max_gross_edge_cents: int = 0
    min_net_edge_cents: int = 0
    max_net_edge_cents: int = 0
    min_supported_quantity: int = 0
    max_supported_quantity: int = 0
    market_state_fingerprints: List[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or self.last_seen_time
        return max(0.0, (end - self.start_time).total_seconds())

    @property
    def is_closed(self) -> bool:
        return self.state == EPISODE_STATE_CLOSED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "basket_id": self.basket_id,
            "strategy_type": self.strategy_type,
            "start_time": self.start_time.isoformat(),
            "last_seen_time": self.last_seen_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": self.duration_seconds,
            "state": self.state,
            "closure_reason": self.closure_reason,
            "observation_count": self.observation_count,
            "repeated_state_count": self.repeated_state_count,
            "min_gross_edge_cents": self.min_gross_edge_cents,
            "max_gross_edge_cents": self.max_gross_edge_cents,
            "min_net_edge_cents": self.min_net_edge_cents,
            "max_net_edge_cents": self.max_net_edge_cents,
            "min_supported_quantity": self.min_supported_quantity,
            "max_supported_quantity": self.max_supported_quantity,
            "unique_fingerprints_count": len(set(self.market_state_fingerprints)),
        }


class OpportunityEpisodeTracker:
    """Tracks and reconstructs continuous opportunity episodes."""

    def __init__(self, config: Optional[EpisodeTrackerConfig] = None) -> None:
        self.config = config or EpisodeTrackerConfig()
        self._active_episodes: Dict[str, OpportunityEpisode] = {}  # key -> episode
        self._completed_episodes: List[OpportunityEpisode] = []

    @property
    def active_episodes(self) -> Mapping[str, OpportunityEpisode]:
        return dict(self._active_episodes)

    @property
    def completed_episodes(self) -> Tuple[OpportunityEpisode, ...]:
        return tuple(self._completed_episodes)

    def _make_key(self, basket_id: str, strategy_type: str) -> str:
        return f"{basket_id}:{strategy_type}"

    def record_candidate(
        self,
        basket_id: str,
        strategy_type: str,
        observed_at: datetime,
        is_qualified: bool,
        gross_edge_cents: int,
        net_edge_cents: int,
        supported_quantity: int,
        rejection_reason: Optional[str] = None,
        market_state_fingerprint: Optional[str] = None,
    ) -> Optional[OpportunityEpisode]:
        """Record an evaluated candidate and update episode lifecycle."""
        key = self._make_key(basket_id, strategy_type)
        active = self._active_episodes.get(key)

        if is_qualified:
            if active is not None:
                gap = (observed_at - active.last_seen_time).total_seconds()
                if gap <= self.config.inactivity_timeout_seconds:
                    # Continue existing episode
                    active.observation_count += 1
                    active.last_seen_time = observed_at
                    active.state = EPISODE_STATE_UPDATED

                    # Check fingerprint repetition
                    if market_state_fingerprint and market_state_fingerprint in active.market_state_fingerprints:
                        active.repeated_state_count += 1
                    if market_state_fingerprint:
                        active.market_state_fingerprints.append(market_state_fingerprint)

                    active.min_gross_edge_cents = min(active.min_gross_edge_cents, gross_edge_cents)
                    active.max_gross_edge_cents = max(active.max_gross_edge_cents, gross_edge_cents)
                    active.min_net_edge_cents = min(active.min_net_edge_cents, net_edge_cents)
                    active.max_net_edge_cents = max(active.max_net_edge_cents, net_edge_cents)
                    active.min_supported_quantity = min(active.min_supported_quantity, supported_quantity)
                    active.max_supported_quantity = max(active.max_supported_quantity, supported_quantity)
                    return active
                else:
                    # Inactivity timeout expired; finalize previous episode
                    self._close_episode(active, observed_at, CLOSURE_REASON_TIMEOUT)

            # Start new episode
            ep_id = f"ep-{basket_id}-{int(observed_at.timestamp())}"
            new_ep = OpportunityEpisode(
                episode_id=ep_id,
                basket_id=basket_id,
                strategy_type=strategy_type,
                start_time=observed_at,
                last_seen_time=observed_at,
                state=EPISODE_STATE_OPEN,
                min_gross_edge_cents=gross_edge_cents,
                max_gross_edge_cents=gross_edge_cents,
                min_net_edge_cents=net_edge_cents,
                max_net_edge_cents=net_edge_cents,
                min_supported_quantity=supported_quantity,
                max_supported_quantity=supported_quantity,
                market_state_fingerprints=[market_state_fingerprint] if market_state_fingerprint else [],
            )
            self._active_episodes[key] = new_ep
            return new_ep
        else:
            # Candidate did not qualify
            if active is not None:
                # Intervening non-qualified state terminates active episode
                closure = self._map_rejection_to_closure(rejection_reason)
                self._close_episode(active, observed_at, closure)
            return None

    def _map_rejection_to_closure(self, rejection_reason: Optional[str]) -> str:
        if not rejection_reason:
            return CLOSURE_REASON_NO_EDGE
        r_upper = rejection_reason.upper()
        if "GROSS" in r_upper:
            return CLOSURE_REASON_NO_EDGE
        if "FEE" in r_upper:
            return CLOSURE_REASON_FEES
        if "DEPTH" in r_upper or "QUANTITY" in r_upper:
            return CLOSURE_REASON_INSUFFICIENT_DEPTH
        if "STALE" in r_upper:
            return CLOSURE_REASON_STALE
        if "SYNC" in r_upper or "SKEW" in r_upper:
            return CLOSURE_REASON_UNSYNCHRONIZED
        return rejection_reason

    def _close_episode(
        self, episode: OpportunityEpisode, end_time: datetime, reason: str
    ) -> None:
        episode.end_time = end_time
        episode.state = EPISODE_STATE_CLOSED
        episode.closure_reason = reason
        key = self._make_key(episode.basket_id, episode.strategy_type)
        self._active_episodes.pop(key, None)
        self._completed_episodes.append(episode)

    def finalize_all(
        self, session_end_time: datetime, reason: str = CLOSURE_REASON_SESSION_END
    ) -> Tuple[OpportunityEpisode, ...]:
        """Finalize all remaining open episodes at session termination."""
        for ep in list(self._active_episodes.values()):
            self._close_episode(ep, session_end_time, reason)
        return tuple(self._completed_episodes)
