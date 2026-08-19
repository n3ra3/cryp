# Paper-Trading Scanner Bot

A research-only scanner for a discretionary **mean-reversion / fade-the-spike**
strategy (target = 50% Fibonacci retracement of the impulse leg).

It reads **public** market data from several exchanges via `ccxt`, computes
indicators on **closed candles only**, detects setups, and — instead of trading —
logs **virtual trades** (entry / stop / target, result in **R**) so you can
measure the strategy's edge over ~2 weeks. It also sends live **Telegram** alerts.

> ⚠️ **This bot never trades.** No order execution. No trading API keys. It only
> touches public OHLCV endpoints. Telegram tokens are the only secrets, and they
> come from environment variables.

---

## Core principles (never violated)

1. **Paper only.** No access to funds; public market-data endpoints only.
2. **Closed candles only.** Every indicator/signal is computed on closed bars.
   The forming bar is ignored — no lookahead, no repaint.
3. **Isolated detector.** All setup logic lives in `scanner/detector.py` behind
   `detect(candles, indicators, config)`. All thresholds live in `config.yaml`,
   marked `# PLACEHOLDER — tune later`. Tuning never touches other modules.
4. **Honest simulation.** Fees + slippage always modelled. Entry fills at the
   **open of the next candle** after the signal. If one candle touches **both**
   stop and target → **STOP wins** (conservative).

---

## Architecture

| Module | Responsibility |
|---|---|
| `scanner/datafeed.py` | ccxt OHLCV polling; returns only closed candles; reconnect w/ exponential backoff |
| `scanner/indicators.py` | ATR(14), RSI(14), EMA(20), swing hi/lo, candle metrics — pure functions |
| `scanner/detector.py` | Isolated setup logic: `detect(candles, indicators, config) -> Signal \| None` |
| `scanner/paper_exec.py` | Virtual trade lifecycle, fees/slippage, resolve stop/target, result in R |
| `scanner/journal.py` | SQLite journal + CSV export; reloads open trades on restart |
| `scanner/stats.py` | Signals, win rate, avg R, expectancy, max drawdown, breakdowns |
| `scanner/telegram_bot.py` | Alerts + `/status /stats /pause /resume /export` |
| `scanner/health.py` | aiohttp `GET /health -> 200` on `:8000` |
| `main.py` | Orchestration: poll → indicators → detector → paper_exec → journal |

---

## Scanning the whole market (not just BTC)

By default the bot **auto-discovers the liquid universe** on every exchange —
it doesn't scan a hand-picked handful. On startup it loads all markets +
tickers and keeps the top pairs by 24h turnover.

Configured under `symbols_mode: auto` + `universe:` in `config.yaml`:

```yaml
symbols_mode: auto
universe:
  quote: USDT           # only USDT pairs
  market_type: spot     # spot | swap (perpetual futures)
  top_n: 60             # keep the top-N by turnover; null = all matches
  min_quote_volume: 3000000   # drop illiquid pairs
  refresh_hours: 12     # re-scan for new listings / volume shifts
  blacklist: [USDC, FDUSD, TUSD, DAI, USDe, EUR, BUSD]
```

Set `symbols_mode: manual` to scan only an explicit `symbols:` list instead.

**Why not poll every coin every minute?** A 15m candle only closes every 15
minutes, and hammering hundreds of pairs per minute trips exchange rate limits.
So the scanner **aligns to candle closes**: it sleeps until just after each bar
closes, then runs one parallel pass (bounded by `scan.concurrency`). Hundreds of
symbols stay well within public rate limits.

`/status` (Telegram or `GET /status`) reports how many markets are being scanned.

---

## The detector (first draft — all numbers are placeholders)

Fade an over-extended impulse; target the 50% retracement of the impulse leg.

**SHORT** (fade a spike up):
1. **Stretch:** `close` is more than `stretch_atr_mult × ATR` above `EMA(20)`
   **or** `RSI ≥ rsi_overbought`.
2. **Level:** price is within `level_tolerance_pct` of the recent swing high
   (max high over `swing_lookback` bars).
3. **Confirmation:** last closed bar is a bearish rejection (`close < open` and
   `close < previous high`).
4. **Entry:** open of the next bar.
5. **Stop:** above the spike high + `stop_buffer_atr × ATR`.
6. **Target:** 50% retracement of the leg (swing low → swing high).

**LONG** is the mirror image (RSI oversold / stretched down, swing low, bullish
rejection, target = 50% back up).

Everything numeric is in `config.yaml` under `detector:`.

---

## Paper execution model

- **Entry fill:** open of the next candle, worsened by `slippage_pct`.
- **Fees:** `taker_fee_pct` on **both** legs.
- **Resolve:** walk candles from entry forward. `stop` and `target` both hit in
  one bar → **STOP**. Timeout after `max_bars_in_trade` → close at that bar's
  close.
- **Result:** `R = net_pnl_after_fees_and_slippage / risk_usd`.
- **Sizing:** `risk_usd = paper_equity × risk_per_trade_pct`;
  `quantity = risk_usd / stop_distance`.

---

## Run locally

Requires Python 3.11+.

```bash
pip install -r requirements.txt
```

Run the test suite (this is what keeps the stats honest):

```bash
python -m pytest -q
```

Start the bot (works even without Telegram — alerts are logged instead):

```bash
python main.py
```

With Telegram alerts:

```bash
export TELEGRAM_TOKEN="123456:your-token"      # from @BotFather
export TELEGRAM_CHAT_ID="your-numeric-chat-id"
python main.py
```

Health check while running:

```bash
curl http://localhost:8000/health     # -> ok (200)
curl http://localhost:8000/status     # -> uptime / open trades / paused
```

### Telegram commands
`/status` · `/stats` · `/pause` · `/resume` · `/export` (sends the CSV).

---

## Run with Docker

```bash
docker build -t paper-scanner .
docker run --rm -p 8000:8000 \
  -e TELEGRAM_TOKEN=... -e TELEGRAM_CHAT_ID=... \
  -v "$(pwd)/data:/app/data" \
  paper-scanner
```

The SQLite journal lives in `/app/data` — mount a volume so trades persist.

---

## Deploy on Northflank (free Developer Sandbox)

> Free-tier limits change; check current CPU/RAM/volume caps in the Northflank
> docs before sizing. The image is intentionally slim to fit a small sandbox.

1. **Push this repo** to GitHub/GitLab.
2. **Create a service** → *Deployment* → *Combined service* (build + run), or a
   *Deployment service* from your Git repo. Point it at this repository; it
   builds from the included `Dockerfile` (no build settings needed).
3. **Instances:** 1 (this is a single always-on process). Pick the smallest
   plan the sandbox allows.
4. **Networking / Health check:**
   - Add an HTTP **health check** on port **`8000`**, path **`/health`**,
     expecting **200**.
   - You do *not* need a public domain — the bot pushes to Telegram itself. A
     public port is optional if you want to hit `/status` externally.
5. **Environment variables** (Service → *Environment*): add
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
   (No exchange keys — the bot doesn't use any.)
6. **Persistent volume:** add a volume mounted at **`/app/data`** so the SQLite
   journal and open paper trades survive restarts/redeploys. A small volume
   (e.g. 1 GB) is plenty.
7. **Deploy.** Watch logs for `health server on :8000` and, once markets warm up,
   `SIGNAL ...` / `CLOSED ...` lines. On restart the service logs
   `restored N open paper trades from journal`.

### Tuning during the research window
Edit thresholds in `config.yaml` under `detector:` (all marked
`# PLACEHOLDER — tune later`) and redeploy. No code changes required.

---

## Project layout

```
crypto/
├── main.py                # orchestration
├── config.yaml            # markets + all detector thresholds (placeholders)
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .env.example
├── scanner/
│   ├── config.py
│   ├── datafeed.py
│   ├── indicators.py
│   ├── detector.py        # <- isolated setup logic
│   ├── paper_exec.py
│   ├── journal.py
│   ├── stats.py
│   ├── telegram_bot.py
│   ├── health.py
│   └── models.py
├── tests/
│   ├── test_indicators.py
│   ├── test_detector.py   # includes the no-lookahead test
│   ├── test_paper_exec.py # includes the stop-wins & fee/slippage tests
│   ├── test_datafeed.py   # closed-candle filter + reconnect backoff
│   ├── test_universe.py   # auto-discovery filtering + candle-boundary timing
│   └── test_journal.py    # open trades survive a restart
└── data/                  # SQLite + CSV (persist this in production)
```
