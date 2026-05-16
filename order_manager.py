"""Modules 3 & 4 — Order Submission, Fill Monitoring."""
import asyncio
import hashlib
import hmac
import math
import time
import aiohttp
from logger import log
import config as cfg


def _sign(params: dict) -> str:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(cfg.BINANCE_API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()


def _round_step(qty: float, step: float) -> float:
    """Round quantity down to the nearest valid step size."""
    if step <= 0:
        return qty
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(qty / step) * step, precision)


class OrderManager:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._lot_steps: dict[str, float] = {}   # symbol → stepSize cache

    async def _ensure_session(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": cfg.BINANCE_API_KEY})

    async def get_lot_step(self, symbol: str) -> float:
        """Fetch and cache the LOT_SIZE stepSize for a symbol."""
        if symbol in self._lot_steps:
            return self._lot_steps[symbol]
        await self._ensure_session()
        try:
            async with self._session.get(
                cfg.SPOT_BASE_URL + "/api/v3/exchangeInfo", params={"symbol": symbol}
            ) as r:
                r.raise_for_status()
                data = await r.json()
                for sym in data.get("symbols", []):
                    for f in sym.get("filters", []):
                        if f["filterType"] == "LOT_SIZE":
                            step = float(f["stepSize"])
                            self._lot_steps[symbol] = step
                            log("MODULE_3", "LOT_STEP", symbol=symbol, step=step)
                            return step
        except Exception as e:
            log("MODULE_3", "LOT_STEP_ERROR", symbol=symbol, error=str(e))
        return 1.0  # safe fallback

    async def get_equity(self, symbol: str | None = None, price: float | None = None) -> float:
        """Total portfolio value in USDT.
        Pass symbol+price to include the value of any held base asset
        (e.g. BTC balance × current price), so a spot BUY doesn't look like a drawdown.
        """
        await self._ensure_session()
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.get(cfg.SPOT_BASE_URL + "/api/v3/account", params=params) as r:
            r.raise_for_status()
            data     = await r.json()
            balances = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in data["balances"]}
            usdt     = balances.get("USDT", 0.0)
            # Include value of held base asset (e.g. BTC) so buying doesn't look like a loss
            if symbol and price and price > 0:
                base     = symbol.replace("USDT", "")
                base_bal = balances.get(base, 0.0)
                usdt    += base_bal * price
            log("MODULE_2", "EQUITY_FETCH", usdt=round(usdt, 2))
            return usdt

    async def get_base_balance(self, symbol: str) -> float:
        """Returns free balance of the base asset (e.g. SUI for SUIUSDT)."""
        await self._ensure_session()
        base = symbol.replace("USDT", "")
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.get(cfg.SPOT_BASE_URL + "/api/v3/account", params=params) as r:
            r.raise_for_status()
            data = await r.json()
            for b in data["balances"]:
                if b["asset"] == base:
                    return float(b["free"])
        return 0.0

    async def _post_order(self, params: dict, retries: int = 3) -> dict | None:
        await self._ensure_session()
        params["recvWindow"] = 5000
        delay = 0.5
        for attempt in range(retries):
            # Refresh timestamp + signature on every attempt (prevents -1021 on retry)
            params.pop("signature", None)
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = _sign(params)
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
                    body = await r.json()
                    if r.status != 200:
                        log("MODULE_3", "ORDER_REJECTED",
                            code=body.get("code"), msg=body.get("msg"),
                            params=str({k: v for k, v in params.items()
                                        if k not in ("signature", "timestamp")}))
                        return None
                    return body
            except Exception as e:
                log("MODULE_3", "ORDER_ERROR", error=str(e), attempt=attempt)
                await asyncio.sleep(delay)
                delay *= 2
        log("MODULE_3", "ORDER_FAILED_ALL_RETRIES",
            params=str({k: v for k, v in params.items() if k not in ("signature", "timestamp")}))
        return None

    async def submit(self, side: str, qty: float, tick: dict, ind: dict,
                     symbol: str | None = None) -> dict | None:
        sym          = symbol or cfg.SYMBOL
        entry_price  = tick["price"]
        book_spread  = tick.get("spread_pct", 0)
        order_type   = "MARKET" if book_spread < 0.05 else "LIMIT"
        limit_price  = round(entry_price * 1.0002, 8) if side == "BUY" else round(entry_price * 0.9998, 8)

        # Enforce LOT_SIZE step — prevents -1013 filter failures
        step = await self.get_lot_step(sym)
        qty  = _round_step(qty, step)
        if qty <= 0:
            log("MODULE_3", "QTY_ZERO_AFTER_ROUNDING", symbol=sym, step=step)
            return None

        # Spot SELL requires holding the base asset — can't short without it
        if side == "SELL":
            base_bal = await self.get_base_balance(sym)
            if base_bal < qty:
                log("MODULE_3", "SELL_SKIPPED_NO_BALANCE",
                    symbol=sym, need=round(qty, 4), have=round(base_bal, 4))
                return None

        params: dict = {
            "symbol":          sym,
            "side":            side,
            "type":            order_type,
            "quantity":        qty,
            "newOrderRespType":"FULL",
        }
        if order_type == "LIMIT":
            params["price"]       = limit_price
            params["timeInForce"] = "GTC"

        log("MODULE_3", "SUBMIT_ORDER", side=side, type=order_type,
            qty=qty, symbol=sym,
            price=limit_price if order_type == "LIMIT" else entry_price)

        result = await self._post_order(params)
        if result:
            fill_price = float(result.get("fills", [{}])[0].get("price", entry_price)) if result.get("fills") else entry_price
            slippage   = abs(fill_price - entry_price) / entry_price
            if slippage > cfg.MAX_SLIPPAGE:
                log("MODULE_3", "SLIPPAGE_BREACH", fill=fill_price,
                    expected=entry_price, pct=round(slippage * 100, 4))
            log("MODULE_3", "ORDER_FILLED", orderId=result.get("orderId"),
                fill_price=fill_price, status=result.get("status"), symbol=sym)
        return result

    async def cancel_all(self):
        await self._ensure_session()
        params = {"symbol": cfg.SYMBOL, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.delete(
            cfg.SPOT_BASE_URL + "/api/v3/openOrders", params=params
        ) as r:
            log("MODULE_7", "CANCEL_ALL_ORDERS", status=r.status)

    async def close(self):
        if self._session:
            await self._session.close()
