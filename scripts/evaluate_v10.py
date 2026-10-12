#!/usr/bin/env python3
"""CLI tool for Kalshi Arbitrage Engine V10: Rigorous Historical Strategy Validation & Quantitative Performance Research.

Usage:
    # Evaluate a historical evidence dataset with full reports and sensitivity grid
    python scripts/evaluate_v10.py --file data/v5_trial/observations.jsonl --output-json data/v10_trial_report.json

    # Evaluate with specific valuation method and custom output markdown
    python scripts/evaluate_v10.py --evidence-dir data/v5_trial --output-report docs/v10_evaluation_summary.md

All execution is strictly historical paper simulation. Live order routing is prohibited.
"""

import argparse
from decimal import Decimal
import json
import logging
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
from kalshi_arbitrage.paper_risk import PaperRiskConfig
from kalshi_arbitrage.universe import UniverseFilterConfig
from kalshi_arbitrage.v10_evaluation import (
    V10EvaluationConfig,
    V10EvaluationReport,
    V10HistoricalStrategyEvaluator,
)

logger = logging.getLogger("v10_evaluator")


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Engine — V10 Historical Strategy Validation & Quantitative Research",
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
        help="Direct path to observations JSONL file",
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
        help="Starting paper portfolio capital in integer cents ($10,000.00)",
    )
    parser.add_argument(
        "--max-stale-seconds",
        type=float,
        default=60.0,
        help="Maximum allowable order book age before fail-closed rejection",
    )
    parser.add_argument(
        "--valuation-method",
        type=str,
        default="MARK_TO_MARKET_BID",
        choices=["MARK_TO_MARKET_BID", "COST_BASIS", "ZERO"],
        help="Valuation methodology for marking open portfolio positions",
    )
    parser.add_argument(
        "--fee-schedule",
        type=str,
        default="cftc_taker",
        choices=["cftc_taker", "cftc_maker", "zero", "flat"],
        help="Fee schedule applied to simulated orders",
    )
    parser.add_argument(
        "--sequencing",
        type=str,
        default="ILLIQUID_FIRST",
        choices=["ILLIQUID_FIRST", "HIGH_PRICE_FIRST", "LOW_PRICE_FIRST", "SIMULTANEOUS"],
        help="Multi-leg order submission sequencing policy",
    )
    parser.add_argument(
        "--unwind",
        type=str,
        default="AGGRESSIVE_CROSS",
        choices=["AGGRESSIVE_CROSS", "EMERGENCY_DUMP", "CONSERVATIVE_LIMIT"],
        help="Unwind policy for legged-out positions",
    )
    parser.add_argument(
        "--no-enforce-universe",
        action="store_true",
        default=False,
        help="Disable point-in-time liquid universe filtering",
    )
    parser.add_argument(
        "--no-out-of-sample",
        action="store_true",
        default=False,
        help="Disable chronological out-of-sample validation partition",
    )
    parser.add_argument(
        "--in-sample-ratio",
        type=float,
        default=0.70,
        help="Proportion of chronological dataset reserved for in-sample evaluation",
    )
    parser.add_argument(
        "--no-sensitivity",
        action="store_true",
        default=False,
        help="Disable multi-dimensional sensitivity parameter grid sweeps",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="File path to save machine-readable JSON evaluation report",
    )
    parser.add_argument(
        "--output-report",
        type=str,
        default=None,
        help="File path to save readable Markdown / text evaluation report",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )

    return parser.parse_args(args)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Resolve input file path
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

    # Map fee schedule
    fee_map = {
        "cftc_taker": FeeScheduleType.KALSHI_OFFICIAL_TAKER,
        "cftc_maker": FeeScheduleType.KALSHI_OFFICIAL_MAKER,
        "zero": FeeScheduleType.EXEMPT,
        "flat": FeeScheduleType.FLAT,
    }
    fee_cfg = FeeModelConfig(schedule_type=fee_map[args.fee_schedule])

    u_cfg = UniverseFilterConfig(
        min_volume_24h=Decimal("100.00"),
        min_open_interest=Decimal("10.00"),
        max_spread_cents=20,
    )

    micro_cfg = MicrostructureConfig(
        network_latency_ms=50.0,
        sequential_leg_delay_ms=25.0,
        sequencing_policy=LegSequencingPolicy(args.sequencing),
        unwind_policy=UnwindPolicy(args.unwind),
        fee_config=fee_cfg,
        enable_latency_decay=True,
    )

    eval_cfg = V10EvaluationConfig(
        initial_cash_cents=args.initial_cash_cents,
        target_quantity=args.target_quantity,
        max_stale_seconds=args.max_stale_seconds,
        universe_config=u_cfg,
        enforce_universe=not args.no_enforce_universe,
        fee_config=fee_cfg,
        microstructure_config=micro_cfg,
        leg_sequencing=LegSequencingPolicy(args.sequencing),
        unwind_policy=UnwindPolicy(args.unwind),
        valuation_method=args.valuation_method,
        enable_out_of_sample=not args.no_out_of_sample,
        in_sample_ratio=args.in_sample_ratio,
        enable_sensitivity=not args.no_sensitivity,
    )

    evaluator = V10HistoricalStrategyEvaluator(config=eval_cfg)
    report = evaluator.evaluate(dataset, source_path=str(file_path))

    summary_text = report.summary()
    print()
    print(summary_text)

    # Export outputs if requested
    if args.output_json:
        out_json_path = Path(args.output_json)
        out_json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_json_path, "w", encoding="utf-8") as f:
            f.write(report.to_json(indent=2))
        print(f"\nSaved structured V10 JSON evaluation report to: {out_json_path}")

    if args.output_report:
        out_rep_path = Path(args.output_report)
        out_rep_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_rep_path, "w", encoding="utf-8") as f:
            f.write(summary_text + "\n")
        print(f"Saved readable V10 evaluation summary report to: {out_rep_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
