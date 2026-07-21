"""
Intraday strategy library. Same backtesting.py Strategy pattern as
strategies.py, but on intraday bars (5m default) with a hard end-of-day
square-off — no overnight positions, ever. This is a NEW, unvalidated
track (added 2026-07-19); see CLAUDE.md "Evaluation history" before
trusting any number that comes out of it.
"""
import datetime as dt

import numpy as np
import pandas as pd
from backtesting import Strategy

from indicators import ema, sma, rsi, adx, atr, session_vwap, orb_high, orb_low, rolling_max, rolling_min

SQUARE_OFF_TIME = dt.time(15, 20)  # NSE closes 15:30; flatten 10 min early


class IntradayMomentum(Strategy):
    """
    VWAP + EMA-stack + ADX momentum continuation on intraday bars.
    Adapted from the same rule family as MomentumContinuation
    (research/strategies.py), with a VWAP filter added (VWAP is only
    meaningful intraday) and a forced end-of-day exit (no overnight risk).
    """
    ema_fast = 9
    ema_slow = 20
    adx_min = 20
    rsi_floor = 55
    rsi_ceiling = 78
    atr_mult = 1.5

    def init(self):
        c, h, l, v = self.data.Close, self.data.High, self.data.Low, self.data.Volume
        self.vwap = self.I(session_vwap, h, l, c, v, self.data.index)
        self.ema_f = self.I(ema, c, self.ema_fast)
        self.ema_s = self.I(ema, c, self.ema_slow)
        self.rsi = self.I(rsi, c, 14)
        self.adx = self.I(adx, h, l, c, 14)
        self.atr = self.I(atr, h, l, c, 14)

    def entry_ok(self) -> bool:
        return (
            self.data.Close[-1] > self.vwap[-1]
            and self.ema_f[-1] > self.ema_s[-1]
            and self.adx[-1] >= self.adx_min
            and self.rsi_floor <= self.rsi[-1] < self.rsi_ceiling
        )

    def is_square_off_bar(self) -> bool:
        return self.data.index[-1].time() >= SQUARE_OFF_TIME

    def next(self):
        if self.position:
            if self.is_square_off_bar() or self.ema_f[-1] < self.ema_s[-1]:
                self.position.close()
            return
        if self.is_square_off_bar():
            return  # don't open new positions into the close
        if self.entry_ok():
            self.buy(sl=self.data.Close[-1] - self.atr_mult * self.atr[-1])


class ORBBreakout(Strategy):
    """
    Classic opening-range breakout: mark the high/low of the first
    `or_bars` bars of the session, buy a close above the range high,
    stop at the range low. Structurally different from IntradayMomentum
    — this reacts to a level break, not a trend/indicator stack.
    """
    or_bars = 3  # 3 x 5m = 15-minute opening range
    atr_mult = 1.0

    def init(self):
        h, l, c = self.data.High, self.data.Low, self.data.Close
        self.or_high = self.I(orb_high, h, l, self.data.index, self.or_bars)
        self.or_low = self.I(orb_low, h, l, self.data.index, self.or_bars)
        self.atr = self.I(atr, h, l, c, 14)

    def is_square_off_bar(self) -> bool:
        return self.data.index[-1].time() >= SQUARE_OFF_TIME

    def next(self):
        if self.position:
            if self.is_square_off_bar():
                self.position.close()
            return
        if self.is_square_off_bar() or np.isnan(self.or_high[-1]):
            return
        if self.data.Close[-1] > self.or_high[-1]:
            stop = min(self.or_low[-1], self.data.Close[-1] - self.atr_mult * self.atr[-1])
            self.buy(sl=stop)


class VWAPBreakout(Strategy):
    """
    Enter on the bar price CROSSES above VWAP with volume confirmation —
    a reclaim event, not "is currently above VWAP" (IntradayMomentum's
    filter). Exit on a close back below VWAP or the ATR stop.
    """
    atr_mult = 1.2
    vol_mult = 1.5  # bar volume vs 20-bar average, confirms the break isn't on thin volume

    def init(self):
        h, l, c, v = self.data.High, self.data.Low, self.data.Close, self.data.Volume
        self.vwap = self.I(session_vwap, h, l, c, v, self.data.index)
        self.atr = self.I(atr, h, l, c, 14)
        self.vol_avg = self.I(sma, v, 20)

    def is_square_off_bar(self) -> bool:
        return self.data.index[-1].time() >= SQUARE_OFF_TIME

    def next(self):
        if self.position:
            if self.is_square_off_bar() or self.data.Close[-1] < self.vwap[-1]:
                self.position.close()
            return
        if self.is_square_off_bar() or len(self.data.Close) < 2 or np.isnan(self.vol_avg[-1]):
            return
        crossed_up = self.data.Close[-2] <= self.vwap[-2] and self.data.Close[-1] > self.vwap[-1]
        vol_confirm = self.data.Volume[-1] > self.vol_mult * self.vol_avg[-1]
        if crossed_up and vol_confirm:
            self.buy(sl=self.data.Close[-1] - self.atr_mult * self.atr[-1])


class RangeReversion(Strategy):
    """
    Intraday range/mean-reversion — the opposite regime from the other
    three strategies here, which are all directional. Per the PDF this
    was adapted from: markets trade sideways ~20-30% of the time, so
    buy near a rolling support level when oversold and ADX confirms
    the market ISN'T trending, target the midpoint/resistance.
    """
    range_bars = 24  # 24 x 5m = 2h rolling lookback for support/resistance
    rsi_oversold = 30
    rsi_exit = 50
    adx_max = 20  # only take range trades when NOT trending (inverse of IntradayMomentum's adx_min)
    near_support_pct = 0.3  # within 0.3% of the rolling low counts as "at support"
    atr_mult = 1.0

    def init(self):
        h, l, c = self.data.High, self.data.Low, self.data.Close
        self.support = self.I(rolling_min, l, self.range_bars)
        self.resistance = self.I(rolling_max, h, self.range_bars)
        self.rsi = self.I(rsi, c, 14)
        self.adx = self.I(adx, h, l, c, 14)
        self.atr = self.I(atr, h, l, c, 14)

    def is_square_off_bar(self) -> bool:
        return self.data.index[-1].time() >= SQUARE_OFF_TIME

    def next(self):
        if self.position:
            if (self.is_square_off_bar()
                    or self.rsi[-1] >= self.rsi_exit
                    or self.data.Close[-1] >= self.resistance[-1]):
                self.position.close()
            return
        if self.is_square_off_bar() or np.isnan(self.support[-1]):
            return
        near_support = self.data.Close[-1] <= self.support[-1] * (1 + self.near_support_pct / 100)
        if near_support and self.rsi[-1] < self.rsi_oversold and self.adx[-1] < self.adx_max:
            stop = self.support[-1] - self.atr_mult * self.atr[-1]
            self.buy(sl=stop)


ALL_INTRADAY_STRATEGIES = {
    "intraday_momentum": IntradayMomentum,
    "orb_breakout": ORBBreakout,
    "vwap_breakout": VWAPBreakout,
    "range_reversion": RangeReversion,
}
