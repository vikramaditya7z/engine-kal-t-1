"""Monitoring, performance, and data-quality metrics for the V5 Market Observer.

Computes reproducible summary statistics across observed markets, candidate detections,
order-book depth evaluations, rejection stages, opportunity lifetimes, and simulated
paper trading without misleading financial claims.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from .arbitrage import cents_to_dollars
from .observer import (
    EvaluatedOpportunity,
    MarketObservation,
    STATUS_QUALIFIED,
)
from .paper_trade import PaperTrade, TradeState


@dataclass(frozen=True)
class ObserverMetrics:
    """Comprehensive structured metrics for an observation session."""

    markets_observed_total: int
    markets_refreshed_success: int
    markets_refreshed_failed: int
    observations_stale: int
    candidates_detected_total: int
    unique_opportunities_count: int
    repeated_observations_count: int
    qualified_opportunities_count: int
    rejections_by_reason: Dict[str, int]
    supported_quantities_min: int
    supported_quantities_max: int
    supported_quantities_mean: Decimal
    gross_edge_cents_min: int
    gross_edge_cents_max: int
    gross_edge_cents_mean: Decimal
    net_edge_cents_min: Optional[int]
    net_edge_cents_max: Optional[int]
    net_edge_cents_mean: Optional[Decimal]
    opportunity_lifetimes_min_seconds: Optional[float]
    opportunity_lifetimes_max_seconds: Optional[float]
    opportunity_lifetimes_mean_seconds: Optional[float]
    paper_trades_submitted: int
    paper_trades_accepted: int
    paper_trades_rejected: int
    paper_trades_filled: int
    paper_trades_partially_filled: int
    paper_fees_total_cents: int
    api_failure_rate: Decimal


@dataclass(frozen=True)
class ObserverReport:
    """Human-readable evaluation report and summary metrics."""

    metrics: ObserverMetrics
    observations_count: int
    evaluations_count: int
    paper_portfolio_equity_cents: Optional[int] = None
    paper_portfolio_cash_cents: Optional[int] = None

    def summary(self) -> str:
        """Format an auditable, human-readable text summary of the observation metrics."""
        m = self.metrics
        lines = [
            "=" * 64,
            "KALSHI ARBITRAGE ENGINE — V5 OBSERVER EVALUATION REPORT",
            "=" * 64,
            "MARKET OBSERVATIONS & DATA QUALITY:",
            f"  Total Market Observations:         {m.markets_observed_total}",
            f"  Successfully Refreshed:            {m.markets_refreshed_success}",
            f"  Failed Refreshes:                  {m.markets_refreshed_failed}",
            f"  Stale / Invalid Observations:      {m.observations_stale}",
            f"  API Failure Rate:                  {m.api_failure_rate:.1%}",
            "",
            "ARBITRAGE OPPORTUNITY DETECTION (V2 / V3):",
            f"  Total Candidates Evaluated:        {m.candidates_detected_total}",
            f"  Unique Opportunity Patterns:       {m.unique_opportunities_count}",
            f"  Repeated Observations (Recurring): {m.repeated_observations_count}",
            f"  Qualified (Executable Depth & Edge):{m.qualified_opportunities_count}",
            "",
            "REJECTIONS BY REASON:",
        ]

        if m.rejections_by_reason:
            for reason, count in sorted(m.rejections_by_reason.items(), key=lambda x: -x[1]):
                lines.append(f"  - {reason}: {count}")
        else:
            lines.append("  (None)")

        lines.extend([
            "",
            "LIQUIDITY & EDGE DISTRIBUTIONS:",
            f"  Depth-Supported Quantity (Min/Mean/Max): {m.supported_quantities_min} / {m.supported_quantities_mean:.1f} / {m.supported_quantities_max}",
            f"  Gross Edge (Min/Mean/Max):             {m.gross_edge_cents_min}¢ / {m.gross_edge_cents_mean:.1f}¢ / {m.gross_edge_cents_max}¢",
        ])

        if m.net_edge_cents_mean is not None:
            lines.append(
                f"  Net Edge (Min/Mean/Max):               {m.net_edge_cents_min}¢ / {m.net_edge_cents_mean:.1f}¢ / {m.net_edge_cents_max}¢"
            )
        else:
            lines.append("  Net Edge:                              (Exchange fees not configured)")

        lines.extend([
            "",
            "OPPORTUNITY LIFETIMES (OBSERVED POLL INTERVALS):",
        ])
        if m.opportunity_lifetimes_mean_seconds is not None:
            lines.append(
                f"  Lifetime (Min/Mean/Max seconds):       {m.opportunity_lifetimes_min_seconds:.1f}s / {m.opportunity_lifetimes_mean_seconds:.1f}s / {m.opportunity_lifetimes_max_seconds:.1f}s"
            )
        else:
            lines.append("  Lifetime:                              (No closed appearance episodes observed)")

        if m.paper_trades_submitted > 0:
            lines.extend([
                "",
                "V4 PAPER TRADING SIMULATION:",
                f"  Trades Submitted:                  {m.paper_trades_submitted}",
                f"  Trades Accepted:                   {m.paper_trades_accepted}",
                f"  Trades Rejected (Risk/Validation): {m.paper_trades_rejected}",
                f"  Trades Fully Filled:               {m.paper_trades_filled}",
                f"  Trades Partially Filled:           {m.paper_trades_partially_filled}",
                f"  Total Estimated Fees Paid:         {m.paper_fees_total_cents}¢ (${cents_to_dollars(m.paper_fees_total_cents)})",
            ])
            if self.paper_portfolio_equity_cents is not None:
                lines.append(
                    f"  Ending Portfolio Book Equity:      {self.paper_portfolio_equity_cents}¢ (${cents_to_dollars(self.paper_portfolio_equity_cents)})"
                )
            if self.paper_portfolio_cash_cents is not None:
                lines.append(
                    f"  Ending Available Cash:             {self.paper_portfolio_cash_cents}¢ (${cents_to_dollars(self.paper_portfolio_cash_cents)})"
                )

        lines.extend([
            "=" * 64,
            "IMPORTANT DEFINITIONS & DISCLAIMERS:",
            "- Data Freshness: Evaluated against the exchange HTTP Date response header.",
            "  Limitation: HTTP Date establishes response time, not necessarily the exact",
            "  matching-engine snapshot generation time or proof that an upstream cache",
            "  was bypassed. Observations without exchange source timestamps or exceeding",
            "  max_stale_seconds are classified as stale and rejected.",
            "- Opportunity Lifetime: Measures the interval between observed first appearance",
            "  and subsequent disappearance across discrete polling cycles. It does NOT",
            "  prove continuous order-book availability between polling intervals.",
            "- Execution Pricing: Evaluated against static order-book depth snapshots.",
            "  Simulated fills do NOT represent live exchange execution or guaranteed profits.",
            "=" * 64,
        ])

        return "\n".join(lines)


def compute_observer_metrics(
    observations: Sequence[MarketObservation],
    evaluations: Sequence[EvaluatedOpportunity],
    *,
    paper_trades: Sequence[PaperTrade] = (),
    lifetimes: Sequence[float] = (),
) -> ObserverMetrics:
    """Compute structured metrics from session observations and evaluations."""
    total_obs = len(observations)
    refreshed_success = sum(1 for o in observations if o.is_success)
    refreshed_failed = sum(1 for o in observations if not o.is_success)
    obs_stale = sum(1 for o in observations if o.is_stale)

    api_failure_rate = (
        Decimal(refreshed_failed) / Decimal(total_obs) if total_obs > 0 else Decimal("0")
    )

    total_candidates = len(evaluations)
    unique_opp_ids = {e.opportunity_id for e in evaluations}
    repeated_count = sum(1 for e in evaluations if e.is_recurrent)
    qualified_count = sum(1 for e in evaluations if e.is_qualified)

    rejections: Dict[str, int] = {}
    quantities: List[int] = []
    gross_edges: List[int] = []
    net_edges: List[int] = []

    for e in evaluations:
        if not e.is_qualified:
            reason = e.rejection_reason or e.status
            rejections[reason] = rejections.get(reason, 0) + 1

        if e.pricing_result is not None:
            quantities.append(e.pricing_result.supported_quantity)
            gross_edges.append(e.pricing_result.gross_profit_cents)
            if e.pricing_result.net_profit_cents is not None:
                net_edges.append(e.pricing_result.net_profit_cents)

    qty_min = min(quantities) if quantities else 0
    qty_max = max(quantities) if quantities else 0
    qty_mean = Decimal(sum(quantities)) / Decimal(len(quantities)) if quantities else Decimal("0")

    gross_min = min(gross_edges) if gross_edges else 0
    gross_max = max(gross_edges) if gross_edges else 0
    gross_mean = (
        Decimal(sum(gross_edges)) / Decimal(len(gross_edges)) if gross_edges else Decimal("0")
    )

    net_min = min(net_edges) if net_edges else None
    net_max = max(net_edges) if net_edges else None
    net_mean = Decimal(sum(net_edges)) / Decimal(len(net_edges)) if net_edges else None

    # Lifetime metrics
    clean_lifetimes = [lt for lt in lifetimes if lt >= 0]
    lt_min = min(clean_lifetimes) if clean_lifetimes else None
    lt_max = max(clean_lifetimes) if clean_lifetimes else None
    lt_mean = sum(clean_lifetimes) / len(clean_lifetimes) if clean_lifetimes else None

    # Paper trading metrics
    trades_submitted = len(paper_trades)
    trades_accepted = sum(1 for t in paper_trades if t.state != TradeState.REJECTED)
    trades_rejected = sum(1 for t in paper_trades if t.state == TradeState.REJECTED)
    trades_filled = sum(1 for t in paper_trades if t.state == TradeState.FILLED)
    trades_partial = sum(1 for t in paper_trades if t.state == TradeState.PARTIALLY_FILLED)
    total_fees = sum(t.estimated_fees_cents for t in paper_trades if t.state != TradeState.REJECTED)

    return ObserverMetrics(
        markets_observed_total=total_obs,
        markets_refreshed_success=refreshed_success,
        markets_refreshed_failed=refreshed_failed,
        observations_stale=obs_stale,
        candidates_detected_total=total_candidates,
        unique_opportunities_count=len(unique_opp_ids),
        repeated_observations_count=repeated_count,
        qualified_opportunities_count=qualified_count,
        rejections_by_reason=rejections,
        supported_quantities_min=qty_min,
        supported_quantities_max=qty_max,
        supported_quantities_mean=qty_mean,
        gross_edge_cents_min=gross_min,
        gross_edge_cents_max=gross_max,
        gross_edge_cents_mean=gross_mean,
        net_edge_cents_min=net_min,
        net_edge_cents_max=net_max,
        net_edge_cents_mean=net_mean,
        opportunity_lifetimes_min_seconds=lt_min,
        opportunity_lifetimes_max_seconds=lt_max,
        opportunity_lifetimes_mean_seconds=lt_mean,
        paper_trades_submitted=trades_submitted,
        paper_trades_accepted=trades_accepted,
        paper_trades_rejected=trades_rejected,
        paper_trades_filled=trades_filled,
        paper_trades_partially_filled=trades_partial,
        paper_fees_total_cents=total_fees,
        api_failure_rate=api_failure_rate,
    )
