#!/usr/bin/env python3
"""Kalshi Arbitrage Engine V11 — Market Evidence Collection & Forward Evaluation CLI.

Usage Examples:

1. Bounded read-only collection session (5 cycles, 5 markets, isolated output directory):
   python scripts/collect_v11.py --collect --output-dir evidence_v11_sample --max-cycles 5 --max-markets 5

2. Bounded collection with prospective forward paper evaluation:
   python scripts/collect_v11.py --collect --output-dir evidence_v11_sample --max-cycles 5 --prospective-eval

3. Resume an existing bounded collection session:
   python scripts/collect_v11.py --collect --output-dir evidence_v11_sample --max-cycles 10 --resume

4. Audit evidence quality and cadence of collected dataset:
   python scripts/collect_v11.py --audit --input-dir evidence_v11_sample

5. Run prospective V10 historical strategy evaluation on newly collected dataset:
   python scripts/collect_v11.py --evaluate --file evidence_v11_sample/observations.jsonl --output-report docs/v11_sample_report.md
"""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalshi_arbitrage.evidence_storage import EvidenceStore
from kalshi_arbitrage.market_data_provider import RestMarketDataProvider
from kalshi_arbitrage.observer import load_declared_baskets
from kalshi_arbitrage.v10_evaluation import (
    V10EvaluationConfig,
    V10HistoricalStrategyEvaluator,
)
from kalshi_arbitrage.v11_collector import (
    CollectionDiagnosticEvent,
    V11CollectorConfig,
    V11EvidenceManifest,
    V11EvidenceQualityAuditor,
    V11EvidenceQualityMetrics,
    V11MarketEvidenceCollector,
    _compute_file_sha256,
)


def _format_quality_audit(audit: V11EvidenceQualityMetrics) -> str:
    lines = [
        "=" * 78,
        "KALSHI ARBITRAGE ENGINE — V11 EVIDENCE QUALITY & CADENCE AUDIT",
        "=" * 78,
        f"Audit Evaluation Time:         {datetime.now(timezone.utc).isoformat()}",
        f"Minimum Research Reqs Met:     {'YES' if audit.meets_minimum_research_requirements else 'NO'}",
    ]
    if audit.requirements_gap_reasons:
        lines.append("Identified Evidence Gaps:")
        for r in audit.requirements_gap_reasons:
            lines.append(f"  - {r}")

    lines.extend([
        "",
        "1. INGESTION CADENCE & EFFICIENCY:",
        f"  Total Collection Attempts:   {audit.total_collection_attempts}",
        f"  Successful Requests:         {audit.successful_requests}",
        f"  Failed Requests (Logged):    {audit.failed_requests}",
        f"  Valid Market Observations:   {audit.valid_observations}",
        f"  Stale / Invalid Records:     {audit.invalid_or_stale_observations}",
        f"  Diagnostic Events Logged:    {audit.diagnostic_events_count}",
        f"  Duplicate Record IDs:        {audit.duplicate_observation_ids}",
        f"  Out-of-Order Timestamps:     {audit.out_of_order_timestamps}",
        "",
        "2. TEMPORAL COVERAGE & CADENCE STABILITY:",
        f"  Start Timestamp:             {audit.collection_start.isoformat() if audit.collection_start else 'None'}",
        f"  End Timestamp:               {audit.collection_end.isoformat() if audit.collection_end else 'None'}",
        f"  Total Duration:              {audit.duration_seconds:.1f} seconds ({audit.duration_seconds / 60.0:.1f} min)",
        f"  Mean Sampling Cadence:       {audit.mean_sampling_cadence_seconds:.2f} seconds",
        f"  Median Sampling Cadence:     {audit.median_sampling_cadence_seconds:.2f} seconds",
        f"  Cadence Gaps (> 2.5x poll):  {audit.cadence_gaps_count}",
        f"  Max Latency Gap:             {audit.max_gap_seconds:.2f} seconds",
        "",
        "3. MARKET BREADTH & LIQUIDITY DEPTH:",
        f"  Unique Markets Monitored:    {audit.unique_markets_count}",
        f"  Unique Events Monitored:     {audit.unique_events_count}",
        f"  Populated Order Books:       {audit.populated_books_count}",
        f"  Empty Order Books:           {audit.empty_books_count}",
        f"  Both Sides Depth (YES & NO): {audit.both_sides_depth_count}",
        f"  One Side Depth Only:         {audit.one_side_depth_count}",
        f"  Repeated Identical Books:    {audit.repeated_identical_books_count}",
        f"  Mutated Books (Price/Qty):   {audit.mutated_books_count}",
    ])

    if audit.failures_by_error_code:
        lines.append("")
        lines.append("4. DIAGNOSTIC FAILURE BREAKDOWN:")
        for code, count in sorted(audit.failures_by_error_code.items()):
            lines.append(f"  - {code:<24}: {count}")

    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kalshi Arbitrage Engine V11 — Market Evidence Collection & Forward Evaluation CLI."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--collect",
        action="store_true",
        help="Run a bounded read-only evidence collection session.",
    )
    group.add_argument(
        "--audit",
        action="store_true",
        help="Audit evidence quality and cadence of an existing collection directory.",
    )
    group.add_argument(
        "--evaluate",
        action="store_true",
        help="Run prospective V10 historical strategy evaluation on recorded evidence.",
    )

    # Collector arguments
    parser.add_argument(
        "--output-dir",
        type=str,
        default="evidence_v11_session",
        help="Target directory for output evidence files (default: evidence_v11_session).",
    )
    parser.add_argument(
        "--session-name",
        type=str,
        default="v11_collection_session",
        help="Descriptive name for the collection session.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Maximum collection cycles to run before halting.",
    )
    parser.add_argument(
        "--max-duration-seconds",
        type=float,
        default=None,
        help="Maximum duration in seconds before halting.",
    )
    parser.add_argument(
        "--max-observations",
        type=int,
        default=None,
        help="Maximum total observations to record before halting.",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=10,
        help="Maximum markets to poll per cycle (default: 10).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Interval between cycles in seconds (default: 2.0).",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Explicit market tickers to monitor (default: discover open binary markets).",
    )
    parser.add_argument(
        "--basket-file",
        type=str,
        default=None,
        help="Path to JSON file declaring MECE event basket(s) for multi-outcome evaluation.",
    )
    parser.add_argument(
        "--basket-json",
        type=str,
        default=None,
        help="JSON string declaring MECE event basket(s) for multi-outcome evaluation.",
    )
    parser.add_argument(
        "--max-leg-skew",
        type=float,
        default=5.0,
        help="Maximum allowable timestamp skew across legs in seconds (default: 5.0).",
    )
    parser.add_argument(
        "--prospective-eval",
        action="store_true",
        help="Enable prospective forward paper evaluation during collection.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing session in output-dir without overwriting prior evidence.",
    )

    # Audit / Evaluation arguments
    parser.add_argument(
        "--input-dir",
        type=str,
        default=None,
        help="Input directory containing observations.jsonl for auditing.",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Path to observations.jsonl file for evaluation.",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Path to write structured JSON evaluation output.",
    )
    parser.add_argument(
        "--output-report",
        type=str,
        default=None,
        help="Path to write formatted Markdown evaluation report.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose DEBUG logging.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 1. Bounded Collection Mode
    if args.collect:
        declared_baskets = ()
        if args.basket_file or args.basket_json:
            try:
                declared_baskets = load_declared_baskets(args.basket_file or args.basket_json)
            except Exception as exc:
                print(f"\n[ERROR] Invalid basket configuration: {exc}", file=sys.stderr)
                return 1

        enable_eval = args.prospective_eval or bool(declared_baskets)
        out_dir = Path(args.output_dir)
        config = V11CollectorConfig(
            session_name=args.session_name,
            output_dir=out_dir,
            target_tickers=tuple(args.tickers) if args.tickers else None,
            declared_baskets=declared_baskets,
            max_leg_timestamp_skew_seconds=args.max_leg_skew,
            max_markets_per_cycle=args.max_markets,
            poll_interval_seconds=args.poll_interval,
            max_cycles=args.max_cycles,
            max_duration_seconds=args.max_duration_seconds,
            max_observations=args.max_observations,
            resume=args.resume,
            prevent_overwrite=not args.resume,
            enable_prospective_evaluation=enable_eval,
        )

        try:
            collector = V11MarketEvidenceCollector(config=config)
            print(f"Starting V11 collection in: {out_dir}")
            print(f"  Session ID:         {collector.session_id}")
            print(f"  Max Markets/Cycle:  {config.max_markets_per_cycle}")
            print(f"  Poll Interval:      {config.poll_interval_seconds}s")
            print(f"  Prospective Eval:   {'ENABLED' if config.enable_prospective_evaluation else 'DISABLED'}")
            if config.declared_baskets:
                print(f"  Declared Baskets:   {len(config.declared_baskets)} basket(s) configured")
                for b in config.declared_baskets:
                    print(f"    - Event: {b.event_ticker} ({len(b.outcomes)} outcomes: {b.ordered_tickers})")
            print("Press Ctrl+C at any time for clean graceful shutdown.\n")

            manifest = collector.run()

            print("\nCollection session finished successfully.")
            print(f"  Recorded Observations: {manifest.successful_observations}")
            print(f"  Diagnostic Failures:   {manifest.diagnostic_events_count}")
            print(f"  Total Cycles:          {manifest.total_cycles}")
            print(f"  Manifest written to:   {collector.manifest_path}")

            if config.enable_prospective_evaluation and collector.prospective_evaluator:
                fin = collector.prospective_evaluator.get_financial_summary()
                print("\nProspective Forward Evaluation Summary:")
                print(f"  Cash Balance:         ${fin['ending_cash_cents'] / 100:.2f}")
                print(f"  Completed Arbitrages: {fin['completed_arbs']}")
                print(f"  Net Realized P&L:     {fin['net_realized_pnl_cents']:+d}¢")
                print(f"  Ledger Reconciled:    {'PASS (100% Invariant Match)' if fin['is_ledger_reconciled'] else 'FAIL'}")
                if fin.get("baskets_evaluated", 0) > 0:
                    print(
                        f"  Baskets Evaluated:    {fin['baskets_evaluated']} "
                        f"({fin['basket_qualified']} qualified, "
                        f"{fin['basket_rej_no_gross_edge']} no edge, "
                        f"{fin['basket_rej_unprofitable_fees']} unprof fees, "
                        f"{fin['basket_rej_insufficient_depth']} no depth, "
                        f"{fin['basket_rej_missing_market']} missing legs, "
                        f"{fin['basket_rej_leg_skew']} skew)"
                    )

            return 0
        except FileExistsError as exc:
            print(f"\n[ERROR] Output directory conflict: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"\n[ERROR] Collection failed: {exc}", file=sys.stderr)
            return 1

    # 2. Audit Mode
    elif args.audit:
        in_dir = Path(args.input_dir or args.output_dir)
        obs_file = in_dir / "observations.jsonl"
        diag_file = in_dir / "diagnostics.jsonl"

        if not obs_file.exists():
            print(f"[ERROR] Observations file not found at: {obs_file}", file=sys.stderr)
            return 1

        store = EvidenceStore(storage_dir=in_dir)
        observations = store.load_observations()

        diagnostics = []
        if diag_file.exists():
            with open(diag_file, "r", encoding="utf-8") as f:
                for line in f:
                    clean = line.strip()
                    if clean:
                        diagnostics.append(CollectionDiagnosticEvent.from_dict(json.loads(clean)))

        metrics = V11EvidenceQualityAuditor.audit(observations, diagnostics)
        formatted = _format_quality_audit(metrics)
        print(formatted)

        if args.output_json:
            with open(args.output_json, "w", encoding="utf-8") as f:
                json.dump(metrics.to_dict(), f, indent=2)
            print(f"\nWrote quality audit JSON to: {args.output_json}")

        return 0

    # 3. Prospective / Historical V10 Evaluation Mode
    elif args.evaluate:
        obs_path = Path(args.file or (Path(args.output_dir) / "observations.jsonl"))
        if not obs_path.exists():
            print(f"[ERROR] Evidence file not found: {obs_path}", file=sys.stderr)
            return 1

        from kalshi_arbitrage.evaluation import HistoricalDataset

        dataset = HistoricalDataset.from_file(obs_path)

        declared_baskets = ()
        if args.basket_file or args.basket_json:
            try:
                declared_baskets = load_declared_baskets(args.basket_file or args.basket_json)
            except Exception as exc:
                print(f"[ERROR] Invalid basket configuration: {exc}", file=sys.stderr)
                return 1

        if declared_baskets:
            from kalshi_arbitrage.v13_basket_replay import (
                HistoricalBasketReplayConfig,
                HistoricalBasketReplayEngine,
            )
            basket_cfg = HistoricalBasketReplayConfig(
                max_leg_timestamp_skew_seconds=args.max_leg_skew,
            )
            engine = HistoricalBasketReplayEngine(config=basket_cfg)
            summary = engine.replay(
                dataset.observations,
                declared_baskets,
                source_path=str(obs_path),
                dataset_sha256=_compute_file_sha256(obs_path),
            )
            md_report = summary.summary_markdown()
            print(md_report)

            if args.output_report:
                with open(args.output_report, "w", encoding="utf-8") as f:
                    f.write(md_report + "\n")
                print(f"Saved evaluation report to: {args.output_report}")

            if args.output_json:
                with open(args.output_json, "w", encoding="utf-8") as f:
                    f.write(json.dumps(summary.to_dict(), indent=2) + "\n")
                print(f"\nSaved basket evaluation JSON to: {args.output_json}")

            return 0

        evaluator = V10HistoricalStrategyEvaluator()
        report = evaluator.evaluate(dataset, source_path=str(obs_path))

        summary_text = report.summary()
        print(summary_text)

        if args.output_json:
            with open(args.output_json, "w", encoding="utf-8") as f:
                f.write(report.to_json(indent=2))
            print(f"\nSaved V10 evaluation JSON to: {args.output_json}")

        if args.output_report:
            with open(args.output_report, "w", encoding="utf-8") as f:
                f.write(summary_text + "\n")
            print(f"Saved evaluation report to: {args.output_report}")

        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
