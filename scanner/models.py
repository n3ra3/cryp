"""Core data structures shared across modules.

These are intentionally plain dataclasses so they are trivial to construct in
tests and to serialise to/from SQLite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class ExitReason(str, Enum):
    STOP = "stop"
    TARGET = "target"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Candle:
    """A single OHLCV candle. `ts` is the candle OPEN time in ms (UTC)."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    # --- convenience metrics (used by the detector) ---
    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @classmethod
    def from_ccxt(cls, row) -> "Candle":
        # ccxt OHLCV row: [timestamp, open, high, low, close, volume]
        return cls(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if row[5] is not None else 0.0,
        )


@dataclass
class Signal:
    """A detected setup. Contains everything needed to open a paper trade and
    to render a Telegram alert. No prices are 'filled' yet — entry happens at
    the OPEN of the next candle."""

    exchange: str
    symbol: str
    timeframe: str
    side: Side
    signal_ts: int              # ts of the closed candle that produced the signal

    signal_close: float         # close of the signal candle (reference price)
    stop_price: float
    target_price: float
    partial_target: Optional[float]

    # geometry / context (also stored for post-hoc analysis)
    swing_level: float          # the swing high (short) / low (long) that was faded
    swing_origin: float         # origin of the impulse leg (leg start)
    rsi_at_signal: float
    atr_at_signal: float
    stretch_atr: float          # (close - EMA) / ATR, absolute value
    rr: float                   # approximate reward:risk using signal_close as entry
    impulse_pct: float = 0.0    # size of the impulse leg as a fraction (the "spike")

    def to_raw_json(self) -> str:
        d = asdict(self)
        d["side"] = self.side.value
        return json.dumps(d, sort_keys=True)


@dataclass
class Trade:
    """A virtual (paper) trade. Lives in memory while OPEN and is persisted to
    SQLite. `id` is assigned by the journal on insert."""

    exchange: str
    symbol: str
    timeframe: str
    side: Side
    signal_ts: int

    entry_price: float          # filled entry (incl. slippage)
    stop_price: float
    target_price: float
    position_notional: float
    risk_usd: float
    quantity: float

    rsi_at_signal: float
    atr_at_signal: float
    stretch_atr: float
    swing_level: float
    raw_signal_json: str

    # --- filled in on close ---
    exit_price: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    r_multiple: Optional[float] = None
    fees_usd: float = 0.0
    slippage_usd: float = 0.0
    bars_held: int = 0

    id: Optional[int] = None
    closed: bool = False

    @property
    def is_open(self) -> bool:
        return not self.closed
