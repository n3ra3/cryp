"""Unit tests for indicators against hand-computed expected values."""

import math

from scanner import indicators as ind
from scanner.models import Candle
from conftest import make_candle


def test_ema_hand_computed():
    # values [1,2,3,4,5], period 2
    #   seed SMA(1,2)=1.5 ; k=2/3
    #   -> 2.5, 3.5, 4.5
    vals = [1, 2, 3, 4, 5]
    series = ind.ema_series(vals, 2)
    assert series[0] is None
    assert math.isclose(series[1], 1.5)
    assert math.isclose(series[2], 2.5)
    assert math.isclose(series[3], 3.5)
    assert math.isclose(series[4], 4.5)
    assert math.isclose(ind.ema(vals, 2), 4.5)


def test_atr_constant_range():
    # identical candles: O=100 H=105 L=95 C=100 -> TR is always 10 -> ATR=10
    candles = [make_candle(i, 100, 105, 95, 100) for i in range(30)]
    assert math.isclose(ind.atr(candles, 14), 10.0, rel_tol=1e-9)


def test_rsi_all_gains_is_100():
    closes = list(range(1, 30))  # strictly increasing
    assert math.isclose(ind.rsi(closes, 14), 100.0)


def test_rsi_all_losses_is_0():
    closes = list(range(30, 1, -1))  # strictly decreasing
    assert math.isclose(ind.rsi(closes, 14), 0.0)


def test_rsi_mixed_hand_computed():
    # closes [100,102,101,104], period 2
    #  deltas: +2, -1, +3
    #  seed(i=1,2): avg_gain=1, avg_loss=0.5 -> RSI=66.666..
    #  i=3: avg_gain=2, avg_loss=0.25 -> rs=8 -> RSI=88.888..
    closes = [100, 102, 101, 104]
    series = ind.rsi_series(closes, 2)
    assert math.isclose(series[2], 100 - 100 / 3, rel_tol=1e-9)
    assert math.isclose(series[3], 100 - 100 / 9, rel_tol=1e-9)


def test_swing_high_low():
    candles = [
        make_candle(0, 10, 12, 9, 11),
        make_candle(1, 11, 15, 10, 14),   # highest high = 15
        make_candle(2, 14, 14, 6, 8),     # lowest low = 6
        make_candle(3, 8, 9, 7, 8),
    ]
    sh, sh_idx = ind.swing_high(candles, 4)
    sl, sl_idx = ind.swing_low(candles, 4)
    assert sh == 15 and sh_idx == 1
    assert sl == 6 and sl_idx == 2


def test_candle_metrics():
    c = make_candle(0, 100, 110, 90, 105)
    assert c.bullish and not c.bearish
    assert math.isclose(c.body, 5)
    assert math.isclose(c.upper_wick, 5)     # 110 - max(100,105)
    assert math.isclose(c.lower_wick, 10)    # min(100,105) - 90
