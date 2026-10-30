"""Data ingestion, aggregation, and process status service for the dashboard.

Reuses existing V1–V7 modules:
- EvidenceStore and schema 1.0 serialization
- V6 HistoricalEvaluator
- V7 EvidenceAuditor and V7HistoricalValidator
- Strict separation of real observation data from simulated mock fixtures
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from kalshi_arbitrage.evidence_storage import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceStore,
    observation_from_dict,
    opportunity_from_dict,
)
from kalshi_arbitrage.observer import MarketObservation
from kalshi_arbitrage.v7_validation import (
    EvidenceAuditor,
    V7HistoricalValidator,
    V7ValidationConfig,
)
from kalshi_arbitrage.evaluation import (
    EvaluationConfig,
    HistoricalDataset,
    HistoricalEvaluator,
)

logger = logging.getLogger(__name__)


def _safe_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _format_age(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = (now - dt).total_seconds()
    if diff < 0:
        return "future"
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h {int((diff % 3600) // 60)}m ago"
    return f"{int(diff // 86400)}d ago"


def normalize_breadth_strategy_type(strat: Optional[str]) -> str:
    """Normalize strategy identifiers across screening enums, candidate types, and UI filters."""
    if not strat:
        return ""
    s = str(strat).strip().lower()
    if s in ("mece_basket_long_yes", "mece_long_yes", "long_yes"):
        return "mece_basket_long_yes"
    if s in ("mece_basket_long_no", "mece_long_no", "long_no"):
        return "mece_basket_long_no"
    if s in ("binary_parity", "parity"):
        return "binary_parity"
    if s in ("crossed_book", "crossed_books", "signal"):
        return "crossed_book"
    return s


class DashboardDataService:
    """Read-only data service powering dashboard metrics, tables, and audit views."""

    def __init__(
        self,
        workspace_root: Optional[Union[str, Path]] = None,
        default_scanner_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        if workspace_root is None:
            self.workspace_root = Path(__file__).resolve().parents[2]
        else:
            self.workspace_root = Path(workspace_root).resolve()
        self.default_scanner_dir = Path(default_scanner_dir) if default_scanner_dir else None

    def get_available_datasets(self) -> List[Dict[str, Any]]:
        """Discover valid observation datasets across the workspace."""
        datasets = []
        candidate_paths = [
            self.workspace_root / "evidence_dashboard_trial",
            self.workspace_root / "evidence_production_trial",
            self.workspace_root / "data" / "v5_trial",
            self.workspace_root / "test_evidence_run",
            self.workspace_root / "test_evidence_run_prod",
        ]

        # Also search for any other directories containing observations.jsonl
        for path in self.workspace_root.glob("**/observations.jsonl"):
            parent = path.parent
            if parent not in candidate_paths and ".venv" not in str(parent) and ".git" not in str(parent):
                candidate_paths.append(parent)

        for candidate in candidate_paths:
            obs_file = candidate / "observations.jsonl" if candidate.is_dir() else candidate
            if obs_file.exists() and obs_file.is_file():
                try:
                    stat = obs_file.stat()
                    line_count = 0
                    with open(obs_file, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.strip():
                                line_count += 1

                    rel_name = str(obs_file.parent.relative_to(self.workspace_root))
                    if rel_name == ".":
                        rel_name = obs_file.name

                    datasets.append({
                        "id": rel_name,
                        "name": rel_name,
                        "path": str(obs_file.resolve()),
                        "record_count": line_count,
                        "size_bytes": stat.st_size,
                        "last_modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        "is_default": "evidence_dashboard_trial" in rel_name,
                    })
                except Exception as exc:
                    logger.warning(f"Error inspecting dataset candidate {candidate}: {exc}")

        # Sort so evidence_dashboard_trial is first, then by record count
        datasets.sort(key=lambda d: (not d["is_default"], -d["record_count"], d["name"]))
        return datasets

    def resolve_dataset_path(self, dataset_identifier: Optional[str] = None) -> Path:
        """Resolve a dataset name or relative path to an absolute Path."""
        if dataset_identifier == "live":
            live_path = self.workspace_root / "data" / "live_collection" / "observations.jsonl"
            if live_path.exists():
                return live_path

        if not dataset_identifier or dataset_identifier == "default":
            default_path = self.workspace_root / "evidence_dashboard_trial" / "observations.jsonl"
            if default_path.exists():
                return default_path
            # Fall back to first available
            available = self.get_available_datasets()
            if available:
                return Path(available[0]["path"])
            return default_path

        # Direct path check
        p = Path(dataset_identifier)
        if not p.is_absolute():
            p = (self.workspace_root / p).resolve()

        if p.is_dir():
            obs = p / "observations.jsonl"
            if obs.exists():
                return obs
            return p
        return p

    def get_observer_process_status(self) -> Dict[str, Any]:
        """Inspect running processes to determine if an observer is active.

        Never infers status from file presence; checks the actual OS process table.
        """
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode != 0:
                return {
                    "status": "UNKNOWN",
                    "pid": None,
                    "environment": None,
                    "command": None,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "reason": f"ps command returned exit code {res.returncode}",
                }

            lines = res.stdout.strip().split("\n")
            observer_procs = []
            for line in lines:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    pid_str, cmd = parts
                    if "scripts/run_observer.py" in cmd or ("run_observer" in cmd and "python" in cmd):
                        # Filter out grep or bash wrappers if any
                        if "grep" not in cmd:
                            try:
                                pid = int(pid_str)
                                env_match = re.search(r"--environment\s+(\w+)", cmd)
                                env = env_match.group(1) if env_match else "demo"
                                observer_procs.append({
                                    "pid": pid,
                                    "command": cmd,
                                    "environment": env,
                                })
                            except ValueError:
                                pass

            if observer_procs:
                proc = observer_procs[0]
                return {
                    "status": "RUNNING",
                    "pid": proc["pid"],
                    "environment": proc["environment"],
                    "command": proc["command"],
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "count": len(observer_procs),
                }
            else:
                return {
                    "status": "STOPPED",
                    "pid": None,
                    "environment": None,
                    "command": None,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "count": 0,
                }
        except Exception as exc:
            return {
                "status": "UNKNOWN",
                "pid": None,
                "environment": None,
                "command": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "reason": str(exc),
            }

    def load_observations(self, dataset_path: Path) -> Tuple[List[MarketObservation], int, int]:
        """Load observations with strict error tracking.

        Returns (valid_observations, malformed_count, total_raw_count).
        """
        if not dataset_path.exists():
            return [], 0, 0

        target_file = dataset_path if dataset_path.is_file() else dataset_path / "observations.jsonl"
        if not target_file.exists():
            return [], 0, 0

        valid_observations: List[MarketObservation] = []
        malformed_count = 0
        total_raw_count = 0

        with open(target_file, "r", encoding="utf-8") as f:
            for line in f:
                clean = line.strip()
                if not clean:
                    continue
                total_raw_count += 1
                try:
                    d = json.loads(clean)
                    obs = observation_from_dict(d)
                    valid_observations.append(obs)
                except Exception:
                    malformed_count += 1

        return valid_observations, malformed_count, total_raw_count

    def get_overview(self, dataset_identifier: Optional[str] = None) -> Dict[str, Any]:
        """Compile executive overview metrics for the selected dataset."""
        dataset_path = self.resolve_dataset_path(dataset_identifier)
        proc_status = self.get_observer_process_status()
        observations, malformed_count, total_raw = self.load_observations(dataset_path)

        unique_tickers = set()
        unique_events = set()
        api_success_count = 0
        api_failure_count = 0
        stale_count = 0
        empty_books_count = 0
        populated_books_count = 0
        earliest_dt: Optional[datetime] = None
        latest_dt: Optional[datetime] = None

        for obs in observations:
            unique_tickers.add(obs.ticker)
            if obs.market and obs.market.event_ticker:
                unique_events.add(obs.market.event_ticker)

            if obs.is_success:
                api_success_count += 1
            else:
                api_failure_count += 1

            if obs.is_stale:
                stale_count += 1

            if obs.order_book:
                has_bids = bool(obs.order_book.yes_bids or obs.order_book.no_bids)
                if has_bids:
                    populated_books_count += 1
                else:
                    empty_books_count += 1
            else:
                empty_books_count += 1

            if earliest_dt is None or obs.observed_at < earliest_dt:
                earliest_dt = obs.observed_at
            if latest_dt is None or obs.observed_at > latest_dt:
                latest_dt = obs.observed_at

        # Infer dataset environment from market metadata if possible
        dataset_env = "demo"
        if observations:
            first_m = observations[0].market
            if first_m and "KX" in first_m.ticker:
                # Demo markets on Kalshi often start with KXMVECROSSCATEGORY
                # But can be demo or production. We mark according to process or path.
                if "production" in str(dataset_path).lower():
                    dataset_env = "production"
                elif "demo" in str(dataset_path).lower():
                    dataset_env = "demo"
                elif proc_status["status"] == "RUNNING" and proc_status["environment"]:
                    dataset_env = proc_status["environment"]

        # Run fast candidate detection through evaluator
        theoretical_candidates = 0
        qualified_opportunities = 0
        rejected_opportunities = 0

        opp_file = dataset_path.parent / "opportunities.jsonl" if dataset_path.is_file() else dataset_path / "opportunities.jsonl"
        if opp_file.exists():
            try:
                with open(opp_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            d = json.loads(line)
                            theoretical_candidates += 1
                            if d.get("is_qualified"):
                                qualified_opportunities += 1
                            else:
                                rejected_opportunities += 1
            except Exception as exc:
                logger.warning(f"Error reading opportunities.jsonl: {exc}")

        rel_name = str(dataset_path.parent.relative_to(self.workspace_root)) if dataset_path.is_file() else str(dataset_path.relative_to(self.workspace_root))
        if rel_name == ".":
            rel_name = dataset_path.name

        return {
            "dataset_id": rel_name,
            "dataset_path": str(dataset_path),
            "observer_process": proc_status,
            "environment": dataset_env,
            "total_raw_records": total_raw,
            "valid_observations_count": len(observations),
            "malformed_records_count": malformed_count,
            "unique_markets_count": len(unique_tickers),
            "unique_events_count": len(unique_events),
            "earliest_observation_at": _safe_iso(earliest_dt),
            "latest_observation_at": _safe_iso(latest_dt),
            "latest_observation_age": _format_age(latest_dt),
            "is_data_historical": True,  # Recorded evidence is always historical
            "api_success_count": api_success_count,
            "api_failure_count": api_failure_count,
            "api_reliability_pct": round(api_success_count / (api_success_count + api_failure_count) * 100, 1) if (api_success_count + api_failure_count) > 0 else 0.0,
            "stale_observations_count": stale_count,
            "empty_books_count": empty_books_count,
            "populated_books_count": populated_books_count,
            "theoretical_candidates_count": theoretical_candidates,
            "qualified_opportunities_count": qualified_opportunities,
            "rejected_opportunities_count": rejected_opportunities,
            "collector_process": self.get_continuous_collector_process_status(),
            "evaluator_process": self.get_continuous_evaluator_process_status(),
            "scanner_process": self.get_breadth_scanner_process_status(),
        }

    def get_markets_table(self, dataset_identifier: Optional[str] = None) -> List[Dict[str, Any]]:
        """Extract aggregated market monitoring records by ticker."""
        dataset_path = self.resolve_dataset_path(dataset_identifier)
        observations, _, _ = self.load_observations(dataset_path)

        by_ticker: Dict[str, List[MarketObservation]] = {}
        for obs in observations:
            by_ticker.setdefault(obs.ticker, []).append(obs)

        results = []
        for ticker, obs_list in sorted(by_ticker.items()):
            latest_obs = obs_list[-1]
            m = latest_obs.market
            b = latest_obs.order_book

            # Order book status classification
            if not latest_obs.is_success or b is None:
                book_status = "INVALID"
            elif latest_obs.is_stale:
                book_status = "STALE"
            elif (not b.yes_bids) and (not b.no_bids):
                book_status = "EMPTY"
            else:
                book_status = "POPULATED"

            # Top of book resting bids
            top_yes = None
            if b and b.yes_bids:
                top_yes = {
                    "price_dollars": str(b.yes_bids[0].price_dollars),
                    "quantity": str(b.yes_bids[0].quantity),
                }

            top_no = None
            if b and b.no_bids:
                top_no = {
                    "price_dollars": str(b.no_bids[0].price_dollars),
                    "quantity": str(b.no_bids[0].quantity),
                }

            results.append({
                "ticker": ticker,
                "event_ticker": m.event_ticker if m else "N/A",
                "title": m.title if m else ticker,
                "status": m.status if m else "unknown",
                "market_type": m.market_type if m else "binary",
                "yes_bid_dollars": str(m.yes_bid_dollars) if m and m.yes_bid_dollars is not None else "0.0000",
                "yes_ask_dollars": str(m.yes_ask_dollars) if m and m.yes_ask_dollars is not None else "1.0000",
                "no_bid_dollars": str(m.no_bid_dollars) if m and m.no_bid_dollars is not None else "0.0000",
                "no_ask_dollars": str(m.no_ask_dollars) if m and m.no_ask_dollars is not None else "1.0000",
                "last_price_dollars": str(m.last_price_dollars) if m and m.last_price_dollars is not None else None,
                "volume": str(m.volume) if m and m.volume is not None else "0.00",
                "order_book_status": book_status,
                "yes_bids_levels_count": len(b.yes_bids) if b else 0,
                "no_bids_levels_count": len(b.no_bids) if b else 0,
                "top_yes_bid": top_yes,
                "top_no_bid": top_no,
                "observation_count": len(obs_list),
                "first_observed_at": _safe_iso(obs_list[0].observed_at),
                "latest_observed_at": _safe_iso(latest_obs.observed_at),
                "latest_source_timestamp": _safe_iso(latest_obs.source_timestamp),
                "is_stale": latest_obs.is_stale,
                "staleness_reason": latest_obs.staleness_reason,
                "error_message": latest_obs.error_message,
            })

        return results

    def get_markets_metadata(self, dataset_identifier: Optional[str] = None) -> Dict[str, Any]:
        """Return reconciled status counts and temporal metadata for the markets dataset."""
        dataset_path = self.resolve_dataset_path(dataset_identifier)
        observations, _, _ = self.load_observations(dataset_path)

        latest_dt: Optional[datetime] = None
        for obs in observations:
            if latest_dt is None or obs.observed_at > latest_dt:
                latest_dt = obs.observed_at

        coll_status = self.get_continuous_collector_status()
        is_coll_running = coll_status.get("process_running", False)
        now = datetime.now(timezone.utc)
        age_seconds = max(0.0, (now - latest_dt).total_seconds()) if latest_dt else None
        is_live = is_coll_running and (age_seconds is not None and age_seconds <= 60.0)

        markets = self.get_markets_table(dataset_identifier)
        counts = {"ALL": len(markets), "POPULATED": 0, "EMPTY": 0, "STALE": 0, "INVALID": 0}
        for m in markets:
            st = m.get("order_book_status", "EMPTY")
            counts[st] = counts.get(st, 0) + 1

        return {
            "total_markets": len(markets),
            "counts_by_status": counts,
            "latest_observation_at": _safe_iso(latest_dt),
            "latest_observation_age_seconds": age_seconds,
            "collector_running": is_coll_running,
            "is_live_stream": is_live,
            "provenance_label": "LIVE STREAM" if is_live else "PERSISTED SNAPSHOT",
        }

    def get_opportunities(
        self,
        dataset_identifier: Optional[str] = None,
        include_simulated: bool = False,
    ) -> Dict[str, Any]:
        """Retrieve genuine evaluated opportunities or simulated demonstration rows."""
        if include_simulated:
            return self._get_simulated_opportunities()

        dataset_path = self.resolve_dataset_path(dataset_identifier)
        opp_file = dataset_path.parent / "opportunities.jsonl" if dataset_path.is_file() else dataset_path / "opportunities.jsonl"

        opportunities = []
        if opp_file.exists():
            try:
                with open(opp_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            d = json.loads(line)
                            opp = opportunity_from_dict(d)
                            pricing = opp.pricing_result
                            cand = opp.candidate_opportunity
                            opportunities.append({
                                "opportunity_id": opp.opportunity_id,
                                "strategy_type": opp.strategy_type,
                                "observation_id": opp.observation_id,
                                "observed_at": _safe_iso(opp.observed_at),
                                "market_tickers": list(opp.market_tickers),
                                "status": opp.status,
                                "is_qualified": opp.is_qualified,
                                "rejection_reason": opp.rejection_reason,
                                "gross_edge_cents": cand.gross_edge_cents,
                                "gross_edge_dollars": str(cand.gross_edge_dollars),
                                "total_cost_cents": cand.total_cost_cents,
                                "total_cost_dollars": str(cand.total_cost_dollars),
                                "guaranteed_payout_cents": cand.guaranteed_payout_cents,
                                "supported_quantity": pricing.supported_quantity if pricing else 0,
                                "requested_quantity": pricing.requested_quantity if pricing else 1,
                                "fee_per_contract_cents": opp.fee_per_contract_cents,
                                "net_edge_cents": cand.net_edge_cents,
                                "net_edge_dollars": str(cand.net_edge_dollars) if cand.net_edge_dollars else None,
                                "is_simulated": False,
                            })
            except Exception as exc:
                logger.warning(f"Error loading recorded opportunities: {exc}")

        return {
            "is_simulated": False,
            "count": len(opportunities),
            "qualified_count": sum(1 for o in opportunities if o["is_qualified"]),
            "opportunities": opportunities,
            "message": (
                "No arbitrage opportunities detected in the selected historical evidence. "
                "All observed order books have 0 resting counterparty bids. "
                "Fail-closed financial integrity verified (no phantom trades manufactured)."
                if len(opportunities) == 0 else "Authentic evaluated opportunities from recorded evidence."
            ),
        }

    def _get_simulated_opportunities(self) -> Dict[str, Any]:
        """Return explicitly labeled SIMULATED demonstration rows to verify populated UI state."""
        return {
            "is_simulated": True,
            "disclaimer": "ATTENTION: VISIBLY LABELED SIMULATION DATA FOR UI TESTING ONLY. NOT FROM LIVE OR HISTORICAL EVIDENCE.",
            "count": 2,
            "qualified_count": 1,
            "opportunities": [
                {
                    "opportunity_id": "BP:KXMVECROSSCATEGORY-SIM-1",
                    "strategy_type": "BINARY_PARITY",
                    "observation_id": "obs-sim-001",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "market_tickers": ["KXMVECROSSCATEGORY-SIM-1"],
                    "status": "QUALIFIED",
                    "is_qualified": True,
                    "rejection_reason": None,
                    "gross_edge_cents": 4,
                    "gross_edge_dollars": "0.0400",
                    "total_cost_cents": 96,
                    "total_cost_dollars": "0.9600",
                    "guaranteed_payout_cents": 100,
                    "supported_quantity": 25,
                    "requested_quantity": 25,
                    "fee_per_contract_cents": 1,
                    "net_edge_cents": 2,
                    "net_edge_dollars": "0.0200",
                    "is_simulated": True,
                },
                {
                    "opportunity_id": "MECE:KXEVENT-SIM-BASKET:YES",
                    "strategy_type": "MECE_BASKET_LONG_YES",
                    "observation_id": "obs-sim-002",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "market_tickers": ["KXEVENT-SIM-LEG1", "KXEVENT-SIM-LEG2", "KXEVENT-SIM-LEG3"],
                    "status": "REJECTED_UNPROFITABLE_FEES",
                    "is_qualified": False,
                    "rejection_reason": "Unprofitable after estimated fees (gross edge 2¢ < fees 3¢)",
                    "gross_edge_cents": 2,
                    "gross_edge_dollars": "0.0200",
                    "total_cost_cents": 98,
                    "total_cost_dollars": "0.9800",
                    "guaranteed_payout_cents": 100,
                    "supported_quantity": 10,
                    "requested_quantity": 10,
                    "fee_per_contract_cents": 1,
                    "net_edge_cents": -1,
                    "net_edge_dollars": "-0.0100",
                    "is_simulated": True,
                },
            ],
            "message": "Simulated demonstration records loaded successfully for UI layout verification.",
        }

    def get_historical_validation(self, dataset_identifier: Optional[str] = None) -> Dict[str, Any]:
        """Run V7 EvidenceAuditor and V7HistoricalValidator and return 10-section report dict."""
        dataset_path = self.resolve_dataset_path(dataset_identifier)
        if not dataset_path.exists():
            return {
                "error": f"Dataset file {dataset_path} does not exist",
                "exists": False,
            }

        try:
            eval_cfg = EvaluationConfig(
                default_fee_per_contract_cents=1,
                conservative_haircut_cents=0,
                max_stale_seconds=60.0,
                target_quantity=1,
                enable_paper_trading=False,
            )
            cfg = V7ValidationConfig(
                evaluation_config=eval_cfg,
                gap_threshold_seconds=60.0,
                audit_only=False,
                run_sensitivity=False,
            )
            dataset = HistoricalDataset.from_file(dataset_path)
            validator = V7HistoricalValidator(config=cfg)
            report = validator.validate(dataset)
            report_dict = report.to_dict()
            report_dict["human_summary"] = report.summary()
            report_dict["exists"] = True

            # Populate convenience aliases for frontend rendering
            s1 = report_dict.get("section_1_dataset_identity", {})
            s2 = report_dict.get("section_2_evidence_quality", {})

            if "time_span_seconds" in s1 and "total_duration" not in s1:
                secs = s1.get("time_span_seconds") or 0.0
                if secs < 60:
                    s1["total_duration"] = f"{secs:.1f}s"
                elif secs < 3600:
                    s1["total_duration"] = f"{secs/60:.1f}m"
                else:
                    s1["total_duration"] = f"{secs/3600:.1f}h"

            if "fresh_records_count" in s2 and "valid_records_count" in s2:
                total_val = s2.get("valid_records_count", 0)
                fresh_val = s2.get("fresh_records_count", 0)
                rate = (fresh_val / total_val * 100.0) if total_val > 0 else 100.0
                s2["freshness_pass_rate"] = f"{rate:.1f}%"

            if "empty_books_count" in s2:
                s2["empty_books"] = s2.get("empty_books_count", 0)
                total_val = s2.get("valid_records_count", 0)
                s2["empty_books_pct"] = round((s2["empty_books"] / total_val * 100.0), 1) if total_val > 0 else 0.0

            if "total_bid_depth_contracts" in s2:
                s2["total_cumulative_bid_depth"] = f"{s2.get('total_bid_depth_contracts', 0)} contracts"

            return report_dict
        except Exception as exc:
            logger.exception(f"Error executing V7 validation: {exc}")
            return {
                "error": str(exc),
                "exists": True,
            }

    def get_v8_simulation(
        self,
        dataset_identifier: Optional[str] = None,
        latency_ms: float = 50.0,
        enforce_universe: bool = True,
    ) -> Dict[str, Any]:
        """Run V8 Realistic Execution & Risk Simulation on dataset and return report dict."""
        dataset_path = self.resolve_dataset_path(dataset_identifier)
        if not dataset_path.exists():
            return {
                "error": f"Dataset file {dataset_path} does not exist",
                "exists": False,
            }

        try:
            from kalshi_arbitrage.v8_simulation import (
                V8SimulationConfig,
                V8SimulationEngine,
            )
            from kalshi_arbitrage.async_execution import MicrostructureConfig
            from kalshi_arbitrage.fee_model import FeeModelConfig, FeeScheduleType

            fee_config = FeeModelConfig(schedule_type=FeeScheduleType.KALSHI_OFFICIAL_TAKER)
            micro_config = MicrostructureConfig(
                network_latency_ms=latency_ms,
                enable_latency_decay=True,
                fee_config=fee_config,
            )
            v8_config = V8SimulationConfig(
                microstructure_config=micro_config,
                fee_config=fee_config,
                enforce_universe=enforce_universe,
            )
            dataset = HistoricalDataset.from_file(dataset_path)
            engine = V8SimulationEngine(config=v8_config)
            report = engine.run(dataset)
            report_dict = report.to_dict()
            report_dict["human_summary"] = report.summary()
            report_dict["exists"] = True
            return report_dict
        except Exception as exc:
            logger.exception(f"Error executing V8 simulation: {exc}")
            return {
                "error": str(exc),
                "exists": True,
            }

    def get_system_health(self) -> Dict[str, Any]:
        """Aggregate system diagnostics, observer process health, and dataset storage metrics."""
        proc_status = self.get_observer_process_status()
        datasets = self.get_available_datasets()

        total_storage_bytes = sum(d["size_bytes"] for d in datasets)
        total_records = sum(d["record_count"] for d in datasets)

        # Warnings list
        warnings = []
        if proc_status["status"] == "STOPPED":
            warnings.append("Market Observer process is currently stopped. No live polling is in progress.")
        elif proc_status["status"] == "UNKNOWN":
            warnings.append("Could not determine observer process status from the OS process table.")

        default_trial = next((d for d in datasets if "evidence_dashboard_trial" in d["id"]), None)
        if default_trial and default_trial["record_count"] == 0:
            warnings.append("Default trial evidence directory has 0 records.")

        paper_health = self.get_paper_trading_status()
        if paper_health.get("kill_switch_active"):
            warnings.append(f"Paper trading kill-switch is active: {paper_health.get('kill_switch_reason')}")

        collector_health = self.get_continuous_collector_status()
        if collector_health.get("status") == "STOPPED":
            warnings.append("Continuous live collector is currently stopped.")

        evaluator_health = self.get_continuous_evaluator_status()
        if evaluator_health.get("status") == "STOPPED":
            warnings.append("Continuous live evaluator is currently stopped.")

        breadth_health = self.get_breadth_scanner_status()
        if breadth_health.get("is_stale") and breadth_health.get("status") == "RUNNING":
            warnings.append(
                f"Breadth scanner telemetry is stale ({int(breadth_health.get('staleness_seconds') or 0)}s old). Old data is not live."
            )

        live_summary = self.get_live_pipeline_summary()

        return {
            "server_time": datetime.now(timezone.utc).isoformat(),
            "observer_process": proc_status,
            "continuous_collector": collector_health,
            "continuous_evaluator": evaluator_health,
            "breadth_scanner": breadth_health,
            "live_pipeline": live_summary,
            "alert_service": self.get_alert_service_status(),
            "paper_trading": paper_health,
            "datasets_count": len(datasets),
            "total_records_stored": total_records,
            "total_storage_bytes": total_storage_bytes,
            "warnings": warnings,
            "workspace_root": str(self.workspace_root),
        }

    def get_continuous_collector_process_status(self) -> Dict[str, Any]:
        """Audit OS process table to determine if run_continuous_collector.py is actively executing."""
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode != 0:
                return {"is_running": False, "pid": None, "reason": f"ps exit code {res.returncode}"}

            lines = res.stdout.strip().split("\n")
            for line in lines:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    pid_str, cmd = parts
                    if "scripts/run_continuous_collector.py" in cmd or ("run_continuous_collector" in cmd and "python" in cmd):
                        if "grep" not in cmd and "pytest" not in cmd:
                            try:
                                return {"is_running": True, "pid": int(pid_str), "command": cmd}
                            except ValueError:
                                pass
            return {"is_running": False, "pid": None}
        except Exception as exc:
            return {"is_running": False, "pid": None, "reason": str(exc)}

    def get_continuous_collector_status(
        self, state_filepath: Optional[Union[str, Path]] = None
    ) -> Dict[str, Any]:
        """Read-only telemetry for the continuous live market data collector."""
        path = (
            Path(state_filepath)
            if state_filepath is not None
            else self.workspace_root / "data" / "live_collector_state.json"
        )

        proc_status = self.get_continuous_collector_process_status()
        is_proc_running = proc_status.get("is_running", False)

        if not path.exists():
            return {
                "status": "RUNNING" if is_proc_running else "STOPPED",
                "process_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "session_id": None,
                "last_successful_collection_time": None,
                "latest_snapshot_timestamp": None,
                "latest_snapshot_age_seconds": None,
                "is_data_fresh": False,
                "consecutive_failures": 0,
                "successful_cycles_count": 0,
                "failed_cycles_count": 0,
                "total_cycles_count": 0,
                "active_monitored_markets_count": 0,
                "message": "No continuous collector state file found.",
                "state_file": str(path),
            }

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            now = datetime.now(timezone.utc)
            snapshot_age = None
            snap_str = data.get("latest_snapshot_timestamp")
            if snap_str:
                try:
                    dt = datetime.fromisoformat(snap_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    snapshot_age = max(0.0, (now - dt).total_seconds())
                except Exception:
                    pass

            persisted_status = data.get("status", "STOPPED")
            if is_proc_running:
                status_label = "RUNNING"
            else:
                status_label = persisted_status if persisted_status in ("STOPPED", "FAILED") else "STOPPED"

            max_stale = float(data.get("max_stale_seconds", 60.0))
            is_fresh = (
                status_label == "RUNNING"
                and int(data.get("consecutive_failures", 0)) == 0
                and snapshot_age is not None
                and snapshot_age <= max_stale
            )

            res = dict(data)
            res.update({
                "status": status_label,
                "process_running": is_proc_running,
                "is_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "pid": proc_status.get("pid"),
                "persisted_pid": data.get("pid"),
                "latest_snapshot_age_seconds": snapshot_age,
                "is_data_fresh": is_fresh,
                "state_file": str(path),
            })
            return res
        except Exception as exc:
            logger.warning(f"Error loading continuous collector state from {path}: {exc}")
            return {
                "status": "ERROR",
                "process_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "error": str(exc),
                "state_file": str(path),
            }

    def get_continuous_evaluator_process_status(self) -> Dict[str, Any]:
        """Audit OS process table to determine if run_continuous_evaluator.py is actively executing."""
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode != 0:
                return {"is_running": False, "pid": None, "reason": f"ps exit code {res.returncode}"}

            lines = res.stdout.strip().split("\n")
            for line in lines:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    pid_str, cmd = parts
                    if "scripts/run_continuous_evaluator.py" in cmd or ("run_continuous_evaluator" in cmd and "python" in cmd):
                        if "grep" not in cmd and "pytest" not in cmd:
                            try:
                                return {"is_running": True, "pid": int(pid_str), "command": cmd}
                            except ValueError:
                                pass
            return {"is_running": False, "pid": None}
        except Exception as exc:
            return {"is_running": False, "pid": None, "reason": str(exc)}

    def get_continuous_evaluator_status(
        self, state_filepath: Optional[Union[str, Path]] = None
    ) -> Dict[str, Any]:
        """Read-only telemetry for the continuous live arbitrage detection engine."""
        path = (
            Path(state_filepath)
            if state_filepath is not None
            else self.workspace_root / "data" / "live_evaluator_state.json"
        )

        proc_status = self.get_continuous_evaluator_process_status()
        is_proc_running = proc_status.get("is_running", False)

        if not path.exists():
            return {
                "status": "RUNNING" if is_proc_running else "STOPPED",
                "process_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "session_id": None,
                "last_evaluation_time": None,
                "latest_input_timestamp": None,
                "evaluations_count": 0,
                "processed_observations_count": 0,
                "active_candidates_count": 0,
                "total_candidates_detected": 0,
                "candidate_transitions_count": 0,
                "message": "No continuous evaluator state file found.",
                "state_file": str(path),
            }

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            persisted_status = data.get("status", "STOPPED")
            if is_proc_running:
                status_label = "RUNNING"
            else:
                status_label = persisted_status if persisted_status in ("STOPPED", "FAILED") else "STOPPED"

            res = dict(data)
            res.update({
                "status": status_label,
                "process_running": is_proc_running,
                "is_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "pid": proc_status.get("pid"),
                "persisted_pid": data.get("pid"),
                "state_file": str(path),
            })
            return res
        except Exception as exc:
            logger.warning(f"Error loading continuous evaluator state from {path}: {exc}")
            return {
                "status": "ERROR",
                "process_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "error": str(exc),
                "state_file": str(path),
            }

    def get_live_pipeline_summary(self) -> Dict[str, Any]:
        """Aggregate operational telemetry across collector, evaluator, and execution qualification."""
        now = datetime.now(timezone.utc)
        collector = self.get_continuous_collector_status()
        evaluator = self.get_continuous_evaluator_status()

        collector_running = collector.get("process_running", False) or collector.get("status") == "RUNNING"
        evaluator_running = evaluator.get("process_running", False) or evaluator.get("status") == "RUNNING"

        # Determine overall pipeline status
        if collector_running and evaluator_running:
            is_fresh = collector.get("is_data_fresh", False)
            failures = int(collector.get("consecutive_failures", 0))
            if is_fresh and failures == 0:
                pipeline_status = "HEALTHY"
            else:
                pipeline_status = "DEGRADED"
        elif collector_running or evaluator_running:
            pipeline_status = "PARTIAL"
        else:
            pipeline_status = "STOPPED"

        provenance = "LIVE" if (collector_running or evaluator_running) else "STOPPED"

        # Recent error messages
        recent_errors: List[str] = []
        if collector.get("last_error"):
            recent_errors.append(f"Collector Error: {collector['last_error']}")
        if evaluator.get("last_error"):
            recent_errors.append(f"Evaluator Error: {evaluator['last_error']}")

        # Candidate and qualification metrics
        active_cands = evaluator.get("active_candidates", [])
        qual_count = int(evaluator.get("qualified_candidates_count", 0))
        rej_count = int(evaluator.get("rejected_candidates_count", 0))
        qual_summary = evaluator.get("qualification_summary", {})
        outcomes = dict(qual_summary.get("outcomes_breakdown", {}))

        # Alert Engine telemetry (V24)
        alert_status = self.get_alert_service_status()

        return {
            "pipeline_status": pipeline_status,
            "provenance": provenance,
            "server_time": now.isoformat(),
            "collector": collector,
            "evaluator": evaluator,
            "alerts": alert_status,
            "collector_status": collector.get("status", "STOPPED"),
            "evaluator_status": evaluator.get("status", "STOPPED"),
            "alert_status": alert_status.get("status", "STOPPED"),
            "latest_snapshot_timestamp": collector.get("latest_snapshot_timestamp"),
            "latest_snapshot_age_seconds": collector.get("latest_snapshot_age_seconds"),
            "is_data_fresh": collector.get("is_data_fresh", False),
            "last_evaluation_time": evaluator.get("last_evaluation_time"),
            "active_monitored_markets_count": collector.get("active_monitored_markets_count", 0),
            "processed_observations_count": evaluator.get("processed_observations_count", 0),
            "active_candidates_count": evaluator.get("active_candidates_count", 0),
            "qualified_candidates_count": qual_count,
            "rejected_candidates_count": rej_count,
            "outcomes_breakdown": outcomes,
            "alerts_created_count": alert_status.get("alerts_created_count", 0),
            "alerts_delivered_count": alert_status.get("alerts_delivered_count", 0),
            "alerts_failed_count": alert_status.get("alerts_failed_count", 0),
            "recent_errors": recent_errors,
        }

    def get_continuous_evaluator_candidates(
        self,
        state_filepath: Optional[Union[str, Path]] = None,
        strategy: Optional[str] = None,
        outcome: Optional[str] = None,
        state: Optional[str] = None,
        market: Optional[str] = None,
        is_qualified: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Return list of currently active arbitrage candidates from state file, with optional filters."""
        status = self.get_continuous_evaluator_status(state_filepath)
        candidates = list(status.get("active_candidates", []))

        filtered = []
        for cand in candidates:
            # Strategy filter
            if strategy and cand.get("strategy_type") != strategy:
                continue

            # State filter
            if state and cand.get("state") != state:
                continue

            # Qualification filter
            cand_is_qual = cand.get("is_qualified", False)
            if is_qualified is not None and cand_is_qual != is_qualified:
                continue

            # Outcome filter
            qual = cand.get("qualification")
            cand_outcome = qual.get("outcome") if isinstance(qual, dict) else cand.get("status")
            if outcome and cand_outcome != outcome:
                continue

            # Market filter
            if market:
                market_lower = market.lower()
                cand_id = str(cand.get("candidate_id", "")).lower()
                cand_market = str(cand.get("market_ticker", "")).lower()
                cand_markets = [str(m).lower() for m in cand.get("market_tickers", ())]
                if market_lower not in cand_id and market_lower not in cand_market and not any(market_lower in m for m in cand_markets):
                    continue

            filtered.append(cand)

        return filtered

    def get_continuous_evaluator_qualified_candidates(
        self,
        state_filepath: Optional[Union[str, Path]] = None,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        min_profit_cents: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return list of currently active candidates that are execution-qualified, with optional filters."""
        candidates = self.get_continuous_evaluator_candidates(
            state_filepath=state_filepath,
            strategy=strategy,
            market=market,
            is_qualified=True,
        )

        qualified = []
        for cand in candidates:
            qual = cand.get("qualification")
            if isinstance(qual, dict):
                if qual.get("is_qualified") is True:
                    if min_profit_cents is not None:
                        net_cents = qual.get("net_edge_cents", 0)
                        if net_cents < min_profit_cents:
                            continue
                    qualified.append(cand)
            elif cand.get("is_qualified") is True:
                if min_profit_cents is not None:
                    net_cents = cand.get("net_edge_cents", 0)
                    if net_cents < min_profit_cents:
                        continue
                qualified.append(cand)

        return qualified

    def get_continuous_evaluator_history(
        self,
        history_filepath: Optional[Union[str, Path]] = None,
        limit: int = 100,
        candidate_id: Optional[str] = None,
        to_state: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return candidate lifecycle transition history from JSONL file in reverse order, with optional filters."""
        path = (
            Path(history_filepath)
            if history_filepath is not None
            else self.workspace_root / "data" / "live_candidate_history.jsonl"
        )
        if not path.exists():
            return []

        records = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            # Candidate filter
                            if candidate_id and record.get("candidate_id") != candidate_id:
                                continue
                            # To state filter
                            if to_state and record.get("to_state") != to_state:
                                continue
                            # Outcome filter
                            if outcome and record.get("qualification_outcome") != outcome:
                                continue
                            records.append(record)
                        except Exception:
                            pass
            records.reverse()
            return records[:limit]
        except Exception as exc:
            logger.warning(f"Error reading candidate history from {path}: {exc}")
            return []

    def get_alert_service_status(
        self, state_filepath: Optional[Union[str, Path]] = None
    ) -> Dict[str, Any]:
        """Read-only telemetry for the V24 qualified-opportunity alert engine."""
        path = (
            Path(state_filepath)
            if state_filepath is not None
            else self.workspace_root / "data" / "live_alert_state.json"
        )
        if not path.exists():
            return {
                "status": "STOPPED",
                "enabled": False,
                "enabled_channels": ["local_history"],
                "alerts_created_count": 0,
                "alerts_delivered_count": 0,
                "alerts_failed_count": 0,
                "pending_queue_depth": 0,
                "last_alert_time": None,
                "last_delivery_time": None,
                "channel_last_delivery_time": {},
                "tracked_candidates_count": 0,
                "currently_qualified_count": 0,
                "recent_delivery_errors": [],
                "state_file": str(path),
            }

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Reconcile status with continuous evaluator daemon process
            eval_proc = self.get_continuous_evaluator_process_status()
            is_eval_running = eval_proc.get("is_running", False)
            persisted_status = data.get("status", "STOPPED")
            status_label = "RUNNING" if is_eval_running else (
                persisted_status if persisted_status in ("STOPPED", "DISABLED") else "STOPPED"
            )

            res = dict(data)
            res.update({
                "status": status_label,
                "state_file": str(path),
            })
            return res
        except Exception as exc:
            logger.warning(f"Error loading alert state from {path}: {exc}")
            return {
                "status": "ERROR",
                "error": str(exc),
                "state_file": str(path),
            }

    def get_alert_history(
        self,
        history_filepath: Optional[Union[str, Path]] = None,
        limit: int = 50,
        event_type: Optional[str] = None,
        candidate_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Read persisted alert history in reverse chronological order with optional filters."""
        path = (
            Path(history_filepath)
            if history_filepath is not None
            else self.workspace_root / "data" / "live_alert_history.jsonl"
        )
        if not path.exists():
            return []

        try:
            alerts: List[Dict[str, Any]] = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    clean = line.strip()
                    if clean:
                        try:
                            record = json.loads(clean)
                            if event_type and record.get("event_type") != event_type:
                                continue
                            if candidate_id and record.get("candidate_id") != candidate_id:
                                continue
                            alerts.append(record)
                        except Exception:
                            pass
            alerts.reverse()
            return alerts[:limit]
        except Exception as exc:
            logger.warning(f"Error reading alert history from {path}: {exc}")
            return []

    def get_paper_trader_process_status(self) -> Dict[str, Any]:
        """Audit OS process table to determine if run_paper_trader.py is actively executing."""
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode != 0:
                return {"is_running": False, "pid": None, "reason": f"ps exit code {res.returncode}"}

            lines = res.stdout.strip().split("\n")
            for line in lines:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    pid_str, cmd = parts
                    if "scripts/run_paper_trader.py" in cmd or ("run_paper_trader" in cmd and "python" in cmd):
                        if "grep" not in cmd and "pytest" not in cmd:
                            try:
                                return {"is_running": True, "pid": int(pid_str), "command": cmd}
                            except ValueError:
                                pass
            return {"is_running": False, "pid": None}
        except Exception as exc:
            return {"is_running": False, "pid": None, "reason": str(exc)}

    def get_paper_trading_status(
        self, state_filepath: Optional[Union[str, Path]] = None
    ) -> Dict[str, Any]:
        """Read-only paper trading status, portfolio metrics, and cycle health."""
        path = (
            Path(state_filepath)
            if state_filepath is not None
            else self.workspace_root / "data" / "paper_trading_state.json"
        )
        if not path.exists():
            return {
                "is_active": False,
                "status": "STOPPED",
                "process_running": False,
                "process_pid": None,
                "execution_mode": "paper",
                "message": "No active paper trading session file found.",
                "state_file": str(path),
            }

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            proc_status = self.get_paper_trader_process_status()
            is_proc_running = proc_status.get("is_running", False)
            persisted_status = data.get("status", "STOPPED")

            # Active only if a live process is running.
            if is_proc_running:
                is_active = True
                status_label = "RUNNING"
            else:
                is_active = False
                status_label = persisted_status if persisted_status in ("STOPPED", "FAILED") else "STOPPED"

            portfolio = data.get("portfolio", {})
            trades = data.get("trades", [])
            completed_arb = sum(
                1
                for t in trades
                if t.get("state") in ("FILLED", "CLOSED")
                and all(
                    l.get("filled_quantity", 0) == t.get("target_quantity", 1)
                    for l in t.get("legs", [])
                )
            )

            avail_cash = int(portfolio.get("available_cash_cents", 0))
            positions = portfolio.get("positions", [])
            cost_basis = sum(int(p.get("total_cost_cents", 0)) for p in positions)
            equity = avail_cash + cost_basis

            return {
                "is_active": is_active,
                "status": status_label,
                "process_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "execution_mode": "paper",
                "session_id": data.get("session_id"),
                "last_completed_cycle": data.get("last_completed_cycle", 0),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "initial_cash_cents": data.get("initial_cash_cents", 1_000_000),
                "available_cash_cents": avail_cash,
                "reserved_cash_cents": int(portfolio.get("reserved_cash_cents", 0)),
                "total_cost_basis_cents": cost_basis,
                "book_value_equity_cents": equity,
                "realized_pnl_cents": int(portfolio.get("realized_pnl_cents", 0)),
                "total_fees_cents": int(portfolio.get("total_fees_cents", 0)),
                "total_trades": len(trades),
                "completed_arbitrage_trades": completed_arb,
                "unwound_or_partial_trades": len(trades) - completed_arb,
                "open_positions": positions,
                "recent_cycles": data.get("cycle_history", [])[-10:],
                "recovery_events_count": len(data.get("recovery_events", [])),
                "state_file": str(path),
            }
        except Exception as exc:
            logger.exception(f"Error loading paper trading state from {path}: {exc}")
            return {
                "is_active": False,
                "status": "ERROR",
                "process_running": False,
                "process_pid": None,
                "execution_mode": "paper",
                "error": str(exc),
                "state_file": str(path),
            }

    def get_breadth_scanner_process_status(self) -> Dict[str, Any]:
        """Audit OS process table to determine if run_breadth_scanner.py is actively executing."""
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode != 0:
                return {"is_running": False, "pid": None, "reason": f"ps exit code {res.returncode}"}

            lines = res.stdout.strip().split("\n")
            for line in lines:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    pid_str, cmd = parts
                    if "scripts/run_breadth_scanner.py" in cmd or ("run_breadth_scanner" in cmd and "python" in cmd):
                        if "grep" not in cmd and "pytest" not in cmd:
                            try:
                                return {"is_running": True, "pid": int(pid_str), "command": cmd}
                            except ValueError:
                                pass
            return {"is_running": False, "pid": None}
        except Exception as exc:
            return {"is_running": False, "pid": None, "reason": str(exc)}

    def _resolve_breadth_scanner_dir(
        self, scanner_dir: Optional[Union[str, Path]] = None
    ) -> Path:
        """Resolve breadth scanner directory from argument, instance default, env, or project default."""
        if scanner_dir is not None:
            p = Path(scanner_dir)
            if p.is_absolute():
                return p
            direct = self.workspace_root / p
            if direct.exists():
                return direct
            nested = self.workspace_root / "data" / "breadth_scanner" / p
            if nested.exists():
                return nested
            return direct
        if self.default_scanner_dir is not None:
            return (
                self.default_scanner_dir
                if self.default_scanner_dir.is_absolute()
                else (self.workspace_root / self.default_scanner_dir)
            )
        env_dir = os.environ.get("KALSHI_SCANNER_DIR") or os.environ.get("BREADTH_SCANNER_DIR")
        if env_dir:
            p = Path(env_dir)
            return p if p.is_absolute() else (self.workspace_root / p)

        # Default: locate latest valid session directory under data/breadth_scanner/
        base = self.workspace_root / "data" / "breadth_scanner"
        if base.exists():
            candidates = []
            for child in base.iterdir():
                state_file = child / "scanner_state.json"
                if child.is_dir() and state_file.exists():
                    try:
                        candidates.append((child, state_file.stat().st_mtime))
                    except Exception:
                        pass
            if candidates:
                candidates.sort(key=lambda x: x[1], reverse=True)
                return candidates[0][0]

        return base

    def get_breadth_scanner_sessions(self) -> List[Dict[str, Any]]:
        """Discover all completed or active breadth scanner trial sessions."""
        base = self.workspace_root / "data" / "breadth_scanner"
        if not base.exists():
            return []

        sessions = []
        for child in base.iterdir():
            state_file = child / "scanner_state.json"
            if child.is_dir() and state_file.exists():
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    stat = state_file.stat()
                    sessions.append({
                        "id": child.name,
                        "name": child.name,
                        "display_name": child.name,
                        "path": str(child),
                        "session_id": data.get("session_id"),
                        "status": data.get("status", "STOPPED"),
                        "updated_at": data.get("updated_at") or data.get("last_persistence_time"),
                        "mtime": stat.st_mtime,
                        "mtime_iso": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        "total_unique_markets_discovered": data.get("total_unique_markets_discovered", 0),
                        "total_strategies_evaluated": data.get("total_strategies_evaluated", 0),
                        "qualified_candidates_count": data.get("qualified_candidates_count", 0),
                    })
                except Exception as exc:
                    logger.warning(f"Error inspecting scanner session in {child}: {exc}")

        sessions.sort(key=lambda s: s["mtime"], reverse=True)
        if sessions:
            sessions[0]["is_latest"] = True
        return sessions

    def get_breadth_scanner_status(
        self,
        scanner_dir: Optional[Union[str, Path]] = None,
        max_stale_seconds: float = 60.0,
    ) -> Dict[str, Any]:
        """Read-only telemetry for the Phase 2A–2D breadth-first continuous scanner."""
        base_dir = self._resolve_breadth_scanner_dir(scanner_dir)
        state_path = base_dir / "scanner_state.json"
        proc_status = self.get_breadth_scanner_process_status()
        is_proc_running = proc_status.get("is_running", False)

        now = datetime.now(timezone.utc)

        if not state_path.exists():
            return {
                "status": "INITIALIZING" if is_proc_running else "STOPPED",
                "process_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "session_id": None,
                "started_at": None,
                "updated_at": None,
                "current_cycle": 0,
                "total_cycles_count": 0,
                "successful_cycles_count": 0,
                "failed_cycles_count": 0,
                "last_successful_discovery_time": None,
                "last_catalog_complete": False,
                "total_unique_markets_discovered": 0,
                "total_eligible_markets": 0,
                "total_unique_markets_screened": 0,
                "total_events_evaluated": 0,
                "total_strategies_evaluated": 0,
                "total_preliminary_candidates_generated": 0,
                "candidates_enqueued": 0,
                "candidates_pending": 0,
                "candidates_deduplicated": 0,
                "qualification_counts": {},
                "qualified_candidates_count": 0,
                "api_requests_count": 0,
                "api_retries_count": 0,
                "api_failures_count": 0,
                "rate_limit_events_count": 0,
                "queue_overflow_count": 0,
                "last_error": None,
                "last_error_time": None,
                "output_dir": str(base_dir),
                "state_file": str(state_path),
                "staleness_seconds": None,
                "is_stale": False,
                "is_data_fresh": False,
                "warnings": ["Breadth scanner has not run or no state file found."],
                "message": "No breadth scanner state file found.",
                "screening_strategy_counts": None,
                "qualification_strategy_counts": None,
            }

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Compute staleness based on updated_at or last_persistence_time
            upd_str = data.get("updated_at") or data.get("last_persistence_time")
            staleness_seconds = None
            if upd_str:
                try:
                    dt = datetime.fromisoformat(upd_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    staleness_seconds = max(0.0, (now - dt).total_seconds())
                except Exception:
                    pass

            is_stale = (
                staleness_seconds is not None and staleness_seconds > max_stale_seconds
            )
            persisted_status = data.get("status", "STOPPED")

            if is_proc_running:
                status_label = persisted_status if persisted_status in ("RUNNING", "INITIALIZING", "ERROR") else "RUNNING"
            else:
                if persisted_status in ("STOPPED", "FAILED", "ERROR"):
                    status_label = persisted_status
                else:
                    status_label = "STOPPED"

            # Freshness rule: running, updated within threshold, and no process deadlocks
            is_fresh = (
                status_label == "RUNNING"
                and not is_stale
                and staleness_seconds is not None
            )

            warnings: List[str] = []
            if not is_proc_running and status_label == "STOPPED":
                warnings.append("Breadth scanner process is currently stopped.")
            if is_stale:
                warnings.append(
                    f"Scanner telemetry is stale ({int(staleness_seconds or 0)}s old > {int(max_stale_seconds)}s limit). Old data is not live."
                )

            if data.get("total_cycles_count", 0) > 0 and not data.get("last_catalog_complete", True):
                warnings.append("Latest catalog discovery was partial or incomplete.")

            if data.get("is_currently_overrunning"):
                warnings.append(
                    f"Scanner cycle overran scheduled interval ({data.get('last_cycle_duration_seconds', 0):.1f}s > {data.get('scheduled_interval_seconds', 0):.1f}s)."
                )

            if data.get("queue_overflow_count", 0) > 0:
                warnings.append(
                    f"{data['queue_overflow_count']} candidate(s) dropped due to qualification queue overflow."
                )

            if data.get("api_failures_count", 0) > 0:
                warnings.append(f"{data['api_failures_count']} order-book API request(s) failed.")

            if data.get("rate_limit_events_count", 0) > 0:
                warnings.append(f"{data['rate_limit_events_count']} rate limit event(s) encountered.")

            if data.get("last_error"):
                warnings.append(f"Last scanner error: {data['last_error']}")

            # Strategy breakdown aggregation from persistent history records
            strat_summary = self.get_breadth_scanner_strategy_breakdown(scanner_dir=base_dir)
            has_records = strat_summary.get("has_candidate_records", False)
            total_prelim = data.get("total_preliminary_candidates_generated", 0)

            if has_records:
                screening_counts = strat_summary["preliminary_by_strategy"]
            elif total_prelim == 0:
                screening_counts = {
                    "binary_parity": 0,
                    "mece_basket_long_yes": 0,
                    "mece_basket_long_no": 0,
                    "mece_long_yes": 0,
                    "mece_long_no": 0,
                    "crossed_book": 0,
                    "total_unique": 0,
                }
            else:
                # History file missing or empty but candidates were reported
                screening_counts = None

            res = dict(data)
            res.update({
                "status": status_label,
                "process_running": is_proc_running,
                "is_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "pid": proc_status.get("pid"),
                "persisted_pid": data.get("pid"),
                "staleness_seconds": staleness_seconds,
                "is_stale": is_stale,
                "is_data_fresh": is_fresh,
                "max_stale_seconds": max_stale_seconds,
                "output_dir": str(base_dir),
                "state_file": str(state_path),
                "warnings": warnings,
                "screening_strategy_counts": screening_counts,
                "qualification_strategy_counts": strat_summary.get("qualification_by_strategy"),
            })
            return res
        except Exception as exc:
            logger.warning(f"Error loading breadth scanner state from {state_path}: {exc}")
            return {
                "status": "ERROR",
                "process_running": is_proc_running,
                "process_pid": proc_status.get("pid"),
                "error": str(exc),
                "state_file": str(state_path),
                "staleness_seconds": None,
                "is_stale": True,
                "is_data_fresh": False,
                "warnings": [f"Failed to parse scanner state file: {exc}"],
                "screening_strategy_counts": None,
                "qualification_strategy_counts": None,
            }

    def get_breadth_scanner_strategy_breakdown(
        self,
        scanner_dir: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Any]:
        """Aggregate unique preliminary candidates and qualification outcomes by strategy.

        Reads candidate_history.jsonl and qualification_history.jsonl directly.
        Deduplicates records by unique candidate key (candidate_id / stable_key) to avoid
        double-counting repeated updates across cycles or quote snapshots.
        """
        base_dir = self._resolve_breadth_scanner_dir(scanner_dir)
        cand_path = base_dir / "candidate_history.jsonl"
        qual_path = base_dir / "qualification_history.jsonl"

        prelim_counts: Dict[str, Any] = {
            "binary_parity": 0,
            "mece_basket_long_yes": 0,
            "mece_basket_long_no": 0,
            "crossed_book": 0,
            "other": 0,
            "total_unique": 0,
        }
        qual_by_strat: Dict[str, Any] = {
            "binary_parity": {"qualified": 0, "rejected": 0, "outcomes": {}},
            "mece_basket_long_yes": {"qualified": 0, "rejected": 0, "outcomes": {}},
            "mece_basket_long_no": {"qualified": 0, "rejected": 0, "outcomes": {}},
            "crossed_book": {"qualified": 0, "rejected": 0, "outcomes": {}},
            "other": {"qualified": 0, "rejected": 0, "outcomes": {}},
        }

        has_cand_file = cand_path.exists()
        has_qual_file = qual_path.exists()

        if has_cand_file:
            seen_cand_ids: Set[str] = set()
            try:
                with open(cand_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            cand = record.get("candidate", record)
                            cid = cand.get("candidate_id") or cand.get("stable_key")
                            if not cid or cid in seen_cand_ids:
                                continue
                            seen_cand_ids.add(cid)

                            strat = normalize_breadth_strategy_type(cand.get("strategy_type", ""))
                            is_crossed = bool(cand.get("is_crossed_book_signal")) or (strat == "crossed_book")
                            if is_crossed:
                                prelim_counts["crossed_book"] += 1
                            elif strat in prelim_counts:
                                prelim_counts[strat] += 1
                            else:
                                prelim_counts["other"] += 1
                            prelim_counts["total_unique"] += 1
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning(f"Error reading candidate history for strategy breakdown: {exc}")

        if has_qual_file:
            seen_qual_ids: Set[str] = set()
            try:
                with open(qual_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                            qual = record.get("qualification", record)
                            qid = qual.get("qualification_id") or qual.get("candidate_id") or qual.get("stable_key")
                            if not qid or qid in seen_qual_ids:
                                continue
                            seen_qual_ids.add(qid)

                            strat = normalize_breadth_strategy_type(qual.get("strategy_type", ""))
                            target_strat = strat if strat in qual_by_strat else "other"
                            is_qual = bool(qual.get("is_qualified", False))
                            outcome = str(qual.get("outcome", "UNKNOWN"))

                            if is_qual or outcome == "QUALIFIED":
                                qual_by_strat[target_strat]["qualified"] += 1
                            else:
                                qual_by_strat[target_strat]["rejected"] += 1

                            outcomes_dict = qual_by_strat[target_strat]["outcomes"]
                            outcomes_dict[outcome] = outcomes_dict.get(outcome, 0) + 1
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning(f"Error reading qualification history for strategy breakdown: {exc}")

        # Also provide convenient UI-friendly aliases
        prelim_counts["mece_long_yes"] = prelim_counts["mece_basket_long_yes"]
        prelim_counts["mece_long_no"] = prelim_counts["mece_basket_long_no"]

        return {
            "has_candidate_records": has_cand_file,
            "has_qualification_records": has_qual_file,
            "preliminary_by_strategy": prelim_counts,
            "qualification_by_strategy": qual_by_strat,
        }

    def get_breadth_scanner_summaries(
        self,
        scanner_dir: Optional[Union[str, Path]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return recent scan cycle summaries from scan_summaries.jsonl in reverse chronological order."""
        base_dir = self._resolve_breadth_scanner_dir(scanner_dir)
        path = base_dir / "scan_summaries.jsonl"
        if not path.exists():
            return []

        summaries = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            summaries.append(record)
                        except Exception:
                            pass
            summaries.reverse()
            return summaries[:limit]
        except Exception as exc:
            logger.warning(f"Error reading scan summaries from {path}: {exc}")
            return []

    def get_breadth_scanner_candidates(
        self,
        scanner_dir: Optional[Union[str, Path]] = None,
        limit: int = 100,
        strategy: Optional[str] = None,
        cycle_number: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return recorded preliminary candidates from candidate_history.jsonl in reverse order."""
        base_dir = self._resolve_breadth_scanner_dir(scanner_dir)
        path = base_dir / "candidate_history.jsonl"
        if not path.exists():
            return []

        candidates = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            cand_data = record.get("candidate", record)
                            rec_cycle = record.get("cycle_number")
                            rec_time = record.get("recorded_at")

                            cand_dict = dict(cand_data)
                            if "cycle_number" not in cand_dict and rec_cycle is not None:
                                cand_dict["cycle_number"] = rec_cycle
                            if "recorded_at" not in cand_dict and rec_time is not None:
                                cand_dict["recorded_at"] = rec_time

                            strat_type = cand_dict.get("strategy_type", "")
                            cand_dict["is_crossed_book_signal"] = (strat_type == "crossed_book")

                            if strategy and strategy.upper() != "ALL":
                                if normalize_breadth_strategy_type(strat_type) != normalize_breadth_strategy_type(strategy):
                                    continue

                            if cycle_number is not None and cand_dict.get("cycle_number") != cycle_number:
                                continue

                            candidates.append(cand_dict)
                        except Exception:
                            pass
            candidates.reverse()
            return candidates[:limit]
        except Exception as exc:
            logger.warning(f"Error reading candidate history from {path}: {exc}")
            return []

    def get_breadth_scanner_qualifications(
        self,
        scanner_dir: Optional[Union[str, Path]] = None,
        limit: int = 100,
        strategy: Optional[str] = None,
        outcome: Optional[str] = None,
        is_qualified: Optional[bool] = None,
        cycle_number: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return recorded targeted order-book qualifications from qualification_history.jsonl in reverse order."""
        base_dir = self._resolve_breadth_scanner_dir(scanner_dir)
        path = base_dir / "qualification_history.jsonl"
        if not path.exists():
            return []

        qualifications = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            record = json.loads(line)
                            qual_data = record.get("qualification", record)
                            rec_cycle = record.get("cycle_number")
                            rec_time = record.get("recorded_at")

                            qual_dict = dict(qual_data)
                            if "cycle_number" not in qual_dict and rec_cycle is not None:
                                qual_dict["cycle_number"] = rec_cycle
                            if "recorded_at" not in qual_dict and rec_time is not None:
                                qual_dict["recorded_at"] = rec_time

                            strat = qual_dict.get("strategy_type", "")
                            if strategy and strategy.upper() != "ALL":
                                if normalize_breadth_strategy_type(strat) != normalize_breadth_strategy_type(strategy):
                                    continue

                            outc = qual_dict.get("outcome", "")
                            if outcome and outcome.upper() != "ALL":
                                if outc.upper() != outcome.upper():
                                    continue

                            if is_qualified is not None:
                                if bool(qual_dict.get("is_qualified")) != is_qualified:
                                    continue

                            if cycle_number is not None and qual_dict.get("cycle_number") != cycle_number:
                                continue

                            qualifications.append(qual_dict)
                        except Exception:
                            pass
            qualifications.reverse()
            return qualifications[:limit]
        except Exception as exc:
            logger.warning(f"Error reading qualification history from {path}: {exc}")
            return []
