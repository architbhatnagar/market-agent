"""
Live mutual fund scan: same pattern as scan_intraday.py, but for Indian
MF NAVs (mfapi.in / AMFI data) via the DAILY strategy library
(ALL_STRATEGIES in strategies.py) instead of the intraday one — funds
publish one NAV per day, so "intraday" has no meaning here.

mfapi.in gives NAV only, no OHLC/Volume. Synthesized Open=High=Low=Close
=NAV, Volume=1 so the existing backtesting.py Strategy classes (built
for OHLC data) work completely unmodified — no new strategy code, same
reuse-not-reinvent approach as everything else in this repo. Side effect:
ATR/ADX degenerate to a close-to-close volatility proxy since there's no
real intraday High/Low — a rougher signal than on stocks, not a bug.

probability_of_success is the strategy's ACTUAL win rate from a
same-window backtest (only if trades >= 30), same honesty rule as the
intraday track — never a made-up confidence number.

Usage:
  python scan_funds.py --funds "Parag Parikh Flexi Cap" 122639 --capital 100000
  (accepts either a scheme name to search, or a numeric scheme code directly)
"""
import argparse
import json
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import requests
from backtesting import Backtest

from strategies import ALL_STRATEGIES

warnings.filterwarnings("ignore")

MFAPI_BASE = "https://api.mfapi.in/mf"
CASH = 100_000
COMMISSION = 0.0  # direct-plan MFs have no per-trade brokerage; exit loads (~1% if redeemed <1yr on equity funds) are NOT modeled
MIN_TRADES_FOR_CONFIDENCE = 30
MIN_BARS_REQUIRED = 260  # breakout/mean_reversion need 200+ bars; leave headroom

# Emergency fallback ONLY — used if discover_fund_universe() can't reach
# mfapi.in's full catalog endpoint. Not the primary universe source
# anymore (see discover_fund_universe / get_default_fund_universe below).
_FALLBACK_FUND_UNIVERSE = [
    "122639",  # Parag Parikh Flexi Cap Fund - Direct Plan - Growth
    "118955",  # HDFC Flexi Cap Fund - Growth Option - Direct Plan
    "118825",  # Mirae Asset Large Cap Fund - Direct Plan - Growth
    "120586",  # ICICI Prudential Large Cap Fund - Direct Plan - Growth
    "120166",  # Kotak Flexicap Fund - Growth - Direct
    "125354",  # Axis Small Cap Fund - Direct Plan - Growth
    "118778",  # Nippon India Small Cap Fund - Direct Plan Growth Plan - Growth Option
    "120828",  # quant Small Cap Fund - Growth Option - Direct Plan
    "146130",  # Canara Robeco Small Cap Fund - Direct Plan - Growth Option
    "120716",  # UTI Nifty 50 Index Fund - Growth Option - Direct
]
DEFAULT_FUND_UNIVERSE = _FALLBACK_FUND_UNIVERSE  # kept for backward compat with existing imports

DEFAULT_CATEGORY_KEYWORDS = [
    "Flexi Cap", "Large Cap", "Large & Mid Cap", "Mid Cap", "Small Cap",
    "Bluechip", "Multi Cap", "Nifty 50", "Nifty Next 50", "ELSS",
]

_all_schemes_cache = None  # module-level cache: fetched once per process, not once per request


def _get_all_schemes() -> list:
    """Full mfapi.in scheme catalog (~37,000+ entries, code+name only, no
    AUM/category/performance data — that's the ceiling of what this free
    API offers). Cached in-process since it's a ~15s fetch and doesn't
    change within a single run."""
    global _all_schemes_cache
    if _all_schemes_cache is None:
        r = requests.get(MFAPI_BASE, timeout=30)
        r.raise_for_status()
        _all_schemes_cache = r.json()
    return _all_schemes_cache


def discover_fund_universe(category_keywords=None, limit: int = 20) -> list:
    """
    Dynamically discovers scheme codes from mfapi.in's live catalog
    instead of a hand-picked list — filters to Direct Plan + Growth
    option (skips IDCW/Dividend/Bonus variants of the same fund) and by
    category keyword (default: mainstream equity categories) to avoid
    scanning random debt/liquid/close-ended funds. mfapi.in has no
    AUM/popularity data, so this can't be a true "top funds by size" —
    it's a broad, algorithmically-filtered universe, not a curated pick.
    Order is whatever mfapi.in's catalog order happens to be (roughly by
    scheme code / registration order) — not a ranking.
    """
    keywords = category_keywords or DEFAULT_CATEGORY_KEYWORDS
    schemes = _get_all_schemes()

    matches = []
    for s in schemes:
        name = s.get("schemeName", "")
        if "Direct" not in name or "Growth" not in name:
            continue
        if "IDCW" in name or "Dividend" in name or "Bonus" in name:
            continue
        if not any(k.lower() in name.lower() for k in keywords):
            continue
        matches.append(str(s["schemeCode"]))
        if len(matches) >= limit:
            break
    return matches


def get_default_fund_universe(limit: int = 20) -> list:
    """Live-discovered universe, falling back to the small hardcoded
    list only if mfapi.in's catalog endpoint is unreachable/slow."""
    try:
        discovered = discover_fund_universe(limit=limit)
        return discovered if discovered else _FALLBACK_FUND_UNIVERSE
    except Exception:
        return _FALLBACK_FUND_UNIVERSE


def load_nav_data(query: str) -> tuple:
    """Accepts a scheme name (searched via mfapi.in) or a numeric scheme
    code directly. Returns (scheme_code, scheme_name, ohlc_data)."""
    q = query.strip()
    if q.isdigit():
        code = int(q)
    else:
        r = requests.get(f"{MFAPI_BASE}/search", params={"q": q}, timeout=15)
        r.raise_for_status()
        results = r.json()
        if not results:
            raise ValueError(f"No mutual fund scheme found matching '{query}'")
        code = results[0]["schemeCode"]

    r = requests.get(f"{MFAPI_BASE}/{code}", timeout=15)
    r.raise_for_status()
    payload = r.json()
    navs = payload.get("data", [])
    if len(navs) < MIN_BARS_REQUIRED:
        raise ValueError(f"Scheme {code} has only {len(navs)} NAV points, need >={MIN_BARS_REQUIRED}")

    scheme_name = payload.get("meta", {}).get("scheme_name", str(code))
    df = pd.DataFrame(navs)
    df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
    df["nav"] = df["nav"].astype(float)
    df = df.sort_values("date").set_index("date")

    ohlc = pd.DataFrame({
        "Open": df["nav"], "High": df["nav"], "Low": df["nav"], "Close": df["nav"], "Volume": 1,
    })
    return code, scheme_name, ohlc


def _fallback_atr_stop(t: pd.Series, trades: pd.DataFrame, entry: float, atr_mult: float = 2.0):
    """
    Not every strategy in ALL_STRATEGIES attaches an explicit sl= to its
    orders (breakout manages its own manual trailing stop, clenow_trend
    sizes by a risk-factor formula, zscore_reversion has no price-stop
    concept at all — only 3 of 6 pass sl=). Without this fallback,
    those strategies silently sized to quantity=0 with no explanation,
    which looked like a real "no position" rather than "can't size."
    If the strategy at least computed ATR as an indicator (breakout and
    clenow_trend both do), use entry - atr_mult*ATR as an ESTIMATED stop
    for sizing purposes only — not the strategy's actual risk control.
    Returns (stop_loss_or_None, is_estimated).
    """
    for col in trades.columns:
        if col.startswith("Entry_atr") and pd.notna(t[col]):
            return entry - atr_mult * t[col], True
    return None, False


def _evaluate_fund_strategy(data: pd.DataFrame, scheme_code: int, scheme_name: str,
                             strategy_name: str, capital: float, risk_pct: float) -> dict:
    """Mirrors scan_intraday.py's _evaluate_strategy: reads the fired
    signal off the actual backtest trade log (fresh entry on the latest
    bar, or a still-open position via the finalize_trades=False check),
    not a re-derived copy of each strategy's rule."""
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
        return {"scheme_code": scheme_code, "scheme_name": scheme_name, "signals": [], "count": 0}

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
            return {"scheme_code": scheme_code, "scheme_name": scheme_name, "signals": [], "count": 0}
        recommendation = "HOLD (already in position)"
        t = last
    else:
        return {"scheme_code": scheme_code, "scheme_name": scheme_name, "signals": [], "count": 0}

    entry = float(t["EntryPrice"])
    stop_loss = float(t["SL"]) if pd.notna(t["SL"]) else None
    stop_loss_estimated = False
    sizing_note = None
    if stop_loss is None:
        stop_loss, stop_loss_estimated = _fallback_atr_stop(t, trades, entry)
        if stop_loss is None:
            sizing_note = (
                f"'{strategy_name}' sets no stop-loss and computes no ATR — "
                "this strategy has no price-based risk control at all "
                "(exits on its own signal, e.g. z-score), so position size "
                "can't be derived from risk. quantity=0 reflects that, not "
                "'no opportunity here.'"
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
        "scheme_code": scheme_code,
        "scheme_name": scheme_name,
        "recommendation": recommendation,
        "entry_nav": round(entry, 4),
        "current_nav": round(float(data.Close.iloc[-1]), 4),
        "stop_loss_nav": round(stop_loss, 4) if stop_loss is not None else None,
        "stop_loss_estimated": stop_loss_estimated,
        "target1_nav": target1,
        "target2_nav": target2,
        "risk_reward": round((target1 - entry) / risk, 2) if risk else None,
        "sizing_note": sizing_note,
        "probability_of_success": round(stats["win_rate"] / 100, 3) if validated else None,
        "confidence": stats["win_rate"] if validated else None,
        "confidence_status": "backtested_same_window" if validated else "UNVALIDATED (<30 trades in backtest)",
        "capital_allocation": capital_allocation,
        "units": quantity,
        "risk_pct": risk_pct,
        "risk_amount": round(risk_amount, 2) if risk_amount is not None else None,
        "entry_date": str(t["EntryTime"].date()),
        "strategy": strategy_name,
        "reasons": reasons,
        "backtest_context": stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"scheme_code": scheme_code, "scheme_name": scheme_name, "signals": [signal], "count": 1}


def scan_fund(query: str, strategy_name: str, capital: float, risk_pct: float = 1.0) -> dict:
    code, name, data = load_nav_data(query)
    return _evaluate_fund_strategy(data, code, name, strategy_name, capital, risk_pct)


def _rank_key(signal: dict):
    ctx = signal["backtest_context"]
    validated = ctx["trades"] >= MIN_TRADES_FOR_CONFIDENCE
    return (validated, ctx["win_rate"] or 0.0, ctx["return_pct"])


def best_fund_signal(query: str, capital: float, risk_pct: float = 1.0) -> dict:
    """
    Cycle through every strategy in ALL_STRATEGIES (the daily library —
    sma_cross, momentum, mean_reversion, breakout, clenow_trend,
    zscore_reversion) for this fund and return whichever is CURRENTLY
    FIRING with the best same-window backtest performance. Same fan-out/
    join pattern as best_ticker_signal in scan_intraday.py: fetch once,
    evaluate all strategies concurrently via ThreadPoolExecutor.

    NOTE: none of these strategies have passed walk-forward validation
    on stocks (see CLAUDE.md Evaluation history), and they have NEVER
    been tested on fund NAV data at all — this is a new, unvalidated
    combination. clenow_trend in particular is meant to be judged as a
    portfolio strategy across many names, not a single fund; treat its
    single-fund output here with extra skepticism.
    """
    code, name, data = load_nav_data(query)

    with ThreadPoolExecutor(max_workers=len(ALL_STRATEGIES)) as pool:
        futures = {
            pool.submit(_evaluate_fund_strategy, data, code, name, sname, capital, risk_pct): sname
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
        return {"scheme_code": code, "scheme_name": name, "signals": [], "count": 0, "candidates_considered": candidates}

    best = max(fired, key=_rank_key)
    return {
        "scheme_code": code,
        "scheme_name": name,
        "signals": [best],
        "count": 1,
        "candidates_considered": candidates,
        "selection_method": (
            "Best of currently-firing strategies only, ranked by "
            "(has >=30 same-window backtest trades, then win rate, then return). "
            "This is in-sample performance, NOT walk-forward-validated edge — "
            "and these strategies have never been tested on fund NAV data before today."
        ),
    }


def top_n_fund_signals(n: int = 5, funds=None, capital: float = 100_000, risk_pct: float = 1.0,
                        max_workers: int = 6, discover_limit: int = 20, rank_key=None) -> dict:
    """
    Scan a universe of funds, keep only the ones with an ACTIVE signal
    right now, and return the top N ranked by same-window backtest
    performance (or a custom `rank_key(signal)` function — e.g.
    paper_trader_funds.py blends in live paper-trading track record;
    passing a custom key here avoids duplicating this scan/threading
    logic elsewhere, same pattern as scan_intraday.top_n_signals).
    If `funds` isn't given, the universe is DISCOVERED live from
    mfapi.in's full scheme catalog (get_default_fund_universe) — not a
    hardcoded list — filtered to Direct/Growth plans in mainstream
    equity categories, falling back to a small fixed list only if
    mfapi.in's catalog endpoint is unreachable.

    Bounded concurrency across funds (max_workers, default 6) via
    ThreadPoolExecutor — each fund still internally threads across its 6
    strategies (best_fund_signal), so worst case is max_workers x 6
    concurrent requests; keep this modest.

    If fewer than N funds have an active signal, returns however many
    actually do — never padded with non-signals just to hit N.
    """
    funds = funds or get_default_fund_universe(limit=discover_limit)
    rank_key = rank_key or _rank_key
    hits, errors = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(best_fund_signal, q, capital, risk_pct): q for q in funds}
        for fut in as_completed(futures):
            q = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                errors.append({"query": q, "error": str(e)})
                continue
            if result["count"]:
                hits.append(result["signals"][0])

    ranked = sorted(hits, key=rank_key, reverse=True)[:n]
    return {
        "requested_top_n": n,
        "universe_size": len(funds),
        "active_signals_found": len(hits),
        "top": ranked,
        "errors": errors,
        "selection_method": (
            "Ranked by (has >=30 same-window backtest trades, then win rate, then return) "
            "among funds with an active signal right now. In-sample performance, "
            "NOT walk-forward-validated edge — and never tested on fund NAV data before today."
        ),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--funds", nargs="+", help="scheme name(s) to search, or numeric scheme code(s); omit with --top to scan the default universe")
    p.add_argument("--strategy", default=None, choices=list(ALL_STRATEGIES),
                    help="omit to auto-select the best currently-firing strategy across all of them")
    p.add_argument("--top", type=int, default=None, help="scan the universe (--funds, or live-discovered from mfapi.in's catalog if omitted) and return the top N active signals")
    p.add_argument("--discover-limit", type=int, default=20, help="max funds to pull from live discovery when --funds is omitted")
    p.add_argument("--capital", type=float, default=100_000)
    p.add_argument("--risk-pct", type=float, default=1.0)
    args = p.parse_args()

    disclaimer = (
        "Research output, not financial advice. These strategies were built and "
        "evaluated on stocks, not fund NAVs — this combination is UNVALIDATED "
        "in every sense (no walk-forward, no fund-specific testing). "
        "Exit loads are not modeled. See CLAUDE.md Evaluation history."
    )

    if args.top:
        result = top_n_fund_signals(args.top, args.funds, args.capital, args.risk_pct,
                                     discover_limit=args.discover_limit)
        result["disclaimer"] = disclaimer
        print(json.dumps(result, indent=2, default=str))
        return

    if not args.funds:
        p.error("--funds is required unless --top is given")

    for q in args.funds:
        try:
            if args.strategy:
                result = scan_fund(q, args.strategy, args.capital, args.risk_pct)
            else:
                result = best_fund_signal(q, args.capital, args.risk_pct)
        except Exception as e:
            result = {"query": q, "error": str(e)}
        result["disclaimer"] = disclaimer
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
