"""Detector tests: a clean SHORT setup, determinism, and NO-LOOKAHEAD."""

from scanner.detector import detect
from scanner.indicators import compute_indicators
from scanner.models import Side
from conftest import make_candle


def _with_meta(window, cfg):
    ind = compute_indicators(window, cfg["detector"])
    if ind is None:
        return None
    ind["_meta"] = {"exchange": "bybit", "symbol": "BTC/USDT", "timeframe": "15m"}
    return ind


def build_short_setup():
    """50 flat bars, a sharp up-impulse into a swing high at 110, then a small
    bearish rejection bar that closes just under the 110 level (within 0.3%)."""
    candles = [make_candle(i, 100, 100.5, 99.5, 100) for i in range(50)]
    candles += [
        make_candle(50, 100, 101.2, 100.0, 101),
        make_candle(51, 101, 103.2, 100.8, 103),
        make_candle(52, 103, 106.2, 102.8, 106),
        make_candle(53, 106, 110.0, 105.8, 109),      # spike high = 110
        make_candle(54, 109.9, 109.95, 109.6, 109.7),  # bearish rejection @ level
    ]
    return candles


def test_short_setup_fires():
    cfg_local = _cfg()
    candles = build_short_setup()
    ind = _with_meta(candles, cfg_local)
    sig = detect(candles, ind, cfg_local)
    assert sig is not None
    assert sig.side is Side.SHORT
    assert sig.stop_price > sig.signal_close        # stop above for a short
    assert sig.target_price < sig.signal_close      # target below
    assert sig.rr >= cfg_local["detector"]["min_rr"]


def test_deterministic():
    cfg_local = _cfg()
    candles = build_short_setup()
    ind1 = _with_meta(candles, cfg_local)
    ind2 = _with_meta([c for c in candles], cfg_local)
    s1 = detect(candles, ind1, cfg_local)
    s2 = detect(candles, ind2, cfg_local)
    assert s1 is not None and s2 is not None
    assert s1.to_raw_json() == s2.to_raw_json()


def test_no_lookahead_signal_needs_the_confirmation_bar():
    """Without the final (confirmation) bar the setup must NOT fire — proving
    the detector only reacts once a bar has CLOSED, never on the forming bar."""
    cfg_local = _cfg()
    candles = build_short_setup()

    # drop the confirmation bar -> last bar is the bullish spike, no rejection
    truncated = candles[:-1]
    ind = _with_meta(truncated, cfg_local)
    sig = detect(truncated, ind, cfg_local) if ind else None
    assert sig is None


def test_weak_spike_is_rejected():
    """A small, gentle rise into the level must NOT fire — the impulse filter
    demands a real spike (>= impulse_min_pct AND >= impulse_min_atr)."""
    cfg_local = _cfg()
    # 50 flat bars, then a tiny drift up to ~100.4 and a doji-ish 'rejection'.
    candles = [make_candle(i, 100, 100.05, 99.95, 100) for i in range(50)]
    candles += [
        make_candle(50, 100.0, 100.15, 99.98, 100.1),
        make_candle(51, 100.1, 100.25, 100.05, 100.2),
        make_candle(52, 100.2, 100.40, 100.15, 100.35),  # swing high ~100.4
        make_candle(53, 100.34, 100.39, 100.30, 100.33),  # micro rejection
    ]
    ind = _with_meta(candles, cfg_local)
    sig = detect(candles, ind, cfg_local) if ind else None
    assert sig is None   # move is < 1% and < 3xATR -> not a spike


def test_stale_spike_is_rejected():
    """A big spike that happened long ago (outside impulse_lookback) must NOT
    fire, even if price is still near the old high."""
    cfg_local = _cfg()
    cfg_local["detector"]["impulse_lookback"] = 3
    candles = build_short_setup()
    # append several quiet bars so the spike is now 'stale' (far in the past)
    base_ts = candles[-1].ts
    for k in range(1, 6):
        candles.append(make_candle(base_ts + k, 109.7, 109.8, 109.6, 109.7))
    ind = _with_meta(candles, cfg_local)
    sig = detect(candles, ind, cfg_local) if ind else None
    assert sig is None


def test_pullback_too_far_is_rejected():
    """If price has already fallen far below the peak (retrace almost done),
    it's too late to fade — must NOT fire."""
    cfg_local = _cfg()
    cfg_local["detector"]["pullback_max_pct"] = 0.05   # 5% window
    candles = build_short_setup()
    # replace the confirmation bar with one that closed ~9% below the 110 high
    candles[-1] = make_candle(54, 109.9, 109.95, 99.5, 100.0)  # deep drop, close 100
    ind = _with_meta(candles, cfg_local)
    sig = detect(candles, ind, cfg_local) if ind else None
    assert sig is None


def test_adx_filter_blocks_strong_trend():
    """The synthetic short setup is a sharp pump -> high ADX. With max_adx set
    low, the regime filter must reject it; without max_adx it still fires."""
    from scanner.indicators import compute_indicators, adx

    candles = build_short_setup()
    trend_adx = adx(candles, 14)
    assert trend_adx is not None and trend_adx >= 20   # the pump is a strong trend

    cfg_no_filter = _cfg()                    # no max_adx -> fires
    ind = _with_meta(candles, cfg_no_filter)
    assert detect(candles, ind, cfg_no_filter) is not None

    cfg_filtered = _cfg()
    cfg_filtered["detector"]["max_adx"] = 20   # range-only -> blocked
    ind2 = _with_meta(candles, cfg_filtered)
    assert detect(candles, ind2, cfg_filtered) is None


def test_flat_market_no_false_signal():
    cfg_local = _cfg()
    candles = [make_candle(i, 100, 100.5, 99.5, 100) for i in range(60)]
    ind = _with_meta(candles, cfg_local)
    sig = detect(candles, ind, cfg_local) if ind else None
    assert sig is None


# local config copy (avoids importing the fixture into helper fns)
def _cfg():
    import copy
    from conftest import BASE_CFG
    return copy.deepcopy(BASE_CFG)
