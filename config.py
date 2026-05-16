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

# Convenience flags
USE_TESTNET = TRADING_MODE == "testnet"
USE_DEMO    = TRADING_MODE == "demo"
USE_LIVE    = TRADING_MODE == "live"

# ── Symbol & interval ─────────────────────────────────────
SYMBOL       = "BTCUSDT"
INTERVAL     = "1h"
CANDLE_LIMIT = 200

# ── Per-mode risk parameters (testnet == demo; live can differ) ──
_RISK = {
    #              risk_pct  max_trade  exposure  leverage  daily_dd  monthly_dd  consec_loss
    "testnet": (   0.02,     0.08,      0.30,     5,        0.07,     0.20,       4 ),
    "demo":    (   0.02,     0.08,      0.30,     5,        0.07,     0.20,       4 ),
    "live":    (   0.02,     0.08,      0.30,     5,        0.07,     0.20,       4 ),
}
(RISK_PCT, MAX_TRADE_PCT, MAX_EXPOSURE, MAX_LEVERAGE,
 DAILY_DD_LIMIT, MONTHLY_DD_LIMIT, MAX_CONSEC_LOSS) = _RISK[TRADING_MODE]

# ── Entry filters ─────────────────────────────────────────
MAX_SPREAD_PCT  = 0.0020
MIN_VOLUME_USDT = 5_000_000
MAX_FUNDING     = 0.0015
MAX_SLIPPAGE    = 0.0015

# ── Indicators ────────────────────────────────────────────
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.2
ATR_TRAIL_MULT = 0.8
RSI_PERIOD     = 14
RSI_EXIT_LONG  = 80
RSI_EXIT_SHORT = 20

# ── Take profits ──────────────────────────────────────────
TP1_R   = 2.0
TP2_R   = 3.5
TP3_R   = 6.0
TP1_PCT = 0.33
TP2_PCT = 0.33
TP3_PCT = 0.34

# ── Time exits ────────────────────────────────────────────
MAX_TRADE_HOURS_SPOT    = 72
MAX_TRADE_HOURS_FUTURES = 12

# ── Alerts ────────────────────────────────────────────────
ALERT_WEBHOOK = os.environ.get("DIPU_ALERT_WEBHOOK", "")

# ── Multi-agent instruction interface ─────────────────────
AUTHORIZED_AGENT_TOKENS = [
    t.strip() for t in os.environ.get("DIPU_AUTHORIZED_AGENT_TOKENS", "").split(",") if t.strip()
]
INSTRUCTION_SERVER_HOST = "0.0.0.0"
INSTRUCTION_SERVER_PORT = int(os.environ.get("AGENT_PORT", 7432))
