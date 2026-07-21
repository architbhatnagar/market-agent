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
Strategy backtesting and daily scanning, built on backtesting.py:
- `strategies.py` — four starter strategies (sma_cross, momentum, mean_reversion, breakout) as plugin classes; add your own by subclassing Strategy
- `indicators.py` — SMA/EMA/RSI/ATR/ADX in plain pandas
- `run_backtest.py` — costed backtests vs buy-and-hold: `python run_backtest.py --tickers RELIANCE.NS TCS.NS AAPL --strategy all --years 8`
- `scan.py` — today's entry signals from the same rules: `python scan.py --tickers RELIANCE.NS TCS.NS INFY.NS AAPL`

Workflow: backtest first, only scan with strategies that PASS, paper-trade signals for 2-3 months before money. Default commission (0.12%/side) approximates Indian delivery costs incl. STT and slippage.

## Known limits of free data
- yfinance is unofficial scraping of Yahoo Finance — it occasionally rate-limits or breaks after Yahoo changes things; pin/upgrade the package when that happens.
- NSE data via Yahoo is delayed ~15 min. Fine for research, not for intraday trading.
- mfapi.in NAVs update once daily (as AMFI publishes them).

## Ideas for v2
- `/digest` command or a daily 8am scheduled summary of your watchlist (APScheduler)
- News/sentiment tool via an RSS parser or the Claude API's web search tool
- Portfolio tracking: store your holdings in SQLite, ask "how is my portfolio doing?"
- Charts: render matplotlib PNGs and send as Telegram photos
