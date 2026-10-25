"""Kalshi Arbitrage Engine V17 — Broad-Market Multi-Event Discovery & Prospective Trial CLI.

Executes:
1. Automated broad-market event discovery and deterministic MECE classification across Kalshi catalog.
2. Selection and prioritization of a multi-event research universe spanning diverse market series.
3. Bounded concurrent multi-event prospective market-data collection.
4. Strict event-isolated basket snapshot reconstruction.
5. Deterministic fee-aware MECE arbitrage evaluation via HistoricalBasketReplayEngine.
6. Temporal opportunity episode tracking.
7. Cryptographic SHA-256 evidence integrity manifest generation.

Strictly read-only; never submits, amends, or cancels exchange orders.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_arbitrage.discovery import (
    BroadMarketDiscoveryEngine,
    DiscoveryConfig,
    DiscoveryReport,
)
from kalshi_arbitrage.episode_tracker import (
    OpportunityEpisodeTracker,
)
from kalshi_arbitrage.evidence_storage import observation_to_dict
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    MarketObservation,
    STATUS_QUALIFIED,
    load_declared_baskets,
)
from kalshi_arbitrage.rest_client import KalshiRestClient
from kalshi_arbitrage.snapshot_reconstruction import (
    EventSnapshotReconstructor,
    ReconstructedBasketSnapshot,
    SnapshotReconstructionConfig,
)
from kalshi_arbitrage.v13_basket_replay import (
    HistoricalBasketReplayConfig,
    HistoricalBasketReplayEngine,
    HistoricalBasketReplayRecord,
    HistoricalBasketReplaySummary,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("collect_v17")


def snapshot_to_dict(snap: ReconstructedBasketSnapshot) -> Dict[str, Any]:
    """Convert reconstructed snapshot to JSON-serializable dictionary."""
    return {
        "snapshot_id": snap.snapshot_id,
        "event_ticker": snap.basket.event_ticker,
        "basket_id": snap.basket.basket_id,
        "ordered_tickers": list(snap.basket.ordered_tickers),
        "observation_ids": list(snap.observation_ids),
        "source_timestamps": dict(snap.source_timestamps),
        "min_timestamp": snap.min_timestamp.isoformat() if snap.min_timestamp else None,
        "max_timestamp": snap.max_timestamp.isoformat() if snap.max_timestamp else None,
        "leg_timestamp_skew_seconds": round(snap.leg_timestamp_skew_seconds, 4),
        "is_complete": snap.is_complete,
        "is_valid": snap.is_valid,
        "status": snap.status,
        "rejection_reason": snap.rejection_reason,
        "missing_tickers": list(snap.missing_tickers),
        "stale_tickers": list(snap.stale_tickers),
        "errored_tickers": list(snap.errored_tickers),
        "market_state_fingerprint": snap.market_state_fingerprint,
    }


def fetch_single_market_obs(
    client: KalshiRestClient,
    ticker: str,
    session_id: str,
) -> MarketObservation:
    """Concurrently fetch market metadata and order book with provenance metadata."""
    try:
        m = client.get_market(ticker)
        b = client.get_order_book(ticker)
        t_recv = datetime.now(timezone.utc)
        return MarketObservation(
            observation_id=f"obs-{ticker}-{int(t_recv.timestamp())}-{t_recv.microsecond}",
            ticker=ticker,
            observed_at=t_recv,
            source_timestamp=b.source_timestamp,
            market=m,
            order_book=b,
            is_success=True,
            is_stale=False,
            source_transport="REST_CONCURRENT_V17",
            local_receive_time=t_recv,
            sync_state="SYNCHRONIZED",
            session_id=session_id,
        )
    except Exception as exc:
        t_err = datetime.now(timezone.utc)
        logger.warning("Error fetching market %s: %s", ticker, exc)
        return MarketObservation(
            observation_id=f"err-{ticker}-{int(t_err.timestamp())}",
            ticker=ticker,
            observed_at=t_err,
            source_timestamp=None,
            market=None,
            order_book=None,
            is_success=False,
            is_stale=False,
            error_message=str(exc),
            source_transport="REST_CONCURRENT_V17",
            local_receive_time=t_err,
            sync_state="FAILED",
            session_id=session_id,
        )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Kalshi V17 Broad-Market Discovery & Trial CLI")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="evidence_v17_broad_market_trial",
        help="Output directory for V17 trial evidence",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=10,
        help="Number of collection cycles (default: 10)",
    )
    parser.add_argument(
        "--cycle-interval",
        type=float,
        default=2.0,
        help="Interval between collection cycles in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--max-events-to-scan",
        type=int,
        default=60,
        help="Maximum events to scan from Kalshi catalog (default: 60)",
    )
    parser.add_argument(
        "--max-selected-events",
        type=int,
        default=4,
        help="Maximum candidate events to select for prospective trial (default: 4)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Concurrent worker thread count (default: 8)",
    )
    parser.add_argument(
        "--target-quantity",
        type=int,
        default=1,
        help="Target contract quantity for evaluation (default: 1)",
    )
    parser.add_argument(
        "--baskets",
        type=str,
        default=None,
        help="Optional path to custom declared baskets JSON",
    )
    args = parser.parse_args()

    out_path = Path(args.output_dir)
    if out_path.exists() and any(out_path.iterdir()):
        logger.error("Output directory %s already exists and is not empty. Aborting.", out_path)
        return 1
    out_path.mkdir(parents=True, exist_ok=True)

    session_id = f"v17-trial-{int(datetime.now(timezone.utc).timestamp())}"
    logger.info("Initializing Kalshi REST client and broad discovery engine...")
    client = KalshiRestClient()

    # Phase 1: Automated Broad-Market Discovery
    logger.info("Scanning public events for MECE baskets (scan limit=%d)...", args.max_events_to_scan)
    disc_cfg = DiscoveryConfig(
        max_events_to_scan=args.max_events_to_scan,
        concurrency=args.concurrency,
    )
    engine = BroadMarketDiscoveryEngine(client, disc_cfg)
    report = engine.discover_and_classify()

    logger.info(
        "Discovery complete: scanned %d events, discovered %d markets, validated %d MECE baskets (%d observable)",
        report.scanned_events_count,
        report.discovered_markets_count,
        len(report.validated_baskets),
        len(report.observable_baskets),
    )

    # Save discovery report
    disc_report_path = out_path / "universe_discovery_report.json"
    disc_report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    # Phase 2: Assemble Multi-Event Universe
    selected_baskets: List[DeclaredMeceBasket] = []
    if args.baskets:
        custom_file = Path(args.baskets)
        selected_baskets = load_declared_baskets(custom_file)
        logger.info("Loaded %d baskets from custom file %s", len(selected_baskets), custom_file)
    else:
        # Prioritize discovered observable MECE baskets
        seen_events: Set[str] = set()
        for p in report.observable_baskets:
            if p.declared_basket and p.event_ticker not in seen_events:
                selected_baskets.append(p.declared_basket)
                seen_events.add(p.event_ticker)
            if len(selected_baskets) >= args.max_selected_events:
                break

        # Also incorporate verified baseline Fed decision baskets to form a diverse multi-event universe
        baseline_file = Path("configs/baskets/validated_mece_universe.json")
        if baseline_file.exists():
            for b in load_declared_baskets(baseline_file):
                if b.event_ticker not in seen_events and len(selected_baskets) < args.max_selected_events:
                    selected_baskets.append(b)
                    seen_events.add(b.event_ticker)

    if not selected_baskets:
        logger.error("No valid MECE baskets available for prospective trial. Aborting.")
        return 1

    logger.info("Selected %d candidate MECE baskets for prospective experiment:", len(selected_baskets))
    all_tickers: List[str] = []
    for b in selected_baskets:
        logger.info("  - Basket %s (%s): %d legs", b.basket_id, b.event_ticker, len(b.ordered_tickers))
        for t in b.ordered_tickers:
            if t not in all_tickers:
                all_tickers.append(t)
    logger.info("Total unique constituent market tickers across all events: %d", len(all_tickers))

    # Phase 3: Prospective Multi-Event Data Collection
    logger.info("Starting %d prospective collection cycles (concurrency=%d)...", args.cycles, args.concurrency)
    obs_file = out_path / "observations.jsonl"
    all_observations: List[MarketObservation] = []

    with open(obs_file, "w", encoding="utf-8") as f_obs:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            for cycle_idx in range(args.cycles):
                c_start = datetime.now(timezone.utc)
                future_to_ticker = {
                    executor.submit(fetch_single_market_obs, client, t, session_id): t
                    for t in all_tickers
                }
                cycle_obs: List[MarketObservation] = []
                for fut in as_completed(future_to_ticker):
                    cycle_obs.append(fut.result())

                for obs in cycle_obs:
                    all_observations.append(obs)
                    f_obs.write(json.dumps(observation_to_dict(obs)) + "\n")

                c_elapsed = (datetime.now(timezone.utc) - c_start).total_seconds()
                logger.info(
                    "Cycle %d/%d complete: %d observations fetched in %.2fs",
                    cycle_idx + 1,
                    args.cycles,
                    len(cycle_obs),
                    c_elapsed,
                )

                if cycle_idx < args.cycles - 1:
                    time.sleep(args.cycle_interval)

    # Phase 4: Snapshot Reconstruction with Strict Isolation
    logger.info("Reconstructing event snapshots across %d total observations...", len(all_observations))
    reconstructor = EventSnapshotReconstructor(
        SnapshotReconstructionConfig(
            max_leg_timestamp_skew_seconds=5.0,
            cycle_window_seconds=15.0,
        )
    )
    snapshots, dup_count = reconstructor.reconstruct(all_observations, selected_baskets)
    valid_snapshots = [s for s in snapshots if s.is_valid]
    logger.info(
        "Reconstructed %d snapshots (%d valid, %d rejected, %d duplicate obs pruned)",
        len(snapshots),
        len(valid_snapshots),
        len(snapshots) - len(valid_snapshots),
        dup_count,
    )

    snap_file = out_path / "snapshots.jsonl"
    with open(snap_file, "w", encoding="utf-8") as f_snap:
        for s in snapshots:
            f_snap.write(json.dumps(snapshot_to_dict(s)) + "\n")

    # Phase 5: Deterministic Arbitrage Evaluation via Replay Engine
    logger.info("Evaluating baskets through HistoricalBasketReplayEngine...")
    replay_cfg = HistoricalBasketReplayConfig(
        target_quantity=args.target_quantity,
        max_leg_timestamp_skew_seconds=5.0,
    )
    replayer = HistoricalBasketReplayEngine(replay_cfg)
    summary = replayer.replay(all_observations, selected_baskets)
    records = summary.records

    eval_file = out_path / "basket_evaluations.jsonl"
    with open(eval_file, "w", encoding="utf-8") as f_eval:
        for rec in records:
            f_eval.write(json.dumps(rec.to_dict()) + "\n")

    # Phase 6: Temporal Opportunity Episode Tracking
    tracker = OpportunityEpisodeTracker()
    skews: List[float] = []
    gross_edges: List[int] = []
    net_edges: List[int] = []
    rejection_breakdown: Dict[str, int] = {}
    qualified_count = 0

    for rec in records:
        if rec.status == STATUS_QUALIFIED:
            qualified_count += 1
        rejection_breakdown[rec.status] = rejection_breakdown.get(rec.status, 0) + 1
        if rec.leg_timestamp_skew_seconds is not None:
            skews.append(rec.leg_timestamp_skew_seconds)
        if rec.gross_edge_cents is not None:
            gross_edges.append(int(rec.gross_edge_cents))
        if rec.net_edge_cents is not None:
            net_edges.append(int(rec.net_edge_cents))

        dt = (
            datetime.fromisoformat(rec.snapshot_max_timestamp)
            if rec.snapshot_max_timestamp
            else datetime.now(timezone.utc)
        )
        tracker.record_candidate(
            basket_id=rec.basket_id or rec.event_ticker,
            strategy_type="OPPORTUNITY_MECE_BASKET_LONG_YES",
            observed_at=dt,
            is_qualified=rec.is_qualified,
            gross_edge_cents=int(rec.gross_edge_cents or 0),
            net_edge_cents=int(rec.net_edge_cents or 0),
            supported_quantity=int(rec.common_executable_quantity or 0),
            rejection_reason=rec.status,
            market_state_fingerprint=rec.market_state_fingerprint,
        )

    tracker.finalize_all(datetime.now(timezone.utc))
    episodes_file = out_path / "episodes.jsonl"
    with open(episodes_file, "w", encoding="utf-8") as f_ep:
        for ep in tracker.completed_episodes:
            f_ep.write(json.dumps(ep.to_dict()) + "\n")

    # Phase 7: Compute Summary Metrics
    total_snaps = summary.complete_snapshots_count
    unique_cnt = summary.unique_market_states_count
    repeated_cnt = summary.repeated_market_states_count
    indep_ratio = (unique_cnt / total_snaps) if total_snaps > 0 else 0.0
    mean_skew = (sum(skews) / len(skews)) if skews else 0.0
    max_skew = max(skews) if skews else 0.0

    trial_summary = {
        "experiment": "Kalshi V17 Broad-Market Opportunity Discovery & Validation Trial",
        "session_id": session_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "cycles": args.cycles,
        "cycle_interval_seconds": args.cycle_interval,
        "selected_baskets_count": len(selected_baskets),
        "selected_events": [b.event_ticker for b in selected_baskets],
        "total_monitored_markets": len(all_tickers),
        "total_observations": len(all_observations),
        "total_snapshots": len(snapshots),
        "complete_snapshots": total_snaps,
        "valid_snapshots": len(valid_snapshots),
        "unique_market_states": unique_cnt,
        "repeated_market_states": repeated_cnt,
        "state_independence_ratio": round(indep_ratio, 4),
        "mean_leg_skew_seconds": round(mean_skew, 3),
        "max_leg_skew_seconds": round(max_skew, 3),
        "gross_edge_min_cents": min(gross_edges) if gross_edges else None,
        "gross_edge_max_cents": max(gross_edges) if gross_edges else None,
        "net_edge_min_cents": min(net_edges) if net_edges else None,
        "net_edge_max_cents": max(net_edges) if net_edges else None,
        "qualified_candidates_count": qualified_count,
        "distinct_opportunity_episodes": len(tracker.completed_episodes),
        "rejection_breakdown": rejection_breakdown,
    }

    summary_file = out_path / "trial_summary.json"
    summary_file.write_text(json.dumps(trial_summary, indent=2), encoding="utf-8")

    # Phase 8: Manifest Generation
    manifest_files = {}
    for p in [disc_report_path, obs_file, snap_file, eval_file, episodes_file, summary_file]:
        manifest_files[p.name] = {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "size_bytes": p.stat().st_size,
        }

    manifest = {
        "manifest_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "experiment": "V17 Broad-Market Discovery & Prospective Validation",
        "files": manifest_files,
        "summary": trial_summary,
    }

    manifest_file = out_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    logger.info("V17 Prospective Trial successfully completed!")
    logger.info("Trial summary: %s", json.dumps(trial_summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
