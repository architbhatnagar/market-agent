# Personal Market Research Agent (Telegram)

A single-user Telegram bot that answers market research questions about Indian and US stocks, ETFs, and Indian mutual funds using free data sources. The reasoning layer is pluggable: **Gemini or Claude**, switched with one env var (`LLM_PROVIDER`).

## Data sources (all free)
- **yfinance** — US tickers (AAPL, VOO), Indian tickers with `.NS`/`.BO` suffix (RELIANCE.NS, TCS.BO), indices (^NSEI, ^GSPC)
- **mfapi.in** — Indian mutual fund NAVs (AMFI data), searchable by scheme name

## Setup (10 minutes)

1. **Telegram bot token** — message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, copy the token.
2. **Your Telegram user ID** — message [@userinfobot](https://t.me/userinfobot), it replies with your numeric ID. The bot ignores everyone else.
3. **LLM key** — Gemini key from https://aistudio.google.com (has a generous free tier), or Anthropic key from https://console.anthropic.com. Set `LLM_PROVIDER` accordingly in `.env`.
4. Then:

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the three values
python bot.py
```

Message your bot on Telegram. `/start` shows examples, `/reset` clears context.

## Buy/sell suggestions
Tell the bot your holdings in plain language — "I hold 10 TCS at 3600 avg, 15 units of Parag Parikh Flexi Cap at 62" — and it saves them to `portfolio.json`. Then `/suggest` runs a full review: it pulls live data on every holding and watchlist item and gives BUY/ADD/HOLD/TRIM/SELL leanings with reasoning, confidence, and the key risk on each call. These are research opinions from delayed public data, not investment advice — verify before acting.

## Example prompts
- "Quote for RELIANCE.NS and how has it done over 1 year?"
- "Compare TCS.NS, INFY.NS and ^GSPC over 3y"
- "Search Parag Parikh flexi cap, show me 3 year returns"
- "Fundamentals of NVDA — how stretched is the valuation?"
- "Add NVDA and HDFCBANK.NS to my watchlist"
- "Should I trim my TCS position?"

## Running it 24/7
- Cheapest: any always-on machine (old laptop, Raspberry Pi) — it's just long-polling, no public IP needed.
- Free-tier cloud: Oracle Cloud always-free VM, or a Fly.io/Railway hobby instance.
- Keep it alive with `systemd` or `tmux` + a restart loop.

## Costs
Data is free. With Gemini Flash on the AI Studio free tier, daily personal use can cost nothing at all. On Claude Sonnet, a typical query with 2–4 tool calls costs roughly a rupee or two (₹100–300/month for daily use).

## Project layout
- `tools.py` — the six market-data tools (shared by both providers)
- `prompts.py` — shared system prompt
- `agent_gemini.py` / `agent_claude.py` — provider adapters with the same `ask`/`reset` interface
- `portfolio.py` — holdings/watchlist persisted to portfolio.json
- `bot.py` — Telegram wiring + provider switch + /suggest command

## Research module (`research/`)
This is the actively developed half of the project — LLM-free strategy backtesting, live signal scanning, and paper trading across NSE stocks, US stocks, and Indian mutual funds. Pure Python: pandas, numpy, yfinance, backtesting.py. The Telegram bot above is parked; this is where current work happens.

### Strategy library
- **Daily/swing** (`strategies.py`): `sma_cross`, `momentum`, `mean_reversion`, `breakout`, `clenow_trend`, `zscore_reversion` — plugin classes, add your own by subclassing `Strategy`. Plus standalone scripts: `dual_momentum.py` (Antonacci-style asset-class rotation), `cross_sectional_momentum.py` (ranks a stock universe by relative strength, not absolute direction), `low_volatility_factor.py` (ranks by trailing volatility — the "calm beats volatile" anomaly).
- **Intraday** (`strategies_intraday.py`, 5-minute bars, hard square-off at 15:20 IST, no overnight positions): `intraday_momentum`, `orb_breakout` (opening-range breakout), `vwap_breakout` (VWAP reclaim), `range_reversion` (mean-reversion, the one non-directional strategy in the set).

### Backtesting
- `run_backtest.py` (daily) / `run_backtest_intraday.py` (intraday) — costed backtests vs buy-and-hold. **Always pass `--walkforward --windows 4`** (or more) — a single full-window verdict is known to look better than it is; splitting into several sequential windows and requiring a pass in each one is the only check that's caught every false positive so far.
- Example: `python run_backtest.py --tickers RELIANCE.NS TCS.NS AAPL --strategy all --years 8`

### Live scanning
Every scanner reads its fired signal off the actual backtest trade log — never a hand-re-derived copy of the entry rule, so it can't silently drift out of sync with what was actually backtested.
- `scan_daily.py` — NSE stocks, NIFTY 500 universe. `.NS` suffix is optional (`RELIANCE` auto-resolves).
- `scan_daily_us.py` — US stocks, S&P 500 universe.
- `scan_intraday.py` — NSE stocks, NIFTY 500 universe, 5-minute bars.
- `scan_funds.py` — Indian mutual funds (mfapi.in), universe live-discovered from mfapi.in's scheme catalog (not hardcoded).

All four support `--top N` (ranked scan of the whole universe, only actionable signals, never padded) and auto-select the best currently-firing strategy per ticker if you don't force one with `--strategy`.

### API
`cd research && uvicorn api:app --reload --port 8000` — interactive docs at `http://localhost:8000/docs`.
- `GET /signal/{symbol}`, `GET /fund/{query}` — single-ticker/fund signal
- `GET /top-stocks`, `GET /top-funds` — ranked scan of the default universe
- `GET /paper/daily/allocation`, `GET /paper/daily-us/allocation`, `GET /paper/allocation` — current paper-trading holdings (read-only; doesn't trigger a trading cycle itself)

### Paper trading
Four independent simulated portfolios — own starting capital, own state file, nothing shared between them:

| Tracker | Market | Capital | Cadence |
|---|---|---|---|
| `paper_trader.py` | NSE intraday | ₹1,00,000 | every 15-30 min, market hours (cron-friendly) |
| `paper_trader_daily.py` | NSE daily/swing | ₹1,00,000 | once/day |
| `paper_trader_funds.py` | Indian mutual funds | ₹1,00,000 | once/day |
| `paper_trader_daily_us.py` | US (S&P 500) | $100,000 (not FX-converted) | once/day |

Each: `--run` (check exits, open new positions against live signals) / `--report` (performance anytime) / `--reset --confirm` (wipe and restart). Position sizing is risk-based (default 1% of current capital per trade) and capped at 25% of total funds per position — no single signal can dominate the portfolio regardless of what the raw risk math would otherwise allow.

### Honest status
Every strategy here is walk-forward tested, not just backtested once, before being trusted — and most have failed that test. As of this writing: none of the daily or intraday rule-based strategies have a clean, consistent pass across multiple windows on the NSE large-cap universe tested; buy-and-hold has beaten all of them more often than not. The most promising lead so far is `low_volatility_factor.py`, which hasn't outright failed any window yet (though it isn't fully consistent either). Paper trading exists specifically to gather real, un-curve-fitted evidence before any real money is involved — nothing produced by this module should be read as a recommendation to act on. See `CLAUDE.md`'s Evaluation history for the full blow-by-blow.

Default commission (0.12%/side for NSE, ~0.05% for US) approximates real delivery/brokerage costs; override with `--commission` on any backtest or scan.

## Known limits of free data
- yfinance is unofficial scraping of Yahoo Finance — it occasionally rate-limits or breaks after Yahoo changes things; pin/upgrade the package when that happens.
- NSE data via Yahoo is delayed ~15 min. Fine for backtesting and paper trading; a known, accepted limitation for the intraday track specifically (see Research module) — not something to trade real money against without a faster feed.
- mfapi.in NAVs update once daily (as AMFI publishes them).

## Ideas for v2
- `/digest` command or a daily 8am scheduled summary of your watchlist (APScheduler)
- News/sentiment tool via an RSS parser or the Claude API's web search tool
- Portfolio tracking: store your holdings in SQLite, ask "how is my portfolio doing?"
- Charts: render matplotlib PNGs and send as Telegram photos
