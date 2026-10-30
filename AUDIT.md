# Audit: intraday paper-trading result

*2026-10-16. Read-only: nothing in `research/` was edited, committed, deleted or restarted.*

**Snapshot.** The cron job kept running during the audit. Ledger figures use the state at ~13:30 IST (1,688 closed trades). The category tables use ~13:50 IST (1,690 trades; the 2 new trades lost ₹180).

**Scripts.** The analysis scripts are in the session scratchpad (`audit.py`, `audit2.py`). Price replays use yfinance 5m bars. That data only goes back to 2026-07-23, so trades before then cannot be replayed.

## Verdict

**The +₹89,837 is an artifact. There is no evidence of an intraday edge.**

- **Most of the profit is from trades outside the session.** 59% of gross P&L (+₹52.9k from 605 trades) came from positions opened after the 15:20 square-off, before the 09:15 open, or on NSE holidays. These positions were filled at stale prices and carried overnight.
- **The in-session trades lose money once fills are realistic.** These are the 1,084 trades entered and closed within 09:15–15:20 on a trading day. If they are filled at the next bar's open, they lose money even at the backtest's own cost rate of 0.03% per side. At 0.12% per side they lose heavily.
- **vwap_breakout has no gross edge.** On in-session trades with realistic fills it is **−₹1,217 before costs**.
- **This matches the failed walk-forward.** The paper engine did not discover an edge the backtest missed. The paper trader's mechanics manufactured one.

| Item | Verdict |
|---|---|
| 1. Costs | **BUG** |
| 2. Fills and lookahead | **BUG** (stale fills, no session or holiday guard) |
| 3. Exits | **BUG** (stop fill, square-off leak in strategies) |
| 4. RISK_PCT | **OK**, irrelevant to the result (see below). Uncommitted. |
| 5. Accounting | **OK** (ties out), **SUSPECT** (churn re-buys) |
| 6. Distribution | **SUSPECT** (concentrated in out-of-session trades and October) |
| 7. Pessimistic recompute | Negative under every realistic case |
| 8. Staleness | 3 trackers unmonitored since July; uncommitted changes |

---

## 1. Costs: BUG

**Evidence**
- **Paper P&L is costless.**
  - `research/paper_trader.py:158`: `pnl = (exit_price - pos["entry_price"]) * pos["quantity"]`
  - `research/paper_trader.py:159`: credits `exit_price * quantity` to cash, with no fee
  - `research/paper_trader.py:230`: debits `capital_needed` (`quantity * entry`), with no fee
  - The same pattern is in `paper_trader_daily.py:133-134`, `paper_trader_daily_us.py:134-135` and `paper_trader_funds.py:130`.
- **The intraday backtest does not use 0.0012 either.**
  - `research/scan_intraday.py:38`: `COMMISSION = 0.0003`
  - `research/run_backtest_intraday.py:134`: `--commission` defaults to `0.0003`
  - CLAUDE.md says 0.0012 is the Indian default. That figure only holds for the daily track (`scan_daily.py:33` = 0.0012, `scan_daily_us.py:42` = 0.0005).

**Numbers (1,688 trades)**

| | ₹ |
|---|---|
| Mean P&L per trade | 53.22 |
| Median position notional | 29,639 |
| Round trip at median notional, 0.12% per side | **71.13** (more than the mean P&L) |
| Round trip at median notional, 0.03% per side | 17.78 |
| Recorded total | +89,837 |
| Net of 0.12% per side | **−27,991** |
| Net of 0.03% per side | +60,380 (still with the fill problems in §2–3) |

## 2. Fills and lookahead: BUG

**Entry price**
- The paper entry is the backtest's `EntryPrice`, not a price observed when the trade is opened (`scan_intraday.py:129` → `:181`, then `paper_trader.py:232`).
- In backtesting.py that price is the **open of the bar after the signal bar** (`backtesting.py:915`, `exclusive_orders`, no `trade_on_close`).
- The paper trade is opened later than that bar open: median **8.2 min**, p90 **23.5 min** (517 trades matched by re-running the backtest). The causes are 5m bars, a 15-minute cron, and a ~3-minute scan of 500 names.
- For a breakout strategy a stale fill is favorable on average. Price has usually moved in the trade's direction by the time the paper trade is opened.

**Incomplete bars**
- yfinance NSE 5m data is **not** 15-minute delayed today. At 13:33:28 IST the 13:30 bar was present and still forming (volume 69k vs 241k on the prior bar).
- `load_data` (`scan_intraday.py:65-74`) keeps that partial bar.
- BUYs still use only completed bars: `EntryBar == last_bar` (`:106`) means the order was placed on bar `last-1`.
- The HOLD and exit checks and the paper stop check use the forming bar's close (`paper_trader.py:135`). This adds noise but no lookahead.

**Same trade log as the backtest**
- **Yes**: `scan_intraday.py:88-128` runs the same `Backtest` and reads `_trades`.
- No indicator lookahead: `session_vwap` is a per-day cumsum (`indicators.py:55-62`), and the opening range is masked until formed (`indicators.py:72`).

**No session or calendar guard on opening: BUG.** This is the largest single problem.
- `open_new_positions` (`paper_trader.py:179-238`) never checks the time of day or whether the market is open.
- The cron is `*/15 9-15 * * 1-5`, so it runs at 09:00 (pre-open) and at 15:30 and 15:45 (after the close).
- After the close, the data is frozen. The last stale BUY keeps being reported (`EntryBar == last_bar`), and the paper trader buys it at the old bar's open.
- Pre-open runs buy at yesterday's last price and then capture the overnight gap.
- On NSE holidays the data is frozen all day, so the same BUY repeats every cycle.

| Category (1,690 trades) | n | Gross | Net 0.03% | Net 0.12% | Win % |
|---|---|---|---|---|---|
| In-session entry, same-day exit ("clean") | 1,084 | +35,990 | +18,261 | **−34,924** | 39.0 |
| In-session entry, held overnight | 1 | +777 | +760 | +710 | 100 |
| Entry at/after 15:20 | 443 | **+37,323** | +29,006 | +4,054 | 59.1 |
| Pre-open entry (before 09:15) | 36 | +5,349 | +4,613 | +2,405 | 61.1 |
| Non-trading days (2026-09-14, 2026-10-02*) | 126 | +10,218 | +7,503 | −642 | **95.2** |

\* Both dates are absent from yfinance's NSE data. On 2026-10-02 there were 86 entries, including BHARATFORG.NS bought **28 times at ₹1,868.10**. Separately, 3 entries came from a manual run at 2026-09-01 19:58 IST.

**Overnight carry.** 209 trades were held overnight: +₹28,069 gross, +₹12,194 even at 0.12% per side. This is an intraday system that must never carry positions overnight.

## 3. Exits: BUG

**Stop and target in the same bar**
- **Paper trader: not applicable.** `target1` is stored (`paper_trader.py:233`) but never read. Exits are stop, strategy-no-longer-HOLD, or end of day.
- **Backtest: not applicable.** No intraday strategy sets `tp=` (`strategies_intraday.py:61, 92, 123, 164`).

**Stop checks**
- **Paper (BUG):** the check runs on the close only, at polling time (`paper_trader.py:135, 139`). The fill is then set to **the stop price** (`:157`), even when price is already below it. The docstring (`:121-125`) says the opposite.
- Measured: of 267 replayed stop exits, the market was below the recorded stop in **254**. That overstates P&L by **₹28,785**, median gap −0.17%.
- **Backtest: OK.** The stop is checked on the bar low and gap-throughs fill at `min(open, stop)` (`backtesting.py:886, 915-917`).

**15:20 square-off**
- **Paper: applied but incomplete.** `eod = now >= 15:20` (`paper_trader.py:128-129`) closes at the current price, but only when a cron run happens after 15:20 (15:30 and 15:45). That run then **opens new positions** right after closing (§2). Positions opened by the 15:45 run, which finishes around 15:52, stay open until the next morning.
- **Strategies: BUG (affects the backtest too).** `position.close()` on the first bar at or after 15:20 (`strategies_intraday.py:51-56`, same at `:81-86`, `:111-116`, `:150-157`) fills at the **next bar's open**.
  - When that bar is 15:25, the last bar of the session, the close fills at the next day's 09:15 open.
  - When the 15:20 bar is missing, which yfinance does in **1,080 of 1,800 sessions** sampled, the same thing happens.
  - Example: HINDZINC.NS vwap_breakout entered 2026-10-15 15:15 and exited 2026-10-16 09:15.
  - Across the 30 most-traded names over 60 days: 166 of 1,630 vwap_breakout backtest trades were held overnight. Those trades' summed returns were +131.4%, against +53.7% for all trades combined.
  - **So the backtests' intraday results are contaminated by overnight gaps too.** The earlier walk-forward verdicts should be re-run after this is fixed.

## 4. RISK_PCT: OK (no effect), but uncommitted

**History**
- One commit only: `cce43d2 2026-07-21 16:31 initial`, which has `RISK_PCT = 1.0` in all 4 files.
- The working tree has `10.0`: `paper_trader.py:49`, `paper_trader_daily.py:49`, `paper_trader_daily_us.py:55`, `paper_trader_funds.py:47`.
- All four files have mtime **2026-07-23 11:19:08**, the same second, so the edit happened then or earlier.
- 17 trades before that time (+₹637); 1,671 after (+₹89,200).

**It made no difference to sizing.**
- vwap_breakout's stop distance has a median of **0.275%** of the entry price (p90 0.51%, max 2.14%).
- At 1% risk, the position size is 1%/0.275% ≈ **363% of cash**. That is far above the 25% cap.
- The 25% cap binds on **100%** of sampled trades at either 1% or 10%.
- From 2026-07-22 on, positions are consistently 24–25% of approximate total funds under both settings. **RISK_PCT is not a cause of the result.**

**What actually grew the positions**
- The cap is computed on `cash + cost basis` (`paper_trader.py:191`), and that total includes the inflated realized P&L.
- Average notional by month: ₹20.0k (Jul), ₹23.4k (Aug), ₹29.3k (Sep), ₹37.2k (Oct). Fake profit compounds into bigger positions.

**Caps in the cron path: OK**
- The cron runs `paper_trader.py --run` → `run_cycle` (`:241`) → `open_new_positions`.
- The 25% cap (`:222-226`) and max 5 positions (`:180, 206`) are enforced. At most 5 positions were open at once, reconstructed from the trade rows.
- The single breach (KOTAKBANK.NS, 2026-07-21 11:06, ~100% of funds) was opened before the cap existed.

## 5. Accounting: OK (ties out), SUSPECT (churn)

**Tie-out**
- Sum of the recorded `pnl` column: ₹89,837.49
- Recomputed from (exit − entry) × qty: ₹89,837.53. The largest per-row difference is ₹0.0025 (rounding).
- `strategy_stats` total: ₹89,837.49 over 1,688 trades, matching the row count.
- Cash + open cost basis − start = ₹89,837.53. Cash and capital are updated consistently.
- There is no double counting. The missing fees are covered in §1.

**Duplicates and churn**
- Exact duplicates (same symbol, entry_time, qty): **0**.
- **209** same-day re-buys at an identical entry price (207 of them vwap_breakout), with a median of 8.7 min between exit and re-entry. Cause: `check_exits` treats a scan result of `"BUY"` as "not held" (`paper_trader.py:150`). A stale BUY therefore triggers exit and re-buy every cycle. These re-entry legs made +₹22,484 gross.
- 22 trades with entry price equal to exit price; 37 held under 5 minutes; 820 (49%) held 16 minutes or less, i.e. one cron interval.

## 6. Distribution: SUSPECT

**By strategy (1,688 trades)**

| Strategy | n | Win % | Gross | Net 0.12% |
|---|---|---|---|---|
| vwap_breakout | 1,454 | 51.0 | +77,194 | −25,938 |
| intraday_momentum | 155 | 37.4 | +11,329 | +1,186 |
| orb_breakout | 71 | 35.2 | +1,518 | −2,403 |
| range_reversion | 8 | 37.5 | −203 | −838 |

**By exit reason**

| Exit | n | Win % | Gross |
|---|---|---|---|
| eod_square_off | 448 | 76.6 | **+70,904** |
| strategy_exit | 889 | 54.6 | +51,632 |
| stop_loss | 351 | 0 | −32,699 |

**Concentration**
- **11 of 62 days** make up 50% of gross P&L; so do **33 of 1,688 trades**.
- 54 of 62 days are positive gross, but only **19 of 62** are positive net of 0.12%.

**By month (gross / net of 0.12%)**

| Month | Trades | Gross | Net |
|---|---|---|---|
| Jul | 83 | −2,813 | −6,790 |
| Aug | 517 | +19,267 | −9,818 |
| Sep | 654 | +34,347 | −11,616 |
| Oct | 434 | +39,036 | +232 |

**Trades per day:** min 5, median 28, max 86 (the 2026-10-02 holiday).

**Top 5 days:** 10-14 +₹7,287 · 09-21 +₹5,618 · 10-12 +₹5,028 · 10-02 (holiday) +₹4,657 · 10-09 +₹4,454
**Bottom 5 days:** 07-24 −₹1,077 · 07-30 −₹1,055 · 07-29 −₹965 · 08-19 −₹824 · 07-23 −₹712

**By ticker**
- 427 tickers traded, median 3 trades each.
- Most traded: BHARATFORG.NS 39 (+₹5,323), CGPOWER.NS 37, JSWENERGY.NS 33 (+₹4,447), NBCC.NS 28, FEDERALBNK.NS 22.
- Worst: AIIL.NS −₹1,156, LODHA.NS −₹854, PFOCUS.NS −₹841.

**Top 10 trades.** Note how many are entries after 15:50 with overnight exits.

| Entry | Symbol | Strategy | Entry → exit | P&L | Exit |
|---|---|---|---|---|---|
| 10-14 13:23 | ELECON.NS | intraday_momentum | 441.10 → 483.95 | +4,371 | eod |
| 10-12 11:38 | GALLANTT.NS | intraday_momentum | 568.05 → 604.50 | +2,734 | eod |
| 10-13 **15:55** | JSWENERGY.NS | vwap_breakout | 460.70 → 487.85 | +2,634 | next day 11:00 |
| 10-01 14:22 | IDBI.NS | orb_breakout | 82.13 → 87.00 | +2,221 | eod |
| 10-15 09:21 | RITES.NS | vwap_breakout | 204.80 → 213.91 | +2,068 | strategy |
| 08-18 12:22 | GLAXO.NS | orb_breakout | 2728.90 → 2906.80 | +1,779 | eod |
| 10-08 **15:52** | ICICIGI.NS | vwap_breakout | 1614.00 → 1685.00 | +1,775 | next day eod |
| 10-13 11:38 | HDBFS.NS | intraday_momentum | 588.15 → 608.95 | +1,539 | eod |
| 09-30 09:53 | LALPATHLAB.NS | vwap_breakout | 1902.00 → 1982.60 | +1,531 | eod |
| 10-02 **15:53** | BAJFINANCE.NS | vwap_breakout | 947.90 → 984.00 | +1,480 | 10-05 10:45 |

**Bottom 10 trades:** AIIL.NS −826 (08-19) · AIIL.NS −689 (07-28) · LODHA.NS −577 · SHRIRAMFIN.NS −554 · PARADEEP.NS −434 · KIRLOSENG.NS −368 · ABB.NS −353 · ONESOURCE.NS −341 · SONATSOFTW.NS −339 · WELCORP.NS −338. Losses are capped near 1–3% per trade, while the largest wins come from end-of-day or overnight holds.

## 7. Pessimistic recompute

**Method**
- The replay covers trades with 5m data available and a same-day bar after the entry: **795 of 1,690**. Most out-of-session trades cannot be replayed, because there is no bar after the entry on that day.
- Stop levels were recovered for 517 trades by re-running the same strategy backtest and matching the entry price. For other stop-loss exits the recorded stop is used.
- **(a)** Entry at the open of the first bar after the paper decision time; exit at the open of the first bar after the recorded exit time.
- **(b)** Plus a stop-first rule: the first bar whose low reaches the stop exits at `min(open, stop)`. If the entry bar itself touches the stop, the stop is assumed hit first (138 trades).
- **(c)** Plus costs and extra slippage per side.

| ₹ | All replayed (795) | Clean in-session (741) | Clean vwap_breakout (638) |
|---|---|---|---|
| Recorded | +37,919 | +31,471 | +18,741 |
| Recorded − 0.12%/side only | −19,792 | −21,825 | −27,055 |
| (a) next-bar-open fills, no costs | +5,794 | +2,939 | **−1,217** |
| (a)+(b) stop-first, no costs | +10,746 | +6,908 | +1,293 |
| (a)+(b) − 0.03%/side (backtest cost) | −3,684 | −6,419 | −10,158 |
| … + 5 bps slippage | −27,734 | −28,630 | −29,243 |
| … + 10 bps slippage | −51,784 | −50,841 | −48,328 |
| (a)+(b) − 0.12%/side | −46,974 | −46,399 | −44,511 |
| … + 5 bps slippage | −71,024 | −68,610 | −63,596 |
| … + 10 bps slippage | **−95,074** | −90,822 | −82,681 |

- Stop-first (b) *improves* on the recorded result. A resting stop exits earlier than 15-minute polling does. So the polling exit costs money in reality, even though the recorded stop *fill price* is optimistic (§3).
- Stale entry and exit timing (a) alone removes ~80–90% of the recorded gross profit.

## 8. Stale state and uncommitted changes

**Trackers**

| Tracker | Last run | Open positions | Notes |
|---|---|---|---|
| paper_trading_daily_state.json | 2026-07-30 | 5 | Unmonitored ~11 weeks: no stop checks since July |
| paper_trading_daily_us_state.json | 2026-07-30 | 5 | Unmonitored ~11 weeks: no stop checks since July |
| paper_trading_funds_state.json | 2026-07-21 | 0 | 0 trades |

Only the intraday tracker has a cron job.

**git status:** 4 paper traders modified (`RISK_PCT` 1.0 → 10.0 only), 4 state/log files modified. `paper_trader_cron.log` is +46k lines. `CONTEXT_FOR_CLAUDE_CHAT.md` and this file are untracked. The state and log files are generated, but they are tracked in git.

---

## Most likely causes of the gap (backtest FAIL vs paper +₹89.8k), ranked

1. **Trading outside the session** (`paper_trader.py:179-238` has no time or calendar guard; the cron runs at 09:00, 15:30 and 15:45). This covers entries after square-off, pre-open entries at yesterday's price, holiday churn, and overnight carry. **+₹52.9k gross (59%) from 605 trades.**
2. **Stale entry fills.** The paper trade takes the backtest's bar-open `EntryPrice`, a median 8 and p90 23 minutes before the decision (`scan_intraday.py:129`, `paper_trader.py:232`). On replayable in-session trades this alone turns +₹31.5k into +₹2.9k.
3. **No transaction costs in paper P&L** (`paper_trader.py:158-159`). ₹53/trade gross against a ₹71 round trip at 0.12% (₹18 at 0.03%). With ~28 trades a day, costs decide the result.
4. **Stop exits filled at the stop price after a close-only poll** (`paper_trader.py:157`). This overstates P&L by about ₹28.8k on replayed stop exits.
5. **Churn re-buys of stale BUYs** (`paper_trader.py:150`). 209 re-entries; this inflates the trade count and win rate and drives the holiday trading.
6. **The square-off in the strategies leaks overnight** (`strategies_intraday.py`, close at next bar's open plus missing 15:20 bars). This contaminates the backtests as well, so the earlier intraday walk-forward verdicts are not clean in either direction.

**Not causes:** RISK_PCT 1% → 10% (the 25% cap binds either way) and the accounting arithmetic (it ties out to the paisa).
