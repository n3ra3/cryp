"""Statistics / reporting over closed paper trades.

All figures are computed straight from the journal so they cannot drift from
what actually happened. Expectancy is reported in R.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .models import Trade, ExitReason


@dataclass
class StatsReport:
    total_signals: int = 0          # closed trades considered
    wins: int = 0
    losses: int = 0
    scratches: int = 0              # ~0 R (rare)
    win_rate: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0         # positive magnitude of the average loss
    expectancy_r: float = 0.0
    total_r: float = 0.0
    max_drawdown_r: float = 0.0
    by_exchange: dict = field(default_factory=dict)
    by_symbol: dict = field(default_factory=dict)
    by_reason: dict = field(default_factory=dict)

    def format_text(self) -> str:
        lines = [
            "📊 Paper-trading stats",
            f"Closed trades: {self.total_signals}",
            f"Win rate: {self.win_rate*100:.1f}%  ({self.wins}W / {self.losses}L)",
            f"Avg win: +{self.avg_win_r:.2f}R   Avg loss: -{self.avg_loss_r:.2f}R",
            f"Expectancy: {self.expectancy_r:+.3f}R / trade",
            f"Total: {self.total_r:+.2f}R   Max DD: -{self.max_drawdown_r:.2f}R",
        ]
        if self.by_reason:
            reasons = ", ".join(f"{k}:{v}" for k, v in sorted(self.by_reason.items()))
            lines.append(f"Exits: {reasons}")
        if self.by_symbol:
            per = ", ".join(
                f"{s} {d['total_r']:+.1f}R({d['n']})"
                for s, d in sorted(self.by_symbol.items())
            )
            lines.append(f"By symbol: {per}")
        if self.by_exchange:
            per = ", ".join(
                f"{e} {d['total_r']:+.1f}R({d['n']})"
                for e, d in sorted(self.by_exchange.items())
            )
            lines.append(f"By exchange: {per}")
        return "\n".join(lines)


def _bucket(trades, keyfn) -> dict:
    out: dict = defaultdict(lambda: {"n": 0, "total_r": 0.0, "wins": 0})
    for t in trades:
        k = keyfn(t)
        out[k]["n"] += 1
        out[k]["total_r"] += t.r_multiple or 0.0
        if (t.r_multiple or 0.0) > 0:
            out[k]["wins"] += 1
    return dict(out)


def compute_stats(trades: list[Trade]) -> StatsReport:
    closed = [t for t in trades if t.closed and t.r_multiple is not None]
    rep = StatsReport()
    rep.total_signals = len(closed)
    if not closed:
        return rep

    wins = [t for t in closed if t.r_multiple > 1e-9]
    losses = [t for t in closed if t.r_multiple < -1e-9]
    scratches = [t for t in closed if abs(t.r_multiple) <= 1e-9]

    rep.wins = len(wins)
    rep.losses = len(losses)
    rep.scratches = len(scratches)
    rep.win_rate = rep.wins / rep.total_signals

    rep.avg_win_r = sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0
    rep.avg_loss_r = abs(sum(t.r_multiple for t in losses) / len(losses)) if losses else 0.0

    loss_rate = rep.losses / rep.total_signals
    rep.expectancy_r = rep.win_rate * rep.avg_win_r - loss_rate * rep.avg_loss_r
    rep.total_r = sum(t.r_multiple for t in closed)

    rep.max_drawdown_r = _max_drawdown([t.r_multiple for t in closed])

    rep.by_exchange = _bucket(closed, lambda t: t.exchange)
    rep.by_symbol = _bucket(closed, lambda t: t.symbol)
    rep.by_reason = {
        r.value: sum(1 for t in closed if t.exit_reason is r)
        for r in ExitReason
        if any(t.exit_reason is r for t in closed)
    }
    return rep


def _max_drawdown(r_series: list[float]) -> float:
    """Max peak-to-trough drawdown of the cumulative R equity curve."""
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for r in r_series:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def daily_loss_halted(recent_closed: list[Trade], max_daily_losses: int) -> bool:
    """True if the last `max_daily_losses` closed trades were all losers.
    (Stat/flag only — does NOT block scanning.)"""
    if len(recent_closed) < max_daily_losses:
        return False
    tail = recent_closed[-max_daily_losses:]
    return all((t.r_multiple or 0.0) < 0 for t in tail)
