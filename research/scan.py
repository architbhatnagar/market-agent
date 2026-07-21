"""
Daily scanner: runs the SAME strategy classes over recent data and reports
which tickers meet entry conditions as of the latest bar. Because it reuses
the backtested code paths, a signal here means "the rules that produced the
backtest stats fire today" — nothing more, nothing less.

Usage:
  python scan.py --tickers RELIANCE.NS TCS.NS INFY.NS HDFCBANK.NS AAPL MSFT
"""
import argparse
import warnings

import numpy as np
import yfinance as yf

from indicators import sma, ema, rsi, atr, adx, rolling_max

warnings.filterwarnings("ignore")


def scan_ticker(ticker: str) -> list[dict]:
    df = yf.Ticker(ticker).history(period="2y", auto_adjust=True)
    if len(df) < 260:
        return [{"ticker": ticker, "note": "insufficient history"}]
    c, h, l = df["Close"].values, df["High"].values, df["Low"].values
    price = c[-1]
    a = atr(h, l, c, 14)[-1]

    e20, e50, e200 = ema(c, 20)[-1], ema(c, 50)[-1], ema(c, 200)[-1]
    r = rsi(c, 14)[-1]
    x = adx(h, l, c, 14)[-1]
    s20, s50, s200 = sma(c, 20)[-1], sma(c, 50)[-1], sma(c, 200)[-1]
    hh250 = rolling_max(c, 250)[-1]

    signals = []

    def add(strategy, reasons, stop_mult):
        signals.append({
            "ticker": ticker,
            "strategy": strategy,
            "action": "BUY",
            "close": round(price, 2),
            "suggested_stop": round(price - stop_mult * a, 2),
            "reasons": reasons,
        })

    # momentum continuation (daily)
    if x >= 25 and e20 > e50 > e200 and price > e20 and 55 <= r < 78:
        add("momentum", [f"ADX {x:.0f} (>=25)", "bullish EMA stack", f"RSI {r:.0f} in band"], 2.5)

    # sma cross state (fresh cross within ~3 bars)
    ma_f, ma_s = sma(c, 20), sma(c, 50)
    if ma_f[-1] > ma_s[-1] and ma_f[-4] <= ma_s[-4]:
        add("sma_cross", ["20-SMA crossed above 50-SMA recently"], 3.0)

    # mean reversion
    if price > s200 and r < 32:
        add("mean_reversion", [f"above 200-SMA, RSI {r:.0f} oversold"], 2.0)

    # 52w breakout
    if price >= hh250 and not np.isnan(hh250):
        add("breakout", ["close at 250-day high"], 3.0)

    return signals


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", required=True)
    args = p.parse_args()

    any_signal = False
    for t in args.tickers:
        try:
            for sig in scan_ticker(t):
                if "note" in sig:
                    print(f"  - {t}: {sig['note']}")
                    continue
                any_signal = True
                print(f"{sig['ticker']:14s} {sig['strategy']:14s} {sig['action']}  "
                      f"close {sig['close']}  stop {sig['suggested_stop']}  | {'; '.join(sig['reasons'])}")
        except Exception as e:
            print(f"  ! {t}: {e}")
    if not any_signal:
        print("No entry signals today. That's normal — most days have none.")


if __name__ == "__main__":
    main()
