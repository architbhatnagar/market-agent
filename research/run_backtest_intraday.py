"""
Intraday backtest runner. Same verdict logic as run_backtest.py, on
intraday bars instead of daily.

Usage:
  python run_backtest_intraday.py --tickers ICICIBANK.NS RELIANCE.NS --strategy all
  python run_backtest_intraday.py --tickers AAPL --interval 5m --period 60d

yfinance intraday history limits (free tier): 1m -> 7d max, 5m/15m/30m ->
60d max, 60m -> 730d max. Requesting beyond these silently truncates.

Costs default to an approximation of Indian intraday round-trip costs
(STT on sell side only, txn charges, GST, stamp duty; zero brokerage on
discount platforms) — NOT reproduced from a fee schedule, treat as rough.
Override with --commission.

Buy & Hold Return here is backtesting.py's default: first bar to last
bar of the whole window, INCLUDING overnight gaps. An intraday strategy
structurally can't capture overnight moves (that's the point of flattening
daily), so losing to this benchmark on quiet, gap-up-drifting names is
expected, not necessarily a strategy flaw. Read the equity curve, not
just the verdict, before concluding anything.
"""
import argparse
import warnings

import numpy as np
import pandas as pd
import yfinance as yf
from backtesting import Backtest

from strategies_intraday import ALL_INTRADAY_STRATEGIES

warnings.filterwarnings("ignore")

CASH = 100_000
MAX_PERIOD = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d", "60m": "730d"}


def load_data(ticker: str, interval: str, period: str) -> tuple:
    """Try `ticker` as given (so US symbols like AAPL work unchanged);
    if empty and no exchange suffix was given, retry with '.NS'
    appended. Returns (resolved_symbol, data)."""
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    resolved = ticker
    if df.empty and "." not in ticker:
        resolved = f"{ticker}.NS"
        df = yf.Ticker(resolved).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data for {ticker} at interval={interval} period={period}" +
                          (f" (tried '{ticker}' and '{resolved}')" if resolved != ticker else ""))
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return resolved, df


def run_bt(data: pd.DataFrame, strategy_name: str, commission: float) -> dict:
    bt = Backtest(data, ALL_INTRADAY_STRATEGIES[strategy_name], cash=CASH,
                  commission=commission, exclusive_orders=True, finalize_trades=True)
    s = bt.run()
    ret, bh = s["Return [%]"], s["Buy & Hold Return [%]"]
    dd = s["Max. Drawdown [%]"]
    verdict = "PASS" if (ret > bh and ret > 0) else ("MIXED" if ret > 0.7 * bh else "FAIL")
    return {
        "bars": len(data),
        "return_pct": round(ret, 1),
        "buy_hold_pct": round(bh, 1),
        "sharpe": round(s["Sharpe Ratio"], 2),
        "max_dd_pct": round(dd, 1),
        "win_rate_pct": round(s["Win Rate [%]"], 1) if s["# Trades"] else None,
        "trades": int(s["# Trades"]),
        "trades_ok": "yes" if s["# Trades"] >= 30 else "NOISE (<30)",
        "exposure_pct": round(s["Exposure Time [%]"], 1),
        "verdict": verdict,
    }


def run_one(ticker: str, strategy_name: str, interval: str, period: str, commission: float) -> dict:
    resolved, data = load_data(ticker, interval, period)
    row = {"ticker": resolved, "strategy": strategy_name}
    row.update(run_bt(data, strategy_name, commission))
    return row


def run_robustness_check(ticker: str, strategy_name: str, interval: str, period: str,
                          commission: float, n_windows: int) -> dict:
    """
    Slice the backtest window into N sequential windows and check what
    fraction the strategy actually beats its benchmark in — adapted from
    the "Robustness Check" (Tier 1 walk-forward) concept in
    github.com/Gabraham4/Intraday-Execution-Analyzer-for-Composer.

    NOTE: this is NOT true out-of-sample walk-forward (that tool's Tier 2
    trains a parameter choice per window then tests on the next unseen
    one). Our strategies have fixed, hand-set rules with nothing to fit —
    so there's no "train" step here, only "does the same fixed rule keep
    working across independent slices of time." Framed honestly as a
    consistency check, not a fitting-validation test.
    """
    resolved, data = load_data(ticker, interval, period)
    days = sorted(pd.Series(data.index.date).drop_duplicates().tolist())
    day_chunks = [c for c in np.array_split(days, n_windows) if len(c) > 0]

    row = {"ticker": resolved, "strategy": strategy_name, "total_days": len(days), "n_windows": len(day_chunks)}
    verdicts = []
    for i, chunk in enumerate(day_chunks, 1):
        mask = (data.index.date >= chunk[0]) & (data.index.date <= chunk[-1])
        try:
            r = run_bt(data[mask], strategy_name, commission)
            row[f"w{i}_verdict"] = r["verdict"]
            row[f"w{i}_return"] = r["return_pct"]
            row[f"w{i}_bh"] = r["buy_hold_pct"]
            row[f"w{i}_trades"] = r["trades"]
            verdicts.append(r["verdict"])
        except Exception as e:
            row[f"w{i}_verdict"] = "ERR"
            verdicts.append("ERR")

    pass_count = sum(1 for v in verdicts if v == "PASS")
    win_rate = pass_count / len(verdicts) if verdicts else 0.0
    row["windows_passed"] = f"{pass_count}/{len(verdicts)}"
    row["win_rate_pct"] = round(win_rate * 100, 1)
    # thresholds match the source repo's Tier 1 labels
    row["consistency"] = "CONSISTENT" if win_rate >= 0.70 else ("EPISODIC" if win_rate >= 0.40 else "OVERFITTED")
    row["holds_up"] = win_rate == 1.0  # strictest bar: PASS in every window, not just most
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", required=True)
    p.add_argument("--strategy", default="all", choices=list(ALL_INTRADAY_STRATEGIES) + ["all"])
    p.add_argument("--interval", default="5m", choices=list(MAX_PERIOD))
    p.add_argument("--period", default=None, help=f"defaults to the max free-tier window for --interval")
    p.add_argument("--commission", type=float, default=0.0003)
    p.add_argument("--walkforward", action="store_true",
                    help="split the window into N sequential windows (see --windows) and check consistency")
    p.add_argument("--windows", type=int, default=2,
                    help="number of sequential windows for --walkforward (default 2 = old first/second-half split; use 3+ for a finer-grained consistency check)")
    args = p.parse_args()
    period = args.period or MAX_PERIOD[args.interval]

    strategies = list(ALL_INTRADAY_STRATEGIES) if args.strategy == "all" else [args.strategy]
    rows = []
    for t in args.tickers:
        for sname in strategies:
            try:
                if args.walkforward:
                    rows.append(run_robustness_check(t, sname, args.interval, period, args.commission, args.windows))
                else:
                    rows.append(run_one(t, sname, args.interval, period, args.commission))
            except Exception as e:
                print(f"  ! {t}/{sname}: {e}")

    if not rows:
        return
    df = pd.DataFrame(rows)
    print(f"\ninterval={args.interval} period={period} commission={args.commission} walkforward={args.walkforward} windows={args.windows if args.walkforward else '-'}")
    print(df.to_string(index=False))
    print("\nRead this table adversarially:")
    if args.walkforward:
        print("- holds_up=True means PASS in EVERY window independently — the strictest bar, expect most rows to fail it.")
        print("- consistency: CONSISTENT (>=70% of windows PASS), EPISODIC (40-70%, regime-dependent), OVERFITTED (<40%).")
        print("- More windows = smaller trade counts per window = individual window verdicts get noisier even as the")
        print("  aggregate consistency signal gets more informative. Check w*_trades before trusting any single window.")
        print("- This is a consistency check across time slices, NOT true walk-forward (no train/fit step — these")
        print("  strategies have fixed rules, nothing is being tuned per window).")
    else:
        print("- PASS = beat buy-and-hold AND made money after costs (not just 'lost less than a falling stock').")
        print("- MIXED = captured most of B&H's move. FAIL = you'd have done better doing nothing.")
        print("- <30 trades means the stats are noise, flagged in trades_ok.")
    print("- Buy&Hold here includes overnight gaps the strategy can't capture by design — see module docstring.")
    print("- This is a NEW, unvalidated track. Do not treat any PASS here as tradeable without walk-forward + paper-trade, same as the daily track.")


if __name__ == "__main__":
    main()
