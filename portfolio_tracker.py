"""Central portfolio value tracker.
total_assets = usdt_free (from pool) + sum of all slot open_usdt values.
Agents write their raw_usdt to the pool; this module aggregates for P&L.
"""
import fcntl
import json
import time
from datetime import datetime
from pathlib import Path
import config as cfg

# Mode-specific files so live, demo, and testnet portfolios never share data.
# All modes run as sub-processes inside the same container sharing /tmp.
POOL_FILE      = Path(f"/tmp/rfe_equity_pool_{cfg.TRADING_MODE}.json")
DAY_START_FILE = Path(f"/tmp/rfe_portfolio_day_{cfg.TRADING_MODE}.json")


def get_portfolio_state() -> dict:
    """Returns {total_assets, usdt_free, coin_value, day_start, pnl_usdt, pnl_pct, slots}"""
    # Read pool file with shared lock
    pool = {}
    try:
        with open(POOL_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                pool = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass

    slots             = pool.get("slots", {})
    usdt_free         = pool.get("usdt_free", 0.0)
    earn_value        = pool.get("earn_value", 0.0)
    parked_value      = pool.get("parked_usdt", 0.0)
    other_coins_usdt  = pool.get("other_coins_usdt", 0.0)
    coin_value        = sum(
        s["open_usdt"] for s in slots.values()
        if s is not None and isinstance(s, dict)
    )
    # coin_value updates every ~5s (equity pusher) via live WS price × base qty.
    # other_coins_usdt covers BNB and any coin not in an active position or parked (updates every 30s).
    total_assets = usdt_free + coin_value + earn_value + other_coins_usdt + parked_value

    day_start = _load_day_start()

    if day_start is None or total_assets == 0:
        return {
            "total_assets":    total_assets,
            "usdt_free":       usdt_free,
            "coin_value":      coin_value,
            "earn_value":      earn_value,
            "parked_value":    parked_value,
            "other_coins_usdt": other_coins_usdt,
            "day_start":       day_start or 0.0,
            "pnl_usdt":        0.0,
            "pnl_pct":         0.0,
            "slots":           slots,
        }

    pnl_usdt = total_assets - day_start
    pnl_pct  = pnl_usdt / day_start * 100 if day_start > 0 else 0.0

    return {
        "total_assets":    round(total_assets, 2),
        "usdt_free":       round(usdt_free, 2),
        "coin_value":      round(coin_value, 2),
        "earn_value":      round(earn_value, 2),
        "parked_value":    round(parked_value, 2),
        "other_coins_usdt": round(other_coins_usdt, 2),
        "day_start":       round(day_start, 2),
        "pnl_usdt":        round(pnl_usdt, 2),
        "pnl_pct":         round(pnl_pct, 4),
        "slots":           slots,
    }


def reset_day_start(total_assets: float) -> None:
    """Call at start of new day or agent fleet restart."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    _save_day_start(total_assets, date_str)


def check_halt_needed(total_assets: float, day_start: float, daily_dd_limit: float = 0.10) -> bool:
    """Returns True if drawdown exceeds limit."""
    if day_start <= 0:
        return False
    drawdown = (day_start - total_assets) / day_start
    return drawdown >= daily_dd_limit


def _load_day_start() -> float | None:
    try:
        with open(DAY_START_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        today = datetime.now().strftime("%Y-%m-%d")
        if data.get("date") != today:
            return None
        return float(data["assets"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _save_day_start(val: float, date_str: str | None = None) -> None:
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    payload = {"assets": val, "ts": time.time(), "date": date_str}
    try:
        with open(DAY_START_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(payload, f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass
