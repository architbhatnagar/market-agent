# market-agent: project context (snapshot 2026-10-16)

This is a summary of my personal market-research project, written so you have the context to discuss it. It is for personal use only: not a product, not advice.

## What it is
Solo developer in India. Python 3.9, pandas/numpy, yfinance (free, ~15-min delayed), backtesting.py (kernc). It covers Indian stocks (NSE), US stocks (S&P 500) and Indian mutual funds (NAVs from mfapi.in). It is **long-only cash equities**: no F&O, no leverage, no shorting. The research code has no LLM dependencies.

- `research/`: the active part. Strategy backtesting, signal scanners, a FastAPI signal API and paper trading.
- Telegram bot (`bot.py`, `agent_*.py`): parked, out of scope.

## Strategy library
**Daily/swing** (`strategies.py`): sma_cross, momentum (ADX≥25 + EMA stack + RSI band), mean_reversion, breakout (250d high), clenow_trend (EMA50/100 filter, 100d-high entry, 50d-low exit, ATR sizing), zscore_reversion (z≤-2 entry, z≥-1 exit, 200-SMA guard).

**Standalone portfolio strategies:** dual_momentum (Antonacci GEM), cross_sectional_momentum (top 5 of 29 NSE large-caps by 6m return, monthly), low_volatility_factor (the 5 lowest-vol names of the same 29, monthly).

**Intraday** (`strategies_intraday.py`, 5m bars, forced square-off at 15:20 IST): intraday_momentum (VWAP + EMA9/20 + ADX + RSI), orb_breakout (15-min opening range), vwap_breakout (VWAP reclaim + volume), range_reversion (rolling S/R + RSI + ADX<20).

## Evaluation rules I hold myself to
- A strategy passes only if it beats buy-and-hold **after costs** (0.0012/side India, 0.0005 US). MIXED means ≥70% of buy-and-hold. Anything else is FAIL.
- Fewer than 30 trades counts as noise.
- Never tune and test on the same window. Use walk-forward or multi-window consistency checks (`--windows N` gives CONSISTENT / EPISODIC / OVERFITTED).
- Real money only after broad PASS/MIXED results **and then** 2–3 months of paper trading that matches the backtest stats.
- I distrust self-reported win rates from repos and blogs until they are reproduced here with costs.

## Backtest results so far
- **Daily, 7 large-caps (RELIANCE, TCS, HDFCBANK, INFY, ITC, AAPL, MSFT), 8y and 20y:** no strategy beat buy-and-hold with a non-noise trade count.
- **Intraday, 14 NSE large-caps, 60 days (yfinance's 5m limit):** none of the 4 strategies held up under walk-forward. The few apparent passes had fewer than 30 trades or returns near zero.
- **Cross-sectional momentum:** MIXED over the full 10y, but the 2nd half failed walk-forward.
- **Low-volatility factor:** the best lead. Full 10y was a PASS (+297.8% vs +257.6% for the benchmark, Sharpe 1.03, max drawdown −26.2%). The 4-window check came out EPISODIC (2 PASS, 2 MIXED, **zero FAIL**). Promising but not proven; it has not been paper traded.
- I reviewed ML repos (openadvisor, a Stock-Market-Prediction LSTM app) and rejected them for data leakage or fabricated constants. awesome-systematic-trading was useful as an idea catalog only (its code targets the LEAN engine).

## Infrastructure
- Scanners read fired signals from the actual backtest trade log, so they share logic with the backtest. They report a BUY (fresh entry) or a HOLD (already in position). Position sizing is risk-based: `min(risk_amount // stop_distance, capital // entry)`.
- Universes are static snapshots, not live feeds: NIFTY 500 (from the NSE CSV), S&P 500 (scraped from Wikipedia), and mutual funds discovered live from mfapi.in (Direct/Growth equity plans).
- API (`research/api.py`): `/signal/{ticker}`, `/fund/{code}`, `/top-stocks`, `/top-funds`, `/paper/allocation`.
- Paper trading runs as 4 independent tracks, each with its own capital and state file. Common settings:
  - at most 5 positions
  - at most 25% of funds in any one position
  - Beta-smoothed adaptive strategy weighting (live win rate blended with backtest rank)
  - exits are detected by polling, not by real stop orders

## Paper-trading status (as of 2026-10-16)

| Track | Start | Running | Closed trades | Win rate | Realized P&L |
|---|---|---|---|---|---|
| Intraday NSE (cron every 15 min, market hours) | ₹1,00,000 | 2026-07-20 → still running | 1,688 | 49% | **+₹89,837 (~+90%)** |
| Daily NSE | ₹1,00,000 | 2026-07-20 → last run 07-30 | 2 | — | −₹68 |
| Daily US | $100,000 | 2026-07-21 → last run 07-30 | 5 | — | −$16 |
| Mutual funds | ₹1,00,000 | last run 07-21 | 0 | — | 0 |

Intraday by strategy:

| Strategy | Trades | Win rate | P&L |
|---|---|---|---|
| vwap_breakout | 1,454 | 51% | +₹77,194 |
| intraday_momentum | 155 | 37% | +₹11,329 |
| orb_breakout | 71 | 35% | +₹1,518 |
| range_reversion | 8 | 38% | −₹203 |

### Caveats on the intraday result (read these before reacting to the +90%)
1. **RISK_PCT was raised from 1% to 10% per position** in all 4 paper traders. The change is uncommitted, and I'm not sure exactly when it was made during the run. This is 10× the original risk budget, so the returns are not comparable to the 1% design.
2. Paper fills use yfinance's ~15-min-delayed data, and exits are checked by polling every 15 min, not with real stop orders. Real slippage and fills could differ a lot, especially for a high-turnover strategy like vwap_breakout (~25 trades/day).
3. In the 60-day backtest, vwap_breakout **failed** walk-forward (1/14 tickers held up, with returns near zero). Paper results this far above the backtest are a reason to look for a fill or accounting bug before believing it's an edge.
4. One regime: roughly 3 months of one market.
5. The daily, US and fund trackers stopped being run after late July, so they have no meaningful data.

## Open questions I'd like to think through
- Is the intraday paper result real, or an artifact of delayed-data fills, polling exits, or the 10% risk change? How should I audit it (e.g. replay closed trades against actual 5m bars, check intrabar stop breaches, add realistic slippage)?
- Should RISK_PCT go back to 1%, with the run restarted cleanly so the record matches the design?
- Is low-volatility factor worth paper trading next, and with what protocol?
- Should I switch to a paid real-time feed (or Alpaca's ~2–3y intraday history) to re-test intraday across more than one regime?

## Working style
Honest, direct engagement, no hype about strategy performance. I tend to collect new tools and strategies instead of finishing what exists, so push back on that. The strategy library is intentionally capped.
