"""Paper execution: turn a Signal into a virtual Trade and walk it forward.

Honest-simulation rules (never relaxed):
  * Entry fills at the OPEN of the entry candle (the bar after the signal),
    worsened by slippage.
  * Taker fee charged on BOTH legs; slippage charged on BOTH legs.
  * Resolving stop/target: iterate candles from the entry bar forward.
      - low  <= stop   -> STOP
      - high >= target -> TARGET
      - if a single candle satisfies BOTH -> STOP wins (conservative).
  * Timeout: after `max_bars_in_trade` bars, close at that candle's CLOSE.
  * Result is expressed in R = net_pnl / risk_usd.

Two entry points:
  * simulate_trade(...)  — pure, deterministic; used by tests & backtests.
  * PaperExecutor        — stateful; drives OPEN trades forward candle-by-candle
                           in live operation, surviving restarts via the journal.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .models import Candle, Signal, Trade, Side, ExitReason


# --------------------------------------------------------------------------- #
#  Trade construction (entry fill)
# --------------------------------------------------------------------------- #
def build_trade(signal: Signal, entry_open: float, cfg: dict) -> Trade:
    """Create an OPEN trade filled at `entry_open` (the entry candle's open),
    with slippage applied and position sized from the risk model."""
    ex = cfg["execution"]
    risk = cfg["risk"]
    slip = ex["slippage_pct"]

    # Entry fill worsened by slippage (sell lower on short, buy higher on long).
    if signal.side is Side.SHORT:
        entry_fill = entry_open * (1.0 - slip)
    else:
        entry_fill = entry_open * (1.0 + slip)

    stop = signal.stop_price
    risk_usd = risk["paper_equity"] * risk["risk_per_trade_pct"]

    stop_dist = abs(entry_fill - stop)
    if stop_dist <= 0:
        raise ValueError("stop distance is zero — cannot size position")
    quantity = risk_usd / stop_dist
    notional = quantity * entry_fill

    return Trade(
        exchange=signal.exchange, symbol=signal.symbol, timeframe=signal.timeframe,
        side=signal.side, signal_ts=signal.signal_ts,
        entry_price=entry_fill, stop_price=stop, target_price=signal.target_price,
        position_notional=notional, risk_usd=risk_usd, quantity=quantity,
        rsi_at_signal=signal.rsi_at_signal, atr_at_signal=signal.atr_at_signal,
        stretch_atr=signal.stretch_atr, swing_level=signal.swing_level,
        raw_signal_json=signal.to_raw_json(),
    )


# --------------------------------------------------------------------------- #
#  Resolve one candle against an open trade
# --------------------------------------------------------------------------- #
def resolve_candle(trade: Trade, candle: Candle, cfg: dict) -> bool:
    """Advance `trade` by one bar. Mutates trade; returns True if it closed."""
    if trade.closed:
        return True
    ex = cfg["execution"]
    trade.bars_held += 1

    if trade.side is Side.SHORT:
        # short: stop is ABOVE entry, target is BELOW
        hit_stop = candle.high >= trade.stop_price
        hit_target = candle.low <= trade.target_price
    else:
        # long: stop is BELOW entry, target is ABOVE
        hit_stop = candle.low <= trade.stop_price
        hit_target = candle.high >= trade.target_price

    if hit_stop and hit_target:
        # conservative: assume stop filled first
        _close(trade, trade.stop_price, ExitReason.STOP, cfg)
        return True
    if hit_stop:
        _close(trade, trade.stop_price, ExitReason.STOP, cfg)
        return True
    if hit_target:
        _close(trade, trade.target_price, ExitReason.TARGET, cfg)
        return True

    if trade.bars_held >= ex["max_bars_in_trade"]:
        _close(trade, candle.close, ExitReason.TIMEOUT, cfg)
        return True
    return False


def _close(trade: Trade, exit_level: float, reason: ExitReason, cfg: dict) -> None:
    ex = cfg["execution"]
    slip = ex["slippage_pct"]
    fee = ex["taker_fee_pct"]

    # Exit fill worsened by slippage (buy back higher on short, sell lower on long).
    if trade.side is Side.SHORT:
        exit_fill = exit_level * (1.0 + slip)
        gross = trade.quantity * (trade.entry_price - exit_fill)
    else:
        exit_fill = exit_level * (1.0 - slip)
        gross = trade.quantity * (exit_fill - trade.entry_price)

    entry_notional = trade.quantity * trade.entry_price
    exit_notional = trade.quantity * exit_fill
    fees = (entry_notional + exit_notional) * fee
    net = gross - fees

    # slippage cost (for the journal) = qty * price concession on both legs
    slip_cost = trade.quantity * (abs(exit_fill - exit_level))
    # entry-leg slippage relative to the raw open is embedded in entry_price;
    # approximate its dollar value symmetrically.
    slip_cost += entry_notional * slip

    trade.exit_price = exit_fill
    trade.exit_reason = reason
    trade.fees_usd = fees
    trade.slippage_usd = slip_cost
    trade.r_multiple = net / trade.risk_usd if trade.risk_usd else 0.0
    trade.closed = True


# --------------------------------------------------------------------------- #
#  Pure simulation (tests / backtest)
# --------------------------------------------------------------------------- #
def simulate_trade(signal: Signal, forward: Sequence[Candle], cfg: dict) -> Trade:
    """`forward[0]` is the ENTRY candle (filled at its open). Remaining candles
    resolve the trade. If it never resolves, it is closed at the last candle's
    close as a timeout."""
    if not forward:
        raise ValueError("need at least one forward candle (the entry candle)")
    trade = build_trade(signal, forward[0].open, cfg)
    for candle in forward:
        if resolve_candle(trade, candle, cfg):
            return trade
    # ran out of data without hitting stop/target/timeout -> mark-to-market close
    _close(trade, forward[-1].close, ExitReason.TIMEOUT, cfg)
    return trade


# --------------------------------------------------------------------------- #
#  Stateful live executor
# --------------------------------------------------------------------------- #
class PaperExecutor:
    """Holds OPEN trades in memory (mirrored to the journal) and advances them
    as new closed candles arrive. Also holds PENDING signals awaiting entry."""

    def __init__(self, cfg: dict, journal, on_close=None):
        self.cfg = cfg
        self.journal = journal
        self.on_close = on_close  # optional callback(trade) after a close
        # keyed by (exchange, symbol, timeframe)
        self._open: dict[tuple, list[Trade]] = {}
        self._pending: dict[tuple, Signal] = {}

    def restore(self) -> None:
        """Reload OPEN trades from the journal after a restart."""
        for trade in self.journal.load_open_trades():
            self._open.setdefault(self._key(trade), []).append(trade)

    @staticmethod
    def _key(obj) -> tuple:
        return (obj.exchange, obj.symbol, obj.timeframe)

    def open_trade_count(self) -> int:
        return sum(len(v) for v in self._open.values())

    def has_position(self, key: tuple) -> bool:
        """True if this market already has an open trade or a pending entry.
        Used to avoid stacking setups on the same market during research."""
        return bool(self._open.get(key)) or key in self._pending

    def register_signal(self, signal: Signal) -> None:
        """Queue a signal; it fills at the OPEN of the next candle for its market."""
        self._pending[self._key(signal)] = signal

    def on_new_candle(self, exchange: str, symbol: str, timeframe: str,
                      candle: Candle) -> list[Trade]:
        """Process one freshly-closed candle for a market. Returns trades that
        closed on this candle."""
        key = (exchange, symbol, timeframe)
        closed_now: list[Trade] = []

        # 1) fill a pending entry at this candle's open
        pending = self._pending.pop(key, None)
        if pending is not None:
            trade = build_trade(pending, candle.open, self.cfg)
            self.journal.insert_trade(trade)
            self._open.setdefault(key, []).append(trade)

        # 2) resolve every open trade for this market against this candle
        still_open: list[Trade] = []
        for trade in self._open.get(key, []):
            if resolve_candle(trade, candle, self.cfg):
                self.journal.update_trade(trade)
                closed_now.append(trade)
                if self.on_close:
                    self.on_close(trade)
            else:
                self.journal.update_trade(trade)
                still_open.append(trade)
        if key in self._open:
            self._open[key] = still_open

        return closed_now
