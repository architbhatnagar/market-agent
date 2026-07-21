"""
Strategy library. Each class is a backtesting.py Strategy — the same rules
run in backtests and in the daily scanner (no separate signal code, which
is how lookahead bugs sneak in).

All are long-only, daily-timeframe, ATR-based stops. Class params are
optimizable via Backtest.optimize().
"""
import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover

from indicators import sma, ema, rsi, atr, adx, rolling_max


class SmaCross(Strategy):
    """Classic trend-following: fast SMA crosses slow SMA."""
    n_fast = 20
    n_slow = 50
    atr_mult = 3.0

    def init(self):
        self.ma_fast = self.I(sma, self.data.Close, self.n_fast)
        self.ma_slow = self.I(sma, self.data.Close, self.n_slow)
        self.atr = self.I(atr, self.data.High, self.data.Low, self.data.Close, 14)

    def next(self):
        if not self.position and crossover(self.ma_fast, self.ma_slow):
            self.buy(sl=self.data.Close[-1] - self.atr_mult * self.atr[-1])
        elif self.position and crossover(self.ma_slow, self.ma_fast):
            self.position.close()


class MomentumContinuation(Strategy):
    """
    Daily-timeframe version of the rule-set from your friend's signal JSON:
    strong trend (ADX >= threshold), bullish EMA stack, RSI in a
    momentum band (above floor, not overbought). Exits when the EMA
    stack breaks or the ATR stop hits.
    """
    adx_min = 25
    rsi_floor = 55
    rsi_ceiling = 78
    atr_mult = 2.5

    def init(self):
        c = self.data.Close
        self.ema20 = self.I(ema, c, 20)
        self.ema50 = self.I(ema, c, 50)
        self.ema200 = self.I(ema, c, 200)
        self.rsi = self.I(rsi, c, 14)
        self.adx = self.I(adx, self.data.High, self.data.Low, c, 14)
        self.atr = self.I(atr, self.data.High, self.data.Low, c, 14)

    def entry_ok(self) -> bool:
        return (
            self.adx[-1] >= self.adx_min
            and self.ema20[-1] > self.ema50[-1] > self.ema200[-1]  # bullish stack
            and self.data.Close[-1] > self.ema20[-1]               # price leading
            and self.rsi_floor <= self.rsi[-1] < self.rsi_ceiling
        )

    def next(self):
        if not self.position and self.entry_ok():
            self.buy(sl=self.data.Close[-1] - self.atr_mult * self.atr[-1])
        elif self.position and self.ema20[-1] < self.ema50[-1]:
            self.position.close()


class MeanReversion(Strategy):
    """
    Buy pullbacks in an uptrend: price above 200-SMA (regime filter),
    RSI dips oversold, exit when RSI recovers or stop hits.
    """
    rsi_buy = 32
    rsi_exit = 60
    atr_mult = 2.0

    def init(self):
        c = self.data.Close
        self.sma200 = self.I(sma, c, 200)
        self.rsi = self.I(rsi, c, 14)
        self.atr = self.I(atr, self.data.High, self.data.Low, c, 14)

    def next(self):
        if not self.position:
            if self.data.Close[-1] > self.sma200[-1] and self.rsi[-1] < self.rsi_buy:
                self.buy(sl=self.data.Close[-1] - self.atr_mult * self.atr[-1])
        elif self.rsi[-1] > self.rsi_exit:
            self.position.close()


class Breakout52w(Strategy):
    """
    Buy closes at new N-day highs (default ~52 weeks), trail out on a
    Chandelier-style ATR stop. Momentum's blunt cousin.
    """
    lookback = 250
    atr_mult = 3.0

    def init(self):
        self.hh = self.I(rolling_max, self.data.Close, self.lookback)
        self.atr = self.I(atr, self.data.High, self.data.Low, self.data.Close, 14)
        self.trail = None

    def next(self):
        price = self.data.Close[-1]
        if not self.position:
            if price >= self.hh[-1]:
                self.trail = price - self.atr_mult * self.atr[-1]
                self.buy()
        else:
            self.trail = max(self.trail, price - self.atr_mult * self.atr[-1])
            if price < self.trail:
                self.position.close()


ALL_STRATEGIES = {
    "sma_cross": SmaCross,
    "momentum": MomentumContinuation,
    "mean_reversion": MeanReversion,
    "breakout": Breakout52w,
}


class ClenowTrend(Strategy):
    """
    Clenow's core trend model (Following the Trend, ch.4), adapted long-only:
    EMA(50)>EMA(100) trend filter; enter on 100-day high; exit on 50-day
    low or filter flip; ATR-normalized position sizing (~15bp risk factor).
    """
    n_fast = 50
    n_slow = 100
    entry_lookback = 100
    exit_lookback = 50
    risk_factor = 0.0015

    def init(self):
        c = self.data.Close
        self.ema_fast = self.I(ema, c, self.n_fast)
        self.ema_slow = self.I(ema, c, self.n_slow)
        self.hh = self.I(rolling_max, c, self.entry_lookback)
        self.ll = self.I(lambda s, n: pd.Series(s).rolling(n).min().values, c, self.exit_lookback)
        self.atr = self.I(atr, self.data.High, self.data.Low, c, 20)

    def next(self):
        price = self.data.Close[-1]
        bullish = self.ema_fast[-1] > self.ema_slow[-1]
        if not self.position:
            if bullish and price >= self.hh[-1]:
                shares = int((self.risk_factor * self.equity) / max(self.atr[-1], 1e-9))
                shares = max(1, min(shares, int(self.equity * 0.99 / price)))
                self.buy(size=shares)
        else:
            if (not bullish) or price <= self.ll[-1]:
                self.position.close()


class ZScoreReversion(Strategy):
    """
    Chan-style Bollinger z-score mean reversion (Quantitative Trading ch.3),
    long-only with a 200-SMA regime guard: enter z<=-2, exit z>=-1.
    """
    n = 20
    z_entry = -2.0
    z_exit = -1.0

    def init(self):
        c = self.data.Close
        self.mean = self.I(sma, c, self.n)
        self.std = self.I(lambda s, n: pd.Series(s).rolling(n).std().values, c, self.n)
        self.sma200 = self.I(sma, c, 200)

    def z(self):
        s = self.std[-1]
        return (self.data.Close[-1] - self.mean[-1]) / s if s and s > 0 else 0.0

    def next(self):
        if not self.position:
            if self.data.Close[-1] > self.sma200[-1] and self.z() <= self.z_entry:
                self.buy()
        elif self.z() >= self.z_exit:
            self.position.close()


ALL_STRATEGIES["clenow_trend"] = ClenowTrend
ALL_STRATEGIES["zscore_reversion"] = ZScoreReversion
