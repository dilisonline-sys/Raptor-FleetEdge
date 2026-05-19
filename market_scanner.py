"""
Market Scanner — ranks all Binance USDT spot pairs by profit potential.
Dipu uses this to auto-select the best coin to trade each cycle.

Scoring formula (higher = better opportunity):
  score = momentum_score * volatility_score * volume_score * trend_score

Run every SCAN_INTERVAL seconds. Returns ranked list of symbols.
"""
import asyncio
import time
import aiohttp
import pandas as pd
import pandas_ta as ta
from logger import log
import config as cfg

SCAN_INTERVAL   = 300        # re-rank every 5 minutes (matches cycle)
MIN_VOLUME_USDT = 3_000_000  # lowered to catch high-momentum small caps (e.g. RAD +9% 1h)
MIN_PRICE_CHG   = 1.0        # need at least 1% move — momentum only
MAX_SPREAD_PCT  = 0.30       # raised — low-price coins (e.g. $0.30) hit 0.29% spread on 1 tick
TOP_N           = 12         # score top 12 candidates with indicators
BLACKLIST       = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT",
                   "USD1USDT", "USDTUSDT"}  # stablecoins + tether pairs

def _is_valid_symbol(sym: str) -> bool:
    """Reject non-ASCII symbols (promotional/meme tokens like 币安人生USDT)."""
    return sym.isascii()


class MarketScanner:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self.best_symbol   = cfg.SYMBOL
        self.ranked: list[dict] = []
        self._last_scan    = 0.0

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _get(self, path: str, params: dict = None) -> list | dict:
        await self._ensure_session()
        async with self._session.get(cfg.PUBLIC_DATA_URL + path, params=params or {}) as r:
            r.raise_for_status()
            return await r.json()

    async def _quick_rank(self) -> list[dict]:
        """Rank coins by 1-hour momentum.

        Step 1 — 24hr ticker: universe filter (volume, spread, valid symbol).
        Step 2 — 1h rolling ticker for the shortlist: re-score by 1h change + 1h volume.
        This surfaces coins that have actually moved in the last hour, not just the last day.
        """
        import json as _json

        # ── Step 1: 24hr ticker for universe filtering ────────────────
        tickers_24h = await self._get("/api/v3/ticker/24hr")
        pre = []
        for t in tickers_24h:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym in BLACKLIST or not _is_valid_symbol(sym):
                continue
            try:
                vol       = float(t["quoteVolume"])
                price     = float(t["lastPrice"])
                bid_price = float(t.get("bidPrice") or price)
                ask_price = float(t.get("askPrice") or price)
                if vol < MIN_VOLUME_USDT or price <= 0:
                    continue
                mid_p      = (bid_price + ask_price) / 2
                spread_pct = (ask_price - bid_price) / mid_p * 100 if mid_p else 99
                if spread_pct > MAX_SPREAD_PCT:
                    continue
                chg_24h = abs(float(t.get("priceChangePercent", 0)))
                pre.append({"symbol": sym, "price": price, "spread_pct": spread_pct,
                             "vol_24h": vol, "chg_24h": chg_24h})
            except (ValueError, KeyError):
                continue

        if not pre:
            return []

        # ── Step 2: 1h rolling ticker for the shortlist ───────────────
        # Two-pool batch: top 60 by 24h volume (liquid coins) + top 60 by 24h abs %
        # change (momentum coins). Deduped. Ensures high-momentum small caps like RAD
        # (+9% 1h but low 24h vol) are always included alongside the liquid heavyweights.
        # Build URL manually — aiohttp's params dict doesn't reliably encode JSON arrays.
        import urllib.parse as _up
        pre_by_vol = sorted(pre, key=lambda x: x["vol_24h"], reverse=True)
        pre_by_chg = sorted(pre, key=lambda x: abs(x.get("chg_24h", 0)), reverse=True)
        seen = set()
        batch = []
        for c in pre_by_vol[:60] + pre_by_chg[:60]:
            if c["symbol"] not in seen:
                seen.add(c["symbol"])
                batch.append(c)
        sym_json = _json.dumps([c["symbol"] for c in batch], separators=(',', ':'))
        url_1h   = (cfg.PUBLIC_DATA_URL + "/api/v3/ticker"
                    + "?windowSize=1h&symbols=" + _up.quote(sym_json))
        try:
            await self._ensure_session()
            async with self._session.get(url_1h) as r1h:
                r1h.raise_for_status()
                tickers_1h = await r1h.json()
            by_sym = {t["symbol"]: t for t in (tickers_1h if isinstance(tickers_1h, list) else [])}
        except Exception as e:
            log("SCANNER", "1H_FETCH_ERROR", error=str(e)[:80])
            by_sym = {}

        # ── Step 3: score by 1h change × 1h volume × 1h range ────────
        candidates = []
        for c in batch:
            sym  = c["symbol"]
            t1h  = by_sym.get(sym)
            if not t1h:
                continue
            try:
                chg_1h  = abs(float(t1h["priceChangePercent"]))
                vol_1h  = float(t1h["quoteVolume"])
                high_1h = float(t1h["highPrice"])
                low_1h  = float(t1h["lowPrice"])
                price   = float(t1h["lastPrice"]) or c["price"]
                # Require at least 0.3% 1h move and $500K 1h volume
                if chg_1h < 0.3 or vol_1h < 500_000:
                    continue
                range_1h    = (high_1h - low_1h) / price if price else 0
                quick_score = chg_1h * (vol_1h / 1e7) * range_1h
                candidates.append({
                    "symbol":     sym,
                    "vol":        vol_1h,
                    "chg_pct":    chg_1h,
                    "price":      price,
                    "spread_pct": c["spread_pct"],
                    "range_ratio":range_1h,
                    "quick_score":quick_score,
                })
            except (ValueError, KeyError):
                continue

        candidates.sort(key=lambda x: x["quick_score"], reverse=True)
        log("SCANNER", "QUICK_RANK", candidates=len(candidates),
            top3=[c["symbol"] for c in candidates[:3]])
        return candidates[:TOP_N]

    async def _score_with_indicators(self, sym: str) -> float:
        """Fetch 15m OHLCV and compute a deep score — volatility-weighted for momentum strategy."""
        try:
            data = await self._get("/api/v3/klines",
                                   {"symbol": sym, "interval": "15m", "limit": 60})
            df = pd.DataFrame(data, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","qv","nt","tbbav","tbqav","ignore"])
            for col in ["open","high","low","close","volume"]:
                df[col] = df[col].astype(float)

            c   = df["close"]
            _atr  = ta.atr(df["high"], df["low"], c, 14)
            _rsi  = ta.rsi(c, 14)
            _e9   = ta.ema(c, 9)
            _e21  = ta.ema(c, 21)
            _e50  = ta.ema(c, 50)
            _macd = ta.macd(c, 12, 26, 9)
            if any(x is None for x in [_atr, _rsi, _e9, _e21, _e50, _macd]):
                return 0.0
            atr   = _atr.dropna().iloc[-1]  if len(_atr.dropna())  else 0
            rsi   = _rsi.dropna().iloc[-1]  if len(_rsi.dropna())  else 50
            e9    = _e9.dropna().iloc[-1]   if len(_e9.dropna())   else 0
            e21   = _e21.dropna().iloc[-1]  if len(_e21.dropna())  else 0
            e50   = _e50.dropna().iloc[-1]  if len(_e50.dropna())  else 0
            macd_col = "MACD_12_26_9"
            msig_col = "MACDs_12_26_9"
            macd  = _macd[macd_col].dropna().iloc[-1] if macd_col in _macd and len(_macd[macd_col].dropna()) else 0
            msig  = _macd[msig_col].dropna().iloc[-1] if msig_col in _macd and len(_macd[msig_col].dropna()) else 0
            price = c.iloc[-1]

            # Volatility score: bell-curve centred on 2.0% ATR.
            # Coins under 0.5% are too quiet to reach TP; coins over 3% blow stops on noise.
            # Peak score = 10 at 2.0% ATR, decays toward extremes.
            atr_pct    = atr / price * 100
            import math as _math
            vol_score  = 10.0 * _math.exp(-0.5 * ((atr_pct - 2.0) / 1.2) ** 2)

            # Trend score: TRENDING regime only is valid — hard penalise no-trend
            trend_up   = e9 > e21 > e50 and macd > msig
            trend_dn   = e9 < e21 < e50 and macd < msig
            trend_score = 3.0 if (trend_up or trend_dn) else 0.3  # strong bias to trending

            # RSI score: momentum zone (40-65 for long, 35-60 for short) scores highest
            rsi_score  = 1.5 if 35 < rsi < 68 else (0.8 if 25 < rsi < 78 else 0.3)

            # Combined: volatility × trend alignment × RSI quality
            deep_score = vol_score * trend_score * rsi_score
            trend_str  = ("↑ BULL" if trend_up else "↓ BEAR" if trend_dn else "→ FLAT")

            # Regime: mirror the main RegimeClassifier logic (no import needed)
            vol_ratio = atr / price if price else 0
            if vol_ratio > 0.05:
                regime = "VOLATILE"
            elif trend_up or trend_dn:
                regime = "TRENDING"
            else:
                regime = "RANGING"

            log("SCANNER", "DEEP_SCORE", symbol=sym,
                atr_pct=round(atr_pct, 3), trend=trend_str, regime=regime,
                rsi=round(rsi, 1), score=round(deep_score, 4))
            return {"score": deep_score, "atr_pct": atr_pct, "trend": trend_str,
                    "rsi": round(rsi, 1), "regime": regime}

        except Exception as e:
            log("SCANNER", "SCORE_ERROR", symbol=sym, error=str(e))
            return {"score": 0.0, "atr_pct": 0.0, "trend": "—", "rsi": 50, "regime": "—"}

    async def scan(self, exclude: set[str] | None = None, force: bool = False) -> str:
        """Full scan — returns the best symbol to trade right now.

        exclude: symbols to skip (e.g. current coin during volatile escape)
        force:   bypass the 15-minute cache and re-rank immediately
        """
        now = time.time()
        if not force and now - self._last_scan < SCAN_INTERVAL and self.best_symbol:
            if not exclude or self.best_symbol not in exclude:
                return self.best_symbol

        log("SCANNER", "SCAN_START", forced=force, excluded=list(exclude or []))
        top_quick = await self._quick_rank()
        if not top_quick:
            log("SCANNER", "NO_CANDIDATES")
            return self.best_symbol

        # Filter excluded symbols before deep scoring
        if exclude:
            top_quick = [c for c in top_quick if c["symbol"] not in exclude]
        if not top_quick:
            log("SCANNER", "NO_CANDIDATES_AFTER_EXCLUDE", excluded=list(exclude))
            return self.best_symbol

        # Deep score top candidates in parallel
        results = await asyncio.gather(
            *[self._score_with_indicators(c["symbol"]) for c in top_quick]
        )

        ranked = []
        for cand, res in zip(top_quick, results):
            ranked.append({
                **cand,
                "deep_score": res["score"],
                "atr_pct":    res["atr_pct"],
                "trend":      res["trend"],
                "rsi":        res["rsi"],
                "regime":     res.get("regime", "—"),
            })
        ranked.sort(key=lambda x: x["deep_score"], reverse=True)

        self.ranked      = ranked
        self._last_scan  = now
        prev             = self.best_symbol
        self.best_symbol = ranked[0]["symbol"] if ranked else self.best_symbol

        log("SCANNER", "SCAN_COMPLETE",
            best=self.best_symbol,
            prev=prev,
            switched=self.best_symbol != prev,
            top5=[r["symbol"] for r in ranked[:5]])

        return self.best_symbol

    async def close(self):
        if self._session:
            await self._session.close()
