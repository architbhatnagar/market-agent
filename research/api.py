"""
Local signal API — thin HTTP wrapper around scan_intraday.py. Same data,
same rules, same UNVALIDATED disclaimer; this just makes it callable
over HTTP instead of the CLI.

Run:
  uvicorn api:app --reload --port 8000

Call:
  curl "http://localhost:8000/signal/ICICIBANK.NS"
  curl "http://localhost:8000/signal/RELIANCE.NS?strategy=orb_breakout&capital=50000"
"""
import warnings
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query

from scan_intraday import (
    scan_ticker, best_ticker_signal, top_n_signals,
    ALL_INTRADAY_STRATEGIES, DEFAULT_UNIVERSE,
)
from scan_funds import (
    scan_fund, best_fund_signal, top_n_fund_signals,
    ALL_STRATEGIES as ALL_FUND_STRATEGIES, DEFAULT_FUND_UNIVERSE,
)
from paper_trader_daily import generate_report as _daily_nse_report
from paper_trader_daily_us import generate_report as _daily_us_report

warnings.filterwarnings("ignore")

DISCLAIMER = (
    "Research output, not financial advice. No strategy in this repo has "
    "passed walk-forward validation as of 2026-07-20 — see CLAUDE.md "
    "Evaluation history. Every signal here is UNVALIDATED."
)
FUND_DISCLAIMER = (
    DISCLAIMER + " These strategies were built and evaluated on stocks, not "
    "fund NAVs — this combination has never been tested at all before "
    "today. Exit loads are not modeled."
)

app = FastAPI(
    title="market-agent signal API",
    description=(
        "Personal research tool, NOT a trading signal service. "
        "/signal wraps scan_intraday.py (intraday stocks); /fund wraps "
        "scan_funds.py (Indian mutual fund NAVs, daily strategies).\n\n"
        f"**{DISCLAIMER}**"
    ),
    version="0.1.0",
    contact={"name": "market-agent (personal project)"},
)


@app.get(
    "/signal/{symbol}",
    summary="Get today's intraday signal for a symbol",
    description=(
        "Omit `strategy` to cycle through ALL strategies and return "
        "whichever one is CURRENTLY FIRING (fresh entry or open position) "
        "with the best same-window backtest performance — the response "
        "includes `candidates_considered` so the comparison isn't hidden "
        "behind one confident-looking pick. Pass `strategy` to force one "
        "specific strategy instead.\n\n"
        "Either way: runs the backtest over the last 60 days of 5m bars "
        "(yfinance free-tier max at this interval) and checks whether it "
        "opened a trade on the most recent bar. Returns an empty "
        "`signals` list if nothing fired — that's the common case, not "
        "an error. `probability_of_success`/`confidence` are null unless "
        "the strategy logged >=30 trades in that same backtest window."
    ),
    response_description="Signal payload (possibly empty) plus a disclaimer.",
)
def get_signal(
    symbol: str,
    strategy: Optional[str] = Query(
        None,
        description=f"One of: {', '.join(ALL_INTRADAY_STRATEGIES)}. Omit to auto-select the best currently-firing strategy. See GET /strategies.",
    ),
    capital: float = Query(100_000, gt=0, description="Total capital available, in rupees."),
    risk_pct: float = Query(
        1.0, gt=0, le=10,
        description="Percent of capital risked per trade (loss if stopped out). Position size is derived from this and the stop distance, not from 'how much fits'.",
    ),
):
    if strategy is not None and strategy not in ALL_INTRADAY_STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown strategy '{strategy}'. Choices: {list(ALL_INTRADAY_STRATEGIES)}",
        )
    try:
        if strategy:
            result = scan_ticker(symbol, strategy, capital, risk_pct)
        else:
            result = best_ticker_signal(symbol, capital, risk_pct)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    result["disclaimer"] = DISCLAIMER
    return result


@app.get(
    "/strategies",
    summary="List available intraday strategies",
    description="Strategy names accepted by the `strategy` param on GET /signal/{symbol}.",
)
def list_strategies():
    return {"strategies": list(ALL_INTRADAY_STRATEGIES), "disclaimer": DISCLAIMER}


@app.get(
    "/top-stocks",
    summary="Top N stocks with an active intraday signal right now",
    description=(
        "Scans a universe of tickers (default: DEFAULT_UNIVERSE, the NIFTY "
        "50 constituents — manually curated, verified against yfinance, "
        "NOT a live/rebalanced feed, will drift as the index changes; "
        "pass `tickers` one or more times to use your own list instead), "
        "keeps only the ones with an ACTIVE signal right now (fresh entry "
        "or open position, auto-selecting the best strategy per ticker "
        "same as GET /signal), ranks by same-window backtest performance, "
        "and returns the top N. Never padded — if fewer than N tickers "
        "have an active signal, you get however many actually do.\n\n"
        "Scans sequentially (not parallel across tickers) to avoid "
        "hammering yfinance, so this is SLOW — expect ~1s per ticker in "
        "the universe (~50s for the default ~49-name universe). Threading "
        "still happens within each ticker across its 4 strategies. Pass "
        "your own smaller `tickers` list for faster responses. Scanned "
        "with bounded concurrency (`max_workers`, default 6) — higher "
        "values are faster but risk tripping yfinance's rate limit on a "
        "burst of concurrent requests."
    ),
    response_description="Ranked top-N signals plus a disclaimer.",
)
def get_top_stocks(
    n: int = Query(5, gt=0, le=20, description="How many top signals to return."),
    tickers: Optional[List[str]] = Query(
        None, description=f"Universe to scan. Repeat the param to pass several, e.g. ?tickers=RELIANCE&tickers=TCS. Defaults to: {DEFAULT_UNIVERSE}"
    ),
    capital: float = Query(100_000, gt=0, description="Total capital available, in rupees."),
    risk_pct: float = Query(1.0, gt=0, le=10, description="Percent of capital risked per trade."),
    max_workers: int = Query(6, gt=0, le=15, description="Concurrent tickers scanned at once. Higher = faster but more load on yfinance."),
):
    result = top_n_signals(n, tickers, capital, risk_pct, max_workers)
    result["disclaimer"] = DISCLAIMER
    return result


@app.get(
    "/top-funds",
    summary="Top N mutual funds with an active signal right now",
    description=(
        "Same as GET /top-stocks, but for Indian mutual fund NAVs (see "
        "GET /fund/{query} for the caveats on the daily strategy library "
        "reused here). If `funds` is omitted, the universe is DISCOVERED "
        "LIVE from mfapi.in's full scheme catalog (~37,000+ schemes) — "
        "not a hardcoded list — filtered to Direct Plan + Growth option "
        "in mainstream equity categories (flexicap/largecap/midcap/"
        "smallcap/index/ELSS), capped at `discover_limit`. mfapi.in has "
        "no AUM/popularity data, so this is a broad discovered universe, "
        "NOT a ranking by fund size — falls back to a small fixed list "
        "only if mfapi.in's catalog endpoint is unreachable. Pass `funds` "
        "one or more times (scheme names or numeric codes) to use your "
        "own list instead.\n\n"
        "Scanned with bounded concurrency (`max_workers`, default 6)."
    ),
    response_description="Ranked top-N signals plus a disclaimer.",
)
def get_top_funds(
    n: int = Query(5, gt=0, le=20, description="How many top signals to return."),
    funds: Optional[List[str]] = Query(
        None, description="Universe to scan (scheme names or numeric codes). Repeat the param to pass several. Omit to live-discover from mfapi.in's catalog instead."
    ),
    capital: float = Query(100_000, gt=0, description="Total capital available, in rupees."),
    risk_pct: float = Query(1.0, gt=0, le=10, description="Percent of capital risked per trade."),
    max_workers: int = Query(6, gt=0, le=15, description="Concurrent funds scanned at once."),
    discover_limit: int = Query(20, gt=0, le=50, description="Max funds to pull from live discovery when `funds` is omitted."),
):
    result = top_n_fund_signals(n, funds, capital, risk_pct, max_workers, discover_limit)
    result["disclaimer"] = FUND_DISCLAIMER
    return result


@app.get(
    "/fund/{query}",
    summary="Get today's signal for an Indian mutual fund",
    description=(
        "`query` is either a scheme name to search (e.g. 'Parag Parikh Flexi "
        "Cap' — mfapi.in fuzzy search, first match wins, may pick the wrong "
        "share class like Regular vs Direct, always check `scheme_name` in "
        "the response) or a numeric mfapi.in scheme code directly.\n\n"
        "Omit `strategy` to cycle through all 6 DAILY strategies (sma_cross, "
        "momentum, mean_reversion, breakout, clenow_trend, zscore_reversion "
        "— the same library used for stocks, reused as-is since funds "
        "publish one NAV per day) and return whichever is CURRENTLY FIRING "
        "with the best same-window backtest performance, with "
        "`candidates_considered` for transparency. Pass `strategy` to force "
        "one.\n\n"
        "mfapi.in gives NAV only (no OHLC/Volume) — Open=High=Low=Close=NAV "
        "is synthesized so the existing Strategy classes work unmodified; "
        "ATR/ADX degenerate to a close-to-close volatility proxy as a result."
    ),
    response_description="Signal payload (possibly empty) plus a disclaimer.",
)
def get_fund_signal(
    query: str,
    strategy: Optional[str] = Query(
        None,
        description=f"One of: {', '.join(ALL_FUND_STRATEGIES)}. Omit to auto-select the best currently-firing strategy.",
    ),
    capital: float = Query(100_000, gt=0, description="Total capital available, in rupees."),
    risk_pct: float = Query(1.0, gt=0, le=10, description="Percent of capital risked per trade."),
):
    if strategy is not None and strategy not in ALL_FUND_STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown strategy '{strategy}'. Choices: {list(ALL_FUND_STRATEGIES)}",
        )
    try:
        if strategy:
            result = scan_fund(query, strategy, capital, risk_pct)
        else:
            result = best_fund_signal(query, capital, risk_pct)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    result["disclaimer"] = FUND_DISCLAIMER
    return result


@app.get(
    "/paper/daily/allocation",
    summary="Current paper-trading allocation — NSE daily/swing stocks",
    description=(
        "Reads the live state from paper_trader_daily.py (run `python "
        "paper_trader_daily.py --run` periodically, e.g. once/day, to "
        "actually trade against live signals — this endpoint only "
        "reports whatever that process has already done, it does not "
        "trigger a new trading cycle itself). Includes current equity, "
        "cash, realized/unrealized PnL, open positions marked to "
        "market, and per-strategy breakdown. ₹, not $."
    ),
)
def get_daily_allocation():
    return _daily_nse_report()


@app.get(
    "/paper/daily-us/allocation",
    summary="Current paper-trading allocation — US (S&P 500) daily/swing stocks",
    description=(
        "Same as GET /paper/daily/allocation but for paper_trader_daily_us.py "
        "— run `python paper_trader_daily_us.py --run` periodically to "
        "actually trade; this endpoint only reports state. $ USD, not ₹ "
        "— a separate, non-FX-converted capital pool from the NSE tracker."
    ),
)
def get_daily_us_allocation():
    return _daily_us_report()


@app.get(
    "/paper/allocation",
    summary="Current paper-trading allocation — NSE daily + US daily, combined",
    description=(
        "Both GET /paper/daily/allocation and GET /paper/daily-us/allocation "
        "in one call, under `nse_daily` (₹) and `us_daily` ($) — two "
        "separate capital pools, not combined into one number since "
        "they're different currencies. Intraday (paper_trader.py) and "
        "mutual funds (paper_trader_funds.py) aren't included here yet — "
        "ask if you want those added too."
    ),
)
def get_combined_allocation():
    return {"nse_daily": _daily_nse_report(), "us_daily": _daily_us_report()}
