"""
Antonacci GEM (dual momentum) — standalone monthly rotation.
Holds exactly ONE asset at a time.

Rules (12-month lookback, monthly):
  1. Relative momentum: US equities vs international equities — take the stronger.
  2. Absolute momentum: if the winner's 12m return <= T-bills' 12m return,
     hold bonds instead.

Usage:
  python dual_momentum.py                          # backtest with SPY/ACWX/BIL/AGG
  python dual_momentum.py --signal                 # just print this month's holding
  python dual_momentum.py --us NIFTYBEES.NS --intl SPY   # Indian home-market variant
"""
import argparse
import warnings

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

LOOKBACK_MONTHS = 12


def monthly_closes(ticker: str, years: int) -> pd.Series:
    df = yf.Ticker(ticker).history(period=f"{years}y", auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data for {ticker}")
    s = df["Close"]
    s.index = s.index.tz_localize(None)
    return s.resample("ME").last().dropna()


def gem(us: str, intl: str, tbill: str, bonds: str, years: int):
    px = pd.DataFrame({t: monthly_closes(t, years) for t in [us, intl, tbill, bonds]}).dropna()
    r12 = px.pct_change(LOOKBACK_MONTHS)

    holdings, rets = [], []
    idx = px.index
    for i in range(LOOKBACK_MONTHS, len(idx) - 1):
        t = idx[i]
        winner = us if r12.loc[t, us] >= r12.loc[t, intl] else intl
        hold = winner if r12.loc[t, winner] > r12.loc[t, tbill] else bonds
        nxt = px.iloc[i + 1][hold] / px.iloc[i][hold] - 1
        holdings.append((t.date(), hold))
        rets.append(nxt)

    strat = (pd.Series(rets, index=idx[LOOKBACK_MONTHS + 1:len(idx)]) + 1).cumprod()
    bench = (px[us].pct_change().iloc[LOOKBACK_MONTHS + 1:] + 1).cumprod()

    yrs = len(strat) / 12
    stats = {
        "GEM total return %": round((strat.iloc[-1] - 1) * 100, 1),
        "GEM CAGR %": round((strat.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
        f"Buy&hold {us} %": round((bench.iloc[-1] - 1) * 100, 1),
        "GEM max drawdown %": round(((strat / strat.cummax()) - 1).min() * 100, 1),
        f"{us} max drawdown %": round(((bench / bench.cummax()) - 1).min() * 100, 1),
        "Switches": sum(1 for a, b in zip(holdings, holdings[1:]) if a[1] != b[1]),
    }
    return holdings, stats


def current_signal(us, intl, tbill, bonds):
    px = pd.DataFrame({t: monthly_closes(t, 3) for t in [us, intl, tbill, bonds]}).dropna()
    r12 = px.pct_change(LOOKBACK_MONTHS).iloc[-1]
    winner = us if r12[us] >= r12[intl] else intl
    hold = winner if r12[winner] > r12[tbill] else bonds
    print(f"12m returns: {us} {r12[us]:+.1%} | {intl} {r12[intl]:+.1%} | {tbill} {r12[tbill]:+.1%}")
    print(f"GEM says hold: {hold}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--us", default="SPY")
    p.add_argument("--intl", default="ACWX")
    p.add_argument("--tbill", default="BIL")
    p.add_argument("--bonds", default="AGG")
    p.add_argument("--years", type=int, default=15)
    p.add_argument("--signal", action="store_true", help="print this month's holding only")
    args = p.parse_args()

    if args.signal:
        current_signal(args.us, args.intl, args.tbill, args.bonds)
        return

    holdings, stats = gem(args.us, args.intl, args.tbill, args.bonds, args.years)
    print("Last 12 months of holdings:")
    for d, h in holdings[-12:]:
        print(f"  {d}  {h}")
    print()
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("\nNote: monthly rotation across ETFs is delayed-data friendly. "
          "Costs are NOT modeled here (one trade/month keeps them small but nonzero).")


if __name__ == "__main__":
    main()
