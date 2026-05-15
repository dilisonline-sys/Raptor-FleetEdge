"""
dipu — Autonomous Crypto Trading Agent
20 years experience | High-risk / High-reward mindset
Binance Spot & USDT-M Futures

Instruction sources: authorized external agents/bots via InstructionServer.
The Binance login account is execution-only — it is NOT an instruction source.
"""
import asyncio
import sys
from market_data import MarketData
from sizing import PositionSizer
from order_manager import OrderManager
from exit_manager import ExitManager
from risk_engine import RiskEngine
from regime import RegimeClassifier
from instruction_server import InstructionServer
from logger import log
import config as cfg


DIPU_PERSONA = """
I am dipu. Twenty years in crypto — lived through Mt. Gox, the ICO bubble,
three Bitcoin halvings, the DeFi summer, and the FTX collapse.
I don't chase pumps and I don't panic-sell. Every trade has a thesis,
a defined risk, and a target. I size big when the setup is pristine
and I sit on my hands when it isn't. Capital preservation is the only
thing that keeps me in the game long enough to win big.
"""


def signal_engine(ind: dict, regime: str, override: str | None = None) -> str:
    """
    Dipu's signal engine — aggressive but disciplined.
    Returns: 'BUY' | 'SELL' | 'NONE'
    External instruction overrides are applied first if present.
    """
    if override in ("BUY", "SELL", "CLOSE_ALL"):
        log("SIGNAL", "EXTERNAL_OVERRIDE", signal=override)
        return override

    if regime == "VOLATILE":
        return "NONE"

    e9, e21, e50 = ind["ema9"], ind["ema21"], ind["ema50"]
    rsi   = ind["rsi14"]
    macd  = ind["macd"]
    msig  = ind["macd_signal"]
    price = ind["vwap"]
    bb_lo = ind["bb_lower"]
    bb_hi = ind["bb_upper"]

    # Dipu's long: trending structure + momentum + not overbought
    long_signal = (
        e9 > e21 > e50 and
        28 < rsi < 68 and
        macd > msig and
        price > e21
    )

    # Dipu's short: clear downtrend + weak momentum
    short_signal = (
        e9 < e21 < e50 and
        32 < rsi < 72 and
        macd < msig and
        price < e21
    )

    # RANGING: mean-reversion entries near BB extremes
    if regime == "RANGING":
        if price <= bb_lo * 1.002 and rsi < 38:
            long_signal = True
        elif price >= bb_hi * 0.998 and rsi > 62:
            short_signal = True
        else:
            return "NONE"

    if long_signal:
        return "BUY"
    if short_signal:
        return "SELL"
    return "NONE"


async def main_loop():
    log("AGENT", "STARTUP", persona=DIPU_PERSONA.strip())

    md      = MarketData(cfg.SYMBOL, cfg.INTERVAL)
    risk    = RiskEngine()
    om      = OrderManager()
    em      = ExitManager()
    rc      = RegimeClassifier()
    queue   = asyncio.Queue()
    server  = InstructionServer(queue)

    await md.connect()
    await server.start()

    equity = await om.get_equity()
    risk.update_metrics(equity)

    pending_override: str | None = None

    log("AGENT", "READY", symbol=cfg.SYMBOL, testnet=cfg.USE_TESTNET, equity=round(equity, 2))

    while True:
        try:
            # ── External instruction ──────────────────────────────
            try:
                instr = queue.get_nowait()
                action = instr["action"]
                if action == "HALT":
                    await risk.emergency_halt(om, "operator_halt", equity)
                elif action == "CLOSE_ALL":
                    await om.cancel_all()
                    em.positions.clear()
                    log("AGENT", "CLOSE_ALL", source=instr["source"])
                elif action in ("BUY", "SELL"):
                    pending_override = action
            except asyncio.QueueEmpty:
                pass

            # ── Halt check ───────────────────────────────────────
            if risk.halt_active():
                log("AGENT", "HALTED", msg="Waiting for halt to lift")
                await asyncio.sleep(60)
                continue

            # ── Module 1: Market Data ─────────────────────────────
            tick       = await md.get_ticker()
            book       = await md.get_orderbook()
            candles    = await md.get_klines()
            indicators = md.compute_indicators(candles)
            current_price = tick["price"]

            if not md.quality_gate(tick, book):
                await asyncio.sleep(30)
                continue

            # ── Module 8: Regime ──────────────────────────────────
            regime    = rc.classify(indicators)
            size_mult = {"TRENDING": 1.0, "RANGING": 0.5, "VOLATILE": 0.25}[regime]

            # ── Module 5: Manage open positions ───────────────────
            em.manage_open_positions(current_price, indicators)

            # ── Signal ────────────────────────────────────────────
            signal = signal_engine(indicators, regime, override=pending_override)
            pending_override = None

            if signal in ("BUY", "SELL") and not em.positions:
                # ── Module 2: Size ────────────────────────────────
                equity = await om.get_equity()
                stop_d = indicators["atr14"] * cfg.ATR_STOP_MULT
                qty    = PositionSizer.calculate(equity, current_price, stop_d, size_mult)

                if qty:
                    # ── Module 3: Submit ──────────────────────────
                    order = await om.submit(signal, qty, tick, indicators)
                    if order:
                        em.attach_exits(order, indicators)

            # ── Module 6: Risk Metrics ─────────────────────────────
            equity = await om.get_equity()
            risk.update_metrics(equity)

            await asyncio.sleep(60)

        except KeyboardInterrupt:
            log("AGENT", "SHUTDOWN", reason="KeyboardInterrupt")
            await om.cancel_all()
            await md.close()
            await om.close()
            sys.exit(0)
        except Exception as e:
            log("AGENT", "LOOP_ERROR", error=str(e))
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main_loop())
