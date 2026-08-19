import copy
import os
import sys

import pytest

# make the package importable when running `pytest` from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.models import Candle, Signal, Side  # noqa: E402


BASE_CFG = {
    "exchanges": ["bybit"],
    "symbols": ["BTC/USDT"],
    "timeframe": "15m",
    "poll_interval_sec": 60,
    "candle_fetch_limit": 200,
    "backoff": {"base_sec": 0.001, "max_sec": 0.01, "factor": 2.0},
    "risk": {
        "paper_equity": 500.0,
        "risk_per_trade_pct": 0.01,
        "max_daily_losses": 3,
    },
    "execution": {
        "taker_fee_pct": 0.00055,
        "slippage_pct": 0.0003,
        "max_bars_in_trade": 100,
    },
    "detector": {
        "atr_period": 14,
        "rsi_period": 14,
        "ema_period": 20,
        "swing_lookback": 50,
        "impulse_min_pct": 0.01,
        "impulse_min_atr": 3.0,
        "impulse_lookback": 6,
        "stretch_atr_mult": 2.5,
        "rsi_overbought": 75,
        "rsi_oversold": 25,
        "level_tolerance_pct": 0.003,
        "stop_buffer_atr": 0.2,
        "fib_target": 0.5,
        "fib_partial": 0.382,
        "min_rr": 1.2,
        "enable_long": True,
        "enable_short": True,
    },
    "storage": {"db_path": "data/test.sqlite", "csv_export_path": "data/test.csv"},
    "health": {"host": "0.0.0.0", "port": 8000},
}


@pytest.fixture
def cfg():
    return copy.deepcopy(BASE_CFG)


def make_candle(ts, o, h, l, c, v=1.0):
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)
