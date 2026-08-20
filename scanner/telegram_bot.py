"""Telegram alerts + control commands.

Secrets come from env ONLY:  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
If TELEGRAM_TOKEN is unset the controller degrades to a logging no-op so the
scanner still runs (useful for local dev / CI).

Commands: /status /stats /pause /resume /export
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .models import Signal, Side
from . import stats as stats_mod

log = logging.getLogger("telegram")


# --------------------------------------------------------------------------- #
#  Pure formatter (unit-testable, no network)
# --------------------------------------------------------------------------- #
def build_market_url(exchange: str, symbol: str) -> str:
    """Direct link to the perpetual's trading page on the exchange.
    'BTC/USDT:USDT' -> MEXC 'BTC_USDT', Bybit 'BTCUSDT'."""
    pair = symbol.split(":")[0]          # 'BTC/USDT:USDT' -> 'BTC/USDT'
    ex = exchange.lower()
    if ex == "mexc":
        return f"https://futures.mexc.com/exchange/{pair.replace('/', '_')}"
    if ex == "bybit":
        return f"https://www.bybit.com/trade/usdt/{pair.replace('/', '')}"
    return f"https://www.tradingview.com/chart/?symbol={pair.replace('/', '')}"


def build_tradingview_url(exchange: str, symbol: str) -> str:
    """TradingView chart for the perpetual (…USDT.P), prefixed by the exchange."""
    pair = symbol.split(":")[0].replace("/", "")     # 'BTC/USDT:USDT' -> 'BTCUSDT'
    ex = "MEXC" if exchange.lower() == "mexc" else \
         "BYBIT" if exchange.lower() == "bybit" else exchange.upper()
    return f"https://www.tradingview.com/chart/?symbol={ex}:{pair}.P"


def build_dex_url(symbol: str) -> str:
    """Dexscreener search by the base token (no on-chain address needed)."""
    base = symbol.split("/")[0]                       # 'COW/USDT:USDT' -> 'COW'
    return f"https://dexscreener.com/search?q={base}"


def _fmt(price: float) -> str:
    """Price formatting that copes with both BTC (64,230) and memecoins (0.00001234)."""
    ap = abs(price)
    if ap >= 100:
        return f"{price:,.2f}"
    if ap >= 1:
        return f"{price:,.4f}"
    if ap >= 0.01:
        return f"{price:.6f}"
    return f"{price:.8f}"


def _fmt_vol(v: float) -> str:
    """Turnover as $2.2M / $958K / $12.3K."""
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def format_alert(sig: Signal) -> str:
    arrow = "🔴 SHORT" if sig.side is Side.SHORT else "🟢 LONG"
    disp_symbol = sig.symbol.split(":")[0]   # 'BTC/USDT:USDT' -> 'BTC/USDT'
    price = sig.signal_close
    level = sig.swing_level
    pullback_off = abs(price - level) / level * 100 if level else 0.0
    stop_pct = abs(sig.stop_price - price) / price * 100 if price else 0.0
    tgt_pct = abs(sig.target_price - price) / price * 100 if price else 0.0
    fib = "0.5"
    text = (
        f"⚡ {arrow} — {disp_symbol} ({sig.exchange}, {sig.timeframe})\n"
        f"Price: {_fmt(price)} | RSI {sig.rsi_at_signal:.0f}\n"
        f"Спайк: {sig.impulse_pct*100:.1f}% | растяжение {sig.stretch_atr:.1f}×ATR\n"
        f"Пик {_fmt(level)} | откат {pullback_off:.1f}%\n"
        f"Stop: {_fmt(sig.stop_price)} ({stop_pct:.2f}%) | "
        f"Target({fib}): {_fmt(sig.target_price)} ({tgt_pct:.2f}%)\n"
        f"R:R ≈ {sig.rr:.1f}"
    )
    ctx = []
    if sig.spot_price:
        spot_off = (price - sig.spot_price) / sig.spot_price * 100
        ctx.append(f"Спот: {_fmt(sig.spot_price)} ({spot_off:+.2f}%)")
    if sig.volume_24h:
        ctx.append(f"Vol24h: {_fmt_vol(sig.volume_24h)}")
    if ctx:
        text += "\n" + " | ".join(ctx)
    return text


# --------------------------------------------------------------------------- #
#  Controller
# --------------------------------------------------------------------------- #
class TelegramController:
    def __init__(self, state):
        self.state = state
        self.token = os.environ.get("TELEGRAM_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)
        self.app = None  # python-telegram-bot Application

    async def start(self) -> None:
        if not self.enabled:
            log.warning("TELEGRAM_TOKEN/CHAT_ID not set — Telegram disabled "
                        "(alerts will be logged only).")
            return
        # imported lazily so the package works without the dep in some CI paths
        from telegram.ext import ApplicationBuilder, CommandHandler

        self.app = ApplicationBuilder().token(self.token).build()
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("export", self._cmd_export))
        self.app.add_handler(CommandHandler("cleanup", self._cmd_cleanup))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram controller started.")

    async def stop(self) -> None:
        if self.app:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("error stopping telegram app")

    # ------------------------------------------------------------------ #
    async def send_alert(self, sig: Signal) -> None:
        text = format_alert(sig)
        ex_url = build_market_url(sig.exchange, sig.symbol)
        tv_url = build_tradingview_url(sig.exchange, sig.symbol)
        dex_url = build_dex_url(sig.symbol)
        if not self.enabled or not self.app:
            log.info("[ALERT]\n%s\n%s | %s | %s", text, ex_url, tv_url, dex_url)
            return
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"📈 {sig.exchange.upper()}", url=ex_url),
                InlineKeyboardButton("📊 TradingView", url=tv_url),
                InlineKeyboardButton("🔍 DEX", url=dex_url),
            ]])
            await self.app.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=kb)
        except Exception:  # noqa: BLE001
            log.exception("failed to send telegram alert")

    async def send_text(self, text: str) -> None:
        if not self.enabled or not self.app:
            log.info("[MSG] %s", text)
            return
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text)
        except Exception:  # noqa: BLE001
            log.exception("failed to send telegram message")

    # --- command handlers --------------------------------------------- #
    def _authorized(self, update) -> bool:
        # only respond in the configured chat
        return str(update.effective_chat.id) == str(self.chat_id)

    async def _cmd_status(self, update, _ctx) -> None:
        if not self._authorized(update):
            return
        import time
        s = self.state
        up = int(time.time() - s.started_at)
        await update.message.reply_text(
            f"🟢 running | uptime {up//3600}h{(up%3600)//60}m\n"
            f"paused: {s.paused}\n"
            f"markets scanned: {s.market_count()}\n"
            f"open paper trades: {s.executor.open_trade_count()}"
        )

    async def _cmd_stats(self, update, _ctx) -> None:
        if not self._authorized(update):
            return
        rep = stats_mod.compute_stats(self.state.journal.all_trades())
        await update.message.reply_text(rep.format_text())

    async def _cmd_pause(self, update, _ctx) -> None:
        if not self._authorized(update):
            return
        self.state.paused = True
        await update.message.reply_text("⏸ scanning paused (open trades keep tracking).")

    async def _cmd_resume(self, update, _ctx) -> None:
        if not self._authorized(update):
            return
        self.state.paused = False
        await update.message.reply_text("▶️ scanning resumed.")

    async def _cmd_cleanup(self, update, _ctx) -> None:
        if not self._authorized(update):
            return
        keep = self.state.cfg["timeframes"]
        n = self.state.journal.delete_trades_not_in(keep)
        await update.message.reply_text(
            f"🧹 Удалено {n} старых сделок с чужими ТФ. В журнале осталось только {keep}."
        )

    async def _cmd_export(self, update, _ctx) -> None:
        if not self._authorized(update):
            return
        path = self.state.journal.export_csv(self.state.cfg["storage"]["csv_export_path"])
        try:
            with open(path, "rb") as fh:
                await update.message.reply_document(document=fh, filename="trades_export.csv")
        except Exception:  # noqa: BLE001
            await update.message.reply_text(f"exported to {path} (could not attach file)")
