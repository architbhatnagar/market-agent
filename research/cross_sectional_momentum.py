"""
Cross-sectional (relative strength) momentum: unlike every other strategy
in this repo, this doesn't ask "is THIS stock's own price action bullish"
(absolute/time-series momentum) — it asks "which stocks in the universe
are outperforming the OTHERS right now" and rotates into the leaders.
This is a structurally different edge source (Jegadeesh-Titman-style),
not a parameter variant of the single-name rules in strategies.py.

Rules (classic, simplified — no 12-1 skip-month refinement yet):
  Each month-end, rank the universe by trailing LOOKBACK_MONTHS return.
  Hold the top N equal-weighted for the next month. Rebalance monthly.

Usage:
  python cross_sectional_momentum.py
  python cross_sectional_momentum.py --top-n 5 --lookback 6 --walkforward
"""
import argparse
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "ITC.NS", "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS",
    "HINDUNILVR.NS", "BAJFINANCE.NS", "MARUTI.NS", "SUNPHARMA.NS",
    "BHARTIARTL.NS", "ASIANPAINT.NS", "TITAN.NS", "ULTRACEMCO.NS",
    "NESTLEIND.NS", "WIPRO.NS", "HCLTECH.NS", "TATASTEEL.NS",
    "JSWSTEEL.NS", "POWERGRID.NS", "NTPC.NS", "ONGC.NS", "COALINDIA.NS",
    "M&M.NS", "BAJAJFINSV.NS",
]

COMMISSION_ROUNDTRIP = 0.0024  # ~0.12%/side delivery costs, matches run_backtest.py default


def monthly_closes(ticker: str, years: int) -> pd.Series:
    df = yf.Ticker(ticker).history(period=f"{years}y", auto_adjust=True)
    if df.empty:
        return pd.Series(dtype=float)
    s = df["Close"]
    s.index = s.index.tz_localize(None)
    return s.resample("ME").last().dropna()


def load_universe(tickers, years):
    px = {}
    for t in tickers:
        s = monthly_closes(t, years)
        if len(s) > 0:
            px[t] = s
        else:
            print(f"  ! no data for {t}, dropping from universe")
    return pd.DataFrame(px).dropna(how="all")


def backtest(px: pd.DataFrame, lookback: int, top_n: int, commission: float):
    mom = px.pct_change(lookback)
    fwd_ret = px.pct_change().shift(-1)  # next month's return, aligned to ranking month

    idx = px.index
    port_rets, holdings_log, prev_holdings = [], [], set()
    for i in range(lookback, len(idx) - 1):
        t = idx[i]
        row = mom.loc[t].dropna()
        if len(row) < top_n:
            continue
        picks = set(row.sort_values(ascending=False).head(top_n).index)
        avg_ret = fwd_ret.loc[t, list(picks)].mean()

        turnover = len(picks - prev_holdings) / top_n if prev_holdings else 1.0
        cost = turnover * commission
        port_rets.append((idx[i + 1], avg_ret - cost))
        holdings_log.append((t.date(), sorted(picks)))
        prev_holdings = picks

    strat = pd.Series({d: r for d, r in port_rets}).sort_index()
    return strat, holdings_log


def stats_for(strat: pd.Series, bench_monthly_ret: pd.Series, label: str) -> dict:
    if len(strat) < 6:
        return {"period": label, "months": len(strat), "verdict": "INSUFFICIENT DATA"}
    curve = (1 + strat).cumprod()
    bench = (1 + bench_monthly_ret.reindex(strat.index)).cumprod()
    yrs = len(strat) / 12
    total = (curve.iloc[-1] - 1) * 100
    bh_total = (bench.iloc[-1] - 1) * 100
    verdict = "PASS" if (total > bh_total and total > 0) else ("MIXED" if total > 0.7 * bh_total else "FAIL")
    sharpe = (strat.mean() / strat.std()) * np.sqrt(12) if strat.std() > 0 else 0.0
    return {
        "period": label,
        "months": len(strat),
        "return_pct": round(total, 1),
        "cagr_pct": round(((curve.iloc[-1]) ** (1 / yrs) - 1) * 100, 1),
        "benchmark_pct": round(bh_total, 1),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(((curve / curve.cummax()) - 1).min() * 100, 1),
        "verdict": verdict,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", default=UNIVERSE)
    p.add_argument("--lookback", type=int, default=6, help="months of trailing return used to rank")
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--commission", type=float, default=COMMISSION_ROUNDTRIP)
    p.add_argument("--walkforward", action="store_true")
    args = p.parse_args()

    print(f"Loading {len(args.tickers)} tickers, {args.years}y monthly closes...")
    px = load_universe(args.tickers, args.years)
    print(f"{px.shape[1]} tickers with data, {px.shape[0]} months\n")

    strat, holdings_log = backtest(px, args.lookback, args.top_n, args.commission)
    bench_ret = px.mean(axis=1).pct_change()  # equal-weight universe = the fair benchmark here

    print("Last 6 rebalances:")
    for d, picks in holdings_log[-6:]:
        print(f"  {d}  {', '.join(picks)}")
    print()

    if args.walkforward:
        half = len(strat) // 2
        rows = [
            stats_for(strat.iloc[:half], bench_ret, "first_half"),
            stats_for(strat.iloc[half:], bench_ret, "second_half"),
        ]
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))
        holds_up = all(r.get("verdict") == "PASS" for r in rows)
        print(f"\nholds_up (PASS in both halves independently): {holds_up}")
    else:
        row = stats_for(strat, bench_ret, "full_window")
        for k, v in row.items():
            print(f"  {k}: {v}")

    print(f"\nUniverse: {list(px.columns)}")
    print("Benchmark = equal-weight buy-and-hold of the same universe (fair comparison for a")
    print("stock-picking strategy — beating a single index isn't the same claim).")
    print("Costs are turnover-based (only names entering/leaving the top-N pay a round trip).")
    print("This is a NEW, unvalidated track (added 2026-07-19). Do not treat any PASS as")
    print("tradeable without walk-forward holding up AND paper-trade confirmation.")


if __name__ == "__main__":
    main()
