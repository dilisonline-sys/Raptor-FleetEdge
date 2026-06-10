"""Rule Analyst — advisory-only indicator-driven market intelligence module.

Runs alongside the trading agent when enabled. Computes structured insights
from technical indicators deterministically (no external API, no LLM) and
returns them for display on the dashboard. Does NOT influence any trading
decisions — purely informational.
"""
import asyncio
import time
from logger import log


def _pivot_levels(candles, price: float) -> tuple[float, float]:
    """Nearest support/resistance from recent swing lows/highs."""
    recent = candles.tail(20) if hasattr(candles, 'tail') else candles[-20:]
    lows, highs = [], []
    for _, row in (recent.iterrows() if hasattr(recent, 'iterrows') else enumerate(recent)):
        if hasattr(row, 'get'):
            lows.append(float(row.get('low', 0)))
            highs.append(float(row.get('high', 0)))
        else:
            lows.append(float(row['low']))
            highs.append(float(row['high']))
    below = [l for l in lows if l < price]
    above = [h for h in highs if h > price]
    support    = max(below) if below else (min(lows) if lows else price * 0.99)
    resistance = min(above) if above else (max(highs) if highs else price * 1.01)
    return support, resistance


def build_analysis(symbol: str, price: float, indicators: dict, candles,
                   regime: str, fear_greed: dict, position: dict | None = None) -> dict:
    """Deterministic market read producing the same fields the AI analyst used."""
    ema9   = indicators.get('ema9', 0) or 0
    ema21  = indicators.get('ema21', 0) or 0
    ema50  = indicators.get('ema50', 0) or 0
    rsi    = indicators.get('rsi14', 50) or 50
    macd   = indicators.get('macd', 0) or 0
    macd_s = indicators.get('macd_signal', 0) or 0
    atr    = indicators.get('atr14', 0) or 0
    vwap   = indicators.get('vwap', 0) or 0
    atr_pct = (atr / price * 100) if price else 0

    # ── Sentiment score: each aligned signal adds weight ──────────────
    score = 0
    if ema9 > ema21:        score += 2
    if ema21 > ema50:       score += 1
    if macd > macd_s:       score += 2
    if macd > 0:            score += 1
    if rsi > 55:            score += 1
    elif rsi < 45:          score -= 1
    if price > vwap > 0:    score += 1
    if ema9 < ema21:        score -= 2
    if ema21 < ema50:       score -= 1
    if macd < macd_s:       score -= 2

    if score >= 3:
        sentiment = "BULLISH"
    elif score <= -3:
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    # Confidence: signal alignment scaled, dampened outside TRENDING regime
    confidence = min(95, max(5, 50 + score * 7))
    if regime != "TRENDING":
        confidence = max(5, confidence - 20)
    if rsi > 75 or rsi < 25:
        confidence = max(5, confidence - 10)  # exhaustion territory

    # ── Trend strength: EMA separation relative to ATR ────────────────
    ema_spread = abs(ema9 - ema21)
    if atr and ema_spread > atr * 0.5:
        trend_strength = "STRONG"
    elif atr and ema_spread > atr * 0.2:
        trend_strength = "MODERATE"
    else:
        trend_strength = "WEAK"

    # ── Momentum: MACD histogram sign vs sentiment direction ──────────
    hist = macd - macd_s
    if (sentiment == "BULLISH" and hist > 0) or (sentiment == "BEARISH" and hist < 0):
        momentum = "ACCELERATING"
    elif abs(hist) < abs(macd) * 0.1 if macd else abs(hist) < 1e-9:
        momentum = "STEADY"
    else:
        momentum = "FADING"

    support, resistance = _pivot_levels(candles, price)

    # ── Long entry quality at current price ───────────────────────────
    room_up   = (resistance - price) / price * 100 if price else 0
    room_down = (price - support) / price * 100 if price else 0
    rr = room_up / room_down if room_down > 0.01 else 0
    if sentiment == "BULLISH" and regime == "TRENDING" and rr >= 1.5 and 45 < rsi < 70:
        entry_quality = "EXCELLENT"
    elif sentiment == "BULLISH" and rr >= 1.0:
        entry_quality = "GOOD"
    elif sentiment == "BEARISH" or rsi > 75:
        entry_quality = "AVOID"
    else:
        entry_quality = "POOR"

    # ── Risk level from volatility and sentiment extremes ─────────────
    fg_val = fear_greed.get('value', 50) if fear_greed else 50
    if atr_pct > 2.0 or regime == "VOLATILE" or fg_val < 25:
        risk_level = "HIGH"
    elif atr_pct > 0.8 or fg_val < 40:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    suggest_rotation = confidence < 50

    insight = (
        f"{symbol} is {sentiment.lower()} with {trend_strength.lower()} trend in a {regime} regime. "
        f"RSI {rsi:.0f}, MACD {'above' if macd > macd_s else 'below'} signal, "
        f"price {'above' if price > vwap else 'below'} VWAP. "
        f"Upside room to resistance {room_up:.2f}% vs downside to support {room_down:.2f}%."
    )
    if position:
        pnl = position.get('pnl_pct', 0) or 0
        insight += f" Open {position.get('side','?')} position running {pnl:+.2f}%."

    if sentiment == "BULLISH":
        watch = f"Watch for a close above {resistance:.4f} to confirm continuation."
    elif sentiment == "BEARISH":
        watch = f"Watch support at {support:.4f} — a close below opens further downside."
    else:
        watch = f"Watch the {support:.4f}–{resistance:.4f} range for a directional break."

    return {
        "sentiment":        sentiment,
        "confidence":       int(confidence),
        "trend_strength":   trend_strength,
        "momentum":         momentum,
        "support":          round(support, 6),
        "resistance":       round(resistance, 6),
        "entry_quality":    entry_quality,
        "risk_level":       risk_level,
        "suggest_rotation": suggest_rotation,
        "insight":          insight,
        "watch":            watch,
        "ts":               round(time.time()),
        "symbol":           symbol,
    }


class RuleAnalyst:
    def __init__(self):
        self.enabled: bool = False
        self.last_analysis: dict = {}
        self.last_run: float = 0.0
        self._lock = asyncio.Lock()
        self.error: str = ""
        self.run_count: int = 0

    def toggle(self, on: bool):
        self.enabled = on
        if not on:
            self.last_analysis = {}
            self.error = ""
        log("ANALYST", "TOGGLED", enabled=on)

    async def maybe_run(self, symbol: str, price: float, indicators: dict,
                        candles, regime: str, fear_greed: dict,
                        position: dict | None = None,
                        interval_secs: int = 180) -> dict | None:
        """Run analysis if enabled and interval has elapsed. Returns latest analysis."""
        if not self.enabled:
            return None
        if time.time() - self.last_run < interval_secs:
            return self.last_analysis or None
        async with self._lock:
            if time.time() - self.last_run < interval_secs:
                return self.last_analysis or None
            try:
                analysis = build_analysis(symbol, price, indicators, candles,
                                          regime, fear_greed, position)
                self.last_analysis = analysis
                self.last_run = time.time()
                self.run_count += 1
                self.error = ""
                log("ANALYST", "ANALYSIS_DONE", symbol=symbol,
                    sentiment=analysis.get("sentiment"),
                    confidence=analysis.get("confidence"))
            except Exception as e:
                self.error = str(e)[:120]
                log("ANALYST", "ERROR", error=self.error)
            return self.last_analysis or None
