"""Module 1 — Market Data, Order Book, Indicators."""
import asyncio
import time
import aiohttp
import pandas as pd
import pandas_ta as ta
from logger import log
import config as cfg


class MarketData:
    def __init__(self, symbol: str, interval: str):
        self.symbol   = symbol
        self.interval = interval
        self._session: aiohttp.ClientSession | None = None

    async def connect(self):
        self._session = aiohttp.ClientSession()
        log("MODULE_1", "CONNECT", symbol=self.symbol)

    async def _get(self, base: str, path: str, params: dict = None) -> dict | list:
        url = base + path
        t0  = time.monotonic()
        async with self._session.get(url, params=params or {}) as r:
            latency_ms = (time.monotonic() - t0) * 1000
            if latency_ms > 2000:
                log("MODULE_1", "HIGH_LATENCY", ms=round(latency_ms))
            r.raise_for_status()
            return await r.json()

    async def get_ticker(self) -> dict:
        data = await self._get(cfg.SPOT_BASE_URL, "/api/v3/ticker/24hr",
                               {"symbol": self.symbol})
        return {
            "price":       float(data["lastPrice"]),
            "volume_usdt": float(data["quoteVolume"]),
            "change_pct":  float(data["priceChangePercent"]),
        }

    async def get_orderbook(self) -> dict:
        data  = await self._get(cfg.SPOT_BASE_URL, "/api/v3/depth",
                                {"symbol": self.symbol, "limit": 20})
        bids  = [(float(p), float(q)) for p, q in data["bids"]]
        asks  = [(float(p), float(q)) for p, q in data["asks"]]
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid   = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100
        bid_vol    = sum(q for _, q in bids[:10])
        ask_vol    = sum(q for _, q in asks[:10])
        imbalance  = bid_vol / (bid_vol + ask_vol) if (bid_vol + ask_vol) else 0.5
        return {
            "best_bid": best_bid, "best_ask": best_ask,
            "spread_pct": spread_pct, "imbalance": imbalance,
        }

    async def get_klines(self) -> pd.DataFrame:
        data = await self._get(cfg.SPOT_BASE_URL, "/api/v3/klines",
                               {"symbol": self.symbol, "interval": self.interval,
                                "limit": cfg.CANDLE_LIMIT})
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","qv","nt","tbbav","tbqav","ignore"])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        return df.set_index("open_time")

    def compute_indicators(self, df: pd.DataFrame) -> dict:
        c = df["close"]
        ind = {}
        ind["ema9"]   = ta.ema(c, 9).iloc[-1]
        ind["ema21"]  = ta.ema(c, 21).iloc[-1]
        ind["ema50"]  = ta.ema(c, 50).iloc[-1]
        ind["rsi14"]  = ta.rsi(c, 14).iloc[-1]
        macd_df       = ta.macd(c, 12, 26, 9)
        ind["macd"]   = macd_df["MACD_12_26_9"].iloc[-1]
        ind["macd_signal"] = macd_df["MACDs_12_26_9"].iloc[-1]
        bb            = ta.bbands(c, 20, 2)
        ind["bb_upper"] = bb["BBU_20_2.0"].iloc[-1]
        ind["bb_lower"] = bb["BBL_20_2.0"].iloc[-1]
        atr           = ta.atr(df["high"], df["low"], df["close"], cfg.ATR_PERIOD)
        ind["atr14"]  = atr.iloc[-1]
        tp            = (df["high"] + df["low"] + df["close"]) / 3
        vp            = (tp * df["volume"]).cumsum()
        ind["vwap"]   = (vp / df["volume"].cumsum()).iloc[-1]
        return ind

    def quality_gate(self, tick: dict, book: dict) -> bool:
        reasons = []
        if book["spread_pct"] > cfg.MAX_SPREAD_PCT * 100:
            reasons.append(f"spread {book['spread_pct']:.4f}% > limit")
        if tick["volume_usdt"] < cfg.MIN_VOLUME_USDT:
            reasons.append(f"volume {tick['volume_usdt']:.0f} < limit")
        if book["imbalance"] < 0.25 or book["imbalance"] > 0.75:
            reasons.append(f"book imbalance {book['imbalance']:.2f}")
        if reasons:
            log("MODULE_1", "QUALITY_GATE_FAIL", reasons=reasons)
            return False
        return True

    async def get_funding_rate(self) -> float:
        data = await self._get(cfg.FUTURES_BASE_URL, "/fapi/v1/premiumIndex",
                               {"symbol": self.symbol})
        return float(data["lastFundingRate"])

    async def close(self):
        if self._session:
            await self._session.close()
