"""
Strategy Advisor — scores all 5 strategies against current market indicators.

Produces a fitness score (0–10) for each strategy and a ranked recommendation.
Called every agent cycle; result pushed to state so the dashboard can display
which strategy is best suited right now without forcing an auto-switch.

Scoring inputs:
  adx14       — trend strength (>25 trending, <20 ranging)
  choppiness14— market structure (<38 trending, >62 choppy)
  bb_width    — band width % (squeeze <2% precedes breakout)
  volume_ratio— current vol / 20-bar SMA (>1.5 = surge)
  stoch_rsi_k — sensitive reversal signal
  rsi14       — overbought/oversold level
  regime      — TRENDING / RANGING / VOLATILE from RegimeClassifier
"""
from logger import log


# ── Thresholds ────────────────────────────────────────────────────────────────
_ADX_TREND    = 25.0
_ADX_WEAK     = 15.0
_CHOP_TREND   = 45.0   # below this = trending market
_CHOP_RANGE   = 58.0   # above this = ranging market
_BB_SQUEEZE   = 2.5    # bb_width % — tight bands signal imminent breakout
_VOL_SURGE    = 1.5    # volume_ratio above this = surge confirmation

_STRATEGY_LABELS = {
    "momentum":     "Volatility Chase · Momentum",
    "ema_cross":    "EMA Cross · Crossover",
    "macd_cross":   "MACD · Crossover",
    "rsi_reversal": "RSI · Mean Reversion",
    "bb_breakout":  "Bollinger · Band Bounce",
}


def score_strategies(indicators: dict, regime: str) -> dict[str, dict]:
    """
    Returns {strategy_key: {"score": float, "reason": str}} for all 5 strategies.
    Score range 0–10 where 10 = ideal conditions.
    """
    adx        = indicators.get("adx14",        20.0)
    chop       = indicators.get("choppiness14", 50.0)
    bb_width   = indicators.get("bb_width",      4.0)
    vol_ratio  = indicators.get("volume_ratio",  1.0)
    srsi_k     = indicators.get("stoch_rsi_k",  50.0)
    rsi        = indicators.get("rsi14",        50.0)
    macd_hist  = indicators.get("macd_hist",     0.0)

    is_trending = (regime == "TRENDING") or (adx > _ADX_TREND and chop < _CHOP_TREND)
    is_ranging  = (regime == "RANGING")  or (adx < _ADX_WEAK  and chop > _CHOP_RANGE)
    is_volatile = (regime == "VOLATILE")
    squeeze     = bb_width < _BB_SQUEEZE
    vol_surge   = vol_ratio > _VOL_SURGE
    rsi_extreme = rsi < 32 or rsi > 68
    srsi_extreme = srsi_k < 20 or srsi_k > 80

    results: dict[str, dict] = {}

    # ── Volatility Chase / Momentum ───────────────────────────────────────────
    if is_volatile:
        score = 3.0
        reason = "VOLATILE regime — momentum gets whipsawed"
    elif is_trending:
        score = 9.0 if adx > 30 else 7.5
        reason = f"TRENDING (ADX={adx:.0f}) — momentum is in its element"
        if vol_surge:
            score = min(score + 0.5, 10.0)
            reason += ", volume surge confirms"
    elif is_ranging:
        score = 2.0
        reason = "RANGING market — momentum churns on fees in chop"
    else:
        score = 5.0
        reason = "mixed signals — momentum may work but not optimal"
    results["momentum"] = {"score": round(score, 1), "reason": reason}

    # ── EMA Cross · Crossover ─────────────────────────────────────────────────
    if is_volatile:
        score = 2.0
        reason = "VOLATILE — live-candle noise generates phantom EMA crosses"
    elif is_ranging:
        score = 2.5
        reason = f"RANGING (chop={chop:.0f}) — EMA crosses in flat markets are mostly false"
        if adx < _ADX_WEAK:
            reason += f", ADX={adx:.0f} confirms weak trend"
    elif is_trending:
        score = 8.5 if adx > 30 else 7.0
        reason = f"TRENDING (ADX={adx:.0f}) — clean EMA cross signals"
    else:
        score = 5.0
        reason = "ADX neutral — EMA crosses may be reliable but verify"
    results["ema_cross"] = {"score": round(score, 1), "reason": reason}

    # ── MACD · Crossover ──────────────────────────────────────────────────────
    hist_growing = macd_hist > 0  # positive histogram = bullish momentum building
    if is_volatile:
        score = 3.0
        reason = "VOLATILE — MACD histogram expands erratically in flash moves"
    elif is_ranging:
        score = 3.5
        reason = "RANGING — MACD crosses frequently without trend to sustain them"
    elif is_trending:
        score = 8.0 if adx > 28 else 6.5
        reason = f"TRENDING (ADX={adx:.0f}) — MACD crossovers align with trend momentum"
        if hist_growing:
            score = min(score + 0.5, 10.0)
            reason += ", histogram building"
    else:
        score = 5.0
        reason = "neutral trend — MACD crossovers hit ~50% in directionless markets"
    results["macd_cross"] = {"score": round(score, 1), "reason": reason}

    # ── RSI · Mean Reversion ─────────────────────────────────────────────────
    if is_volatile:
        score = 6.0 if rsi_extreme else 3.0
        reason = ("VOLATILE + RSI extreme — oversold/overbought bounces can be sharp"
                  if rsi_extreme else "VOLATILE — RSI extremes unclear without ranging context")
    elif is_ranging:
        score = 8.5 if srsi_extreme else 6.5
        reason = (f"RANGING (chop={chop:.0f}) + Stoch-RSI extreme ({srsi_k:.0f}) — ideal conditions"
                  if srsi_extreme else f"RANGING market — RSI mean reversion in its element")
        if rsi_extreme:
            score = min(score + 0.5, 10.0)
            reason += f", RSI={rsi:.0f} confirms extreme"
    elif is_trending:
        score = 2.5
        reason = "TRENDING — counter-trend RSI reversals fail; trend over-extends RSI"
    else:
        score = 4.5
        reason = "mixed — RSI reversal marginal without clear ranging structure"
    results["rsi_reversal"] = {"score": round(score, 1), "reason": reason}

    # ── Bollinger · Band Bounce ───────────────────────────────────────────────
    bb_pct  = indicators.get("bb_pct", 0.5)
    at_band = bb_pct <= 0.05 or bb_pct >= 0.95   # price touching band edge
    if is_volatile:
        score = 4.0
        reason = "VOLATILE — bands expand sharply; breakouts often continue rather than reverse"
        if at_band and vol_surge:
            score = 5.5
            reason += "; price at band + volume surge — reversal possible"
    elif is_ranging:
        score = 9.0 if (at_band and squeeze) else (7.5 if at_band else 5.5)
        if squeeze:
            reason = f"RANGING + BB squeeze ({bb_width:.1f}%) — band touch after squeeze is high-probability bounce"
        elif at_band:
            reason = f"RANGING + price at band ({bb_pct:.2f}) — classic mean-reversion entry"
        else:
            reason = "RANGING market — wait for price to touch a band for entry"
        if vol_surge:
            score = min(score + 0.5, 10.0)
            reason += ", volume confirms"
    elif is_trending:
        score = 3.5
        reason = "TRENDING — price rides the upper/lower band; bounces fail quickly"
        if squeeze:
            score = 5.5
            reason = f"TRENDING + BB squeeze ({bb_width:.1f}%) — squeeze may precede continuation break"
    else:
        score = 5.0
        reason = f"neutral — BB width={bb_width:.1f}%; watch for squeeze below {_BB_SQUEEZE}%"
    results["bb_breakout"] = {"score": round(score, 1), "reason": reason}

    log("ADVISOR", "SCORES", regime=regime, adx=round(adx, 1), chop=round(chop, 1),
        bb_width=round(bb_width, 2), vol_ratio=round(vol_ratio, 2),
        momentum=results["momentum"]["score"],
        ema_cross=results["ema_cross"]["score"],
        macd_cross=results["macd_cross"]["score"],
        rsi_reversal=results["rsi_reversal"]["score"],
        bb_breakout=results["bb_breakout"]["score"])

    return results


def best_strategy(indicators: dict, regime: str) -> tuple[str, float, str]:
    """Returns (strategy_key, score, reason) for the highest-scoring strategy."""
    scores = score_strategies(indicators, regime)
    top = max(scores.items(), key=lambda kv: kv[1]["score"])
    key, data = top
    return key, data["score"], data["reason"]


def advice_payload(indicators: dict, regime: str) -> dict:
    """
    Convenience wrapper — returns a dict ready to merge into agent state.
    Includes ranked list and top recommendation.
    """
    scores  = score_strategies(indicators, regime)
    ranked  = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)
    top_key, top_data = ranked[0]
    return {
        "advised_strategy":        top_key,
        "advised_strategy_label":  _STRATEGY_LABELS.get(top_key, top_key),
        "advised_strategy_score":  top_data["score"],
        "advised_strategy_reason": top_data["reason"],
        "strategy_scores": [
            {"key": k, "label": _STRATEGY_LABELS.get(k, k),
             "score": v["score"], "reason": v["reason"]}
            for k, v in ranked
        ],
    }
