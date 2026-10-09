#!/usr/bin/env python3
"""CLI tool for V6 Historical Opportunity Evaluation & Sensitivity Analysis.

Loads recorded Kalshi market observations from disk, evaluates arbitrage detection,
execution pricing, and executable liquidity under configurable fee and freshness
assumptions, optionally simulates paper trading, and outputs auditable research reports.
"""

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_arbitrage import (
    EvaluationConfig,
    EvaluationError,
    HistoricalDataset,
    HistoricalEvaluator,
    evaluate_historical_evidence,
    run_sensitivity_analysis,
)


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Engine — V6 Historical Opportunity Evaluation",
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
        "--fee-cents",
        type=int,
        default=1,
        help="Modeled exchange fee per contract in integer cents",
    )
    parser.add_argument(
        "--haircut-cents",
        type=int,
        default=0,
        help="Conservative slippage haircut per contract in integer cents",
    )
    parser.add_argument(
        "--max-stale-seconds",
        type=float,
        default=60.0,
        help="Maximum allowed order book age in seconds before rejection",
    )
    parser.add_argument(
        "--target-quantity",
        type=int,
        default=1,
        help="Target contract acquisition quantity to test against book depth",
    )
    parser.add_argument(
        "--initial-cash-cents",
        type=int,
        default=100_000,
        help="Initial capital for simulated paper trading in integer cents",
    )
    parser.add_argument(
        "--enable-paper-trading",
        action="store_true",
        default=False,
        help="Simulate paper execution and portfolio ledger (disabled by default)",
    )
    parser.add_argument(
        "--sensitivity",
        action="store_true",
        default=False,
        help="Run parameter sensitivity analysis across fees, freshness, and haircuts",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional path to output machine-readable evaluation report as JSON",
    )

    return parser.parse_args(args)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    source_path: Path
    if args.evidence_dir:
        dir_p = Path(args.evidence_dir)
        source_path = dir_p / "observations.jsonl"
        if not source_path.exists():
            print(f"Error: observations.jsonl not found in directory: {dir_p}", file=sys.stderr)
            return 1
    else:
        source_path = Path(args.file)
        if not source_path.exists():
            print(f"Error: Evidence file not found: {source_path}", file=sys.stderr)
            return 1

    try:
        config = EvaluationConfig(
            initial_cash_cents=args.initial_cash_cents,
            target_quantity=args.target_quantity,
            default_fee_per_contract_cents=args.fee_cents,
            max_stale_seconds=args.max_stale_seconds,
            conservative_haircut_cents=args.haircut_cents,
            enable_paper_trading=args.enable_paper_trading,
        )

        dataset = HistoricalDataset.from_file(source_path)
        evaluator = HistoricalEvaluator(config)
        report = evaluator.evaluate(dataset)

        print(report.summary())

        if args.sensitivity:
            print("\n" + "=" * 72)
            print("RUNNING PARAMETER SENSITIVITY ANALYSIS...")
            sens_report = run_sensitivity_analysis(dataset, base_config=config)
            print(sens_report.summary())

        if args.output_json:
            out_p = Path(args.output_json)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            with open(out_p, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, indent=2)
            print(f"\nSaved JSON evaluation report to: {out_p}")

        return 0

    except Exception as exc:
        print(f"Evaluation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
