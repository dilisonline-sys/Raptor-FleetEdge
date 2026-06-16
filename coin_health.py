"""
Coin Health Check — pre-assignment structural fitness filter.

Rejects coins that are structurally unable to generate gross P&L
large enough to cover round-trip fees, before they are ever assigned
for trading.  Called by MarketScanner after quick-rank and before
deep-scoring, so bad coins are eliminated cheaply.

Failure modes detected:
  TICK_TRAP   — min tick size ≥ 50% of ATR (coin can't move enough per candle).
                XPLUSDT at $0.08 is the canonical example: 1 tick = 0.125%,
                fee break-even = 0.15% → need 1.2 ticks of movement to profit.
  FLAT_MOVER  — ≥ 3 of the last 4 completed 15m candles had body < fee_threshold.
                Indicates the coin has been statistically unable to beat fees
                on a rolling basis, regardless of what the scanner score says.
  LOW_DAILY   — 24h price range < daily_range_min_pct (default 1.0%).
                BTC during a low-volatility day: 0.9% 24h range means each
                15m entry has almost no room to reach TP before time-exit.
  LOW_PRICE   — coin price below min_price floor (separate from config.MIN_PRICE
                so the health check can apply its own tighter threshold).
"""
import asyncio
import math
from dataclasses import dataclass
from logger import log
import config as cfg

# ── Thresholds ────────────────────────────────────────────────────────────────
FEE_ROUNDTRIP    = 0.00150   # 0.075% per side × 2 (BNB rate) — fee break-even
TICK_TRAP_RATIO  = 0.50      # reject if 1 tick ≥ 50% of ATR
FLAT_CANDLES     = 4         # look-back window (completed candles)
FLAT_MIN_PASS    = 2         # need ≥ 2 of FLAT_CANDLES with body > fee_threshold
DAILY_RANGE_MIN  = 0.010     # 1.0% minimum 24h range for any coin
BTC_DAILY_RANGE_MIN = 0.015  # 1.5% for BTC specifically (Solution 3)
MIN_PRICE_HEALTH = 50.0      # $50 floor — only trade coins above this price


@dataclass
class HealthResult:
    passed:    bool
    reason:    str   # short machine tag
    detail:    str   # human-readable detail for logs/dashboard


async def check(sym: str, ticker_24h: dict, klines_15m: list) -> HealthResult:
    """
    Run all health checks for a candidate coin.

    Parameters
    ----------
    sym        : symbol string e.g. "XPLUSDT"
    ticker_24h : dict from Binance GET /api/v3/ticker/24hr for this symbol
    klines_15m : list of recent 15m klines (at least 5 entries),
                 each entry is the raw Binance kline list:
                 [open_time, open, high, low, close, volume, ...]
    """
    try:
        price     = float(ticker_24h.get("lastPrice") or ticker_24h.get("price", 0))
        high_24h  = float(ticker_24h.get("highPrice", price))
        low_24h   = float(ticker_24h.get("lowPrice",  price))
    except (ValueError, TypeError):
        return HealthResult(False, "BAD_TICKER", f"{sym}: could not parse ticker data")

    if price <= 0:
        return HealthResult(False, "ZERO_PRICE", f"{sym}: price reported as 0")

    # ── Check: minimum price floor ────────────────────────────────────────────
    if price < MIN_PRICE_HEALTH:
        return HealthResult(
            False, "LOW_PRICE",
            f"{sym}: price ${price:.5f} < ${MIN_PRICE_HEALTH:.2f} floor — "
            f"tick size too large relative to fee threshold"
        )

    # ── Check: 24h range adequacy ─────────────────────────────────────────────
    daily_range_pct = (high_24h - low_24h) / low_24h if low_24h > 0 else 0
    # BTC gets a stricter threshold because it is always slot-0 and cannot rotate
    min_range = BTC_DAILY_RANGE_MIN if sym == "BTCUSDT" else DAILY_RANGE_MIN
    if daily_range_pct < min_range:
        return HealthResult(
            False, "LOW_DAILY",
            f"{sym}: 24h range {daily_range_pct:.2%} < {min_range:.1%} minimum — "
            f"coin too flat for 15m fees (high=${high_24h:.4f} low=${low_24h:.4f})"
        )

    # ── Parse klines ──────────────────────────────────────────────────────────
    if not klines_15m or len(klines_15m) < FLAT_CANDLES + 1:
        # Not enough data — pass through, let deep scorer decide
        return HealthResult(True, "PASS_NO_KLINES", f"{sym}: insufficient klines, skipping health checks")

    try:
        # Use completed candles only (exclude the last in-progress candle)
        completed = klines_15m[-(FLAT_CANDLES + 1):-1]
        opens     = [float(c[1]) for c in completed]
        closes    = [float(c[4]) for c in completed]
        highs     = [float(c[2]) for c in completed]
        lows      = [float(c[3]) for c in completed]
    except (IndexError, ValueError, TypeError):
        return HealthResult(True, "PASS_PARSE_ERR", f"{sym}: kline parse error, skipping")

    # ── Check: tick trap ──────────────────────────────────────────────────────
    # Estimate minimum tick size from the smallest non-zero price step observed
    # across adjacent candle boundaries.  If we can't measure it, infer from
    # the number of decimal places in the price string.
    min_tick = _estimate_tick(price, ticker_24h)
    if min_tick > 0:
        # ATR approximation from kline data (simple mean of high-low ranges)
        atr_approx = sum(h - l for h, l in zip(highs, lows)) / len(highs)
        if atr_approx > 0 and (min_tick / atr_approx) >= TICK_TRAP_RATIO:
            tick_pct = min_tick / price * 100
            return HealthResult(
                False, "TICK_TRAP",
                f"{sym}: min_tick={min_tick:.6f} ({tick_pct:.3f}% of price) "
                f"is {min_tick/atr_approx:.1%} of ATR — coin cannot move "
                f"enough per candle to cover {FEE_ROUNDTRIP:.2%} round-trip fee"
            )

    # ── Check: flat mover ─────────────────────────────────────────────────────
    # A candle "passes" if its body (|close - open| / open) exceeds half the
    # fee threshold. We halve the threshold because we only need the coin to
    # move in one direction; the full round-trip threshold applies to the trade.
    body_threshold = FEE_ROUNDTRIP / 2   # 0.075%
    passing_candles = sum(
        1 for o, c in zip(opens, closes)
        if o > 0 and abs(c - o) / o >= body_threshold
    )
    if passing_candles < FLAT_MIN_PASS:
        return HealthResult(
            False, "FLAT_MOVER",
            f"{sym}: only {passing_candles}/{FLAT_CANDLES} recent candles moved "
            f">{body_threshold:.3%} — rolling history shows coin cannot beat fees "
            f"(need {FLAT_MIN_PASS}+ candles with body >{body_threshold:.3%})"
        )

    return HealthResult(True, "PASS", f"{sym}: all health checks passed")


def _estimate_tick(price: float, ticker: dict) -> float:
    """
    Estimate the minimum price tick from the last-price string's decimal places.
    e.g. "0.08000" → 5 decimals → tick=0.00001; "63575.13" → 2 decimals → tick=0.01

    Do NOT strip trailing zeros — Binance pads price strings to a fixed precision
    that reflects the actual tick size (e.g. "2.5000" means tick=0.0001, not 0.1).
    """
    try:
        price_str = str(ticker.get("lastPrice") or ticker.get("price", ""))
        # Remove any scientific notation and get the fractional part as-is
        if "e" not in price_str.lower() and "." in price_str:
            frac = price_str.split(".")[-1]
            # Count all fractional digits including trailing zeros
            return 10 ** -len(frac)
    except Exception:
        pass
    # fallback: 4 significant figures below the price magnitude
    if price > 0:
        magnitude = math.floor(math.log10(price))
        return 10 ** (magnitude - 3)
    return 0.0


class CoinHealthFilter:
    """
    Wraps check() with a session-level cache to avoid re-checking the same
    coin within CACHE_TTL seconds.  MarketScanner creates one instance and
    passes it across scan cycles.
    """
    CACHE_TTL = 300   # 5 min — re-check after one scan cycle

    def __init__(self):
        self._cache: dict[str, tuple[float, HealthResult]] = {}

    def _cached(self, sym: str) -> HealthResult | None:
        import time
        entry = self._cache.get(sym)
        if entry and (time.time() - entry[0]) < self.CACHE_TTL:
            return entry[1]
        return None

    def _store(self, sym: str, result: HealthResult) -> None:
        import time
        self._cache[sym] = (time.time(), result)

    async def is_healthy(self, sym: str, ticker_24h: dict,
                         klines_15m: list, session) -> bool:
        """
        Returns True if the coin passes all health checks.
        Logs and returns False on failure so the scanner can skip the coin.

        session: aiohttp.ClientSession (for fetching klines if not provided)
        """
        cached = self._cached(sym)
        if cached is not None:
            if not cached.passed:
                log("HEALTH", "CACHED_FAIL", symbol=sym, reason=cached.reason)
            return cached.passed

        # Fetch 15m klines if not already provided (scanner passes them when available)
        kl = klines_15m
        if not kl and session:
            try:
                import config as cfg
                url = (cfg.PUBLIC_DATA_URL
                       + f"/api/v3/klines?symbol={sym}&interval=15m&limit=6")
                async with session.get(url) as r:
                    r.raise_for_status()
                    kl = await r.json()
            except Exception as e:
                log("HEALTH", "KLINE_FETCH_ERR", symbol=sym, error=str(e)[:60])
                kl = []

        result = await check(sym, ticker_24h, kl)
        self._store(sym, result)

        if result.passed:
            log("HEALTH", "PASS", symbol=sym, check=result.reason)
        else:
            log("HEALTH", "FAIL", symbol=sym, reason=result.reason, detail=result.detail)

        return result.passed

    def invalidate(self, sym: str) -> None:
        """Force re-check next time (call when a coin is being de-assigned)."""
        self._cache.pop(sym, None)
