"""Paper-execution tests: the stop-wins edge case and honest fee/slippage math."""

import copy
import math

from scanner.models import Candle, Signal, Side, ExitReason
from scanner import paper_exec
from conftest import make_candle


def _short_signal():
    return Signal(
        exchange="bybit", symbol="BTC/USDT", timeframe="15m", side=Side.SHORT,
        signal_ts=0, signal_close=100.0,
        stop_price=102.0, target_price=98.0, partial_target=99.0,
        swing_level=101.5, swing_origin=90.0,
        rsi_at_signal=78.0, atr_at_signal=1.0, stretch_atr=3.0, rr=2.0,
    )


def test_stop_wins_when_one_candle_hits_both(cfg):
    sig = _short_signal()
    # entry candle: fill at open=100; range spans BOTH stop(102) and target(98)
    entry = make_candle(1, 100, 103, 97, 99)   # high>=102 AND low<=98
    trade = paper_exec.simulate_trade(sig, [entry], cfg)
    assert trade.closed
    assert trade.exit_reason is ExitReason.STOP     # conservative
    assert trade.r_multiple < 0


def test_target_hit_is_a_win(cfg):
    sig = _short_signal()
    entry = make_candle(1, 100, 100.5, 99.8, 100.0)  # nothing hit
    hit = make_candle(2, 100, 100.2, 97.9, 98.0)     # low<=98 -> target
    trade = paper_exec.simulate_trade(sig, [entry, hit], cfg)
    assert trade.exit_reason is ExitReason.TARGET
    assert trade.r_multiple > 0


def test_fees_and_slippage_reduce_r(cfg):
    sig = _short_signal()
    entry = make_candle(1, 100, 100.5, 99.8, 100.0)
    hit = make_candle(2, 100, 100.2, 97.9, 98.0)

    # frictionless config
    frictionless = copy.deepcopy(cfg)
    frictionless["execution"]["taker_fee_pct"] = 0.0
    frictionless["execution"]["slippage_pct"] = 0.0

    r_with = paper_exec.simulate_trade(sig, [entry, hit], cfg).r_multiple
    r_without = paper_exec.simulate_trade(sig, [entry, hit], frictionless).r_multiple

    assert r_with < r_without           # friction genuinely costs R
    assert r_without > r_with > 0       # still a winner, but smaller


def test_stop_r_is_about_minus_one_plus_friction(cfg):
    sig = _short_signal()
    entry = make_candle(1, 100, 102.5, 99.9, 100.0)  # high>=102 -> stop
    trade = paper_exec.simulate_trade(sig, [entry], cfg)
    assert trade.exit_reason is ExitReason.STOP
    # a full stop loses ~1R plus fees/slippage; never better than -1R
    assert trade.r_multiple < -1.0
    assert trade.r_multiple > -1.2


def test_timeout_closes_at_market(cfg):
    sig = _short_signal()
    cfg = copy.deepcopy(cfg)
    cfg["execution"]["max_bars_in_trade"] = 2
    c1 = make_candle(1, 100, 100.4, 99.7, 100.0)
    c2 = make_candle(2, 100, 100.4, 99.7, 100.1)  # 2nd bar -> timeout
    trade = paper_exec.simulate_trade(sig, [c1, c2], cfg)
    assert trade.exit_reason is ExitReason.TIMEOUT
    assert trade.bars_held == 2


def test_long_target_and_stop_directionality(cfg):
    sig = Signal(
        exchange="bybit", symbol="ETH/USDT", timeframe="15m", side=Side.LONG,
        signal_ts=0, signal_close=100.0,
        stop_price=98.0, target_price=104.0, partial_target=102.0,
        swing_level=98.5, swing_origin=110.0,
        rsi_at_signal=22.0, atr_at_signal=1.0, stretch_atr=3.0, rr=2.0,
    )
    entry = make_candle(1, 100, 100.2, 99.9, 100.0)
    win = make_candle(2, 100, 104.1, 99.9, 104.0)   # high>=104 -> target
    trade = paper_exec.simulate_trade(sig, [entry, win], cfg)
    assert trade.exit_reason is ExitReason.TARGET
    assert trade.r_multiple > 0
