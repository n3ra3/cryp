"""Market-data feed via ccxt (async). PUBLIC endpoints only.

Guarantees:
  * Returns ONLY closed candles — the still-forming bar is dropped based on
    wall-clock time vs. the candle open time + timeframe duration.
  * Reconnects with exponential backoff on transient errors.
  * No API keys are ever passed to ccxt (public data needs none).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import ccxt.async_support as ccxt

from .models import Candle

log = logging.getLogger("datafeed")


# timeframe string -> milliseconds
_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}


def timeframe_ms(tf: str) -> int:
    if tf not in _TF_MS:
        raise ValueError(f"unsupported timeframe: {tf}")
    return _TF_MS[tf]


def next_boundary_ms(tf: str, now_ms: int) -> int:
    """Wall-clock ms of the next candle close for this timeframe."""
    step = timeframe_ms(tf)
    return ((now_ms // step) + 1) * step


def filter_universe(markets: dict, tickers: dict, uni_cfg: dict) -> list[str]:
    """Pure selector: given ccxt markets + tickers, return the symbols to scan.

    Kept side-effect-free so it is unit-testable without network access.
    """
    quote = uni_cfg.get("quote", "USDT")
    mtype = uni_cfg.get("market_type", "spot")
    blacklist = {b.upper() for b in uni_cfg.get("blacklist", [])}
    blacklist_contains = [s.upper() for s in uni_cfg.get("blacklist_contains", [])]
    min_vol = uni_cfg.get("min_quote_volume", 0) or 0
    top_n = uni_cfg.get("top_n")

    def turnover(sym: str) -> float:
        t = tickers.get(sym) or {}
        v = t.get("quoteVolume")
        return float(v) if v is not None else 0.0

    picked: list[str] = []
    for sym, m in markets.items():
        if not m.get("active", True):
            continue
        if m.get("quote") != quote:
            continue
        if mtype == "spot" and not m.get("spot", False):
            continue
        if mtype == "swap" and not m.get("swap", False):
            continue
        base = (m.get("base") or "").upper()
        if base in blacklist or sym.upper() in blacklist:
            continue
        if any(sub in base for sub in blacklist_contains):
            continue
        if turnover(sym) < min_vol:
            continue
        picked.append(sym)

    picked.sort(key=turnover, reverse=True)
    if top_n:
        picked = picked[: int(top_n)]
    return picked


def only_closed(rows: list, tf: str, now_ms: Optional[int] = None) -> list[Candle]:
    """Convert ccxt OHLCV rows to Candles, keeping only fully-closed bars.

    A candle that opened at `ts` closes at `ts + tf_ms`. It is closed iff
    now >= ts + tf_ms.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    tf_ms = timeframe_ms(tf)
    out = []
    for row in rows:
        candle = Candle.from_ccxt(row)
        if candle.ts + tf_ms <= now_ms:
            out.append(candle)
    return out


class DataFeed:
    """Wraps one or more ccxt exchanges for public OHLCV polling."""

    def __init__(self, exchange_ids: list[str], cfg: dict):
        self.cfg = cfg
        self.timeframe = cfg["timeframe"]
        self.limit = cfg.get("candle_fetch_limit", 200)
        bo = cfg.get("backoff", {})
        self.bo_base = bo.get("base_sec", 2)
        self.bo_max = bo.get("max_sec", 120)
        self.bo_factor = bo.get("factor", 2.0)

        self.exchanges: dict[str, "ccxt.Exchange"] = {}
        for eid in exchange_ids:
            klass = getattr(ccxt, eid)
            # enableRateLimit keeps us within public limits; NO keys supplied.
            self.exchanges[eid] = klass({"enableRateLimit": True})

    async def close(self) -> None:
        for ex in self.exchanges.values():
            try:
                await ex.close()
            except Exception:  # noqa: BLE001
                pass

    def normalize_symbol(self, symbol: str) -> str:
        # ccxt unified symbols are already 'BASE/QUOTE'; keep a hook for overrides.
        return symbol.strip().upper()

    async def discover_symbols(self, exchange_id: str, uni_cfg: dict) -> list[str]:
        """Auto-select the liquid universe for one exchange (public data only).

        Loads markets + tickers, then applies `filter_universe`. On any failure
        it returns [] and logs — the caller can fall back to a manual list.
        """
        ex = self.exchanges.get(exchange_id)
        if ex is None:
            log.error("unknown exchange id: %s", exchange_id)
            return []
        mtype = uni_cfg.get("market_type", "spot")
        try:
            markets = await ex.load_markets()
            # Some exchanges (e.g. Bybit) default fetch_tickers to futures, so the
            # spot pairs come back with no volume. Ask for the right type explicitly.
            try:
                tickers = await ex.fetch_tickers(params={"type": mtype})
            except Exception:  # noqa: BLE001
                tickers = await ex.fetch_tickers()
        except Exception as e:  # noqa: BLE001
            log.exception("%s: universe discovery failed: %s", exchange_id, e)
            return []
        syms = filter_universe(markets, tickers, uni_cfg)
        log.info("%s: universe = %d symbols (quote=%s, type=%s, top_n=%s)",
                 exchange_id, len(syms), uni_cfg.get("quote"),
                 uni_cfg.get("market_type"), uni_cfg.get("top_n"))
        return syms

    async def fetch_closed(self, exchange_id: str, symbol: str,
                           timeframe: str | None = None) -> list[Candle]:
        """Fetch closed candles for one market/timeframe, retrying with backoff.

        Returns [] only if the exchange object is missing; otherwise it keeps
        retrying transient failures (the caller polls again next cycle).
        """
        ex = self.exchanges.get(exchange_id)
        if ex is None:
            log.error("unknown exchange id: %s", exchange_id)
            return []

        tf = timeframe or self.timeframe
        sym = self.normalize_symbol(symbol)
        delay = self.bo_base
        attempt = 0
        while True:
            try:
                rows = await ex.fetch_ohlcv(sym, timeframe=tf, limit=self.limit)
                candles = only_closed(rows, tf)
                if attempt:
                    log.info("%s %s recovered after %d retries", exchange_id, sym, attempt)
                return candles
            except ccxt.BadSymbol:
                log.error("%s: bad symbol %s — skipping", exchange_id, sym)
                return []
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable,
                    ccxt.RequestTimeout, ccxt.DDoSProtection) as e:
                attempt += 1
                log.warning("%s %s transient error (attempt %d): %s; backoff %ss",
                            exchange_id, sym, attempt, type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * self.bo_factor, self.bo_max)
            except Exception as e:  # noqa: BLE001
                attempt += 1
                log.exception("%s %s unexpected error (attempt %d): %s; backoff %ss",
                              exchange_id, sym, attempt, e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * self.bo_factor, self.bo_max)
