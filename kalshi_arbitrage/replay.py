"""Deterministic historical replay engine and performance evaluation.

V4 Replay feeds chronological market snapshots through the V2/V3/V4 pipeline,
evaluates risk and paper execution, and tracks portfolio equity and drawdown
without look-ahead bias or live network interaction.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .arbitrage import (
    OPPORTUNITY_BINARY_PARITY,
    OPPORTUNITY_MECE_BASKET_LONG_NO,
    OPPORTUNITY_MECE_BASKET_LONG_YES,
    OPPORTUNITY_PORTFOLIO,
    cents_to_dollars,
)
from .contract import NO, YES
from .execution import (
    STATUS_INSUFFICIENT_DEPTH,
    STATUS_INVALID_INPUT,
    STATUS_NO_GROSS_EDGE,
    evaluate_binary_parity_execution,
    evaluate_mece_basket_execution,
    evaluate_portfolio_execution,
)
from .ledger import PaperPortfolio
from .market_data import NormalizedOrderBook
from .paper_executor import PaperExecutionEngine
from .paper_trade import PaperTrade, TradeState
from .portfolio import Event, Portfolio
from .risk import RiskConfig, RiskManager


class ReplayError(ValueError):
    """Raised when replay sequence or parameters are invalid."""


EVENT_TYPE_OPPORTUNITY = "OPPORTUNITY"
EVENT_TYPE_SETTLEMENT = "SETTLEMENT"


@dataclass(frozen=True)
class ReplayEvent:
    """A point-in-time market event or settlement action in the replay sequence."""

    timestamp: str
    event_type: str  # EVENT_TYPE_OPPORTUNITY or EVENT_TYPE_SETTLEMENT
    strategy_type: Optional[str] = None
    book: Optional[NormalizedOrderBook] = None
    event: Optional[Event] = None
    books_by_outcome: Optional[Mapping[str, NormalizedOrderBook]] = None
    portfolio: Optional[Portfolio] = None
    books_by_contract: Optional[Mapping[str, NormalizedOrderBook]] = None
    requested_quantity: int = 1
    fee_per_contract_cents: Optional[int] = None
    settle_market_ticker: Optional[str] = None
    settle_winning_side: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, str) or not self.timestamp:
            raise ReplayError("timestamp must be a non-empty string")
        if self.event_type not in (EVENT_TYPE_OPPORTUNITY, EVENT_TYPE_SETTLEMENT):
            raise ReplayError(f"unrecognized event_type: {self.event_type!r}")


@dataclass(frozen=True)
class ReplayMetrics:
    """Summary of simulated trading performance across a replay session."""

    initial_cash_cents: int
    final_cash_cents: int
    final_equity_cents: int
    total_cost_cents: int
    total_fees_cents: int
    realized_pnl_cents: int
    unrealized_pnl_cents: int
    net_profit_cents: int
    max_drawdown_cents: int
    max_drawdown_pct: Decimal
    opportunities_evaluated: int
    opportunities_rejected_risk: int
    opportunities_rejected_depth: int
    trades_accepted: int
    trades_filled: int
    trades_partially_filled: int
    rejection_reasons: Tuple[Tuple[str, int], ...]


@dataclass(frozen=True)
class ReplayReport:
    """Comprehensive output of a historical replay session."""

    metrics: ReplayMetrics
    equity_curve: Tuple[Tuple[str, int], ...]  # (timestamp, equity_cents)
    trades: Tuple[PaperTrade, ...]
    portfolio: PaperPortfolio

    def summary(self) -> str:
        """Format metrics into a clear, audit-ready text report."""
        m = self.metrics
        lines = [
            "=" * 60,
            "KALSHI ARBITRAGE ENGINE — HISTORICAL REPLAY REPORT",
            "=" * 60,
            f"Initial Cash:             ${cents_to_dollars(m.initial_cash_cents):.2f}",
            f"Final Cash:               ${cents_to_dollars(m.final_cash_cents):.2f}",
            f"Final Equity:             ${cents_to_dollars(m.final_equity_cents):.2f}",
            f"Equity Model:             Book Value (Cash + Position Cost Basis)",
            f"Net Simulated Profit:     ${cents_to_dollars(m.net_profit_cents):.2f}",
            f"Realized P&L:             ${cents_to_dollars(m.realized_pnl_cents):.2f}",
            f"Unrealized P&L:           ${cents_to_dollars(m.unrealized_pnl_cents):.2f}",
            f"Total Fees Paid:          ${cents_to_dollars(m.total_fees_cents):.2f}",
            f"Max Drawdown:             ${cents_to_dollars(m.max_drawdown_cents):.2f} ({m.max_drawdown_pct:.2f}%)",
            "-" * 60,
            f"Opportunities Evaluated:  {m.opportunities_evaluated}",
            f"Trades Accepted:          {m.trades_accepted}",
            f"Trades Filled:            {m.trades_filled}",
            f"Trades Partially Filled:  {m.trades_partially_filled}",
            f"Rejected by Risk Limits:  {m.opportunities_rejected_risk}",
            f"Rejected by Book Depth:   {m.opportunities_rejected_depth}",
            "-" * 60,
            "Top Rejection Reasons:",
        ]
        if m.rejection_reasons:
            for reason, count in m.rejection_reasons:
                lines.append(f"  - {reason}: {count}")
        else:
            lines.append("  (None)")
        lines.append("=" * 60)
        lines.append(
            "ACCOUNTING POLICY: Replay equity curve is computed using book-value equity"
        )
        lines.append(
            "(total cash + position cost basis). Unrealized arbitrage edge is recognized"
        )
        lines.append(
            "only upon market settlement. Temporary drawdown reflects transaction fees paid."
        )
        lines.append(
            "DISCLAIMER: Replay results reflect simulated paper execution using recorded"
        )
        lines.append(
            "order-book depth. Historical simulation does not guarantee live execution."
        )
        lines.append("=" * 60)
        return "\n".join(lines)


class ReplayEngine:
    """Orchestrates deterministic chronological replay of historical market snapshots."""

    def __init__(
        self,
        risk_config: Optional[RiskConfig] = None,
        default_fee_per_contract_cents: Optional[int] = None,
    ):
        self.risk_config = risk_config or RiskConfig()
        self.default_fee = default_fee_per_contract_cents
        self.executor = PaperExecutionEngine(
            default_fee_per_contract_cents=self.default_fee
        )

    def run(
        self,
        events: Sequence[ReplayEvent],
        initial_cash_cents: int = 100_000,
    ) -> ReplayReport:
        """Run the replay session across the chronologically ordered events.

        Equity is evaluated deterministically at each event step using book-value equity
        (total cash + position acquisition cost basis). Drawdown calculations track
        maximum peak-to-trough decline in this book-value equity series.
        """
        # Sort chronologically to eliminate look-ahead bias
        sorted_events = sorted(events, key=lambda ev: ev.timestamp)

        portfolio = PaperPortfolio(initial_cash_cents=initial_cash_cents)
        risk_manager = RiskManager(self.risk_config)

        equity_curve: List[Tuple[str, int]] = []
        trades: List[PaperTrade] = []
        rejection_counts: Dict[str, int] = {}

        opps_eval = 0
        opps_rej_risk = 0
        opps_rej_depth = 0
        trades_accepted = 0
        trades_filled = 0
        trades_partial = 0
        total_cost_cents = 0

        peak_equity_cents = initial_cash_cents
        max_drawdown_cents = 0

        initial_equity = portfolio.portfolio_equity_cents()
        equity_curve.append(("START", initial_equity))

        for ev in sorted_events:
            ts = ev.timestamp

            if ev.event_type == EVENT_TYPE_SETTLEMENT:
                if ev.settle_market_ticker and ev.settle_winning_side:
                    portfolio.settle_binary_market(
                        ev.settle_market_ticker,
                        ev.settle_winning_side,
                        timestamp=ts,
                    )
            elif ev.event_type == EVENT_TYPE_OPPORTUNITY:
                opps_eval += 1
                trade = self._evaluate_and_simulate(ev, ts, portfolio, risk_manager)

                if trade.state == TradeState.REJECTED:
                    reason = trade.rejection_reason or "Unknown rejection"
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    if "Risk limit exceeded" in reason:
                        opps_rej_risk += 1
                    else:
                        opps_rej_depth += 1
                    trades.append(trade)
                else:
                    trades_accepted += 1
                    if trade.state == TradeState.FILLED:
                        trades_filled += 1
                    elif trade.state == TradeState.PARTIALLY_FILLED:
                        trades_partial += 1

                    portfolio.apply_trade_fill(trade, timestamp=ts)
                    risk_manager.record_processed_trade(trade.trade_id)
                    total_cost_cents += trade.total_cost_cents
                    trades.append(trade)

            # Record equity point after event
            current_equity = portfolio.portfolio_equity_cents()
            equity_curve.append((ts, current_equity))

            if current_equity > peak_equity_cents:
                peak_equity_cents = current_equity
            drawdown = peak_equity_cents - current_equity
            if drawdown > max_drawdown_cents:
                max_drawdown_cents = drawdown

        # Reconcile portfolio at end of replay
        rec = portfolio.reconcile()
        if not rec.is_reconciled:
            raise ReplayError(f"Replay ended in unreconciled portfolio state: {rec.discrepancies}")

        final_equity = portfolio.portfolio_equity_cents()
        net_profit = final_equity - initial_cash_cents
        max_dd_pct = (
            (Decimal(max_drawdown_cents) / Decimal(peak_equity_cents)) * Decimal(100)
            if peak_equity_cents > 0
            else Decimal(0)
        )

        sorted_rejections = tuple(
            sorted(rejection_counts.items(), key=lambda kv: kv[1], reverse=True)
        )

        metrics = ReplayMetrics(
            initial_cash_cents=initial_cash_cents,
            final_cash_cents=portfolio.available_cash_cents,
            final_equity_cents=final_equity,
            total_cost_cents=total_cost_cents,
            total_fees_cents=portfolio.total_fees_paid_cents,
            realized_pnl_cents=portfolio.realized_pnl_cents,
            unrealized_pnl_cents=0,  # At end of session, positions valued at cost
            net_profit_cents=net_profit,
            max_drawdown_cents=max_drawdown_cents,
            max_drawdown_pct=max_dd_pct,
            opportunities_evaluated=opps_eval,
            opportunities_rejected_risk=opps_rej_risk,
            opportunities_rejected_depth=opps_rej_depth,
            trades_accepted=trades_accepted,
            trades_filled=trades_filled,
            trades_partially_filled=trades_partial,
            rejection_reasons=sorted_rejections,
        )

        return ReplayReport(
            metrics=metrics,
            equity_curve=tuple(equity_curve),
            trades=tuple(trades),
            portfolio=portfolio,
        )

    def _evaluate_and_simulate(
        self,
        ev: ReplayEvent,
        timestamp: str,
        portfolio: Optional[PaperPortfolio] = None,
        risk_manager: Optional[RiskManager] = None,
    ) -> PaperTrade:
        fee = ev.fee_per_contract_cents or self.default_fee
        if ev.strategy_type == OPPORTUNITY_BINARY_PARITY:
            if not ev.book:
                raise ReplayError("Binary parity replay event requires 'book'")
            return self.executor.simulate_binary_parity(
                ev.book,
                requested_quantity=ev.requested_quantity,
                fee_per_contract_cents=fee,
                timestamp=timestamp,
                portfolio=portfolio,
                risk_manager=risk_manager,
            )
        if ev.strategy_type in (
            OPPORTUNITY_MECE_BASKET_LONG_YES,
            OPPORTUNITY_MECE_BASKET_LONG_NO,
        ):
            if not ev.event or not ev.books_by_outcome:
                raise ReplayError("MECE basket replay event requires 'event' and 'books_by_outcome'")
            side = (
                YES
                if ev.strategy_type == OPPORTUNITY_MECE_BASKET_LONG_YES
                else NO
            )
            return self.executor.simulate_mece_basket(
                ev.event,
                ev.books_by_outcome,
                basket_side=side,
                requested_quantity=ev.requested_quantity,
                fee_per_contract_cents=fee,
                timestamp=timestamp,
                portfolio=portfolio,
                risk_manager=risk_manager,
            )
        if ev.strategy_type == OPPORTUNITY_PORTFOLIO:
            if not ev.portfolio or not ev.books_by_contract:
                raise ReplayError("Portfolio replay event requires 'portfolio' and 'books_by_contract'")
            return self.executor.simulate_portfolio(
                ev.portfolio,
                ev.books_by_contract,
                unit_quantity=ev.requested_quantity,
                fee_per_contract_cents=fee,
                timestamp=timestamp,
                paper_portfolio=portfolio,
                risk_manager=risk_manager,
            )
        raise ReplayError(f"Unsupported strategy type for replay: {ev.strategy_type!r}")
