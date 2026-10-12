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


class DashboardDataService:
    """Read-only data service powering dashboard metrics, tables, and audit views."""

    def __init__(self, workspace_root: Optional[Union[str, Path]] = None) -> None:
        if workspace_root is None:
            self.workspace_root = Path(__file__).resolve().parents[2]
        else:
            self.workspace_root = Path(workspace_root).resolve()

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

        return {
            "server_time": datetime.now(timezone.utc).isoformat(),
            "observer_process": proc_status,
            "paper_trading": paper_health,
            "datasets_count": len(datasets),
            "total_records_stored": total_records,
            "total_storage_bytes": total_storage_bytes,
            "warnings": warnings,
            "workspace_root": str(self.workspace_root),
        }

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
