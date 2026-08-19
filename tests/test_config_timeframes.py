"""Multi-timeframe config normalization + per-timeframe stats split."""

import copy
import pytest

from scanner.config import _normalize_timeframes
from scanner import stats as stats_mod
from scanner import paper_exec
from scanner.models import Signal, Side, ExitReason
from conftest import make_candle


def test_timeframes_list_kept():
    cfg = {"timeframes": ["5m", "15m"]}
    _normalize_timeframes(cfg)
    assert cfg["timeframes"] == ["5m", "15m"]
    assert cfg["timeframe"] == "5m"          # legacy default = first


def test_single_timeframe_promoted_to_list():
    cfg = {"timeframe": "15m"}
    _normalize_timeframes(cfg)
    assert cfg["timeframes"] == ["15m"]
    assert cfg["timeframe"] == "15m"


def test_missing_timeframe_raises():
    with pytest.raises(ValueError):
        _normalize_timeframes({})


def _sig(tf, target, stop):
    return Signal(
        exchange="mexc", symbol="BTC/USDT:USDT", timeframe=tf, side=Side.SHORT,
        signal_ts=0, signal_close=100.0, stop_price=stop, target_price=target,
        partial_target=99.0, swing_level=101.0, swing_origin=90.0,
        rsi_at_signal=78, atr_at_signal=1.0, stretch_atr=3.0, rr=2.0,
    )


def test_stats_split_by_timeframe(cfg):
    # one winner on 5m, one loser on 15m -> stats must separate them
    win = paper_exec.simulate_trade(
        _sig("5m", 98.0, 102.0),
        [make_candle(1, 100, 100.2, 97.9, 98.0)], cfg)
    loss = paper_exec.simulate_trade(
        _sig("15m", 98.0, 102.0),
        [make_candle(1, 100, 102.5, 99.9, 100.0)], cfg)
    assert win.exit_reason is ExitReason.TARGET
    assert loss.exit_reason is ExitReason.STOP

    rep = stats_mod.compute_stats([win, loss])
    assert set(rep.by_timeframe.keys()) == {"5m", "15m"}
    assert rep.by_timeframe["5m"]["total_r"] > 0
    assert rep.by_timeframe["15m"]["total_r"] < 0
