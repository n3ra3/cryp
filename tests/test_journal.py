"""Journal persistence: open trades survive a 'restart' and can be re-driven."""

import os

from scanner.journal import Journal
from scanner.paper_exec import PaperExecutor, build_trade
from scanner.models import Signal, Side, ExitReason
from conftest import make_candle


def _short_signal():
    return Signal(
        exchange="bybit", symbol="BTC/USDT", timeframe="15m", side=Side.SHORT,
        signal_ts=0, signal_close=100.0,
        stop_price=102.0, target_price=98.0, partial_target=99.0,
        swing_level=101.5, swing_origin=90.0,
        rsi_at_signal=78.0, atr_at_signal=1.0, stretch_atr=3.0, rr=2.0,
    )


def test_open_trade_survives_restart(tmp_path, cfg):
    db = os.path.join(tmp_path, "j.sqlite")

    # --- session 1: open a trade, do NOT close it ---
    j1 = Journal(db)
    ex1 = PaperExecutor(cfg, j1)
    ex1.register_signal(_short_signal())
    ex1.on_new_candle("bybit", "BTC/USDT", "15m", make_candle(1, 100, 100.4, 99.8, 100.0))
    assert ex1.open_trade_count() == 1
    j1.close()

    # --- session 2: fresh objects, reload from disk ---
    j2 = Journal(db)
    ex2 = PaperExecutor(cfg, j2)
    ex2.restore()
    assert ex2.open_trade_count() == 1

    # drive it forward to a stop and confirm it persists as closed
    closed = ex2.on_new_candle("bybit", "BTC/USDT", "15m",
                               make_candle(2, 100, 102.5, 99.9, 100.0))
    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.STOP

    j3 = Journal(db)
    assert len(j3.load_open_trades()) == 0     # nothing left open
    assert len(j3.all_trades(closed_only=True)) == 1
    j3.close()


def test_delete_trades_not_in(tmp_path, cfg):
    db = os.path.join(tmp_path, "j.sqlite")
    j = Journal(db)
    for tf in ["5m", "15m", "1h", "4h"]:
        t = build_trade(_short_signal(), 100.0, cfg)
        t.timeframe = tf
        j.insert_trade(t)
    removed = j.delete_trades_not_in(["1h", "4h"])
    assert removed == 2                       # 5m + 15m deleted
    left = {t.timeframe for t in j.all_trades()}
    assert left == {"1h", "4h"}
    j.close()


def test_csv_export(tmp_path, cfg):
    db = os.path.join(tmp_path, "j.sqlite")
    j = Journal(db)
    t = build_trade(_short_signal(), 100.0, cfg)
    j.insert_trade(t)
    out = os.path.join(tmp_path, "export.csv")
    j.export_csv(out)
    assert os.path.exists(out)
    with open(out, encoding="utf-8") as fh:
        header = fh.readline()
    assert "r_multiple" in header and "raw_signal_json" in header
    j.close()
