"""Rule-based coin selection — profitability-first analysis before assigning coins to agents.

Triggered when an agent's session P&L drops to zero or below. Runs a fully
deterministic profitability assessment (regime, R:R estimate, momentum, market
sentiment) and only recommends coins where a long trade has a realistic
positive edge. No external API, no LLM.
"""
import time
from logger import log


def _build_candidates(raw_candidates: list[dict]) -> list[dict]:
    """Pre-filter and enrich candidates with profitability metrics."""
    enriched = []
    for c in raw_candidates:
        atr_pct = c.get("atr_pct", 0)
        regime  = c.get("regime", "RANGING")
        trend   = c.get("trend", "neutral")
        chg_pct = c.get("chg_pct", 0)

        # Hard pre-filter: skip coins that can't possibly be profitable
        if regime == "RANGING":
            continue
        if atr_pct > 3.0 or atr_pct < 0.1:
            continue
        if trend == "bearish" and chg_pct < -2.0:
            continue

        # Estimate R:R: stop = 1×ATR, TP1 = 1.5×ATR → floor at 1.5R by our TP formula
        stop_pct = atr_pct * 1.5
        tp1_pct  = max(stop_pct * 1.5, atr_pct * 0.4 * 100 / max(atr_pct, 0.01))
        est_rr   = round(tp1_pct / stop_pct, 2) if stop_pct else 0

        enriched.append({**c, "est_rr": est_rr})

    return enriched


def _assess(c: dict, fg_val: int) -> tuple[bool, int, str]:
    """Score a single candidate. Returns (profitable, confidence, reason).

    Mirrors the strict rules the AI selector used:
    - TRENDING regime required, ATR 0.2–2.5%, est_rr >= 1.3
    - positive chg% strongly preferred, no bearish-trend entries
    - confidence bar 65, raised to 75 under Fear & Greed < 30
    """
    regime  = c.get("regime", "RANGING")
    trend   = c.get("trend", "neutral")
    atr_pct = c.get("atr_pct", 0)
    chg_pct = c.get("chg_pct", 0)
    vol_m   = c.get("vol_m", 0)
    score   = c.get("deep_score", 0)
    est_rr  = c.get("est_rr", 0)

    if regime != "TRENDING":
        return False, 0, f"regime {regime} — TRENDING required"
    if not (0.2 <= atr_pct <= 2.5):
        return False, 0, f"ATR {atr_pct:.2f}% outside 0.2–2.5% band"
    if est_rr < 1.3:
        return False, 0, f"est R:R {est_rr:.2f}x below 1.3x edge floor"
    if trend == "bearish":
        return False, 0, "bearish trend — no long edge"

    confidence = 50
    if trend == "bullish":  confidence += 12
    if chg_pct > 1.0:       confidence += 10
    elif chg_pct > 0:       confidence += 6
    elif chg_pct < -1.0:    confidence -= 12
    if est_rr >= 1.5:       confidence += 8
    if 0.4 <= atr_pct <= 1.5:  confidence += 6   # sweet spot for stop width
    if vol_m >= 50:         confidence += 5
    confidence += min(10, int(score * 10))
    confidence = min(95, max(0, confidence))

    bar = 75 if fg_val < 30 else 65
    profitable = confidence >= bar and chg_pct > -0.5
    reason = (
        f"TRENDING {trend} momentum, {chg_pct:+.2f}% change, ATR {atr_pct:.2f}%, "
        f"est R:R {est_rr:.2f}x (confidence bar {bar})"
    )
    return profitable, confidence, reason


class RuleCoinSelector:
    def __init__(self):
        self.last_result: dict = {}
        self.last_run: float = 0.0
        self.available: bool = True

    async def select(self, candidates: list[dict], fear_greed: dict | None = None,
                     interval_secs: int = 900) -> dict | None:
        """
        Run profitability analysis on candidates and return recommendations.
        Returns dict or None if throttled / no candidates survive pre-filtering.
        interval_secs=0 forces a fresh run regardless of last run time.
        """
        if not candidates:
            return None
        if interval_secs > 0 and time.time() - self.last_run < interval_secs:
            return self.last_result or None

        enriched = _build_candidates(candidates)
        if not enriched:
            log("SELECTOR", "NO_CANDIDATES", reason="all pre-filtered out")
            return None

        fg_val = fear_greed.get("value", 50) if fear_greed else 50

        recommendations, avoided = [], []
        for c in enriched[:6]:
            profitable, confidence, reason = _assess(c, fg_val)
            if profitable:
                recommendations.append({
                    "symbol":     c["symbol"],
                    "confidence": confidence,
                    "profitable": True,
                    "est_rr":     c.get("est_rr", 0),
                    "reason":     reason,
                })
            else:
                avoided.append(c["symbol"])

        recommendations.sort(key=lambda r: r["confidence"], reverse=True)

        if fg_val < 30:
            market_comment = f"Extreme fear (F&G {fg_val}) — confidence bar raised to 75 for longs."
        elif fg_val < 45:
            market_comment = f"Fearful market (F&G {fg_val}) — only strong TRENDING setups considered."
        elif fg_val > 70:
            market_comment = f"Greedy market (F&G {fg_val}) — momentum longs favoured, watch for exhaustion."
        else:
            market_comment = f"Neutral sentiment (F&G {fg_val}) — standard profitability criteria applied."

        result = {
            "recommendations": recommendations,
            "avoided":         avoided,
            "market_comment":  market_comment,
        }
        self.last_result = result
        self.last_run = time.time()

        log("SELECTOR", "DONE",
            profitable=len(recommendations),
            avoided=len(avoided),
            top=recommendations[0]["symbol"] if recommendations else "none",
            fg=fg_val,
            comment=market_comment[:80])
        return result
