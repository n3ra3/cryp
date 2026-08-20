"""Historical backtest of the detector over MEXC perpetuals.

Walks the SAME detector forward over historical candles, one position per market
(like live), simulates each trade with fees+slippage, and aggregates the result
with the same stats module used in production.

Usage (from repo root):
    python -m scripts.backtest                 # discover universe, both timeframes
    python -m scripts.backtest --symbols COW/USDT:USDT --tf 5m --bars 1000
    python -m scripts.backtest --top 40 --bars 1000

Public data only. Nothing is written; this is read-only research.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import ccxt.async_support as ccxt

from scanner.config import load_config
from scanner.datafeed import only_closed
from scanner.indicators import compute_indicators
from scanner.detector import detect
from scanner.paper_exec import simulate_trade
from scanner.stats import compute_stats
from scanner.datafeed import filter_universe

logging.basicConfig(level=logging.ERROR)


async def fetch_history(ex, symbol: str, tf: str, bars: int) -> list:
    """Fetch up to `bars` candles, paginating backwards if needed."""
    out: list = []
    limit = min(bars, 1000)
    since = None
    # simplest: one page (MEXC returns up to ~1000). Paginate for more.
    remaining = bars
    cursor = None
    all_rows: list = []
    # page backwards using the 'since' of the earliest fetched
    rows = await ex.fetch_ohlcv(symbol, tf, limit=limit)
    all_rows = rows
    while len(all_rows) < bars and rows:
        earliest = rows[0][0]
        step = (rows[1][0] - rows[0][0]) if len(rows) > 1 else 60_000
        since = earliest - step * limit
        rows = await ex.fetch_ohlcv(symbol, tf, since=since, limit=limit)
        if not rows or rows[0][0] >= all_rows[0][0]:
            break
        all_rows = rows + all_rows
    return only_closed(all_rows, tf)


def backtest_market(candles: list, cfg: dict, tf: str, symbol: str) -> list:
    """Walk-forward, one position at a time. Returns closed Trades."""
    det = cfg["detector"]
    need = max(det["atr_period"], det["rsi_period"], det["ema_period"],
               det["swing_lookback"]) + 2
    trades: list = []
    open_until = -1
    for i in range(need, len(candles)):
        if i <= open_until:
            continue
        window = candles[: i + 1]
        ind = compute_indicators(window, det)
        if ind is None:
            continue
        ind["_meta"] = {"exchange": "MEXC", "symbol": symbol, "timeframe": tf}
        sig = detect(window, ind, cfg)
        if sig is None:
            continue
        forward = candles[i + 1:]
        if not forward:
            break
        tr = simulate_trade(sig, forward, cfg)
        trades.append(tr)
        # entry at bar i+1, held tr.bars_held bars -> exit at i + bars_held
        open_until = i + tr.bars_held
    return trades


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", help="explicit symbols; default = discover universe")
    ap.add_argument("--tf", nargs="*", help="timeframes; default = config timeframes")
    ap.add_argument("--bars", type=int, default=1000, help="candles of history per market/tf")
    ap.add_argument("--top", type=int, help="cap universe to top-N by turnover")
    args = ap.parse_args()

    cfg = load_config()
    tfs = args.tf or cfg["timeframes"]
    ex = ccxt.mexc({"enableRateLimit": True})
    await ex.load_markets()

    if args.symbols:
        symbols = args.symbols
    else:
        tickers = await ex.fetch_tickers(params={"type": cfg["universe"]["market_type"]})
        uni = dict(cfg["universe"])
        if args.top:
            uni["top_n"] = args.top
        symbols = filter_universe(ex.markets, tickers, uni)

    print(f"Backtest: {len(symbols)} markets x {tfs}, {args.bars} bars each\n")

    all_trades = []
    sem = asyncio.Semaphore(6)

    async def one(sym, tf):
        async with sem:
            try:
                candles = await fetch_history(ex, sym, tf, args.bars)
            except Exception as e:  # noqa: BLE001
                print(f"  {sym} {tf}: fetch error {e}")
                return
            if len(candles) < 60:
                return
            trades = backtest_market(candles, cfg, tf, sym)
            all_trades.extend(trades)

    await asyncio.gather(*[one(s, tf) for s in symbols for tf in tfs])
    await ex.close()

    rep = compute_stats(all_trades)
    print("=" * 56)
    print(rep.format_text())
    print("=" * 56)

    # a few sample trades (best & worst)
    closed = [t for t in all_trades if t.r_multiple is not None]
    closed.sort(key=lambda t: t.r_multiple)
    if closed:
        print("\nWorst 3:")
        for t in closed[:3]:
            print(f"  {t.symbol.split(':')[0]:<14}{t.timeframe:>4} {t.side.value:<5} "
                  f"{t.exit_reason.value:<7} {t.r_multiple:+.2f}R")
        print("Best 3:")
        for t in closed[-3:][::-1]:
            print(f"  {t.symbol.split(':')[0]:<14}{t.timeframe:>4} {t.side.value:<5} "
                  f"{t.exit_reason.value:<7} {t.r_multiple:+.2f}R")


if __name__ == "__main__":
    asyncio.run(main())
