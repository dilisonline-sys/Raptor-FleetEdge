"""
Raptor FleetEdge Stock Agent — port 7431
Monitors all coins held in the wallet that are not actively traded by any fleet agent.
Sells each parked coin automatically once its price rises 5% above the park price.
Also auto-parks any unmanaged coin it finds that isn't already in the registry.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

# Config must be imported before any other local module so TRADING_MODE is set.
import config as cfg
import equity_pool as _ep
import parked_coins as _pc
import order_manager as _om_mod
from logger import log

STOCK_AGENT_PORT  = int(os.environ.get("AGENT_PORT", 7431))
SCAN_INTERVAL_SEC = 30   # how often to check prices
_agent_name       = os.environ.get("AGENT_NAME", "stock_agent")

# BNB is excluded: Binance uses BNB internally for fee discounts.
# Selling BNB can disable the fee discount and cause unexpected fee increases.
EXCLUDED_ASSETS   = {"BNB"}
EXCLUDED_SYMBOLS  = {"BNBUSDT"}

# ── State ─────────────────────────────────────────────────────────────────────
_sse_clients: list[web.StreamResponse] = []
_log_buffer:  list[str] = []
_status: dict = {
    "running": True,
    "mode":    cfg.TRADING_MODE,
    "parked":  {},
    "last_scan": 0.0,
    "sells_today": 0,
}


def push_log(msg: str):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _log_buffer.append(line)
    if len(_log_buffer) > 500:
        _log_buffer.pop(0)
    for client in list(_sse_clients):
        asyncio.create_task(_send_sse(client, line))
    print(line, flush=True)


async def _send_sse(client: web.StreamResponse, data: str):
    try:
        await client.write(f"data: {data}\n\n".encode())
    except Exception:
        _sse_clients.discard(client) if hasattr(_sse_clients, 'discard') else None


# ── HTTP handlers ──────────────────────────────────────────────────────────────
async def handle_status(request):
    parked = _pc.get_parked()
    _status["parked"] = parked
    _status["parked_count"] = len(parked)
    return web.json_response(_status)


async def handle_sse(request):
    resp = web.StreamResponse(headers={
        "Content-Type":  "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    _sse_clients.append(resp)
    try:
        for line in _log_buffer[-60:]:
            await resp.write(f"data: {line}\n\n".encode())
        while True:
            await asyncio.sleep(15)
            await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        if resp in _sse_clients:
            _sse_clients.remove(resp)
    return resp


async def handle_index(request):
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Stock Agent — Raptor FleetEdge</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080808;color:#fff;font-family:'Courier New',monospace;padding:28px}}
h1{{color:#00e5ff;font-size:1.4rem;margin-bottom:4px}}
.sub{{color:#aaa;font-size:.76rem;margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;margin-bottom:24px;font-size:.8rem}}
th{{color:#00e5ff;border-bottom:1px solid #222;padding:8px 12px;text-align:left}}
td{{padding:7px 12px;border-bottom:1px solid #111}}
.g{{color:#00e676}} .r{{color:#ff1744}} .y{{color:#ffd600}}
#log{{background:#0a0a0a;border:1px solid #1e1e1e;border-radius:6px;
      padding:14px;height:340px;overflow-y:auto;font-size:.72rem;line-height:1.7}}
</style>
</head>
<body>
<h1>Stock Agent</h1>
<div class="sub">Mode: <b>{cfg.TRADING_MODE}</b> &nbsp;|&nbsp; Port: {STOCK_AGENT_PORT} &nbsp;|&nbsp; Scan: every {SCAN_INTERVAL_SEC}s</div>
<table id="tbl">
<thead><tr><th>Symbol</th><th>Qty</th><th>Park Price</th><th>Target (+5%)</th><th>Status</th></tr></thead>
<tbody id="tbody"><tr><td colspan="5" style="color:#555">Loading...</td></tr></tbody>
</table>
<div id="log"></div>
<script>
async function refresh(){{
  const r = await fetch('/status');
  const d = await r.json();
  const rows = Object.entries(d.parked||{{}}).map(([sym,e])=>
    `<tr><td class="y">${{sym}}</td><td>${{(+e.qty).toFixed(6)}}</td>
     <td>${{(+e.park_price).toFixed(4)}}</td>
     <td class="g">${{(+e.target_price).toFixed(4)}}</td>
     <td>watching</td></tr>`).join('');
  document.getElementById('tbody').innerHTML=rows||'<tr><td colspan="5" style="color:#555">No parked coins</td></tr>';
}}
refresh(); setInterval(refresh,10000);
const evs=new EventSource('/sse');
const logEl=document.getElementById('log');
evs.onmessage=e=>{{
  const d=document.createElement('div'); d.textContent=e.data;
  logEl.appendChild(d); logEl.scrollTop=logEl.scrollHeight;
}};
</script>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


# ── Core scanning logic ────────────────────────────────────────────────────────

async def _fetch_prices_batch(session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, float]:
    """Batch-fetch live prices for a list of symbols from Binance public API."""
    if not symbols:
        return {}
    syms_json = json.dumps(symbols)
    prices = {}
    try:
        async with session.get(
            cfg.PUBLIC_DATA_URL + "/api/v3/ticker/price",
            params={"symbols": syms_json},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            r.raise_for_status()
            for item in await r.json():
                prices[item["symbol"]] = float(item["price"])
    except Exception as e:
        log("STOCK_AGENT", "PRICE_FETCH_ERROR", error=str(e)[:120])
    return prices


async def scan_loop(om: _om_mod.OrderManager):
    """Main monitoring loop — runs every SCAN_INTERVAL_SEC seconds."""
    await asyncio.sleep(5)  # brief startup delay
    push_log(f"[STOCK_AGENT] Started — mode={cfg.TRADING_MODE}, port={STOCK_AGENT_PORT}")

    async with aiohttp.ClientSession() as http:
        while True:
            try:
                await _scan_once(om, http)
            except Exception as e:
                push_log(f"[STOCK_AGENT] scan error: {e}")
                log("STOCK_AGENT", "SCAN_ERROR", error=str(e)[:160])
            _status["last_scan"] = time.time()
            await asyncio.sleep(SCAN_INTERVAL_SEC)


async def _scan_once(om: _om_mod.OrderManager, http: aiohttp.ClientSession):
    parked = _pc.get_parked()

    # Auto-park unmanaged coins not already tracked ──────────────────────────
    try:
        held = await om.get_all_significant_balances(min_usdt_value=5.0)
        managed_symbols = _ep.get_other_symbols(-1)  # all active agent symbols
        # Add currently parked symbols so we don't re-park them
        already_parked = set(parked.keys())

        for coin in held:
            if coin["asset"] in EXCLUDED_ASSETS:
                continue
            sym = coin["asset"] + "USDT"
            if sym in EXCLUDED_SYMBOLS or sym in managed_symbols or sym in already_parked:
                continue
            # Unmanaged coin — auto-park at current price
            _pc.park(sym, coin["qty"], coin["price"], slot=-1)
            already_parked.add(sym)
            parked[sym] = {
                "qty": coin["qty"],
                "park_price": coin["price"],
                "target_price": round(coin["price"] * _pc.RECOVERY_PCT, 8),
                "parked_at": time.time(),
                "slot": -1,
            }
            push_log(f"[AUTO_PARK] {sym} qty={coin['qty']:.6f} @ {coin['price']:.4f} — monitoring for +5%")
            log("STOCK_AGENT", "AUTO_PARK", symbol=sym, qty=round(coin["qty"], 6),
                price=round(coin["price"], 4))
    except Exception as e:
        push_log(f"[STOCK_AGENT] balance sweep error: {e}")

    # Refresh parked after possible auto-parks
    parked = _pc.get_parked()
    if not parked:
        return

    # Fetch live prices for all parked symbols ────────────────────────────────
    symbols = list(parked.keys())
    prices = await _fetch_prices_batch(http, symbols)

    # Update pool with current parked USDT value
    parked_usdt = sum(
        entry["qty"] * prices.get(sym, entry["park_price"])
        for sym, entry in parked.items()
    )
    _ep.report_parked_usdt(parked_usdt)
    _ep.set_parked_symbols(list(parked.keys()))

    # Check each coin for recovery target; log watching status for all ─────────
    ready_to_sell: list[tuple[str, float, float]] = []  # (sym, qty, price)
    for sym, entry in list(parked.items()):
        if sym in EXCLUDED_SYMBOLS:
            continue
        price = prices.get(sym)
        if price is None:
            continue
        target = entry["target_price"]
        pct_to_target = (price / target - 1) * 100
        push_log(f"[WATCH] {sym} price={price:.4f} target={target:.4f} ({pct_to_target:+.2f}%)")
        if price >= target:
            ready_to_sell.append((sym, entry["qty"], price))
            log("STOCK_AGENT", "SELL_TRIGGER", symbol=sym, qty=round(entry["qty"], 6),
                price=round(price, 4), target=round(target, 4))

    # Fire all sell orders in parallel so multiple coins exit simultaneously ───
    if ready_to_sell:
        push_log(f"[SELL_BATCH] {len(ready_to_sell)} coin(s) reached target — selling in parallel: "
                 f"{[s for s,*_ in ready_to_sell]}")

        async def _sell_one(sym: str, qty: float, price: float):
            tick = {"bid": price * 0.9995, "ask": price * 1.0005,
                    "price": price, "volume_24h": 1e9}
            ind  = {"atr14": price * 0.01, "rsi14": 50.0,
                    "ema20": price, "ema50": price}
            try:
                sold = await om.submit("SELL", qty, tick, ind, symbol=sym)
                if sold:
                    _pc.unpark(sym)
                    _status["sells_today"] = _status.get("sells_today", 0) + 1
                    push_log(f"[SOLD] {sym} qty={qty:.6f} @ ~{price:.4f} — unparked")
                    log("STOCK_AGENT", "SOLD", symbol=sym, qty=round(qty, 6), price=round(price, 4))
                else:
                    push_log(f"[SELL_WARN] {sym} submit failed — will retry next scan")
            except Exception as sell_err:
                push_log(f"[SELL_ERR] {sym}: {sell_err}")
                log("STOCK_AGENT", "SELL_ERROR", symbol=sym, error=str(sell_err)[:120])

        await asyncio.gather(*[_sell_one(s, q, p) for s, q, p in ready_to_sell])


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    om = _om_mod.OrderManager()

    app = web.Application()
    app.router.add_get("/",       handle_index)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/sse",    handle_sse)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", STOCK_AGENT_PORT)
    await site.start()
    push_log(f"[STOCK_AGENT] HTTP server on port {STOCK_AGENT_PORT}")

    await scan_loop(om)


if __name__ == "__main__":
    asyncio.run(main())
