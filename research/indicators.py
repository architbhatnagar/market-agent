"""
Indicator functions used by strategies. Plain pandas/numpy — no TA-lib
dependency headaches. Each takes arrays/Series, returns a numpy array,
suitable for backtesting.py's self.I() wrapper.
"""
import numpy as np
import pandas as pd


def sma(series, n: int):
    return pd.Series(series).rolling(n).mean().values


def ema(series, n: int):
    return pd.Series(series).ewm(span=n, adjust=False).mean().values


def rsi(series, n: int = 14):
    s = pd.Series(series)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).values


def atr(high, low, close, n: int = 14):
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean().values


def adx(high, low, close, n: int = 14):
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    up = h.diff()
    down = -l.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0))
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().values


def rolling_max(series, n: int):
    return pd.Series(series).rolling(n).max().values


def rolling_min(series, n: int):
    return pd.Series(series).rolling(n).min().values


def session_vwap(high, low, close, volume, index):
    """VWAP that resets each trading session (groups by calendar date of `index`)."""
    h, l, c, v = pd.Series(high), pd.Series(low), pd.Series(close), pd.Series(volume)
    typical = (h + l + c) / 3
    day = pd.Series(pd.DatetimeIndex(index)).dt.date
    cum_pv = (typical * v).groupby(day).cumsum()
    cum_v = v.groupby(day).cumsum()
    return (cum_pv / cum_v).values


def _opening_range(high, low, index, bars, which):
    h, l = pd.Series(high), pd.Series(low)
    day = pd.Series(pd.DatetimeIndex(index)).dt.date
    bar_num = day.groupby(day).cumcount()
    src = h if which == "high" else l
    agg = (lambda x: x.iloc[:bars].max()) if which == "high" else (lambda x: x.iloc[:bars].min())
    val = src.groupby(day).transform(agg)
    return val.where(bar_num >= bars - 1).values


def orb_high(high, low, index, bars: int = 3):
    """Opening-range high: max High of the first `bars` bars of each session. NaN until formed."""
    return _opening_range(high, low, index, bars, "high")


def orb_low(high, low, index, bars: int = 3):
    """Opening-range low: min Low of the first `bars` bars of each session. NaN until formed."""
    return _opening_range(high, low, index, bars, "low")
