"""
Portfolio + watchlist, persisted to a local JSON file.
Holdings: ticker (or MF scheme code), quantity/units, avg buy price.
The agent uses these so buy/sell suggestions are grounded in what you own.
"""
import json
import os
from pathlib import Path

DATA_FILE = Path(os.environ.get("PORTFOLIO_FILE", "portfolio.json"))


def _load() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"holdings": [], "watchlist": []}


def _save(data: dict):
    DATA_FILE.write_text(json.dumps(data, indent=2))


def get_portfolio() -> dict:
    """Return all holdings and the watchlist."""
    return _load()


def add_holding(instrument: str, kind: str, quantity: float, avg_buy_price: float) -> dict:
    """Add or update a holding.
    instrument: ticker like 'TCS.NS'/'AAPL', or an MF scheme code as a string.
    kind: 'stock' or 'mutual_fund'.
    quantity: shares or fund units. avg_buy_price: in the instrument's currency (INR for MF/NSE, USD for US).
    """
    data = _load()
    data["holdings"] = [h for h in data["holdings"] if h["instrument"] != instrument]
    data["holdings"].append({
        "instrument": instrument, "kind": kind,
        "quantity": quantity, "avg_buy_price": avg_buy_price,
    })
    _save(data)
    return {"ok": True, "holdings_count": len(data["holdings"])}


def remove_holding(instrument: str) -> dict:
    """Remove a holding entirely (e.g. after selling out)."""
    data = _load()
    before = len(data["holdings"])
    data["holdings"] = [h for h in data["holdings"] if h["instrument"] != instrument]
    _save(data)
    return {"ok": True, "removed": before - len(data["holdings"])}


def update_watchlist(instrument: str, action: str) -> dict:
    """Add or remove an instrument from the watchlist. action: 'add' or 'remove'."""
    data = _load()
    wl = set(data["watchlist"])
    if action == "add":
        wl.add(instrument)
    elif action == "remove":
        wl.discard(instrument)
    else:
        return {"error": "action must be 'add' or 'remove'"}
    data["watchlist"] = sorted(wl)
    _save(data)
    return {"ok": True, "watchlist": data["watchlist"]}
