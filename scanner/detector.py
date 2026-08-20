"""Setup detector — ISOLATED module.

Interface:  detect(candles, indicators, config) -> Signal | None

Strategy: fade-the-spike mean reversion. We fade an over-extended impulse and
target a 50% Fibonacci retracement of the impulse leg. Both directions.

Everything numeric lives in config.yaml under `detector:` and is marked
`# PLACEHOLDER — tune later`. Tuning must never require edits to other modules.

No-lookahead guarantee: `candles` are CLOSED candles only. The signal is
produced on candles[-1] (the most recently closed bar). The forming bar is
never passed in. Entry is executed later at the OPEN of the next bar.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .models import Candle, Signal, Side


def detect(candles: Sequence[Candle], indicators: dict, config: dict) -> Optional[Signal]:
    """Return a Signal if the last closed candle completes a setup, else None.

    `config` is the FULL config dict; detector reads only `config['detector']`
    plus exchange/symbol/timeframe passed via `indicators['_meta']`.
    """
    det = config["detector"]
    meta = indicators["_meta"]  # {'exchange','symbol','timeframe'}

    last = candles[-1]
    prev = candles[-2]

    atr = indicators["atr"]
    ema = indicators["ema"]
    rsi = indicators["rsi"]
    if atr <= 0:
        return None

    # Regime filter: only fade in a range / weak trend. In a strong trend the
    # pump keeps running and stops us out. Backtested (both exchanges, 1000+
    # trades) ADX<20 ~3x'd expectancy. Skipped when max_adx is unset.
    max_adx = det.get("max_adx")
    if max_adx is not None:
        _adx = indicators.get("adx")
        if _adx is not None and _adx >= max_adx:
            return None

    # signed stretch of the close away from the EMA, in ATR units
    stretch = (last.close - ema) / atr

    short_sig = None
    long_sig = None

    if det.get("enable_short", True):
        short_sig = _try_short(candles, last, prev, indicators, det, meta, atr, rsi, stretch)
    if det.get("enable_long", True):
        long_sig = _try_long(candles, last, prev, indicators, det, meta, atr, rsi, stretch)

    # If both fired (rare/contradictory) prefer the one with better R:R.
    candidates = [s for s in (short_sig, long_sig) if s is not None]
    if not candidates:
        return None
    best = max(candidates, key=lambda s: s.rr)
    if best.rr < det["min_rr"]:
        return None
    return best


# --------------------------------------------------------------------------- #
#  SHORT — fade an up-spike into a swing high
# --------------------------------------------------------------------------- #
def _try_short(candles, last, prev, ind, det, meta, atr, rsi, stretch) -> Optional[Signal]:
    swing_high = ind["swing_high"]
    swing_high_idx = ind["swing_high_idx"]
    if swing_high <= 0:
        return None

    # 1) SPIKE: impulse leg big + fresh + CONFIRMED (peak already rolled over).
    leg_origin = _leg_low_before(candles, swing_high_idx)
    leg = swing_high - leg_origin
    if leg <= 0:
        return None
    impulse_pct = _spike_ok(candles, swing_high_idx, leg, leg_origin, atr, det)
    if impulse_pct is None:
        return None

    # 2) Exhaustion: stretched above EMA OR RSI overbought OR the spike itself is
    #    already huge (a 30%+ pump IS exhaustion; RSI/stretch fade at the peak).
    stretched = stretch >= det["stretch_atr_mult"]
    overbought = rsi >= det["rsi_overbought"]
    huge = impulse_pct >= det.get("big_spike_exempt_pct", 1.0)
    if not (stretched or overbought or huge):
        return None

    # 3) Retracement has STARTED: price pulled back off the peak within a sane
    #    window (fade enters as the move rolls over, not at the very high).
    pullback = (swing_high - last.close) / swing_high
    pmin = det.get("pullback_min_pct", 0.0)
    pmax = det.get("pullback_max_pct", 0.10)
    if not (pmin <= pullback <= pmax):
        return None

    # 4) Bearish rejection confirmation: down-close AND below prior candle high
    if not (last.bearish and last.close < prev.high):
        return None

    # 5) Stop above the spike high + buffer; target = fib retrace of the leg
    stop = swing_high + det["stop_buffer_atr"] * atr
    target = swing_high - det["fib_target"] * leg
    partial = swing_high - det["fib_partial"] * leg

    rr = _rr(last.close, stop, target, Side.SHORT)
    if rr is None:
        return None

    return Signal(
        exchange=meta["exchange"], symbol=meta["symbol"], timeframe=meta["timeframe"],
        side=Side.SHORT, signal_ts=last.ts,
        signal_close=last.close, stop_price=stop, target_price=target, partial_target=partial,
        swing_level=swing_high, swing_origin=leg_origin,
        rsi_at_signal=rsi, atr_at_signal=atr, stretch_atr=abs(stretch), rr=rr,
        impulse_pct=impulse_pct,
    )


# --------------------------------------------------------------------------- #
#  LONG — fade a down-spike into a swing low (mirror image)
# --------------------------------------------------------------------------- #
def _try_long(candles, last, prev, ind, det, meta, atr, rsi, stretch) -> Optional[Signal]:
    swing_low = ind["swing_low"]
    swing_low_idx = ind["swing_low_idx"]
    if swing_low <= 0:
        return None

    # 1) SPIKE (mirror): down-impulse big + fresh + CONFIRMED (low rolled over).
    leg_origin = _leg_high_before(candles, swing_low_idx)
    leg = leg_origin - swing_low
    if leg <= 0:
        return None
    impulse_pct = _spike_ok(candles, swing_low_idx, leg, leg_origin, atr, det)
    if impulse_pct is None:
        return None

    # 2) Exhaustion: stretched below EMA OR RSI oversold OR the dump is huge.
    stretched = stretch <= -det["stretch_atr_mult"]
    oversold = rsi <= det["rsi_oversold"]
    huge = impulse_pct >= det.get("big_spike_exempt_pct", 1.0)
    if not (stretched or oversold or huge):
        return None

    # 3) Bounce has STARTED: price pulled back up off the low within the window
    pullback = (last.close - swing_low) / swing_low
    pmin = det.get("pullback_min_pct", 0.0)
    pmax = det.get("pullback_max_pct", 0.10)
    if not (pmin <= pullback <= pmax):
        return None

    # 4) bullish rejection: up-close AND above prior candle low
    if not (last.bullish and last.close > prev.low):
        return None

    # 5) stop below the spike low + buffer; target = fib retrace up
    stop = swing_low - det["stop_buffer_atr"] * atr
    target = swing_low + det["fib_target"] * leg
    partial = swing_low + det["fib_partial"] * leg

    rr = _rr(last.close, stop, target, Side.LONG)
    if rr is None:
        return None

    return Signal(
        exchange=meta["exchange"], symbol=meta["symbol"], timeframe=meta["timeframe"],
        side=Side.LONG, signal_ts=last.ts,
        signal_close=last.close, stop_price=stop, target_price=target, partial_target=partial,
        swing_level=swing_low, swing_origin=leg_origin,
        rsi_at_signal=rsi, atr_at_signal=atr, stretch_atr=abs(stretch), rr=rr,
        impulse_pct=impulse_pct,
    )


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _spike_ok(candles, spike_idx: int, leg: float, leg_origin: float,
              atr: float, det: dict):
    """The core 'сильный скачок' gate.

    Returns the impulse size as a fraction (leg/origin) if the move is both
    strong enough (>= impulse_min_pct AND >= impulse_min_atr) and recent enough
    (the spike occurred within impulse_lookback bars). Otherwise returns None.

    Thresholds default to 'off' when absent, so older configs/tests still work.
    """
    bars_since = (len(candles) - 1) - spike_idx
    lookback = det.get("impulse_lookback")
    if lookback is not None and bars_since > lookback:
        return None
    # Peak CONFIRMED: the extreme must be at least `peak_confirm_bars` old, i.e.
    # price has NOT made a new extreme for that many bars -> the pump has rolled
    # over. This is what stops us from shorting into a still-rising pump.
    if bars_since < det.get("peak_confirm_bars", 0):
        return None

    leg_pct = leg / leg_origin if leg_origin > 0 else 0.0
    leg_atr = leg / atr if atr > 0 else 0.0

    if leg_pct < det.get("impulse_min_pct", 0.0):
        return None
    if leg_atr < det.get("impulse_min_atr", 0.0):
        return None
    return leg_pct


def _leg_low_before(candles: Sequence[Candle], high_idx: int) -> float:
    """Lowest low from the start of the window up to (and including) the spike
    high bar — the origin of the up-impulse leg."""
    segment = candles[: high_idx + 1]
    if not segment:
        return candles[0].low
    return min(c.low for c in segment)


def _leg_high_before(candles: Sequence[Candle], low_idx: int) -> float:
    """Highest high from the start of the window up to (and including) the
    spike low bar — the origin of the down-impulse leg."""
    segment = candles[: low_idx + 1]
    if not segment:
        return candles[0].high
    return max(c.high for c in segment)


def _rr(entry: float, stop: float, target: float, side: Side) -> Optional[float]:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return None
    # sanity: target must be on the profit side of entry
    if side is Side.SHORT and not (target < entry < stop):
        return None
    if side is Side.LONG and not (stop < entry < target):
        return None
    return reward / risk
