"""
Cross-sectional low-volatility factor: a structurally different edge
source from everything else in this repo — every other strategy here
ranks stocks by price DIRECTION (trend, momentum, mean-reversion). This
ranks by CALMNESS instead. Each month-end, rank the universe by trailing
realized volatility (ascending) and hold the least-volatile N
equal-weighted for the next month. The "low-volatility anomaly"
(low-vol stocks historically outperform high-vol ones on a risk-adjusted
basis, contrary to naive CAPM) is a well-documented academic effect —
see https://quantpedia.com/strategies/low-volatility-factor-effect-in-stocks/
— added 2026-07-20 after reviewing paperswithbacktest/awesome-systematic-
trading (that repo's code is QuantConnect/LEAN-specific and not directly
usable here; this is a from-scratch adaptation of the idea, long-only,
in this project's own backtesting style, not a port of their code).

Reuses UNIVERSE, COMMISSION_ROUNDTRIP, and stats_for from
cross_sectional_momentum.py rather than duplicating them — same
universe, same benchmark/verdict logic, same walk-forward pattern, only
the ranking signal differs (volatility instead of trailing return).

Usage:
  python low_volatility_factor.py
  python low_volatility_factor.py --top-n 5 --vol-window 126 --walkforward
"""
import argparse
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

from cross_sectional_momentum import UNIVERSE, COMMISSION_ROUNDTRIP, stats_for

warnings.filterwarnings("ignore")


def load_daily_closes(ticker: str, years: int) -> pd.Series:
    df = yf.Ticker(ticker).history(period=f"{years}y", auto_adjust=True)
    if df.empty:
        return pd.Series(dtype=float)
    s = df["Close"]
    s.index = s.index.tz_localize(None)
    return s.dropna()


def load_universe_daily(tickers, years):
    px = {}
    for t in tickers:
        s = load_daily_closes(t, years)
        if len(s) > 0:
            px[t] = s
        else:
            print(f"  ! no data for {t}, dropping from universe")
    return pd.DataFrame(px).dropna(how="all")


def backtest(daily_px: pd.DataFrame, vol_window: int, top_n: int, commission: float):
    daily_ret = daily_px.pct_change()
    trailing_vol = daily_ret.rolling(vol_window).std()  # daily vol, needs vol_window days of history before it's real

    monthly_px = daily_px.resample("ME").last()
    monthly_vol = trailing_vol.resample("ME").last()  # vol as of each month-end's last trading day
    fwd_ret = monthly_px.pct_change().shift(-1)  # next month's return, aligned to ranking month

    idx = monthly_px.index
    port_rets, holdings_log, prev_holdings = [], [], set()
    for i in range(len(idx) - 1):
        t = idx[i]
        row = monthly_vol.loc[t].dropna()
        if len(row) < top_n:
            continue  # not enough trailing history yet, or not enough tickers with data this month
        picks = set(row.sort_values(ascending=True).head(top_n).index)  # ASCENDING — lowest vol wins, opposite of momentum's ranking
        avg_ret = fwd_ret.loc[t, list(picks)].mean()

        turnover = len(picks - prev_holdings) / top_n if prev_holdings else 1.0
        cost = turnover * commission
        port_rets.append((idx[i + 1], avg_ret - cost))
        holdings_log.append((t.date(), sorted(picks)))
        prev_holdings = picks

    strat = pd.Series({d: r for d, r in port_rets}).sort_index()
    return strat, holdings_log


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", default=UNIVERSE)
    p.add_argument("--vol-window", type=int, default=126, help="trading days of daily returns used to compute trailing volatility (126 ~= 6 months)")
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--commission", type=float, default=COMMISSION_ROUNDTRIP)
    p.add_argument("--walkforward", action="store_true")
    p.add_argument("--windows", type=int, default=2, help="number of sequential windows for --walkforward (default 2; use 3+ for a finer consistency check, per the lesson from the intraday track where a 2-way split looked better than a 4-way one)")
    args = p.parse_args()

    print(f"Loading {len(args.tickers)} tickers, {args.years}y daily closes...")
    px = load_universe_daily(args.tickers, args.years)
    print(f"{px.shape[1]} tickers with data, {px.shape[0]} trading days\n")

    strat, holdings_log = backtest(px, args.vol_window, args.top_n, args.commission)
    bench_ret = px.resample("ME").last().mean(axis=1).pct_change()  # equal-weight universe = the fair benchmark here

    print("Last 6 rebalances (lowest-volatility picks):")
    for d, picks in holdings_log[-6:]:
        print(f"  {d}  {', '.join(picks)}")
    print()

    if args.walkforward:
        chunk_size = len(strat) // args.windows
        rows = []
        for w in range(args.windows):
            start = w * chunk_size
            end = len(strat) if w == args.windows - 1 else (w + 1) * chunk_size
            rows.append(stats_for(strat.iloc[start:end], bench_ret, f"window_{w+1}"))
        df = pd.DataFrame(rows)
        print(df.to_string(index=False))
        pass_count = sum(1 for r in rows if r.get("verdict") == "PASS")
        win_rate = pass_count / len(rows)
        consistency = "CONSISTENT" if win_rate >= 0.70 else ("EPISODIC" if win_rate >= 0.40 else "OVERFITTED")
        holds_up = win_rate == 1.0
        print(f"\nwindows_passed: {pass_count}/{len(rows)} ({consistency})")
        print(f"holds_up (PASS in EVERY window independently): {holds_up}")
    else:
        row = stats_for(strat, bench_ret, "full_window")
        for k, v in row.items():
            print(f"  {k}: {v}")

    print(f"\nUniverse: {list(px.columns)}")
    print("Benchmark = equal-weight buy-and-hold of the same universe (fair comparison for a")
    print("stock-picking strategy — beating a single index isn't the same claim).")
    print("Costs are turnover-based (only names entering/leaving the top-N pay a round trip).")
    print("This is a NEW, unvalidated track (added 2026-07-20). Do not treat any PASS as")
    print("tradeable without walk-forward holding up AND paper-trade confirmation.")


if __name__ == "__main__":
    main()
