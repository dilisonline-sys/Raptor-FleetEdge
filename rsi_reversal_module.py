"""
RSI Mean Reversion strategy — signal module.

BUY  : RSI drops below the oversold threshold (default 30) — exhausted sellers,
       expect a bounce. Confirmation: price below EMA21 (confirms dip is real).
SELL : RSI rises above the overbought threshold (default 70) — exhausted buyers,
       expect a pullback. Confirmation: price above EMA21.

Contrarian by design — works best in RANGING markets, not during strong trends.
"""
from logger import log

RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70


def signal_engine(indicators: dict, override: str | None = None) -> str:
    if override in ("BUY", "SELL", "CLOSE_ALL"):
        log("RSI_REV", "EXTERNAL_OVERRIDE", signal=override)
        return override

    rsi   = indicators.get("rsi14", 50)
    price = indicators.get("close", 0)
    e21   = indicators.get("ema21", price)

    if rsi <= RSI_OVERSOLD and price < e21:
        result = "BUY"
    elif rsi >= RSI_OVERBOUGHT and price > e21:
        result = "SELL"
    else:
        result = "NONE"

    log("RSI_REV", "EVAL",
        rsi=round(rsi, 2), price=round(price, 4), ema21=round(e21, 4),
        result=result)
    return result
