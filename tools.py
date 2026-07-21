"""
Market data tools. All free sources:
- yfinance: US stocks/ETFs + Indian stocks (.NS / .BO suffixes)
- mfapi.in: Indian mutual fund NAVs (AMFI data)
"""
import json
import requests
import yfinance as yf

from portfolio import get_portfolio, add_holding, remove_holding, update_watchlist

MFAPI_BASE = "https://api.mfapi.in/mf"


# ---------- Stocks (US + India via yfinance) ----------

def get_quote(ticker: str) -> dict:
    """Current price + day stats. Indian tickers need .NS or .BO suffix."""
    t = yf.Ticker(ticker)
    info = t.fast_info
    return {
        "ticker": ticker,
        "last_price": round(info.last_price, 2) if info.last_price else None,
        "currency": info.currency,
        "day_high": round(info.day_high, 2) if info.day_high else None,
        "day_low": round(info.day_low, 2) if info.day_low else None,
        "year_high": round(info.year_high, 2) if info.year_high else None,
        "year_low": round(info.year_low, 2) if info.year_low else None,
        "market_cap": info.market_cap,
        "fifty_day_avg": round(info.fifty_day_average, 2) if info.fifty_day_average else None,
        "two_hundred_day_avg": round(info.two_hundred_day_average, 2) if info.two_hundred_day_average else None,
    }


def get_fundamentals(ticker: str) -> dict:
    """Valuation & quality metrics for a stock."""
    info = yf.Ticker(ticker).info
    keys = [
        "longName", "sector", "industry", "trailingPE", "forwardPE",
        "priceToBook", "returnOnEquity", "debtToEquity", "profitMargins",
        "revenueGrowth", "earningsGrowth", "dividendYield", "beta",
        "totalRevenue", "freeCashflow", "recommendationKey",
    ]
    return {k: info.get(k) for k in keys}


def get_price_history(ticker: str, period: str = "1y") -> dict:
    """Historical performance summary. period: 1mo, 3mo, 6mo, 1y, 3y, 5y, max."""
    hist = yf.Ticker(ticker).history(period=period)
    if hist.empty:
        return {"error": f"No data for {ticker}"}
    close = hist["Close"]
    start, end = float(close.iloc[0]), float(close.iloc[-1])
    returns = close.pct_change().dropna()
    return {
        "ticker": ticker,
        "period": period,
        "start_price": round(start, 2),
        "end_price": round(end, 2),
        "total_return_pct": round((end / start - 1) * 100, 2),
        "annualized_volatility_pct": round(float(returns.std()) * (252 ** 0.5) * 100, 2),
        "max_price": round(float(close.max()), 2),
        "min_price": round(float(close.min()), 2),
        "max_drawdown_pct": round(float(((close / close.cummax()) - 1).min()) * 100, 2),
    }


def compare_tickers(tickers: list[str], period: str = "1y") -> list[dict]:
    """Side-by-side history comparison for up to 5 tickers."""
    return [get_price_history(t, period) for t in tickers[:5]]


# ---------- Indian mutual funds (mfapi.in / AMFI) ----------

def search_mutual_funds(query: str) -> list[dict]:
    """Search Indian MF schemes by name. Returns scheme codes for other MF tools."""
    r = requests.get(MFAPI_BASE, params={"q": query}, timeout=15)
    r.raise_for_status()
    results = r.json()[:10]
    return [{"schemeCode": x["schemeCode"], "schemeName": x["schemeName"]} for x in results]


def get_fund_nav_history(scheme_code: int, days: int = 365) -> dict:
    """NAV history + return summary for an Indian MF scheme (use search_mutual_funds for the code)."""
    r = requests.get(f"{MFAPI_BASE}/{scheme_code}", timeout=15)
    r.raise_for_status()
    data = r.json()
    navs = data.get("data", [])[:days]  # newest first
    if not navs:
        return {"error": f"No NAV data for scheme {scheme_code}"}
    latest = float(navs[0]["nav"])
    oldest = float(navs[-1]["nav"])
    return {
        "scheme": data["meta"].get("scheme_name"),
        "fund_house": data["meta"].get("fund_house"),
        "category": data["meta"].get("scheme_category"),
        "latest_nav": latest,
        "latest_date": navs[0]["date"],
        "nav_days_ago": oldest,
        "oldest_date": navs[-1]["date"],
        "return_pct_over_window": round((latest / oldest - 1) * 100, 2),
        "window_days_requested": days,
    }


# ---------- Tool registry for the Claude API ----------

TOOL_DEFINITIONS = [
    {
        "name": "get_quote",
        "description": "Get current price and day stats for a stock/ETF. US tickers as-is (AAPL, VOO). Indian tickers need .NS (NSE) or .BO (BSE) suffix, e.g. RELIANCE.NS, TCS.NS. Indices: ^NSEI (Nifty 50), ^BSESN (Sensex), ^GSPC (S&P 500).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_fundamentals",
        "description": "Get valuation and quality metrics (PE, ROE, margins, growth, debt) for a stock.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "get_price_history",
        "description": "Get historical return, volatility, and drawdown for a ticker over a period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "period": {"type": "string", "enum": ["1mo", "3mo", "6mo", "1y", "3y", "5y", "max"]},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "compare_tickers",
        "description": "Compare up to 5 tickers side-by-side over the same period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {"type": "array", "items": {"type": "string"}},
                "period": {"type": "string", "enum": ["1mo", "3mo", "6mo", "1y", "3y", "5y", "max"]},
            },
            "required": ["tickers"],
        },
    },
    {
        "name": "search_mutual_funds",
        "description": "Search Indian mutual fund schemes by name (AMFI data). Returns scheme codes needed for NAV lookups.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_fund_nav_history",
        "description": "Get NAV history and return for an Indian mutual fund scheme code (find the code via search_mutual_funds first).",
        "input_schema": {
            "type": "object",
            "properties": {
                "scheme_code": {"type": "integer"},
                "days": {"type": "integer", "description": "Lookback window in days, e.g. 365, 1095"},
            },
            "required": ["scheme_code"],
        },
    },
]

PORTFOLIO_TOOL_DEFINITIONS = [
    {
        "name": "get_portfolio",
        "description": "Get the user's current holdings and watchlist. Always call this before giving buy/sell/hold suggestions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_holding",
        "description": "Add or update a holding in the user's portfolio. instrument: ticker (TCS.NS, AAPL) or MF scheme code as string. kind: 'stock' or 'mutual_fund'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string"},
                "kind": {"type": "string", "enum": ["stock", "mutual_fund"]},
                "quantity": {"type": "number"},
                "avg_buy_price": {"type": "number"},
            },
            "required": ["instrument", "kind", "quantity", "avg_buy_price"],
        },
    },
    {
        "name": "remove_holding",
        "description": "Remove a holding from the portfolio entirely (after a full sell).",
        "input_schema": {
            "type": "object",
            "properties": {"instrument": {"type": "string"}},
            "required": ["instrument"],
        },
    },
    {
        "name": "update_watchlist",
        "description": "Add or remove an instrument on the user's watchlist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string"},
                "action": {"type": "string", "enum": ["add", "remove"]},
            },
            "required": ["instrument", "action"],
        },
    },
]

TOOL_DEFINITIONS = TOOL_DEFINITIONS + PORTFOLIO_TOOL_DEFINITIONS

TOOL_FUNCTIONS = {
    "get_portfolio": get_portfolio,
    "add_holding": add_holding,
    "remove_holding": remove_holding,
    "update_watchlist": update_watchlist,
    "get_quote": get_quote,
    "get_fundamentals": get_fundamentals,
    "get_price_history": get_price_history,
    "compare_tickers": compare_tickers,
    "search_mutual_funds": search_mutual_funds,
    "get_fund_nav_history": get_fund_nav_history,
}


def run_tool(name: str, tool_input: dict) -> str:
    """Execute a tool and return JSON string result (never raises)."""
    try:
        result = TOOL_FUNCTIONS[name](**tool_input)
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
