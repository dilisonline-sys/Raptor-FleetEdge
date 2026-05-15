"""Claude Haiku market analyst — per-cycle trade thesis via Anthropic API."""
import os
from logger import log

try:
    import anthropic as _anthropic
    _HAS_SDK = True
except ImportError:
    _HAS_SDK = False

_MODEL = "claude-haiku-4-5-20251001"


class ClaudeAnalyst:
    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._enabled = _HAS_SDK and bool(api_key)
        if self._enabled:
            self._client = _anthropic.AsyncAnthropic(api_key=api_key)
            log("CLAUDE_ANALYST", "READY", model=_MODEL)
        else:
            self._client = None
            log("CLAUDE_ANALYST", "DISABLED",
                reason="missing ANTHROPIC_API_KEY or anthropic package")

    async def analyze(
        self,
        symbol: str,
        indicators: dict,
        regime: str,
        fear_greed: dict,
        signal: str,
    ) -> str:
        if not self._enabled:
            return ""
        try:
            prompt = (
                f"Symbol: {symbol} | Regime: {regime} | Signal engine output: {signal}\n"
                f"Fear/Greed Index: {fear_greed['value']} ({fear_greed['label']})\n"
                f"RSI14: {indicators['rsi14']:.1f} | "
                f"EMA9/21/50: {indicators['ema9']:.2f}/{indicators['ema21']:.2f}/{indicators['ema50']:.2f}\n"
                f"MACD: {indicators['macd']:.4f} vs signal {indicators['macd_signal']:.4f}\n"
                f"ATR14: {indicators['atr14']:.4f} | VWAP: {indicators['vwap']:.2f}\n\n"
                f"You are dipu, a 20-year crypto veteran. In exactly 2 sentences: "
                f"state your trade thesis for {symbol} right now and confirm or "
                f"override the signal engine's {signal} call. Be direct and decisive."
            )
            msg = await self._client.messages.create(
                model=_MODEL,
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            thesis = msg.content[0].text.strip()
            log("CLAUDE_ANALYST", "THESIS", symbol=symbol, signal=signal,
                fg=fear_greed["value"], snippet=thesis[:80])
            return thesis
        except Exception as e:
            log("CLAUDE_ANALYST", "ERROR", error=str(e))
            return ""
