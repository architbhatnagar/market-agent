# Strategy Specs (extracted from books, reimplemented)

Rules restated in our own words for implementation. Read the source
chapters for the reasoning, testing methodology, and failure modes —
that context is why the books are worth owning.

## 1. Clenow Core Trend Model → `clenow_trend` in strategies.py
Source: Following the Trend (2nd ed.), Ch. 4.

- Trend filter: bullish when EMA(50) > EMA(100), bearish when reversed.
- Entry (long): close makes a new 100-day high AND filter is bullish.
- Exit (long): close falls to a 50-day low, OR filter turns bearish.
- Sizing: volatility-normalized. Shares ≈ (risk_factor × equity) / ATR(20),
  risk_factor ≈ 0.0015 (15 bp of equity per ATR-unit of daily movement).

Adaptations vs the book (be aware — these change the strategy's character):
- Book trades ~50+ futures across currencies/rates/commodities/equities,
  long AND short, with leverage. We run long-only cash equities.
  Expect: no crisis alpha, worse bear-market behavior, higher correlation
  between positions. Diversify across many tickers to partially compensate.

## 2. Chan Bollinger z-score reversion → `zscore_reversion` in strategies.py
Source: Quantitative Trading, Ch. 3 (index mean-reversion example), Ch. 7.

- Compute rolling mean and std of close over N days (default 20).
- z = (close − mean) / std.
- Enter long when z ≤ −2 (stretched below), exit when z ≥ −1 (reverted).
- Book version also shorts at z ≥ +2; we skip shorting in cash equities.
- Regime guard (our addition): only take longs above the 200-day SMA;
  mean reversion against a downtrend is how accounts die.

Not implemented (needs pairs infrastructure): Chan's cointegration pair
trading (e.g. the gold-miners spread), half-life estimation, Kelly sizing.
Future extension if the single-asset versions earn attention.

## 3. Antonacci GEM (dual momentum) → dual_momentum.py (standalone)
Source: Dual Momentum Investing (slide-deck edition uploaded).

Monthly, 12-month lookback total return:
1. Relative momentum: compare US equities vs international equities;
   pick the stronger.
2. Absolute momentum: if the winner's 12-month return ≤ T-bill return,
   hold aggregate bonds instead.
3. Hold one asset at a time; re-evaluate monthly. That's the whole thing.

Default ETF proxies: SPY (US), ACWX (intl ex-US), BIL (T-bills), AGG (bonds).
Indian-investor variants worth testing: NIFTYBEES.NS as the "home" equity
sleeve vs SPY as the "international" one, with an Indian liquid/gilt fund
proxy for the safe asset. Currency risk becomes part of the signal — test it,
don't assume it.

## Testing discipline (applies to all)
- Backtest across MANY tickers/universes and multiple windows, after costs.
- <30 trades → stats are noise. Sharp parameter peaks → memorization.
- A strategy is adopted only after PASS/MIXED verdicts across a broad set,
  then 2–3 months of paper-traded signals matching backtest behavior.
