"""Kalshi Arbitrage Engine V18 — Two-Sided Multi-Universe Economic Trial CLI.

Executes a bounded, read-only multi-universe trial comparing two distinct research cohorts:
- Cohort A (Broad-Market / Long-Horizon & Tech):
    - KXDEELRIP-40 (Pairwise IPO race, 2 legs)
    - KXGDPYEAR-35 (GDP growth continuous partition, 14 legs)
    - KXGDPYEAR-34 (GDP growth continuous partition, 14 legs)
    Total: 30 markets across 3 events.
- Cohort B (Monetary Policy Benchmark MECE):
    - KXFEDDECISION-26OCT (FOMC rate decision partition, 5 legs)
    - KXFEDDECISION-26DEC (FOMC rate decision partition, 5 legs)
    - KXFEDDECISION-27DEC (FOMC rate decision partition, 5 legs)
    Total: 15 markets across 3 events.

Analyzes:
1. Two-sided payout and cost structures (Long YES and Long NO).
2. Theoretical complementarity invariant: Gross Edge (YES) + Gross Edge (NO) == -Aggregate Spread.
3. Microstructure spreads, depth limits, and fee erosion.
4. Cryptographic SHA-256 evidence integrity manifest.

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

from kalshi_arbitrage.attribution import (
    EconomicAttributionCategory,
    OpportunityEconomicsAnalyzer,
    SnapshotEconomicAttribution,
)
from kalshi_arbitrage.contract import NO, YES
from kalshi_arbitrage.discovery import (
    BroadMarketDiscoveryEngine,
    DiscoveryConfig,
)
from kalshi_arbitrage.evidence_storage import observation_to_dict
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.observer import (
    DeclaredMeceBasket,
    MarketObservation,
)
from kalshi_arbitrage.rest_client import KalshiRestClient
from kalshi_arbitrage.snapshot_reconstruction import (
    EventSnapshotReconstructor,
    ReconstructedBasketSnapshot,
    SnapshotReconstructionConfig,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("evaluate_v18")


def fetch_single_ticker(
    client: KalshiRestClient,
    ticker: str,
    session_id: str,
) -> MarketObservation:
    """Fetch order book and metadata for a single ticker."""
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
            source_transport="REST_CONCURRENT_V18",
            local_receive_time=t_recv,
            sync_state="SYNCHRONIZED",
            session_id=session_id,
        )
    except Exception as exc:
        t_err = datetime.now(timezone.utc)
        logger.warning("Error fetching ticker %s: %s", ticker, exc)
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
            source_transport="REST_CONCURRENT_V18",
            local_receive_time=t_err,
            sync_state="FAILED",
            session_id=session_id,
        )


def build_baskets_for_events(
    client: KalshiRestClient,
    event_tickers: Sequence[str],
) -> List[DeclaredMeceBasket]:
    """Dynamically construct validated DeclaredMeceBasket instances for specified events."""
    baskets = []
    for evt in event_tickers:
        res, _ = client._get_json_response(f"/events/{evt}", {"with_nested_markets": "true"})
        markets = res.get("event", {}).get("markets", [])
        active_markets = [m for m in markets if m.get("status", "").lower() == "active"]
        tickers = sorted([m["ticker"] for m in active_markets])
        out_map = {}
        outcomes = []
        for m in active_markets:
            t = m["ticker"]
            sub = m.get("subtitle") or m.get("yes_subtitle") or t
            sub = sub.strip()
            if sub in outcomes:
                sub = f"{sub} ({t})"
            outcomes.append(sub)
            out_map[t] = sub

        b = DeclaredMeceBasket(
            event_ticker=evt,
            basket_id=f"BASKET-{evt}",
            outcomes=tuple(outcomes),
            market_outcome_map=out_map,
            basket_side=YES,
            notes=f"V18 research basket for {evt} ({len(tickers)} legs)",
        )
        baskets.append(b)
    return baskets


def run_cohort_trial(
    client: KalshiRestClient,
    cohort_name: str,
    baskets: Sequence[DeclaredMeceBasket],
    cycles: int,
    cycle_interval: float,
    concurrency: int,
    session_id: str,
    out_dir: Path,
) -> Tuple[List[MarketObservation], List[ReconstructedBasketSnapshot], List[SnapshotEconomicAttribution]]:
    """Execute collection, reconstruction, and attribution for a research cohort."""
    logger.info("Starting collection for %s (%d baskets)...", cohort_name, len(baskets))
    all_tickers: List[str] = []
    for b in baskets:
        for t in b.ordered_tickers:
            if t not in all_tickers:
                all_tickers.append(t)

    logger.info("  %s total monitored tickers: %d", cohort_name, len(all_tickers))
    all_obs: List[MarketObservation] = []

    obs_file = out_dir / f"{cohort_name.lower().replace(' ', '_')}_observations.jsonl"
    with open(obs_file, "w", encoding="utf-8") as f_obs:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for cycle_idx in range(cycles):
                c_start = datetime.now(timezone.utc)
                fut_to_t = {
                    executor.submit(fetch_single_ticker, client, t, session_id): t
                    for t in all_tickers
                }
                cycle_obs = [fut.result() for fut in as_completed(fut_to_t)]
                for obs in cycle_obs:
                    all_obs.append(obs)
                    f_obs.write(json.dumps(observation_to_dict(obs)) + "\n")

                elapsed = (datetime.now(timezone.utc) - c_start).total_seconds()
                logger.info("  %s cycle %d/%d: %d obs in %.2fs", cohort_name, cycle_idx + 1, cycles, len(cycle_obs), elapsed)
                if cycle_idx < cycles - 1:
                    time.sleep(cycle_interval)

    # Reconstruct snapshots
    reconstructor = EventSnapshotReconstructor(SnapshotReconstructionConfig())
    snapshots, _ = reconstructor.reconstruct(all_obs, baskets)
    logger.info("  %s reconstructed %d snapshots (%d valid)", cohort_name, len(snapshots), sum(1 for s in snapshots if s.is_valid))

    # Economics Attribution Analysis
    analyzer = OpportunityEconomicsAnalyzer(FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER))
    attributions = analyzer.analyze_snapshots(snapshots, requested_quantity=1)

    return all_obs, snapshots, attributions


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Kalshi V18 Two-Sided Multi-Universe Evaluation CLI")
    parser.add_argument("--output-dir", type=str, default="evidence_v18_economics_trial")
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--cycle-interval", type=float, default=2.0)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    out_path = Path(args.output_dir)
    if out_path.exists() and any(out_path.iterdir()):
        logger.error("Output directory %s already exists and is not empty. Aborting.", out_path)
        return 1
    out_path.mkdir(parents=True, exist_ok=True)

    session_id = f"v18-trial-{int(datetime.now(timezone.utc).timestamp())}"
    client = KalshiRestClient()

    # Step 1: Universe Exploration Report
    logger.info("Generating research-universe exploration report...")
    engine = BroadMarketDiscoveryEngine(client, DiscoveryConfig(max_events_to_scan=100, concurrency=args.concurrency))
    disc_report = engine.discover_and_classify()
    (out_path / "universe_exploration_report.json").write_text(json.dumps(disc_report.to_dict(), indent=2), encoding="utf-8")

    # Define Cohorts
    cohort_a_events = ["KXDEELRIP-40", "KXGDPYEAR-35", "KXGDPYEAR-34"]
    cohort_b_events = ["KXFEDDECISION-26OCT", "KXFEDDECISION-26DEC", "KXFEDDECISION-27DEC"]

    cohort_a_baskets = build_baskets_for_events(client, cohort_a_events)
    cohort_b_baskets = build_baskets_for_events(client, cohort_b_events)

    # Step 2: Run prospective trial for Cohort A
    obs_a, snaps_a, attrs_a = run_cohort_trial(
        client=client,
        cohort_name="Cohort_A",
        baskets=cohort_a_baskets,
        cycles=args.cycles,
        cycle_interval=args.cycle_interval,
        concurrency=args.concurrency,
        session_id=session_id,
        out_dir=out_path,
    )

    # Step 3: Run prospective trial for Cohort B
    obs_b, snaps_b, attrs_b = run_cohort_trial(
        client=client,
        cohort_name="Cohort_B",
        baskets=cohort_b_baskets,
        cycles=args.cycles,
        cycle_interval=args.cycle_interval,
        concurrency=args.concurrency,
        session_id=session_id,
        out_dir=out_path,
    )

    # Step 4: Write all snapshots and attributions
    all_snaps = snaps_a + snaps_b
    all_attrs = attrs_a + attrs_b

    snap_file = out_path / "snapshots.jsonl"
    with open(snap_file, "w", encoding="utf-8") as f:
        for s in all_snaps:
            f.write(json.dumps({
                "snapshot_id": s.snapshot_id,
                "event_ticker": s.basket.event_ticker,
                "basket_id": s.basket.basket_id,
                "is_valid": s.is_valid,
                "status": s.status,
                "leg_timestamp_skew_seconds": s.leg_timestamp_skew_seconds,
                "ordered_tickers": list(s.basket.ordered_tickers),
                "market_state_fingerprint": s.market_state_fingerprint,
            }) + "\n")

    attr_file = out_path / "attributions.jsonl"
    with open(attr_file, "w", encoding="utf-8") as f:
        for a in all_attrs:
            f.write(json.dumps(a.to_dict()) + "\n")

    # Step 5: Summaries
    analyzer = OpportunityEconomicsAnalyzer()
    summary_a = analyzer.summarize_attributions(attrs_a)
    summary_b = analyzer.summarize_attributions(attrs_b)
    combined_summary = analyzer.summarize_attributions(all_attrs)

    trial_summary = {
        "experiment": "Kalshi V18 Arbitrage Strategy Research & Economic Validation Trial",
        "session_id": session_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "cycles": args.cycles,
        "cycle_interval_seconds": args.cycle_interval,
        "cohort_a_broad_market": {
            "events": cohort_a_events,
            "baskets_count": len(cohort_a_baskets),
            "total_markets": sum(len(b.ordered_tickers) for b in cohort_a_baskets),
            "observations_count": len(obs_a),
            "snapshots_count": len(snaps_a),
            "summary": summary_a,
        },
        "cohort_b_monetary_policy": {
            "events": cohort_b_events,
            "baskets_count": len(cohort_b_baskets),
            "total_markets": sum(len(b.ordered_tickers) for b in cohort_b_baskets),
            "observations_count": len(obs_b),
            "snapshots_count": len(snaps_b),
            "summary": summary_b,
        },
        "combined_summary": combined_summary,
    }

    summary_file = out_path / "trial_summary.json"
    summary_file.write_text(json.dumps(trial_summary, indent=2), encoding="utf-8")

    # Step 6: Manifest Generation
    manifest_files = {}
    for p in [
        out_path / "universe_exploration_report.json",
        out_path / "cohort_a_observations.jsonl",
        out_path / "cohort_b_observations.jsonl",
        snap_file,
        attr_file,
        summary_file,
    ]:
        manifest_files[p.name] = {
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "size_bytes": p.stat().st_size,
        }

    manifest = {
        "manifest_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "experiment": "V18 Two-Sided Multi-Universe Economic Trial",
        "files": manifest_files,
        "summary": trial_summary,
    }
    (out_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    logger.info("V18 Multi-Universe Trial Complete! Output stored in %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
