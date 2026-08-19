"""Universe auto-selection: quote/type filtering, blacklist, min-volume, top-N."""

from scanner.datafeed import filter_universe, next_boundary_ms, timeframe_ms


def _markets():
    return {
        "BTC/USDT":  {"active": True, "spot": True,  "swap": False, "quote": "USDT", "base": "BTC"},
        "ETH/USDT":  {"active": True, "spot": True,  "swap": False, "quote": "USDT", "base": "ETH"},
        "SOL/USDT":  {"active": True, "spot": True,  "swap": False, "quote": "USDT", "base": "SOL"},
        "DOGE/USDT": {"active": True, "spot": True,  "swap": False, "quote": "USDT", "base": "DOGE"},
        "USDC/USDT": {"active": True, "spot": True,  "swap": False, "quote": "USDT", "base": "USDC"},  # stable
        "BTC/USDT:USDT": {"active": True, "spot": False, "swap": True, "quote": "USDT", "base": "BTC"},  # perp
        "XYZ/USDT":  {"active": False, "spot": True,  "swap": False, "quote": "USDT", "base": "XYZ"},  # inactive
        "ETH/BTC":   {"active": True, "spot": True,  "swap": False, "quote": "BTC",  "base": "ETH"},   # wrong quote
    }


def _tickers():
    return {
        "BTC/USDT":  {"quoteVolume": 900_000_000},
        "ETH/USDT":  {"quoteVolume": 500_000_000},
        "SOL/USDT":  {"quoteVolume": 100_000_000},
        "DOGE/USDT": {"quoteVolume": 1_000_000},     # below min_quote_volume
        "USDC/USDT": {"quoteVolume": 800_000_000},
        "BTC/USDT:USDT": {"quoteVolume": 700_000_000},
    }


def test_spot_usdt_filter_sorts_and_caps():
    cfg = {
        "quote": "USDT", "market_type": "spot", "top_n": 2,
        "min_quote_volume": 3_000_000, "blacklist": ["USDC"],
    }
    picked = filter_universe(_markets(), _tickers(), cfg)
    # DOGE dropped (volume), USDC dropped (blacklist), perp/inactive/wrong-quote excluded
    # sorted by turnover, capped at top 2 -> BTC, ETH
    assert picked == ["BTC/USDT", "ETH/USDT"]


def test_min_volume_excludes_illiquid():
    cfg = {"quote": "USDT", "market_type": "spot", "top_n": None,
           "min_quote_volume": 3_000_000, "blacklist": ["USDC"]}
    picked = filter_universe(_markets(), _tickers(), cfg)
    assert "DOGE/USDT" not in picked
    assert set(picked) == {"BTC/USDT", "ETH/USDT", "SOL/USDT"}


def test_blacklist_contains_excludes_tokenized_stocks():
    markets = {
        "BTC/USDT":       {"active": True, "spot": True, "swap": False, "quote": "USDT", "base": "BTC"},
        "SNDKSTOCK/USDT": {"active": True, "spot": True, "swap": False, "quote": "USDT", "base": "SNDKSTOCK"},
        "XAU/USDT":       {"active": True, "spot": True, "swap": False, "quote": "USDT", "base": "XAU"},
    }
    tickers = {"BTC/USDT": {"quoteVolume": 1e9},
               "SNDKSTOCK/USDT": {"quoteVolume": 9e6},
               "XAU/USDT": {"quoteVolume": 9e6}}
    cfg = {"quote": "USDT", "market_type": "spot", "top_n": None,
           "min_quote_volume": 0, "blacklist": ["XAU"], "blacklist_contains": ["STOCK"]}
    picked = filter_universe(markets, tickers, cfg)
    assert picked == ["BTC/USDT"]   # stock (substring) + XAU (exact) removed


def test_swap_market_type_selects_perps_only():
    cfg = {"quote": "USDT", "market_type": "swap", "top_n": None,
           "min_quote_volume": 0, "blacklist": []}
    picked = filter_universe(_markets(), _tickers(), cfg)
    assert picked == ["BTC/USDT:USDT"]


def test_next_boundary_alignment():
    step = timeframe_ms("15m")
    now = 3 * step + 123        # partway through the 4th candle
    assert next_boundary_ms("15m", now) == 4 * step
