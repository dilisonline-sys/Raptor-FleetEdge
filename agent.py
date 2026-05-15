"""
dipu — Autonomous Crypto Trading Agent
20 years experience | High-risk / High-reward mindset
Binance Spot & USDT-M Futures

Instruction sources: authorized external agents/bots via InstructionServer.
The Binance login account is execution-only — it is NOT an instruction source.

Dynamic symbol selection: dipu scans all USDT pairs every 15 minutes and
switches to whichever coin offers the best profit potential per minute.
"""
import asyncio
import sys
import time as _time
from market_data import MarketData
from market_scanner import MarketScanner
from sizing import PositionSizer
from order_manager import OrderManager
from exit_manager import ExitManager
from risk_engine import RiskEngine
from regime import RegimeClassifier
from sentiment import FearGreedClient
from claude_analyst import ClaudeAnalyst
from instruction_server import (InstructionServer, update_state, push_log,
                                push_transaction, update_chart, update_open_pos, _state)
from logger import log
import config as cfg


DIPU_PERSONA = """
I am dipu. Twenty years in crypto — lived through Mt. Gox, the ICO bubble,
three Bitcoin halvings, the DeFi summer, and the FTX collapse.
I don't chase pumps and I don't panic-sell. Every trade has a thesis,
a defined risk, and a target. I size big when the setup is pristine
and I sit on my hands when it isn't. Capital preservation is the only
thing that keeps me in the game long enough to win big.
I go where the money is — I don't marry a coin.
"""


def signal_engine(ind: dict, regime: str, override: str | None = None) -> str:
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

    if regime == "TRENDING":
        # Two valid entry setups in a trend:
        # 1) Momentum entry: EMA bullish stack + MACD confirmed + price above EMA21
        # 2) Pullback entry: EMA bullish stack + RSI deeply oversold (price can be below EMA21)
        # Short equivalents are mirrored.
        ema_bull = e9 > e21 > e50
        ema_bear = e9 < e21 < e50

        # Momentum entry: EMA aligned + MACD confirmed (RSI not at opposite extreme)
        long_momentum  = ema_bull and rsi < 72   and macd > msig and price > e21
        short_momentum = ema_bear and rsi > 28   and macd < msig and price < e21

        # Pullback/continuation entry: EMA aligned + RSI at extreme (trend continuation)
        # In a strong downtrend RSI can stay < 32 — that's a continuation, not a reversal
        long_pullback  = ema_bull and rsi < 32   and price > e50
        short_pullback = ema_bear and rsi < 35   and macd < msig  # oversold in downtrend → short continuation

        long_signal  = long_momentum  or long_pullback
        short_signal = short_momentum or short_pullback

    else:
        # RANGING: tighter band — mean reversion off Bollinger bands
        long_signal = (
            e9 > e21 > e50 and 28 < rsi < 65 and macd > msig and price > e21
        )
        short_signal = (
            e9 < e21 < e50 and 35 < rsi < 72 and macd < msig and price < e21
        )

    if regime == "RANGING":
        if price <= bb_lo * 1.002 and rsi < 38:
            long_signal = True
        elif price >= bb_hi * 0.998 and rsi > 62:
            short_signal = True
        else:
            return "NONE"

    if long_signal:  return "BUY"
    if short_signal: return "SELL"
    return "NONE"


async def main_loop():
    log("AGENT", "STARTUP", persona=DIPU_PERSONA.strip())

    scanner  = MarketScanner()
    risk     = RiskEngine()
    om       = OrderManager()
    em       = ExitManager()
    rc       = RegimeClassifier()
    fg       = FearGreedClient()
    analyst  = ClaudeAnalyst()
    queue    = asyncio.Queue()
    server   = InstructionServer(queue)

    await server.start()
    await fg.connect()

    equity = await om.get_equity()
    risk.update_metrics(equity)

    # Initial scan to pick best symbol
    active_symbol = await scanner.scan()
    md = MarketData(active_symbol, cfg.INTERVAL)
    await md.connect()

    pending_override:    str | None = None
    volatile_since:      float      = 0.0   # when current coin first entered VOLATILE
    gate_fail_streak:    int        = 0     # consecutive quality gate failures
    none_signal_streak:  int        = 0     # consecutive NONE-signal cycles (no trade setup)
    _ranked_idx:         int        = 0     # which scanner.ranked slot we're currently on
    VOLATILE_ESCAPE_SECS   = 1800          # 30 minutes
    GATE_FAIL_SWITCH_AFTER = 5            # switch coin after 5 straight gate fails (~5 min)
    NONE_SIGNAL_ROTATE     = 3            # rotate to next ranked coin after 3 NONE cycles (~3 min)

    log("AGENT", "READY", symbol=active_symbol, testnet=cfg.USE_TESTNET, equity=round(equity, 2))
    update_state(equity=equity, symbol=active_symbol)

    while True:
        try:
            # ── External instruction ──────────────────────────────
            try:
                instr = queue.get_nowait()
                action = instr["action"]
                if action == "RESUME":
                    risk.halt_flag  = False
                    risk.halt_until = 0
                    push_log("[INSTRUCTION] RESUME — trading resumed by operator")
                    log("AGENT", "RESUMED", source=instr.get("source",""))
                    update_state(halt=False)
                elif action == "HALT":
                    await risk.emergency_halt(om, "operator_halt", equity)
                elif action == "CLOSE_ALL":
                    await om.cancel_all()
                    em.positions.clear()
                    log("AGENT", "CLOSE_ALL", source=instr["source"])
                    push_log("[INSTRUCTION] CLOSE_ALL received — all positions cleared")
                elif action == "SWITCH_MODE":
                    new_mode = instr.get("mode", "").lower()
                    if new_mode in ("testnet", "demo", "live"):
                        cfg.TRADING_MODE = new_mode
                        ep = {
                            "testnet": ("https://testnet.binance.vision",    "wss://testnet.binance.vision/ws"),
                            "demo":    ("https://demo-api.binance.com",       "wss://demo-stream.binance.com/ws"),
                            "live":    ("https://api.binance.com",            "wss://stream.binance.com:9443/ws"),
                        }[new_mode]
                        cfg.SPOT_BASE_URL = ep[0]
                        cfg.SPOT_WS_URL   = ep[1]
                        cfg.USE_TESTNET   = new_mode == "testnet"
                        cfg.USE_DEMO      = new_mode == "demo"
                        cfg.USE_LIVE      = new_mode == "live"
                        update_state(trading_mode=new_mode)
                        push_log(f"[MODE_SWITCH] → {new_mode.upper()} | {ep[0]}")
                        log("AGENT", "MODE_SWITCH", mode=new_mode, url=ep[0])
                        await om.cancel_all()
                        em.positions.clear()
                    else:
                        push_log(f"[MODE_SWITCH] rejected — unknown mode: {new_mode}")
                elif action == "SWITCH_COIN":
                    requested = instr.get("symbol", "auto").upper()
                    if requested == "AUTO":
                        # Return to scanner-driven selection
                        update_state(coin_mode="auto")
                        push_log("[COIN_SELECT] Switched to automatic coin selection")
                        log("AGENT", "COIN_MODE_AUTO", source=instr.get("source",""))
                    elif requested != active_symbol and not em.positions:
                        # Manual override — switch to requested coin immediately
                        push_log(f"[COIN_SELECT] Manual override → {requested}")
                        log("AGENT", "COIN_MANUAL", symbol=requested, source=instr.get("source",""))
                        await md.close()
                        active_symbol = requested
                        md = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        update_state(symbol=active_symbol, coin_mode=requested)
                        none_signal_streak = 0
                    elif em.positions:
                        push_log(f"[COIN_SELECT] Cannot switch while position open — close first")
                elif action in ("BUY", "SELL"):
                    pending_override = action
            except asyncio.QueueEmpty:
                pass

            # ── Halt check ───────────────────────────────────────
            if risk.halt_active():
                log("AGENT", "HALTED", msg="Waiting for halt to lift")
                await asyncio.sleep(60)
                continue

            # ── Dynamic symbol selection (no open position) ───────
            if not em.positions:
                # Always run scanner to keep top_coins list fresh
                new_symbol = await scanner.scan()
                top5 = [r["symbol"] for r in scanner.ranked[:5]]
                update_state(top_coins=top5)
                # Only switch coin if in auto mode
                coin_mode = _state.get("coin_mode", "auto")
                if coin_mode == "auto" and new_symbol != active_symbol:
                    push_log(f"[SWITCH] {active_symbol} → {new_symbol} | top5={top5}")
                    log("AGENT", "SYMBOL_SWITCH", from_=active_symbol, to=new_symbol, top5=top5)
                    await md.close()
                    active_symbol = new_symbol
                    md = MarketData(active_symbol, cfg.INTERVAL)
                    await md.connect()
                    update_state(symbol=active_symbol, top_coins=top5)
                elif coin_mode != "auto":
                    push_log(f"[COIN_SELECT] Manual mode — holding {active_symbol} | top5={top5}")
                else:
                    push_log(f"[SCAN] staying on {active_symbol} | top5={top5}")

            # ── Module 1: Market Data ─────────────────────────────
            try:
                tick       = await md.get_ticker()
                book       = await md.get_orderbook()
                candles    = await md.get_klines()
                indicators = md.compute_indicators(candles)
                current_price = tick["price"]
            except Exception as e:
                log("AGENT", "DATA_ERROR", symbol=active_symbol, error=str(e))
                await asyncio.sleep(30)
                continue

            if not md.quality_gate(tick, book):
                gate_fail_streak += 1
                push_log(f"[QUALITY_GATE] {active_symbol} — gate fail #{gate_fail_streak}, sitting out")
                if gate_fail_streak >= GATE_FAIL_SWITCH_AFTER and not em.positions:
                    push_log(f"[QUALITY_GATE] {gate_fail_streak} straight fails — forcing coin switch")
                    log("AGENT", "GATE_FAIL_SWITCH", symbol=active_symbol, streak=gate_fail_streak)
                    new_symbol = await scanner.scan(exclude={active_symbol}, force=True)
                    if new_symbol != active_symbol:
                        await md.close()
                        active_symbol = new_symbol
                        md = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        update_state(symbol=active_symbol)
                        push_log(f"[SWITCH] → {active_symbol} (quality gate escape)")
                    gate_fail_streak = 0
                await asyncio.sleep(30)
                continue
            gate_fail_streak = 0  # reset on pass

            # ── Push chart data ───────────────────────────────────
            try:
                import pandas_ta as _ta
                import pandas as _pd
                c = candles["close"]
                ts_idx = [int(t.timestamp()) for t in candles.index]
                chart_candles = [
                    {"time": ts_idx[i],
                     "open":  float(candles["open"].iloc[i]),
                     "high":  float(candles["high"].iloc[i]),
                     "low":   float(candles["low"].iloc[i]),
                     "close": float(c.iloc[i])}
                    for i in range(len(candles))
                ]
                _e9  = _ta.ema(c, 9)
                _e21 = _ta.ema(c, 21)
                ema9_data  = [{"time": ts_idx[i], "value": float(_e9.iloc[i])}
                              for i in range(len(c)) if _e9.iloc[i] == _e9.iloc[i]]
                ema21_data = [{"time": ts_idx[i], "value": float(_e21.iloc[i])}
                              for i in range(len(c)) if _e21.iloc[i] == _e21.iloc[i]]
                update_chart(active_symbol, chart_candles, ema9_data, ema21_data)
            except Exception as ce:
                log("AGENT", "CHART_ERROR", error=str(ce))

            # ── Sentiment: Fear & Greed ───────────────────────────
            fear_greed = await fg.fetch()
            fg_mult    = fg.size_multiplier()
            update_state(fear_greed=fear_greed)
            push_log(f"[SENTIMENT] Fear&Greed={fear_greed['value']} ({fear_greed['label']}) "
                     f"| size_mult={fg_mult:.2f}")

            # ── Module 8: Regime ──────────────────────────────────
            regime    = rc.classify(indicators)
            size_mult = {"TRENDING": 1.0, "RANGING": 0.5, "VOLATILE": 0.25}[regime] * fg_mult
            update_state(regime=regime, equity=equity, positions=len(em.positions),
                         symbol=active_symbol)
            push_log(f"[CYCLE] {active_symbol} | price={current_price:.4f} | regime={regime} "
                     f"| RSI={indicators['rsi14']:.1f} | EMA9={indicators['ema9']:.4f}")

            # ── Volatile escape: switch coin after 30 min of VOLATILE ─
            if regime == "VOLATILE":
                if volatile_since == 0.0:
                    volatile_since = _time.time()
                elapsed = _time.time() - volatile_since
                if elapsed >= VOLATILE_ESCAPE_SECS:
                    mins = int(elapsed // 60)
                    push_log(f"[VOLATILE_ESCAPE] {active_symbol} volatile for {mins}m — forcing coin switch")
                    log("AGENT", "VOLATILE_ESCAPE", symbol=active_symbol, minutes=mins)
                    if em.positions:
                        await om.cancel_all()
                        em.positions.clear()
                        push_log(f"[VOLATILE_ESCAPE] closed open position to switch coin")
                    new_symbol = await scanner.scan(exclude={active_symbol}, force=True)
                    if new_symbol != active_symbol:
                        push_log(f"[SWITCH] {active_symbol} → {new_symbol} (volatile escape)")
                        log("AGENT", "SYMBOL_SWITCH", from_=active_symbol, to=new_symbol,
                            reason="volatile_escape")
                        await md.close()
                        active_symbol = new_symbol
                        md = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        update_state(symbol=active_symbol)
                    volatile_since = 0.0
                    await asyncio.sleep(60)
                    continue
            else:
                volatile_since = 0.0  # reset counter when regime clears

            # ── Module 5: Manage open positions ───────────────────
            exit_actions = em.manage_open_positions(current_price, indicators)
            for act in exit_actions:
                push_log(f"[EXIT] {act} @ {current_price:.4f}")

            # Update open position overlay
            if em.positions:
                p = em.positions[0]
                update_open_pos({
                    "side":      p.side,
                    "avg_entry": p.avg_entry,
                    "stop":      p.stop,
                    "tp1":       p.tp1,
                    "tp2":       p.tp2,
                    "qty":       p.qty,
                })
            else:
                update_open_pos(None)

            # ── Signal ────────────────────────────────────────────
            signal = signal_engine(indicators, regime, override=pending_override)
            pending_override = None
            update_state(last_signal=signal)
            if signal != "NONE":
                push_log(f"[SIGNAL] {signal} on {active_symbol} | regime={regime}")
                none_signal_streak = 0
                _ranked_idx        = 0
            else:
                none_signal_streak += 1
                # Rotate to the next best ranked coin when no setup found repeatedly
                if none_signal_streak >= NONE_SIGNAL_ROTATE and not em.positions and scanner.ranked:
                    _ranked_idx = (_ranked_idx + 1) % len(scanner.ranked)
                    next_sym    = scanner.ranked[_ranked_idx]["symbol"]
                    if next_sym != active_symbol:
                        push_log(f"[ROTATE] No setup on {active_symbol} for {none_signal_streak} cycles — trying {next_sym}")
                        log("AGENT", "SIGNAL_ROTATE", from_=active_symbol, to=next_sym,
                            streak=none_signal_streak, idx=_ranked_idx)
                        await md.close()
                        active_symbol      = next_sym
                        md                 = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        update_state(symbol=active_symbol)
                        none_signal_streak = 0
                    await asyncio.sleep(60)
                    continue

            # ── Claude thesis (async, non-blocking) ───────────────
            thesis = await analyst.analyze(
                active_symbol, indicators, regime, fear_greed, signal
            )
            if thesis:
                update_state(claude_thesis=thesis)
                push_log(f"[CLAUDE] {thesis[:120]}")

            if signal in ("BUY", "SELL") and not em.positions:
                equity = await om.get_equity()
                stop_d = indicators["atr14"] * cfg.ATR_STOP_MULT
                qty    = PositionSizer.calculate(equity, current_price, stop_d, size_mult)

                if qty:
                    order = await om.submit(signal, qty, tick, indicators,
                                            symbol=active_symbol)

                    if order:
                        push_log(f"[TRADE] {signal} {active_symbol} qty={round(qty,5)} @ ~{current_price:.4f}")
                        pos = em.attach_exits(order, indicators)
                        if pos:
                            push_transaction({
                                "side":   signal,
                                "symbol": active_symbol,
                                "qty":    round(pos.qty, 5),
                                "price":  pos.avg_entry,
                                "stop":   pos.stop,
                                "tp1":    pos.tp1,
                                "tp2":    pos.tp2,
                                "risk":   round(pos.initial_risk, 2),
                                "pnl":    "",
                                "status": "OPEN",
                            })

            # ── Module 6: Risk Metrics ─────────────────────────────
            equity = await om.get_equity()
            risk.update_metrics(equity)
            update_state(
                equity=equity,
                daily_dd=((risk.day_start_equity - equity) / risk.day_start_equity * 100)
                          if risk.day_start_equity else 0,
                halt=risk.halt_flag,
            )

            await asyncio.sleep(60)

        except KeyboardInterrupt:
            log("AGENT", "SHUTDOWN", reason="KeyboardInterrupt")
            await om.cancel_all()
            await md.close()
            await om.close()
            await scanner.close()
            await fg.close()
            sys.exit(0)
        except Exception as e:
            log("AGENT", "LOOP_ERROR", error=str(e))
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main_loop())
