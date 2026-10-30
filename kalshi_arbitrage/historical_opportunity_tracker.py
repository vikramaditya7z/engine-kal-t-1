"""Historical Opportunity Tracker and Offline Replay Analytics Engine.

Provides deep historical analytics on persisted scanner datasets (Trial 1, Trial 2, smoke tests):
1. Opportunity Frequency and Rate Metrics.
2. Opportunity Lifetime and Temporal Persistence.
3. Multi-Stage Qualification Funnel Analysis.
4. False-Positive Categorization and Rejection Forensics.
5. Strategy-Specific Performance & Fee Impact Analysis.
6. Market Category Concentration Analysis.
7. Depth, Slippage, and Liquidity Constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger("historical_opportunity_tracker")


@dataclass
class OpportunityTemporalRecord:
    """Tracks a single candidate's lifecycle across chronological scan cycles."""

    stable_key: str
    strategy_type: str
    event_ticker: str
    market_tickers: Tuple[str, ...]
    gross_edge_dollars: Decimal
    first_seen_cycle: int
    first_seen_timestamp: str
    last_seen_cycle: int
    last_seen_timestamp: str
    cycles_observed: int = 1
    duration_seconds: float = 0.0
    is_qualified: bool = False
    qualification_outcomes: List[str] = field(default_factory=list)
    rejection_reasons: List[str] = field(default_factory=list)
    net_edge_cents: Optional[int] = None
    total_fees_cents: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stable_key": self.stable_key,
            "strategy_type": self.strategy_type,
            "event_ticker": self.event_ticker,
            "market_tickers": list(self.market_tickers),
            "gross_edge_dollars": str(self.gross_edge_dollars),
            "first_seen_cycle": self.first_seen_cycle,
            "first_seen_timestamp": self.first_seen_timestamp,
            "last_seen_cycle": self.last_seen_cycle,
            "last_seen_timestamp": self.last_seen_timestamp,
            "cycles_observed": self.cycles_observed,
            "duration_seconds": round(self.duration_seconds, 2),
            "is_qualified": self.is_qualified,
            "qualification_outcomes": self.qualification_outcomes,
            "rejection_reasons": self.rejection_reasons,
            "net_edge_cents": self.net_edge_cents,
            "total_fees_cents": self.total_fees_cents,
        }


@dataclass
class HistoricalAnalysisReport:
    """Comprehensive structured analytics report for a scanner observation dataset."""

    dataset_path: str
    session_id: Optional[str]
    total_cycles: int
    total_markets_screened: int
    total_events_screened: int
    total_strategies_evaluated: int
    total_preliminary_candidates: int
    unique_candidates_count: int
    total_qualified_candidates: int
    overall_qualification_rate_pct: float
    rejection_breakdown: Dict[str, int]
    strategy_breakdown: Dict[str, Dict[str, Any]]
    category_concentration: Dict[str, int]
    temporal_opportunities: List[OpportunityTemporalRecord]
    average_opportunity_duration_seconds: float
    max_opportunity_duration_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_path": self.dataset_path,
            "session_id": self.session_id,
            "total_cycles": self.total_cycles,
            "total_markets_screened": self.total_markets_screened,
            "total_events_screened": self.total_events_screened,
            "total_strategies_evaluated": self.total_strategies_evaluated,
            "total_preliminary_candidates": self.total_preliminary_candidates,
            "unique_candidates_count": self.unique_candidates_count,
            "total_qualified_candidates": self.total_qualified_candidates,
            "overall_qualification_rate_pct": round(self.overall_qualification_rate_pct, 4),
            "rejection_breakdown": dict(self.rejection_breakdown),
            "strategy_breakdown": self.strategy_breakdown,
            "category_concentration": dict(self.category_concentration),
            "average_opportunity_duration_seconds": round(self.average_opportunity_duration_seconds, 2),
            "max_opportunity_duration_seconds": round(self.max_opportunity_duration_seconds, 2),
            "opportunities_count": len(self.temporal_opportunities),
            "temporal_opportunities": [opp.to_dict() for opp in self.temporal_opportunities],
        }


class HistoricalOpportunityTracker:
    """Analyzes and reconstructs arbitrage opportunity lifecycles from scanner output directories."""

    def __init__(self, scanner_dir: Path | str) -> None:
        self.scanner_dir = Path(scanner_dir)
        self.state_file = self.scanner_dir / "scanner_state.json"
        self.summaries_file = self.scanner_dir / "scan_summaries.jsonl"
        self.candidate_file = self.scanner_dir / "candidate_history.jsonl"
        self.qualification_file = self.scanner_dir / "qualification_history.jsonl"

    def analyze(self) -> HistoricalAnalysisReport:
        """Parse persisted scanner data and generate comprehensive historical research report."""
        session_id: Optional[str] = None
        total_markets = 0
        total_events = 0
        total_strats = 0
        total_cycles = 0

        # 1. Parse scanner state if available
        if self.state_file.exists():
            try:
                state_data = json.loads(self.state_file.read_text(encoding="utf-8"))
                session_id = state_data.get("session_id")
                total_cycles = state_data.get("total_cycles_count", 0)
                total_markets = state_data.get("total_unique_markets_screened", 0)
                total_events = state_data.get("total_events_evaluated", 0)
                total_strats = state_data.get("total_strategies_evaluated", 0)
            except Exception as exc:
                logger.warning("Error reading state file %s: %s", self.state_file, exc)

        # 2. Parse scan cycle summaries
        summaries = []
        if self.summaries_file.exists():
            try:
                with open(self.summaries_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            summaries.append(json.loads(line))
                if summaries:
                    total_cycles = max(total_cycles, len(summaries))
                    if total_markets == 0:
                        total_markets = max(s.get("markets_screened", 0) for s in summaries)
                    if total_events == 0:
                        total_events = max(s.get("events_screened", 0) for s in summaries)
                    if total_strats == 0:
                        total_strats = sum(s.get("strategies_evaluated", 0) for s in summaries)
            except Exception as exc:
                logger.warning("Error reading summaries file %s: %s", self.summaries_file, exc)

        # 3. Track temporal candidate appearances
        opportunities_by_key: Dict[str, OpportunityTemporalRecord] = {}
        total_preliminary = 0

        if self.candidate_file.exists():
            try:
                with open(self.candidate_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        record = json.loads(line)
                        total_preliminary += 1
                        cand = record.get("candidate", record)
                        key = cand.get("stable_key", cand.get("candidate_id", ""))
                        cycle_num = record.get("cycle_number", 1)
                        ts_str = record.get("recorded_at", cand.get("observation_timestamp", ""))
                        strat = cand.get("strategy_type", "unknown")
                        event_tkr = cand.get("event_ticker", "")
                        mkt_tkrs = tuple(cand.get("market_tickers", ()))
                        gross_edge = Decimal(str(cand.get("gross_edge_dollars", "0.0")))

                        if key not in opportunities_by_key:
                            opportunities_by_key[key] = OpportunityTemporalRecord(
                                stable_key=key,
                                strategy_type=strat,
                                event_ticker=event_tkr,
                                market_tickers=mkt_tkrs,
                                gross_edge_dollars=gross_edge,
                                first_seen_cycle=cycle_num,
                                first_seen_timestamp=ts_str,
                                last_seen_cycle=cycle_num,
                                last_seen_timestamp=ts_str,
                                cycles_observed=1,
                            )
                        else:
                            opp = opportunities_by_key[key]
                            opp.last_seen_cycle = cycle_num
                            opp.last_seen_timestamp = ts_str
                            opp.cycles_observed += 1
            except Exception as exc:
                logger.warning("Error reading candidate file %s: %s", self.candidate_file, exc)

        # 4. Integrate targeted qualification results
        rejection_counts: Dict[str, int] = {}
        total_qualified = 0

        if self.qualification_file.exists():
            try:
                with open(self.qualification_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        record = json.loads(line)
                        qual = record.get("qualification", record)
                        key = qual.get("stable_key", qual.get("candidate_id", ""))
                        outc = qual.get("outcome", "UNKNOWN")
                        is_qual = bool(qual.get("is_qualified", False))
                        reason = qual.get("rejection_reason")

                        rejection_counts[outc] = rejection_counts.get(outc, 0) + 1
                        if is_qual:
                            total_qualified += 1

                        if key in opportunities_by_key:
                            opp = opportunities_by_key[key]
                            opp.qualification_outcomes.append(outc)
                            if is_qual:
                                opp.is_qualified = True
                                opp.net_edge_cents = qual.get("net_edge_cents")
                                opp.total_fees_cents = qual.get("total_fees_cents")
                            if reason and reason not in opp.rejection_reasons:
                                opp.rejection_reasons.append(reason)
            except Exception as exc:
                logger.warning("Error reading qualification file %s: %s", self.qualification_file, exc)

        # 5. Compute temporal duration per opportunity
        durations: List[float] = []
        for opp in opportunities_by_key.values():
            try:
                t1 = datetime.fromisoformat(opp.first_seen_timestamp)
                t2 = datetime.fromisoformat(opp.last_seen_timestamp)
                diff = max(0.0, (t2 - t1).total_seconds())
                opp.duration_seconds = diff
                durations.append(diff)
            except Exception:
                opp.duration_seconds = 0.0
                durations.append(0.0)

        avg_dur = (sum(durations) / len(durations)) if durations else 0.0
        max_dur = max(durations) if durations else 0.0

        # 6. Breakdown by strategy
        strategy_stats: Dict[str, Dict[str, Any]] = {}
        for opp in opportunities_by_key.values():
            s = opp.strategy_type
            if s not in strategy_stats:
                strategy_stats[s] = {
                    "count": 0,
                    "qualified": 0,
                    "total_gross_edge": Decimal("0"),
                    "max_gross_edge": Decimal("0"),
                }
            strategy_stats[s]["count"] += 1
            if opp.is_qualified:
                strategy_stats[s]["qualified"] += 1
            strategy_stats[s]["total_gross_edge"] += opp.gross_edge_dollars
            strategy_stats[s]["max_gross_edge"] = max(strategy_stats[s]["max_gross_edge"], opp.gross_edge_dollars)

        serialized_strat_stats: Dict[str, Dict[str, Any]] = {
            k: {
                "count": v["count"],
                "qualified": v["qualified"],
                "total_gross_edge": str(v["total_gross_edge"]),
                "max_gross_edge": str(v["max_gross_edge"]),
            }
            for k, v in strategy_stats.items()
        }

        # 7. Category / Series concentration
        category_counts: Dict[str, int] = {}
        for opp in opportunities_by_key.values():
            series = opp.event_ticker.split("-")[0] if "-" in opp.event_ticker else opp.event_ticker
            category_counts[series] = category_counts.get(series, 0) + 1

        qual_rate = (
            (total_qualified / len(opportunities_by_key) * 100.0)
            if opportunities_by_key
            else 0.0
        )

        return HistoricalAnalysisReport(
            dataset_path=str(self.scanner_dir),
            session_id=session_id,
            total_cycles=total_cycles,
            total_markets_screened=total_markets,
            total_events_screened=total_events,
            total_strategies_evaluated=total_strats,
            total_preliminary_candidates=total_preliminary,
            unique_candidates_count=len(opportunities_by_key),
            total_qualified_candidates=total_qualified,
            overall_qualification_rate_pct=qual_rate,
            rejection_breakdown=rejection_counts,
            strategy_breakdown=serialized_strat_stats,
            category_concentration=category_counts,
            temporal_opportunities=list(opportunities_by_key.values()),
            average_opportunity_duration_seconds=avg_dur,
            max_opportunity_duration_seconds=max_dur,
        )
