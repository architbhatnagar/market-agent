"""
Live daily-stock scan for US equities — mirrors scan_daily.py (mirrored
not shared, same reasoning as that file/scan_funds.py: different
universe, different commission profile, not worth forcing through one
module). Same DAILY strategy library (ALL_STRATEGIES in strategies.py),
same trade-log-based rigor, same reuse-not-reinvent approach.

Universe: S&P 500, sourced from Wikipedia's constituent table and
validated against yfinance 2026-07-21 (503/503 resolved cleanly — share
classes like BRK.B converted to Yahoo's dash format, BRK-B). NOT a live
feed — will drift as S&P 500 rebalances; regenerate by re-scraping and
re-validating, same process as this was built (see git history / ask
for the build script if regenerating).

Commission defaults to 0.0005 (~0.05%), matching the "US runs can use
~0.0005" note already in run_backtest.py/CLAUDE.md — near-zero-commission
US brokers plus a small allowance for spread, vs India's 0.0012 (STT +
charges). This is an approximation, not a real fee schedule — override
with --commission if you have a more precise number for your broker.

Usage:
  python scan_daily_us.py --tickers AAPL MSFT --capital 100000
  python scan_daily_us.py --top 10
"""
import argparse
import json
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from backtesting import Backtest

from strategies import ALL_STRATEGIES

warnings.filterwarnings("ignore")

YEARS = 10
CASH = 100_000
COMMISSION = 0.0005  # US approximation — see module docstring
MIN_TRADES_FOR_CONFIDENCE = 30
MIN_BARS_REQUIRED = 260

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sp500_constituents.json")) as _f:
    DEFAULT_UNIVERSE = json.load(_f)


def load_data(ticker: str, years: int = YEARS) -> tuple:
    """S&P 500 symbols are already in correct Yahoo format after
    validation, so the .NS fallback (kept for consistency with
    scan_daily.py) should rarely if ever trigger here."""
    df = yf.Ticker(ticker).history(period=f"{years}y", auto_adjust=True)
    resolved = ticker
    if df.empty and "." not in ticker:
        resolved = f"{ticker}.NS"
        df = yf.Ticker(resolved).history(period=f"{years}y", auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data for {ticker}" + (f" (tried '{ticker}' and '{resolved}')" if resolved != ticker else ""))
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if len(df) < MIN_BARS_REQUIRED:
        raise ValueError(f"{resolved} has only {len(df)} daily bars, need >={MIN_BARS_REQUIRED}")
    return resolved, df


def _fallback_atr_stop(t: pd.Series, trades: pd.DataFrame, entry: float, atr_mult: float = 2.0):
    for col in trades.columns:
        if col.startswith("Entry_atr") and pd.notna(t[col]):
            return entry - atr_mult * t[col], True
    return None, False


def _evaluate_strategy(data: pd.DataFrame, ticker: str, strategy_name: str, capital: float, risk_pct: float) -> dict:
    strategy_cls = ALL_STRATEGIES[strategy_name]
    bt = Backtest(data, strategy_cls, cash=CASH, commission=COMMISSION,
                  exclusive_orders=True, finalize_trades=True)
    s = bt.run()
    trades = s["_trades"]
    trade_count = int(s["# Trades"])
    stats = {
        "win_rate": round(s["Win Rate [%]"], 1) if trade_count else None,
        "trades": trade_count,
        "return_pct": round(s["Return [%]"], 1),
        "buy_hold_pct": round(s["Buy & Hold Return [%]"], 1),
    }

    if len(trades) == 0:
        return {"symbol": ticker, "signals": [], "count": 0}

    last_bar = len(data) - 1
    last = trades.iloc[-1]

    if last["EntryBar"] == last_bar:
        recommendation = "BUY"
        t = last
    elif last["ExitBar"] == last_bar and last["EntryBar"] != last_bar:
        bt_check = Backtest(data, strategy_cls, cash=CASH, commission=COMMISSION,
                             exclusive_orders=True, finalize_trades=False)
        trades_open_only = bt_check.run()["_trades"]
        still_open = (
            len(trades_open_only) < len(trades)
            or len(trades_open_only) == 0
            or trades_open_only.iloc[-1]["EntryBar"] != last["EntryBar"]
        )
        if not still_open:
            return {"symbol": ticker, "signals": [], "count": 0}
        recommendation = "HOLD (already in position)"
        t = last
    else:
        return {"symbol": ticker, "signals": [], "count": 0}

    entry = float(t["EntryPrice"])
    stop_loss = float(t["SL"]) if pd.notna(t["SL"]) else None
    stop_loss_estimated = False
    sizing_note = None
    if stop_loss is None:
        stop_loss, stop_loss_estimated = _fallback_atr_stop(t, trades, entry)
        if stop_loss is None:
            sizing_note = (
                f"'{strategy_name}' sets no stop-loss and computes no ATR — "
                "position size can't be derived from risk. quantity=0 "
                "reflects that, not 'no opportunity here.'"
            )
    risk = (entry - stop_loss) if stop_loss is not None else None
    target1 = round(entry + risk, 2) if risk else None
    target2 = round(entry + risk * 1.5, 2) if risk else None

    if risk and risk > 0:
        risk_amount = capital * (risk_pct / 100)
        quantity = min(int(risk_amount // risk), int(capital // entry))
        capital_allocation = round(quantity * entry, 2)
    else:
        risk_amount = None
        quantity = 0
        capital_allocation = 0.0

    reasons = [
        f"{col.replace('Entry_', '')}: {t[col]:.2f}"
        for col in trades.columns
        if col.startswith("Entry_") and pd.notna(t[col])
    ]

    validated = stats["trades"] >= MIN_TRADES_FOR_CONFIDENCE
    signal = {
        "stock": ticker, "symbol": ticker, "recommendation": recommendation,
        "entry": round(entry, 2), "current_price": round(float(data.Close.iloc[-1]), 2),
        "stop_loss": round(stop_loss, 2) if stop_loss is not None else None,
        "stop_loss_estimated": stop_loss_estimated,
        "target1": target1, "target2": target2, "target3": None,
        "risk_reward": round((target1 - entry) / risk, 2) if risk else None,
        "sizing_note": sizing_note,
        "probability_of_success": round(stats["win_rate"] / 100, 3) if validated else None,
        "confidence": stats["win_rate"] if validated else None,
        "confidence_status": "backtested_same_window" if validated else "UNVALIDATED (<30 trades in backtest)",
        "capital_allocation": capital_allocation, "quantity": quantity,
        "risk_pct": risk_pct, "risk_amount": round(risk_amount, 2) if risk_amount is not None else None,
        "entry_time": str(t["EntryTime"]), "strategy": strategy_name,
        "reasons": reasons, "backtest_context": stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"symbol": ticker, "signals": [signal], "count": 1}


def scan_ticker(ticker: str, strategy_name: str, capital: float, risk_pct: float = 1.0) -> dict:
    resolved, data = load_data(ticker)
    return _evaluate_strategy(data, resolved, strategy_name, capital, risk_pct)


def _rank_key(signal: dict):
    ctx = signal["backtest_context"]
    validated = ctx["trades"] >= MIN_TRADES_FOR_CONFIDENCE
    return (validated, ctx["win_rate"] or 0.0, ctx["return_pct"])


def best_ticker_signal(ticker: str, capital: float, risk_pct: float = 1.0) -> dict:
    resolved, data = load_data(ticker)
    with ThreadPoolExecutor(max_workers=len(ALL_STRATEGIES)) as pool:
        futures = {
            pool.submit(_evaluate_strategy, data, resolved, sname, capital, risk_pct): sname
            for sname in ALL_STRATEGIES
        }
        results = {futures[fut]: fut.result() for fut in as_completed(futures)}

    fired, candidates = [], []
    for sname in ALL_STRATEGIES:
        result = results[sname]
        if result["count"]:
            sig = result["signals"][0]
            fired.append(sig)
            ctx = sig["backtest_context"]
            candidates.append({
                "strategy": sname, "fired": True, "recommendation": sig["recommendation"],
                "same_window_win_rate": ctx["win_rate"], "same_window_return_pct": ctx["return_pct"],
                "trades": ctx["trades"],
            })
        else:
            candidates.append({"strategy": sname, "fired": False})

    if not fired:
        return {"symbol": resolved, "signals": [], "count": 0, "candidates_considered": candidates}

    best = max(fired, key=_rank_key)
    return {
        "symbol": resolved, "signals": [best], "count": 1, "candidates_considered": candidates,
        "selection_method": (
            "Best of currently-firing strategies only, ranked by "
            "(has >=30 same-window backtest trades, then win rate, then return). "
            "This is in-sample performance, NOT walk-forward-validated edge — this "
            "daily library was walk-forward tested only on NSE names (0-for-7); "
            "never tested on US stocks before today."
        ),
    }


def top_n_signals(n: int = 5, tickers=None, capital: float = 100_000, risk_pct: float = 1.0,
                   max_workers: int = 6, rank_key=None) -> dict:
    tickers = tickers or DEFAULT_UNIVERSE
    rank_key = rank_key or _rank_key
    hits, errors = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(best_ticker_signal, t, capital, risk_pct): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                errors.append({"symbol": t, "error": str(e)})
                continue
            if result["count"]:
                hits.append(result["signals"][0])

    ranked = sorted(hits, key=rank_key, reverse=True)[:n]
    return {
        "requested_top_n": n, "universe_size": len(tickers), "active_signals_found": len(hits),
        "top": ranked, "errors": errors,
        "selection_method": (
            "Ranked by (has >=30 same-window backtest trades, then win rate, then return) "
            "among tickers with an active signal right now."
        ),
    }


def main():
    global COMMISSION
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", help="omit with --top to scan the default universe")
    p.add_argument("--strategy", default=None, choices=list(ALL_STRATEGIES),
                    help="omit to auto-select the best currently-firing strategy")
    p.add_argument("--top", type=int, default=None)
    p.add_argument("--capital", type=float, default=100_000)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--commission", type=float, default=COMMISSION)
    args = p.parse_args()
    COMMISSION = args.commission

    disclaimer = (
        "Research output, not financial advice. This daily strategy library was "
        "walk-forward tested on NSE names (0-for-7) — never tested on US stocks "
        "before today. UNVALIDATED in every sense on this universe."
    )

    if args.top:
        result = top_n_signals(args.top, args.tickers, args.capital, args.risk_pct)
        result["disclaimer"] = disclaimer
        print(json.dumps(result, indent=2, default=str))
        return

    if not args.tickers:
        p.error("--tickers is required unless --top is given")

    for t in args.tickers:
        try:
            result = scan_ticker(t, args.strategy, args.capital, args.risk_pct) if args.strategy else best_ticker_signal(t, args.capital, args.risk_pct)
        except Exception as e:
            result = {"symbol": t, "error": str(e)}
        result["disclaimer"] = disclaimer
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
