"""
Paper trading engine for US daily/swing stocks (S&P 500) — sibling to
paper_trader_daily.py (NSE), mirrored not shared (different universe,
different currency, different commission profile — see scan_daily_us.py
docstring for why this isn't the same module as the NSE version).

Same DAILY strategy library (sma_cross, momentum, mean_reversion,
breakout, clenow_trend, zscore_reversion), same once-per-day cadence
(no square-off — swing positions held for weeks), same Beta-smoothed
adaptive weighting (NOT a trained ML model — see paper_trader.py's
docstring for why), same MAX_POSITION_PCT=25 concentration cap and
"still_held defaults True" fix as the other two paper traders.

Starting capital is $100,000 (USD), not the ₹1,00,000 the other three
trackers use — different currency, deliberately not FX-converted (that
would just add an assumption this project has no reason to make). This
is its own independent simulated portfolio, own state file — four
total now (intraday NSE, daily NSE, funds, daily US), none share a
capital pool.

No intraday US tracker and no cron for this one, by explicit choice
(see CLAUDE.md) — US market hours are ~19:00-01:30 IST during EDT, an
inconvenient schedule to run/monitor from India; daily cadence sidesteps
that entirely since it only needs to run once, at any convenient time
after the US close. Run manually, or set up your own cron if you want
one later.

This daily strategy library has NEVER been tested on US stocks before
today (only walk-forward tested on NSE names, where it went 0-for-7) —
treat any result here as even less validated than the NSE daily tracker.

Usage:
  python paper_trader_daily_us.py --run
  python paper_trader_daily_us.py --report
  python paper_trader_daily_us.py --reset --confirm
"""
import argparse
import json
import os
import warnings
from datetime import datetime, timezone

from scan_daily_us import (
    scan_ticker, top_n_signals, load_data, _rank_key,
    DEFAULT_UNIVERSE,
)
from strategies import ALL_STRATEGIES

warnings.filterwarnings("ignore")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading_daily_us_state.json")
STARTING_CAPITAL = 100_000.0  # USD, not INR — see module docstring
MAX_POSITIONS = 5
MAX_POSITION_PCT = 25.0
RISK_PCT = 1.0
BETA_PRIOR = 3
FULL_TRUST_AT_TRADES = 20

DISCLAIMER = (
    "Simulated paper trading only — no real money, no real orders. USD, not INR. "
    "This daily strategy library was walk-forward tested only on NSE names (0-for-7) "
    "— never tested on US stocks before today. UNVALIDATED in every sense on this "
    "universe. Exit detection is a periodic-polling approximation, not a real stop "
    "order. Meant to be run ONCE PER DAY — running more often than daily bars update "
    "won't change anything."
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
    for symbol in list(state["positions"].keys()):
        pos = state["positions"][symbol]
        strategy = pos["strategy"]
        try:
            data = load_data(symbol)[1]
            current_price = float(data.Close.iloc[-1])
        except Exception:
            continue

        stop_hit = pos["stop_loss"] is not None and current_price <= pos["stop_loss"]

        still_held = True  # default to "still held" on an inability to confirm — see paper_trader.py for why
        try:
            result = scan_ticker(symbol, strategy, 100_000, RISK_PCT)
            still_held = result["count"] == 1 and result["signals"][0]["recommendation"].startswith("HOLD")
        except Exception:
            pass

        if not (stop_hit or not still_held):
            continue

        exit_price = pos["stop_loss"] if stop_hit else current_price
        pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
        state["cash"] += exit_price * pos["quantity"]

        trade = {
            "symbol": symbol, "strategy": strategy,
            "entry_price": pos["entry_price"], "exit_price": exit_price, "quantity": pos["quantity"],
            "pnl": round(pnl, 2), "pnl_pct": round((exit_price / pos["entry_price"] - 1) * 100, 2),
            "entry_time": pos["entry_time"], "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_reason": "stop_loss" if stop_hit else "strategy_exit",
        }
        state["closed_trades"].append(trade)
        stats = state["strategy_stats"].setdefault(strategy, {"trades": 0, "wins": 0, "total_pnl": 0.0})
        stats["trades"] += 1
        stats["wins"] += 1 if pnl > 0 else 0
        stats["total_pnl"] = round(stats["total_pnl"] + pnl, 2)
        del state["positions"][symbol]
        closed.append(trade)
    return closed


def open_new_positions(state: dict) -> list:
    slots = MAX_POSITIONS - len(state["positions"])
    if slots <= 0:
        return []
    held = set(state["positions"].keys())
    universe = [t for t in DEFAULT_UNIVERSE if t not in held]

    total_funds = state["cash"] + sum(p["capital_allocated"] for p in state["positions"].values())
    max_position_capital = total_funds * (MAX_POSITION_PCT / 100)

    result = top_n_signals(
        n=len(universe), tickers=universe, capital=state["cash"], risk_pct=RISK_PCT,
        rank_key=_blended_rank_key(state),
    )

    opened = []
    for sig in result["top"]:
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        if sig["recommendation"] != "BUY":
            continue
        quantity, capital_needed = sig["quantity"], sig["capital_allocation"]
        if quantity <= 0 or capital_needed <= 0:
            continue

        if capital_needed > max_position_capital:
            quantity = int(max_position_capital // sig["entry"])
            if quantity <= 0:
                continue
            capital_needed = round(quantity * sig["entry"], 2)

        if capital_needed > state["cash"]:
            continue
        state["cash"] -= capital_needed
        state["positions"][sig["symbol"]] = {
            "strategy": sig["strategy"], "entry_price": sig["entry"], "quantity": quantity,
            "stop_loss": sig["stop_loss"], "target1": sig["target1"],
            "entry_time": datetime.now(timezone.utc).isoformat(), "capital_allocated": capital_needed,
        }
        opened.append({"symbol": sig["symbol"], "strategy": sig["strategy"], "entry_price": sig["entry"],
                        "quantity": quantity, "capital_allocated": capital_needed})
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
    for symbol, pos in state["positions"].items():
        try:
            current_price = float(load_data(symbol)[1].Close.iloc[-1])
        except Exception:
            current_price = pos["entry_price"]
        pos_pnl = (current_price - pos["entry_price"]) * pos["quantity"]
        unrealized_pnl += pos_pnl
        holdings.append({
            "symbol": symbol, "strategy": pos["strategy"], "entry_price": pos["entry_price"],
            "current_price": round(current_price, 2), "quantity": pos["quantity"],
            "unrealized_pnl": round(pos_pnl, 2),
        })

    equity = state["cash"] + sum(h["current_price"] * h["quantity"] for h in holdings)
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
        print(f"State reset. Starting capital: ${STARTING_CAPITAL}")
        return

    if args.run:
        print(json.dumps(run_cycle(), indent=2, default=str))
        return

    if args.report or not (args.run or args.reset):
        print(json.dumps(generate_report(), indent=2, default=str))


if __name__ == "__main__":
    main()
