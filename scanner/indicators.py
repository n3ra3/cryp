"""Technical indicators. All pure functions over lists of closed `Candle`s.

Design rules:
  * Never look at a forming candle — callers pass only CLOSED candles.
  * Wilder smoothing (RMA) for ATR and RSI, matching TradingView defaults.
  * Functions return the LATEST value (float) unless a *_series variant is used.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .models import Candle


# --------------------------------------------------------------------------- #
#  Moving averages
# --------------------------------------------------------------------------- #
def ema_series(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Exponential moving average. Seeded with an SMA of the first `period`
    values (TradingView-compatible). Warmup slots are None."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (values[i] - prev) * k + prev
        out[i] = prev
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    s = ema_series(values, period)
    return s[-1] if s else None


# --------------------------------------------------------------------------- #
#  True Range / ATR (Wilder)
# --------------------------------------------------------------------------- #
def true_ranges(candles: Sequence[Candle]) -> list[float]:
    trs: list[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        if prev_close is None:
            trs.append(c.high - c.low)
        else:
            trs.append(max(
                c.high - c.low,
                abs(c.high - prev_close),
                abs(c.low - prev_close),
            ))
        prev_close = c.close
    return trs


def atr_series(candles: Sequence[Candle], period: int) -> list[Optional[float]]:
    trs = true_ranges(candles)
    n = len(trs)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    # first ATR = simple average of the first `period` true ranges
    first = sum(trs[:period]) / period
    out[period - 1] = first
    prev = first
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def atr(candles: Sequence[Candle], period: int) -> Optional[float]:
    s = atr_series(candles, period)
    return s[-1] if s else None


# --------------------------------------------------------------------------- #
#  RSI (Wilder)
# --------------------------------------------------------------------------- #
def rsi_series(values: Sequence[float], period: int) -> list[Optional[float]]:
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi(values: Sequence[float], period: int) -> Optional[float]:
    s = rsi_series(values, period)
    return s[-1] if s else None


# --------------------------------------------------------------------------- #
#  Swing high / low over a lookback window
# --------------------------------------------------------------------------- #
def swing_high(candles: Sequence[Candle], lookback: int) -> tuple[float, int]:
    """Highest HIGH within the last `lookback` candles.
    Returns (price, index_within_full_list)."""
    window = list(candles)[-lookback:]
    offset = len(candles) - len(window)
    best_i = max(range(len(window)), key=lambda i: window[i].high)
    return window[best_i].high, offset + best_i


def swing_low(candles: Sequence[Candle], lookback: int) -> tuple[float, int]:
    """Lowest LOW within the last `lookback` candles.
    Returns (price, index_within_full_list)."""
    window = list(candles)[-lookback:]
    offset = len(candles) - len(window)
    best_i = min(range(len(window)), key=lambda i: window[i].low)
    return window[best_i].low, offset + best_i


# --------------------------------------------------------------------------- #
#  Bundle: compute everything the detector needs on the LATEST closed candle
# --------------------------------------------------------------------------- #
def compute_indicators(candles: Sequence[Candle], det_cfg: dict) -> Optional[dict]:
    """Returns latest indicator values, or None if there is not enough history."""
    closes = [c.close for c in candles]
    atr_p = det_cfg["atr_period"]
    rsi_p = det_cfg["rsi_period"]
    ema_p = det_cfg["ema_period"]
    swing_lb = det_cfg["swing_lookback"]

    need = max(atr_p, rsi_p, ema_p, swing_lb) + 1
    if len(candles) < need:
        return None

    _atr = atr(candles, atr_p)
    _rsi = rsi(closes, rsi_p)
    _ema = ema(closes, ema_p)
    if _atr is None or _rsi is None or _ema is None:
        return None

    sh_price, sh_idx = swing_high(candles, swing_lb)
    sl_price, sl_idx = swing_low(candles, swing_lb)

    return {
        "atr": _atr,
        "rsi": _rsi,
        "ema": _ema,
        "swing_high": sh_price,
        "swing_high_idx": sh_idx,
        "swing_low": sl_price,
        "swing_low_idx": sl_idx,
    }
