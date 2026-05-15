"""Modules 3 & 4 — Order Submission, Fill Monitoring."""
import asyncio
import hashlib
import hmac
import time
import aiohttp
from logger import log
import config as cfg


def _sign(params: dict) -> str:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(cfg.BINANCE_API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()


class OrderManager:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": cfg.BINANCE_API_KEY})

    async def get_equity(self) -> float:
        await self._ensure_session()
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.get(cfg.SPOT_BASE_URL + "/api/v3/account", params=params) as r:
            r.raise_for_status()
            data     = await r.json()
            balances = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in data["balances"]}
            usdt     = balances.get("USDT", 0.0)
            log("MODULE_2", "EQUITY_FETCH", usdt=round(usdt, 2))
            return usdt

    async def _post_order(self, params: dict, retries: int = 3) -> dict | None:
        await self._ensure_session()
        params["timestamp"]  = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"]  = _sign(params)
        delay = 0.5
        for attempt in range(retries):
            try:
                async with self._session.post(
                    cfg.SPOT_BASE_URL + "/api/v3/order", params=params
                ) as r:
                    if r.status == 429:
                        log("MODULE_3", "RATE_LIMIT", attempt=attempt)
                        await asyncio.sleep(10)
                        continue
                    if r.status >= 500:
                        log("MODULE_3", "SERVER_ERROR", status=r.status, attempt=attempt)
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    r.raise_for_status()
                    return await r.json()
            except Exception as e:
                log("MODULE_3", "ORDER_ERROR", error=str(e), attempt=attempt)
                await asyncio.sleep(delay)
                delay *= 2
        log("MODULE_3", "ORDER_FAILED_ALL_RETRIES", params=str(params))
        return None

    async def submit(self, side: str, qty: float, tick: dict, ind: dict) -> dict | None:
        entry_price  = tick["price"]
        book_spread  = tick.get("spread_pct", 0)
        order_type   = "MARKET" if book_spread < 0.05 else "LIMIT"
        limit_price  = round(entry_price * 1.0002, 2) if side == "BUY" else round(entry_price * 0.9998, 2)

        params: dict = {
            "symbol":          cfg.SYMBOL,
            "side":            side,
            "type":            order_type,
            "quantity":        round(qty, 5),
            "newOrderRespType":"FULL",
        }
        if order_type == "LIMIT":
            params["price"]       = limit_price
            params["timeInForce"] = "GTC"

        log("MODULE_3", "SUBMIT_ORDER", side=side, type=order_type, qty=round(qty, 5),
            price=limit_price if order_type == "LIMIT" else entry_price)

        result = await self._post_order(params)
        if result:
            fill_price = float(result.get("fills", [{}])[0].get("price", entry_price)) if result.get("fills") else entry_price
            slippage   = abs(fill_price - entry_price) / entry_price
            if slippage > cfg.MAX_SLIPPAGE:
                log("MODULE_3", "SLIPPAGE_BREACH", fill=fill_price, expected=entry_price, pct=round(slippage*100, 4))
            log("MODULE_3", "ORDER_FILLED", orderId=result.get("orderId"), fill_price=fill_price,
                status=result.get("status"))
        return result

    async def cancel_all(self):
        await self._ensure_session()
        params = {"symbol": cfg.SYMBOL, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.delete(cfg.SPOT_BASE_URL + "/api/v3/openOrders", params=params) as r:
            log("MODULE_7", "CANCEL_ALL_ORDERS", status=r.status)

    async def close(self):
        if self._session:
            await self._session.close()
