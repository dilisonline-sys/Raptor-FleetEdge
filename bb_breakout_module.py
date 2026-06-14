"""
Bollinger Band Bounce strategy — signal module.

BUY  : Price touches or breaks below the lower band — expects mean reversion upward.
       RSI must be below 50 to confirm the dip is real (avoids fading a strong downtrend).
SELL : Price touches or breaks above the upper band — expects mean reversion downward.
       RSI must be above 50 to confirm the spike is real.

Mean-reversion by design — complements RSI Reversal and works well in
RANGING markets. Signal fires at the band touch, not after a candle close,
so it is well-suited to live-tick indicators.
"""
from logger import log


def signal_engine(indicators: dict, override: str | None = None) -> str:
    if override in ("BUY", "SELL", "CLOSE_ALL"):
        log("BB_BOUNCE", "EXTERNAL_OVERRIDE", signal=override)
        return override

    price    = indicators.get("close", 0.0)
    bb_lower = indicators.get("bb_lower", 0.0)
    bb_upper = indicators.get("bb_upper", float("inf"))
    rsi      = indicators.get("rsi14", 50.0)

    if bb_lower > 0 and price <= bb_lower and rsi < 50:
        result = "BUY"
    elif bb_upper < float("inf") and price >= bb_upper and rsi > 50:
        result = "SELL"
    else:
        result = "NONE"

    log("BB_BOUNCE", "EVAL",
        price=round(price, 4),
        bb_lower=round(bb_lower, 4), bb_upper=round(bb_upper, 4),
        rsi=round(rsi, 2), result=result)
    return result
