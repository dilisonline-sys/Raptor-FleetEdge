import os
from dotenv import load_dotenv

load_dotenv()

# ── Identity ──────────────────────────────────────────────
AGENT_NAME    = os.environ.get("AGENT_NAME", "dipu")
AGENT_VERSION = "1.1"

# ── Trading mode ─────────────────────────────────────────
# Values: "testnet" | "demo" | "live"
TRADING_MODE = os.environ.get("TRADING_MODE", "testnet").lower().strip()
assert TRADING_MODE in ("testnet", "demo", "live"), \
    f"TRADING_MODE must be testnet | demo | live, got: {TRADING_MODE}"

# ── Credentials per environment ───────────────────────────
_KEYS = {
    "testnet": (
        os.environ.get("BINANCE_TESTNET_API_KEY",    ""),
        os.environ.get("BINANCE_TESTNET_API_SECRET",  ""),
    ),
    "demo": (
        os.environ.get("BINANCE_DEMO_API_KEY",       ""),
        os.environ.get("BINANCE_DEMO_API_SECRET",     ""),
    ),
    "live": (
        os.environ.get("BINANCE_LIVE_API_KEY",        ""),
        os.environ.get("BINANCE_LIVE_API_SECRET",     ""),
    ),
}

# Legacy single-key support (backwards compat with existing .env)
_legacy_key    = os.environ.get("BINANCE_API_KEY",    "")
_legacy_secret = os.environ.get("BINANCE_API_SECRET", "")

BINANCE_API_KEY, BINANCE_API_SECRET = _KEYS[TRADING_MODE]
if not BINANCE_API_KEY and _legacy_key:
    BINANCE_API_KEY    = _legacy_key
    BINANCE_API_SECRET = _legacy_secret

# ── Endpoint URLs per environment ─────────────────────────
_ENDPOINTS = {
    "testnet": {
        "spot_rest":     "https://testnet.binance.vision",
        "futures_rest":  "https://testnet.binancefuture.com",
        "spot_ws":       "wss://testnet.binance.vision/ws",
        "futures_ws":    "wss://stream.binancefuture.com/ws",
    },
    "demo": {
        "spot_rest":     "https://demo-api.binance.com",
        "futures_rest":  "https://testnet.binancefuture.com",   # no demo futures yet
        "spot_ws":       "wss://demo-stream.binance.com/ws",
        "futures_ws":    "wss://stream.binancefuture.com/ws",
    },
    "live": {
        "spot_rest":     "https://api.binance.com",
        "futures_rest":  "https://fapi.binance.com",
        "spot_ws":       "wss://stream.binance.com:9443/ws",
        "futures_ws":    "wss://fstream.binance.com/ws",
    },
}

_ep = _ENDPOINTS[TRADING_MODE]
SPOT_BASE_URL    = _ep["spot_rest"]
FUTURES_BASE_URL = _ep["futures_rest"]
SPOT_WS_URL      = _ep["spot_ws"]
FUTURES_WS_URL   = _ep["futures_ws"]

# Public market data always fetched from live Binance API.
# Demo and testnet simulate orders at real prices — their public endpoints
# may return limited or stale data, so all unauthenticated data reads
# (klines, ticker, depth, scanner) come from the real exchange.
PUBLIC_DATA_URL = "https://api.binance.com"

# Convenience flags
USE_TESTNET = TRADING_MODE == "testnet"
USE_DEMO    = TRADING_MODE == "demo"
USE_LIVE    = TRADING_MODE == "live"

# ── Symbol & interval ─────────────────────────────────────
SYMBOL       = "BTCUSDT"
INTERVAL     = "15m"
CANDLE_LIMIT = 200

# ── Shared equity pool ────────────────────────────────────
# All agents draw from the same USDT balance on this Binance account.
# SHARED_EQUITY_MODE must be True for live/demo — agents coordinate
# through equity_pool.py to prevent over-deployment.
#
# FLEET_SIZE:    total number of agent slots sharing the pool (fixed at 4).
#               Used by get_budget() to divide the pool evenly even when
#               not all slots are currently active, avoiding one agent
#               monopolising equity while siblings are starting up.
#
# MAX_EXPOSURE:  maximum fraction of total equity the entire fleet may
#               have deployed at once (across all open positions combined).
#
# MAX_TRADE_PCT: maximum fraction of total equity a single agent may
#               deploy in one position.
#               Each agent's effective budget = min(
#                   total_equity × MAX_TRADE_PCT / FLEET_SIZE,   ← per-slot share
#                   total_equity × MAX_EXPOSURE  − other_open     ← pool headroom
#               )
SHARED_EQUITY_MODE = True
FLEET_SIZE         = int(os.environ.get("DIPU_FLEET_SIZE", 4))

# ── Per-mode risk parameters (testnet == demo; live can differ) ──
_RISK = {
    #              risk_pct  max_trade  exposure  leverage  daily_dd  monthly_dd  consec_loss
    "testnet": (   0.02,     0.08,      0.30,     3,        0.10,     0.15,       3 ),
    "demo":    (   0.02,     0.08,      0.30,     3,        0.10,     0.15,       3 ),
    "live":    (   0.10,     0.70,      0.70,     3,        0.10,     0.15,       3 ),
}
(RISK_PCT, MAX_TRADE_PCT, MAX_EXPOSURE, MAX_LEVERAGE,
 DAILY_DD_LIMIT, MONTHLY_DD_LIMIT, MAX_CONSEC_LOSS) = _RISK[TRADING_MODE]

# ── Entry filters ─────────────────────────────────────────
MAX_SPREAD_PCT  = 0.003    # 0.30%
MIN_VOLUME_USDT = 15_000_000  # $15M 24h volume — filters out micro-caps that spike and reverse
MIN_PRICE       = 0.50    # Solution 2: raised from $0.05 — below $0.50, tick size eats into
                          # the 0.15% fee break-even (XPLUSDT at $0.08 = 0.125%/tick)
MAX_FUNDING     = 0.0010   # 0.1% per 8h — spec aligned
MAX_SLIPPAGE    = 0.0010   # 0.1% — spec aligned

# ── Indicators ────────────────────────────────────────────
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.0   # 1× ATR stop: tighter entry, TPs reachable within 1-3 candles
ATR_TRAIL_MULT = 1.0   # spec aligned
RSI_PERIOD     = 14
RSI_EXIT_LONG  = 78    # spec aligned
RSI_EXIT_SHORT = 22    # spec aligned

# ── Take profits ──────────────────────────────────────────
TP1_R   = 1.5   # spec aligned — 1.5R
TP2_R   = 2.5   # spec aligned — 2.5R
TP3_R   = 4.0   # spec aligned — 4.0R
TP1_PCT = 0.33
TP2_PCT = 0.33
TP3_PCT = 0.34

# ── Time exits (5-min cycle checks; 2h target window) ─────
MAX_TRADE_HOURS_SPOT    = 2   # exit if trade flat after 2h
MAX_TRADE_HOURS_FUTURES = 2
CYCLE_SLEEP_SECONDS     = 60   # 1-minute update window

# ── Alerts ────────────────────────────────────────────────
ALERT_WEBHOOK = os.environ.get("DIPU_ALERT_WEBHOOK", "")

# ── Multi-agent instruction interface ─────────────────────
AUTHORIZED_AGENT_TOKENS = [
    t.strip() for t in os.environ.get("DIPU_AUTHORIZED_AGENT_TOKENS", "").split(",") if t.strip()
]
INSTRUCTION_SERVER_HOST = "0.0.0.0"
INSTRUCTION_SERVER_PORT = int(os.environ.get("AGENT_PORT", 7432))
