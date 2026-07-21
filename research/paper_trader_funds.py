"""
Paper trading engine for Indian mutual funds — sibling to paper_trader.py
(intraday stocks) and paper_trader_daily.py (daily stocks), mirrored not
shared (see those files' docstrings for why: different data source,
different strategy library reuse pattern, different position units —
"units" of a fund, not "quantity" of shares).

Universe is LIVE-DISCOVERED from mfapi.in's catalog by default (see
scan_funds.discover_fund_universe), same as GET /top-funds — not a
hardcoded list. Funds publish one NAV/day, so like paper_trader_daily.py
this is meant to be run ONCE PER DAY, not every 15-30 minutes.

Same Beta-smoothed adaptive weighting as the other two paper traders
(NOT a trained ML model — see paper_trader.py's docstring for why).
Separate ₹1,00,000 starting capital and separate state file — three
independent simulated portfolios (intraday stocks / daily stocks /
funds), not a shared pot.

Per CLAUDE.md Evaluation history, these 6 strategies have NEVER been
tested on fund NAV data before this session, and exit loads (~1% if
redeemed <1yr on equity funds) are not modeled — treat results here
with even more skepticism than the stock paper traders.

Usage:
  python paper_trader_funds.py --run
  python paper_trader_funds.py --report
  python paper_trader_funds.py --reset --confirm
"""
import argparse
import json
import os
import warnings
from datetime import datetime, timezone

from scan_funds import (
    scan_fund, top_n_fund_signals, load_nav_data, _rank_key,
    get_default_fund_universe,
)
from strategies import ALL_STRATEGIES

warnings.filterwarnings("ignore")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading_funds_state.json")
STARTING_CAPITAL = 100_000.0
MAX_POSITIONS = 5
MAX_POSITION_PCT = 25.0    # hard cap: no single position may exceed this % of total funds — see paper_trader.py for why this was added
RISK_PCT = 1.0
BETA_PRIOR = 3
FULL_TRUST_AT_TRADES = 20

DISCLAIMER = (
    "Simulated paper trading only — no real money, no real orders. Per CLAUDE.md "
    "Evaluation history, these strategies were built for stocks and have never been "
    "tested on fund NAV data before this session — UNVALIDATED in every sense. Exit "
    "loads (~1% if redeemed <1yr on equity funds) are NOT modeled. Exit detection is "
    "a periodic-polling approximation, not a real redemption order. Meant to be run "
    "ONCE PER DAY — funds publish one NAV/day, running more often changes nothing."
)


def _init_state() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "strategy_stats": {s: {"trades": 0, "wins": 0, "total_pnl": 0.0} for s in ALL_STRATEGIES},
        "created_at": now,
        "last_run": None,
    }


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return _init_state()
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _live_win_rate_smoothed(strategy_name: str, state: dict) -> float:
    stats = state["strategy_stats"].get(strategy_name, {"trades": 0, "wins": 0})
    return (stats["wins"] + BETA_PRIOR) / (stats["trades"] + 2 * BETA_PRIOR)


def _blended_rank_key(state: dict):
    def key(signal: dict):
        backtest_validated, backtest_win_rate, backtest_return = _rank_key(signal)
        strategy_name = signal["strategy"]
        live_trades = state["strategy_stats"].get(strategy_name, {}).get("trades", 0)
        live_wr_pct = _live_win_rate_smoothed(strategy_name, state) * 100
        blend = min(live_trades / FULL_TRUST_AT_TRADES, 1.0)
        blended_score = (1 - blend) * (backtest_win_rate or 0) + blend * live_wr_pct
        return (backtest_validated, blended_score, backtest_return)
    return key


def check_exits(state: dict) -> list:
    closed = []
    for scheme_code in list(state["positions"].keys()):
        pos = state["positions"][scheme_code]
        strategy = pos["strategy"]
        try:
            current_nav = float(load_nav_data(scheme_code)[2].Close.iloc[-1])
        except Exception:
            continue

        stop_hit = pos["stop_loss"] is not None and current_nav <= pos["stop_loss"]

        # Default to "still held" on an inability to confirm — see
        # paper_trader.py (intraday) for where this bug (defaulting to
        # False, silently forcing an exit on a transient API failure
        # instead of safely retrying) was first caught and fixed.
        still_held = True
        try:
            result = scan_fund(scheme_code, strategy, 100_000, RISK_PCT)
            still_held = result["count"] == 1 and result["signals"][0]["recommendation"].startswith("HOLD")
        except Exception:
            pass

        if not (stop_hit or not still_held):
            continue

        exit_nav = pos["stop_loss"] if stop_hit else current_nav
        pnl = (exit_nav - pos["entry_nav"]) * pos["units"]
        state["cash"] += exit_nav * pos["units"]

        trade = {
            "scheme_code": scheme_code, "scheme_name": pos["scheme_name"], "strategy": strategy,
            "entry_nav": pos["entry_nav"], "exit_nav": exit_nav, "units": pos["units"],
            "pnl": round(pnl, 2), "pnl_pct": round((exit_nav / pos["entry_nav"] - 1) * 100, 2),
            "entry_time": pos["entry_time"], "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_reason": "stop_loss" if stop_hit else "strategy_exit",
        }
        state["closed_trades"].append(trade)
        stats = state["strategy_stats"].setdefault(strategy, {"trades": 0, "wins": 0, "total_pnl": 0.0})
        stats["trades"] += 1
        stats["wins"] += 1 if pnl > 0 else 0
        stats["total_pnl"] = round(stats["total_pnl"] + pnl, 2)
        del state["positions"][scheme_code]
        closed.append(trade)
    return closed


def open_new_positions(state: dict) -> list:
    slots = MAX_POSITIONS - len(state["positions"])
    if slots <= 0:
        return []
    held = set(state["positions"].keys())
    universe = [f for f in get_default_fund_universe() if f not in held]

    total_funds = state["cash"] + sum(p["capital_allocated"] for p in state["positions"].values())
    max_position_capital = total_funds * (MAX_POSITION_PCT / 100)

    # Request the FULL active-signal list, not just `slots` — see the
    # same fix in paper_trader.py/paper_trader_daily.py for why: most
    # active signals are HOLD, not a fresh BUY, so a small `n` can
    # silently starve real opportunities that exist further down the rank.
    result = top_n_fund_signals(
        n=len(universe), funds=universe, capital=state["cash"], risk_pct=RISK_PCT,
        rank_key=_blended_rank_key(state),
    )

    opened = []
    for sig in result["top"]:
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        if sig["recommendation"] != "BUY":
            continue
        units, capital_needed = sig["units"], sig["capital_allocation"]
        if units <= 0 or capital_needed <= 0:
            continue

        if capital_needed > max_position_capital:
            units = int(max_position_capital // sig["entry_nav"])
            if units <= 0:
                continue
            capital_needed = round(units * sig["entry_nav"], 2)

        if capital_needed > state["cash"]:
            continue
        code = str(sig["scheme_code"])
        state["cash"] -= capital_needed
        state["positions"][code] = {
            "scheme_name": sig["scheme_name"], "strategy": sig["strategy"],
            "entry_nav": sig["entry_nav"], "units": units,
            "stop_loss": sig["stop_loss_nav"], "target1": sig["target1_nav"],
            "entry_time": datetime.now(timezone.utc).isoformat(), "capital_allocated": capital_needed,
        }
        opened.append({"scheme_code": code, "scheme_name": sig["scheme_name"], "strategy": sig["strategy"],
                        "entry_nav": sig["entry_nav"], "units": units, "capital_allocated": capital_needed})
    return opened


def run_cycle() -> dict:
    state = load_state()
    closed = check_exits(state)
    opened = open_new_positions(state)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    return {
        "closed_trades": closed, "opened_positions": opened,
        "cash": round(state["cash"], 2), "open_position_count": len(state["positions"]),
        "disclaimer": DISCLAIMER,
    }


def generate_report(state: dict = None) -> dict:
    state = state or load_state()
    closed = state["closed_trades"]
    wins = [t for t in closed if t["pnl"] > 0]
    realized_pnl = sum(t["pnl"] for t in closed)

    unrealized_pnl = 0.0
    holdings = []
    for scheme_code, pos in state["positions"].items():
        try:
            current_nav = float(load_nav_data(scheme_code)[2].Close.iloc[-1])
        except Exception:
            current_nav = pos["entry_nav"]
        pos_pnl = (current_nav - pos["entry_nav"]) * pos["units"]
        unrealized_pnl += pos_pnl
        holdings.append({
            "scheme_code": scheme_code, "scheme_name": pos["scheme_name"], "strategy": pos["strategy"],
            "entry_nav": pos["entry_nav"], "current_nav": round(current_nav, 4), "units": pos["units"],
            "unrealized_pnl": round(pos_pnl, 2),
        })

    equity = state["cash"] + sum(h["current_nav"] * h["units"] for h in holdings)
    total_return_pct = (equity - state["starting_capital"]) / state["starting_capital"] * 100

    return {
        "starting_capital": state["starting_capital"],
        "current_equity": round(equity, 2),
        "cash": round(state["cash"], 2),
        "total_return_pct": round(total_return_pct, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "closed_trade_count": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "open_positions": holdings,
        "strategy_breakdown": state["strategy_stats"],
        "created_at": state["created_at"],
        "last_run": state["last_run"],
        "disclaimer": DISCLAIMER,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    p.add_argument("--report", action="store_true")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--confirm", action="store_true")
    args = p.parse_args()

    if args.reset:
        if not args.confirm:
            print("--reset requires --confirm too (this wipes all paper trading history). Aborting.")
            return
        save_state(_init_state())
        print(f"State reset. Starting capital: {STARTING_CAPITAL}")
        return

    if args.run:
        print(json.dumps(run_cycle(), indent=2, default=str))
        return

    if args.report or not (args.run or args.reset):
        print(json.dumps(generate_report(), indent=2, default=str))


if __name__ == "__main__":
    main()
