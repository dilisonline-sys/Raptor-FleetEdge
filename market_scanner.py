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

SCAN_INTERVAL   = 900        # re-rank every 15 minutes
MIN_VOLUME_USDT = 5_000_000  # minimum 24h volume
MIN_PRICE_CHG   = 0.5        # minimum 24h % move (need momentum)
MAX_SPREAD_PCT  = 0.15       # tighter pre-filter than quality gate (0.15%)
TOP_N           = 10         # analyse full indicators on top N by quick score
BLACKLIST       = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT"}  # stablecoins


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
        """Fetch all 24hr tickers and rank by quick score, pre-filtering by spread."""
        tickers = await self._get("/api/v3/ticker/24hr")
        candidates = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym in BLACKLIST:
                continue
            try:
                vol       = float(t["quoteVolume"])
                chg       = abs(float(t["priceChangePercent"]))
                price     = float(t["lastPrice"])
                high      = float(t["highPrice"])
                low       = float(t["lowPrice"])
                bid_price = float(t.get("bidPrice") or price)
                ask_price = float(t.get("askPrice") or price)
                if vol < MIN_VOLUME_USDT or chg < MIN_PRICE_CHG or price <= 0:
                    continue
                # Pre-filter: spread must be below MAX_SPREAD_PCT
                mid_p = (bid_price + ask_price) / 2
                spread_pct = (ask_price - bid_price) / mid_p * 100 if mid_p else 99
                if spread_pct > MAX_SPREAD_PCT:
                    continue
                range_ratio  = (high - low) / price if price else 0
                quick_score  = chg * (vol / 1e8) * range_ratio
                candidates.append({
                    "symbol":     sym,
                    "vol":        vol,
                    "chg_pct":    chg,
                    "price":      price,
                    "spread_pct": spread_pct,
                    "range_ratio":range_ratio,
                    "quick_score":quick_score,
                })
            except (ValueError, KeyError):
                continue
        candidates.sort(key=lambda x: x["quick_score"], reverse=True)
        log("SCANNER", "QUICK_RANK", candidates=len(candidates),
            top3=[c["symbol"] for c in candidates[:3]])
        return candidates[:TOP_N]

    async def _score_with_indicators(self, sym: str) -> float:
        """Fetch 1H OHLCV and compute a deep score for a single symbol."""
        try:
            data = await self._get("/api/v3/klines",
                                   {"symbol": sym, "interval": "1h", "limit": 48})
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

            # Volatility score: ATR/price (bigger moves = more opportunity)
            vol_score  = min(atr / price * 100, 5.0)      # cap at 5%

            # Trend score: EMA alignment + MACD confirmation
            trend_up   = e9 > e21 > e50 and macd > msig
            trend_dn   = e9 < e21 < e50 and macd < msig
            trend_score = 2.0 if (trend_up or trend_dn) else 0.8

            # RSI score: tradeable zone (30-70) scores highest; extremes (<25/>75) penalised
            rsi_score  = 1.5 if 30 < rsi < 70 else (1.0 if 20 < rsi < 80 else 0.5)

            # Profit per minute proxy: vol_score * trend * rsi_quality
            deep_score = vol_score * trend_score * rsi_score

            log("SCANNER", "DEEP_SCORE", symbol=sym,
                atr_pct=round(atr/price*100, 3), trend=str(trend_up or trend_dn),
                rsi=round(rsi, 1), score=round(deep_score, 4))
            return deep_score

        except Exception as e:
            log("SCANNER", "SCORE_ERROR", symbol=sym, error=str(e))
            return 0.0

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
        scores = await asyncio.gather(
            *[self._score_with_indicators(c["symbol"]) for c in top_quick]
        )

        ranked = []
        for cand, score in zip(top_quick, scores):
            ranked.append({**cand, "deep_score": score})
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
