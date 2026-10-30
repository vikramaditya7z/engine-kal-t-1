#!/usr/bin/env python3
"""CLI entry point for the Kalshi Arbitrage Engine V20 Continuous Live Market Data Collector.

Runs a reliable, continuous background or foreground collection service against
the Kalshi public read-only REST API.

Safety: Operates strictly with read-only public endpoints. Never submits trades,
cancels orders, or requires private API credentials.
"""

import argparse
import logging
from pathlib import Path
import signal
import sys
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_arbitrage import (
    ContinuousCollector,
    ContinuousCollectorConfig,
    KalshiRestClient,
)

logger = logging.getLogger("continuous_collector")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Kalshi Arbitrage Engine V20 Continuous Live Market Data Collector (Read-Only)."
    )
    parser.add_argument(
        "--environment",
        choices=["production", "demo"],
        default="production",
        help="Kalshi environment for public market data (default: production).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval between collection cycles in seconds (default: 2.0).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Maximum concurrent worker threads for ticker fetches (default: 4).",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=[],
        help="Explicit market tickers to monitor. If omitted, discovers active open markets.",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=30,
        help="Maximum open markets to poll per cycle in discovery mode (default: 30).",
    )
    parser.add_argument(
        "--max-stale-seconds",
        type=float,
        default=60.0,
        help="Maximum allowable age for order book timestamps before flagging stale (default: 60.0).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "live_collection",
        help="Directory to persist timestamped observations and manifests (default: data/live_collection).",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=ROOT / "data" / "live_collector_state.json",
        help="JSON file path to expose live collector health telemetry (default: data/live_collector_state.json).",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Maximum collection cycles to run before halting (default: infinite until stopped).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 68)
    print("KALSHI ARBITRAGE ENGINE — V20 CONTINUOUS LIVE MARKET DATA SERVICE")
    print("=" * 68)
    print(f"Environment:       {args.environment}")
    print(f"Polling Interval:  {args.interval}s")
    print(f"Concurrency:       {args.concurrency} workers")
    print(f"Max Stale Limit:   {args.max_stale_seconds}s")
    if args.tickers:
        print(f"Target Tickers:    {', '.join(args.tickers)} ({len(args.tickers)} markets)")
    else:
        print(f"Discovery Mode:    Active open markets (limit: {args.max_markets})")
    print(f"Output Directory:  {args.output_dir}")
    print(f"State Telemetry:   {args.state_file}")
    if args.cycles:
        print(f"Run Duration:      {args.cycles} bounded cycles")
    else:
        print(f"Run Duration:      Continuous indefinite (press Ctrl+C to halt)")
    print("=" * 68)
    print("Operational Notice: Read-only live ingestion. Zero trading credentials required.", flush=True)

    client = KalshiRestClient(
        environment=args.environment,
        timeout_seconds=10.0,
    )

    config = ContinuousCollectorConfig(
        environment=args.environment,
        poll_interval_seconds=args.interval,
        max_stale_seconds=args.max_stale_seconds,
        concurrency=args.concurrency,
        target_tickers=tuple(args.tickers) if args.tickers else None,
        max_markets_to_poll=args.max_markets,
        output_dir=args.output_dir,
        state_file=args.state_file,
        max_cycles=args.cycles,
    )

    collector = ContinuousCollector(client=client, config=config)

    shutdown_in_progress = False

    def handle_shutdown(signum, frame):
        nonlocal shutdown_in_progress
        if not shutdown_in_progress:
            shutdown_in_progress = True
            print(f"\n[Signal {signum}] Initiating graceful continuous collector shutdown...", flush=True)
            collector.stop()
        else:
            print(f"\n[Signal {signum}] Forcing immediate exit.", flush=True)
            sys.exit(1)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        collector.run()
    except KeyboardInterrupt:
        print("\n[Interrupted] Cleanly shutting down continuous collector...", flush=True)
        collector.stop()
    except Exception as exc:
        logger.exception("Continuous collector failed with unexpected exception: %s", exc)
        return 1

    health = collector.get_health()
    print("\n" + "=" * 68)
    print("COLLECTOR SHUTDOWN SUMMARY")
    print("=" * 68)
    print(f"Session ID:         {health.session_id}")
    print(f"Final Status:       {health.status}")
    print(f"Total Cycles:       {health.total_cycles_count}")
    print(f"Successful Cycles:  {health.successful_cycles_count}")
    print(f"Failed Cycles:      {health.failed_cycles_count}")
    print(f"Total Observations: {health.total_observations_count}")
    print(f"Rate Limit Events:  {health.rate_limit_count}")
    print(f"Evidence Location:  {args.output_dir / 'observations.jsonl'}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
