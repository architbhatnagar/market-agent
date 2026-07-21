"""
Backtest runner.

Usage:
  python run_backtest.py --tickers RELIANCE.NS TCS.NS AAPL --strategy momentum --years 8
  python run_backtest.py --tickers TCS.NS --strategy all --years 10

Costs default to Indian delivery trading: ~0.12% per side covers STT,
exchange charges, stamp duty, and slippage (discount-broker delivery
brokerage is zero). Override with --commission for US (e.g. 0.0005).

Verdict logic is deliberately harsh: a strategy must beat buy-and-hold
or have a materially better drawdown to earn a PASS.
"""
import argparse
import warnings

import pandas as pd
import yfinance as yf
from backtesting import Backtest

from strategies import ALL_STRATEGIES

warnings.filterwarnings("ignore")

CASH = 100_000


def load_data(ticker: str, years: int) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=f"{years}y", auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data for {ticker}")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index = df.index.tz_localize(None)
    return df


def run_one(ticker: str, strategy_name: str, years: int, commission: float) -> dict:
    data = load_data(ticker, years)
    bt = Backtest(data, ALL_STRATEGIES[strategy_name], cash=CASH,
                  commission=commission, exclusive_orders=True, finalize_trades=True)
    s = bt.run()
    ret, bh = s["Return [%]"], s["Buy & Hold Return [%]"]
    dd = s["Max. Drawdown [%]"]
    # honest verdict: beat B&H = PASS; >=70% of B&H = MIXED (check drawdown); else FAIL
    verdict = "PASS" if ret > bh else ("MIXED" if ret > 0.7 * bh else "FAIL")
    return {
        "ticker": ticker,
        "strategy": strategy_name,
        "return_pct": round(ret, 1),
        "buy_hold_pct": round(bh, 1),
        "cagr_pct": round(s["CAGR [%]"], 1),
        "sharpe": round(s["Sharpe Ratio"], 2),
        "max_dd_pct": round(dd, 1),
        "win_rate_pct": round(s["Win Rate [%]"], 1) if s["# Trades"] else None,
        "trades": int(s["# Trades"]),
        "exposure_pct": round(s["Exposure Time [%]"], 1),
        "verdict": verdict,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", required=True)
    p.add_argument("--strategy", default="all", choices=list(ALL_STRATEGIES) + ["all"])
    p.add_argument("--years", type=int, default=8)
    p.add_argument("--commission", type=float, default=0.0012)
    args = p.parse_args()

    strategies = list(ALL_STRATEGIES) if args.strategy == "all" else [args.strategy]
    rows = []
    for t in args.tickers:
        for sname in strategies:
            try:
                rows.append(run_one(t, sname, args.years, args.commission))
            except Exception as e:
                print(f"  ! {t}/{sname}: {e}")

    if not rows:
        return
    df = pd.DataFrame(rows)
    print("\n" + df.to_string(index=False))
    print("\nRead this table adversarially:")
    print("- PASS = beat buy-and-hold after costs. MIXED = captured most of it (check drawdown). FAIL = you'd have done better doing nothing.")
    print("- <30 trades means the stats are noise. High exposure + FAIL means paying costs to underperform.")


if __name__ == "__main__":
    main()
