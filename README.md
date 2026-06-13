# Raptor FleetEdge

> 20-year veteran persona. Sizes aggressively on pristine setups, sits flat when the market is ambiguous. Capital preservation is the floor, not the goal.

---

## Overview

Raptor FleetEdge is a multi-agent cryptocurrency trading system built for Binance Spot. A **fleet manager** spawns up to 4 independent trading agents, each watching a different coin. The system:

- Runs a live volatility-chase strategy on 15-minute candles
- Classifies market regime (TRENDING / RANGING / VOLATILE) and only enters on TRENDING
- Uses EMA 9/21/50 stack, RSI, MACD, ATR, Bollinger Bands, and VWAP
- Sizes positions dynamically based on ATR and account equity
- Manages exits with a 3-tier TP ladder aligned to the past 1-hour price range
- Agent 1 (slot 0) is permanently locked to BTCUSDT; the other three rotate to the best-ranked coins
- Liquidates base asset back to USDT automatically before every coin rotation (slots 1–3)
- Persists open positions across restarts — exact entry, stop, and TP values are restored
- Each agent exposes a live dashboard with 1-second candle charts, rule analyst panel, and order log
- Pre-screens every candidate coin for structural fee viability before assignment (coin health filter)

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

---

## Quick Start (Docker Compose)

### 1. Clone the repository

```bash
git clone https://github.com/dilisonline-sys/dipu-agent.git
cd dipu-agent   # repository folder name unchanged
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
The fleet auto-starts 5 seconds after the manager comes up — no manual spawn required.

### 4. Open the fleet dashboard

```
http://localhost:7430
```

---

## Accessing Dashboards

| URL | Description |
|---|---|
| `http://localhost:7430` | Agent Manager — fleet overview, spawn/stop controls |
| `http://localhost:7430/agent/fleetedge1/` | BTC agent live dashboard |
| `http://localhost:7430/agent/fleetedge2/` | Agent 2 dashboard |
| `http://localhost:7430/agent/fleetedge3/` | Agent 3 dashboard |
| `http://localhost:7430/agent/fleetedge4/` | Agent 4 dashboard |

Each agent dashboard includes:
- **1-second live candle chart** — builds from the SSE price stream in real time
- **EMA 9/21 overlay** — refreshed each trading cycle (~60s)
- **USDT balance + coin asset value** — live from Binance
- **Signal / regime / RSI / MACD** — current indicator readings
- **Open positions** with entry, stop, TP1/TP2 lines on chart
- **Portfolio card** — full account value: USDT + active spot positions + Simple Earn holdings
- **Rule Analyst panel** — indicator-driven advisory read, enable/disable per dashboard
- **Order log** — recent fills and open orders
- **Day / Night theme toggle** — persistent preference saved in `localStorage`

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

### Fee reduction (BNB)
Enable **Use BNB to pay for fees** in your Binance account settings. This reduces the fee rate from 0.10% to 0.075% per side (25% saving). Keep a small BNB balance in the spot wallet — the system does not manage BNB holdings automatically.

---

## Manual Docker Commands

If you prefer not to use Compose:

```bash
# Build the image
docker build -t raptor-fleetedge .

# Run the manager (starts on port 7430)
docker run -d \
  --name raptor-fleetedge \
  --restart unless-stopped \
  --env-file .env \
  -p 7430:7430 \
  -p 7434:7434 \
  -p 7435:7435 \
  -p 7436:7437 \
  -v rfe_logs:/tmp \
  raptor-fleetedge

# View logs
docker logs -f raptor-fleetedge

# Stop
docker stop raptor-fleetedge && docker rm raptor-fleetedge
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
  -d '{"name":"fleetedge1","mode":"live","port":7434,"symbol":"BTCUSDT","slot":0}'

# Spawn an auto-rotating agent (slot 1)
curl -X POST http://localhost:7430/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"name":"fleetedge2","mode":"live","port":7435,"symbol":"ETHUSDT","slot":1}'
```

Stop an agent:

```bash
curl -X POST http://localhost:7430/api/stop \
  -H "Content-Type: application/json" \
  -d '{"name":"fleetedge2"}'
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
curl -X POST http://localhost:7430/agent/fleetedge1/instruction \
  -H "Content-Type: application/json" \
  -H "X-Agent-Token: internal" \
  -d '{"action":"SWITCH_MODE","mode":"live","source":"manual"}'
```

---

## Coin Selection & Health Screening

### Market Scanner

The background scanner ranks all Binance USDT pairs every 5 minutes. Coins are scored in two steps:

1. **Quick rank** — 24h ticker universe filter (volume ≥ $15M, price ≥ $0.50, spread ≤ 0.30%, 1h move ≥ 0.5%)
2. **Deep score** — 15m OHLCV indicators: ATR volatility bell curve × trend alignment × RSI zone

### Coin Health Filter

Before a candidate is deep-scored, `coin_health.py` runs four structural checks to reject coins that are **structurally unable to beat the 0.15% round-trip fee**:

| Check | Condition | Example failure |
|---|---|---|
| `LOW_PRICE` | Price < $0.50 | XPLUSDT at $0.08 — one tick is 0.125% of price |
| `LOW_DAILY` | 24h range < 1.0% (1.5% for BTC) | BTC on a flat day with only 0.9% total range |
| `TICK_TRAP` | Min tick ≥ 50% of 15m ATR | Coin cannot produce enough per-candle movement to cover fees |
| `FLAT_MOVER` | < 2 of last 4 candles had body > 0.075% | Rolling history shows coin has been statistically unable to beat fees |

Results are cached 5 minutes per coin to avoid redundant API calls. Coins that fail any check are excluded from the ranked list and logged as `HEALTH_EXCLUDED`.

### Coin Rotation

Agents in slots 1–3 automatically rotate to the best-ranked coin when:

| Trigger | Description |
|---|---|
| Scanner pick | Background scanner finds a higher-ranked coin with no open position |
| Quality gate escape | 2 consecutive data quality fails on the current coin |
| Ranging escape | Current coin classified RANGING — rotates to next ranked coin |
| Volatile escape | Current coin VOLATILE for 10+ minutes — forces rotation |
| Signal rotate | No tradeable setup for 5 consecutive cycles |
| SELL with no position | Bear signal on a coin we don't hold — rotates immediately |
| Manual `SWITCH_COIN` | Operator-requested switch via instruction endpoint |
| Manual `FORCE_BTC` | Forces switch to BTCUSDT |

**Before every rotation** (slots 1–3 only), the agent automatically sells any remaining base asset back to USDT so the equity pool always has liquid capital for the next entry.

**Slot 0 (BTC agent) never rotates and never sells** — it stays in BTCUSDT permanently.

---

## Entry Gates

Every BUY signal passes through a chain of gates before an order is placed. Any gate failure cancels the entry for that cycle and is logged to the dashboard:

| Gate | Condition | Log tag |
|---|---|---|
| Cooldown | 30-minute re-entry cooldown after last fill on same coin | `[COOLDOWN]` |
| BTC flat-day | BTC 24h range < 1.5% — flat market, fees consume profit | `GATE_BLOCK_BTC_FLAT` |
| TP distance | TP1 distance < 0.22% of price (fees + margin) | `GATE_BLOCK_TP` |
| R:R ratio | Reward < 1.5× risk at current ATR | `GATE_BLOCK_RR` |
| Fear & Greed | F&G index < 10 (full panic — no new longs) | `GATE_BLOCK_FG` |
| ATR cap | ATR > 3% of price — too volatile, stops blow on noise | (inline skip) |
| NN confirmation | Neural network up-probability < confidence floor (when model accuracy > 57%) | `NN.GATE_BLOCK` |
| Duplicate coin | Another slot already holds the same coin | `[SKIP] duplicate` |
| Pool budget | Order notional < $10 after equity pool allocation | `[SKIP] sizing aborted` |

---

## Signal Engine

Signals are computed from the **previous completed 15m candle** (not the live forming candle) to avoid entry on transient tick noise.

| Signal | Required conditions |
|---|---|
| `BUY` (momentum) | EMA9 > EMA21 > EMA50 AND RSI < 70 AND MACD > signal AND price > EMA21 AND candle not bearish |
| `BUY` (pullback) | EMA stack bullish AND RSI < 35 AND price > EMA50 |
| `BUY` (BB bounce) | Price at/below lower BB AND RSI < 32 AND bullish EMA stack |
| `SELL` (momentum) | EMA stack bearish AND RSI > 30 AND MACD < signal AND price < EMA21 AND bullish candle |
| `SELL` (pullback) | EMA stack bearish AND RSI > 65 AND price < EMA21 |

The regime classifier (`regime.py`) gates which signals are valid:
- **TRENDING**: all signals active
- **RANGING / VOLATILE**: no new entries — wait for regime change or rotate

---

## Risk Engine & Halt System

### Automatic halts

| Trigger | Halt duration | Reason logged |
|---|---|---|
| Daily drawdown ≥ 10% | 4 hours | `daily drawdown X% (start $Y → now $Z)` |
| Monthly drawdown ≥ 15% | 30 days | `monthly drawdown X%` |
| N consecutive losses (default 3) | 4 hours | `3 consecutive losses` |

The halt reason is always displayed in the agent dashboard log panel so the cause is never ambiguous.

### Transient equity glitch protection

`update_metrics` is skipped if the reported equity is less than 30% of the day-start equity — this prevents a Binance API pricing glitch (e.g. a coin price returned as 0) from triggering a false drawdown halt.

### Halts are only evaluated after a position closes

The risk engine is only called when a trade actually exits. It is never called on idle cycles, preventing compounding halt triggers during periods of no trading activity.

### RESUME behaviour

Sending `RESUME` via instruction:
1. Clears `halt_flag` and `halt_until`
2. Resets `consec_losses` to 0 (prevents re-halt on the next losing trade)
3. Logs the previous halt reason so the dashboard shows what was resolved

---

## Position Sizing

Position size is calculated as:

```
risk_amount = equity × RISK_PCT          # 10% of equity for live
stop_dist   = ATR × ATR_STOP_MULT        # 1.0× ATR (tight stop)
raw_qty     = risk_amount / stop_dist

budget      = min(per_slot_share, pool_headroom)
order_usdt  = min(raw_qty × price, budget, equity × MAX_TRADE_PCT)
```

If the per-slot equity share falls below the Binance $10 minimum order, the system falls back to the full available pool headroom rather than silently skipping the trade.

### TP ladder

| Level | Distance from entry | Close fraction |
|---|---|---|
| TP1 | 1.5× ATR (or 40% of 1h range, whichever is larger) | 33% |
| TP2 | 2.5× ATR | 33% |
| TP3 | 4.0× ATR | 34% |

---

## Email Notifications

Raptor FleetEdge can send email alerts for key events. Configure it from the manager dashboard at `http://localhost:7430` (scroll to the **Email Notifications** panel) or via the API.

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
3. Create an App Password — name it `raptor-fleetedge`
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
| `RESUME` | Resume after halt (also resets consecutive-loss counter) |
| `RESET_DAY_START` | Reset risk baselines to current equity — use after `RESUME` to prevent immediate re-halt |
| `SWITCH_COIN` | Switch to a specific symbol (e.g. `"symbol":"SOLUSDT"`) |
| `RESUME_AUTO` | Return to scanner-driven coin selection |
| `FORCE_BTC` | Switch to BTCUSDT immediately |
| `ANALYST_ON` | Enable the Rule Analyst |
| `ANALYST_OFF` | Disable the Rule Analyst |

Example — halt BTC agent:

```bash
curl -X POST http://localhost:7430/agent/fleetedge1/instruction \
  -H "Content-Type: application/json" \
  -H "X-Agent-Token: internal" \
  -d '{"action":"HALT","source":"manual"}'
```

---

## Logs

Agent logs are written to `/tmp/rfe_<name>.log` inside the container (JSON Lines format).

```bash
# Stream BTC agent log
docker exec raptor-fleetedge tail -f /tmp/rfe_fleetedge1.log | python3 -m json.tool

# Or with the named volume mounted, on the host:
docker run --rm -v rfe_logs:/tmp alpine tail -f /tmp/rfe_fleetedge1.log
```

---

## Rule Analyst (Optional)

Each dashboard shows a **Rule Analyst** panel. It analyses current market indicators every 3 minutes using a deterministic rule engine (no external API) and returns:

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
- [ ] Daily drawdown halt is set appropriately (`DAILY_DD_LIMIT` in `config.py`)
- [ ] Starting equity is the amount you are comfortable losing entirely
- [ ] BNB balance held in spot wallet to receive 25% fee discount
- [ ] You have read and understood the risk parameters in `config.py`

---

## Key Configuration Parameters (`config.py`)

| Parameter | Default (live) | Description |
|---|---|---|
| `RISK_PCT` | `0.10` | Fraction of equity risked per trade |
| `MAX_TRADE_PCT` | `0.70` | Max fraction of equity deployed in one position |
| `MAX_EXPOSURE` | `0.70` | Max fraction of total equity deployed fleet-wide |
| `FLEET_SIZE` | `4` | Number of agent slots sharing the pool |
| `ATR_STOP_MULT` | `1.0` | Stop distance = 1× ATR from entry |
| `TP1_R` | `1.5` | TP1 = 1.5× stop distance |
| `TP2_R` | `2.5` | TP2 = 2.5× stop distance |
| `TP3_R` | `4.0` | TP3 = 4.0× stop distance |
| `DAILY_DD_LIMIT` | `0.10` | Halt after 10% daily drawdown |
| `MONTHLY_DD_LIMIT` | `0.15` | Halt after 15% monthly drawdown |
| `MAX_CONSEC_LOSS` | `3` | Halt after 3 consecutive losing trades |
| `MIN_PRICE` | `0.50` | Reject coins below $0.50 (tick-size protection) |
| `MIN_VOLUME_USDT` | `15,000,000` | Reject coins with < $15M 24h volume |
| `CYCLE_SLEEP_SECONDS` | `60` | Seconds between trading loop iterations |

---

## File Reference

| File | Role |
|---|---|
| `agent.py` | Main trading loop — orchestrates all modules |
| `agent_manager.py` | Fleet manager — spawns/proxies/monitors agents, auto-starts fleet on boot |
| `coin_health.py` | Pre-assignment structural fitness filter — rejects coins that cannot beat fees |
| `market_data.py` | Feeds, order book, indicators (RSI, EMA, MACD, ATR, BB, VWAP) |
| `market_scanner.py` | Ranks coins by volatility/momentum score; integrates health filter |
| `sizing.py` | Position sizing with volatility and sentiment adjustment |
| `order_manager.py` | Order submission, retries, fill tracking, Simple Earn value fetcher |
| `exit_manager.py` | Hard stops, break-even, trailing stops, TP1/2/3 ladder |
| `risk_engine.py` | Drawdown tracking, kill switch, halt reason logging, alerts |
| `regime.py` | TRENDING / RANGING / VOLATILE classification |
| `equity_pool.py` | Shared pool — coordinates budgets, coin exclusions, earn value across agents |
| `portfolio_tracker.py` | Full account P&L — USDT + spot positions + Simple Earn (Flexible) holdings |
| `agent_monitor.py` | 30-minute health check with auto-resolution — resumes halted agents, resets baselines, emails report |
| `rule_analyst.py` | Indicator-driven advisory analyst |
| `rule_coin_selector.py` | Rule-based profitability coin selector |
| `nn_predictor.py` | Pure-NumPy MLP — trains on live candle data to confirm entry direction |
| `email_notifier.py` | Email alerts — fills, rotations, 4h P&L reports |
| `instruction_server.py` | Per-agent HTTP dashboard and instruction endpoint |
| `config.py` | All parameters in one place |

---

## Portfolio Tracking

The `portfolio_tracker` module maintains a real-time view of the complete account value:

```
total_assets = usdt_free + spot_positions + simple_earn_holdings
```

- **USDT free** — spendable balance, updated every 30 s by each agent's background pusher
- **Spot positions** — sum of each slot's `open_usdt` (active coin valued at live price)
- **Simple Earn** — all `LD`-prefixed Flexible Savings positions priced at market; fetched every 5 min by slot 0 and published to the shared pool

This value is shown in the **PORTFOLIO card** on every agent dashboard and included in all monitor emails. Day P&L is calculated against the day-start baseline (`/tmp/rfe_portfolio_day.json`), which resets each UTC calendar day.

---

## Health Monitor

`agent_monitor.py` runs an independent health check every 30 minutes. It **auto-resolves halts** before reporting, then emails a full fleet summary.

### Auto-resolution logic

Before each check the monitor queries every agent's live state via HTTP. If an agent is halted but today's daily drawdown is below 5% (i.e. it's a new UTC day and the drawdown counter has reset):

1. Sends `RESUME` to clear the halt flag and reset consecutive-loss counter
2. Waits 2 seconds, then sends `RESET_DAY_START` to reset the risk engine baselines to current equity — this prevents the agent from immediately re-halting on its next metrics update
3. Resets `/tmp/rfe_portfolio_day.json` so future restarts also use the correct baseline

If today's drawdown is still ≥ 5% the agent is left halted (active loss situation — the safeguard should hold).

### Email report contents

- **Status** — `ALL OK`, `N agent(s) auto-resumed`, or `N issue(s) detected`
- Agent state per slot (RUNNING / HALTED / LOOP_ERR)
- Per-slot open positions and daily P&L
- Full portfolio breakdown (USDT + Spot + Earn)
- Auto-resolution actions taken (✅ RESUMED / ⚠ SKIPPED with reason)
- Any remaining risk alerts or anomalies

Configure the monitor with the same email settings used for trade notifications.

---

## Risk Disclaimer

This software trades real cryptocurrency. Past performance does not guarantee future results. Only use funds you can afford to lose entirely. The authors take no responsibility for financial losses.
