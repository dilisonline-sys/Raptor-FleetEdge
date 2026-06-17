"""Shared equity pool — coordinates USDT budgets and coin exclusions across up to 4 live agents.
Each agent registers its slot (0-3), reports its open position value each cycle, and
reads other slots' symbols to avoid trading the same coin simultaneously.
Uses fcntl file locking for safe concurrent multi-process access.
"""
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
import config as cfg

# Mode-specific pool file: live, demo, and testnet agents share the same /tmp
# directory inside the container, so each mode must use a separate file to
# prevent live balances from contaminating demo/testnet portfolio displays.
POOL_FILE = Path(f"/tmp/rfe_equity_pool_{cfg.TRADING_MODE}.json")
SLOT_TTL  = 90  # seconds without heartbeat → slot treated as dead


@contextlib.contextmanager
def _locked_file(mode: str, lock_type: int):
    """Open POOL_FILE with an fcntl lock and yield the file object.
    H-11: exclusive lock (LOCK_EX) for all writes so read-modify-write is atomic."""
    try:
        with open(POOL_FILE, mode) as f:
            fcntl.flock(f, lock_type)
            try:
                yield f
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        yield None


def _default_state() -> dict:
    return {"slots": {str(i): None for i in range(cfg.FLEET_SIZE)}, "ts": 0, "usdt_free": 0.0}


def _live(state: dict) -> dict:
    """Clear slots that haven't sent a heartbeat within SLOT_TTL seconds."""
    now = time.time()
    for k, s in list(state.get("slots", {}).items()):
        if s and now - s.get("ts", 0) > SLOT_TTL:
            state["slots"][k] = None
    return state


def _read_locked(f) -> dict:
    """Read state from an already-open, already-locked file handle."""
    if f is None:
        return _default_state()
    try:
        f.seek(0)
        return json.load(f)
    except Exception:
        return _default_state()


def _write_locked(f, state: dict):
    """Write state to an already-open, already-locked file handle (truncate + rewrite)."""
    if f is None:
        return
    try:
        f.seek(0)
        f.truncate()
        json.dump(state, f, indent=2)
        f.flush()
    except Exception:
        pass


def _read() -> dict:
    """Read-only access (shared lock) — for callers that don't modify state."""
    try:
        with open(POOL_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            except Exception:
                return _default_state()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        return _default_state()


def _modify(fn):
    """H-11: atomic read-modify-write under an exclusive lock.
    fn receives the current state dict and must return the modified state dict."""
    # Ensure file exists before opening in r+ mode
    if not POOL_FILE.exists():
        POOL_FILE.write_text(json.dumps(_default_state(), indent=2))
    try:
        with open(POOL_FILE, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                try:
                    state = json.load(f)
                except Exception:
                    state = _default_state()
                state = _live(state)
                state = fn(state)
                f.seek(0)
                f.truncate()
                json.dump(state, f, indent=2)
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass


def register(slot: int, symbol: str, pid: int, port: int):
    """Announce this agent's existence in the pool at startup."""
    def _fn(state):
        state["slots"][str(slot)] = {
            "symbol": symbol, "open_usdt": 0.0, "daily_pnl": 0.0,
            "ts": time.time(), "pid": pid, "port": port,
        }
        return state
    _modify(_fn)


def report(slot: int, symbol: str, open_usdt: float, daily_pnl: float, *,
           usdt_free: float | None = None):
    """Update this slot's live state. Called each cycle by the agent."""
    def _fn(state):
        s = state["slots"].get(str(slot)) or {}
        s.update({"symbol": symbol, "open_usdt": open_usdt,
                   "daily_pnl": daily_pnl, "slot_pnl": daily_pnl, "ts": time.time()})
        state["slots"][str(slot)] = s
        if usdt_free is not None:
            state["usdt_free"] = usdt_free
        return state
    _modify(_fn)


def report_usdt(raw_usdt: float):
    """Update only the top-level usdt_free field in the pool (called by background equity pusher)."""
    def _fn(state):
        state["usdt_free"] = raw_usdt
        return state
    _modify(_fn)


def report_earn(earn_value: float):
    """Update Simple Earn total in the pool (called by slot 0's equity pusher every 5 min)."""
    def _fn(state):
        state["earn_value"] = earn_value
        return state
    _modify(_fn)


def deregister(slot: int):
    """Remove this slot from the pool on clean shutdown."""
    def _fn(state):
        state["slots"][str(slot)] = None
        return state
    _modify(_fn)


def report_other_coins(value: float):
    """Value of wallet coins that are not USDT, not in any active position, and not parked.
    Covers BNB (fee buffer), orphan alts, etc. Written by slot 0 every 30s."""
    def _fn(state):
        state["other_coins_usdt"] = round(value, 4)
        return state
    _modify(_fn)


def report_parked_usdt(value: float):
    """Update the total USDT value of parked coins (written by stock agent each scan)."""
    def _fn(state):
        state["parked_usdt"] = round(value, 4)
        return state
    _modify(_fn)


def set_parked_symbols(symbols: list):
    """Update the list of parked coin symbols so trading agents can exclude them."""
    def _fn(state):
        state["parked_symbols"] = list(symbols)
        return state
    _modify(_fn)


def get_state() -> dict:
    """Return current pool state with stale slots cleared."""
    return _live(_read())


def get_other_symbols(slot: int) -> set[str]:
    """Symbols with an open position in other slots, or parked by stock agent.
    Idle slots (registered but open_usdt == 0) are excluded — registration alone
    must not prevent the BTC slot from entering its own permanently-assigned coin."""
    state = _live(_read())
    active = {
        s["symbol"]
        for k, s in state.get("slots", {}).items()
        if s and int(k) != slot and s.get("open_usdt", 0.0) > 1.0
    }
    parked = set(state.get("parked_symbols", []))
    return active | parked


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
    MIN_TRADEABLE = 10.0
    if per_agent_cap < MIN_TRADEABLE:
        return max(0.0, min(pool_cap, total_equity * cfg.MAX_TRADE_PCT))

    return max(0.0, min(pool_cap, per_agent_cap))
