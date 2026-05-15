import os
from dotenv import load_dotenv

load_dotenv()

# ── Identity ──────────────────────────────────────────────
AGENT_NAME = "dipu"
AGENT_VERSION = "1.0"

# ── Exchange ──────────────────────────────────────────────
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
USE_TESTNET        = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"

SPOT_BASE_URL      = "https://testnet.binance.vision"    if USE_TESTNET else "https://api.binance.com"
FUTURES_BASE_URL   = "https://testnet.binancefuture.com" if USE_TESTNET else "https://fapi.binance.com"
SPOT_WS_URL        = "wss://testnet.binance.vision/ws"   if USE_TESTNET else "wss://stream.binance.com:9443/ws"
FUTURES_WS_URL     = "wss://stream.binancefuture.com/ws" if USE_TESTNET else "wss://fstream.binance.com/ws"

SYMBOL   = "BTCUSDT"
INTERVAL = "1h"
CANDLE_LIMIT = 200

# ── Dipu's high-risk / high-reward parameters ─────────────
# 20-year vet: pushes limits but never blows the account.
RISK_PCT        = 0.02   # 2% per trade (vs conservative 1%)
MAX_TRADE_PCT   = 0.08   # 8% equity max single trade
MAX_EXPOSURE    = 0.30   # 30% total open exposure
MAX_LEVERAGE    = 5      # up to 5× on futures
DAILY_DD_LIMIT  = 0.07   # 7% daily halt threshold
MONTHLY_DD_LIMIT= 0.20   # 20% monthly halt
MAX_CONSEC_LOSS = 4      # halt after 4 consecutive losses

# ── Entry Filters ─────────────────────────────────────────
MAX_SPREAD_PCT  = 0.0020  # 0.20% — dipu tolerates wider spreads
MIN_VOLUME_USDT = 5_000_000
MAX_FUNDING     = 0.0015  # 0.15% per 8h
MAX_SLIPPAGE    = 0.0015  # 0.15%

# ── Indicators ────────────────────────────────────────────
ATR_PERIOD      = 14
ATR_STOP_MULT   = 1.2    # tighter initial stop for better R:R
ATR_TRAIL_MULT  = 0.8
RSI_PERIOD      = 14
RSI_EXIT_LONG   = 80
RSI_EXIT_SHORT  = 20

# ── Take Profits (aggressive ladder) ─────────────────────
TP1_R   = 2.0   # higher TP1 for better expectancy
TP2_R   = 3.5
TP3_R   = 6.0
TP1_PCT = 0.33
TP2_PCT = 0.33
TP3_PCT = 0.34

# ── Time Exits ────────────────────────────────────────────
MAX_TRADE_HOURS_SPOT    = 72
MAX_TRADE_HOURS_FUTURES = 12

# ── Alerts ────────────────────────────────────────────────
ALERT_WEBHOOK = os.environ.get("DIPU_ALERT_WEBHOOK", "")

# ── Multi-agent instruction interface ─────────────────────
# Dipu accepts trade signals from authorized external agents/bots.
# The Binance account owner (login) is intentionally NOT in this list
# — trade instructions must originate from authorized signal sources only.
AUTHORIZED_AGENT_TOKENS = [
    t.strip() for t in os.environ.get("DIPU_AUTHORIZED_AGENT_TOKENS", "").split(",") if t.strip()
]
INSTRUCTION_SERVER_HOST = "0.0.0.0"
INSTRUCTION_SERVER_PORT = 7432
