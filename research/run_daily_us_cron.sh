#!/bin/bash
# Launchd wrapper for the US paper tracker (one-week trial, ends 2026-10-24).
# launchd runs missed jobs when the Mac wakes; cron does not. Because of that,
# this script can fire several times a day, and the guards keep it to one real run.
ROOT=/Users/classplus/Git/market-agent
LOG=$ROOT/research/paper_trader_daily_us_cron.log
export TZ=Asia/Kolkata

[ "$(date +%Y%m%d)" -le 20261024 ] || exit 0          # trial over
dow=$(date +%u)                                       # 1=Mon .. 7=Sun
[ "$dow" -ge 2 ] && [ "$dow" -le 6 ] || exit 0        # Tue-Sat (after a US session)
hour=$(date +%H)
# Skip during US market hours (~19:00-02:00 IST) so we never fill at a partial bar.
[ "$hour" -ge 19 ] && exit 0
# Already ran today?
grep -qE "^=== $(date '+%a %b %e') .* $(date +%Y)" "$LOG" 2>/dev/null && exit 0

{ echo "=== $(date) ==="; "$ROOT/.venv/bin/python" "$ROOT/research/paper_trader_daily_us.py" --run; } >> "$LOG" 2>&1
