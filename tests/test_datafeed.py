"""Datafeed tests: closed-candle filtering and reconnect-with-backoff."""

import ccxt.async_support as ccxt

from scanner.datafeed import DataFeed, only_closed, timeframe_ms


def test_only_closed_drops_forming_bar():
    tf = "15m"
    step = timeframe_ms(tf)  # 900_000
    rows = [
        [0, 100, 101, 99, 100, 1],
        [step, 100, 101, 99, 100, 1],
        [2 * step, 100, 101, 99, 100, 1],   # forming
    ]
    # now is 1ms before the 3rd bar would close
    now = 3 * step - 1
    closed = only_closed(rows, tf, now_ms=now)
    assert len(closed) == 2
    assert closed[-1].ts == step


async def test_reconnect_backoff_then_success(cfg):
    """fetch_closed retries transient NetworkErrors and eventually returns the
    candles once — no duplicates, no lost data."""
    feed = DataFeed(["bybit"], cfg)

    calls = {"n": 0}
    step = timeframe_ms(cfg["timeframe"])
    good_rows = [
        [0, 100, 101, 99, 100, 1],
        [step, 100, 101, 99, 100, 1],
    ]

    class FakeExchange:
        async def fetch_ohlcv(self, symbol, timeframe, limit):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ccxt.NetworkError("simulated drop")
            return good_rows

        async def close(self):
            pass

    feed.exchanges["bybit"] = FakeExchange()

    # now well past both bars so both are 'closed'
    candles = await feed.fetch_closed("bybit", "BTC/USDT")
    await feed.close()

    assert calls["n"] == 3            # failed twice, succeeded on the third try
    assert len(candles) == 2          # both closed candles returned exactly once
    assert [c.ts for c in candles] == [0, step]


async def test_bad_symbol_returns_empty(cfg):
    feed = DataFeed(["bybit"], cfg)

    class FakeExchange:
        async def fetch_ohlcv(self, symbol, timeframe, limit):
            raise ccxt.BadSymbol("nope")

        async def close(self):
            pass

    feed.exchanges["bybit"] = FakeExchange()
    candles = await feed.fetch_closed("bybit", "NOPE/USDT")
    await feed.close()
    assert candles == []
