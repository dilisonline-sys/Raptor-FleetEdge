"""Module 8 — Market Regime Classification."""
import config as cfg
from logger import log


class RegimeClassifier:
    def classify(self, ind: dict, atr_20bar_avg: float | None = None) -> str:
        e9, e21, e50 = ind["ema9"], ind["ema21"], ind["ema50"]
        atr          = ind["atr14"]
        price        = ind.get("vwap", e21)
        vol_ratio    = atr / price if price else 0

        # Volatile first — override everything
        spike = (atr / atr_20bar_avg) > 2 if atr_20bar_avg else False
        if vol_ratio > 0.03 or spike:
            regime = "VOLATILE"
        elif e9 > e21 > e50 or e9 < e21 < e50:
            regime = "TRENDING"
        else:
            regime = "RANGING"

        size_mult = {"TRENDING": 1.0, "RANGING": 0.5, "VOLATILE": 0.25}[regime]
        log("MODULE_8", "REGIME", regime=regime, vol_ratio=round(vol_ratio, 4), size_mult=size_mult)
        return regime
