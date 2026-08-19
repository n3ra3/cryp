"""Orchestration entry point.

Pipeline per market, once per poll interval:
    fetch closed candles -> for each NEW closed candle:
        1) advance/resolve open paper trades (fills pending entries at bar open)
        2) run detector on the window ending at that candle
        3) on a signal: queue a pending entry + send an alert

Runs the Telegram controller and the health server as concurrent asyncio tasks.
NO trading. Public market data only.
"""

from __future__ import annotations

import asyncio
import logging
import signal as os_signal
import time

from scanner.config import load_config
from scanner.datafeed import DataFeed, next_boundary_ms
from scanner.indicators import compute_indicators
from scanner.detector import detect
from scanner.journal import Journal
from scanner.paper_exec import PaperExecutor
from scanner.telegram_bot import TelegramController
from scanner.health import HealthServer
from scanner import stats as stats_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


class AppState:
    """Shared, mutable app state referenced by health + telegram + main loop."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.started_at = time.time()
        self.paused = False
        self.journal: Journal | None = None
        self.executor: PaperExecutor | None = None
        self.telegram: TelegramController | None = None
        self.last_ts: dict[tuple, int] = {}   # (exchange,symbol,tf) -> last closed ts
        self.markets: dict[str, list[str]] = {}  # exchange -> [symbols] currently scanned

    def market_count(self) -> int:
        return sum(len(v) for v in self.markets.values())


async def scan_market(state: AppState, feed: DataFeed, exchange: str, symbol: str) -> None:
    cfg = state.cfg
    det_cfg = cfg["detector"]
    tf = cfg["timeframe"]
    executor = state.executor
    key = (exchange, symbol, tf)

    candles = await feed.fetch_closed(exchange, symbol)
    need = max(det_cfg["atr_period"], det_cfg["rsi_period"],
               det_cfg["ema_period"], det_cfg["swing_lookback"]) + 2
    if len(candles) < need:
        return

    last = state.last_ts.get(key)

    # Warm start: record position in the stream, detect once on the latest bar,
    # but do NOT replay history as live paper trades.
    if last is None:
        state.last_ts[key] = candles[-1].ts
        if not state.paused:
            await _maybe_detect(state, candles, exchange, symbol, tf)
        return

    for idx, c in enumerate(candles):
        if c.ts <= last:
            continue
        # 1) always advance open trades / fill pending entries
        closed_now = executor.on_new_candle(exchange, symbol, tf, c)
        for t in closed_now:
            log.info("CLOSED %s %s %s exit=%s R=%.2f bars=%d",
                     exchange, symbol, t.side.value, t.exit_reason.value,
                     t.r_multiple, t.bars_held)
        # 2) detect (unless paused, and only if this market has no position)
        if not state.paused and not executor.has_position(key):
            await _maybe_detect(state, candles[: idx + 1], exchange, symbol, tf)

    state.last_ts[key] = candles[-1].ts


async def _maybe_detect(state: AppState, window, exchange: str, symbol: str, tf: str) -> None:
    cfg = state.cfg
    ind = compute_indicators(window, cfg["detector"])
    if ind is None:
        return
    ind["_meta"] = {"exchange": exchange, "symbol": symbol, "timeframe": tf}
    sig = detect(window, ind, cfg)
    if sig is None:
        return
    state.executor.register_signal(sig)
    log.info("SIGNAL %s %s %s rr=%.2f stop=%.4f target=%.4f",
             exchange, symbol, sig.side.value, sig.rr, sig.stop_price, sig.target_price)
    if state.telegram:
        await state.telegram.send_alert(sig)


async def resolve_universe(state: AppState, feed: DataFeed) -> None:
    """Populate state.markets from config: auto-discovery or the manual list."""
    cfg = state.cfg
    mode = cfg.get("symbols_mode", "manual")
    markets: dict[str, list[str]] = {}
    for exchange in cfg["exchanges"]:
        if mode == "auto":
            syms = await feed.discover_symbols(exchange, cfg["universe"])
            if not syms:
                syms = cfg.get("symbols") or []
                log.warning("%s: auto universe empty — falling back to %d manual symbols",
                            exchange, len(syms))
        else:
            syms = cfg.get("symbols") or []
        markets[exchange] = syms
    state.markets = markets
    log.info("scanning %d markets across %d exchange(s)",
             state.market_count(), len(markets))


async def universe_refresh(state: AppState, feed: DataFeed) -> None:
    """Periodically re-discover the universe (new listings / volume shifts)."""
    cfg = state.cfg
    if cfg.get("symbols_mode") != "auto":
        return
    hours = cfg["universe"].get("refresh_hours", 12)
    while True:
        await asyncio.sleep(max(1, hours) * 3600)
        try:
            await resolve_universe(state, feed)
        except Exception:  # noqa: BLE001
            log.exception("universe refresh failed")


async def _scan_one(state: AppState, feed: DataFeed, sem: asyncio.Semaphore,
                    exchange: str, symbol: str) -> None:
    async with sem:
        try:
            await scan_market(state, feed, exchange, symbol)
        except Exception:  # noqa: BLE001
            log.exception("scan error for %s %s", exchange, symbol)


async def scan_loop(state: AppState, feed: DataFeed) -> None:
    cfg = state.cfg
    scan_cfg = cfg.get("scan", {})
    align = scan_cfg.get("align_to_candle", True)
    lag_sec = scan_cfg.get("boundary_lag_sec", 5)
    interval = scan_cfg.get("poll_interval_sec", 60)
    sem = asyncio.Semaphore(scan_cfg.get("concurrency", 8))
    tf = cfg["timeframe"]

    while True:
        t0 = time.time()
        tasks = [
            _scan_one(state, feed, sem, ex, sym)
            for ex, syms in state.markets.items()
            for sym in syms
        ]
        if tasks:
            await asyncio.gather(*tasks)

        if align:
            # sleep until just after the next candle close, then do one pass
            now_ms = int(time.time() * 1000)
            wake_ms = next_boundary_ms(tf, now_ms) + int(lag_sec * 1000)
            delay = max(1.0, (wake_ms - now_ms) / 1000.0)
        else:
            delay = max(1.0, interval - (time.time() - t0))
        await asyncio.sleep(delay)


async def daily_report(state: AppState) -> None:
    """Send a stats summary once per day."""
    while True:
        await asyncio.sleep(86_400)
        try:
            rep = stats_mod.compute_stats(state.journal.all_trades())
            if state.telegram:
                await state.telegram.send_text(rep.format_text())
            log.info("daily report:\n%s", rep.format_text())
        except Exception:  # noqa: BLE001
            log.exception("daily report failed")


async def run() -> None:
    cfg = load_config()
    state = AppState(cfg)

    journal = Journal(cfg["storage"]["db_path"])
    state.journal = journal

    executor = PaperExecutor(cfg, journal)
    executor.restore()   # re-load OPEN trades so they survive restarts
    state.executor = executor
    log.info("restored %d open paper trades from journal", executor.open_trade_count())

    feed = DataFeed(cfg["exchanges"], cfg)
    await resolve_universe(state, feed)   # auto-discover the universe (or manual list)

    telegram = TelegramController(state)
    state.telegram = telegram
    await telegram.start()

    health = HealthServer(cfg, state)
    await health.start()
    log.info("health server on :%s", cfg["health"]["port"])

    stop_event = asyncio.Event()

    def _shutdown(*_a):
        log.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(os_signal, sig_name):
            try:
                loop.add_signal_handler(getattr(os_signal, sig_name), _shutdown)
            except NotImplementedError:
                # Windows: add_signal_handler unsupported; rely on KeyboardInterrupt
                pass

    tasks = [
        asyncio.create_task(scan_loop(state, feed), name="scan_loop"),
        asyncio.create_task(daily_report(state), name="daily_report"),
        asyncio.create_task(universe_refresh(state, feed), name="universe_refresh"),
    ]

    await stop_event.wait()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await telegram.stop()
    await health.stop()
    await feed.close()
    journal.close()
    log.info("bye.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
