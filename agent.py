"""
Raptor FleetEdge — Autonomous Crypto Trading Agent
20 years experience | High-risk / High-reward mindset
Binance Spot & USDT-M Futures

Instruction sources: authorized external agents/bots via InstructionServer.
The Binance login account is execution-only — it is NOT an instruction source.

Dynamic symbol selection: Raptor FleetEdge scans all USDT pairs every 15 minutes and
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
import parked_coins as _pc
import ema_cross_module       as _ema_cross
import rsi_reversal_module    as _rsi_rev
import macd_cross_module      as _macd_cross
import bb_breakout_module     as _bb_break
import strategy_advisor_module as _advisor
from rule_analyst import RuleAnalyst
from rule_coin_selector import RuleCoinSelector
from nn_predictor import PricePredictor, OOS_MIN_ACC, OOS_MIN_SAMP

# ── Strategy registry ─────────────────────────────────────────────────────
STRATEGY_LABELS: dict[str, str] = {
    "ema_cross":    "EMA Cross · Crossover",
    "momentum":     "Volatility Chase · Momentum",
    "rsi_reversal": "RSI · Mean Reversion",
    "macd_cross":   "MACD · Crossover",
    "bb_breakout":  "Bollinger · Band Bounce",
}
VALID_STRATEGIES = set(STRATEGY_LABELS.keys())
# Strategies that skip RANGING regime (trend-following only)
RANGING_SKIP_STRATEGIES = {"momentum", "macd_cross"}


# ── Position persistence ──────────────────────────────────────────────────
def _pos_file(slot: int) -> str:
    return f"/tmp/rfe_positions_{slot}.json"


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
        # Discard if the file is older than 24 h — positions that old are stale
        age_hours = (_time.time() - os.path.getmtime(path)) / 3600
        if age_hours > 24:
            os.unlink(path)
            log("AGENT", "POS_RESTORE_SKIP", reason="stale_file",
                age_hours=round(age_hours, 1), slot=slot)
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


RAPTOR_PERSONA = """
I am Raptor FleetEdge. Twenty years in crypto — lived through Mt. Gox, the ICO bubble,
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

    # Candle body filter: use the PREVIOUS COMPLETED candle, not the live one.
    # The live candle body changes every tick (briefly negative even in clear up-moves)
    # and produces constant false NONEs.  The previous closed candle gives a stable,
    # committed momentum read that matches what a trader sees on the chart.
    atr14         = ind.get("atr14", 0)
    atr_pct       = (atr14 / price * 100) if price and atr14 else 0
    body_threshold = max(0.03, atr_pct * 0.20)  # 20% of ATR%: slightly looser floor
    prev_close    = ind.get("prev_close", price)
    prev_open     = ind.get("prev_open", open_price)
    candle_body_pct = (prev_close - prev_open) / prev_open * 100 if prev_open else 0
    candle_bull = candle_body_pct > body_threshold
    candle_bear = candle_body_pct < -body_threshold
    # Not-strongly-bear: allows entry on a flat/small-red candle when trend is confirmed
    # (avoids blocking legitimate continuation entries on minor retracement candles)
    candle_not_bear = candle_body_pct > -body_threshold

    # Primary momentum entry: EMA stack + MACD + price above/below EMA21
    # candle_not_bear (instead of candle_bull) allows entry on small-bodied or flat candles
    # within a confirmed trend — only hard bearish candles are filtered out.
    long_momentum  = ema_bull and rsi < 70 and macd > msig and price > e21 and candle_not_bear
    short_momentum = ema_bear and rsi > 30 and macd < msig and price < e21 and candle_bull

    # Continuation entry: deep pullback in confirmed bull trend only
    # Disabled in bear — counter-trend pullbacks fail too quickly in downtrends
    long_pullback  = ema_bull and rsi < 35 and price > e50
    short_pullback = ema_bear and rsi > 65 and price < e50

    # Extreme oversold BUY at lower BB support (bull trend only)
    long_bb = price <= bb_lo * 1.003 and rsi < 32 and ema_bull

    # Mean-reversion BUY: deeply oversold in bear trend — catch the bounce
    # RSI < 28: rare but catches genuine capitulation bottoms.
    # H-6: require above-average volume to confirm the flush (avoids falling-knife entries
    # on thin-volume drift; genuine capitulation bottoms spike volume as weak hands sell).
    _vol_ratio = ind.get("volume_ratio", 1.0)
    bear_reversal = ema_bear and rsi < 28 and price <= bb_lo * 1.005 and _vol_ratio >= 1.3

    # Trend-transition BUY: bear stack but MACD has gone bullish and price reclaimed EMA50.
    # Captures early reversals where EMA stack lags the actual price recovery.
    # MACD > signal confirms momentum shift; price > EMA50 confirms structure recovery.
    # RSI window widened to 38-72: catches more genuine reversals without being too loose.
    transition_long = (ema_bear and macd > msig and price > e50
                       and 38 < rsi < 72 and candle_bull)

    # In a bear trend: allow bear_reversal and transition_long
    if ema_bear:
        long_signal = bear_reversal or transition_long
    else:
        long_signal = long_momentum or long_pullback or long_bb

    short_signal = short_momentum or short_pullback

    log("SIGNAL", "EVAL",
        ema_bull=ema_bull, ema_bear=ema_bear,
        rsi=round(rsi, 1), macd_vs_sig=round(macd - msig, 6),
        prev_candle_body=round(candle_body_pct, 3), body_thr=round(body_threshold, 3),
        candle_bull=candle_bull, candle_not_bear=candle_not_bear,
        long_momentum=long_momentum, long_pullback=long_pullback,
        long_bb=long_bb, bear_rev=bear_reversal, transition=transition_long,
        result="BUY" if long_signal else ("SELL" if short_signal else "NONE"))

    if long_signal:  return "BUY"
    if short_signal: return "SELL"
    return "NONE"


async def _run_analyst(analyst, symbol, price, indicators, candles, regime, fear_greed, position):
    """Fire-and-forget wrapper: runs rule analyst and pushes result to dashboard state."""
    try:
        result = await analyst.maybe_run(symbol, price, indicators, candles,
                                         regime, fear_greed, position, interval_secs=180)
        if result:
            update_state(analysis=result)
    except Exception as e:
        log("ANALYST", "TASK_ERROR", error=str(e)[:80])


async def main_loop():
    log("AGENT", "STARTUP", persona=RAPTOR_PERSONA.strip())

    scanner  = MarketScanner()
    risk     = RiskEngine()
    om       = OrderManager()
    em       = ExitManager()
    rc       = RegimeClassifier()
    fg       = FearGreedClient()
    _agent_slot = int(os.environ.get("AGENT_SLOT", "0"))
    _agent_name = os.environ.get("AGENT_NAME", f"fleetedge{_agent_slot + 1}")
    _strategy      = os.environ.get("AGENT_STRATEGY", "momentum").lower()
    _strategies: list = [_strategy]    # multi-strategy list; [0] = primary driver
    _risk_pct      = cfg.RISK_PCT       # overridable per-agent risk fraction
    _max_trade_pct = cfg.MAX_TRADE_PCT  # overridable per-agent max-trade fraction
    analyst   = RuleAnalyst()
    selector  = RuleCoinSelector()
    # NM-3: predictor is only used by momentum — skip heavy numpy weight init for all other strategies
    predictor = PricePredictor() if _strategy == "momentum" else None
    queue      = asyncio.Queue()
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
    # Subtract earn_value so the baseline is spot-consistent with per-cycle equity
    # (earn is not included in the per-cycle equity passed to update_metrics).
    # This avoids false halts from transient REST spikes on first startup call.
    import portfolio_tracker as _pt_init
    _pf_init = _pt_init.get_portfolio_state()
    _port_day_start = _pf_init.get("day_start", 0.0)
    _earn_val       = _pf_init.get("earn_value", 0.0)
    _spot_day_start = _port_day_start - _earn_val  # strip earn so it matches per-cycle equity
    if _spot_day_start > 0:
        risk.day_start_equity   = _spot_day_start
        risk.month_start_equity = _spot_day_start
        log("AGENT", "DAY_START_FROM_PORTFOLIO",
            day_start=round(_spot_day_start, 2), earn_stripped=round(_earn_val, 2))
    else:
        # Fallback: fetch live equity on startup (used if no portfolio day file exists yet)
        try:
            _init_equity = await om.get_balances_raw(cfg.SYMBOL)
            risk.update_metrics(_init_equity[0])  # index 0 = non-base usdt equiv
        except Exception as _bal_err:
            log("AGENT", "BALANCE_FETCH_FAILED",
                error=str(_bal_err)[:200],
                hint="Check API keys in .env — agent will start but cannot trade")
    equity = risk.day_start_equity or 0.0  # ensure equity var is defined for READY log below

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

    if _strategy == "ema_cross":
        # EMA cross: lock permanently to assigned coin, never rotates.
        # NC-4: default ETHUSDT (not BTCUSDT) to avoid double BTC exposure with slot 0
        active_symbol = _env_symbol or "ETHUSDT"
        update_state(coin_mode=active_symbol)
    elif cfg.USE_DEMO:
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
        """Independent background task — re-ranks all coins every SCAN_INTERVAL seconds.
        L-3: sleep 270s (just under the 300s prompt-cache TTL) instead of 60s to avoid
        firing 5× redundant scans within one SCAN_INTERVAL and burning API rate limit."""
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
            await asyncio.sleep(270)

    # FIX-13: EMA cross is locked to one coin and never rotates — scanner wastes API rate limit
    if _strategy != "ema_cross":
        asyncio.create_task(_background_scanner())


    _prev_indicators:    dict | None = None  # EMA cross: previous cycle's indicators
    pending_override:    str | None = None
    pending_force_coin:  str | None = None  # force-switch target (closes position first)
    volatile_since:      float      = 0.0   # when current coin first entered VOLATILE
    gate_fail_streak:    int        = 0     # consecutive quality gate failures
    none_signal_streak:  int        = 0     # consecutive NONE-signal cycles (no trade setup)
    _pool_open_usdt:     float      = 0.0   # cached open USDT — reported to pool every cycle start
    _pool_pnl:           float      = 0.0   # cached daily pnl — reported to pool every cycle start
    _orphan_checked:     bool       = False  # one-shot: recover orphaned base-asset positions on restart
    _last_fill_time:     dict       = {}    # symbol → last fill timestamp (entry cooldown)
    _nn_cycle_count:     int        = 0     # incremented each cycle; triggers NN retrain at interval
    VOLATILE_ESCAPE_SECS   = 600           # 10 minutes
    GATE_FAIL_SWITCH_AFTER = 2            # switch coin after 2 straight gate fails (~2 min)
    NONE_SIGNAL_ROTATE     = 6            # rotate to next ranked coin after 6 NONE cycles (~6 min, was 3 — excessive churn)

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
    # FIX-12: persist session start equity across restarts within the same calendar day
    import datetime as _dt
    _session_eq_file = f"/tmp/rfe_session_start_{_agent_slot}_{_dt.date.today()}.json"
    _session_start_equity = equity
    try:
        if os.path.exists(_session_eq_file):
            _ses_data = json.load(open(_session_eq_file))
            if _ses_data.get("equity", 0) > 0:
                _session_start_equity = float(_ses_data["equity"])
    except Exception:
        pass
    try:
        json.dump({"equity": _session_start_equity}, open(_session_eq_file, "w"))
    except Exception:
        pass
    # NH-3: clean up session equity files from previous calendar days
    try:
        import glob as _glob
        _today_str = str(_dt.date.today())
        for _old_f in _glob.glob(f"/tmp/rfe_session_start_{_agent_slot}_*.json"):
            if _today_str not in _old_f:
                os.remove(_old_f)
    except Exception:
        pass
    _session_realized_pnl  = 0.0           # sum of all closed-trade P&L this session
    _session_start_ts      = _time.time()
    # FIX-14: track session open price for a more accurate intraday BTC range guard
    _session_open_price: float = 0.0
    _session_high:       float = 0.0
    _session_low:        float = float("inf")
    _ai_sel_triggered      = False          # one-shot: fired when session P&L ≤ 0, resets on recovery
    _ai_sel_pending_ts     = 0.0           # stagger: slot N waits N×20s before firing
    _strategy_label = STRATEGY_LABELS.get(_strategy, _strategy)
    update_state(equity=equity, symbol=active_symbol,
                 session_start_equity=round(_session_start_equity, 2),
                 session_start_ts=_session_start_ts,
                 session_pnl=0.0, session_pnl_pct=0.0,
                 strategy=_strategy_label, strategy_key=_strategy,
                 strategy_keys=_strategies,
                 risk_pct=_risk_pct, max_trade_pct=_max_trade_pct)

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
                # Non-slot-0 must never take over BTCUSDT — sell the holding and let slot 0 own BTC
                if _agent_slot != 0 and _sym == "BTCUSDT":
                    push_log(f"[ORPHAN_SWEEP] Slot {_agent_slot}: found BTC (${_val:.2f}) — selling (slot 0 owns BTC)")
                    log("AGENT", "ORPHAN_BTC_SELL", slot=_agent_slot, qty=round(_qty, 6), usdt_val=round(_val, 2))
                    try:
                        # C-2: build a proper tick dict from _held["price"] — get_lot_step returns
                        # a float (step size), not a tick dict, which would cause TypeError in submit()
                        _tick_btc = {"price": _held["price"], "spread_pct": 0}
                        await om.submit("SELL", _qty, _tick_btc, {}, symbol="BTCUSDT")
                    except Exception as _btc_sell_e:
                        log("AGENT", "ORPHAN_BTC_SELL_ERROR", error=str(_btc_sell_e)[:80])
                    continue
                # Skip coins already being managed by another slot — those coins belong to them
                _pool_managed = _ep.get_other_symbols(_agent_slot)
                if _sym in _pool_managed:
                    push_log(f"[ORPHAN_SWEEP] Slot {_agent_slot}: skipping {_asset} (${_val:.2f}) — managed by another slot")
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
            # EMA cross: snapshot prev indicators at loop top before any continue can fire
            _ema_prev        = _prev_indicators
            _prev_indicators = None  # will be set after indicators are computed

            # Heartbeat to equity pool using values from previous cycle (fires on every iteration)
            _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)

            # ── External instruction ──────────────────────────────
            try:
                instr = queue.get_nowait()
                action = instr["action"]
                if action == "RESUME":
                    # H-3: block RESUME from clearing a monthly drawdown halt — that requires
                    # reviewing the loss and using RESET_DAY_START first.
                    if risk.is_monthly_halt():
                        push_log("[INSTRUCTION] RESUME rejected — monthly drawdown halt active. "
                                 "Use RESET_DAY_START after reviewing losses, then RESUME.")
                        log("AGENT", "RESUME_REJECTED_MONTHLY", reason=risk.halt_reason)
                    else:
                        _prev_reason       = risk.halt_reason or "operator halt"
                        risk.halt_flag     = False
                        risk.halt_until    = 0
                        risk.halt_reason   = ""
                        risk.consec_losses = 0
                        push_log(f"[INSTRUCTION] RESUME — trading resumed (was halted: {_prev_reason})")
                        log("AGENT", "RESUMED", source=instr.get("source",""), prev_reason=_prev_reason)
                        update_state(halt=False)
                elif action == "HALT":
                    await risk.emergency_halt(om, "operator_halt", equity, symbol=active_symbol)
                    update_state(halt=True)
                elif action == "CLOSE_ALL":
                    await om.cancel_all(symbol=active_symbol)
                    if em.positions:
                        try:
                            _close_price = (await md.get_ticker())["price"]
                            _close_tick  = await md.get_ticker()
                            _base_bal    = await om.get_base_balance(active_symbol)
                            if _base_bal > 0:
                                _close_ord = await om.submit("SELL", _base_bal, _close_tick,
                                                             {}, symbol=active_symbol)
                                if _close_ord:
                                    # NC-3: check fill before clearing — partial fill leaves orphaned coin
                                    _ca_status = _close_ord.get("status", "FILLED")
                                    if _ca_status == "PARTIALLY_FILLED":
                                        _ca_exec = float(_close_ord.get("executedQty", 0))
                                        push_log(f"[CLOSE_ALL] ⚠ PARTIAL fill: {_ca_exec:.6f}/{_base_bal:.6f} sold — remainder unmanaged until next restart")
                                    else:
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
                        # C-5: reload API credentials for the new mode from env
                        _key_map = {
                            "testnet": ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
                            "demo":    ("BINANCE_DEMO_API_KEY",    "BINANCE_DEMO_API_SECRET"),
                            "live":    ("BINANCE_LIVE_API_KEY",    "BINANCE_LIVE_API_SECRET"),
                        }
                        _k, _s = _key_map[new_mode]
                        cfg.BINANCE_API_KEY    = os.environ.get(_k, "")
                        cfg.BINANCE_API_SECRET = os.environ.get(_s, "")
                        await om.reset_session()
                        update_state(trading_mode=new_mode)
                        push_log(f"[MODE_SWITCH] → {new_mode.upper()} | {ep[0]}")
                        log("AGENT", "MODE_SWITCH", mode=new_mode, url=ep[0])
                        await om.cancel_all(symbol=active_symbol)
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
                        if risk.clear_if_consec_loss(active_symbol):
                            push_log(f"[COIN_SELECT] Consecutive-loss halt cleared — fresh start on {active_symbol}")
                            update_state(halt=False)
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
                elif action == "RESET_DAY_START":
                    _raw_reset = await om.get_balances_raw(active_symbol)
                    _reset_eq  = _raw_reset[0] if _raw_reset else equity
                    risk.day_start_equity   = _reset_eq
                    risk.month_start_equity = _reset_eq
                    push_log(f"[RESET_DAY_START] Risk baselines reset to ${_reset_eq:.2f}")
                    log("AGENT", "DAY_START_RESET", equity=round(_reset_eq, 2), source=instr.get("source", ""))
                elif action == "RESUME_AUTO":
                    update_state(coin_mode="auto")
                    push_log("[RESUME_AUTO] Auto-scanner re-enabled — will switch to best coin next cycle")
                    log("AGENT", "RESUME_AUTO", symbol=active_symbol)
                elif action == "ANALYST_ON":
                    analyst.toggle(True)
                    update_state(analyst_enabled=True, analysis={})
                    push_log("[ANALYST] Advisory analyst enabled")
                elif action == "ANALYST_OFF":
                    analyst.toggle(False)
                    update_state(analyst_enabled=False, analysis={})
                    push_log("[ANALYST] Advisory analyst disabled")
                elif action == "SET_STRATEGY":
                    # Accept list (multi-strategy) or single key (backward compat)
                    _req_list = instr.get("strategies", None)
                    if _req_list is None:
                        _req_list = [instr.get("strategy", "")]
                    _req_list = [s.lower().strip() for s in _req_list
                                 if s.lower().strip() in VALID_STRATEGIES][:3]
                    if not _req_list:
                        push_log(f"[SET_STRATEGY] No valid strategies — ignored")
                    else:
                        _old_strat      = _strategy
                        _strategies     = _req_list
                        _strategy       = _strategies[0]
                        _strategy_label = STRATEGY_LABELS[_strategy]
                        _prev_indicators = None  # reset crossover detection state
                        update_state(strategy=_strategy_label, strategy_key=_strategy,
                                     strategy_keys=_strategies)
                        _sec = _strategies[1:] or []
                        push_log(f"[SET_STRATEGY] {_old_strat} → {_strategy}"
                                 + (f" + {_sec}" if _sec else ""))
                        log("AGENT", "STRATEGY_SWITCHED", from_=_old_strat, to=_strategy,
                            secondaries=_strategies[1:])
                elif action == "SET_RISK":
                    _new_r = instr.get("risk_pct")
                    _new_m = instr.get("max_trade_pct")
                    if _new_r is not None:
                        _risk_pct = max(0.001, min(float(_new_r), 1.0))
                    if _new_m is not None:
                        _max_trade_pct = max(0.001, min(float(_new_m), 1.0))
                    update_state(risk_pct=_risk_pct, max_trade_pct=_max_trade_pct)
                    push_log(f"[SET_RISK] risk={_risk_pct*100:.1f}%  max_trade={_max_trade_pct*100:.1f}%")
                    log("AGENT", "RISK_UPDATED",
                        risk_pct=round(_risk_pct, 4), max_trade_pct=round(_max_trade_pct, 4))
            except asyncio.QueueEmpty:
                pass

            # ── Halt check ───────────────────────────────────────
            if risk.halt_active():
                update_state(halt=True)
                log("AGENT", "HALTED", msg="Waiting for halt to lift")
                await _cycle_sleep()
                continue

            # ── Dynamic symbol selection: locked to rule selector only ──────
            # Coin rotation is driven exclusively by the rule-based coin selector (triggered when
            # session P&L ≤ 0). Scanner still runs in the background for data, but will
            # NOT switch coins automatically. Manual SWITCH_COIN instruction still works.
            coin_mode = _state.get("coin_mode", "auto")
            if not em.positions:
                # NM-4: scanner is not started for ema_cross — suppress misleading "top5=[]" log
                if _strategy != "ema_cross" and scanner.ranked:
                    top5 = [r["symbol"] for r in scanner.ranked[:5]]
                    update_state(top_coins=top5)
                    push_log(f"[COIN] Holding {active_symbol} | scanner top5={top5}")

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

            # FIX-14: track session open / high / low for accurate intraday BTC range guard
            if _session_open_price == 0.0:
                _session_open_price = current_price
            _session_high = max(_session_high, current_price)
            _session_low  = min(_session_low,  current_price)

            # H-12: ALL strategies use closed-candle indicators for signal evaluation.
            # The live candle is patched with the WS price in get_klines(), causing EMAs, RSI,
            # and MACD to shift mid-bar — producing phantom crossovers and early signals.
            # Stripping the live candle (iloc[:-1]) gives stable, committed signals on closed bars.
            # `indicators` (live-candle) is kept for real-time position management (exit levels).
            _closed_indicators = md.compute_indicators(candles.iloc[:-1])

            # EMA cross: persist closed-candle indicators for next cycle's crossover comparison
            _prev_indicators = dict(_closed_indicators)

            if not md.quality_gate(tick, book):
                gate_fail_streak += 1
                push_log(f"[QUALITY_GATE] {active_symbol} — gate fail #{gate_fail_streak}, sitting out")
                if (gate_fail_streak >= GATE_FAIL_SWITCH_AFTER
                        and not em.positions
                        and _agent_slot != 0
                        and coin_mode == "auto"
                        and scanner.ranked):
                    _gf_excl = _ep.get_other_symbols(_agent_slot) | {"BTCUSDT"}
                    _gf_next = [r["symbol"] for r in scanner.ranked[:8]
                                if r["symbol"] not in _gf_excl and r["symbol"] != active_symbol]
                    if _gf_next:
                        _gf_sym = _gf_next[0]
                        push_log(f"[ROTATE] Gate fail ×{gate_fail_streak} on {active_symbol} → {_gf_sym}")
                        log("AGENT", "COIN_ROTATE_GATE_FAIL",
                            from_=active_symbol, to=_gf_sym, streak=gate_fail_streak)
                        scanner._health.invalidate(active_symbol)  # M-6: clear stale health cache
                        await md.close()
                        active_symbol = _gf_sym
                        md = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                        update_state(symbol=active_symbol)
                        gate_fail_streak = 0
                        if risk.clear_if_consec_loss(active_symbol):
                            push_log(f"[ROTATE] Consecutive-loss halt cleared — fresh start on {active_symbol}")
                            update_state(halt=False)
                await _cycle_sleep()
                continue
            gate_fail_streak = 0

            # ── NN model: retrain when symbol changes or interval elapses ────
            # NM-3: predictor is None for all non-momentum strategies — skip block entirely
            if predictor is not None:
                _nn_cycle_count += 1
                if (not predictor.is_trained
                        or predictor.symbol != active_symbol
                        or _nn_cycle_count >= 100):
                    try:
                        # FIX-9: run_in_executor prevents NN training from blocking the async loop
                        _nn_acc = await asyncio.get_event_loop().run_in_executor(
                            None, predictor.train, candles, active_symbol)
                        _nn_cycle_count = 0
                        update_state(nn_accuracy=round(_nn_acc * 100, 1),
                                     nn_oos_acc=round(predictor.oos_acc * 100, 1))
                        push_log(f"[NN] Retrained {active_symbol} — "
                                 f"train={predictor.train_acc*100:.1f}%  "
                                 f"OOS={predictor.oos_acc*100:.1f}%  (samples≈{len(candles)-12})")
                    except Exception as _nn_e:
                        log("NN", "TRAIN_ERROR", symbol=active_symbol, error=str(_nn_e)[:120])

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
                    # FIX-10: synthetic position uses current price as avg_entry (actual entry unknown).
                    # Stop is widened to 1.5×ATR so an upward-shifted entry doesn't prematurely
                    # stop out a position that was actually entered lower. Review manually via dashboard.
                    _sp  = Position(
                        side="BUY", avg_entry=current_price, qty=_bb,
                        stop=round(current_price - _atr * cfg.ATR_STOP_MULT * 1.5, 2),  # wider stop for synthetic
                        tp1=round(current_price + _tp1_dist, 2),
                        tp2=round(current_price + _tp2_dist, 2),
                        tp3=round(current_price + _tp3_dist, 2),
                        initial_risk=round(_bb * _atr * cfg.ATR_STOP_MULT, 4),
                        symbol=active_symbol,
                    )
                    em.positions.append(_sp)
                    push_log(f"[RECOVERY] ⚠ SYNTHETIC POSITION — entry={current_price:.4f} is CURRENT PRICE not actual fill. "
                             f"Stop widened to 1.5×ATR. Review manually. "
                             f"qty={round(_bb,6)} {active_symbol[:-4]} | stop={_sp.stop} | tp1={_sp.tp1}")
                    log("AGENT", "ORPHAN_RECOVERY", symbol=active_symbol,
                        qty=round(_bb, 6), synthetic_entry=current_price, stop=_sp.stop, tp1=_sp.tp1,
                        warning="actual_fill_price_unknown")

            # ── Push chart data ───────────────────────────────────
            _fc_slope_pct = 0.0   # neutral fallback — gates inactive if chart block fails
            try:
                import pandas_ta as _ta
                import math as _math
                c   = candles["close"]
                N   = len(c)
                ts_idx = [int(t.timestamp()) for t in candles.index]

                def _series(arr, n=N):
                    return [{"time": ts_idx[i], "value": float(arr.iloc[i])}
                            for i in range(n) if arr.iloc[i] == arr.iloc[i]]

                chart_candles = [
                    {"time": ts_idx[i],
                     "open":  float(candles["open"].iloc[i]),
                     "high":  float(candles["high"].iloc[i]),
                     "low":   float(candles["low"].iloc[i]),
                     "close": float(c.iloc[i])}
                    for i in range(N)
                ]
                _e9  = _ta.ema(c, 9)
                _e21 = _ta.ema(c, 21)
                _e50 = _ta.ema(c, 50)
                _bb  = _ta.bbands(c, 20, 2)
                _mac = _ta.macd(c, 12, 26, 9)
                _rsi = _ta.rsi(c, 14)
                _atr = _ta.atr(candles["high"], candles["low"], c, 14)

                # Volume: normalise to 0-100 range for display
                _vol_raw = candles["volume"]
                _vol_max = _vol_raw.max() or 1
                _vol_norm = _vol_raw / _vol_max * 100

                # MACD histogram sign determines bar colour
                _mhist = _mac["MACD_12_26_9"] - _mac["MACDs_12_26_9"]

                # ── Forecast: linear regression on last 20 candles, project 6 bars ──
                _LR_WIN = min(20, N - 1)
                _xs  = list(range(_LR_WIN))
                _ys  = [float(c.iloc[-((_LR_WIN - i))] ) for i in range(_LR_WIN)]
                _xm  = sum(_xs) / _LR_WIN
                _ym  = sum(_ys) / _LR_WIN
                _ss  = sum((x - _xm) ** 2 for x in _xs) or 1
                _sl  = sum((_xs[i] - _xm) * (_ys[i] - _ym) for i in range(_LR_WIN)) / _ss
                _fc_slope_pct = _sl / current_price * 100  # % per 15m bar — shared with trade gates
                _int = _ym - _sl * _xm
                _bar_secs = 900   # 15m candles
                _last_ts  = ts_idx[-1]
                _last_atr = float(_atr.dropna().iloc[-1]) if len(_atr.dropna()) else 0
                _fc = []
                for _fi in range(1, 7):
                    _ft = _last_ts + _fi * _bar_secs
                    _fv = _int + _sl * (_LR_WIN - 1 + _fi)
                    _fc.append({"time": _ft, "value": round(_fv, 5),
                                "hi": round(_fv + _last_atr, 5),
                                "lo": round(_fv - _last_atr, 5)})

                update_chart(active_symbol, chart_candles,
                             ema9  = _series(_e9),
                             ema21 = _series(_e21),
                             ema50 = _series(_e50),
                             bb_upper  = _series(_bb["BBU_20_2_2.0"]),
                             bb_lower  = _series(_bb["BBL_20_2_2.0"]),
                             macd      = _series(_mac["MACD_12_26_9"]),
                             macd_sig  = _series(_mac["MACDs_12_26_9"]),
                             macd_hist = [{"time": ts_idx[i],
                                           "value": float(_mhist.iloc[i]),
                                           "color": "#00e676" if _mhist.iloc[i] >= 0 else "#ff1744"}
                                          for i in range(N) if _mhist.iloc[i] == _mhist.iloc[i]],
                             rsi       = _series(_rsi),
                             volume    = [{"time": ts_idx[i], "value": float(_vol_norm.iloc[i]),
                                           "color": "#00e5ff33"}
                                          for i in range(N)],
                             forecast  = _fc)
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
                     f"| RSI={indicators['rsi14']:.1f} | EMA9={indicators['ema9']:.4f} "
                     f"| ADX={indicators.get('adx14',0):.1f} | Chop={indicators.get('choppiness14',50):.1f} "
                     f"| FC={_fc_slope_pct:+.3f}%/bar")

            # ── Strategy Advisor ──────────────────────────────────
            _advice = _advisor.advice_payload(indicators, regime)
            update_state(**_advice)

            # ── Rule Analyst (advisory) ─────────────────────────────────────────
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

            # NC-5: Crossover/mean-reversion strategies block new entries during VOLATILE
            # (flash crash / liquidity crisis). Open positions are still managed.
            if regime == "VOLATILE" and not em.positions and _strategy not in RANGING_SKIP_STRATEGIES:
                push_log(f"[{_strategy.upper()}] VOLATILE regime — no new entries (flash-crash guard)")
                await _cycle_sleep()
                continue

            # ── RANGING / VOLATILE with no position: rotate if stuck ────────
            # Trend-following strategies (momentum, macd_cross) skip RANGING/VOLATILE.
            # Mean-reversion strategies (rsi_reversal, bb_breakout, ema_cross) may trade ranging.
            if regime in ("RANGING", "VOLATILE") and not em.positions and _strategy in RANGING_SKIP_STRATEGIES:
                none_signal_streak += 1
                push_log(f"[{regime}] {active_symbol} — no-entry | streak={none_signal_streak}")
                if (none_signal_streak >= NONE_SIGNAL_ROTATE
                        and _agent_slot != 0
                        and coin_mode == "auto"
                        and scanner.ranked):
                    _rot_excl = _ep.get_other_symbols(_agent_slot) | {"BTCUSDT"}
                    _rot_next = [r["symbol"] for r in scanner.ranked[:8]
                                 if r["symbol"] not in _rot_excl and r["symbol"] != active_symbol]
                    if _rot_next:
                        _rot_sym = _rot_next[0]
                        push_log(f"[ROTATE] {active_symbol} "
                                 f"({regime.lower()}×{none_signal_streak}) → {_rot_sym}")
                        log("AGENT", "COIN_ROTATE", from_=active_symbol, to=_rot_sym,
                            reason=f"{regime.lower()}×{none_signal_streak}")
                        await _liquidate_before_switch(active_symbol)
                        scanner._health.invalidate(active_symbol)  # M-6: clear stale health cache
                        await md.close()
                        active_symbol = _rot_sym
                        md = MarketData(active_symbol, cfg.INTERVAL)
                        await md.connect()
                        _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                        update_state(symbol=active_symbol, coin_mode="auto")
                        none_signal_streak = 0
                        if risk.clear_if_consec_loss(active_symbol):
                            push_log(f"[ROTATE] Consecutive-loss halt cleared — fresh start on {active_symbol}")
                            update_state(halt=False)

                await _cycle_sleep()
                continue
            # VOLATILE *with* open position: falls through to manage_open_positions below

            # ── Module 5: Manage open positions ───────────────────
            exit_actions = em.manage_open_positions(current_price, indicators)
            # exit_actions is now list[tuple[str, float, float]] — (action, pnl, sell_qty)
            for act, trade_pnl, _act_sell_qty in exit_actions:
                _session_realized_pnl += trade_pnl
                push_log(f"[EXIT] {act} @ {current_price:.4f}")
                if act.startswith("CLOSE:") and act.endswith(":STOP"):
                    # Stop-loss hit: park the coin instead of selling.
                    # The stock agent will sell it when price recovers 5%.
                    _pending_pos = [p for p in em.positions if p.pending_close]
                    for _pp in _pending_pos:
                        if _pp.exchange_stop_id > 0:
                            asyncio.create_task(om.cancel_order(active_symbol, _pp.exchange_stop_id))
                            _pp.exchange_stop_id = 0
                    _park_qty = _act_sell_qty if _act_sell_qty > 0 else sum(p.qty for p in _pending_pos)
                    _pc.park(active_symbol, _park_qty, current_price, _agent_slot)
                    em.positions = [p for p in em.positions if not p.pending_close]
                    push_log(f"[STOP_PARK] {active_symbol} qty={round(_park_qty,6)} parked @ {current_price:.4f} — stock agent targets {round(current_price*1.05,4)}")
                    log("AGENT", "STOP_PARK", symbol=active_symbol, qty=round(_park_qty,6),
                        park_price=round(current_price,4), target=round(current_price*1.05,4))
                    asyncio.create_task(asyncio.to_thread(
                        _email.notify_fill, _agent_name, active_symbol, "PARK (stop)", _park_qty, current_price))
                elif act.startswith("CLOSE:"):
                    # TIME or SIGNAL_REVERSAL exits — sell normally.
                    # C-1: position is marked pending_close in exit_manager; remove only after confirmed SELL.
                    # C-3: cancel exchange stop before submitting market SELL to prevent double-sell.
                    _pending_pos = [p for p in em.positions if p.pending_close]
                    for _pp in _pending_pos:
                        if _pp.exchange_stop_id > 0:
                            asyncio.create_task(om.cancel_order(active_symbol, _pp.exchange_stop_id))
                            _pp.exchange_stop_id = 0
                    # H-15: use tracked sell_qty from exit_manager instead of querying live balance
                    sell_qty = _act_sell_qty if _act_sell_qty > 0 else (await om.get_base_balance(active_symbol))
                    if sell_qty > 0:
                        close_order = await om.submit("SELL", sell_qty, tick, indicators,
                                                      symbol=active_symbol)
                        if close_order:
                            _ex_status = close_order.get("status", "FILLED")
                            if _ex_status == "PARTIALLY_FILLED":
                                _ex_exec = float(close_order.get("executedQty", 0))
                                _ex_rem  = sell_qty - _ex_exec
                                # C-1: on partial fill, reset pending_close and adjust qty so
                                # the remainder is re-managed next cycle
                                for _pp in _pending_pos:
                                    _pp.pending_close = False
                                    _pp.qty = _ex_rem
                                if _ex_rem * current_price > 10:
                                    push_log(f"[PARTIAL_FILL] Exit {_ex_exec:.6f}/{sell_qty:.6f} sold — {_ex_rem:.6f} remaining, position retained")
                                else:
                                    em.positions = [p for p in em.positions if not p.pending_close]
                            else:
                                # Full fill: clear pending positions
                                em.positions = [p for p in em.positions if not p.pending_close]
                                push_log(f"[EXIT_EXECUTED] SELL {active_symbol} qty={round(sell_qty,6)} @ ~{current_price:.4f}")
                                asyncio.create_task(asyncio.to_thread(
                                    _email.notify_fill, _agent_name, active_symbol, "SELL", sell_qty, current_price))
                        else:
                            # SELL submit failed — reset pending_close so position is re-managed
                            for _pp in _pending_pos:
                                _pp.pending_close = False
                            push_log(f"[CLOSE_WARN] SELL submit failed — position retained, will retry next cycle")
                elif act.startswith("PARTIAL_CLOSE:BUY:TP1"):
                    # H-15: use tracked sold_qty from exit_manager (exact 33% of original)
                    sell_qty = _act_sell_qty if _act_sell_qty > 0 else 0
                    if sell_qty > 0:
                        tp_order = await om.submit("SELL", sell_qty, tick, indicators,
                                                   symbol=active_symbol)
                        if tp_order:
                            push_log(f"[TP1_EXECUTED] SELL {active_symbol} qty={round(sell_qty,6)} @ ~{current_price:.4f}")
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_fill, _agent_name, active_symbol, "SELL (TP1)", sell_qty, current_price))
                            risk.record_trade(trade_pnl, equity)
                elif act.startswith("PARTIAL_CLOSE:BUY:TP2"):
                    # H-15: use tracked sold_qty from exit_manager
                    sell_qty = _act_sell_qty if _act_sell_qty > 0 else 0
                    if sell_qty > 0:
                        tp_order = await om.submit("SELL", sell_qty, tick, indicators,
                                                   symbol=active_symbol)
                        if tp_order:
                            push_log(f"[TP2_EXECUTED] SELL {active_symbol} qty={round(sell_qty,6)} @ ~{current_price:.4f}")
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_fill, _agent_name, active_symbol, "SELL (TP2)", sell_qty, current_price))
                            risk.record_trade(trade_pnl, equity)
                elif act.startswith("PARTIAL_CLOSE:BUY:TP3"):
                    # H-1: position is marked pending_close in exit_manager — clear after confirmed SELL
                    _pending_pos = [p for p in em.positions if p.pending_close]
                    for _pp in _pending_pos:
                        if _pp.exchange_stop_id > 0:
                            asyncio.create_task(om.cancel_order(active_symbol, _pp.exchange_stop_id))
                            _pp.exchange_stop_id = 0
                    sell_qty = _act_sell_qty if _act_sell_qty > 0 else 0
                    if sell_qty > 0:
                        tp_order = await om.submit("SELL", sell_qty, tick, indicators,
                                                   symbol=active_symbol)
                        if tp_order:
                            push_log(f"[TP3_EXECUTED] SELL {active_symbol} qty={round(sell_qty,6)} @ ~{current_price:.4f}")
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_fill, _agent_name, active_symbol, "SELL (TP3)", sell_qty, current_price))
                            risk.record_trade(trade_pnl, equity)
                            # H-1: remove zombie — clear pending_close positions after TP3 fill
                            em.positions = [p for p in em.positions if not p.pending_close]
                        else:
                            for _pp in _pending_pos:
                                _pp.pending_close = False

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

            # ── Signal dispatch — all strategies use closed-candle indicators ──
            if _strategy == "ema_cross":
                _adx_ok = _closed_indicators.get("adx14", 20.0) >= 18.0
                signal = _ema_cross.signal_engine(_closed_indicators, _ema_prev,
                                                   override=pending_override)
                if signal in ("BUY", "SELL") and not _adx_ok:
                    push_log(f"[EMA_CROSS] ADX {_closed_indicators.get('adx14', 0):.1f} < 18 — cross filtered (choppy)")
                    signal = "NONE"
            elif _strategy == "rsi_reversal":
                signal = _rsi_rev.signal_engine(_closed_indicators, override=pending_override)
            elif _strategy == "macd_cross":
                signal = _macd_cross.signal_engine(_closed_indicators, _ema_prev,
                                                    override=pending_override)
            elif _strategy == "bb_breakout":
                signal = _bb_break.signal_engine(_closed_indicators, override=pending_override)
            else:  # "momentum" (default)
                signal = signal_engine(_closed_indicators, regime, override=pending_override)
            _was_override    = pending_override is not None
            pending_override = None

            # ── Secondary strategy consensus veto ─────────────────────────────
            if signal in ("BUY", "SELL") and len(_strategies) > 1:
                _opposite = "SELL" if signal == "BUY" else "BUY"
                for _sec_strat in _strategies[1:]:
                    _sec_sig = "NONE"
                    if _sec_strat == "ema_cross":
                        _sec_sig = _ema_cross.signal_engine(_closed_indicators, _ema_prev)
                    elif _sec_strat == "rsi_reversal":
                        _sec_sig = _rsi_rev.signal_engine(_closed_indicators)
                    elif _sec_strat == "macd_cross":
                        _sec_sig = _macd_cross.signal_engine(_closed_indicators, _ema_prev)
                    elif _sec_strat == "bb_breakout":
                        _sec_sig = _bb_break.signal_engine(_closed_indicators)
                    elif _sec_strat == "momentum":
                        _sec_sig = signal_engine(_closed_indicators, regime)
                    if _sec_sig == _opposite:
                        push_log(f"[CONSENSUS] {_sec_strat.upper()} vetoed {signal} — "
                                 f"secondary says {_sec_sig}")
                        signal = "NONE"
                        break

            # ── Neural network signal augmentation ───────────────────────────
            _uses_nn = _strategy == "momentum"
            if not _uses_nn:
                _nn_prob = 0.5
                update_state(nn_confidence=50.0)
            else:
                _nn_prob = predictor.predict(candles)
                update_state(nn_confidence=round(_nn_prob * 100, 1))
                NN_BUY_THRESHOLD  = 0.70
                NN_SELL_THRESHOLD = 0.30

                # H-7: NN-initiated BUY requires RSI not overbought — avoids chasing momentum tops.
                # All standard entry gates still apply after this promotion.
                if (signal == "NONE"
                        and not em.positions
                        and regime == "TRENDING"
                        and _nn_prob >= NN_BUY_THRESHOLD
                        and _closed_indicators.get("rsi14", 100) < 75):
                    push_log(f"[NN] {active_symbol} up={_nn_prob:.1%} in TRENDING "
                             f"— initiating BUY (subject to all gates)")
                    log("NN", "BUY_INITIATED", symbol=active_symbol, prob=round(_nn_prob, 3))
                    signal = "BUY"

                if (signal != "SELL"
                        and em.positions
                        and _nn_prob <= NN_SELL_THRESHOLD):
                    push_log(f"[NN] {active_symbol} down={1 - _nn_prob:.1%} "
                             f"— triggering protective exit on open position")
                    log("NN", "SELL_INITIATED", symbol=active_symbol, prob=round(_nn_prob, 3))
                    signal = "SELL"

            update_state(last_signal=signal)
            # Crossover strategies: bearish cross with no position = wait for bullish cross.
            # Convert SELL → NONE so rotation logic is not triggered.
            if signal == "SELL" and not em.positions and _strategy in ("ema_cross", "macd_cross"):
                push_log(f"[{_strategy.upper()}] Bearish cross — no position, waiting for bullish cross")
                signal = "NONE"

            # SELL with no open position is spot-untradeable — treat like NONE for rotation.
            # Without this, a bear-trending coin produces endless SELL signals and the agent
            # never rotates, since none_signal_streak stays at 0.
            _actionable = signal == "BUY" or (signal == "SELL" and em.positions)
            # SELL with no position = coin is falling and we can't short; rotate immediately
            _sell_no_pos = signal == "SELL" and not em.positions
            if _actionable:
                push_log(f"[SIGNAL] {signal} on {active_symbol} | regime={regime} "
                         f"| nn={_nn_prob:.1%}")
                none_signal_streak = 0
            else:
                none_signal_streak += 1
                push_log(f"[SIGNAL] NONE on {active_symbol} | regime={regime} "
                         f"| nn={_nn_prob:.1%} | streak={none_signal_streak}")

            # ── Auto coin rotation ─────────────────────────────────────────────
            # EMA cross stays on its assigned coin — crossover is the only gate.
            # slot 0 is BTC-locked; demo locks to its assigned coin.
            _can_rotate = (not em.positions
                           and _agent_slot != 0
                           and coin_mode == "auto"
                           and scanner.ranked
                           and _strategy != "ema_cross")
            _should_rotate = (_sell_no_pos
                              or none_signal_streak >= NONE_SIGNAL_ROTATE)
            if _can_rotate and _should_rotate:
                # H-10: re-query pool at rotation time so we don't race to the same coin as a
                # sibling slot that just registered its new coin in the same cycle.
                _rot_excl = _ep.get_other_symbols(_agent_slot) | {"BTCUSDT"}
                _rot_next = [r["symbol"] for r in scanner.ranked[:8]
                             if r["symbol"] not in _rot_excl and r["symbol"] != active_symbol]
                if _rot_next:
                    _rot_sym = _rot_next[0]
                    _reason  = "bear_trend" if _sell_no_pos else f"no_signal×{none_signal_streak}"
                    push_log(f"[ROTATE] {active_symbol} ({_reason}) → {_rot_sym}")
                    log("AGENT", "COIN_ROTATE",
                        from_=active_symbol, to=_rot_sym, reason=_reason)
                    if _agent_slot != 0:
                        await _liquidate_before_switch(active_symbol)
                    scanner._health.invalidate(active_symbol)  # M-6: clear stale health cache
                    await md.close()
                    active_symbol = _rot_sym
                    md = MarketData(active_symbol, cfg.INTERVAL)
                    await md.connect()
                    _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                    update_state(symbol=active_symbol, coin_mode="auto")
                    none_signal_streak = 0
                    if risk.clear_if_consec_loss(active_symbol):
                        push_log(f"[ROTATE] Consecutive-loss halt cleared — fresh start on {active_symbol}")
                        update_state(halt=False)
                    await _cycle_sleep()
                    continue

            # Shared forecast threshold used by both BUY and SELL gates below.
            _FC_SLOPE_THRESHOLD = 0.01   # % per 15m bar

            # Forecast gate for SELL: if the linear regression slope is clearly
            # upward the position is still in an uptrend — suppress the SELL so
            # it continues riding.  Manual dashboard overrides bypass this gate.
            if (signal == "SELL"
                    and em.positions
                    and not _was_override
                    and _fc_slope_pct > _FC_SLOPE_THRESHOLD):
                push_log(f"[SKIP] {active_symbol} forecast slope {_fc_slope_pct:+.3f}%/bar "
                         f"(uptrend) — SELL blocked by regression gate")
                log("AGENT", "GATE_BLOCK_FORECAST_SELL", symbol=active_symbol,
                    slope_pct=round(_fc_slope_pct, 4), threshold=_FC_SLOPE_THRESHOLD)
                signal = "NONE"

            # Strategy-driven SELL — close the open position
            if signal == "SELL" and em.positions:
                # C-3: cancel exchange stop before market SELL to prevent double-fill
                for _sp in em.positions:
                    if _sp.exchange_stop_id > 0:
                        asyncio.create_task(om.cancel_order(active_symbol, _sp.exchange_stop_id))
                        _sp.exchange_stop_id = 0
                base_bal = await om.get_base_balance(active_symbol)
                if base_bal > 0:
                    close_order = await om.submit("SELL", base_bal, tick, indicators,
                                                  symbol=active_symbol)
                    if close_order:
                        _fill_status = close_order.get("status", "FILLED")
                        if _fill_status == "FILLED":
                            em.positions.clear()
                            push_log(f"[MANUAL_SELL] Closed {active_symbol} qty={round(base_bal,6)} @ ~{current_price:.4f}")
                            log("AGENT", "MANUAL_SELL_EXECUTED", symbol=active_symbol,
                                qty=round(base_bal, 6), price=current_price)
                            asyncio.create_task(asyncio.to_thread(
                                _email.notify_fill, _agent_name, active_symbol, "SELL", base_bal, current_price))
                        elif _fill_status == "PARTIALLY_FILLED":
                            _exec_qty = float(close_order.get("executedQty", 0))
                            _rem_qty  = base_bal - _exec_qty
                            if em.positions and _rem_qty * current_price > 10:
                                em.positions[0].qty = _rem_qty
                                push_log(f"[PARTIAL_FILL] {_exec_qty:.6f}/{base_bal:.6f} {active_symbol[:-4]} sold — position qty reduced to {_rem_qty:.6f}")
                            else:
                                em.positions.clear()
                        else:
                            push_log(f"[CLOSE_WARN] Sell status={_fill_status} — position retained, will retry next cycle")
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
                    if risk.clear_if_consec_loss(active_symbol):
                        push_log(f"[FORCE_SWITCH] Consecutive-loss halt cleared — fresh start on {active_symbol}")
                        update_state(halt=False)
                await _cycle_sleep()
                continue

            # Re-check risk after an actual position close this cycle.
            # Only fires when exit_actions is non-empty (a close/TP actually happened),
            # not on every idle cycle — avoids spurious halts from transient API pricing
            # failures that make equity appear low when no trade occurred.
            # C-2: unpack 3-element tuples; om.get_equity() does not exist — use get_balances_raw()
            _full_close_pnl  = sum(pnl for act, pnl, _ in exit_actions if act.startswith("CLOSE:"))
            _had_full_close  = any(act.startswith("CLOSE:") for act, _, _ in exit_actions)
            _post_close_equity_fetched = False
            if exit_actions and not em.positions:
                try:
                    _pc_raw = await om.get_balances_raw(active_symbol)
                    _post_close_equity = _pc_raw[0] + _pc_raw[1] * current_price
                    risk.update_metrics(_post_close_equity)
                    _post_close_equity_fetched = True
                    # C-2: record_trade for every full close (stop-out and time-exit) so
                    # consecutive-loss halt and daily-DD metrics are correctly updated
                    if _had_full_close:
                        risk.record_trade(_full_close_pnl, _post_close_equity)
                except Exception as _eq_err:
                    log("AGENT", "EQUITY_FETCH_FAILED", error=str(_eq_err)[:160])
            if risk.halt_active():
                _halt_reason = risk.halt_reason or "risk limit reached"
                update_state(halt=True)
                push_log(f"[HALT] {_halt_reason} — no new entries this session")
                log("AGENT", "HALT_ACTIVE", reason=_halt_reason,
                    day_start=round(risk.day_start_equity or 0, 2),
                    equity=round(equity, 2),
                    consec_losses=risk.consec_losses)
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

            # BTC range guard: block entries when BTC is too flat to cover fees.
            # Uses the LARGER of the session range (high/low since agent start) and the
            # 24h ticker range so a restart mid-day doesn't artificially zero out the range.
            if signal == "BUY" and not em.positions and active_symbol == "BTCUSDT":
                _BTC_MIN_DAILY_RANGE = 0.015   # 1.5%
                _btc_h24 = tick.get("high", current_price)
                _btc_l24 = tick.get("low",  current_price)
                _btc_ticker_range  = (_btc_h24 - _btc_l24) / _btc_l24 if _btc_l24 > 0 else 1.0
                _btc_session_range = (
                    (_session_high - _session_low) / _session_open_price
                    if _session_open_price > 0 and _session_low < float("inf")
                    else 0.0
                )
                _btc_range = max(_btc_ticker_range, _btc_session_range)
                if _btc_range < _BTC_MIN_DAILY_RANGE:
                    push_log(
                        f"[SKIP] BTCUSDT range {_btc_range:.2%} "
                        f"(24h={_btc_ticker_range:.2%} session={_btc_session_range:.2%}) "
                        f"< {_BTC_MIN_DAILY_RANGE:.1%} minimum — flat market, fees would consume profit"
                    )
                    log("AGENT", "GATE_BLOCK_BTC_FLAT",
                        range_pct=round(_btc_range * 100, 3),
                        ticker_24h_pct=round(_btc_ticker_range * 100, 3),
                        session_pct=round(_btc_session_range * 100, 3),
                        min_pct=_BTC_MIN_DAILY_RANGE * 100)
                    signal = "NONE"

            if signal == "BUY" and not em.positions:
                try:
                    usdt_equiv, base_bal, raw_usdt = await om.get_balances_raw(active_symbol)
                    equity = usdt_equiv + base_bal * current_price  # full equity (includes BTC)
                except Exception as _bal_err:
                    log("AGENT", "BALANCE_FETCH_FAILED_GATE", error=str(_bal_err)[:160])
                    signal = "NONE"
                    await _cycle_sleep()
                    continue
                log("AGENT", "BUY_GATE_ENTER",
                    symbol=active_symbol, equity=round(equity, 2),
                    raw_usdt=round(raw_usdt, 2), price=current_price)
                stop_d = indicators["atr14"] * cfg.ATR_STOP_MULT
                # Effective TP1 uses the same floor logic as exit_manager: max(h1_range, ATR)
                _h1r         = indicators.get("h1_range", 0)
                _tp1_atr_d   = stop_d * cfg.TP1_R
                _tp1_h1_d    = _h1r * 0.40 if _h1r > 0 else 0
                tp1_dist     = max(_tp1_atr_d, _tp1_h1_d)
                # FIX-5: Precision strategies (crossover/mean-reversion) use reduced TP threshold (0.15% vs 0.22%)
                # without any gate, tight crossovers in ranging markets guaranteed fee losses.
                _this_min_tp = current_price * (0.0015 if _strategy != "momentum" else 0.0022)
                if tp1_dist < _this_min_tp:
                    push_log(f"[SKIP] {active_symbol} TP1 too close ({tp1_dist/current_price*100:.3f}%) — won't cover fees")
                    log("AGENT", "GATE_BLOCK_TP", symbol=active_symbol,
                        tp1_pct=round(tp1_dist/current_price*100, 4),
                        min_pct=round(_this_min_tp/current_price*100, 3))
                    signal = "NONE"
                # Minimum R:R gate — reject entries where reward < 1.5× risk
                MIN_ENTRY_RR = 1.5
                pre_rr = tp1_dist / stop_d if stop_d else 0
                if signal == "BUY" and pre_rr < MIN_ENTRY_RR:
                    push_log(f"[SKIP] {active_symbol} R:R {pre_rr:.2f}x < {MIN_ENTRY_RR} minimum — skipping entry")
                    log("AGENT", "GATE_BLOCK_RR", symbol=active_symbol,
                        rr=round(pre_rr, 3), min_rr=MIN_ENTRY_RR,
                        stop_d=round(stop_d, 6), tp1_d=round(tp1_dist, 6))
                    signal = "NONE"
                # Fear & Greed gate — block only full-panic levels (< 10).
                # size_multiplier() already halves positions at F&G ≤ 15, so 10-19 is
                # covered by sizing. Blocking at < 20 was also catching recovery bounces
                # that fire precisely when sentiment is at extreme-fear lows.
                _fg_val = fear_greed.get("value", 50) if fear_greed else 50
                if signal == "BUY" and _fg_val < 10:
                    push_log(f"[SKIP] {active_symbol} Fear&Greed={_fg_val} (full panic) — no new longs")
                    log("AGENT", "GATE_BLOCK_FG", symbol=active_symbol, fg=_fg_val)
                    signal = "NONE"
                # ATR cap: reject extremely volatile coins — the same volatility that scores them
                # high will blow through stops on normal noise (FIDA: 4.1% ATR → stop hit -11%)
                MAX_ENTRY_ATR_PCT = 3.0
                atr_pct_live = indicators["atr14"] / current_price * 100
                if signal == "BUY" and atr_pct_live > MAX_ENTRY_ATR_PCT:
                    push_log(f"[SKIP] {active_symbol} ATR {atr_pct_live:.2f}% > {MAX_ENTRY_ATR_PCT}% cap — too volatile to enter safely")
                    signal = "NONE"
                # Forecast gate: linear regression on last 20 candles must slope upward.
                # A downward-sloping regression means recent price action is trending lower —
                # entering a BUY against it would be fighting the short-term trend.
                # Threshold: 0.01%/bar filters noise on flat markets; near-zero slope = neutral = allow.
                if signal == "BUY" and _fc_slope_pct < -_FC_SLOPE_THRESHOLD:
                    push_log(f"[SKIP] {active_symbol} forecast slope {_fc_slope_pct:+.3f}%/bar "
                             f"(downtrend) — BUY blocked by regression gate")
                    log("AGENT", "GATE_BLOCK_FORECAST", symbol=active_symbol,
                        slope_pct=round(_fc_slope_pct, 4), threshold=-_FC_SLOPE_THRESHOLD)
                    signal = "NONE"
                # NH-2: gate activates only when OOS accuracy ≥ 58% AND test set ≥ 50 samples.
                # NM-3: predictor is None for ema_cross — gate is always inactive, entry allowed.
                _nn_gate_active = (predictor is not None
                                   and predictor.is_trained
                                   and predictor.oos_acc >= OOS_MIN_ACC
                                   and predictor.oos_samples >= OOS_MIN_SAMP)
                if signal == "BUY" and _nn_gate_active and _nn_prob < predictor.conf_floor:
                    push_log(f"[NN] {active_symbol} up={_nn_prob:.1%} "
                             f"< {predictor.conf_floor:.0%} floor — entry blocked "
                             f"(OOS={predictor.oos_acc*100:.1f}% n={predictor.oos_samples})")
                    log("NN", "GATE_BLOCK", symbol=active_symbol,
                        prob=round(_nn_prob, 3), floor=predictor.conf_floor,
                        oos_acc=round(predictor.oos_acc, 3), oos_n=predictor.oos_samples)
                    signal = "NONE"
                elif signal == "BUY":
                    if predictor is None:
                        _gate_note = "ema_cross — no NN gate"
                    elif _nn_gate_active:
                        _gate_note = f"OOS={predictor.oos_acc*100:.1f}% n={predictor.oos_samples} — gate active"
                    else:
                        _gate_note = f"gate bypassed (OOS={predictor.oos_acc*100:.1f}% n={predictor.oos_samples} — insufficient)"
                    push_log(f"[NN] {active_symbol} up={_nn_prob:.1%} — entry confirmed ({_gate_note})")

            if signal == "BUY" and not em.positions:
                # NH-1: pool collision check applies to ALL strategies including ema_cross.
                # Removing the ema_cross exemption prevents double ETH exposure when a momentum
                # slot is already in ETHUSDT (which ema_cross now uses as its assigned coin).
                if active_symbol in _ep.get_other_symbols(_agent_slot):
                    push_log(f"[SKIP] {active_symbol} already held by another slot — skipping to avoid duplicate exposure")
                    signal = "NONE"

            if signal == "BUY" and not em.positions:
                # H-1: reuse usdt_equiv/base_bal/raw_usdt from the first get_balances_raw() call
                # above (lines ~1155). No trade occurred between the two blocks — values are fresh.
                stop_d      = indicators["atr14"] * cfg.ATR_STOP_MULT
                pool_budget = _ep.get_budget(_agent_slot, equity)
                log("AGENT", "SIZING_PRE",
                    symbol=active_symbol, equity=round(equity, 2),
                    raw_usdt=round(raw_usdt, 2), pool_budget=round(pool_budget, 2),
                    stop_d=round(stop_d, 4), size_mult=size_mult)
                qty    = PositionSizer.calculate(equity, current_price, stop_d, size_mult,
                                                 usdt_available=raw_usdt,
                                                 pool_budget=pool_budget,
                                                 risk_pct=_risk_pct,
                                                 max_trade_pct=_max_trade_pct)

                if not qty:
                    _order_usdt = (equity * _risk_pct / stop_d * current_price) if stop_d else 0
                    push_log(f"[SKIP] {active_symbol} sizing aborted — "
                             f"equity=${equity:.2f} pool_budget=${pool_budget:.2f} "
                             f"est_order=${min(_order_usdt, pool_budget):.2f} "
                             f"(min $10 required)")
                    log("AGENT", "SIZING_ABORTED", symbol=active_symbol,
                        equity=round(equity, 2), pool_budget=round(pool_budget, 2),
                        raw_usdt=round(raw_usdt, 2), stop_d=round(stop_d, 4),
                        size_mult=size_mult, price=current_price)

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
                            # C-3: place exchange-side stop order immediately after BUY fill.
                            # This acts as a safety net if the agent process crashes — the stop
                            # executes on the exchange even without a running agent.
                            try:
                                _stop_oid = await om.place_stop_limit(active_symbol, pos.qty, pos.stop)
                                if _stop_oid:
                                    pos.exchange_stop_id = _stop_oid
                                    push_log(f"[STOP_ORDER] Exchange stop placed: {active_symbol} "
                                             f"stop=${pos.stop:.4f} orderId={_stop_oid}")
                                    log("AGENT", "EXCHANGE_STOP_PLACED", symbol=active_symbol,
                                        stop=round(pos.stop, 4), orderId=_stop_oid)
                                else:
                                    push_log(f"[STOP_ORDER] Exchange stop placement failed — software stop active only")
                            except Exception as _stop_e:
                                log("AGENT", "EXCHANGE_STOP_ERROR", error=str(_stop_e)[:80])
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
            # M-7: skip if update_metrics already ran with fresh post-close equity this cycle
            if not _post_close_equity_fetched:
                risk.update_metrics(equity)
            import portfolio_tracker as _pt
            _pf = _pt.get_portfolio_state()
            _unrealized_pnl = sum(
                (current_price - p.avg_entry) * p.qty * (1 if p.side == "BUY" else -1)
                for p in em.positions
            )
            _s_pnl     = _session_realized_pnl + _unrealized_pnl
            _s_pnl_pct = (_s_pnl / _session_start_equity * 100) if _session_start_equity else 0
            update_state(
                equity=equity,
                daily_dd=((risk.day_start_equity - equity) / risk.day_start_equity * 100)
                          if risk.day_start_equity else 0,
                halt=risk.halt_flag,
                halt_reason=risk.halt_reason,
                portfolio=_pf,
                session_pnl=round(_s_pnl, 2),
                session_pnl_pct=round(_s_pnl_pct, 2),
                session_start_equity=round(_session_start_equity, 2),
            )

            # ── Coin selection trigger: fires once when session P&L ≤ 0 ─
            # Resets when P&L recovers above 0 so it can fire again on the next drawdown.
            if _s_pnl > 0:
                _ai_sel_triggered  = False
                _ai_sel_pending_ts = 0.0
            elif not _ai_sel_triggered and not em.positions and _agent_slot != 0 and coin_mode == "auto":
                # Stagger: slot1=20s, slot2=40s, slot3=60s — ensures earlier slots register
                # their coins in the equity pool before later slots query it
                if _ai_sel_pending_ts == 0.0:
                    _ai_sel_pending_ts = _time.time() + _agent_slot * 20
                    push_log(f"[SELECT] P&L {_s_pnl:+.4f} ≤ 0 — selection fires in {_agent_slot * 20}s")
                elif _time.time() < _ai_sel_pending_ts:
                    pass  # still in stagger window
                else:
                    # Stagger elapsed — fire now
                    _ai_sel_triggered  = True
                    _ai_sel_pending_ts = 0.0
                    log("AGENT", "SELECT_TRIGGERED", session_pnl=round(_s_pnl, 4))

                    # Build exclusion set: all coins in use by other slots + BTCUSDT (slot 0 locked)
                    _hard_excl = _ep.get_other_symbols(_agent_slot) | {"BTCUSDT"}

                    # Build candidate list, stripping already-taken and BTC coins before the selector sees them
                    _sel_candidates = [
                        {
                            "symbol":     r["symbol"],
                            "deep_score": r.get("deep_score", 0),
                            "atr_pct":    r.get("atr_pct", 0),
                            "chg_pct":    r.get("chg_pct", 0),
                            "vol_m":      r.get("vol", 0) / 1_000_000,
                            "regime":     r.get("regime", "?"),
                            "trend":      r.get("trend", "?"),
                        }
                        for r in scanner.ranked[:12]
                        if r["symbol"] not in _hard_excl
                    ] if scanner.ranked else []

                    push_log(f"[SELECT] Firing for slot {_agent_slot} | excluded={sorted(_hard_excl)} | candidates={[c['symbol'] for c in _sel_candidates[:5]]}")

                    if not _sel_candidates:
                        push_log(f"[SELECT] No candidates yet (scanner still warming up) — retry in 2m")
                        _ai_sel_triggered  = False
                        _ai_sel_pending_ts = _time.time() + 120
                    else:
                        try:
                            _sel_result = await selector.select(
                                _sel_candidates, fear_greed=fear_greed, interval_secs=0
                            )
                            if _sel_result:
                                _profitable_recs = [
                                    r for r in _sel_result.get("recommendations", [])
                                    if r.get("profitable") and r.get("confidence", 0) >= 65
                                    and r["symbol"] not in _hard_excl  # double-check after selector response
                                ]
                                push_log(f"[SELECT] Market: {_sel_result.get('market_comment','')} | Profitable: {[r['symbol'] for r in _profitable_recs]}")
                                _assigned = False
                                for _rec in _profitable_recs:
                                    _sel_sym = _rec["symbol"]
                                    push_log(f"[SELECT] → {_sel_sym} conf={_rec['confidence']}% est_rr={_rec.get('est_rr','?')}x | {_rec.get('reason','')}")
                                    log("AGENT", "SELECT_ASSIGN", symbol=_sel_sym,
                                        confidence=_rec["confidence"], est_rr=_rec.get("est_rr"))
                                    if _sel_sym != active_symbol:
                                        await _liquidate_before_switch(active_symbol)
                                        scanner._health.invalidate(active_symbol)  # M-6: clear stale health cache
                                        await md.close()
                                        active_symbol = _sel_sym
                                        md = MarketData(active_symbol, cfg.INTERVAL)
                                        await md.connect()
                                        _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)
                                        update_state(symbol=active_symbol)
                                        none_signal_streak = 0
                                        if risk.clear_if_consec_loss(active_symbol):
                                            push_log(f"[SELECT] Consecutive-loss halt cleared — fresh start on {active_symbol}")
                                            update_state(halt=False)
                                    selector.last_result = {}
                                    _assigned = True
                                    break
                                if not _assigned:
                                    push_log(f"[SELECT] No profitable pick — will retry in 15m")
                                    _ai_sel_triggered  = False
                                    _ai_sel_pending_ts = _time.time() + 900
                            else:
                                push_log(f"[SELECT] Selector returned nothing — will retry in 15m")
                                _ai_sel_triggered  = False
                                _ai_sel_pending_ts = _time.time() + 900
                        except Exception as _sel_e:
                            log("SELECTOR", "TRIGGER_ERROR", error=str(_sel_e)[:80])
                            _ai_sel_triggered  = False
                            _ai_sel_pending_ts = _time.time() + 900

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

            # Update pool immediately so portfolio_tracker total stays in sync with usdt_free.
            # The top-of-loop report uses last cycle's value; this ensures the pool reflects
            # any position open/close that happened this cycle before the next 30s USDT refresh.
            _pool_open_usdt = sum(p.qty * current_price for p in em.positions)
            _pool_pnl = sum(p.qty * current_price - p.qty * p.avg_entry for p in em.positions)
            _ep.report(_agent_slot, active_symbol, _pool_open_usdt, _pool_pnl)

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
            await om.cancel_all(symbol=active_symbol)
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
