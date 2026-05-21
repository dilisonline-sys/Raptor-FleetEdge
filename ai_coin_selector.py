"""AI coin selection — profitability-first analysis before assigning coins to agents.

Triggered when an agent's session P&L drops to zero or below. Runs a full
profitability assessment (regime, R:R estimate, momentum, market sentiment)
and only recommends coins where a long trade has a realistic positive edge.
Falls back gracefully when Anthropic is unavailable.
"""
import json
import time
from logger import log

try:
    import anthropic as _anthropic
    _CLIENT = _anthropic.AsyncAnthropic()
    _AVAILABLE = True
except Exception:
    _CLIENT = None
    _AVAILABLE = False

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a professional crypto trading analyst evaluating whether specific coins are PROFITABLE to trade right now for a spot LONG-ONLY momentum bot on 15-minute charts.

Your job is profitability assessment — not just ranking coins by score. Only recommend a coin if you genuinely believe a long entry NOW has a positive expected value (positive R:R, real momentum, favourable regime).

For each candidate you are given:
- regime: TRENDING / RANGING / VOLATILE
- trend: bullish / bearish / neutral
- ATR%: average true range as % of price (stop-loss width proxy)
- chg%: recent price change
- vol: 24h volume in millions USDT
- est_rr: estimated reward:risk ratio based on ATR (1.5R target)
- score: scanner momentum score

Return JSON only:
{
  "recommendations": [
    {
      "symbol": "XYZUSDT",
      "confidence": <integer 0-100>,
      "profitable": <true|false>,
      "est_rr": <float>,
      "reason": "<one sentence explaining WHY this trade is profitable>"
    }
  ],
  "avoided": ["AAAUSDT", "BBBUSDT"],
  "market_comment": "<one sentence — current market condition for longs>"
}

Strict rules:
- Only include coins where profitable=true AND confidence >= 65
- TRENDING regime required — never recommend RANGING or VOLATILE
- Positive chg% strongly preferred — do not enter coins in a downtrend
- ATR must be 0.2–2.5% — outside this range the R:R math breaks down
- est_rr must be >= 1.3 to have a positive edge after fees
- If Fear & Greed is below 30 (Fear), raise the bar: require confidence >= 75
- If no coin meets the profitability criteria, return empty recommendations
- Respond with JSON only, no other text"""


def _build_candidates(raw_candidates: list[dict]) -> list[dict]:
    """Pre-filter and enrich candidates with profitability metrics before sending to AI."""
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


class AICoinSelector:
    def __init__(self):
        self.last_result: dict = {}
        self.last_run: float = 0.0
        self.available: bool = _AVAILABLE

    async def select(self, candidates: list[dict], fear_greed: dict | None = None,
                     interval_secs: int = 900) -> dict | None:
        """
        Run profitability analysis on candidates and return recommendations.
        Returns dict or None if AI unavailable / throttled / no profitable coins.
        interval_secs=0 forces a fresh run regardless of last run time.
        """
        if not _AVAILABLE or not _CLIENT or not candidates:
            return None
        if interval_secs > 0 and time.time() - self.last_run < interval_secs:
            return self.last_result or None

        enriched = _build_candidates(candidates)
        if not enriched:
            log("AI_SELECTOR", "NO_CANDIDATES", reason="all pre-filtered out")
            return None

        try:
            fg_val   = fear_greed.get("value", 50) if fear_greed else 50
            fg_label = fear_greed.get("label", "Neutral") if fear_greed else "Neutral"

            lines = []
            for i, c in enumerate(enriched[:6], 1):
                lines.append(
                    f"{i}. {c['symbol']}: regime={c.get('regime','?')}, "
                    f"trend={c.get('trend','?')}, ATR={c.get('atr_pct',0):.2f}%, "
                    f"chg={c.get('chg_pct',0):+.2f}%, vol={c.get('vol_m',0):.0f}M, "
                    f"score={c.get('deep_score',0):.3f}, est_rr={c.get('est_rr',0):.2f}x"
                )

            user_msg = (
                f"MARKET SENTIMENT: Fear & Greed = {fg_val} ({fg_label})\n\n"
                f"Candidate coins for profitability assessment:\n"
                + "\n".join(lines)
                + "\n\nAssess each for profitable LONG entry potential right now."
            )

            msg = await _CLIENT.messages.create(
                model=MODEL,
                max_tokens=450,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )

            raw = msg.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw.strip())
            self.last_result = result
            self.last_run = time.time()

            try:
                from ai_logger import record_call
                record_call(MODEL, msg.usage.input_tokens, msg.usage.output_tokens, task="coin_selector")
            except Exception:
                pass

            recs     = [r for r in result.get("recommendations", []) if r.get("profitable")]
            avoided  = result.get("avoided", [])
            log("AI_SELECTOR", "DONE",
                profitable=len(recs),
                avoided=len(avoided),
                top=recs[0]["symbol"] if recs else "none",
                fg=fg_val,
                comment=result.get("market_comment", "")[:80])
            return result

        except Exception as e:
            log("AI_SELECTOR", "ERROR", error=str(e)[:100])
            return None
