#!/usr/bin/env python3
"""CLI entry point for the Kalshi Arbitrage Engine Breadth-First Scanner (Phase 2D).

Continuously discovers the broad Kalshi market catalog via cursor pagination, screens
for preliminary top-of-book arbitrage, and executes targeted L2 order-book qualification
for genuine candidates.

Safety: Operates strictly with read-only public endpoints. Never submits trades,
cancels orders, or requires private API credentials.
"""

from __future__ import annotations

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
    BreadthScannerRunner,
    BreadthScannerRunnerConfig,
    FeeScheduleType,
    KalshiRestClient,
)

logger = logging.getLogger("breadth_scanner")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Kalshi Arbitrage Engine Standalone Breadth-First Scanner (Read-Only)."
    )
    parser.add_argument(
        "--environment",
        choices=["production", "demo"],
        default="production",
        help="Kalshi API environment for public market data (default: production).",
    )
    parser.add_argument(
        "--interval",
        "--refresh-interval",
        dest="interval",
        type=float,
        default=30.0,
        help="Catalog discovery refresh interval between passes in seconds (default: 30.0).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Cursor pagination page size limit (default: 100).",
    )
    parser.add_argument(
        "--queue-size",
        type=int,
        default=100,
        help="Maximum capacity of the priority qualification candidate queue (default: 100).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Maximum worker concurrency for targeted order-book qualification (default: 4).",
    )
    parser.add_argument(
        "--request-rate",
        type=float,
        default=5.0,
        help="Maximum outgoing requests per second across all scanner API requests (default: 5.0).",
    )
    parser.add_argument(
        "--max-stale-seconds",
        type=float,
        default=60.0,
        help="Maximum allowable age for quote timestamps before invalidating (default: 60.0).",
    )
    parser.add_argument(
        "--target-quantity",
        type=int,
        default=1,
        help="Target contract quantity to qualify executable depth for (default: 1).",
    )
    parser.add_argument(
        "--require-full-quantity",
        action="store_true",
        default=False,
        help="Require 100%% of target quantity to be executable across all legs (default: False).",
    )
    parser.add_argument(
        "--min-net-profit",
        type=int,
        default=1,
        help="Minimum net profit in integer cents required for qualification (default: 1).",
    )
    parser.add_argument(
        "--max-candidate-legs",
        type=int,
        default=8,
        help="Maximum constituent legs allowed for a candidate basket before pre-qualification rejection (default: 8).",
    )
    parser.add_argument(
        "--enable-event-prefilter",
        dest="enable_event_prefilter",
        action="store_true",
        default=True,
        help="Enable event-level top-of-book pre-filter (default: True).",
    )
    parser.add_argument(
        "--no-event-prefilter",
        dest="enable_event_prefilter",
        action="store_false",
        help="Disable event-level top-of-book pre-filter.",
    )
    parser.add_argument(
        "--event-cache-ttl",
        type=float,
        default=2.0,
        help="Freshness cache TTL in seconds for event pre-filter deduplication (default: 2.0s).",
    )
    parser.add_argument(
        "--check-prefilter-quote-staleness",
        action="store_true",
        default=False,
        help="Check constituent market catalog updated_time against max_stale_seconds during event pre-filter (default: False; actual quote freshness is validated on order book).",
    )
    parser.add_argument(
        "--require-source-timestamp",
        action="store_true",
        default=False,
        help="Reject order books missing an exchange source timestamp (default: False).",
    )
    parser.add_argument(
        "--fee-schedule",
        choices=["kalshi_official_taker", "zero_fee"],
        default="kalshi_official_taker",
        help="Fee schedule for net edge calculation (default: kalshi_official_taker).",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help="Optional category filters (e.g. Economics Politics Financials).",
    )
    parser.add_argument(
        "--series",
        nargs="*",
        default=None,
        help="Optional series ticker filters (e.g. KXFEDDECISION).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "breadth_scanner",
        help="Directory to persist scanner state and history (default: data/breadth_scanner).",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Path to JSON state file (default: <output-dir>/scanner_state.json).",
    )
    parser.add_argument(
        "--summaries-file",
        type=Path,
        default=None,
        help="Path to JSONL scan cycle summaries (default: <output-dir>/scan_summaries.jsonl).",
    )
    parser.add_argument(
        "--candidate-history-file",
        type=Path,
        default=None,
        help="Path to JSONL preliminary candidate history (default: <output-dir>/candidate_history.jsonl).",
    )
    parser.add_argument(
        "--qualification-history-file",
        type=Path,
        default=None,
        help="Path to JSONL qualification history (default: <output-dir>/qualification_history.jsonl).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Finite run duration in seconds before halting (default: continuous).",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Maximum scan cycles to execute before halting (default: infinite until stopped).",
    )
    parser.add_argument(
        "--single-pass",
        action="store_true",
        default=False,
        help="Execute exactly one complete pass and exit immediately.",
    )
    parser.add_argument(
        "--enable-lookahead-gate",
        dest="enable_lookahead_gate",
        action="store_true",
        default=True,
        help="Enable lookahead cycle initiation gate based on estimated duration (default: True).",
    )
    parser.add_argument(
        "--no-lookahead-gate",
        dest="enable_lookahead_gate",
        action="store_false",
        help="Disable lookahead cycle initiation gate.",
    )
    parser.add_argument(
        "--lookahead-threshold",
        type=float,
        default=600.0,
        help="Minimum cycle duration threshold in seconds for lookahead gate (default: 600.0).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose DEBUG logging.",
    )

    args = parser.parse_args(argv)

    # CLI Argument Validation
    if args.interval < 0:
        parser.error("--interval cannot be negative.")
    if args.page_size <= 0:
        parser.error("--page-size must be a positive integer.")
    if args.queue_size <= 0:
        parser.error("--queue-size must be a positive integer.")
    if args.concurrency <= 0:
        parser.error("--concurrency must be a positive integer.")
    if args.request_rate <= 0:
        parser.error("--request-rate must be positive.")
    if args.max_stale_seconds <= 0:
        parser.error("--max-stale-seconds must be positive.")
    if args.target_quantity <= 0:
        parser.error("--target-quantity must be positive.")
    if args.min_net_profit < 0:
        parser.error("--min-net-profit cannot be negative.")
    if args.max_candidate_legs <= 0:
        parser.error("--max-candidate-legs must be a positive integer.")
    if args.event_cache_ttl < 0:
        parser.error("--event-cache-ttl cannot be negative.")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be positive.")
    if args.cycles is not None and args.cycles <= 0:
        parser.error("--cycles must be positive.")
    if args.lookahead_threshold < 0:
        parser.error("--lookahead-threshold cannot be negative.")

    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    output_dir = args.output_dir
    state_file = args.state_file or (output_dir / "scanner_state.json")

    fee_schedule = (
        FeeScheduleType.ZERO_FEE
        if args.fee_schedule == "zero_fee"
        else FeeScheduleType.KALSHI_OFFICIAL_TAKER
    )

    print("=" * 72)
    print("KALSHI ARBITRAGE ENGINE — BREADTH-FIRST SCANNER (PHASE 2D)")
    print("=" * 72)
    print(f"Environment:          {args.environment}")
    print(f"Refresh Interval:     {args.interval}s")
    print(f"Discovery Page Size:  {args.page_size} items/page")
    print(f"Queue Capacity:       {args.queue_size} candidates")
    print(f"Request Rate Limit:   {args.request_rate} req/s")
    print(f"Max Stale Limit:      {args.max_stale_seconds}s")
    print(f"Target Quantity:      {args.target_quantity} contracts")
    print(f"Max Candidate Legs:   {args.max_candidate_legs} legs")
    print(f"Event Pre-Filter:     {'Enabled' if args.enable_event_prefilter else 'Disabled'} (cache TTL: {args.event_cache_ttl}s)")
    print(f"Fee Schedule:         {args.fee_schedule}")
    print(f"Output Directory:     {output_dir}")
    print(f"State Telemetry File: {state_file}")
    if args.single_pass:
        print("Run Mode:             Single-Pass (deterministic verification)")
    elif args.duration:
        print(f"Run Mode:             Finite Duration ({args.duration}s)")
    elif args.cycles:
        print(f"Run Mode:             Finite Cycles ({args.cycles} passes)")
    else:
        print("Run Mode:             Continuous indefinite (press Ctrl+C to halt)")
    print("=" * 72)
    print("Safety Notice: Read-only public market data ingestion. Zero trading credentials.", flush=True)

    client = KalshiRestClient(environment=args.environment)

    config = BreadthScannerRunnerConfig(
        environment=args.environment,
        refresh_interval_seconds=args.interval,
        page_size=args.page_size,
        category_filter=tuple(args.categories) if args.categories else None,
        series_ticker_filter=tuple(args.series) if args.series else None,
        status_filter="open",
        target_quantity=args.target_quantity,
        max_queue_size=args.queue_size,
        max_concurrency=args.concurrency,
        max_requests_per_second=args.request_rate,
        max_stale_seconds=args.max_stale_seconds,
        fee_schedule=fee_schedule,
        require_full_quantity=args.require_full_quantity,
        min_net_profit_cents=args.min_net_profit,
        require_source_timestamp=args.require_source_timestamp,
        max_candidate_legs=args.max_candidate_legs,
        enable_event_prefilter=args.enable_event_prefilter,
        check_prefilter_quote_staleness=args.check_prefilter_quote_staleness,
        event_cache_ttl_seconds=args.event_cache_ttl,
        output_dir=output_dir,
        state_file=state_file,
        summaries_file=args.summaries_file,
        candidate_history_file=args.candidate_history_file,
        qualification_history_file=args.qualification_history_file,
        max_cycles=args.cycles,
        duration_seconds=args.duration,
        single_pass=args.single_pass,
        enable_lookahead_gate=args.enable_lookahead_gate,
        lookahead_threshold_seconds=args.lookahead_threshold,
    )

    runner = BreadthScannerRunner(client=client, config=config)

    shutdown_in_progress = False

    def handle_shutdown(signum: int, frame: object) -> None:
        nonlocal shutdown_in_progress
        if not shutdown_in_progress:
            shutdown_in_progress = True
            print(f"\n[Signal {signum}] Initiating graceful breadth scanner shutdown...", flush=True)
            runner.stop()
        else:
            print(f"\n[Signal {signum}] Forcing immediate exit.", flush=True)
            sys.exit(1)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        runner.run()
    except KeyboardInterrupt:
        print("\n[Interrupted] Cleanly shutting down breadth scanner...", flush=True)
        runner.stop()
    except Exception as exc:
        logger.exception("Breadth scanner failed with unexpected fatal error: %s", exc)
        return 1

    health = runner.get_health()
    print("\n" + "=" * 72)
    print("BREADTH SCANNER SHUTDOWN SUMMARY")
    print("=" * 72)
    print(f"Session ID:                 {health.session_id}")
    print(f"Final Status:               {health.status}")
    print(f"Total Cycles:               {health.total_cycles_count}")
    print(f"Successful Cycles:          {health.successful_cycles_count}")
    print(f"Failed Cycles:              {health.failed_cycles_count}")
    print(f"Unique Markets Discovered:  {health.total_unique_markets_discovered}")
    print(f"Unique Markets Screened:    {health.total_unique_markets_screened}")
    print(f"Total Events Evaluated:     {health.total_events_evaluated}")
    print(f"Preliminary Candidates:     {health.total_preliminary_candidates_generated}")
    print(f"Candidates Enqueued:        {health.candidates_enqueued}")
    print(f"Candidates Deduplicated:    {health.candidates_deduplicated}")
    print(f"Execution-Qualified Arbs:   {health.qualified_candidates_count}")
    print(f"API Requests Made:          {health.api_requests_count}")
    print(f"API Failures / Retries:     {health.api_failures_count} / {health.api_retries_count}")
    print(f"Rate Limit Events:          {health.rate_limit_events_count}")
    print(f"Queue Overflows:            {health.queue_overflow_count}")
    print(f"State File:                 {health.state_file}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
