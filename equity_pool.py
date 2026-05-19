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

POOL_FILE = Path("/tmp/dipu_equity_pool.json")
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
        return {"slots": {str(i): None for i in range(4)}, "ts": 0}


def _write(state: dict):
    try:
        with open(POOL_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(state, f, indent=2)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
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


def report(slot: int, symbol: str, open_usdt: float, daily_pnl: float):
    """Update this slot's live state. Called each cycle by the agent."""
    state = _live(_read())
    s = state["slots"].get(str(slot)) or {}
    s.update({"symbol": symbol, "open_usdt": open_usdt,
               "daily_pnl": daily_pnl, "ts": time.time()})
    state["slots"][str(slot)] = s
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

    = min(
        per-agent share  = total_equity × MAX_TRADE_PCT / n_active_agents,
        pool remaining   = total_equity × MAX_EXPOSURE  - other_slots_open_usdt
    )
    Prevents any single agent from over-sizing when siblings already have positions open.
    """
    state  = _live(_read())
    slots  = state.get("slots", {})
    n      = max(sum(1 for s in slots.values() if s is not None), 1)

    other_open    = sum(s["open_usdt"] for k, s in slots.items()
                        if s is not None and int(k) != slot)
    pool_cap      = total_equity * cfg.MAX_EXPOSURE - other_open
    per_agent_cap = total_equity * cfg.MAX_TRADE_PCT / n

    return max(0.0, min(pool_cap, per_agent_cap))
