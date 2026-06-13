"""
EMA Crossover Strategy — signal module.

BUY  (golden cross): EMA9 crosses above EMA21
SELL (death cross) : EMA9 crosses below EMA21

Crossover is detected by comparing consecutive cycle EMA values.
prev_indicators must be from the immediately preceding cycle;
the caller is responsible for passing None on the first cycle.
"""
from logger import log


def signal_engine(indicators: dict, prev_indicators: dict | None,
                  override: str | None = None) -> str:
    if override in ("BUY", "SELL", "CLOSE_ALL"):
        log("EMA_CROSS", "EXTERNAL_OVERRIDE", signal=override)
        return override

    if prev_indicators is None:
        return "NONE"

    e9_now   = indicators["ema9"]
    e21_now  = indicators["ema21"]
    e9_prev  = prev_indicators["ema9"]
    e21_prev = prev_indicators["ema21"]

    # Golden cross: EMA9 was at/below EMA21 last cycle, now above
    golden_cross = e9_prev <= e21_prev and e9_now > e21_now
    # Death cross:  EMA9 was at/above EMA21 last cycle, now below
    death_cross  = e9_prev >= e21_prev and e9_now < e21_now

    result = "BUY" if golden_cross else ("SELL" if death_cross else "NONE")

    log("EMA_CROSS", "EVAL",
        e9_prev=round(e9_prev, 4), e21_prev=round(e21_prev, 4),
        e9_now=round(e9_now, 4),  e21_now=round(e21_now, 4),
        golden_cross=golden_cross, death_cross=death_cross, result=result)

    return result
