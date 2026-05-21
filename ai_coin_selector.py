"""AI coin selection — proactively ranks scanner candidates for momentum trading.

Runs as a background task every 15 minutes. When AI is unavailable the module
returns None and the caller falls back to the existing scanner-driven logic.
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

SYSTEM_PROMPT = """You are a crypto momentum trading specialist for a spot LONG-ONLY trading bot on 15-minute charts.

Given a list of USDT spot coins from a technical scanner, select the best candidates for long momentum trades in the next 30-60 minutes.

Return JSON only:
{
  "recommendations": [
    {"symbol": "XYZUSDT", "confidence": <integer 0-100>, "reason": "<one sentence>"},
    ...
  ],
  "market_comment": "<one sentence overall market read>"
}

Rules:
- Only include coins with confidence >= 60
- Prefer: TRENDING regime, rising volume, ATR 0.3-2.5%, positive price change
- Avoid: RANGING regime, ATR > 3%, falling volume, sharp recent drop
- If market conditions are broadly poor for longs, return empty recommendations list
- Respond with JSON only, no other text"""


class AICoinSelector:
    def __init__(self):
        self.last_result: dict = {}
        self.last_run: float = 0.0
        self.available: bool = _AVAILABLE

    async def select(self, candidates: list[dict], interval_secs: int = 900) -> dict | None:
        """Rank candidates for momentum trading. Returns dict or None if unavailable/throttled."""
        if not _AVAILABLE or not _CLIENT or not candidates:
            return None
        if time.time() - self.last_run < interval_secs:
            return self.last_result or None
        try:
            lines = []
            for i, c in enumerate(candidates[:8], 1):
                lines.append(
                    f"{i}. {c['symbol']}: score={c.get('deep_score',0):.3f}, "
                    f"ATR={c.get('atr_pct',0):.2f}%, chg={c.get('chg_pct',0):+.1f}%, "
                    f"vol={c.get('vol_m',0):.0f}M, regime={c.get('regime','?')}, "
                    f"trend={c.get('trend','?')}"
                )
            user_msg = "Scanner top coins (ranked by momentum score):\n" + "\n".join(lines)
            msg = await _CLIENT.messages.create(
                model=MODEL,
                max_tokens=350,
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
            recs = result.get("recommendations", [])
            log("AI_SELECTOR", "DONE",
                top=recs[0]["symbol"] if recs else "none",
                count=len(recs),
                comment=result.get("market_comment", "")[:80])
            return result
        except Exception as e:
            log("AI_SELECTOR", "ERROR", error=str(e)[:100])
            return None
