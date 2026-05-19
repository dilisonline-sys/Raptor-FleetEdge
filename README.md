# dipu — Crypto Trading Agent Fleet

> 20-year veteran persona. Sizes aggressively on pristine setups, sits flat when the market is ambiguous. Capital preservation is the floor, not the goal.

---

## Overview

dipu is a multi-agent cryptocurrency trading system built for Binance Spot. A **fleet manager** spawns up to 4 independent trading agents, each watching a different coin. The system:

- Runs a live volatility-chase strategy on 15-minute candles
- Classifies market regime (TRENDING / RANGING / VOLATILE) and only enters on TRENDING
- Uses EMA 9/21/50 stack, RSI, MACD, ATR, Bollinger Bands, and VWAP
- Sizes positions dynamically based on ATR and account equity
- Manages exits with a 3-tier TP ladder aligned to the past 1-hour price range
- Agent 1 (slot 0) is permanently locked to BTCUSDT; the other three rotate to the best-ranked coins
- Liquidates base asset back to USDT automatically before every coin rotation (slots 1–3)
- Persists open positions across restarts — exact entry, stop, and TP values are restored
- Each agent exposes a live dashboard with 1-second candle charts, AI analyst panel, and order log

---

## Architecture

```
                        ┌─────────────────────────────┐
                        │   Agent Manager  :7430       │
                        │   Fleet overview dashboard   │
                        └───────────┬─────────────────┘
               ┌───────────┬────────┴────────┬───────────┐
               ▼           ▼                 ▼           ▼
         Agent 0       Agent 1           Agent 2     Agent 3
         :7434         :7435             :7436       :7437
         BTCUSDT       top coin          top coin    top coin
         (locked)      (auto-rotate)     (auto)      (auto)
```

Each agent runs an independent `agent.py` process. The manager proxy forwards `/agent/<name>/*` requests to the correct agent port.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Docker | 24+ |
| Docker Compose | v2 (bundled with Docker Desktop or `docker compose` plugin) |
| Binance account | Spot trading API key |
| Anthropic API key | Optional — for AI Analyst feature |

---

## Quick Start (Docker Compose)

### 1. Clone the repository

```bash
git clone https://github.com/dilisonline-sys/dipu-agent.git
cd dipu-agent
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```env
# Trading mode: testnet | demo | live
TRADING_MODE=demo

# Testnet credentials (testnet.binance.vision — free, no real funds)
BINANCE_TESTNET_API_KEY=your_testnet_key
BINANCE_TESTNET_API_SECRET=your_testnet_secret

# Demo credentials (demo-api.binance.com — virtual money, live prices)
BINANCE_DEMO_API_KEY=your_demo_key
BINANCE_DEMO_API_SECRET=your_demo_secret

# Live credentials (api.binance.com — REAL FUNDS)
# WARNING: use Trade + Read permissions only — NEVER enable withdrawal
BINANCE_LIVE_API_KEY=your_live_key
BINANCE_LIVE_API_SECRET=your_live_secret

# Claude AI Analyst (optional — enables the per-dashboard advisory AI)
ANTHROPIC_API_KEY=your_anthropic_key

# Webhook for alerts (optional — Discord/Slack/Telegram URL)
DIPU_ALERT_WEBHOOK=

# Comma-separated tokens for external agent-to-agent POST calls
DIPU_AUTHORIZED_AGENT_TOKENS=
```

### 3. Build and start

```bash
docker compose up -d --build
```

This builds the image, starts the manager, and makes it available at **http://localhost:7430**.

### 4. Open the fleet dashboard

```
http://localhost:7430
```

From there, click **Spawn Fleet** to launch all 4 agents. They will auto-assign coins: slot 0 always takes BTCUSDT, slots 1–3 take the top 3 ranked altcoins.

---

## Accessing Dashboards

| URL | Description |
|---|---|
| `http://localhost:7430` | Agent Manager — fleet overview, spawn/stop controls |
| `http://localhost:7430/agent/dipu-live/` | BTC agent live dashboard |
| `http://localhost:7430/agent/dipu-live-1/` | Agent 2 dashboard |
| `http://localhost:7430/agent/dipu-live-2/` | Agent 3 dashboard |
| `http://localhost:7430/agent/dipu-live-3/` | Agent 4 dashboard |

Each agent dashboard includes:
- **1-second live candle chart** — builds from the SSE price stream in real time
- **EMA 9/21 overlay** — refreshed each trading cycle (~60s)
- **USDT balance + coin asset value** — live from Binance
- **Signal / regime / RSI / MACD** — current indicator readings
- **Open positions** with entry, stop, TP1/TP2 lines on chart
- **AI Analyst panel** — enable/disable per dashboard (requires `ANTHROPIC_API_KEY`)
- **Order log** — recent fills and open orders

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `TRADING_MODE` | Yes | `testnet`, `demo`, or `live` |
| `BINANCE_TESTNET_API_KEY` | If testnet | From testnet.binance.vision |
| `BINANCE_TESTNET_API_SECRET` | If testnet | |
| `BINANCE_DEMO_API_KEY` | If demo | From demo-api.binance.com |
| `BINANCE_DEMO_API_SECRET` | If demo | |
| `BINANCE_LIVE_API_KEY` | If live | From api.binance.com — Trade + Read only |
| `BINANCE_LIVE_API_SECRET` | If live | |
| `ANTHROPIC_API_KEY` | No | Enables the Claude AI Analyst feature |
| `DIPU_ALERT_WEBHOOK` | No | Discord/Slack webhook for trade alerts |
| `DIPU_AUTHORIZED_AGENT_TOKENS` | No | Tokens for external agent POST access |

---

## Binance API Key Setup

### Testnet (recommended for first run)
1. Go to [testnet.binance.vision](https://testnet.binance.vision)
2. Log in with GitHub
3. Generate API key — testnet keys have full permissions by default

### Demo (live prices, virtual funds)
1. Log in to [binance.com](https://www.binance.com)
2. Go to **API Management** → Create API
3. Enable **Enable Spot & Margin Trading**
4. No IP restriction needed for demo

### Live (real funds)
1. Go to **API Management** → Create API
2. Enable **Enable Reading** and **Enable Spot & Margin Trading**
3. **Do NOT enable withdrawals**
4. Restrict to your server IP for security
5. Set `TRADING_MODE=live` only after at least 2 weeks of successful demo trading

---

## Manual Docker Commands

If you prefer not to use Compose:

```bash
# Build the image
docker build -t dipu-agent .

# Run the manager (starts on port 7430)
docker run -d \
  --name dipu \
  --restart unless-stopped \
  --env-file .env \
  -p 7430:7430 \
  -p 7434:7434 \
  -p 7435:7435 \
  -p 7436:7436 \
  -p 7437:7437 \
  -v dipu_logs:/tmp \
  dipu-agent

# View logs
docker logs -f dipu

# Stop
docker stop dipu && docker rm dipu
```

---

## Spawning the Fleet via API

Once the manager is running, spawn all 4 agents programmatically:

```bash
curl -s -X POST http://localhost:7430/api/spawn-fleet \
  -H "Content-Type: application/json" | python3 -m json.tool
```

Or spawn a single agent manually:

```bash
# Spawn BTC agent (slot 0 — always BTCUSDT)
curl -X POST http://localhost:7430/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"name":"dipu-live","mode":"live","port":7434,"symbol":"BTCUSDT","slot":0}'

# Spawn an auto-rotating agent (slot 1)
curl -X POST http://localhost:7430/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"name":"dipu-live-1","mode":"live","port":7435,"symbol":"ETHUSDT","slot":1}'
```

Stop an agent:

```bash
curl -X POST http://localhost:7430/api/stop \
  -H "Content-Type: application/json" \
  -d '{"name":"dipu-live-1"}'
```

List all agents and their status:

```bash
curl -s http://localhost:7430/api/agents | python3 -m json.tool
```

---

## Trading Modes

| Mode | Funds | Prices | Use for |
|---|---|---|---|
| `testnet` | Virtual (testnet) | Testnet (lagged) | First-time setup validation |
| `demo` | Virtual | Live Binance | Strategy testing before going live |
| `live` | Real | Live Binance | Live trading |

Switch mode for a running agent from the dashboard (Mode button) or via API:

```bash
curl -X POST http://localhost:7430/agent/dipu-live/instruction \
  -H "Content-Type: application/json" \
  -H "X-Agent-Token: internal" \
  -d '{"action":"SWITCH_MODE","mode":"live","source":"manual"}'
```

---

## Coin Rotation & Liquidation

Agents in slots 1–3 automatically rotate to the best-ranked coin when:

| Trigger | Description |
|---|---|
| Scanner pick | Background scanner finds a higher-ranked coin with no open position |
| Quality gate escape | 2 consecutive data quality fails on the current coin |
| Ranging escape | Current coin classified RANGING — immediately rotates to next ranked coin |
| Volatile escape | Current coin VOLATILE for 10+ minutes — forces rotation |
| Signal rotate | No tradeable setup for 5 consecutive cycles |
| SELL with no position | Bear signal on a coin we don't hold — rotates immediately |
| Manual `SWITCH_COIN` | Operator-requested switch via instruction endpoint |
| Manual `FORCE_BTC` | Forces switch to BTCUSDT |

**Before every rotation** (slots 1–3 only), the agent automatically sells any remaining base asset back to USDT so the equity pool always has liquid capital for the next entry.

**Slot 0 (BTC agent) never rotates and never sells** — it stays in BTCUSDT permanently.

---

## Email Notifications

dipu can send email alerts for key events. Configure it from the manager dashboard at `http://localhost:7430` (scroll to the **Email Notifications** panel) or via the API.

### What gets notified

| Event | Toggle |
|---|---|
| Coin rotation | Any time an agent switches to a new coin (with reason) |
| Order fills | BUY entries, SELL exits, TP1/TP2 partial closes |
| Coin traded | When the active coin changes |
| 4h P&L report | Automatically every 4 hours — full fleet summary |

### Gmail setup (recommended)

1. Enable **2-Step Verification** on your Google account
2. Go to **myaccount.google.com/apppasswords**
3. Create an App Password — name it `dipu`
4. Copy the 16-character password (format: `xxxx xxxx xxxx xxxx`)

### Configure via dashboard

Open `http://localhost:7430`, scroll to **Email Notifications**, fill in:
- **Recipient** — address to receive alerts
- **Sender (Gmail)** — the Gmail address sending the emails
- **App password** — the 16-char app password from step above
- Select which events to be notified about
- Check **Enable email notifications** and click **Save**
- Click **Test email** to verify delivery

### Configure via API

```bash
curl -X POST http://localhost:7430/api/email-config \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "recipient": "you@example.com",
    "smtp_user": "sender@gmail.com",
    "smtp_password": "xxxx xxxx xxxx xxxx",
    "notifications": {
      "coin_rotation": true,
      "order_fills":   true,
      "coin_traded":   true,
      "pnl_report":    true
    }
  }'
```

Send a test email:

```bash
curl -X POST http://localhost:7430/api/email-test \
  -H "Content-Type: application/json" \
  -d '{"recipient": "you@example.com"}'
```

Trigger a P&L report immediately:

```bash
curl -X POST http://localhost:7430/api/email-pnl
```

### Using other SMTP providers

The default is Gmail on port **465 (SSL)**. Port 587 (STARTTLS) is also supported — set `smtp_port` to `587` if your network requires it. For non-Gmail providers, set `smtp_host` and `smtp_port` in the config call above.

---

## Sending Instructions to Agents

Each agent accepts POST instructions at `/instruction`. Valid actions:

| Action | Description |
|---|---|
| `BUY` | Force a buy signal this cycle |
| `SELL` | Force a sell / close this cycle |
| `CLOSE_ALL` | Submit market sell and clear all positions |
| `HALT` | Emergency halt — stop trading |
| `RESUME` | Resume after halt |
| `SWITCH_COIN` | Switch to a specific symbol (e.g. `"symbol":"SOLUSDT"`) |
| `RESUME_AUTO` | Return to scanner-driven coin selection |
| `FORCE_BTC` | Switch to BTCUSDT immediately |
| `AI_ANALYST_ON` | Enable the Claude AI Analyst |
| `AI_ANALYST_OFF` | Disable the Claude AI Analyst |

Example — halt BTC agent:

```bash
curl -X POST http://localhost:7430/agent/dipu-live/instruction \
  -H "Content-Type: application/json" \
  -H "X-Agent-Token: internal" \
  -d '{"action":"HALT","source":"manual"}'
```

---

## Logs

Agent logs are written to `/tmp/dipu_<name>.log` inside the container (JSON Lines format).

```bash
# Stream BTC agent log
docker exec dipu tail -f /tmp/dipu_dipu-live.log | python3 -m json.tool

# Or with the named volume mounted, on the host:
docker run --rm -v dipu_logs:/tmp alpine tail -f /tmp/dipu_dipu-live.log
```

---

## AI Analyst (Optional)

When `ANTHROPIC_API_KEY` is set, each dashboard shows an **AI Analyst** panel. It uses Claude Haiku to analyse current market indicators every 3 minutes and returns:

- Sentiment (BULLISH / NEUTRAL / BEARISH) with confidence score
- Trend strength, momentum, key support/resistance levels
- Entry quality rating and risk level
- Plain-English market read and next-candle watch signal

The analyst is **advisory only** — it does not influence any trading decisions. Toggle it per-dashboard with the Enable/Disable button.

---

## Go-Live Checklist

Before switching to `TRADING_MODE=live`:

- [ ] Run at least 2 weeks on `demo` mode with positive results
- [ ] Binance API key has **Trade + Read only** — no withdrawal permission
- [ ] API key restricted to your server IP
- [ ] `.env` is not committed to git (it is in `.gitignore`)
- [ ] Daily drawdown halt is set appropriately (`DAILY_HALT_PCT` in `config.py`)
- [ ] Starting equity is the amount you are comfortable losing entirely
- [ ] You have read and understood the risk parameters in `config.py`

---

## File Reference

| File | Role |
|---|---|
| `agent.py` | Main trading loop — orchestrates all modules |
| `agent_manager.py` | Fleet manager — spawns/proxies/monitors agents |
| `market_data.py` | Feeds, order book, indicators (RSI, EMA, MACD, ATR, BB, VWAP) |
| `market_scanner.py` | Ranks coins by volatility/momentum score |
| `sizing.py` | Position sizing with volatility and sentiment adjustment |
| `order_manager.py` | Order submission, retries, fill tracking |
| `exit_manager.py` | Hard stops, break-even, trailing stops, TP1/2/3 ladder |
| `risk_engine.py` | Drawdown tracking, kill switch, alerts |
| `regime.py` | TRENDING / RANGING / VOLATILE classification |
| `equity_pool.py` | Shared pool — prevents sibling agents picking the same coin |
| `ai_analyst.py` | Claude Haiku advisory analyst |
| `email_notifier.py` | Email alerts — fills, rotations, 4h P&L reports |
| `instruction_server.py` | Per-agent HTTP dashboard and instruction endpoint |
| `config.py` | All parameters in one place |

---

## Risk Disclaimer

This software trades real cryptocurrency. Past performance does not guarantee future results. Only use funds you can afford to lose entirely. The authors take no responsibility for financial losses.
