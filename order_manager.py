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
        self._price_cache: dict[str, float] = {}  # asset → last known USDT price

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
        """Total portfolio value in USDT — counts USDT + all held coins at live prices.
        Active base asset is priced at the provided `price` (live WS tick); all others via REST.
        """
        await self._ensure_session()
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.get(cfg.SPOT_BASE_URL + "/api/v3/account", params=params) as r:
            r.raise_for_status()
            data        = await r.json()
            balances    = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in data["balances"]}
            usdt        = balances.get("USDT", 0.0)
            active_base = symbol.replace("USDT", "") if symbol else None

            # Add active base at caller-provided live price
            if active_base and price and price > 0:
                usdt += balances.get(active_base, 0.0) * price

            # Price every other non-USDT, non-active-base coin via REST
            for asset, bal in balances.items():
                if asset in ("USDT",) or asset == active_base or bal < 1e-8:
                    continue
                try:
                    async with self._session.get(
                        cfg.PUBLIC_DATA_URL + "/api/v3/ticker/price",
                        params={"symbol": asset + "USDT"}
                    ) as rp:
                        coin_price = float((await rp.json()).get("price", 0))
                    if coin_price > 0:
                        self._price_cache[asset] = coin_price
                        usdt += bal * coin_price
                except Exception:
                    cached = self._price_cache.get(asset, 0.0)
                    if cached > 0:
                        usdt += bal * cached

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

    async def get_balances_raw(self, symbol: str) -> tuple[float, float, float]:
        """Returns (non_base_usdt_equiv, base_qty, raw_usdt).
        non_base_usdt_equiv = USDT + all non-active coins priced via batch fetch
        base_qty            = active base asset quantity (caller provides live price for it)
        raw_usdt            = spendable USDT only (for order sizing)
        Equity = non_base_usdt_equiv + base_qty * live_price
        """
        await self._ensure_session()
        base = symbol.replace("USDT", "")
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.get(
            cfg.SPOT_BASE_URL + "/api/v3/account",
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            r.raise_for_status()
            data = await r.json()

        bals     = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in data["balances"]}
        raw_usdt = bals.get("USDT", 0.0)
        usdt     = raw_usdt

        # Collect assets that need pricing (all except USDT and the active base)
        to_price = {a: q for a, q in bals.items()
                    if a not in ("USDT", base) and q >= 1e-8}
        if not to_price:
            return usdt, bals.get(base, 0.0), raw_usdt

        # Batch-fetch ALL USDT pair prices in one public call
        all_prices: dict[str, float] = {}
        try:
            async with self._session.get(
                cfg.PUBLIC_DATA_URL + "/api/v3/ticker/price",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as rp:
                rp.raise_for_status()
                for item in await rp.json():
                    sym = item.get("symbol", "")
                    if sym.endswith("USDT"):
                        try:
                            all_prices[sym] = float(item["price"])
                        except (ValueError, KeyError):
                            pass
        except Exception as e:
            log("ORDER_MANAGER", "BATCH_PRICE_FETCH_FAILED", error=str(e)[:80])

        for asset, qty in to_price.items():
            price = all_prices.get(asset + "USDT", 0.0)
            if price > 0:
                self._price_cache[asset] = price
                usdt += qty * price
            else:
                # Fall back to cache for assets without a direct USDT pair (e.g. LD tokens)
                cached = self._price_cache.get(asset, 0.0)
                if cached > 0:
                    usdt += qty * cached

        return usdt, bals.get(base, 0.0), raw_usdt

    # Stablecoins to skip when iterating balances — they are USDT-equivalent, not tradeable positions
    _STABLECOINS = frozenset({"USDT", "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USD1"})

    async def get_all_significant_balances(self, min_usdt_value: float = 5.0) -> list[dict]:
        """Returns all spot coin holdings worth >= min_usdt_value as [{asset, qty, usdt_value, price}].

        Uses two API calls total:
          1. GET /api/v3/account — authenticated, returns all balances
          2. GET /api/v3/ticker/price — public, returns ALL ~2000 pair prices in one shot

        The previous implementation made one individual price call per asset sequentially,
        meaning most coins were silently dropped when requests failed or timed out.
        """
        await self._ensure_session()

        # 1. Fetch all account balances (authenticated)
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.get(
            cfg.SPOT_BASE_URL + "/api/v3/account",
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            r.raise_for_status()
            data = await r.json()

        # Skip stablecoins, LD-prefixed earn tokens, and dust
        bals: dict[str, float] = {}
        for b in data.get("balances", []):
            asset = b["asset"]
            if asset in self._STABLECOINS or asset.startswith("LD"):
                continue
            qty = float(b["free"]) + float(b["locked"])
            if qty >= 1e-8:
                bals[asset] = qty

        if not bals:
            return []

        # 2. Batch-fetch ALL live prices in a single public call
        all_prices: dict[str, float] = {}
        try:
            async with self._session.get(
                cfg.PUBLIC_DATA_URL + "/api/v3/ticker/price",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as rp:
                rp.raise_for_status()
                for item in await rp.json():
                    sym = item.get("symbol", "")
                    if sym.endswith("USDT"):
                        try:
                            all_prices[sym] = float(item["price"])
                        except (ValueError, KeyError):
                            pass
        except Exception as e:
            log("ORDER_MANAGER", "BATCH_PRICE_FETCH_FAILED", error=str(e)[:120])
            return []

        # 3. Match each held asset against the price map
        result = []
        for asset, qty in bals.items():
            sym = asset + "USDT"
            price = all_prices.get(sym, 0.0)
            if price <= 0:
                continue
            usdt_val = qty * price
            if usdt_val >= min_usdt_value:
                self._price_cache[asset] = price
                result.append({"asset": asset, "qty": qty,
                                "usdt_value": round(usdt_val, 4), "price": price})

        return result

    # Maps Binance Simple Earn LD-prefixed tokens to their underlying asset symbol
    _LD_MAP: dict[str, str] = {
        "LDETH": "ETH", "LDLINK": "LINK", "LDZEC": "ZEC", "LDONT": "ONT",
        "LDALGO": "ALGO", "LDCHZ": "CHZ", "LDSTORJ": "STORJ", "LDENJ": "ENJ",
        "LDNEAR": "NEAR", "LDSHIB": "SHIB", "LDSHIB2": "SHIB", "LDFIDA": "FIDA",
        "LDSUI": "SUI", "LDTON": "TON", "LDTST": "TST", "LDLAYER": "LAYER",
        "LDONDO": "ONDO", "LDHOME": "HOME", "LDSAHARA": "SAHARA", "LDOPEN": "OPEN",
        "LDXAUT": "XAUT", "LDAIGENSYN": "AIGENSYN", "LDBTC": "BTC",
        "LDBNB": "BNB", "LDETH2": "ETH", "LDSOL": "SOL", "LDUSDC": "USDC",
    }

    async def get_earn_value(self) -> float:
        """Returns total USDT value of all Simple Earn (LD-prefixed) flexible holdings."""
        await self._ensure_session()
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        try:
            async with self._session.get(cfg.SPOT_BASE_URL + "/api/v3/account", params=params) as r:
                r.raise_for_status()
                data = await r.json()
            bals = {b["asset"]: float(b["free"]) + float(b["locked"])
                    for b in data["balances"]
                    if b["asset"].startswith("LD") and float(b["free"]) + float(b["locked"]) > 1e-8}
        except Exception as e:
            log("MODULE_2", "EARN_FETCH_ERROR", error=str(e))
            return 0.0

        total = 0.0
        for ld_asset, qty in bals.items():
            underlying = self._LD_MAP.get(ld_asset, ld_asset[2:])  # strip "LD" prefix as fallback
            price = 0.0
            try:
                async with self._session.get(
                    cfg.PUBLIC_DATA_URL + "/api/v3/ticker/price",
                    params={"symbol": underlying + "USDT"}
                ) as rp:
                    rdata = await rp.json()
                    if "price" in rdata:
                        price = float(rdata["price"])
                        self._price_cache[underlying] = price
            except Exception:
                price = self._price_cache.get(underlying, 0.0)
            if price > 0:
                total += qty * price
        log("MODULE_2", "EARN_VALUE", usdt=round(total, 2))
        return total

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

    async def cancel_all(self, symbol: str | None = None):
        # FIX-1: accept active symbol so callers don't silently cancel the wrong pair
        sym = symbol or cfg.SYMBOL
        await self._ensure_session()
        params = {"symbol": sym, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        async with self._session.delete(
            cfg.SPOT_BASE_URL + "/api/v3/openOrders", params=params
        ) as r:
            log("MODULE_7", "CANCEL_ALL_ORDERS", symbol=sym, status=r.status)

    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """Cancel a specific order by orderId. Returns True if successfully cancelled."""
        await self._ensure_session()
        params = {"symbol": symbol, "orderId": order_id,
                  "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params)
        try:
            async with self._session.delete(
                cfg.SPOT_BASE_URL + "/api/v3/order", params=params
            ) as r:
                ok = r.status == 200
                log("MODULE_7", "CANCEL_ORDER", symbol=symbol, orderId=order_id, ok=ok)
                return ok
        except Exception as e:
            log("MODULE_7", "CANCEL_ORDER_ERROR", symbol=symbol, orderId=order_id, error=str(e))
            return False

    async def place_stop_limit(self, symbol: str, qty: float, stop_price: float) -> int:
        """Place a STOP_LOSS_LIMIT SELL order. Limit is 0.5% below stop to ensure fill.
        Returns the exchange orderId (0 on failure)."""
        step = await self.get_lot_step(symbol)
        qty  = _round_step(qty, step)
        if qty <= 0:
            return 0
        limit_price = round(stop_price * 0.995, 8)
        params = {
            "symbol":          symbol,
            "side":            "SELL",
            "type":            "STOP_LOSS_LIMIT",
            "quantity":        qty,
            "stopPrice":       stop_price,
            "price":           limit_price,
            "timeInForce":     "GTC",
            "newOrderRespType":"RESULT",
        }
        result = await self._post_order(params)
        if result:
            oid = int(result.get("orderId", 0))
            log("MODULE_3", "STOP_LIMIT_PLACED", symbol=symbol,
                qty=qty, stop=stop_price, limit=limit_price, orderId=oid)
            return oid
        return 0

    async def reset_session(self):
        """Close and recreate the aiohttp session with fresh API credentials from cfg.
        Called after a SWITCH_MODE so new API keys are picked up."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = aiohttp.ClientSession(headers={"X-MBX-APIKEY": cfg.BINANCE_API_KEY})
        log("MODULE_3", "SESSION_RESET", mode=cfg.TRADING_MODE)

    async def close(self):
        if self._session:
            await self._session.close()
