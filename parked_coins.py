"""Parked-coin registry — coins held after a stop-loss hit, waiting for 5% recovery.

When a trading agent's stop loss fires, instead of selling the coin it parks it here.
The stock agent (port 7431) monitors all parked coins and sells each one when its
live price reaches target_price (= park_price × 1.05).

File is mode-specific so live / demo / testnet parks never mix.
All writes use fcntl.LOCK_EX for safe concurrent multi-process access.
"""
import fcntl
import json
import time
from pathlib import Path
import config as cfg

PARK_FILE = Path(f"/tmp/rfe_parked_{cfg.TRADING_MODE}.json")
RECOVERY_PCT     = 1.05  # 5% above park price
# BNB is excluded: Binance uses it internally for fee discounts
EXCLUDED_SYMBOLS = {"BNBUSDT"}


def _read() -> dict:
    try:
        with open(PARK_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            except Exception:
                return {}
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return {}


def _modify(fn):
    if not PARK_FILE.exists():
        PARK_FILE.write_text(json.dumps({}))
    with open(PARK_FILE, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                state = json.load(f)
            except Exception:
                state = {}
            state = fn(state)
            f.seek(0)
            f.truncate()
            json.dump(state, f, indent=2)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def park(symbol: str, qty: float, park_price: float, slot: int) -> None:
    """Register a coin as parked after stop-loss. Overwrites any prior entry for this symbol."""
    if symbol in EXCLUDED_SYMBOLS:
        return
    def _fn(state):
        state[symbol] = {
            "qty":          round(qty, 8),
            "park_price":   round(park_price, 8),
            "target_price": round(park_price * RECOVERY_PCT, 8),
            "parked_at":    time.time(),
            "slot":         slot,
        }
        return state
    _modify(_fn)


def unpark(symbol: str) -> None:
    """Remove a coin from the parked registry (called after stock agent sells it)."""
    def _fn(state):
        state.pop(symbol, None)
        return state
    _modify(_fn)


def get_parked() -> dict:
    """Returns all parked entries as {symbol: {qty, park_price, target_price, parked_at, slot}}."""
    return _read()


def is_parked(symbol: str) -> bool:
    """True if this symbol is currently parked."""
    return symbol in _read()


def total_usdt_value(prices: dict[str, float]) -> float:
    """Estimate total USDT value of all parked coins using supplied price map."""
    total = 0.0
    for sym, entry in _read().items():
        price = prices.get(sym, entry.get("park_price", 0.0))
        total += entry.get("qty", 0.0) * price
    return total
