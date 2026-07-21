"""
Live intraday scan: runs the SAME backtesting.py Strategy class used for
backtesting, then checks whether it opened a trade on the LATEST bar —
same code path as the backtest, so no re-derived/duplicated entry logic
to drift out of sync (that duplication bug bit us once already: this
used to hardcode IntradayMomentum's attribute names and crashed on
ORBBreakout/VWAPBreakout). Works for any strategy in ALL_INTRADAY_STRATEGIES.

probability_of_success is either the strategy's ACTUAL win rate from a
same-window backtest (and only if trades >= 30), or null — never a
made-up confidence number.

Usage:
  python scan_intraday.py --tickers ICICIBANK RELIANCE --capital 100000

NSE tickers don't need the .NS suffix — 'ITC' auto-resolves to 'ITC.NS'
if 'ITC' alone returns no data (see load_data). US tickers like AAPL are
tried as-is first and left unchanged.
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

from strategies_intraday import ALL_INTRADAY_STRATEGIES

warnings.filterwarnings("ignore")

INTERVAL = "5m"
PERIOD = "60d"
CASH = 100_000
COMMISSION = 0.0003
MIN_TRADES_FOR_CONFIDENCE = 30

# NIFTY 500 constituents, sourced from NSE's official list
# (archives.nseindia.com/content/indices/ind_nifty500list.csv) and
# validated against yfinance 2026-07-21 (all 500 resolved cleanly — a
# better hit rate than NIFTY 50's manual list, which had 2 dead symbols).
# Stored as a separate JSON file, not inline, since 500 entries isn't
# readable as a literal list in the module. NOT a live feed: NSE
# rebalances index composition periodically, so this will drift out of
# date eventually — it's a broad, standard universe, not a "trending" or
# "top picks" list. Regenerate by re-downloading the NSE CSV and
# re-validating each symbol against yfinance, same as this was built.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty500_constituents.json")) as _f:
    DEFAULT_UNIVERSE = json.load(_f)


def load_data(ticker: str) -> tuple:
    """
    Try the ticker as given first (so US symbols like AAPL still work
    unchanged). If that returns nothing AND the ticker has no exchange
    suffix already (no '.'), retry with '.NS' appended — covers the
    common case of typing 'ITC' instead of 'ITC.NS' without breaking
    tickers that are correct as-is. Returns (resolved_symbol, data) —
    the resolved symbol is what actually had data and should be used
    everywhere downstream, not the raw input.
    """
    df = yf.Ticker(ticker).history(period=PERIOD, interval=INTERVAL, auto_adjust=True)
    resolved = ticker
    if df.empty and "." not in ticker:
        resolved = f"{ticker}.NS"
        df = yf.Ticker(resolved).history(period=PERIOD, interval=INTERVAL, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data for {ticker} (tried '{ticker}'" +
                          (f" and '{resolved}'" if resolved != ticker else "") + ")")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return resolved, df


def scan_ticker(ticker: str, strategy_name: str, capital: float, risk_pct: float = 1.0) -> dict:
    resolved, data = load_data(ticker)
    return _evaluate_strategy(data, resolved, strategy_name, capital, risk_pct)


def _evaluate_strategy(data: pd.DataFrame, ticker: str, strategy_name: str, capital: float, risk_pct: float) -> dict:
    """Same logic as scan_ticker, but takes already-fetched data — lets
    best_ticker_signal fetch once and reuse it across all 4 strategies
    instead of re-hitting yfinance per strategy."""
    strategy_cls = ALL_INTRADAY_STRATEGIES[strategy_name]

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
        # Brand-new entry on the latest bar.
        recommendation = "BUY"
        t = last
    elif last["ExitBar"] == last_bar and last["EntryBar"] != last_bar:
        # finalize_trades=True force-closes any position still open when the
        # data ends — that's NOT a real exit, it's "you're still holding
        # this right now." Confirm with a second, non-forcing run: a trade
        # only shows up here if it never naturally closed.
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
        # All 4 current intraday strategies pass sl= explicitly, so this
        # path isn't live today — but scan_funds.py hit exactly this gap
        # (3 of 6 daily strategies don't set sl=) and silently showed
        # quantity=0 with no explanation. Fall back to an ATR-based
        # estimate if the strategy at least computed ATR; otherwise say
        # plainly why sizing isn't possible, rather than looking broken.
        for col in trades.columns:
            if col.startswith("Entry_atr") and pd.notna(t[col]):
                stop_loss = entry - 2.0 * t[col]
                stop_loss_estimated = True
                break
        if stop_loss is None:
            sizing_note = (
                f"'{strategy_name}' sets no stop-loss and computes no ATR — "
                "position size can't be derived from risk. quantity=0 "
                "reflects that, not 'no opportunity here.'"
            )
    risk = (entry - stop_loss) if stop_loss is not None else None
    target1 = round(entry + risk, 2) if risk else None
    target2 = round(entry + risk * 1.5, 2) if risk else None

    # Position size by RISK, not by "how much fits" — deploying full capital
    # into one signal ignores stop distance entirely. Standard rule (see
    # Best-Intraday-Trading-Strategies.pdf): risk a fixed % of capital per
    # trade, sized so a stop-out costs exactly that much, not more.
    if risk and risk > 0:
        risk_amount = capital * (risk_pct / 100)
        quantity = min(int(risk_amount // risk), int(capital // entry))
        capital_allocation = round(quantity * entry, 2)
    else:
        risk_amount = None
        quantity = 0
        capital_allocation = 0.0

    # backtesting.py auto-names indicator columns Entry_<funcname(args)>;
    # surface whatever this particular strategy tracked, generically.
    reasons = [
        f"{col.replace('Entry_', '')}: {t[col]:.2f}"
        for col in trades.columns
        if col.startswith("Entry_") and pd.notna(t[col])
    ]

    validated = stats["trades"] >= MIN_TRADES_FOR_CONFIDENCE
    signal = {
        "stock": ticker,
        "symbol": ticker,
        "recommendation": recommendation,
        "entry": round(entry, 2),
        "current_price": round(float(data.Close.iloc[-1]), 2),
        "stop_loss": round(stop_loss, 2) if stop_loss is not None else None,
        "stop_loss_estimated": stop_loss_estimated,
        "target1": target1,
        "target2": target2,
        "target3": None,
        "risk_reward": round((target1 - entry) / risk, 2) if risk else None,
        "sizing_note": sizing_note,
        "probability_of_success": round(stats["win_rate"] / 100, 3) if validated else None,
        "confidence": stats["win_rate"] if validated else None,
        "confidence_status": "backtested_same_window" if validated else "UNVALIDATED (<30 trades in backtest)",
        "capital_allocation": capital_allocation,
        "quantity": quantity,
        "risk_pct": risk_pct,
        "risk_amount": round(risk_amount, 2) if risk_amount is not None else None,
        "entry_time": str(t["EntryTime"]),
        "strategy": strategy_name,
        "reasons": reasons,
        "backtest_context": stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"symbol": ticker, "signals": [signal], "count": 1}


def _rank_key(signal: dict):
    ctx = signal["backtest_context"]
    validated = ctx["trades"] >= MIN_TRADES_FOR_CONFIDENCE
    return (validated, ctx["win_rate"] or 0.0, ctx["return_pct"])


def best_ticker_signal(ticker: str, capital: float, risk_pct: float = 1.0) -> dict:
    """
    Cycle through every strategy in ALL_INTRADAY_STRATEGIES for this ticker
    and return whichever one is CURRENTLY FIRING (fresh entry or open
    position) with the best same-window backtest performance. That
    ranking is still an in-sample number, not proof of edge — none of
    these strategies have passed walk-forward (see CLAUDE.md Evaluation
    history) — so `candidates_considered` is included for transparency
    rather than hiding the comparison behind a single confident pick.

    Data is fetched ONCE and shared across all 4 strategy evaluations
    (each used to re-fetch independently — 4x redundant network calls
    for identical data). The 4 evaluations then run concurrently via
    ThreadPoolExecutor: fan out one thread per strategy, join on all of
    them, combine results — same shape as a countdown latch, just using
    Python's standard fan-out/join primitive instead of a manual one.
    """
    resolved, data = load_data(ticker)  # fetch once, share across all strategies

    with ThreadPoolExecutor(max_workers=len(ALL_INTRADAY_STRATEGIES)) as pool:
        futures = {
            pool.submit(_evaluate_strategy, data, resolved, sname, capital, risk_pct): sname
            for sname in ALL_INTRADAY_STRATEGIES
        }
        results = {futures[fut]: fut.result() for fut in as_completed(futures)}

    fired, candidates = [], []
    for sname in ALL_INTRADAY_STRATEGIES:  # fixed order for stable output, regardless of thread completion order
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
        "symbol": resolved,
        "signals": [best],
        "count": 1,
        "candidates_considered": candidates,
        "selection_method": (
            "Best of currently-firing strategies only, ranked by "
            "(has >=30 same-window backtest trades, then win rate, then return). "
            "This is in-sample performance, NOT walk-forward-validated edge."
        ),
    }


def top_n_signals(n: int = 5, tickers=None, capital: float = 100_000, risk_pct: float = 1.0,
                   max_workers: int = 6, rank_key=None) -> dict:
    """
    Scan a universe of tickers (default: DEFAULT_UNIVERSE), keep only the
    ones with an ACTIVE signal right now (fresh entry or open position —
    same definition as best_ticker_signal), and return the top N ranked
    by same-window backtest performance (or a custom `rank_key(signal)`
    function, e.g. paper_trader.py blends in live paper-trading track
    record — passing a custom key here avoids duplicating this scan/
    threading logic elsewhere).

    Tickers are scanned with BOUNDED concurrency (max_workers, default 6)
    via ThreadPoolExecutor — fully sequential was too slow on the 49-name
    NIFTY 50 universe (~40s), but fully unbounded risks tripping
    yfinance's rate limit on a burst of concurrent requests (see CLAUDE.md
    gotchas). Each ticker still internally threads across its 4
    strategies (best_ticker_signal), so worst case is max_workers x 4
    concurrent requests in flight — keep max_workers modest.

    If fewer than N tickers have an active signal, returns however many
    actually do — never padded with non-signals just to hit N.
    """
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
        "requested_top_n": n,
        "universe_size": len(tickers),
        "active_signals_found": len(hits),
        "top": ranked,
        "errors": errors,
        "selection_method": (
            "Ranked by (has >=30 same-window backtest trades, then win rate, then return) "
            "among tickers with an active signal right now. In-sample performance, "
            "NOT walk-forward-validated edge — see CLAUDE.md Evaluation history."
        ),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", help="omit with --top to scan the default universe instead")
    p.add_argument("--strategy", default=None, choices=list(ALL_INTRADAY_STRATEGIES),
                    help="omit to auto-select the best currently-firing strategy across all of them")
    p.add_argument("--top", type=int, default=None, help="scan the universe (--tickers, or DEFAULT_UNIVERSE if omitted) and return the top N active signals")
    p.add_argument("--capital", type=float, default=100_000)
    p.add_argument("--risk-pct", type=float, default=1.0, help="%% of capital risked per trade (stop-to-entry distance)")
    args = p.parse_args()

    disclaimer = (
        "Research output, not financial advice. This strategy track is "
        "UNVALIDATED (added 2026-07-19, no walk-forward or paper-trade "
        "history yet) — see CLAUDE.md Evaluation history before acting."
    )

    if args.top:
        result = top_n_signals(args.top, args.tickers, args.capital, args.risk_pct)
        result["disclaimer"] = disclaimer
        print(json.dumps(result, indent=2))
        return

    if not args.tickers:
        p.error("--tickers is required unless --top is given")

    for t in args.tickers:
        try:
            if args.strategy:
                result = scan_ticker(t, args.strategy, args.capital, args.risk_pct)
            else:
                result = best_ticker_signal(t, args.capital, args.risk_pct)
        except Exception as e:
            result = {"symbol": t, "error": str(e)}
        result["disclaimer"] = disclaimer
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
