# Raptor FleetEdge

> 20-year veteran persona. Sizes aggressively on pristine setups, sits flat when the market is ambiguous. Capital preservation is the floor, not the goal.

---

## Overview

Raptor FleetEdge is a multi-agent cryptocurrency trading system built for Binance Spot. A **fleet manager** spawns up to **5 independent trading agents**, each watching a different coin. The system:

- Runs one of **5 selectable strategies** per agent on 15-minute candles, switchable live from the dashboard
- Supports **1–3 simultaneous strategies per agent** — primary drives the signal, secondaries apply a consensus veto
- Classifies market regime (TRENDING / RANGING / VOLATILE) and filters entries accordingly
- Uses EMA 9/21/50 stack, RSI, MACD, ATR, Bollinger Bands, ADX, Stochastic RSI, Choppiness Index, and VWAP
- **Strategy Advisor** scores all 5 strategies against live indicators every cycle and highlights the best fit on the dashboard
- Sizes positions dynamically based on ATR and account equity
- Manages exits with a **staircase stop + 3-tier TP ladder**: stop locks to TP1 price after TP1 hits, locks to TP2 price after TP2 hits
- **Agent 1 (slot 0)** is permanently locked to BTCUSDT
- **Agents 2–4 (slots 1–3)** use momentum strategy and rotate to the best-ranked coins
- **Agent 5 (slot 4)** runs EMA Crossover strategy, permanently locked to ETHUSDT
- Liquidates base asset back to USDT automatically before every coin rotation (slots 1–3)
- Persists open positions across restarts — exact entry, stop, and TP values are restored
- Each agent exposes a live dashboard with real-time candle charts, toggleable indicator overlays (BB, EMA50, MACD, RSI, Volume, Forecast), rule analyst panel, and order log
- Pre-screens every candidate coin for structural fee viability before assignment (coin health filter)
- Neural network (pure NumPy MLP) augments entry decisions when trained OOS accuracy ≥ 58%

---

## Architecture

```
                        ┌──────────────────────────────┐
                        │   Agent Manager  :7430        │
                        │   Fleet overview dashboard    │
                        └──────┬──────────┬────────────┘
          ┌───────────┬─────────┴──┬──────┴──────┬───────────┐
          ▼           ▼            ▼              ▼           ▼
      Agent 0     Agent 1      Agent 2        Agent 3     Agent 4
      :7434       :7435        :7436          :7437       :7438
      BTCUSDT     top coin     top coin       top coin    ETHUSDT
      (locked)    (auto-rot)   (auto-rot)     (auto-rot)  EMA Cross
                  momentum     momentum       momentum    (locked)
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
git clone https://github.com/dilisonline-sys/Raptor-FleetEdge.git
cd Raptor-FleetEdge
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
sudo docker compose up -d --build
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
| `http://localhost:7430/agent/fleetedge1/` | Slot 0 — BTC agent live dashboard |
| `http://localhost:7430/agent/fleetedge2/` | Slot 1 — Momentum agent dashboard |
| `http://localhost:7430/agent/fleetedge3/` | Slot 2 — Momentum agent dashboard |
| `http://localhost:7430/agent/fleetedge4/` | Slot 3 — Momentum agent dashboard |
| `http://localhost:7430/agent/fleetedge5/` | Slot 4 — EMA Cross agent (ETHUSDT) |

Each agent dashboard includes:
- **1-second live candle chart** — builds from the SSE price stream in real time
- **EMA 9/21 overlay** — always visible; EMA 50 and Bollinger Bands toggled via indicator buttons
- **Toggleable indicator overlays** — BB, EMA50 on the main chart; MACD histogram, RSI 14, Volume, and Forecast projection as sub-panels below (MACD, BB, EMA50 are on by default)
- **Forecast sub-chart** — linear regression on last 20 candles projected 6 bars forward with ±1 ATR confidence bands
- **Strategy Advisor bar** — shows best-fit strategy and score (0–10) with plain-English reason; advised strategy is highlighted with a dashed border on the selector
- **Multi-strategy selector** — click up to 3 strategies simultaneously; primary (bright yellow) drives the signal, secondaries (gold outline) apply a consensus veto on opposing signals
- **USDT balance + coin asset value** — live from Binance
- **Signal / regime / RSI / MACD** — current indicator readings
- **Open positions** with entry, stop, TP1/TP2 price lines on chart (stop auto-advances with staircase logic)
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
| `DIPU_FLEET_SIZE` | No | Number of agent slots (default: 5) |

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
  -p 7436:7436 \
  -p 7437:7437 \
  -p 7438:7438 \
  -v rfe_logs:/tmp \
  raptor-fleetedge

# View logs
docker logs -f raptor-fleetedge

# Stop
docker stop raptor-fleetedge && docker rm raptor-fleetedge
```

---

## Spawning the Fleet via API

Once the manager is running, spawn all 5 agents programmatically:

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

# Spawn EMA Cross agent (slot 4 — always ETHUSDT)
curl -X POST http://localhost:7430/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"name":"fleetedge5","mode":"live","port":7438,"symbol":"ETHUSDT","slot":4,"strategy":"ema_cross"}'
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

Results are cached 5 minutes per coin to avoid redundant API calls. Coins that fail any check are excluded from the ranked list and logged as `HEALTH_EXCLUDED`. The health cache for a coin is **cleared before every rotation** so the new coin always gets a fresh check.

### Coin Rotation

Agents in slots 1–3 automatically rotate to the best-ranked coin when:

| Trigger | Description |
|---|---|
| Quality gate escape | 2 consecutive data quality fails on the current coin |
| Ranging escape | Current coin classified RANGING — rotates to next ranked coin |
| Volatile escape | Current coin VOLATILE for 10+ minutes — forces rotation |
| Signal rotate | No tradeable setup for 6 consecutive cycles (~6 minutes) |
| SELL with no position | Bear signal on a coin we don't hold — rotates immediately |
| Manual `SWITCH_COIN` | Operator-requested switch via instruction endpoint |
| Manual `FORCE_BTC` | Forces switch to BTCUSDT |

**Before every rotation** (slots 1–3 only), the agent automatically sells any remaining base asset back to USDT so the equity pool always has liquid capital for the next entry. The health cache for the departing coin is also invalidated so it gets a fresh score on re-entry.

**Slot 0 (BTC agent) never rotates and never sells** — it stays in BTCUSDT permanently.

**Slot 4 (EMA Cross) never rotates** — it is permanently locked to ETHUSDT and runs the crossover strategy, not the momentum strategy.

**ETHUSDT is excluded from the momentum slot 1–3 coin picker** — preventing double ETH exposure when the EMA cross agent is active.

---

## Entry Gates

Every BUY signal passes through a chain of gates before an order is placed. Any gate failure cancels the entry for that cycle and is logged to the dashboard:

| Gate | Condition | Log tag |
|---|---|---|
| Cooldown | 30-minute re-entry cooldown after last fill on same coin | `[COOLDOWN]` |
| BTC flat-session | BTC session range < 1.5% — flat market, fees consume profit | `GATE_BLOCK_BTC_FLAT` |
| TP distance | TP1 distance < 0.22% of price (fees + margin) | `GATE_BLOCK_TP` |
| R:R ratio | Reward < 1.5× risk at current ATR | `GATE_BLOCK_RR` |
| Fear & Greed | F&G index < 10 (full panic — no new longs) | `GATE_BLOCK_FG` |
| ATR cap | ATR > 3% of price — too volatile, stops blow on noise | (inline skip) |
| NN confirmation | Neural network up-probability < confidence floor (when model OOS accuracy ≥ 58% and ≥ 50 test samples) | `NN.GATE_BLOCK` |
| Duplicate coin | Another slot already holds the same coin | `[SKIP] duplicate` |
| Pool budget | Order notional < $10 after equity pool allocation | `[SKIP] sizing aborted` |

---

## Strategies

Each agent runs one **primary strategy** (drives entry signals) and optionally one or two **secondary strategies** (consensus veto only — if a secondary fires the opposite direction, the primary's signal is suppressed to NONE). Strategy is switchable live from the dashboard; changes take effect within 1–2 seconds.

### Strategy Advisor

Every cycle the **Strategy Advisor** scores all 5 strategies against the current market indicators (0–10 scale) and displays the best-fit recommendation on the dashboard. It does not auto-switch — it is advisory only. Scoring inputs:

| Indicator | Role in advisor |
|---|---|
| ADX 14 | Trend strength — >25 = trending, <15 = weak/ranging |
| Choppiness Index 14 | Market structure — <45 = trending, >58 = choppy |
| BB Width % | Squeeze detection — <2.5% signals imminent breakout |
| Volume Ratio | Current bar vs 20-bar SMA — >1.5 = surge confirmation |
| Stochastic RSI %K | Sensitive reversal signal |
| RSI 14 | Overbought/oversold level |
| Regime | TRENDING / RANGING / VOLATILE from RegimeClassifier |

### Available Strategies

| Key | Name | Best in | Entry condition summary |
|---|---|---|---|
| `momentum` | Volatility Chase | TRENDING, high ADX | EMA9>21>50 + MACD > signal + RSI < 70 |
| `ema_cross` | EMA Cross | TRENDING, ADX ≥ 18 | Golden/death cross on closed candles; filtered when ADX < 18 |
| `rsi_reversal` | RSI Mean Reversion | RANGING, Stoch-RSI extreme | RSI < 32 or > 68 with band-touch confirmation |
| `macd_cross` | MACD Crossover | TRENDING | MACD histogram cross with EMA alignment |
| `bb_breakout` | Bollinger Band Bounce | RANGING, squeeze + band touch | Price at/beyond BB edge after squeeze; RSI confirms |

Signals are computed from the **previous completed 15m candle** to avoid entry on transient tick noise.

### Multi-Strategy Consensus

When 2–3 strategies are active simultaneously:
- The **primary** (first selected, bright yellow button) computes the entry signal as normal
- Each **secondary** (gold outline button) independently evaluates the same indicators
- If any secondary returns the **opposite direction** (e.g. primary says BUY, secondary says SELL), the signal is vetoed to NONE
- Secondaries that return NONE do not veto — only an explicit opposite signal blocks entry

### EMA Cross — ADX Filter

The EMA Cross strategy applies an additional ADX gate: if ADX < 18 at signal time, the cross is classified as occurring in a choppy/ranging market and the signal is discarded. This prevents false crosses from generating entries when there is no trend to ride.

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

`update_metrics` is skipped if the reported equity is less than 30% of the day-start equity — this prevents a Binance API pricing glitch (e.g. a coin price returned as 0) from triggering a false drawdown halt. It is also skipped on idle cycles when a fresh post-close equity figure was already fetched — preventing duplicate metric updates from distorting the drawdown calculation.

### P&L recording

`risk.record_trade()` is called for **every realized P&L event** — full closes (stop, time, signal reversal), and TP1 / TP2 / TP3 partial closes. This ensures the consecutive-loss counter and drawdown ledger are always accurate, even when positions are only partially closed.

### RESUME behaviour

Sending `RESUME` via instruction:
1. Clears `halt_flag` and `halt_until`
2. Resets `consec_losses` to 0 (prevents re-halt on the next losing trade)
3. Logs the previous halt reason so the dashboard shows what was resolved

---

## Position Sizing

Position size is calculated as:

```
risk_amount = equity × RISK_PCT          # 1% of equity for live
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
| TP2 | 2.5× ATR (or 80% of 1h range, whichever is larger) | 33% |
| TP3 | 4.0× ATR (or 125% of 1h range, whichever is larger) | 34% (full remaining) |

All three levels are fully wired: TP3 fires a market sell for the remaining position and records P&L.

### Staircase Stop

After each TP level is hit the **stop is locked to that TP price**, preventing the remaining position from giving back secured profit:

- **TP1 hit** → stop moves to TP1 price (guaranteed break-even+ on remaining 67%)
- **TP2 hit** → stop moves to TP2 price (TP3 runner can never lose TP2 gains)

The staircase activates only if the new stop level is above (for BUY) or below (for SELL) the current stop — it never widens the stop.

---

## Email Notifications

Raptor FleetEdge can send email alerts for key events. Configure it from the manager dashboard at `http://localhost:7430` (scroll to the **Email Notifications** panel) or via the API.

### What gets notified

| Event | Toggle |
|---|---|
| Coin rotation | Any time an agent switches to a new coin (with reason) |
| Order fills | BUY entries, SELL exits, TP1/TP2/TP3 partial closes |
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
      "order_fills":   true,
      "coin_traded":   true,
      "pnl_report":    true
    }
  }'
```

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
| `SWITCH_MODE` | Switch trading mode (`testnet`/`demo`/`live`) — reloads API keys and recreates HTTP session |
| `SWITCH_COIN` | Switch to a specific symbol (e.g. `"symbol":"SOLUSDT"`) |
| `RESUME_AUTO` | Return to scanner-driven coin selection |
| `FORCE_BTC` | Switch to BTCUSDT immediately |
| `ANALYST_ON` | Enable the Rule Analyst |
| `ANALYST_OFF` | Disable the Rule Analyst |
| `SET_STRATEGY` | Switch active strategy — send `{"strategy":"momentum"}` (single) or `{"strategies":["momentum","rsi_reversal"]}` (multi, max 3); takes effect within 1–2 seconds |

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

| Parameter | Live default | Description |
|---|---|---|
| `RISK_PCT` | `0.01` | Fraction of equity risked per trade (1%) |
| `MAX_TRADE_PCT` | `0.15` | Max fraction of equity deployed in one position (15%) |
| `MAX_EXPOSURE` | `0.40` | Max fraction of total equity deployed fleet-wide (40%) |
| `FLEET_SIZE` | `5` | Number of agent slots sharing the pool |
| `ATR_STOP_MULT` | `1.5` | Stop distance = 1.5× ATR from entry (wider than 15m candle noise) |
| `TP1_R` | `1.5` | TP1 = 1.5× stop distance |
| `TP2_R` | `2.5` | TP2 = 2.5× stop distance |
| `TP3_R` | `4.0` | TP3 = 4.0× stop distance (remaining position exits here) |
| `MAX_TRADE_HOURS_SPOT` | `4` | Exit flat positions after 4h (TP2/TP3 need 3–6h to print on 15m) |
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
| `order_manager.py` | Order submission, retries, fill tracking, stop-limit placement, session management |
| `exit_manager.py` | Hard stops, break-even, trailing stops, TP1/TP2/TP3 ladder |
| `risk_engine.py` | Drawdown tracking, kill switch, halt reason logging, alerts |
| `regime.py` | TRENDING / RANGING / VOLATILE classification |
| `equity_pool.py` | Shared pool — coordinates budgets, coin exclusions, earn value across agents |
| `portfolio_tracker.py` | Full account P&L — USDT + spot positions + Simple Earn (Flexible) holdings |
| `agent_monitor.py` | 30-minute health check with auto-resolution — resumes halted agents, resets baselines, emails report |
| `rule_analyst.py` | Indicator-driven advisory analyst |
| `rule_coin_selector.py` | Rule-based profitability coin selector |
| `nn_predictor.py` | Pure-NumPy MLP — trains on live candle data to confirm entry direction |
| `ema_cross_module.py` | EMA crossover signal engine (golden/death cross on closed candles) |
| `rsi_reversal_module.py` | RSI mean-reversion signal engine |
| `macd_cross_module.py` | MACD histogram crossover signal engine |
| `bb_breakout_module.py` | Bollinger Band bounce signal engine |
| `strategy_advisor_module.py` | Scores all 5 strategies (0–10) against live indicators each cycle; powers dashboard advisor bar |
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

---

## Bug Fixes & Corrections Log

This section documents all defects identified and resolved across three independent expert audits.

### Round 1 — Core Infrastructure Fixes

| ID | File | Fix |
|---|---|---|
| FIX-1 | `order_manager.py` | `cancel_all()` silently cancelled the wrong pair — now accepts the active symbol as a parameter |
| FIX-2 | `risk_engine.py` | `record_trade()` was never called for break-even exits (pnl=0) — consecutive-loss counter could not be accurate |
| FIX-4 | `agent_manager.py` | EMA cross agent assigned BTCUSDT (same as slot 0) — changed to ETHUSDT to prevent double BTC exposure |
| FIX-5 | `agent.py` | EMA cross fee gate threshold corrected from 0.22% to 0.15% (tighter crossovers need lower threshold) |
| FIX-6 | `agent.py` | Partial fills cleared the full position; now checks `status` before clearing and re-tracks remaining qty |
| FIX-7 | `ema_cross_module.py` | Used live-forming candle indicators — phantom crossovers from intra-candle tick noise; switched to closed-candle indicators |
| FIX-8 | `market_data.py` | WS cache returned stale data after silent disconnect; added monotonic timestamp and 10s REST fallback |
| FIX-9 | `agent.py` | NN training ran synchronously in the async loop, blocking the cycle for several seconds; moved to `run_in_executor` |
| FIX-10 | `agent.py` | Orphan recovery assumed actual fill price was known; switched to synthetic position with widened stop and explicit warning |
| FIX-11 | `regime.py` | Regime changed every candle (no persistence); added 2-candle hysteresis — regime only confirms after 2 consecutive identical reads |
| FIX-12 | `agent.py` | Session-start equity reset on every container restart; now persisted to `/tmp/rfe_session_start_<slot>_<date>.json` |
| FIX-13 | `agent.py` | Background scanner started for EMA cross agents — wasted API rate limit since they never rotate; scanner now skipped for `strategy=ema_cross` |
| FIX-14 | `agent.py` | BTC flat-day guard used 24h rolling range (included previous session's move); switched to session-high/low from current session open |
| FIX-15 | `exit_manager.py` | TP2 quantity overcalculated — applied TP2_PCT to full original qty instead of remaining post-TP1 qty; corrected fractional calculation |
| FIX-16 | `market_data.py` | WS reconnect gave up after 4 failures; replaced with infinite reconnect with capped exponential backoff (max 60s) |
| FIX-17 | `equity_pool.py` | Pool initialised with only live slots (skipped slot 4 budget entry); now defaults to `FLEET_SIZE` slots |

### Round 2 — Audit Corrections

| ID | File | Fix |
|---|---|---|
| NM-2 | `agent.py` | Stale `_ranked_idx` variable accumulated across rotations causing wrong coin selection; removed all assignments |
| NM-3 | `agent.py` | NN retrain block ran for EMA cross agents where `predictor=None`; wrapped block with strategy guard |
| NM-4 | `agent.py` | Background scanner logged `top5=[]` for EMA cross agents (scanner not started); added strategy guard to log block |
| NH-2 | `agent.py` | NN gate activated when `predictor.oos_acc` was set but `oos_samples < 50` — gate could block entries on statistically invalid model; added sample count guard |
| NH-3 | `agent.py` | Session equity files from previous calendar days accumulated in `/tmp`; added cleanup on startup |
| NC-1 | `agent.py` | TP2 sell quantity in agent loop used wrong fraction (applied to full balance, not remaining); corrected to match exit_manager logic |
| NC-3 | `agent.py` | `CLOSE_ALL` instruction didn't check fill status before clearing positions; partial fills left orphaned coins |
| NC-4 | `agent.py` | EMA cross coin could be overridden by `AGENT_SYMBOL` env var to BTCUSDT; locked to ETHUSDT with env var as secondary fallback only |
| NC-5 | `agent.py` | EMA cross could open new positions during VOLATILE regime; added VOLATILE block for new EMA cross entries |
| STARTUP | `agent.py` | `queue = asyncio.Queue()` accidentally dropped during audit edits; agents started then immediately crashed with `NameError` |

### Round 3 — Expert Audit (22 Findings)

| ID | Severity | File | Fix |
|---|---|---|---|
| C-1 | Critical | `config.py` | Live risk params dangerously aggressive: `RISK_PCT=10%`, `MAX_TRADE_PCT=70%` — corrected to `1%` / `15%` / `40%` exposure |
| C-2 | Critical | `agent.py` | Orphan BTC sell called `om.get_lot_step()` as a tick dict — `get_lot_step()` returns a `float`; `submit()` immediately crashed with `TypeError: 'float' object is not subscriptable` |
| C-3 | Critical | `equity_pool.py` | Pool file opened with `"w"` mode (truncating it immediately) before `fcntl.flock()` could protect it — concurrent readers saw an empty file; replaced with atomic write-then-`os.replace()` |
| C-4 | Critical | `market_data.py` | Both WS loops hard-coded live Binance endpoints (`wss://stream.binance.com:9443/ws/...`); testnet and demo agents always streamed live prices; fixed to use `cfg.SPOT_WS_URL` |
| C-5 | Critical | `agent.py` / `order_manager.py` | `SWITCH_MODE` updated URL config but kept the old API session with old credentials; added API key reload from env and `om.reset_session()` call; added `reset_session()` method to `OrderManager` |
| H-1 | High | `agent.py` | `get_balances_raw()` called twice per BUY cycle (lines ~1155 and ~1239) — two REST calls to Binance per entry; second call removed, values reused |
| H-2 | High | `exit_manager.py` / `agent.py` | TP3 target was computed and stored but never triggered; added `tp3_hit` field to `Position`, TP3 detection in exit manager, and `PARTIAL_CLOSE:BUY:TP3` handler in agent loop |
| H-3 | High | `agent_manager.py` | Fleet spawner minimum price filter was `$0.05` — allowed coins where a single tick costs 0.125% of price (worse than fee threshold); raised to `$0.50` |
| H-4 | High | `agent_manager.py` | ETHUSDT (EMA cross coin) could appear in top3_others picker for momentum slots 1–3, causing double ETH exposure; ETHUSDT explicitly excluded from momentum slot assignment |
| H-5 | High | `agent.py` | `RESET_DAY_START` fetched balances for `cfg.SYMBOL` (static BTCUSDT) instead of `active_symbol`; slots trading other coins reset baselines against the wrong symbol's price |
| H-6 | High | `agent_manager.py` | `/api/pool` fallback response returned `range(4)` slots — slot 4 (EMA cross) invisible in fleet UI when pool file was missing; corrected to `range(5)` |
| M-1 | Medium | `market_data.py` | Book imbalance in WS book cache was never updated from live stream — stored unchanged `prev_imb` every tick; real imbalance now back-filled from REST orderbook every main cycle |
| M-2 | Medium | `agent.py` | `from nn_predictor import OOS_MIN_ACC, OOS_MIN_SAMP` inside main loop (re-imported every cycle); moved to top-level module imports |
| M-3 | Medium | `regime.py` | Regime ATR-to-price ratio used VWAP as price fallback — VWAP drifts from close price intraday, making the VOLATILE threshold inconsistent with scanner/signal engine; changed to `ind["close"]` |
| M-4 | Medium | `config.py` | `FLEET_SIZE` default was `4` — slot 4 had no equity pool entry at startup; changed default to `5` (also configurable via `DIPU_FLEET_SIZE` env var) |
| M-5 | Medium | `agent.py` | `risk.record_trade()` not called for TP1/TP2 partial closes — consecutive-loss counter and drawdown ledger were blind to partial P&L; now called after every TP fill |
| M-6 | Medium | `agent.py` | `scanner._health.invalidate()` never called before coin rotation — health filter could serve stale (cached) rejection for a coin the agent just rotated away from; invalidate now called before every `md.close()` at rotation sites |
| M-7 | Medium | `agent.py` | After a full position close, `risk.update_metrics()` ran twice in the same cycle (once with fresh post-close equity, once with cached equity from `_state`); second call now skipped if post-close equity was already fetched |
| L-1 | Low | `agent.py` | Signal reversal exit only implemented for BUY side (RSI > 78 + MACD cross-down); SELL positions have no equivalent signal-reversal exit; documented as known limitation |
| L-2 | Low | `agent.py` | Same as M-5 — TP partial close P&L not fed to `risk.record_trade()`; resolved together with M-5 |
| L-3 | Low | `agent.py` | Background scanner loop slept only 60s but `SCAN_INTERVAL=300s` — scanner fired 5× per interval, burning API rate limit on redundant scans; sleep changed to `270s` |
| NH-3 | Low | `agent.py` | Old session equity files from previous calendar days accumulated in `/tmp`; cleanup now runs on startup, removing files whose date does not match today |

### Round 4 — Strategy & Profitability Overhaul

| ID | File | Fix / Feature |
|---|---|---|
| S-1 | `instruction_server.py` | `SET_STRATEGY` not in `URGENT_ACTIONS` — strategy changes sat in queue up to 60s before taking effect; added to urgent set so wake event fires immediately |
| S-2 | `exit_manager.py` | Staircase stop: after TP1 hit, stop is locked to TP1 price; after TP2 hit, stop locked to TP2 price — prevents remaining position giving back secured levels |
| S-3 | `market_data.py` | Six indicators missing from `compute_indicators()` that strategy advisor and filters require: `adx14`, `macd_hist`, `bb_width`, `bb_pct`, `stoch_rsi_k`, `choppiness14`, `volume_ratio` — all added |
| S-4 | `strategy_advisor_module.py` | New module: scores all 5 strategies against live indicators every cycle (0–10 scale); result shown on dashboard advisor bar |
| S-5 | `agent.py` | `_strategies` list added (max 3); `SET_STRATEGY` now accepts `{strategies:[...]}` list or legacy single `{strategy:""}` — enables multi-strategy consensus mode |
| S-6 | `agent.py` | Secondary strategy consensus veto: if any secondary strategy signals the opposite direction to the primary, signal is suppressed to NONE before execution |
| S-7 | `agent.py` | EMA Cross ADX filter: cross signals discarded when ADX < 18 — prevents false crosses in choppy/ranging markets (primary cause of EMA cross losses on ETHUSDT) |
| S-8 | `config.py` | `ATR_STOP_MULT` 1.0 → 1.5 — 1× ATR too tight vs 15m crypto noise; stops hit before any TP level could be reached |
| S-9 | `config.py` | `MAX_TRADE_HOURS_SPOT/FUTURES` 2h → 4h — TP2 (2.5R) and TP3 (4R) on 15m candles typically need 3–6h; time exits triggered before targets |
| S-10 | `agent.py` | `NONE_SIGNAL_ROTATE` 3 → 6 cycles — 3-minute coin rotation was excessive churn; 6 minutes gives setups time to develop |
| S-11 | `instruction_server.py` | Chart `refreshChart()` now caches `_lastChartData` and applies all active indicator overlays (BB, EMA50) and sub-panels (MACD, RSI, Volume, Forecast) on every reload |
| S-12 | `instruction_server.py` | Multi-strategy UI: strategy buttons support multi-select (click to add/remove, max 3); primary = bright yellow, secondary = gold outline, inactive = grey |
| S-13 | `instruction_server.py` | MACD, BB, and EMA50 enabled by default on all chart loads; MACD sub-panel pre-initialised at boot |
| S-14 | `agent.py` | Strategy advisor scores pushed to agent state each cycle (`advised_strategy`, `advised_strategy_score`, `strategy_scores[]`) for dashboard display |

---

## Risk Disclaimer

This software trades real cryptocurrency. Past performance does not guarantee future results. Only use funds you can afford to lose entirely. The authors take no responsibility for financial losses.
