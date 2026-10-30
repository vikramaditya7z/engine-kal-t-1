#!/usr/bin/env python3
"""CLI entry point for the Kalshi Arbitrage Engine V21 Continuous Live Arbitrage Detector.

Evaluates fresh live market observations from continuous ingestion and detects genuine,
mathematically valid arbitrage candidates across single-market binary parity and
multi-market MECE event baskets.

Safety: Operates strictly as a read-only detection service. Never places orders, submits trades,
or requires private API keys.
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
    AlertConfig,
    ContinuousEvaluator,
    ContinuousEvaluatorConfig,
    FeeScheduleType,
)

logger = logging.getLogger("continuous_evaluator")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Kalshi Arbitrage Engine V21 Continuous Live Arbitrage Detection Service (Read-Only)."
    )
    parser.add_argument(
        "--observations-file",
        type=Path,
        default=ROOT / "data" / "live_collection" / "observations.jsonl",
        help="Path to JSONL observations evidence stream (default: data/live_collection/observations.jsonl).",
    )
    parser.add_argument(
        "--baskets",
        type=Path,
        default=None,
        help="Path to declared MECE event baskets JSON configuration (optional).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval between stream checks in seconds (default: 1.0).",
    )
    parser.add_argument(
        "--max-stale-seconds",
        type=float,
        default=60.0,
        help="Maximum allowable age for order book timestamps before invalidating (default: 60.0).",
    )
    parser.add_argument(
        "--max-leg-skew-seconds",
        type=float,
        default=5.0,
        help="Maximum allowable timestamp skew across basket legs in seconds (default: 5.0).",
    )
    parser.add_argument(
        "--target-quantity",
        type=int,
        default=1,
        help="Target contract quantity to evaluate execution depth for (default: 1).",
    )
    parser.add_argument(
        "--require-full-quantity",
        action="store_true",
        default=False,
        help="Require 100%% of target quantity to be executable; reject partial fills (default: False).",
    )
    parser.add_argument(
        "--min-net-profit",
        type=int,
        default=1,
        help="Minimum net profit in integer cents required for qualification (default: 1).",
    )
    parser.add_argument(
        "--require-source-timestamp",
        action="store_true",
        default=False,
        help="Reject observations lacking an explicit source timestamp (default: False).",
    )
    parser.add_argument(
        "--fee-schedule",
        choices=["kalshi_official_taker", "zero_fee"],
        default="kalshi_official_taker",
        help="Fee schedule for net edge calculation (default: kalshi_official_taker).",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=ROOT / "data" / "live_evaluator_state.json",
        help="JSON file path to expose live evaluator state and active candidates (default: data/live_evaluator_state.json).",
    )
    parser.add_argument(
        "--history-file",
        type=Path,
        default=ROOT / "data" / "live_candidate_history.jsonl",
        help="JSONL file path to persist candidate lifecycle transitions (default: data/live_candidate_history.jsonl).",
    )
    parser.add_argument(
        "--disappear-timeout",
        type=float,
        default=180.0,
        help="Timeout in seconds before transitioning unobserved active candidates to DISAPPEARED (default: 180.0).",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Maximum evaluation cycles to run before halting (default: infinite until stopped).",
    )
    # Alert and Reliability Options (V24)
    parser.add_argument(
        "--alerts-enabled",
        action="store_true",
        default=True,
        help="Enable qualified-opportunity alert engine (default: True).",
    )
    parser.add_argument(
        "--no-alerts",
        action="store_false",
        dest="alerts_enabled",
        help="Disable qualified-opportunity alert engine.",
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help="Destination HTTP webhook URL for qualified opportunity alerts (optional).",
    )
    parser.add_argument(
        "--desktop-alerts",
        action="store_true",
        default=False,
        help="Enable native desktop notifications on macOS (default: False).",
    )
    parser.add_argument(
        "--alert-state-file",
        type=Path,
        default=ROOT / "data" / "live_alert_state.json",
        help="JSON file path for persistent alert deduplication state (default: data/live_alert_state.json).",
    )
    parser.add_argument(
        "--alert-history-file",
        type=Path,
        default=ROOT / "data" / "live_alert_history.jsonl",
        help="JSONL file path for local alert history log (default: data/live_alert_history.jsonl).",
    )
    parser.add_argument(
        "--profit-change-threshold",
        type=int,
        default=5,
        help="Material net profit change in cents required to trigger an update alert (default: 5).",
    )
    parser.add_argument(
        "--quantity-change-threshold",
        type=int,
        default=1,
        help="Material executable quantity change in contracts required to trigger an update alert (default: 1).",
    )
    parser.add_argument(
        "--re-alert-cooldown",
        type=float,
        default=60.0,
        help="Cooldown in seconds before re-alerting on a re-qualifying candidate (default: 60.0).",
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

    fee_sched = (
        FeeScheduleType.KALSHI_OFFICIAL_TAKER
        if args.fee_schedule == "kalshi_official_taker"
        else FeeScheduleType.EXEMPT
    )

    print("=" * 68)
    print("KALSHI ARBITRAGE ENGINE — V22 EXECUTION-AWARE ARBITRAGE DETECTOR")
    print("=" * 68)
    print(f"Observations Stream: {args.observations_file}")
    print(f"Polling Interval:    {args.interval}s")
    print(f"Max Stale Limit:     {args.max_stale_seconds}s")
    print(f"Max Leg Skew Limit:  {args.max_leg_skew_seconds}s")
    print(f"Target Quantity:     {args.target_quantity} contracts")
    print(f"Require Full Depth:  {args.require_full_quantity}")
    print(f"Min Net Profit:      {args.min_net_profit} cents")
    print(f"Req Source Timestamp:{args.require_source_timestamp}")
    print(f"Fee Schedule:        {args.fee_schedule}")
    if args.baskets:
        print(f"MECE Baskets Config: {args.baskets}")
    else:
        print(f"MECE Baskets Config: None (Single-market binary parity only)")
    print(f"State Telemetry:     {args.state_file}")
    print(f"Transition History:  {args.history_file}")
    if args.alerts_enabled:
        channels = ["local_history"]
        if args.desktop_alerts:
            channels.append("desktop")
        if args.webhook_url:
            channels.append("webhook")
        print(f"Alerts Engine (V24): Enabled (Channels: {', '.join(channels)})")
        print(f"Alert Deduplication: State={args.alert_state_file.name}, History={args.alert_history_file.name}")
        print(f"Material Change:     Net Profit >= {args.profit_change_threshold}c, Qty >= {args.quantity_change_threshold}")
    else:
        print(f"Alerts Engine (V24): Disabled (--no-alerts specified)")
    if args.cycles:
        print(f"Run Duration:        {args.cycles} bounded cycles")
    else:
        print(f"Run Duration:        Continuous indefinite (press Ctrl+C to halt)")
    print("=" * 68)
    print("Operational Notice: Read-only live detection & qualification. Zero trading credentials required.", flush=True)

    alert_cfg = None
    if args.alerts_enabled:
        channels_tuple = ["local_history"]
        if args.desktop_alerts:
            channels_tuple.append("desktop")
        if args.webhook_url:
            channels_tuple.append("webhook")

        alert_cfg = AlertConfig(
            enabled=True,
            enabled_channels=tuple(channels_tuple),
            webhook_url=args.webhook_url,
            desktop_notifications_enabled=args.desktop_alerts,
            profit_change_threshold_cents=args.profit_change_threshold,
            quantity_change_threshold=args.quantity_change_threshold,
            re_alert_cooldown_seconds=args.re_alert_cooldown,
            state_file=args.alert_state_file,
            history_file=args.alert_history_file,
        )

    config = ContinuousEvaluatorConfig(
        poll_interval_seconds=args.interval,
        max_stale_seconds=args.max_stale_seconds,
        max_leg_skew_seconds=args.max_leg_skew_seconds,
        target_quantity=args.target_quantity,
        require_full_quantity=args.require_full_quantity,
        min_net_profit_cents=args.min_net_profit,
        require_source_timestamp=args.require_source_timestamp,
        fee_schedule=fee_sched,
        baskets_path=args.baskets,
        observations_file=args.observations_file,
        state_file=args.state_file,
        history_file=args.history_file,
        max_cycles=args.cycles,
        disappear_timeout_seconds=args.disappear_timeout,
        alerts_enabled=args.alerts_enabled,
        alert_config=alert_cfg,
        alert_state_file=args.alert_state_file,
        alert_history_file=args.alert_history_file,
    )

    evaluator = ContinuousEvaluator(config=config)

    shutdown_in_progress = False

    def handle_shutdown(signum, frame):
        nonlocal shutdown_in_progress
        if not shutdown_in_progress:
            shutdown_in_progress = True
            print(f"\n[Signal {signum}] Initiating graceful continuous evaluator shutdown...", flush=True)
            evaluator.stop()
        else:
            print(f"\n[Signal {signum}] Forcing immediate exit.", flush=True)
            sys.exit(1)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        evaluator.run()
    except KeyboardInterrupt:
        print("\n[Interrupted] Cleanly shutting down continuous evaluator...", flush=True)
        evaluator.stop()
    except Exception as exc:
        logger.exception("Continuous evaluator failed with unexpected exception: %s", exc)
        return 1

    health = evaluator.get_health()
    qual_summary = evaluator.get_qualification_summary()
    print("\n" + "=" * 68)
    print("EVALUATOR SHUTDOWN SUMMARY")
    print("=" * 68)
    print(f"Session ID:              {health.session_id}")
    print(f"Final Status:            {health.status}")
    print(f"Evaluations Count:       {health.evaluations_count}")
    print(f"Processed Observations:  {health.processed_observations_count}")
    print(f"Active Candidates Count: {health.active_candidates_count}")
    print(f"Total Candidates Found:  {health.total_candidates_detected}")
    print(f"Qualified Candidates:    {health.qualified_candidates_count}")
    print(f"Rejected Candidates:     {health.rejected_candidates_count}")
    print(f"Candidate Transitions:   {health.candidate_transitions_count}")
    if qual_summary.get("outcomes_breakdown"):
        print(f"Outcomes Breakdown:      {qual_summary['outcomes_breakdown']}")
    print(f"State File Location:     {args.state_file}")
    print(f"History File Location:   {args.history_file}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
