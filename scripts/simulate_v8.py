#!/usr/bin/env python3
"""CLI tool for Kalshi Arbitrage Engine V8: Realistic Execution & Risk Simulation.

Replays recorded historical observation datasets, filters liquid market universes,
applies CFTC quadratic fee schedules, simulates asynchronous multi-leg execution
with latency and partial fill risks, manages legged-out unwinds, and computes
ledger reconciliation and parameter sensitivity sweeps.
"""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_arbitrage.async_execution import (
    LegSequencingPolicy,
    MicrostructureConfig,
    UnwindPolicy,
)
from kalshi_arbitrage.evaluation import HistoricalDataset
from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType
from kalshi_arbitrage.universe import UniverseFilterConfig
from kalshi_arbitrage.v8_simulation import (
    V8SimulationConfig,
    V8SimulationEngine,
    V8SimulationReport,
)


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Engine — V8 Realistic Execution & Risk Simulation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--evidence-dir",
        type=str,
        help="Directory containing observations.jsonl",
    )
    source_group.add_argument(
        "--file",
        type=str,
        help="Path to specific observations JSONL file",
    )

    parser.add_argument(
        "--fee-schedule",
        type=str,
        default="cftc_taker",
        choices=[
            "cftc_taker",
            "cftc_maker",
            "flat",
            "exempt",
        ],
        help="Kalshi fee schedule to simulate",
    )
    parser.add_argument(
        "--flat-fee-cents",
        type=int,
        default=1,
        help="Baseline flat fee per contract in cents for discrepancy comparison",
    )
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=50.0,
        help="Simulated network latency in milliseconds",
    )
    parser.add_argument(
        "--leg-delay-ms",
        type=float,
        default=25.0,
        help="Sequential delay between submitting leg 1 and leg 2 in ms",
    )
    parser.add_argument(
        "--sequencing",
        type=str,
        default="ILLIQUID_FIRST",
        choices=["ILLIQUID_FIRST", "HIGH_PRICE_FIRST", "LOW_PRICE_FIRST", "SIMULTANEOUS"],
        help="Leg sequencing execution policy",
    )
    parser.add_argument(
        "--unwind",
        type=str,
        default="AGGRESSIVE_CROSS",
        choices=["AGGRESSIVE_CROSS", "EMERGENCY_DUMP", "CONSERVATIVE_LIMIT"],
        help="Unwind recovery policy for legged-out positions",
    )
    parser.add_argument(
        "--target-quantity",
        type=int,
        default=1,
        help="Target contract acquisition quantity per opportunity",
    )
    parser.add_argument(
        "--initial-cash-cents",
        type=int,
        default=1_000_000,
        help="Starting paper portfolio capital in integer cents",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=100.0,
        help="Liquid universe minimum 24-hour volume in dollars",
    )
    parser.add_argument(
        "--min-oi",
        type=float,
        default=10.0,
        help="Liquid universe minimum open interest in dollars",
    )
    parser.add_argument(
        "--max-spread",
        type=int,
        default=20,
        help="Liquid universe maximum top-of-book bid-ask spread in cents",
    )
    parser.add_argument(
        "--no-enforce-universe",
        action="store_true",
        default=False,
        help="Disable universe filtering (evaluate all observed markets regardless of liquidity)",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional file path to write full JSON simulation report",
    )

    return parser.parse_args(args)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # Determine input path
    if args.file:
        file_path = Path(args.file)
    else:
        file_path = Path(args.evidence_dir) / "observations.jsonl"

    if not file_path.exists():
        print(f"Error: dataset file not found: {file_path}", file=sys.stderr)
        return 1

    print(f"Loading historical evidence from: {file_path}")
    dataset = HistoricalDataset.from_file(file_path)
    print(f"Loaded {len(dataset.observations)} valid chronological observations ({dataset.total_raw_records} raw records).")

    # Build configs
    fee_sched_map = {
        "cftc_taker": FeeScheduleType.KALSHI_OFFICIAL_TAKER,
        "cftc_maker": FeeScheduleType.KALSHI_OFFICIAL_MAKER,
        "flat": FeeScheduleType.FLAT,
        "exempt": FeeScheduleType.EXEMPT,
    }
    fee_config = FeeModelConfig(schedule_type=fee_sched_map[args.fee_schedule])

    universe_config = UniverseFilterConfig(
        min_volume_24h=Decimal(str(args.min_volume)),
        min_open_interest=Decimal(str(args.min_oi)),
        max_spread_cents=args.max_spread,
    )

    micro_config = MicrostructureConfig(
        network_latency_ms=args.latency_ms,
        sequential_leg_delay_ms=args.leg_delay_ms,
        sequencing_policy=LegSequencingPolicy(args.sequencing),
        unwind_policy=UnwindPolicy(args.unwind),
        fee_config=fee_config,
        enable_latency_decay=True,
    )

    v8_config = V8SimulationConfig(
        universe_config=universe_config,
        microstructure_config=micro_config,
        fee_config=fee_config,
        flat_fee_per_contract_cents=args.flat_fee_cents,
        unwind_policy=UnwindPolicy(args.unwind),
        leg_sequencing=LegSequencingPolicy(args.sequencing),
        target_quantity=args.target_quantity,
        initial_cash_cents=args.initial_cash_cents,
        enforce_universe=not args.no_enforce_universe,
    )

    engine = V8SimulationEngine(config=v8_config)
    report = engine.run(dataset)

    print()
    print(report.summary())

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report.to_json(indent=2))
        print(f"\nSaved structured V8 simulation report to: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
