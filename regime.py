"""Module 8 — Market Regime Classification."""
import config as cfg
from logger import log


class RegimeClassifier:
    # FIX-11: stateful 2-candle hysteresis — regime only changes after 2 consecutive
    # identical readings, preventing TRENDING/RANGING flicker on every EMA oscillation.
    def __init__(self):
        self._last_raw    = "RANGING"  # last single-candle reading
        self._confirmed   = "RANGING"  # stable regime (changes only after 2 consecutive)
        self._streak      = 0          # how many candles in a row match _last_raw

    def classify(self, ind: dict, atr_20bar_avg: float | None = None) -> str:
        e9, e21, e50 = ind["ema9"], ind["ema21"], ind["ema50"]
        atr          = ind["atr14"]
        price        = ind.get("vwap", e21)
        vol_ratio    = atr / price if price else 0

        # Volatile first — override everything
        # 5% ATR/price: normal BTC chop (3-4%) stays tradeable; only flash-crash events blocked
        spike = (atr / atr_20bar_avg) > 2 if atr_20bar_avg else False
        if vol_ratio > 0.05 or spike:
            raw = "VOLATILE"
        elif (e9 > e21 and e9 > e50) or (e9 < e21 and e9 < e50):
            # TRENDING: EMA9 must agree with EMA50 direction AND lead EMA21.
            # This captures early trends where EMA21 hasn't yet crossed EMA50
            # (the most profitable entry zone) without requiring perfect stack alignment.
            # Strict stack (e9>e21>e50) is a special case that always satisfies this.
            raw = "TRENDING"
        else:
            raw = "RANGING"

        # Hysteresis: require 2 consecutive same readings before confirming a regime change
        if raw == self._last_raw:
            self._streak += 1
        else:
            self._streak   = 1
            self._last_raw = raw

        if self._streak >= 2 and raw != self._confirmed:
            self._confirmed = raw

        regime    = self._confirmed
        size_mult = {"TRENDING": 1.0, "RANGING": 0.5, "VOLATILE": 0.25}[regime]
        log("MODULE_8", "REGIME", regime=regime, raw=raw, streak=self._streak,
            vol_ratio=round(vol_ratio, 4), size_mult=size_mult)
        return regime
