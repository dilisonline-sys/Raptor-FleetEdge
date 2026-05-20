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
import dataclasses
import json
import os
import sys
import time as _time
from market_data import MarketData
from market_scanner import MarketScanner
from sizing import PositionSizer
from order_manager import OrderManager
from exit_manager import ExitManager, Position
from risk_engine import RiskEngine
from regime import RegimeClassifier
from sentiment import FearGreedClient
from instruction_server import (InstructionServer, update_state, push_log,
                                push_transaction, update_chart, update_open_pos, _state,
                                set_wake_event)
from logger import log
import config as cfg
import email_notifier as _email
import equity_pool as _ep
from ai_analyst import AIAnalyst


# ── Position persistence ──────────────────────────────────────────────────
def _pos_file(slot: int) -> str:
    return f"/tmp/dipu_positions_{slot}.json"


def _save_positions(slot: int, symbol: str, positions: list) -> None:
    """Write open positions to disk so they survive a restart."""
    try:
        data = {
            "symbol":    symbol,
            "slot":      slot,
            "ts":        _time.time(),
            "positions": [dataclasses.asdict(p) for p in positions],
        }
        with open(_pos_file(slot), "w") as _f:
            json.dump(data, _f)
        log("AGENT", "POS_SAVED", slot=slot, symbol=symbol, count=len(positions))
    except Exception as _e:
        log("AGENT", "POS_SAVE_ERROR", error=str(_e))


def _load_positions(slot: int, symbol: str) -> list | None:
    """Restore positions from the previous run if they match the current symbol."""
    path = _pos_file(slot)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as _f:
            data = json.load(_f)
        if data.get("symbol") != symbol:
            log("AGENT", "POS_RESTORE_SKIP", reason="symbol_mismatch",
                saved=data.get("symbol"), current=symbol)
            return None
        positions = [Position(**p) for p in data.get("positions", [])]
        os.unlink(path)  # consume — prevents re-loading stale state on second restart
        log("AGENT", "POS_RESTORED", slot=slot, symbol=symbol, count=len(positions))
        return positions if positions else None
    except Exception as _e:
        log("AGENT", "POS_LOAD_ERROR", error=str(_e))
        return None


def _clear_positions_file(slot: int) -> None:
    """Remove position file once all positions are closed."""
    try:
        path = _pos_file(slot)
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


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

    # Volatility chase strategy: momentum-only, skip RANGING entirely
    if regime in ("VOLATILE", "RANGING"):
        return "NONE"

    e9, e21, e50 = ind["ema9"], ind["ema21"], ind["ema50"]
    rsi        = ind["rsi14"]
    macd       = ind["macd"]
    msig       = ind["macd_signal"]
    price      = ind["close"]
    open_price = ind.get("open", price)
    bb_lo      = ind["bb_lower"]
    bb_hi      = ind["bb_upper"]

    # TRENDING only — momentum-first on 15m candles
    ema_bull = e9 > e21 > e50
    ema_bear = e9 < e21 < e50

    # Candle body filter: current candle must be showing live momentum, not a stale crossover.
    # Threshold is ATR-relative so low-vol coins (BTC) aren't permanently blocked.
    # Floor at 0.05% to keep the filter meaningful even on ultra-low-ATR coins.
    atr14         = ind.get("atr14", 0)
    atr_pct       = (atr14 / price * 100) if price and atr14 else 0
    body_threshold = max(0.05, atr_pct * 0.25)  # 25% of ATR%: BTC≈0.07%, alts≈0.3-1%
    candle_body_pct = (price - open_price) / open_price * 100 if open_price else 0
    candle_bull = candle_body_pct > body_threshold
    candle_bear = candle_body_pct < -body_threshold

    # Primary momentum entry: EMA stack + MACD + price above/below EMA21 + live candle
    long_momentum  = ema_bull and rsi < 70 and macd > msig and price > e21 and candle_bull
    short_momentum = ema_bear and rsi > 30 and macd < msig and price < e21 and candle_bear

    # Continuation entry: deep pullback in confirmed bull trend only
    # Disabled in bear — counter-trend pullbacks fail too quickly in downtrends
    long_pullback  = ema_bull and rsi < 35 and price > e50
    short_pullback = ema_bear and rsi > 65 and price < e50

    # Extreme oversold BUY at lower BB support (bull trend only)
    long_bb = price <= bb_lo * 1.003 and rsi < 32 and ema_bull

    # Mean-reversion BUY: deeply oversold in bear trend — catch the bounce
    # RSI < 28 (raised from 22): still rare but catches genuine capitulation bottoms
    # Requires price at/below lower BB to confirm flush
    bear_reversal = ema_bear and rsi < 28 and price <= bb_lo * 1.005

    # Trend-transition BUY: bear stack but MACD has gone bullish and price reclaimed EMA50.
    # Captures early reversals where EMA stack lags the actual price recovery.
    # MACD > signal confirms momentum shift; price > EMA50 confirms structure recovery.
    transition_long = (ema_bear and macd > msig and price > e50
                       and 45 < rsi < 68 and candle_bull)

    # In a bear trend: allow bear_reversal and transition_long
    if ema_bear:
        long_signal = bear_reversal or transition_long
    else:
        long_signal = long_momentum or long_pullback or long_bb

    short_signal = short_momentum or short_pullback

    if long_signal:  return "BUY"
    if short_signal: return "SELL"
    return "NONE"


async def _run_analyst(analyst, symbol, price, indicators, candles, regime, fear_greed, position):
    """Fire-and-forget wrapper: runs AI analyst and pushes result to dashboard state."""
    try:
        result = await analyst.maybe_run(symbol, price, indicators, candles,
                                         regime, fear_greed, position, interval_secs=180)
        if result:
            update_state(ai_analysis=result)
    except Exception as e:
        log("AI_ANALYST", "TASK_ERROR", error=str(e)[:80])


async def main_loop():
    log("AGENT", "STARTUP", persona=DIPU_PERSONA.strip())

    scanner  = MarketScanner()
    risk     = RiskEngine()
    om       = OrderManager()
    em       = ExitManager()
    rc       = RegimeClassifier()
    fg       = FearGreedClient()
    analyst  = AIAnalyst()
    queue       = asyncio.Queue()
    _agent_slot = int(os.environ.get("AGENT_SLOT", "0"))
    _agent_name = os.environ.get("AGENT_NAME", f"dipu-slot{_agent_slot}")
    _wake      = asyncio.Event()
    server     = InstructionServer(queue)
    set_wake_event(_wake)

    # ── SIGTERM handler: save positions before the process dies ───────────────
    import signal as _sig
    def _on_sigterm(*_):
        try:
            _sym = active_symbol  # may not be set yet if SIGTERM arrives very early
        except NameError:
            _sym = os.environ.get("AGENT_SYMBOL", "BTCUSDT")
        if em.positions:
            _save_positions(_agent_slot, _sym, em.positions)
            log("AGENT", "SIGTERM_SAVE", slot=_agent_slot, symbol=_sym, count=len(em.positions))
        sys.exit(0)
    _sig.signal(_sig.SIGTERM, _on_sigterm)

    await server.start()
    update_state(pool_slot=_agent_slot)
    await fg.connect()

    # Initialise day_start_equity from the persistent portfolio_tracker baseline.
    # This avoids false halts from transient equity spikes on the very first REST call.
    # The portfolio_tracker file is written by reset_day_start() and survives restarts.
    import portfolio_tracker as _pt_init
    _pf_init = _pt_init.get_portfolio_state()
    _day_start = _pf_init.get("day_start", 0.0)
    if _day_start > 0:
        risk.day_start_equity   = _day_start
        risk.month_start_equity = _day_start
        log("AGENT", "DAY_START_FROM_PORTFOLIO", day_start=round(_day_start, 2))
    else:
        # Fallback: fetch live equity and use it as the baseline (original behaviour)
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _s:
                _init_sym = "BTCUSDT" if cfg.TRADING_MODE == "live" else (os.environ.get("AGENT_SYMBOL", "BTCUSDT") or "BTCUSDT")
                async with _s.get("https://api.binance.com/api/v3/ticker/price",
                                   params={"symbol": _init_sym},
                                   timeout=_aio.ClientTimeout(total=5)) as _r:
                    _init_price = float((await _r.json()).get("price", 0))
        except Exception:
            _init_price = 0.0
        _init_equity = await om.get_equity(
            symbol=os.environ.get("AGENT_SYMBOL", "") or cfg.SYMBOL,
            price=_init_price if _init_price else None,
        )
        risk.update_metrics(_init_equity)

    async def _price_pusher():
        """Push live WS ticker price to dashboard state every 1s for chart live-tick."""
        while True:
            try:
                if md._ws_ticker and md._ws_ticker.get("price", 0) > 0:
                    update_state(price=md._ws_ticker["price"])
            except Exception:
                pass
            await asyncio.sleep(1)

    # Shared cache so the main cycle can read equity without an extra REST call
    _raw_usdt_cache = [0.0]
    _base_cache     = [0.0]

    async def _equity_pusher():
        """Recompute displayed equity every 1s using live WS price + cached Binance balances.
        Balances are re-fetched from the API every 30s; valuation uses the WS tick price.
        Slot 0 also fetches Simple Earn (LD) value every 5 min and publishes to the pool."""
        _usdt           = 0.0
        _base           = 0.0
        _raw_usdt       = 0.0
        _last_fetch     = 0.0
        _last_earn_fetch = 0.0
        import time as _time
        while True:
            try:
                now   = _time.time()
                price = (md._ws_ticker or {}).get("price", 0)
                # Refresh raw balances from Binance API every 30s
                if now - _last_fetch >= 30:
                    _usdt, _base, _raw_usdt = await om.get_balances_raw(active_symbol)
                    _last_fetch  = now
                    # Publish raw USDT to the equity pool so portfolio_tracker can aggregate
                    _ep.report_usdt(_raw_usdt)
                    # Update shared cache for main cycle's equity computation
                    _raw_usdt_cache[0] = _raw_usdt
                    _base_cache[0]     = _base
                # Slot 0 fetches Simple Earn (LD) holdings every 5 min and writes to pool
                if _agent_slot == 0 and now - _last_earn_fetch >= 300:
                    try:
                        earn_val = await om.get_earn_value()
                        _ep.report_earn(earn_val)
                        _last_earn_fetch = now
                    except Exception:
                        pass
                # _usdt = USDT + all other coins priced at REST; add active base at live WS price
                if price > 0:
                    _coin_asset = active_symbol[:-4] if active_symbol.endswith("USDT") else active_symbol
                    update_state(
                        equity=round(_usdt + _base * price, 2),
                        usdt_balance=round(_raw_usdt, 2),
                        coin_qty=round(_base, 6),
                        coin_value_usdt=round(_base * price, 2),
                        coin_asset=_coin_asset,
                    )
            except Exception:
                pass
            await asyncio.sleep(1)

    # Coin selection: honour AGENT_SYMBOL env var (set by manager spawn form);
    # demo locks to it; testnet/live use it as starting point (auto-switch still applies)
    _env_symbol = os.environ.get("AGENT_SYMBOL", "").upper().strip()
    _start_sym  = _env_symbol or cfg.SYMBOL

    if cfg.USE_DEMO:
        active_symbol = _start_sym
        update_state(coin_mode=active_symbol)  # demo locks to specified coin
    elif cfg.TRADING_MODE == "live":
        if _agent_slot == 0:
            # Agent 1 is permanently locked to BTC — never rotates
            active_symbol = "BTCUSDT"
            update_state(coin_mode="BTCUSDT")
        else:
            # Multi-agent slots: use assigned symbol, auto-switch enabled
            active_symbol = _env_symbol or "BTCUSDT"
            update_state(coin_mode="auto")
    else:
        # Testnet: start on specified coin, auto-switch enabled
        active_symbol = _start_sym
        update_state(coin_mode="auto")
    md = MarketData(active_symbol, cfg.INTERVAL)
    await md.connect()
    _ep.register(_agent_slot, active_symbol, os.getpid(), cfg.INSTRUCTION_SERVER_PORT)
    asyncio.create_task(_price_pusher())
    asyncio.create_task(_equity_pusher())

    async def _background_scanner():
        """Independent background task — re-ranks all coins every 60s regardless of cycle."""
        await asyncio.sleep(10)  # let agent settle first
        while True:
            try:
                _ep_excl = _ep.get_other_symbols(_agent_slot)
                await scanner.scan(force=True, exclude=_ep_excl if _ep_excl else None)
                ranked_data = [
                    {
                        "symbol":    r["symbol"],
                        "score":     round(r.get("deep_score", 0), 3),
                        "atr_pct":   round(r.get("atr_pct", 0), 3),
                        "chg_pct":   round(r.get("chg_pct", 0), 2),
                        "vol_m":     round(r.get("vol", 0) / 1_000_000, 1),
                        "trend":     r.get("trend", "—"),
                        "regime":    r.get("regime", "—"),
                    }
                    for r in scanner.ranked[:4]
                ]
                update_state(scanner_ranked=ranked_data,
                             scanner_best=scanner.best_symbol,
                             scanner_ts=round(_time.time()))
                log("SCANNER", "BG_RANKED", best=scanner.best_symbol,
                    top3=[r["symbol"] for r in scanner.ranked[:3]])
            except asyncio.CancelledError:
                return
            except Exception as e:
                log("SCANNER", "BG_ERROR", error=str(e)[:80])
            await asyncio.sleep(60)

    asyncio.create_task(_background_scanner())

    pending_override:    str | None = None
    pending_force_coin:  str | None = None  # force-switch target (closes position first)
    volatile_since:      float      = 0.0   # when current coin first entered VOLATILE
    gate_fail_streak:    int        = 0     # consecutive quality gate failures
    none_signal_streak:  int        = 0     # consecutive NONE-signal cycles (no trade setup)
    _ranked_idx:         int        = 0     # which scanner.ranked slot we're currently on
    _pool_open_usdt:     float      = 0.0   # cached open USDT — reported to pool every cycle start
    _pool_pnl:           float      = 0.0   # cached daily pnl — reported to pool every cycle start
    _orphan_checked:     bool       = False  # one-shot: recover orphaned base-asset positions on restart
    _last_fill_time:     dict       = {}    # symbol → last fill timestamp (entry cooldown)
    VOLATILE_ESCAPE_SECS   = 600           # 10 minutes
    GATE_FAIL_SWITCH_AFTER = 2            # switch coin after 2 straight gate fails (~2 min)
    NONE_SIGNAL_ROTATE     = 5            # rotate to next ranked coin after 5 NONE cycles (~5 min)

    async def _cycle_sleep():
        """Sleep one cycle, but wake immediately if _wake event is fired."""
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=cfg.CYCLE_SLEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()

    async def _liquidate_before_switch(symbol: str) -> None:
        """Sell any remaining base asset back to USDT before rotating to a new coin.
        Never called for slot 0 (BTC-locked). Silently skips if:
          - balance is below min notional ($11)
          - coin price is below MIN_PRICE (micro-cap — don't dump into a thin market)
        """
        try:
            _tick  = await md.get_ticker()
            _price = _tick.get("price", 0)
            # Skip micro-caps: selling an underwater sub-penny coin just crystallises
            # the loss into a thin book. Leave the orphan balance — it's too small to matter.
            if _price < cfg.MIN_PRICE:
                push_log(f"[PRE_SWITCH] Skipping {symbol[:-4]} liquidation — price ${_price:.5f} below MIN_PRICE ${cfg.MIN_PRICE}")
                log("AGENT", "PRE_SWITCH_SKIP", symbol=symbol, price=_price, reason="below_min_price")
                return
            base_bal = await om.get_base_balance(symbol)
            min_qty  = 11.0 / _price if _price > 0 else 0
            if base_bal > min_qty:
                push_log(f"[PRE_SWITCH] Liquidating {base_bal:.6f} {symbol[:-4]} → USDT before rotation")
                log("AGENT", "PRE_SWITCH_SELL", symbol=symbol, qty=round(base_bal, 6))
                await om.submit("SELL", base_bal, _tick, {}, symbol=symbol)
                await asyncio.sleep(1.5)
        except Exception as _liq_e:
            log("AGENT", "PRE_SWITCH_SELL_ERROR", symbol=symbol, error=str(_liq_e))

    log("AGENT", "READY", symbol=active_symbol, testnet=cfg.USE_TESTNET, equity=round(equity, 2))
    update_state(equity=equity, symbol=active_symbol)

    # ── Staggered startup: let earlier slots register in pool before this one
    # scans for a coin, preventing all agents from picking the same top coin simultaneously.
    if _agent_slot > 0:
        _stagger_secs = _agent_slot * 12
        push_log(f"[STARTUP] Slot {_agent_slot} — waiting {_stagger_secs}s for pool to populate")
        await asyncio.sleep(_stagger_secs)

    # ── Restore positions from previous run (exact entry/stop/TP preserved) ──
    _restored = _load_positions(_agent_slot, active_symbol)
    if _restored:
        em.positions = _restored
        _p0 = _restored[0]
        push_log(f"[POS_RESTORE] Restored {len(_restored)} position(s) for {active_symbol} "
                 f"| entry={_p0.avg_entry:.5f} stop={_p0.stop:.5f} tp1={_p0.tp1:.5f} qty={_p0.qty:.6f}")

    # ── Pre-loop orphan sweep (skipped if positions were just restored) ───────
    try:
        _all_held = await om.get_all_significant_balances(min_usdt_value=5.0)
        if em.positions:
            _all_held = []  # positions already restored — skip synthetic recreation
        if _all_held:
            log("AGENT", "ORPHAN_SWEEP", coins=[c["asset"] for c in _all_held])
            for _held in _all_held:
                _asset  = _held["asset"]
                _sym    = _asset + "USDT"
                _qty    = _held["qty"]
                _price  = _held["price"]
                _val    = _held["usdt_value"]
                # Slot 0 is BTC-locked — skip switching to orphaned non-BTC coins
                if _agent_slot == 0 and _sym != "BTCUSDT":
                    push_log(f"[ORPHAN_SWEEP] Slot 0 (BTC-locked): ignoring {_qty:.4f} {_asset} (${_val:.2f}) — will be recovered by another slot")
                    continue
                # If we hold a coin that isn't the active symbol, switch to it
                if _sym != active_symbol:
                    push_log(f"[ORPHAN_SWEEP] Found {_qty:.4f} {_asset} (${_val:.2f}) — switching to {_sym}")
                    log("AGENT", "ORPHAN_SWITCH", from_=active_symbol, to=_sym,
                        asset=_asset, qty=round(_qty, 6), usdt_val=round(_val, 2))
                    await md.close()
                    active_symbol = _sym
                    md = MarketData(active_symbol, cfg.INTERVAL)
                    await md.connect()
                    update_state(symbol=active_symbol)
                push_log(f"[ORPHAN_SWEEP] {_asset}: qty={_qty:.4f}, ~${_val:.2f} — will recover on first cycle")
    except Exception as _oe:
        log("AGENT", "ORPHAN_SWEEP_ERROR", error=str(_oe))

    while True:
        try:
            # Heartbeat to equity pool using values from previous cycle (fires on every iteration)
            _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)

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
                    if em.positions:
                        try:
                            _close_price = (await md.get_ticker())["price"]
                            _close_tick  = await md.get_ticker()
                            _base_bal    = await om.get_base_balance(active_symbol)
                            if _base_bal > 0:
                                _close_ord = await om.submit("SELL", _base_bal, _close_tick,
                                                             {}, symbol=active_symbol)
                                if _close_ord:
                                    push_log(f"[CLOSE_ALL] Market sell submitted: qty={round(_base_bal,6)} @ ~{_close_price:.6f}")
                        except Exception as _ce:
                            push_log(f"[CLOSE_ALL] Sell submit error: {_ce}")
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
                        if _agent_slot != 0:
                            await _liquidate_before_switch(active_symbol)
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
                elif action == "FORCE_BTC":
                    pending_force_coin = "BTCUSDT"
                    if em.positions:
                        pending_override = "SELL"  # close position this cycle
                    push_log("[FORCE_BTC] Queued — will close position and switch to BTCUSDT (locked)")
                    log("AGENT", "FORCE_BTC_QUEUED", current=active_symbol)
                elif action == "RESUME_AUTO":
                    update_state(coin_mode="auto")
                    push_log("[RESUME_AUTO] Auto-scanner re-enabled — will switch to best coin next cycle")
                    log("AGENT", "RESUME_AUTO", symbol=active_symbol)
                elif action == "AI_ANALYST_ON":
                    analyst.toggle(True)
                    update_state(ai_analyst_enabled=True, ai_analysis={})
                    push_log("[AI_ANALYST] Advisory analyst enabled")
                elif action == "AI_ANALYST_OFF":
                    analyst.toggle(False)
                    update_state(ai_analyst_enabled=False, ai_analysis={})
                    push_log("[AI_ANALYST] Advisory analyst disabled")
            except asyncio.QueueEmpty:
                pass

            # ── Halt check ───────────────────────────────────────
            if risk.halt_active():
                log("AGENT", "HALTED", msg="Waiting for halt to lift")
                await _cycle_sleep()
                continue

            # ── Dynamic symbol selection (no open position, auto mode only) ──
            coin_mode = _state.get("coin_mode", "auto")
            if not em.positions and coin_mode == "auto" and _agent_slot != 0:
                _ep_excl = _ep.get_other_symbols(_agent_slot)
                new_symbol = await scanner.scan(exclude=_ep_excl if _ep_excl else None)
                top5 = [r["symbol"] for r in scanner.ranked[:5]]
                update_state(top_coins=top5)
                if new_symbol != active_symbol:
                    push_log(f"[SWITCH] {active_symbol} → {new_symbol} | top5={top5}")
                    log("AGENT", "SYMBOL_SWITCH", from_=active_symbol, to=new_symbol, top5=top5)
                    asyncio.create_task(asyncio.to_thread(
                        _email.notify_rotation, _agent_name, active_symbol, new_symbol, "scanner pick"))
                    await _liquidate_before_switch(active_symbol)
                    await md.close()
                    active_symbol = new_symbol
                    md = MarketData(active_symbol, cfg.INTERVAL)
                    await md.connect()
                    _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                    update_state(symbol=active_symbol, top_coins=top5)
                else:
                    push_log(f"[SCAN] staying on {active_symbol} | top5={top5}")
            elif not em.positions:
                push_log(f"[COIN_SELECT] Manual mode — holding {active_symbol}")

            # ── Module 1: Market Data ─────────────────────────────
            try:
                tick       = await md.get_ticker()
                book       = await md.get_orderbook()
                candles    = await md.get_klines()
                indicators = md.compute_indicators(candles)
                current_price = tick["price"]
            except Exception as e:
                log("AGENT", "DATA_ERROR", symbol=active_symbol, error=str(e))
                await _cycle_sleep()
                continue

            if not md.quality_gate(tick, book):
                gate_fail_streak += 1
                push_log(f"[QUALITY_GATE] {active_symbol} — gate fail #{gate_fail_streak}, sitting out")
                if gate_fail_streak >= GATE_FAIL_SWITCH_AFTER and not em.positions and _agent_slot != 0:
                    coin_mode = _state.get("coin_mode", "auto")
                    if coin_mode == "auto":
                        push_log(f"[QUALITY_GATE] {gate_fail_streak} straight fails — forcing coin switch")
                        log("AGENT", "GATE_FAIL_SWITCH", symbol=active_symbol, streak=gate_fail_streak)
                        new_symbol = await scanner.scan(exclude={active_symbol}, force=True)
                        if new_symbol != active_symbol:
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_rotation, _agent_name, active_symbol, new_symbol, "quality gate escape"))
                            await _liquidate_before_switch(active_symbol)
                            await md.close()
                            active_symbol = new_symbol
                            md = MarketData(active_symbol, cfg.INTERVAL)
                            await md.connect()
                            _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                            update_state(symbol=active_symbol)
                            push_log(f"[SWITCH] → {active_symbol} (quality gate escape)")
                    else:
                        push_log(f"[QUALITY_GATE] {gate_fail_streak} straight fails — manual mode, holding {active_symbol}")
                    gate_fail_streak = 0
                await _cycle_sleep()
                continue
            gate_fail_streak = 0  # reset on pass

            # ── Orphan recovery: reconstruct position from existing balance ──
            if not _orphan_checked:
                _orphan_checked = True
                _fu, _bb, _ = await om.get_balances_raw(active_symbol)
                _min_notional = 10.0 / current_price
                if _bb > _min_notional and not em.positions:
                    _atr      = indicators["atr14"]
                    _h1r      = indicators.get("h1_range", 0)
                    _tp1_dist = _h1r * 0.40 if _h1r > 0 else _atr * cfg.ATR_STOP_MULT * cfg.TP1_R
                    _tp2_dist = _h1r * 0.80 if _h1r > 0 else _atr * cfg.ATR_STOP_MULT * cfg.TP2_R
                    _tp3_dist = _h1r * 1.25 if _h1r > 0 else _atr * cfg.ATR_STOP_MULT * cfg.TP3_R
                    _sp  = Position(
                        side="BUY", avg_entry=current_price, qty=_bb,
                        stop=round(current_price - _atr * cfg.ATR_STOP_MULT, 2),
                        tp1=round(current_price + _tp1_dist, 2),
                        tp2=round(current_price + _tp2_dist, 2),
                        tp3=round(current_price + _tp3_dist, 2),
                        initial_risk=round(_bb * _atr * cfg.ATR_STOP_MULT, 4),
                        symbol=active_symbol,
                    )
                    em.positions.append(_sp)
                    push_log(f"[RECOVERY] Orphaned {round(_bb,6)} {active_symbol[:-4]} detected "
                             f"— synthetic position created at {current_price:.2f} "
                             f"| stop={_sp.stop} | tp1={_sp.tp1}")
                    log("AGENT", "ORPHAN_RECOVERY", symbol=active_symbol,
                        qty=round(_bb, 6), price=current_price, stop=_sp.stop, tp1=_sp.tp1)

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
                         symbol=active_symbol, price=current_price)
            push_log(f"[CYCLE] {active_symbol} | price={current_price:.4f} | regime={regime} "
                     f"| RSI={indicators['rsi14']:.1f} | EMA9={indicators['ema9']:.4f}")

            # ── AI Analyst (advisory only — no trading impact) ────
            if analyst.enabled:
                _pos_info = None
                if em.positions:
                    _p = em.positions[0]
                    _pos_info = {"side": _p.side, "avg_entry": _p.avg_entry,
                                 "stop": _p.stop, "tp1": _p.tp1,
                                 "pnl_pct": (_p.avg_entry and (current_price - _p.avg_entry) / _p.avg_entry * 100)}
                asyncio.create_task(
                    _run_analyst(analyst, active_symbol, current_price, indicators,
                                 candles, regime, fear_greed, _pos_info)
                )

            # ── RANGING: immediately rotate to the next best ranked coin ─────
            if regime == "RANGING" and not em.positions and _agent_slot != 0:
                coin_mode = _state.get("coin_mode", "auto")
                # A named coin_mode (e.g. "BTCUSDT") means "start here" not "stay forever".
                # When that coin is ranging with no scanner data yet, sit out.
                # When scanner has better candidates, escape to auto and rotate.
                _locked_coin = coin_mode if coin_mode not in ("auto", None) else None
                _can_escape  = coin_mode == "auto" or (
                    _locked_coin and scanner.ranked and
                    scanner.ranked[0]["symbol"] != active_symbol
                )
                if _can_escape and scanner.ranked:
                    _pool_exclude = _ep.get_other_symbols(_agent_slot)
                    for _ in range(len(scanner.ranked)):
                        _ranked_idx = (_ranked_idx + 1) % len(scanner.ranked)
                        next_sym    = scanner.ranked[_ranked_idx]["symbol"]
                        if next_sym not in _pool_exclude:
                            break
                    else:
                        next_sym = active_symbol  # all taken, stay put
                    if next_sym != active_symbol:
                        push_log(f"[RANGING_ESCAPE] {active_symbol} is RANGING — rotating to {next_sym}")
                        log("AGENT", "RANGING_ESCAPE", from_=active_symbol, to=next_sym,
                            idx=_ranked_idx)
                        asyncio.create_task(asyncio.to_thread(
                            _email.notify_rotation, _agent_name, active_symbol, next_sym, "ranging escape"))
                        await _liquidate_before_switch(active_symbol)
                        await md.close()
                        active_symbol      = next_sym
                        md                 = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                        # Unlock coin_mode to auto so scanner drives future rotation
                        update_state(symbol=active_symbol, coin_mode="auto")
                        none_signal_streak = 0
                    else:
                        push_log(f"[RANGING_ESCAPE] {active_symbol} RANGING — no better coin available, sitting out")
                else:
                    push_log(f"[RANGING] {active_symbol} is RANGING — sitting out (no scanner data yet)")
                await _cycle_sleep()
                continue

            # ── Volatile escape: switch coin after 30 min of VOLATILE ─
            if regime == "VOLATILE" and _agent_slot != 0:
                if volatile_since == 0.0:
                    volatile_since = _time.time()
                elapsed = _time.time() - volatile_since
                if elapsed >= VOLATILE_ESCAPE_SECS:
                    mins = int(elapsed // 60)
                    coin_mode = _state.get("coin_mode", "auto")
                    if coin_mode == "auto":
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
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_rotation, _agent_name, active_symbol, new_symbol, "volatile escape"))
                            await _liquidate_before_switch(active_symbol)
                            await md.close()
                            active_symbol = new_symbol
                            md = MarketData(active_symbol, cfg.INTERVAL)
                            await md.connect()
                            _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                            update_state(symbol=active_symbol)
                    else:
                        push_log(f"[VOLATILE_ESCAPE] {active_symbol} volatile for {mins}m — manual mode, holding")
                    volatile_since = 0.0
                    await _cycle_sleep()
                    continue
            else:
                volatile_since = 0.0  # reset counter when regime clears

            # ── Module 5: Manage open positions ───────────────────
            exit_actions = em.manage_open_positions(current_price, indicators)
            for act in exit_actions:
                push_log(f"[EXIT] {act} @ {current_price:.4f}")
                if act.startswith("CLOSE:"):
                    # Full close — sell entire base asset balance on the exchange
                    base_bal = await om.get_base_balance(active_symbol)
                    if base_bal > 0:
                        close_order = await om.submit("SELL", base_bal, tick, indicators,
                                                      symbol=active_symbol)
                        if close_order:
                            push_log(f"[EXIT_EXECUTED] SELL {active_symbol} qty={round(base_bal,6)} @ ~{current_price:.4f}")
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_fill, _agent_name, active_symbol, "SELL", base_bal, current_price))
                elif act.startswith("PARTIAL_CLOSE:BUY:TP1"):
                    # Sell TP1_PCT of the position
                    base_bal = await om.get_base_balance(active_symbol)
                    sell_qty = base_bal * cfg.TP1_PCT / (1 - cfg.TP1_PCT + cfg.TP1_PCT)
                    if sell_qty > 0:
                        tp_order = await om.submit("SELL", sell_qty, tick, indicators,
                                                   symbol=active_symbol)
                        if tp_order:
                            push_log(f"[TP1_EXECUTED] SELL {active_symbol} qty={round(sell_qty,6)} @ ~{current_price:.4f}")
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_fill, _agent_name, active_symbol, "SELL (TP1)", sell_qty, current_price))
                elif act.startswith("PARTIAL_CLOSE:BUY:TP2"):
                    base_bal = await om.get_base_balance(active_symbol)
                    sell_qty = base_bal * cfg.TP2_PCT / (1 - cfg.TP2_PCT + cfg.TP2_PCT)
                    if sell_qty > 0:
                        tp_order = await om.submit("SELL", sell_qty, tick, indicators,
                                                   symbol=active_symbol)
                        if tp_order:
                            push_log(f"[TP2_EXECUTED] SELL {active_symbol} qty={round(sell_qty,6)} @ ~{current_price:.4f}")
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_fill, _agent_name, active_symbol, "SELL (TP2)", sell_qty, current_price))

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
            # SELL with no open position is spot-untradeable — treat like NONE for rotation.
            # Without this, a bear-trending coin produces endless SELL signals and the agent
            # never rotates, since none_signal_streak stays at 0.
            _actionable = signal == "BUY" or (signal == "SELL" and em.positions)
            # SELL with no position = coin is falling and we can't short; rotate immediately
            _sell_no_pos = signal == "SELL" and not em.positions
            if _actionable:
                push_log(f"[SIGNAL] {signal} on {active_symbol} | regime={regime}")
                none_signal_streak = 0
                _ranked_idx        = 0
            else:
                none_signal_streak += 1
                coin_mode  = _state.get("coin_mode", "auto")
                _can_rotate = coin_mode == "auto" or (
                    coin_mode not in ("auto", None) and scanner.ranked and
                    scanner.ranked[0]["symbol"] != active_symbol
                )
                # Rotate immediately on SELL-no-pos; otherwise wait NONE_SIGNAL_ROTATE cycles
                _rotate_threshold = 1 if _sell_no_pos else NONE_SIGNAL_ROTATE
                if _can_rotate and none_signal_streak >= _rotate_threshold and not em.positions and scanner.ranked and _agent_slot != 0:
                    _pool_exclude = _ep.get_other_symbols(_agent_slot)
                    for _ in range(len(scanner.ranked)):
                        _ranked_idx = (_ranked_idx + 1) % len(scanner.ranked)
                        next_sym    = scanner.ranked[_ranked_idx]["symbol"]
                        if next_sym not in _pool_exclude:
                            break
                    else:
                        next_sym = active_symbol  # all taken, stay put
                    if next_sym != active_symbol:
                        _rotate_reason = "SELL with no position" if _sell_no_pos else f"no setup for {none_signal_streak} cycles"
                        push_log(f"[ROTATE] {active_symbol}: {_rotate_reason} — switching to {next_sym}")
                        log("AGENT", "SIGNAL_ROTATE", from_=active_symbol, to=next_sym,
                            streak=none_signal_streak, idx=_ranked_idx)
                        asyncio.create_task(asyncio.to_thread(
                            _email.notify_rotation, _agent_name, active_symbol, next_sym, _rotate_reason))
                        await _liquidate_before_switch(active_symbol)
                        await md.close()
                        active_symbol      = next_sym
                        md                 = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                        update_state(symbol=active_symbol, coin_mode="auto")
                        none_signal_streak = 0
                    await _cycle_sleep()
                    continue

            # Manual SELL override — close the open position immediately
            if signal == "SELL" and em.positions:
                base_bal = await om.get_base_balance(active_symbol)
                if base_bal > 0:
                    close_order = await om.submit("SELL", base_bal, tick, indicators,
                                                  symbol=active_symbol)
                    if close_order:
                        em.positions.clear()
                        push_log(f"[MANUAL_SELL] Closed {active_symbol} qty={round(base_bal,6)} @ ~{current_price:.4f}")
                        log("AGENT", "MANUAL_SELL_EXECUTED", symbol=active_symbol,
                            qty=round(base_bal, 6), price=current_price)
                        asyncio.create_task(asyncio.to_thread(
                            _email.notify_fill, _agent_name, active_symbol, "SELL", base_bal, current_price))
                else:
                    push_log(f"[MANUAL_SELL] No {active_symbol[:-4]} balance to sell")
                    em.positions.clear()

            # Force-switch after position is closed (e.g. FORCE_BTC)
            if pending_force_coin and not em.positions:
                target = pending_force_coin
                pending_force_coin = None
                if target != active_symbol:
                    push_log(f"[FORCE_SWITCH] {active_symbol} → {target}")
                    log("AGENT", "FORCE_SWITCH", from_=active_symbol, to=target)
                    if _agent_slot != 0:
                        await _liquidate_before_switch(active_symbol)
                    await md.close()
                    active_symbol = target
                    md = MarketData(active_symbol, cfg.INTERVAL)
                    await md.connect()
                    _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                    update_state(symbol=active_symbol, coin_mode=active_symbol)  # lock to target; use RESUME_AUTO to release
                    none_signal_streak = 0
                await _cycle_sleep()
                continue

            # Re-check risk state immediately after any position close in this cycle.
            # Prevents entering a new trade in the same cycle a stop-loss was hit — the daily
            # DD limit may have just been breached (as happened: FIDA stop → AIGENSYN entry).
            if not em.positions:
                _post_close_equity = await om.get_equity(symbol=active_symbol, price=current_price)
                risk.update_metrics(_post_close_equity)
                if risk.halt_active():
                    update_state(halt=True)
                    push_log(f"[HALT] Daily DD limit reached after exit — no new entries this session")
                    await _cycle_sleep()
                    continue

            # Spot mode: only BUY can open a new position (no naked short selling)
            if signal == "BUY" and not em.positions:
                import time as _t
                _now = _t.time()
                ENTRY_COOLDOWN = 30 * 60  # 30 min no re-entry same symbol after a fill
                _last_fill = _last_fill_time.get(active_symbol, 0)
                if _now - _last_fill < ENTRY_COOLDOWN:
                    _wait = int(ENTRY_COOLDOWN - (_now - _last_fill)) // 60
                    push_log(f"[COOLDOWN] {active_symbol} — {_wait}m left before re-entry allowed")
                    signal = "NONE"

            if signal == "BUY" and not em.positions:
                usdt_equiv, base_bal, raw_usdt = await om.get_balances_raw(active_symbol)
                equity = usdt_equiv + base_bal * current_price  # full equity (includes BTC)
                stop_d = indicators["atr14"] * cfg.ATR_STOP_MULT
                tp1_dist = stop_d * cfg.TP1_R  # TP1 distance from entry
                min_tp_dist = current_price * 0.0022  # 0.22% = round-trip fees (2×0.1%) + 0.02% margin
                if tp1_dist < min_tp_dist:
                    push_log(f"[SKIP] {active_symbol} TP1 too close ({tp1_dist/current_price*100:.3f}%) — won't cover fees")
                    signal = "NONE"
                # ATR cap: reject extremely volatile coins — the same volatility that scores them
                # high will blow through stops on normal noise (FIDA: 4.1% ATR → stop hit -11%)
                MAX_ENTRY_ATR_PCT = 3.0
                atr_pct_live = indicators["atr14"] / current_price * 100
                if signal == "BUY" and atr_pct_live > MAX_ENTRY_ATR_PCT:
                    push_log(f"[SKIP] {active_symbol} ATR {atr_pct_live:.2f}% > {MAX_ENTRY_ATR_PCT}% cap — too volatile to enter safely")
                    signal = "NONE"

            if signal == "BUY" and not em.positions:
                usdt_equiv, base_bal, raw_usdt = await om.get_balances_raw(active_symbol)
                equity = usdt_equiv + base_bal * current_price  # full equity (includes BTC)
                stop_d      = indicators["atr14"] * cfg.ATR_STOP_MULT
                pool_budget = _ep.get_budget(_agent_slot, equity)
                qty    = PositionSizer.calculate(equity, current_price, stop_d, size_mult,
                                                 usdt_available=raw_usdt,
                                                 pool_budget=pool_budget)

                if qty:
                    order = await om.submit("BUY", qty, tick, indicators,
                                            symbol=active_symbol)

                    if order:
                        import time as _t
                        _last_fill_time[active_symbol] = _t.time()
                        push_log(f"[TRADE] BUY {active_symbol} qty={round(qty,5)} @ ~{current_price:.4f}")
                        asyncio.create_task(asyncio.to_thread(
                            _email.notify_fill, _agent_name, active_symbol, "BUY", qty, current_price))
                        pos = em.attach_exits(order, indicators, symbol=active_symbol)
                        if pos:
                            push_transaction({
                                "side":   "BUY",
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
            # Use background-task cached values — avoids a duplicate REST call per cycle.
            # _state["equity"] is kept current by _equity_pusher every second.
            equity = _state.get("equity") or (_raw_usdt_cache[0] + _base_cache[0] * current_price)
            risk.update_metrics(equity)
            import portfolio_tracker as _pt
            _pf = _pt.get_portfolio_state()
            update_state(
                equity=equity,
                daily_dd=((risk.day_start_equity - equity) / risk.day_start_equity * 100)
                          if risk.day_start_equity else 0,
                halt=risk.halt_flag,
                portfolio=_pf,
            )

            # ── Position heartbeat (displayed every cycle in live log) ────
            if em.positions:
                pos = em.positions[0]
                pnl_pct = (current_price - pos.avg_entry) / pos.avg_entry * 100
                update_open_pos({
                    "side":      pos.side,
                    "avg_entry": pos.avg_entry,
                    "stop":      pos.stop,
                    "tp1":       pos.tp1,
                    "tp2":       pos.tp2,
                    "qty":       pos.qty,
                })
                update_state(positions=len(em.positions))
                push_log(
                    f"[STATUS] {active_symbol} | price={current_price:.2f} | entry={pos.avg_entry:.2f} "
                    f"| pnl={pnl_pct:+.2f}% | stop={pos.stop:.2f} | tp1={pos.tp1:.2f} "
                    f"| equity={round(equity,2)} | halt={risk.halt_flag}"
                )
            else:
                update_open_pos(None)
                update_state(positions=0)
                push_log(
                    f"[STATUS] {active_symbol} | price={current_price:.2f} | no open position "
                    f"| signal={signal} | regime={regime} | equity={round(equity,2)}"
                )

            # Cache pool values for next cycle's heartbeat at top of loop
            _pool_open_usdt = sum(p.qty * current_price for p in em.positions)
            # slot_pnl = unrealized P&L on THIS slot's open coin position only
            _pool_pnl = sum(p.qty * current_price - p.qty * p.avg_entry for p in em.positions)

            # ── Persist positions after every cycle ───────────────────────────
            if em.positions:
                _save_positions(_agent_slot, active_symbol, em.positions)
            else:
                _clear_positions_file(_agent_slot)

            await _cycle_sleep()

        except KeyboardInterrupt:
            if em.positions:
                _save_positions(_agent_slot, active_symbol, em.positions)
            log("AGENT", "SHUTDOWN", reason="KeyboardInterrupt")
            await om.cancel_all()
            await md.close()
            await om.close()
            await scanner.close()
            await fg.close()
            _ep.deregister(_agent_slot)
            sys.exit(0)
        except Exception as e:
            import traceback as _tb
            log("AGENT", "LOOP_ERROR", error=str(e), trace=_tb.format_exc()[-300:])
            await _cycle_sleep()


if __name__ == "__main__":
    asyncio.run(main_loop())
