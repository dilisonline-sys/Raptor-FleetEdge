"""
MACD Crossover strategy — signal module.

BUY  (bullish cross): MACD line was at/below signal last cycle, now above.
SELL (bearish cross): MACD line was at/above signal last cycle, now below.

Uses the same consecutive-cycle comparison as ema_cross so the caller must
pass prev_indicators from the immediately preceding cycle (None on first cycle).
"""
from logger import log


def signal_engine(indicators: dict, prev_indicators: dict | None,
                  override: str | None = None) -> str:
    if override in ("BUY", "SELL", "CLOSE_ALL"):
        log("MACD_CROSS", "EXTERNAL_OVERRIDE", signal=override)
        return override

    if prev_indicators is None:
        return "NONE"

    macd_now  = indicators.get("macd",        0.0)
    sig_now   = indicators.get("macd_signal", 0.0)
    macd_prev = prev_indicators.get("macd",        0.0)
    sig_prev  = prev_indicators.get("macd_signal", 0.0)

    bullish = macd_prev <= sig_prev and macd_now > sig_now
    bearish = macd_prev >= sig_prev and macd_now < sig_now

    result = "BUY" if bullish else ("SELL" if bearish else "NONE")

    log("MACD_CROSS", "EVAL",
        macd_prev=round(macd_prev, 6), sig_prev=round(sig_prev, 6),
        macd_now=round(macd_now, 6),   sig_now=round(sig_now, 6),
        bullish=bullish, bearish=bearish, result=result)
    return result
