"""Alert formatting + exchange deep-link building."""

from scanner.telegram_bot import build_market_url, format_alert
from scanner.models import Signal, Side


def test_mexc_futures_url():
    assert build_market_url("mexc", "BTC/USDT:USDT") == \
        "https://futures.mexc.com/exchange/BTC_USDT"


def test_bybit_futures_url():
    assert build_market_url("bybit", "ETH/USDT:USDT") == \
        "https://www.bybit.com/trade/usdt/ETHUSDT"


def test_alert_contains_key_fields():
    sig = Signal(
        exchange="mexc", symbol="COW/USDT:USDT", timeframe="4h", side=Side.SHORT,
        signal_ts=0, signal_close=0.1495, stop_price=0.1627, target_price=0.1313,
        partial_target=0.14, swing_level=0.1600, swing_origin=0.099,
        rsi_at_signal=72, atr_at_signal=0.004, stretch_atr=3.1, rr=1.4,
        impulse_pct=0.61,
    )
    text = format_alert(sig)
    assert "SHORT" in text and "COW/USDT" in text and "4h" in text
    assert "61" in text          # spike %
