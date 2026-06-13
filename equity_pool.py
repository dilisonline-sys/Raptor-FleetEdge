"""Shared equity pool — coordinates USDT budgets and coin exclusions across up to 4 live agents.
Each agent registers its slot (0-3), reports its open position value each cycle, and
reads other slots' symbols to avoid trading the same coin simultaneously.
Uses fcntl file locking for safe concurrent multi-process access.
"""
import fcntl
import json
import os
import time
from pathlib import Path
import config as cfg

POOL_FILE = Path("/tmp/rfe_equity_pool.json")
SLOT_TTL  = 90  # seconds without heartbeat → slot treated as dead


def _read() -> dict:
    try:
        with open(POOL_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        # FIX-17: default to FLEET_SIZE slots (5) so slot 4 (EMA cross) has a budget entry
        return {"slots": {str(i): None for i in range(cfg.FLEET_SIZE)}, "ts": 0, "usdt_free": 0.0}


def _write(state: dict):
    # C-3: write to a temp file then atomically replace — prevents concurrent readers
    # seeing a truncated pool file between open("w") and the first json.dump byte.
    try:
        _tmp = POOL_FILE.with_suffix(".tmp")
        _tmp.write_text(json.dumps(state, indent=2))
        _tmp.replace(POOL_FILE)   # atomic on Linux (same filesystem)
    except Exception:
        pass


def _live(state: dict) -> dict:
    """Clear slots that haven't sent a heartbeat within SLOT_TTL seconds."""
    now = time.time()
    for k, s in list(state.get("slots", {}).items()):
        if s and now - s.get("ts", 0) > SLOT_TTL:
            state["slots"][k] = None
    return state


def register(slot: int, symbol: str, pid: int, port: int):
    """Announce this agent's existence in the pool at startup."""
    state = _live(_read())
    state["slots"][str(slot)] = {
        "symbol": symbol, "open_usdt": 0.0, "daily_pnl": 0.0,
        "ts": time.time(), "pid": pid, "port": port,
    }
    _write(state)


def report(slot: int, symbol: str, open_usdt: float, daily_pnl: float, *,
           usdt_free: float | None = None):
    """Update this slot's live state. Called each cycle by the agent.

    daily_pnl is stored as both 'daily_pnl' (backward compat) and 'slot_pnl'
    (slot's own unrealized P&L on its open coin position only).
    """
    state = _live(_read())
    s = state["slots"].get(str(slot)) or {}
    s.update({"symbol": symbol, "open_usdt": open_usdt,
               "daily_pnl": daily_pnl, "slot_pnl": daily_pnl, "ts": time.time()})
    state["slots"][str(slot)] = s
    if usdt_free is not None:
        state["usdt_free"] = usdt_free
    _write(state)


def report_usdt(raw_usdt: float):
    """Update only the top-level usdt_free field in the pool (called by background equity pusher)."""
    state = _live(_read())
    state["usdt_free"] = raw_usdt
    _write(state)


def report_earn(earn_value: float):
    """Update Simple Earn total in the pool (called by slot 0's equity pusher every 5 min)."""
    state = _live(_read())
    state["earn_value"] = earn_value
    _write(state)


def deregister(slot: int):
    """Remove this slot from the pool on clean shutdown."""
    state = _live(_read())
    state["slots"][str(slot)] = None
    _write(state)


def get_state() -> dict:
    """Return current pool state with stale slots cleared."""
    return _live(_read())


def get_other_symbols(slot: int) -> set[str]:
    """Symbols actively traded by other slots — pass to scanner as exclude set."""
    return {
        s["symbol"]
        for k, s in _live(_read()).get("slots", {}).items()
        if s and int(k) != slot
    }


def get_budget(slot: int, total_equity: float) -> float:
    """Max USDT this slot may deploy for a new position.

    All agents share one USDT balance (SHARED_EQUITY_MODE). Budget is:

        min(
            per-slot share  = total_equity × MAX_TRADE_PCT / FLEET_SIZE,
            pool headroom   = total_equity × MAX_EXPOSURE  − other_slots_open_usdt
        )

    FLEET_SIZE (default 4) is always used as the divisor — not just the
    number of currently-active agents — so a single running agent cannot
    monopolise the shared pool while siblings are starting up.
    """
    if not cfg.SHARED_EQUITY_MODE:
        # Standalone mode: agent owns all equity, no pool coordination
        return total_equity * cfg.MAX_TRADE_PCT

    state  = _live(_read())
    slots  = state.get("slots", {})
    n      = cfg.FLEET_SIZE  # divide by full fleet, not just live agents

    other_open    = sum(s["open_usdt"] for k, s in slots.items()
                        if s is not None and int(k) != slot)
    pool_cap      = total_equity * cfg.MAX_EXPOSURE - other_open
    per_agent_cap = total_equity * cfg.MAX_TRADE_PCT / n

    # When the per-slot share is below the Binance minimum order ($10), fall
    # back to the full pool headroom so a lone active agent can still trade.
    # pool_cap already accounts for other slots' open exposure, so this is safe.
    MIN_TRADEABLE = 10.0
    if per_agent_cap < MIN_TRADEABLE:
        return max(0.0, min(pool_cap, total_equity * cfg.MAX_TRADE_PCT))

    return max(0.0, min(pool_cap, per_agent_cap))
