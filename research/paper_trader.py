"""
Paper trading engine — simulates trading the intraday strategy library
with virtual capital, tracks state across runs, and adaptively weights
strategy selection by LIVE paper-trading performance (not just backtest
stats). This is the "2-3 months of paper-traded signals matching
backtest stats" step CLAUDE.md's evaluation rules already require
before any real money — nothing here is a new strategy or a new data
source, just an execution/tracking layer on top of what's already built.

Adaptive weighting is deliberately NOT a trained ML model: with only a
handful of trades/day, there isn't remotely enough data to train one
without it just memorizing noise (see CLAUDE.md's reviews of two
external repos that made exactly this mistake). Instead: each
strategy's live win rate is Beta-smoothed (weak prior, alpha=beta=3) so
it starts neutral (0.5) with zero trades and only pulls the ranking
away from pure backtest stats as real paper-trading evidence
accumulates — the same "don't trust <30 trades" discipline already
used everywhere else, just applied continuously instead of as a single
gate.

State persists in paper_trading_state.json (gitignored-worthy — this
file is generated, not source). Meant to be run periodically (e.g. every
15-30 min during market hours via cron/manual) with --run, and checked
with --report at any time.

Usage:
  python paper_trader.py --run                  # one cycle: check exits, open new positions
  python paper_trader.py --report                # print performance report
  python paper_trader.py --reset --confirm        # wipe state, restart at starting capital
"""
import argparse
import json
import os
import warnings
from datetime import datetime, time as dtime, timezone

import pandas as pd

from scan_intraday import (
    scan_ticker, top_n_signals, load_data, _rank_key,
    ALL_INTRADAY_STRATEGIES, DEFAULT_UNIVERSE, COMMISSION,
)
from strategies_intraday import SQUARE_OFF_TIME

warnings.filterwarnings("ignore")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading_state.json")
STARTING_CAPITAL = 100_000.0
MAX_POSITIONS = 5          # diversification cap — never all-in on one signal
MAX_POSITION_PCT = 25.0    # hard cap: no single position may exceed this % of total funds, regardless of what risk-based sizing computes
RISK_PCT = 1.0              # % of CURRENT cash risked per new position (compounds as equity moves)
BETA_PRIOR = 3               # weak Bayesian prior (alpha=beta=3): neutral 0.5 win rate until real trades accumulate
FULL_TRUST_AT_TRADES = 20   # live track record fully overrides backtest ranking once a strategy has this many paper trades
MARKET_OPEN = dtime(9, 15)   # NSE continuous session start; no entries before this
MAX_SIGNAL_AGE_MIN = 10      # a BUY whose entry bar started longer ago than this is stale (holiday / after close / slow scan) — skip it
# Paper fills pay the SAME per-side cost the intraday backtest charges, so
# the two can be compared like for like. Before 2026-10-16 paper P&L was
# costless, which turned a losing strategy into +90% (see AUDIT.md §1).

DISCLAIMER = (
    "Simulated paper trading only — no real money, no real orders. Strategies here "
    "are UNVALIDATED (no walk-forward pass yet, see CLAUDE.md Evaluation history); "
    "this engine is itself the validation step CLAUDE.md calls for before real money, "
    "not a substitute for it. Exit detection is an approximation (checked periodically "
    "against current price, not a live stop order) — see check_exits() docstring."
)


def _init_state() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "strategy_stats": {s: {"trades": 0, "wins": 0, "total_pnl": 0.0} for s in ALL_INTRADAY_STRATEGIES},
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
    """Closure so it can be passed as top_n_signals(rank_key=...) — blends
    backtest same-window stats with live paper-trading track record,
    shifting weight toward live evidence as it accumulates per strategy."""
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
    """
    For each open paper position, exit if ANY of:
      (a) current price has crossed the stored stop-loss,
      (b) the strategy that opened it no longer reports HOLD for that
          symbol (its own rules would have closed it by now), or
      (c) it's past the intraday square-off time (SQUARE_OFF_TIME, 15:20
          IST) — unconditional, doesn't depend on (b) resolving
          correctly. This is a deliberate, explicit backstop: intraday
          positions must never carry overnight (see architecture
          decisions), and (b) alone is an indirect signal that can be
          wrong if scan_ticker's check fails transiently — this
          guarantees the close regardless.

    This is an approximation, not a real stop order: it only checks
    whenever this function runs (periodic polling), and uses the current
    close as the exit price rather than the exact stop-loss print if
    price gapped through it between checks. Good enough for paper
    tracking, not a claim of perfect fill simulation.
    """
    closed = []
    now_ist = datetime.now()  # system TZ is Asia/Kolkata, see CLAUDE.md
    eod = now_ist.time() >= SQUARE_OFF_TIME
    for symbol in list(state["positions"].keys()):
        pos = state["positions"][symbol]
        strategy = pos["strategy"]
        try:
            data = load_data(symbol)[1]
            current_price = float(data.Close.iloc[-1])
        except Exception as e:
            continue  # data hiccup this cycle — leave position open, try again next run

        stop_hit = pos["stop_loss"] is not None and current_price <= pos["stop_loss"]

        # Default to "still held" on an inability to confirm — matches the
        # load_data exception above ("leave position open, try again").
        # The old code defaulted to False here, which meant a TRANSIENT
        # scan_ticker failure would silently force an exit instead of
        # safely retrying next cycle — a real bug, fixed alongside adding
        # the (c) EOD backstop above.
        still_held = True
        try:
            result = scan_ticker(symbol, strategy, 100_000, RISK_PCT)  # capital arg unused for status check
            # A fresh "BUY" from the same strategy means it's (still or again)
            # long this symbol — not a reason to exit. Treating BUY as "not
            # held" made every stale BUY exit and re-buy the same position
            # each cycle (209 churn trades, AUDIT.md §5).
            still_held = result["count"] == 1 and (
                result["signals"][0]["recommendation"].startswith("HOLD")
                or result["signals"][0]["recommendation"] == "BUY"
            )
        except Exception:
            pass  # can't confirm strategy state — fall back to stop-loss/EOD checks only this cycle

        if not (stop_hit or not still_held or eod):
            continue

        # Polled, not a resting stop order: if price is already through the
        # stop, the fill is the price we actually see, not the stop level.
        exit_price = min(pos["stop_loss"], current_price) if stop_hit else current_price
        exit_cost = exit_price * pos["quantity"] * COMMISSION
        pnl = (exit_price - pos["entry_price"]) * pos["quantity"] - pos.get("entry_cost", 0.0) - exit_cost
        state["cash"] += exit_price * pos["quantity"] - exit_cost

        exit_reason = "stop_loss" if stop_hit else ("eod_square_off" if eod else "strategy_exit")
        trade = {
            "symbol": symbol, "strategy": strategy,
            "entry_price": pos["entry_price"], "exit_price": exit_price, "quantity": pos["quantity"],
            "costs": round(pos.get("entry_cost", 0.0) + exit_cost, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl / (pos["entry_price"] * pos["quantity"]) * 100, 2),
            "entry_time": pos["entry_time"], "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_reason": exit_reason,
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
    # Session guard: the cron also fires at 09:00, 15:30 and 15:45, and on NSE
    # holidays. Off-session the data is frozen and the last BUY keeps being
    # reported, so the old code bought stale prices and carried them overnight
    # — 59% of the old run's gross P&L (AUDIT.md §2). Holidays are caught by
    # the per-signal freshness check below (no new bars → every BUY is stale).
    now_ist = datetime.now()  # system TZ is Asia/Kolkata
    if now_ist.weekday() >= 5 or not (MARKET_OPEN <= now_ist.time() < SQUARE_OFF_TIME):
        return []
    held = set(state["positions"].keys())
    universe = [t for t in DEFAULT_UNIVERSE if t not in held]

    # Total funds = cash + cost basis of currently-held positions (NOT
    # remarked to market — this is a position-SIZING reference, not a P&L
    # figure; generate_report() does the real mark-to-market). Computed
    # ONCE here as a fixed snapshot: existing positions are never resized
    # or touched by this cap, only new buys are capped against this total.
    total_funds = state["cash"] + sum(p["capital_allocated"] for p in state["positions"].values())
    max_position_capital = total_funds * (MAX_POSITION_PCT / 100)

    # Request the FULL active-signal list, not just `slots` — most active
    # signals are HOLD (an already-open position from days ago), not a
    # fresh BUY, so asking for only `slots` results can come back with
    # zero openable BUYs even when real ones exist further down the
    # ranking. Filter for BUY after getting everything, not before.
    result = top_n_signals(
        n=len(universe), tickers=universe, capital=state["cash"], risk_pct=RISK_PCT,
        rank_key=_blended_rank_key(state),
    )

    opened = []
    for sig in result["top"]:
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        if sig["recommendation"] != "BUY":
            continue  # only open on a FRESH entry, not an already-open HOLD from another strategy's window
        entry_bar = pd.Timestamp(sig["entry_time"])
        if entry_bar.tzinfo is None:
            entry_bar = entry_bar.tz_localize("Asia/Kolkata")
        if pd.Timestamp.now(tz="Asia/Kolkata") - entry_bar > pd.Timedelta(minutes=MAX_SIGNAL_AGE_MIN):
            continue  # stale: entry bar opened too long ago (holiday, frozen feed, slow scan)

        # Fill at the price observed now, not the backtest's EntryPrice (the
        # entry bar's OPEN, a median 8 / p90 23 min before we act — on a
        # breakout that stale price is systematically better than reality,
        # AUDIT.md §2). Stop/target stay at the strategy's levels.
        fill = sig["current_price"]
        if sig["stop_loss"] is not None and fill <= sig["stop_loss"]:
            continue  # already through the stop by the time we'd fill
        quantity = sig["quantity"]
        if quantity <= 0:
            continue
        capital_needed = round(quantity * fill, 2)

        # Hard concentration cap: risk-based sizing (scan_intraday.py) can
        # still allocate almost all available cash to one signal whenever
        # the stop distance is tight relative to price (the risk-amount
        # ceiling never binds, so it falls through to "buy as many shares
        # as cash allows" instead). Caught live: KOTAKBANK.NS took 99.87%
        # of total equity this way. No single NEW position may exceed
        # MAX_POSITION_PCT of total funds, full stop, regardless of what
        # the risk formula alone would allow.
        if capital_needed > max_position_capital:
            quantity = int(max_position_capital // fill)
            if quantity <= 0:
                continue  # can't buy even 1 share within the cap (very expensive stock, small account)
            capital_needed = round(quantity * fill, 2)

        entry_cost = round(capital_needed * COMMISSION, 2)
        if capital_needed + entry_cost > state["cash"]:
            continue
        state["cash"] -= capital_needed + entry_cost
        state["positions"][sig["symbol"]] = {
            "strategy": sig["strategy"], "entry_price": fill, "signal_entry_price": sig["entry"],
            "quantity": quantity, "stop_loss": sig["stop_loss"], "target1": sig["target1"],
            "entry_time": datetime.now(timezone.utc).isoformat(), "capital_allocated": capital_needed,
            "entry_cost": entry_cost,
        }
        opened.append({"symbol": sig["symbol"], "strategy": sig["strategy"], "entry_price": fill,
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
            current_price = pos["entry_price"]  # fallback if data fetch fails right now
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
    p.add_argument("--run", action="store_true", help="one cycle: check exits, open new positions, save state")
    p.add_argument("--report", action="store_true", help="print the current performance report")
    p.add_argument("--reset", action="store_true", help="wipe state and restart at starting capital (requires --confirm)")
    p.add_argument("--confirm", action="store_true", help="required alongside --reset to actually wipe state")
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
