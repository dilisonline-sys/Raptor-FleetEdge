"""Minimal pandas_ta shim — implements only the functions used by Raptor FleetEdge.

Provides the same call signatures and return shapes as pandas_ta so no
other file needs to change. Uses pure pandas/numpy — no external deps.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder smoothing)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line, and histogram.

    Returns a DataFrame with column names matching pandas_ta:
      MACD_{fast}_{slow}_{signal}   — MACD line
      MACDs_{fast}_{slow}_{signal}  — signal line
      MACDh_{fast}_{slow}_{signal}  — histogram
    """
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    sig_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - sig_line
    prefix = f"{fast}_{slow}_{signal}"
    return pd.DataFrame({
        f"MACD_{prefix}":  macd_line,
        f"MACDs_{prefix}": sig_line,
        f"MACDh_{prefix}": hist,
    }, index=series.index)


def bbands(series: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands.

    Returns a DataFrame with column names matching pandas_ta:
      BBL_{length}_{std}   — lower band
      BBM_{length}_{std}   — middle band (SMA)
      BBU_{length}_{std}   — upper band
      BBB_{length}_{std}   — bandwidth
      BBP_{length}_{std}   — percent-B
    """
    mid = series.rolling(length).mean()
    sd  = series.rolling(length).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd
    bw = (upper - lower) / mid.replace(0, np.nan)
    pct = (series - lower) / (upper - lower).replace(0, np.nan)
    # pandas_ta uses "{length}_{int_std}_{float_std}" e.g. "20_2_2.0"
    int_std = int(std)
    float_std = float(std)
    prefix = f"{length}_{int_std}_{float_std}"
    return pd.DataFrame({
        f"BBL_{prefix}":   lower,
        f"BBM_{prefix}":   mid,
        f"BBU_{prefix}":   upper,
        f"BBB_{prefix}":   bw,
        f"BBP_{prefix}":   pct,
    }, index=series.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing, same as pandas_ta)."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()
