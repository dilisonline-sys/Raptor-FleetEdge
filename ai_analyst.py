"""AI Analyst — advisory-only Claude-powered market intelligence module.

Runs alongside the trading agent when enabled. Analyses current market data
and returns structured insights displayed on the dashboard. Does NOT influence
any trading decisions — purely informational.
"""
import asyncio
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

SYSTEM_PROMPT = """You are an expert cryptocurrency technical analyst with deep experience in short-term momentum trading on 15-minute charts.

You are advising a trading agent. Your role is purely analytical — you observe and report, you do not execute trades.

Given current market data, produce a concise JSON analysis with these exact fields:
{
  "sentiment": "BULLISH" | "NEUTRAL" | "BEARISH",
  "confidence": <integer 0-100>,
  "trend_strength": "STRONG" | "MODERATE" | "WEAK",
  "momentum": "ACCELERATING" | "STEADY" | "FADING",
  "support": <float — nearest key support price>,
  "resistance": <float — nearest key resistance price>,
  "entry_quality": "EXCELLENT" | "GOOD" | "POOR" | "AVOID",
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "suggest_rotation": <boolean — true when confidence < 50 and you would advise the agent to switch to a different coin>,
  "insight": "<2-3 sentence plain-English market read>",
  "watch": "<1 sentence — specific thing to monitor next candle>"
}

Rules:
- Base everything strictly on the data provided (no speculation beyond given indicators)
- entry_quality refers to the quality of a LONG entry at current price
- suggest_rotation must be true when confidence < 50 — the setup is too weak to justify staying on this coin
- Be concise and precise — the insight must be actionable within the next 1-3 candles
- Respond with JSON only, no other text
"""


class AIAnalyst:
    def __init__(self):
        self.enabled: bool = False
        self.last_analysis: dict = {}
        self.last_run: float = 0.0
        self._lock = asyncio.Lock()
        self.error: str = ""
        self.run_count: int = 0
        self.total_tokens: int = 0

    def toggle(self, on: bool):
        self.enabled = on
        if not on:
            self.last_analysis = {}
            self.error = ""
        log("AI_ANALYST", "TOGGLED", enabled=on)

    async def maybe_run(self, symbol: str, price: float, indicators: dict,
                        candles, regime: str, fear_greed: dict,
                        position: dict | None = None,
                        interval_secs: int = 180) -> dict | None:
        """Run analysis if enabled and interval has elapsed. Returns latest analysis."""
        if not self.enabled or not _AVAILABLE:
            return None
        if time.time() - self.last_run < interval_secs:
            return self.last_analysis or None
        async with self._lock:
            if time.time() - self.last_run < interval_secs:
                return self.last_analysis or None
            result = await self._analyse(symbol, price, indicators, candles, regime, fear_greed, position)
            if result:
                self.last_analysis = result
                self.last_run = time.time()
            return self.last_analysis or None

    async def _analyse(self, symbol, price, indicators, candles, regime, fear_greed, position):
        try:
            # Build recent candle summary (last 8 candles)
            recent = candles.tail(8) if hasattr(candles, 'tail') else candles[-8:]
            candle_lines = []
            for _, row in (recent.iterrows() if hasattr(recent, 'iterrows') else enumerate(recent)):
                if hasattr(row, 'get'):
                    o, h, l, c, v = row.get('open',0), row.get('high',0), row.get('low',0), row.get('close',0), row.get('volume',0)
                else:
                    o, h, l, c, v = row['open'], row['high'], row['low'], row['close'], row['volume']
                body_pct = (c - o) / o * 100 if o else 0
                candle_lines.append(
                    f"  O={o:.4f} H={h:.4f} L={l:.4f} C={c:.4f} Vol={v:.0f} ({body_pct:+.2f}%)"
                )

            pos_text = "No open position"
            if position:
                pnl = position.get('pnl_pct', 0)
                pos_text = (f"OPEN {position.get('side','?')} | entry={position.get('avg_entry',0):.4f} "
                            f"| stop={position.get('stop',0):.4f} | tp1={position.get('tp1',0):.4f} "
                            f"| pnl={pnl:+.2f}%")

            user_msg = f"""SYMBOL: {symbol}
REGIME: {regime}
CURRENT PRICE: {price:.6f}
FEAR & GREED: {fear_greed.get('value', 50)} ({fear_greed.get('label', 'Neutral')})
POSITION: {pos_text}

INDICATORS (15m):
  EMA9={indicators.get('ema9', 0):.4f}  EMA21={indicators.get('ema21', 0):.4f}  EMA50={indicators.get('ema50', 0):.4f}
  RSI14={indicators.get('rsi14', 0):.1f}
  MACD={indicators.get('macd', 0):.4f}  Signal={indicators.get('macd_signal', 0):.4f}
  BB_lower={indicators.get('bb_lower', 0):.4f}  BB_upper={indicators.get('bb_upper', 0):.4f}
  ATR14={indicators.get('atr14', 0):.4f}
  VWAP={indicators.get('vwap', 0):.4f}
  1h_range={indicators.get('h1_range', 0):.4f}

RECENT CANDLES (last 8 × 15m, newest last):
{chr(10).join(candle_lines)}
"""

            msg = await _CLIENT.messages.create(
                model=MODEL,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )

            raw = msg.content[0].text.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            analysis = json.loads(raw.strip())
            analysis["ts"] = round(time.time())
            analysis["symbol"] = symbol

            self.run_count += 1
            in_tok  = msg.usage.input_tokens
            out_tok = msg.usage.output_tokens
            self.total_tokens += in_tok + out_tok

            # Log to ai_logger if available
            try:
                from ai_logger import record_call
                record_call(MODEL, in_tok, out_tok, task="ai_analyst")
            except Exception:
                pass

            log("AI_ANALYST", "ANALYSIS_DONE", symbol=symbol,
                sentiment=analysis.get("sentiment"), confidence=analysis.get("confidence"),
                tokens=in_tok + out_tok)
            self.error = ""
            return analysis

        except Exception as e:
            self.error = str(e)[:120]
            log("AI_ANALYST", "ERROR", error=self.error)
            return None
