"""Module 1 — Market Data, Order Book, Indicators (REST + WebSocket streaming)."""
import asyncio
import json
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
        # WS-backed caches (None = not yet received, falls back to REST)
        self._ws_ticker: dict | None = None
        self._ws_book:   dict | None = None
        self._ws_tasks:  list        = []

    async def connect(self):
        self._session = aiohttp.ClientSession()
        log("MODULE_1", "CONNECT", symbol=self.symbol)
        self._ws_tasks = [
            asyncio.create_task(self._ws_ticker_loop()),
            asyncio.create_task(self._ws_book_loop()),
        ]

    # ── WebSocket background streams ──────────────────────────────────────────

    async def _ws_ticker_loop(self):
        sym  = self.symbol.lower()
        url  = f"wss://stream.binance.com:9443/ws/{sym}@ticker"
        fail = 0
        while fail < 4:
            try:
                async with self._session.ws_connect(
                    url, heartbeat=20, timeout=aiohttp.ClientTimeout(total=None)
                ) as ws:
                    log("MODULE_1", "WS_TICKER_UP", symbol=self.symbol)
                    fail = 0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(msg.data)
                            self._ws_ticker = {
                                "price":       float(d["c"]),
                                "volume_usdt": float(d["q"]),
                                "change_pct":  float(d["P"]),
                            }
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR
                        ):
                            break
            except asyncio.CancelledError:
                return
            except Exception as e:
                fail += 1
                log("MODULE_1", "WS_TICKER_ERR", attempt=fail, error=str(e)[:80])
            await asyncio.sleep(min(5 * fail, 30))
        log("MODULE_1", "WS_TICKER_DISABLED", reason="too many failures — using REST fallback")

    async def _ws_book_loop(self):
        sym  = self.symbol.lower()
        url  = f"wss://stream.binance.com:9443/ws/{sym}@bookTicker"
        fail = 0
        while fail < 4:
            try:
                async with self._session.ws_connect(
                    url, heartbeat=20, timeout=aiohttp.ClientTimeout(total=None)
                ) as ws:
                    log("MODULE_1", "WS_BOOK_UP", symbol=self.symbol)
                    fail = 0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            d   = json.loads(msg.data)
                            bid = float(d["b"])
                            ask = float(d["a"])
                            mid = (bid + ask) / 2
                            spread_pct = (ask - bid) / mid * 100 if mid else 0
                            prev_imb = (self._ws_book or {}).get("imbalance", 0.5)
                            self._ws_book = {
                                "best_bid":   bid,
                                "best_ask":   ask,
                                "spread_pct": spread_pct,
                                "imbalance":  prev_imb,
                            }
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR
                        ):
                            break
            except asyncio.CancelledError:
                return
            except Exception as e:
                fail += 1
                log("MODULE_1", "WS_BOOK_ERR", attempt=fail, error=str(e)[:80])
            await asyncio.sleep(min(5 * fail, 30))
        log("MODULE_1", "WS_BOOK_DISABLED", reason="too many failures — using REST fallback")

    # ── REST helpers ──────────────────────────────────────────────────────────

    async def _get(self, base: str, path: str, params: dict = None) -> dict | list:
        url = base + path
        t0  = time.monotonic()
        async with self._session.get(url, params=params or {}) as r:
            latency_ms = (time.monotonic() - t0) * 1000
            if latency_ms > 2000:
                log("MODULE_1", "HIGH_LATENCY", ms=round(latency_ms))
            r.raise_for_status()
            return await r.json()

    # ── Public data methods ───────────────────────────────────────────────────

    async def get_ticker(self) -> dict:
        if self._ws_ticker is not None:
            return self._ws_ticker
        # REST fallback (first cycle before WS warms up)
        data = await self._get(cfg.PUBLIC_DATA_URL, "/api/v3/ticker/24hr",
                               {"symbol": self.symbol})
        return {
            "price":       float(data["lastPrice"]),
            "volume_usdt": float(data["quoteVolume"]),
            "change_pct":  float(data["priceChangePercent"]),
        }

    async def get_orderbook(self) -> dict:
        # Always REST for depth — need 10-level book to compute imbalance
        data  = await self._get(cfg.PUBLIC_DATA_URL, "/api/v3/depth",
                                {"symbol": self.symbol, "limit": 20})
        bids  = [(float(p), float(q)) for p, q in data["bids"]]
        asks  = [(float(p), float(q)) for p, q in data["asks"]]
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid   = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100
        bid_vol    = sum(q for _, q in bids[:10])
        ask_vol    = sum(q for _, q in asks[:10])
        imbalance  = bid_vol / (bid_vol + ask_vol) if (bid_vol + ask_vol) else 0.5
        result = {
            "best_bid": best_bid, "best_ask": best_ask,
            "spread_pct": spread_pct, "imbalance": imbalance,
        }
        # Back-fill imbalance into WS book cache
        if self._ws_book is not None:
            self._ws_book["imbalance"] = imbalance
        return result

    async def get_klines(self) -> pd.DataFrame:
        data = await self._get(cfg.PUBLIC_DATA_URL, "/api/v3/klines",
                               {"symbol": self.symbol, "interval": self.interval,
                                "limit": cfg.CANDLE_LIMIT})
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","qv","nt","tbbav","tbqav","ignore"])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("open_time")
        # Patch last candle close with live WS price for up-to-date indicators
        if self._ws_ticker is not None:
            df.iloc[-1, df.columns.get_loc("close")] = self._ws_ticker["price"]
        return df

    def compute_indicators(self, df: pd.DataFrame) -> dict:
        c = df["close"]
        ind = {}
        ind["ema9"]        = ta.ema(c, 9).iloc[-1]
        ind["ema21"]       = ta.ema(c, 21).iloc[-1]
        ind["ema50"]       = ta.ema(c, 50).iloc[-1]
        ind["rsi14"]       = ta.rsi(c, 14).iloc[-1]
        macd_df            = ta.macd(c, 12, 26, 9)
        ind["macd"]        = macd_df["MACD_12_26_9"].iloc[-1]
        ind["macd_signal"] = macd_df["MACDs_12_26_9"].iloc[-1]
        bb                 = ta.bbands(c, 20, 2)
        ind["bb_upper"]    = bb["BBU_20_2_2.0"].iloc[-1]
        ind["bb_lower"]    = bb["BBL_20_2_2.0"].iloc[-1]
        atr                = ta.atr(df["high"], df["low"], df["close"], cfg.ATR_PERIOD)
        ind["atr14"]       = atr.iloc[-1]
        tp                 = (df["high"] + df["low"] + df["close"]) / 3
        vp                 = (tp * df["volume"]).cumsum()
        ind["vwap"]        = (vp / df["volume"].cumsum()).iloc[-1]
        ind["close"]       = float(c.iloc[-1])   # current close — use this for EMA/BB comparisons
        ind["open"]        = float(df["open"].iloc[-1])   # last candle open — for candle body filter
        # 1h range: max-high minus min-low of the last 4 × 15m candles (= 1 hour of actual price action)
        ind["h1_range"]    = float(df["high"].iloc[-4:].max() - df["low"].iloc[-4:].min())
        return ind

    def quality_gate(self, tick: dict, book: dict) -> bool:
        reasons = []
        if book["spread_pct"] > cfg.MAX_SPREAD_PCT * 100:
            reasons.append(f"spread {book['spread_pct']:.4f}% > limit")
        if tick["volume_usdt"] < cfg.MIN_VOLUME_USDT:
            reasons.append(f"volume {tick['volume_usdt']:.0f} < limit")
        imb_lo, imb_hi = (0.02, 0.98)  # practical: BTC runs 0.95+ naturally, only block extreme wash
        if book["imbalance"] < imb_lo or book["imbalance"] > imb_hi:
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
        for task in self._ws_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._ws_tasks = []
        if self._session:
            await self._session.close()
