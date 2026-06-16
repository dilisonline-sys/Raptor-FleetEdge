"""
Multi-agent instruction interface for Raptor FleetEdge.
"""
import asyncio
import json
import os
import time
from aiohttp import web
from logger import log
import config as cfg

VALID_ACTIONS = {"BUY", "SELL", "CLOSE_ALL", "HALT", "RESUME", "STATUS", "SWITCH_MODE", "SWITCH_COIN", "FORCE_BTC", "RESUME_AUTO", "ANALYST_ON", "ANALYST_OFF", "SET_STRATEGY"}

_state = {
    "started_at":    time.time(),
    "equity":        0.0,
    "symbol":        "—",
    "trading_mode":  cfg.TRADING_MODE,
    "regime":        "—",
    "last_signal":   "—",
    "last_action":   "—",
    "positions":     0,
    "daily_dd":      0.0,
    "halt":          False,
    "last_log":      [],
    "transactions":  [],
    "top_coins":     [],
    "chart_data":    {},
    "open_pos":      None,
    "fear_greed":    {"value": 50, "label": "Neutral"},
    "coin_mode":     "auto",   # "auto" | "<SYMBOL>"
    "price":         0.0,      # current live price — updated every cycle for chart ticks
    "interval":      cfg.INTERVAL,
    "risk_pct":      cfg.RISK_PCT,
    "max_trade_pct": cfg.MAX_TRADE_PCT,
    "cycle_sleep":   cfg.CYCLE_SLEEP_SECONDS,
    "strategy":      "Volatility Chase · Momentum Only",
    "strategy_key":  "momentum",
    "strategy_keys": ["momentum"],
    "scanner_ranked": [],
    "scanner_best":   "—",
    "scanner_ts":     0,
    "pool_slot":  int(os.environ.get("AGENT_SLOT", "0")),
    "pool_state": {},
    "usdt_balance":     0.0,
    "coin_qty":         0.0,
    "coin_value_usdt":  0.0,
    "coin_asset":       "—",
    "analyst_enabled": False,
    "analysis":        {},
    "portfolio":        {},
    "advised_strategy":        "—",
    "advised_strategy_label":  "—",
    "advised_strategy_score":  0.0,
    "advised_strategy_reason": "—",
    "strategy_scores":         [],
}

# ── HTML template ──────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Raptor FleetEdge — trading agent</title>
<style>#sse-status{font-size:.7rem;margin-left:12px;vertical-align:middle}</style>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#fff;font-family:'Courier New',monospace;padding:20px}
h1{color:#00e5ff;font-size:1.5rem;margin-bottom:3px}
h2{color:#fff;font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;margin:22px 0 8px}
.sub{color:#fff;font-size:.78rem;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:6px}
.card{background:#111;border:1px solid #1e1e1e;border-radius:7px;padding:14px}
.card .lbl{color:#fff;font-size:.65rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.card .val{font-size:1.3rem;font-weight:bold;color:#00e5ff}
.val.g{color:#00e676}.val.r{color:#ff1744}.val.y{color:#ffd600}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.68rem;font-weight:bold}
.badge.live{background:#00e676;color:#000}
.badge.halt{background:#ff1744;color:#fff}
.badge.testnet{background:#ffd600;color:#000}
.badge.demo{background:#00bcd4;color:#000}
/* chart */
.chart-wrap{background:#0d0d0d;border:1px solid #1e1e1e;border-radius:8px;overflow:hidden;margin-bottom:4px}
.chart-header{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid #1a1a1a;background:#111}
.chart-sym{font-size:.95rem;font-weight:bold;color:#00e5ff}
.chart-meta{font-size:.7rem;color:#fff}
#chart{width:100%;height:380px}
.legend{display:flex;gap:16px;padding:6px 14px;background:#0d0d0d;font-size:.68rem}
.legend span{display:flex;align-items:center;gap:5px}
.dot{width:10px;height:3px;border-radius:1px}
/* transactions */
table{width:100%;border-collapse:collapse;font-size:.73rem;margin-bottom:20px}
thead th{background:#111;color:#fff;font-size:.65rem;text-transform:uppercase;letter-spacing:.07em;padding:7px 9px;border-bottom:1px solid #1e1e1e;text-align:left}
tbody tr{border-bottom:1px solid #141414}
tbody tr:hover{background:#111}
td{padding:6px 9px;vertical-align:middle;color:#fff}
td.buy{color:#00e676;font-weight:bold}
td.sell{color:#ff1744;font-weight:bold}
td.close{color:#fff}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pp{color:#00e676}.pn{color:#ff1744}
/* log */
.log-wrap{background:#080808;border:1px solid #1a1a1a;border-radius:8px;display:flex;flex-direction:column;height:300px}
.log-toolbar{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid #1a1a1a;background:#0e0e0e;border-radius:8px 8px 0 0}
.log-toolbar input{background:#0a0a0a;border:1px solid #222;color:#fff;padding:3px 8px;border-radius:4px;font-family:inherit;font-size:.7rem;flex:1}
.log-toolbar button{background:#161616;border:1px solid #222;color:#fff;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:.68rem;font-family:inherit}
.log-toolbar button:hover{color:#fff}
.log-body{flex:1;overflow-y:auto;padding:10px 12px}
.log-line{font-size:.69rem;line-height:1.7;white-space:pre-wrap;word-break:break-all;border-bottom:1px solid #0f0f0f;padding:1px 0}
.log-line.err{color:#ff6d6d}
.log-line.warn{color:#ffd600}
.log-line.info{color:#fff}
.log-line.trade{color:#00e676}
.log-line.switch{color:#00e5ff}
.none{color:#fff;font-size:.73rem;padding:10px 0}
/* coin selector */
.cbtn{background:#111;border:1px solid #222;color:#fff;padding:4px 11px;border-radius:4px;cursor:pointer;font-size:.72rem;font-family:inherit;transition:all .15s}
.cbtn:hover{border-color:#fff;color:#fff}
.cbtn.active{background:#00e5ff;border-color:#00e5ff;color:#000;font-weight:bold}
.cbtn.auto{background:#161616;border-color:#333;color:#00e5ff}
.cbtn.auto.active{background:#00e5ff;color:#000}
/* strategy selector */
.sbtn{background:#111;border:1px solid #222;color:#aaa;padding:4px 11px;border-radius:4px;cursor:pointer;font-size:.70rem;font-family:inherit;transition:all .15s}
.sbtn:hover{border-color:#ffd600;color:#ffd600}
.sbtn.active{background:#ffd600;border-color:#ffd600;color:#000;font-weight:bold}
.sbtn.secondary{background:#1a1400;border-color:#665500;color:#ffd600}
/* indicator panel toggle buttons */
.ibtn{background:#111;border:1px solid #222;color:#555;padding:2px 8px;border-radius:3px;cursor:pointer;font-size:.65rem;font-family:inherit;transition:all .15s}
.ibtn:hover{border-color:#aaa;color:#aaa}
.ibtn.on{background:#1a1a2e;border-color:#00e5ff;color:#00e5ff}
/* ── Theme toggle button ── */
#theme-btn{position:fixed;top:12px;right:16px;z-index:9999;background:#1e1e2e;border:1px solid #444;color:#e0e0ff;border-radius:20px;padding:5px 14px;cursor:pointer;font-family:'Courier New',monospace;font-size:.72rem;font-weight:bold;letter-spacing:.04em;box-shadow:0 2px 8px rgba(0,0,0,.5);transition:.2s}
#theme-btn:hover{background:#2a2a3e;border-color:#00e5ff;color:#00e5ff}
/* ── Day theme overrides ── */
[data-theme="day"]{background:#f4f5f7;color:#1a1a2e}
[data-theme="day"] h1,[data-theme="day"] h2{color:#0a0a1e}
[data-theme="day"] .sub{color:#1a1a2e}
[data-theme="day"] #theme-btn{background:#e8e8f0;border-color:#aaa;color:#333;box-shadow:0 2px 8px rgba(0,0,0,.15)}
[data-theme="day"] #theme-btn:hover{background:#d8d8ee;border-color:#0077bb;color:#0077bb}
[data-theme="day"] .card{background:#fff;border-color:#dde0e6}
[data-theme="day"] .card .lbl{color:#444}
[data-theme="day"] .card .val{color:#0077bb}
[data-theme="day"] #pool-bar{background:#eef0f3;border-color:#d4d8de;color:#1a1a2e}
[data-theme="day"] .chart-wrap{background:#f0f2f5;border-color:#d4d8de}
[data-theme="day"] .chart-header{background:#fff;border-color:#e0e3e8}
[data-theme="day"] .chart-sym{color:#0077bb}
[data-theme="day"] .legend{background:#f0f2f5}
[data-theme="day"] table thead th{background:#eef0f3;color:#333;border-color:#d4d8de}
[data-theme="day"] tbody tr{border-color:#eee}
[data-theme="day"] tbody tr:hover{background:#eef0f3}
[data-theme="day"] .log-wrap{background:#eef0f3;border-color:#d4d8de}
[data-theme="day"] .log-toolbar{background:#e4e6ea;border-color:#d4d8de}
[data-theme="day"] .log-toolbar input{background:#fff;border-color:#ccc;color:#111}
[data-theme="day"] .log-toolbar button{background:#dde0e6;border-color:#ccc;color:#222}
[data-theme="day"] .log-body{background:#f8f9fb}
[data-theme="day"] .log-line{border-color:#e4e6ea}
[data-theme="day"] .log-line.info{color:#1a1a2e}
[data-theme="day"] .cbtn{background:#eef0f3;border-color:#ccc;color:#222}
[data-theme="day"] .cbtn:hover{border-color:#333;color:#111}
[data-theme="day"] .cbtn.active{background:#0077bb;border-color:#0077bb;color:#fff}
[data-theme="day"] .cbtn.auto{background:#e4e6ea;border-color:#bbb;color:#0077bb}
[data-theme="day"] .cbtn.auto.active{background:#0077bb;color:#fff}
</style>
</head>
<body>
<button id="theme-btn" onclick="toggleTheme()">☀ Day</button>
<h1>&#9654; __AGENT_NAME__ <span class="badge __MODE_CLASS__">__MODE__</span><span id="sse-status" style="color:#fff">○ connecting…</span></h1>
<div id="pool-bar" style="display:flex;align-items:center;gap:16px;padding:6px 12px;background:#0e0e0e;border:1px solid #1a1a1a;border-radius:6px;margin-bottom:12px;font-size:.7rem;flex-wrap:wrap">
  <span style="color:#fff;text-transform:uppercase;letter-spacing:.08em">Pool</span>
  <span>Slot <span id="pb-slot" style="color:#00e5ff;font-weight:bold">—</span></span>
  <span>Budget <span id="pb-budget" style="color:#ffd600;font-weight:bold">—</span></span>
  <span>Other agents: <span id="pb-others" style="color:#fff">—</span></span>
  <span style="margin-left:auto;color:#fff;font-size:.65rem" id="pb-ts">—</span>
</div>
<div class="sub">
  <span style="color:#00e5ff;font-family:monospace">&#128279; __API_URL__</span>
  &nbsp;·&nbsp; autonomous crypto trading agent
  &nbsp;·&nbsp;
  <button id="stop-btn" onclick="stopAgent()" style="background:#ff1744;color:#fff;border:none;padding:3px 12px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;margin-left:6px">&#9646;&#9646; Stop Trading</button>
  <button id="start-btn" onclick="startAgent()" style="background:#00e676;color:#000;border:none;padding:3px 12px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;margin-left:4px;display:none">&#9654; Resume Trading</button>
  &nbsp;|&nbsp;
  <button id="btn-buy"  onclick="marketOrder('BUY')"  style="background:#00e676;color:#000;border:none;padding:3px 14px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;font-weight:bold;margin-left:4px">&#9650; Buy —</button>
  <button id="btn-sell" onclick="marketOrder('SELL')" style="background:#ff1744;color:#fff;border:none;padding:3px 14px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;font-weight:bold;margin-left:4px">&#9660; Sell —</button>
  &nbsp;|&nbsp;
  <button onclick="forceBTC()" style="background:#f7931a;color:#000;border:none;padding:3px 14px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;font-weight:bold;margin-left:4px">&#9679; Trade BTC</button>
  <button onclick="resumeAuto()" style="background:#00e5ff;color:#000;border:none;padding:3px 14px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;font-weight:bold;margin-left:4px">&#9654; Trade Auto</button>
</div>

<div class="grid" id="cards">
  <div class="card"><div class="lbl">USDT Balance</div><div class="val" id="c-usdt-bal">—</div><div style="font-size:.7rem;color:#fff;margin-top:3px">Total: <span id="c-eq">—</span></div></div>
  <div class="card"><div class="lbl">Active Coin</div><div class="val g" id="c-sym">—</div></div>
  <div class="card"><div class="lbl">Asset Balance</div><div class="val y" id="c-coin-qty">—</div><div style="font-size:.7rem;color:#fff;margin-top:3px" id="c-coin-val">—</div></div>
  <div class="card"><div class="lbl">Mode</div><div class="val" id="c-mode">—</div></div>
  <div class="card"><div class="lbl">Regime</div><div class="val y" id="c-reg">—</div></div>
  <div class="card"><div class="lbl">Open Positions</div><div class="val" id="c-pos">—</div></div>
  <div class="card"><div class="lbl">Daily Drawdown</div><div class="val" id="c-dd">—</div></div>
  <div class="card" id="session-card">
    <div class="lbl">SESSION P&amp;L</div>
    <div style="margin-top:4px;display:flex;align-items:baseline;gap:6px">
      <span id="session-pnl" style="font-size:1.3rem;font-weight:bold;font-family:'Courier New',monospace">—</span>
      <span id="session-pnl-pct" style="font-size:.78rem"></span>
    </div>
    <div style="font-size:.68rem;color:#fff;margin-top:3px">Start: <span id="session-start-eq" style="color:#fff">—</span></div>
  </div>
  <div class="card"><div class="lbl">Last Signal</div><div class="val" id="c-sig">—</div></div>
  <div class="card"><div class="lbl">Uptime</div><div class="val g" id="c-up">—</div></div>
  <div class="card"><div class="lbl">Fear &amp; Greed</div><div class="val" id="c-fg" style="font-size:1rem">—</div></div>
  <div class="card"><div class="lbl">Interval</div><div class="val y" id="c-interval">—</div></div>
  <div class="card"><div class="lbl">Risk / Max Trade</div><div class="val" id="c-risk">—</div></div>
  <div class="card"><div class="lbl">Cycle</div><div class="val" id="c-cycle">—</div></div>
  <div class="card"><div class="lbl">Strategy</div><div class="val g" id="c-strat">—</div></div>
  <div class="card" id="port-card">
    <div class="lbl">PORTFOLIO</div>
    <div style="font-size:.85rem;margin-top:4px">
      <span style="color:#fff">Total:</span>&nbsp;<span id="port-total" style="color:#00e676;font-weight:bold">—</span>
      &nbsp;&nbsp;<span style="color:#fff">USDT:</span>&nbsp;<span id="port-usdt" style="color:#fff">—</span>
      &nbsp;&nbsp;<span style="color:#fff">Spot:</span>&nbsp;<span id="port-coins" style="color:#00e5ff">—</span>
      &nbsp;&nbsp;<span style="color:#fff">Earn:</span>&nbsp;<span id="port-earn" style="color:#ffd600">—</span>
    </div>
    <div style="font-size:.82rem;margin-top:3px">
      <span style="color:#fff">Day P&L:</span>&nbsp;<span id="port-pnl" style="font-weight:bold">—</span>
      &nbsp;&nbsp;<span style="color:#fff">Start:</span>&nbsp;<span id="port-start" style="color:#fff">—</span>
    </div>
  </div>
</div>

<!-- Coin selector -->
<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
  <span style="color:#fff;font-size:.65rem;text-transform:uppercase;letter-spacing:.1em">Coin</span>
  <div id="coin-selector" style="display:flex;gap:6px;flex-wrap:wrap"></div>
</div>

<!-- Strategy selector + advisor -->
<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">
  <span style="color:#aaa;font-size:.65rem;text-transform:uppercase;letter-spacing:.1em">Strategy</span>
  <div id="strategy-selector" style="display:flex;gap:6px;flex-wrap:wrap"></div>
</div>
<div id="strategy-advice-bar" style="display:none;margin-bottom:12px;padding:6px 12px;background:#0d1a0d;border:1px solid #1a3a1a;border-radius:5px;font-size:.70rem;color:#aaa;line-height:1.5">
  <span style="color:#00e676;font-weight:bold">&#9670; Advisor: </span>
  <span id="advice-label" style="color:#ffd600;font-weight:bold">—</span>
  <span style="color:#aaa"> · score </span><span id="advice-score" style="color:#fff">—</span>
  <span style="color:#555"> · </span><span id="advice-reason" style="color:#aaa">—</span>
</div>

<h2 style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px">
  <span>&#9646; Chart — <span id="chart-sym-title">loading…</span></span>
  <span style="display:flex;gap:5px;flex-wrap:wrap">
    <button class="ibtn on" id="ibtn-bb"  onclick="togglePanel('bb')"  title="Bollinger Bands">BB</button>
    <button class="ibtn on" id="ibtn-e50" onclick="togglePanel('e50')" title="EMA 50">EMA50</button>
    <button class="ibtn" id="ibtn-rsi" onclick="togglePanel('rsi')" title="RSI 14">RSI</button>
    <button class="ibtn on" id="ibtn-mac" onclick="togglePanel('mac')" title="MACD">MACD</button>
    <button class="ibtn" id="ibtn-vol" onclick="togglePanel('vol')" title="Volume">VOL</button>
    <button class="ibtn" id="ibtn-fc"  onclick="togglePanel('fc')"  title="Forecast">&#8987; FC</button>
  </span>
</h2>
<div class="chart-wrap">
  <div class="chart-header">
    <span class="chart-sym" id="chart-sym-label">—</span>
    <span class="chart-meta" id="chart-meta">15m · EMA 9/21 · entry / stop / TP</span>
  </div>
  <div id="chart"></div>
  <div class="legend" id="chart-legend">
    <span><div class="dot" style="background:#00e5ff"></div>EMA 9</span>
    <span><div class="dot" style="background:#ffd600"></div>EMA 21</span>
    <span id="leg-e50"><div class="dot" style="background:#b39ddb"></div>EMA 50</span>
    <span id="leg-bb"><div class="dot" style="background:#607d8b"></div>BB Bands</span>
    <span><div class="dot" style="background:#00e676;width:2px;height:12px;border-radius:0"></div>Entry</span>
    <span><div class="dot" style="background:#ff1744;width:2px;height:12px;border-radius:0"></div>Stop</span>
    <span><div class="dot" style="background:#00bcd4;width:2px;height:12px;border-radius:0"></div>TP1/TP2</span>
  </div>
  <!-- Sub-panels: mac on by default; rsi/vol/fc hidden by default -->
  <div id="panel-rsi" style="display:none">
    <div style="font-size:.60rem;color:#aaa;padding:2px 6px;background:#0d0d0d">RSI 14 · oversold &lt;30 / overbought &gt;70</div>
    <div id="chart-rsi" style="height:80px"></div>
  </div>
  <div id="panel-mac" style="display:block">
    <div style="font-size:.60rem;color:#aaa;padding:2px 6px;background:#0d0d0d">MACD (12,26,9) · histogram</div>
    <div id="chart-mac" style="height:80px"></div>
  </div>
  <div id="panel-vol" style="display:none">
    <div style="font-size:.60rem;color:#aaa;padding:2px 6px;background:#0d0d0d">Volume (normalised)</div>
    <div id="chart-vol" style="height:60px"></div>
  </div>
  <div id="panel-fc" style="display:none">
    <div style="font-size:.60rem;color:#ffd600;padding:2px 6px;background:#0d0d0d">&#8987; Forecast · linear regression projection · 6 bars · ±1 ATR band</div>
    <div id="chart-fc" style="height:120px"></div>
  </div>
</div>

<h2 style="display:flex;align-items:center;justify-content:space-between">
  <span>&#9632; Rule Analyst <span style="font-size:.6rem;color:#fff;font-weight:normal;margin-left:6px">advisory only · does not affect trades</span></span>
  <button id="ai-toggle-btn" onclick="toggleAnalyst()" style="font-size:.68rem;padding:4px 12px;border-radius:4px;border:1px solid #333;background:#111;color:#fff;cursor:pointer">Enable</button>
</h2>
<div id="ai-panel" style="display:none;background:#0a0d0a;border:1px solid #1a2e1a;border-radius:8px;padding:14px;margin-bottom:20px;font-size:.73rem">
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:10px">
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Sentiment</div>
      <div id="ai-sentiment" style="font-size:1.1rem;font-weight:bold;color:#00e676">—</div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Confidence</div>
      <div id="ai-confidence" style="font-size:1.1rem;font-weight:bold;color:#00e5ff">—</div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Trend</div>
      <div id="ai-trend" style="font-size:1.1rem;font-weight:bold;color:#ffd600">—</div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Momentum</div>
      <div id="ai-momentum" style="font-size:1.1rem;font-weight:bold;color:#fff">—</div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Entry Quality</div>
      <div id="ai-entry" style="font-size:1.1rem;font-weight:bold;color:#fff">—</div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Risk Level</div>
      <div id="ai-risk" style="font-size:1.1rem;font-weight:bold;color:#fff">—</div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Support</div>
      <div id="ai-support" style="font-size:.95rem;font-weight:bold;color:#00e676">—</div>
    </div>
    <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
      <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Resistance</div>
      <div id="ai-resistance" style="font-size:.95rem;font-weight:bold;color:#ff1744">—</div>
    </div>
  </div>
  <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px;margin-bottom:6px">
    <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px">Market Insight</div>
    <div id="ai-insight" style="color:#fff;line-height:1.5">—</div>
  </div>
  <div style="background:#0e0e0e;border:1px solid #1e1e1e;border-radius:6px;padding:10px">
    <div style="color:#fff;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px">Watch Next Candle</div>
    <div id="ai-watch" style="color:#ffd600;line-height:1.5">—</div>
  </div>
  <div style="margin-top:6px;color:#fff;font-size:.62rem;text-align:right" id="ai-ts">—</div>
</div>

<h2>&#9646; Market Scanner <span id="scanner-ts" style="font-size:.6rem;color:#fff;margin-left:8px">scanning…</span></h2>
<div style="background:#0d0d0d;border:1px solid #1e1e1e;border-radius:8px;padding:12px;margin-bottom:20px;overflow-x:auto">
  <table style="width:100%;border-collapse:collapse;font-size:.7rem">
    <thead>
      <tr style="color:#fff;text-transform:uppercase;letter-spacing:.06em;font-size:.62rem">
        <th style="text-align:left;padding:4px 8px">#</th>
        <th style="text-align:left;padding:4px 8px">Symbol</th>
        <th style="text-align:right;padding:4px 8px">Score</th>
        <th style="text-align:right;padding:4px 8px">ATR%</th>
        <th style="text-align:right;padding:4px 8px">24h %</th>
        <th style="text-align:right;padding:4px 8px">Vol (M)</th>
        <th style="text-align:center;padding:4px 8px">Trend</th>
        <th style="text-align:center;padding:4px 8px">Regime</th>
        <th style="text-align:center;padding:4px 8px">Action</th>
      </tr>
    </thead>
    <tbody id="scanner-tbody">
      <tr><td colspan="9" style="color:#fff;padding:10px 8px">Scanning market…</td></tr>
    </tbody>
  </table>
</div>

<h2>&#9646; Binance Orders <span id="orders-refresh" style="font-size:.6rem;color:#fff;cursor:pointer;margin-left:8px" onclick="loadOrders()">&#8635; refresh</span></h2>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
  <div>
    <div style="font-size:.65rem;color:#fff;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Open Orders</div>
    <div id="open-orders-wrap" style="background:#0d0d0d;border:1px solid #1e1e1e;border-radius:6px;padding:10px;font-size:.7rem;min-height:40px"></div>
  </div>
  <div>
    <div style="font-size:.65rem;color:#fff;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Order History</div>
    <div id="order-history-wrap" style="background:#0d0d0d;border:1px solid #1e1e1e;border-radius:6px;padding:10px;font-size:.7rem;min-height:40px"></div>
  </div>
</div>

<h2>&#9646; Transactions</h2>
<div id="tx-wrap"></div>

<h2>&#9646; Live Log</h2>
<div class="log-wrap">
  <div class="log-toolbar">
    <input id="log-filter" placeholder="filter… (e.g. TRADE, ERROR, SWITCH)" oninput="filterLog()">
    <button onclick="clearFilter()">clear</button>
    <button onclick="scrollBottom()">&#9660; bottom</button>
  </div>
  <div class="log-body" id="log-body"></div>
</div>

<script>
// ── State ─────────────────────────────────────────────────
let chart, candleSeries, ema9Series, ema21Series;
let ema50Series = null, bbUpperSeries = null, bbLowerSeries = null;
let rsiChart = null, rsiSeries = null;
let macChart = null, macHistSeries = null, macLineSeries = null, macSigSeries = null;
let volChart = null, volSeries = null;
let fcChart  = null, fcLineSeries = null, fcHiSeries = null, fcLoSeries = null;
let _lastChartData = null;
const _panelOn = { bb: true, e50: true, rsi: false, mac: true, vol: false, fc: false };
let startedAt = __STARTED_AT__;
let allLogLines = [];
let _currentSym  = '';
let _lastOpenPos = null;
let _coinMode    = 'auto';
let _topCoins    = [];
let _lastCandle  = null;
let _lastChartTs = 0;

const _CHART_OPT = (h) => ({
  width: document.getElementById('chart').clientWidth,
  height: h,
  layout: { background: { color: '#0d0d0d' }, textColor: '#aaa' },
  grid: { vertLines: { color: '#0f0f0f' }, horzLines: { color: '#0f0f0f' } },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: '#1e1e1e', scaleMargins: { top: 0.05, bottom: 0.05 } },
  timeScale: { borderColor: '#1e1e1e', timeVisible: true, secondsVisible: false },
  handleScroll: false, handleScale: false,
});

function _syncTime(slave) {
  chart.timeScale().subscribeVisibleTimeRangeChange(r => {
    try { slave.timeScale().setVisibleRange(r); } catch(_) {}
  });
}

// ── Chart init ────────────────────────────────────────────
function initChart() {
  const el = document.getElementById('chart');
  chart = LightweightCharts.createChart(el, {
    ..._CHART_OPT(380), handleScroll: true, handleScale: true,
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: '#00e676', downColor: '#ff1744',
    borderUpColor: '#00e676', borderDownColor: '#ff1744',
    wickUpColor: '#00e676', wickDownColor: '#ff1744',
    priceFormat: { type: 'price', precision: 5, minMove: 0.00001 },
  });
  ema9Series  = chart.addLineSeries({ color: '#00e5ff', lineWidth: 1, priceLineVisible: false, priceFormat: { type: 'price', precision: 5, minMove: 0.00001 } });
  ema21Series = chart.addLineSeries({ color: '#ffd600', lineWidth: 1, priceLineVisible: false, priceFormat: { type: 'price', precision: 5, minMove: 0.00001 } });
  window.addEventListener('resize', () => {
    const w = el.clientWidth;
    chart.applyOptions({ width: w });
    [rsiChart, macChart, volChart, fcChart].forEach(c => c && c.applyOptions({ width: w }));
  });
}

function togglePanel(key) {
  _panelOn[key] = !_panelOn[key];
  const btn = document.getElementById('ibtn-' + key);
  if (btn) btn.classList.toggle('on', _panelOn[key]);
  if (key === 'e50') document.getElementById('leg-e50').style.display = _panelOn.e50 ? '' : 'none';
  if (key === 'bb')  document.getElementById('leg-bb').style.display  = _panelOn.bb  ? '' : 'none';
  if (_lastChartData) {
    _applyIndicatorSeries(_lastChartData);
    ['rsi','mac','vol','fc'].forEach(p => {
      const div = document.getElementById('panel-' + p);
      if (div) div.style.display = _panelOn[p] ? 'block' : 'none';
      if (_panelOn[p]) { _initSubChart(p); _applySubChart(p, _lastChartData); }
    });
  }
}

function _initSubChart(p) {
  if (p === 'rsi' && !rsiChart) {
    rsiChart  = LightweightCharts.createChart(document.getElementById('chart-rsi'), _CHART_OPT(80));
    rsiSeries = rsiChart.addLineSeries({ color: '#ab47bc', lineWidth: 1, priceLineVisible: false });
    [30, 70].forEach(lv => rsiSeries.createPriceLine({ price: lv, color: '#333', lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: lv.toString() }));
    _syncTime(rsiChart);
  }
  if (p === 'mac' && !macChart) {
    macChart      = LightweightCharts.createChart(document.getElementById('chart-mac'), _CHART_OPT(80));
    macHistSeries = macChart.addHistogramSeries({ priceLineVisible: false });
    macLineSeries = macChart.addLineSeries({ color: '#00bcd4', lineWidth: 1, priceLineVisible: false });
    macSigSeries  = macChart.addLineSeries({ color: '#ff7043', lineWidth: 1, priceLineVisible: false });
    _syncTime(macChart);
  }
  if (p === 'vol' && !volChart) {
    volChart  = LightweightCharts.createChart(document.getElementById('chart-vol'), _CHART_OPT(60));
    volSeries = volChart.addHistogramSeries({ color: '#00e5ff33', priceLineVisible: false });
    _syncTime(volChart);
  }
  if (p === 'fc' && !fcChart) {
    fcChart      = LightweightCharts.createChart(document.getElementById('chart-fc'), _CHART_OPT(120));
    fcChart._candleCtx = fcChart.addCandlestickSeries({
      upColor:'#00e67666', downColor:'#ff174466',
      borderUpColor:'#00e676', borderDownColor:'#ff1744',
      wickUpColor:'#00e67666', wickDownColor:'#ff174466',
      priceFormat:{ type:'price', precision:5, minMove:0.00001 },
    });
    fcLineSeries = fcChart.addLineSeries({ color: '#ffd600', lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false,
      priceFormat:{ type:'price', precision:5, minMove:0.00001 } });
    fcHiSeries   = fcChart.addLineSeries({ color: '#00e67655', lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dotted, priceLineVisible: false,
      priceFormat:{ type:'price', precision:5, minMove:0.00001 } });
    fcLoSeries   = fcChart.addLineSeries({ color: '#ff174455', lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dotted, priceLineVisible: false,
      priceFormat:{ type:'price', precision:5, minMove:0.00001 } });
    _syncTime(fcChart);
  }
}

function _applyIndicatorSeries(cd) {
  if (!ema50Series) {
    ema50Series   = chart.addLineSeries({ color: '#b39ddb', lineWidth: 1, priceLineVisible: false, priceFormat:{ type:'price', precision:5, minMove:0.00001 } });
    bbUpperSeries = chart.addLineSeries({ color: '#607d8b66', lineWidth: 1, priceLineVisible: false, priceFormat:{ type:'price', precision:5, minMove:0.00001 } });
    bbLowerSeries = chart.addLineSeries({ color: '#607d8b66', lineWidth: 1, priceLineVisible: false, priceFormat:{ type:'price', precision:5, minMove:0.00001 } });
  }
  const sh = v => ({...v, time: v.time + TZ_OFFSET});
  ema50Series.setData(_panelOn.e50 && cd.ema50 ? cd.ema50.map(sh) : []);
  bbUpperSeries.setData(_panelOn.bb && cd.bb_upper ? cd.bb_upper.map(sh) : []);
  bbLowerSeries.setData(_panelOn.bb && cd.bb_lower ? cd.bb_lower.map(sh) : []);
}

function _applySubChart(p, cd) {
  const sh = v => ({...v, time: v.time + TZ_OFFSET});
  if (p === 'rsi' && rsiSeries && cd.rsi)       rsiSeries.setData(cd.rsi.map(sh));
  if (p === 'mac' && macHistSeries) {
    if (cd.macd_hist) macHistSeries.setData(cd.macd_hist.map(sh));
    if (macLineSeries && cd.macd)     macLineSeries.setData(cd.macd.map(sh));
    if (macSigSeries  && cd.macd_sig) macSigSeries.setData(cd.macd_sig.map(sh));
  }
  if (p === 'vol' && volSeries && cd.volume)     volSeries.setData(cd.volume.map(sh));
  if (p === 'fc'  && fcLineSeries && cd.forecast) {
    const last   = cd.candles[cd.candles.length - 1];
    const anchor = { time: last.time + TZ_OFFSET, value: last.close };
    fcLineSeries.setData([anchor, ...cd.forecast.map(f => ({time: f.time + TZ_OFFSET, value: f.value}))]);
    fcHiSeries.setData([anchor,  ...cd.forecast.map(f => ({time: f.time + TZ_OFFSET, value: f.hi}))]);
    fcLoSeries.setData([anchor,  ...cd.forecast.map(f => ({time: f.time + TZ_OFFSET, value: f.lo}))]);
    fcChart._candleCtx.setData(cd.candles.slice(-40).map(c => ({...c, time: c.time + TZ_OFFSET})));
  }
}

// ── Horizontal price lines ────────────────────────────────
let priceLines = [];
function clearPriceLines() {
  priceLines.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
  priceLines = [];
}
function addLine(price, color, title) {
  if (!price) return;
  const l = candleSeries.createPriceLine({
    price, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true, title,
  });
  priceLines.push(l);
}
function updatePosLines(pos) {
  clearPriceLines();
  if (pos) {
    addLine(pos.avg_entry, '#00e676', 'ENTRY');
    addLine(pos.stop,      '#ff1744', 'STOP');
    addLine(pos.tp1,       '#00bcd4', 'TP1');
    addLine(pos.tp2,       '#0097a7', 'TP2');
  }
}

// Browser local timezone offset in seconds (e.g. GMT+4 → +14400)
const TZ_OFFSET = -new Date().getTimezoneOffset() * 60;

// ── Chart: load 15m historical candles + live-tick current bar ───────────
async function refreshChart(sym) {
  if (!sym || sym === '—') return;
  try {
    const r  = await fetch('/api/chart?symbol=' + encodeURIComponent(sym));
    if (!r.ok) return;
    const cd = await r.json();
    if (!cd || !cd.candles || !cd.candles.length) return;
    const shiftC = c => ({...c, time: c.time + TZ_OFFSET});
    const shiftV = v => ({...v, time: v.time + TZ_OFFSET});
    candleSeries.setData(cd.candles.map(shiftC));
    // Anchor the live candle to the current 15m bar boundary (local time)
    const last    = cd.candles[cd.candles.length - 1];
    const barTime = Math.floor((Date.now() / 1000 + TZ_OFFSET) / 900) * 900;
    _lastCandle = {
      time:  Math.max(last.time + TZ_OFFSET, barTime),
      open:  barTime > last.time + TZ_OFFSET ? last.close : last.open,
      high:  barTime > last.time + TZ_OFFSET ? last.close : last.high,
      low:   barTime > last.time + TZ_OFFSET ? last.close : last.low,
      close: last.close,
    };
    if (cd.ema9  && cd.ema9.length)  ema9Series.setData(cd.ema9.map(shiftV));
    if (cd.ema21 && cd.ema21.length) ema21Series.setData(cd.ema21.map(shiftV));
    _lastChartData = cd;
    _applyIndicatorSeries(cd);
    ['rsi','mac','vol','fc'].forEach(p => { if (_panelOn[p]) _applySubChart(p, cd); });
    updatePosLines(_lastOpenPos);
  } catch(e) { console.error('chart fetch:', e); }
}

// ── Real-time SSE stream ──────────────────────────────────
let sseRetries = 0;
function connectSSE() {
  const es = new EventSource('/api/stream');
  es.onmessage = (e) => {
    try {
      const s = JSON.parse(e.data);
      renderCards(s);
      _lastOpenPos = s.open_pos || null;
      renderTx(s.transactions || []);
      renderLog(s.last_log || []);
      renderScanner(s);
      syncHaltButtons(s.halt || false);
      renderAnalyst(s);
      // On symbol switch or new chart data: reload historical candles
      const sym = s.symbol || '';
      if (sym && sym !== _currentSym) {
        _currentSym = sym;
        _lastCandle = null;
        _lastChartTs = s.chart_ts || 0;
        refreshChart(sym);
      } else if (s.chart_ts && s.chart_ts !== _lastChartTs) {
        _lastChartTs = s.chart_ts;
        refreshChart(sym);
      }
      // Live-tick the rightmost candle every second from SSE price
      if (s.price && s.price > 0 && candleSeries) {
        const p = s.price;
        if (!_lastCandle) {
          if (sym) refreshChart(sym);
        } else {
          const barTime = Math.floor((Date.now() / 1000 + TZ_OFFSET) / 900) * 900;
          if (barTime > _lastCandle.time) {
            _lastCandle = { time: barTime, open: p, high: p, low: p, close: p };
          } else {
            _lastCandle = {
              time:  _lastCandle.time,
              open:  _lastCandle.open,
              high:  Math.max(_lastCandle.high, p),
              low:   Math.min(_lastCandle.low,  p),
              close: p,
            };
          }
          try { candleSeries.update(_lastCandle); } catch(e) { console.warn('chart tick:', e); }
        }
        updatePosLines(_lastOpenPos);
      }
      sseRetries = 0;
      document.getElementById('sse-status').textContent = '● live';
      document.getElementById('sse-status').style.color = '#00e676';
    } catch(err) { console.error(err); }
  };
  es.onerror = () => {
    document.getElementById('sse-status').textContent = '○ reconnecting…';
    document.getElementById('sse-status').style.color = '#ff1744';
    es.close();
    sseRetries++;
    setTimeout(connectSSE, Math.min(1000 * sseRetries, 8000));
  };
}

// Fallback poll if SSE not available
async function refresh() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    renderCards(s);
    _lastOpenPos = s.open_pos || null;
    _currentSym  = s.symbol || '';
    renderTx(s.transactions || []);
    renderLog(s.last_log || []);
    renderScanner(s);
    if (_currentSym) refreshChart(_currentSym);
  } catch(e) { console.error(e); }
}

// ── Coin selector ─────────────────────────────────────────
function renderCoinSelector(topCoins, coinMode) {
  const el = document.getElementById('coin-selector');
  if (!el) return;
  const coins = (topCoins || []).slice(0, 8);
  const active = coinMode || 'auto';
  let html = `<button class="cbtn auto ${active==='auto'?'active':''}" onclick="selectCoin('auto')">&#8635; Auto</button>`;
  coins.forEach(sym => {
    const label = sym.replace('USDT', '');
    const isActive = active === sym;
    html += `<button class="cbtn ${isActive?'active':''}" onclick="selectCoin('${sym}')">${label}</button>`;
  });
  el.innerHTML = html;
}

async function selectCoin(sym) {
  _coinMode = sym;
  renderCoinSelector(_topCoins, _coinMode);
  await fetch('/instruction', {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action:'SWITCH_COIN', symbol: sym, source:'dashboard'}),
  }).catch(()=>{});
}

// ── Strategy selector (multi-select: 1 primary + up to 2 secondary) ────────
let _strategyKey  = 'momentum';      // primary alias (_strategyKeys[0])
let _strategyKeys = ['momentum'];    // full active set: [primary, ...secondaries]
const _STRATEGIES = [
  {key:'momentum',     label:'Volatility Chase · Momentum'},
  {key:'ema_cross',    label:'EMA Cross · Crossover'},
  {key:'rsi_reversal', label:'RSI · Mean Reversion'},
  {key:'macd_cross',   label:'MACD · Crossover'},
  {key:'bb_breakout',  label:'Bollinger · Band Bounce'},
];

function renderStrategySelector(keys) {
  const el = document.getElementById('strategy-selector');
  if (!el) return;
  const primary = Array.isArray(keys) ? keys[0] : keys;
  const all     = Array.isArray(keys) ? keys : [keys];
  el.innerHTML = _STRATEGIES.map(s => {
    let cls = 'sbtn';
    if (s.key === primary)         cls = 'sbtn active';
    else if (all.includes(s.key))  cls = 'sbtn secondary';
    return `<button class="${cls}" onclick="toggleStrategy('${s.key}')">${s.label}</button>`;
  }).join('');
}

async function toggleStrategy(key) {
  if (_strategyKeys.includes(key)) {
    if (_strategyKeys.length === 1) return;   // can't deselect the only strategy
    _strategyKeys = _strategyKeys.filter(k => k !== key);
  } else {
    if (_strategyKeys.length >= 3) return;    // max 3 simultaneous strategies
    _strategyKeys = [..._strategyKeys, key];
  }
  _strategyKey = _strategyKeys[0];
  renderStrategySelector(_strategyKeys);
  await fetch('/instruction', {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action:'SET_STRATEGY', strategies: _strategyKeys, source:'dashboard'}),
  }).catch(()=>{});
}

function renderCards(s) {
  const upSec = Math.floor(Date.now()/1000 - startedAt);
  const h = Math.floor(upSec/3600), m = Math.floor((upSec%3600)/60);
  const fmtUsd = v => v ? v.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
  setEl('c-usdt-bal', s.usdt_balance != null ? '$' + fmtUsd(s.usdt_balance) : '—');
  setEl('c-eq',  s.equity ? '$' + fmtUsd(s.equity) : '—');
  setEl('c-sym', s.symbol || '—');
  const coinQty = s.coin_qty || 0;
  const coinVal = s.coin_value_usdt || 0;
  const coinAsset = s.coin_asset || '—';
  setEl('c-coin-qty', coinQty > 0 ? coinQty.toLocaleString('en',{maximumSignificantDigits:6}) + ' ' + coinAsset : '—');
  setEl('c-coin-val', coinVal > 0 ? '$' + fmtUsd(coinVal) : '');
  const modeEl = document.getElementById('c-mode');
  const modeMap = {testnet:'TESTNET', demo:'DEMO', live:'⚠ LIVE'};
  const modeClr = {testnet:'#ffd600', demo:'#00bcd4', live:'#00e676'};
  const md = s.trading_mode || 'testnet';
  modeEl.textContent = modeMap[md] || md.toUpperCase();
  modeEl.style.color = modeClr[md] || '#fff';
  setEl('c-reg', s.regime || '—');
  setEl('c-pos', s.positions ?? '—');
  const dd = (s.daily_dd||0).toFixed(2);
  const ddEl = document.getElementById('c-dd');
  ddEl.textContent = dd + '%';
  ddEl.className = 'val ' + (parseFloat(dd) > 3 ? 'r' : 'g');
  // Session P&L
  const spnl    = s.session_pnl    || 0;
  const spnlPct = s.session_pnl_pct || 0;
  const spnlEl    = document.getElementById('session-pnl');
  const spnlPctEl = document.getElementById('session-pnl-pct');
  if (spnlEl) {
    const sign = spnl >= 0 ? '+' : '';
    spnlEl.textContent  = sign + '$' + Math.abs(spnl).toFixed(2);
    spnlEl.style.color  = spnl >= 0 ? '#00e676' : '#ff1744';
    if (spnl < 0) spnlEl.textContent = '-$' + Math.abs(spnl).toFixed(2);
    spnlPctEl.textContent = '(' + (spnlPct >= 0 ? '+' : '') + spnlPct.toFixed(2) + '%)';
    spnlPctEl.style.color = spnlPct >= 0 ? '#00e676' : '#ff1744';
  }
  if (s.session_start_equity) {
    document.getElementById('session-start-eq').textContent = '$' + s.session_start_equity.toFixed(2);
  }
  setEl('c-sig', s.last_signal || '—');
  setEl('c-up',  `${h}h ${m}m`);
  setEl('c-interval', s.interval || '—');
  setEl('c-risk', s.risk_pct ? (s.risk_pct*100).toFixed(0)+'% / '+(s.max_trade_pct*100).toFixed(0)+'%' : '—');
  setEl('c-cycle', s.cycle_sleep ? s.cycle_sleep+'s' : '—');
  setEl('c-strat', s.strategy || '—');
  document.getElementById('chart-sym-title').textContent = s.symbol || '—';
  document.getElementById('chart-sym-label').textContent = (s.symbol||'—') + ' · ' + (s.interval||'1H');
  // Update Buy/Sell button labels with live price
  if (s.price && s.price > 0) {
    const priceStr = s.price.toFixed(5);
    const buyBtn  = document.getElementById('btn-buy');
    const sellBtn = document.getElementById('btn-sell');
    if (buyBtn)  buyBtn.textContent  = '▲ Buy @ ' + priceStr;
    if (sellBtn) sellBtn.textContent = '▼ Sell @ ' + priceStr;
  }
  // Refresh coin buttons only when the list itself changes — never touch _coinMode from server
  const incoming = (s.top_coins || []).join(',');
  if (incoming !== _topCoins.join(',')) {
    _topCoins = s.top_coins || [];
    renderCoinSelector(_topCoins, _coinMode);   // preserve user's current selection
  }
  // Sync strategy selector when agent reports a strategy change
  if (s.strategy_keys && JSON.stringify(s.strategy_keys) !== JSON.stringify(_strategyKeys)) {
    _strategyKeys = s.strategy_keys;
    _strategyKey  = _strategyKeys[0] || 'momentum';
    renderStrategySelector(_strategyKeys);
  }
  // Strategy advisor bar
  if (s.advised_strategy && s.advised_strategy !== '—') {
    const bar = document.getElementById('strategy-advice-bar');
    if (bar) {
      bar.style.display = 'block';
      setEl('advice-label', s.advised_strategy_label || s.advised_strategy);
      setEl('advice-score', (s.advised_strategy_score || 0).toFixed(1) + '/10');
      setEl('advice-reason', s.advised_strategy_reason || '');
      // Highlight the advised button if it differs from the active strategy
      document.querySelectorAll('.sbtn').forEach(btn => {
        btn.style.outline = '';
      });
      if (!_strategyKeys.includes(s.advised_strategy)) {
        const advBtn = document.querySelector(`.sbtn[onclick*="${s.advised_strategy}"]`);
        if (advBtn) advBtn.style.outline = '1px dashed #00e676';
      }
    }
  }
  // Portfolio card
  if (s.portfolio && s.portfolio.total_assets) {
    const pt = s.portfolio;
    setEl('port-total', '$' + pt.total_assets.toFixed(2));
    setEl('port-usdt',  '$' + pt.usdt_free.toFixed(2));
    setEl('port-coins', '$' + (pt.coin_value || 0).toFixed(2));
    setEl('port-earn',  '$' + (pt.earn_value || 0).toFixed(2));
    const pnlColor = pt.pnl_usdt >= 0 ? '#00e676' : '#ff1744';
    const pnlSign  = pt.pnl_usdt >= 0 ? '+' : '';
    const portPnlEl = document.getElementById('port-pnl');
    if (portPnlEl) portPnlEl.innerHTML = `<span style="color:${pnlColor}">${pnlSign}$${pt.pnl_usdt.toFixed(2)} (${pnlSign}${pt.pnl_pct.toFixed(2)}%)</span>`;
    setEl('port-start', '$' + pt.day_start.toFixed(2));
  }
  // Fear & Greed
  const fg = s.fear_greed || {value: 50, label: 'Neutral'};
  const fgEl = document.getElementById('c-fg');
  if (fgEl) {
    fgEl.textContent = fg.value + ' · ' + (fg.label || 'Neutral');
    const v = fg.value;
    fgEl.style.color = v <= 20 ? '#ff1744' : v <= 40 ? '#ff9800' : v <= 60 ? '#ffd600' : v <= 80 ? '#66bb6a' : '#00e676';
  }
  // Pool bar update
  if (s.pool_state && Object.keys(s.pool_state).length) {
    const ps = s.pool_state;
    setEl('pb-slot', s.pool_slot !== undefined ? s.pool_slot : '—');
    const mySlot = s.pool_slot;
    const slots  = ps.slots || {};
    const others = Object.entries(slots)
      .filter(([k,v]) => v && parseInt(k) !== mySlot)
      .map(([k,v]) => v.symbol.replace('USDT',''));
    setEl('pb-others', others.length ? others.join(', ') : 'none');
    // Rough budget display: pool_state doesn't carry budget directly, show other agents' open
    const othersOpen = Object.entries(slots)
      .filter(([k,v]) => v && parseInt(k) !== mySlot)
      .reduce((acc,[,v]) => acc + (v.open_usdt||0), 0);
    setEl('pb-budget', '$' + ((s.equity || 0) * 0.7 - othersOpen).toFixed(0) + ' avail');
    setEl('pb-ts', new Date(ps.ts*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}));
  }
}

function renderTx(txs) {
  const wrap = document.getElementById('tx-wrap');
  if (!txs.length) {
    wrap.innerHTML = '<p class="none">No transactions yet — Raptor FleetEdge is scanning the market.</p>';
    return;
  }
  let rows = '';
  txs.forEach(t => {
    const pnl = t.pnl !== '' && t.pnl !== undefined ? parseFloat(t.pnl) : null;
    const pnlStr = pnl !== null ? (pnl >= 0 ? '+' : '') + pnl.toFixed(2) : '—';
    const pnlCls = pnl !== null ? (pnl >= 0 ? 'pp' : 'pn') : '';
    const sideCls = t.side === 'BUY' ? 'buy' : t.side === 'SELL' ? 'sell' : 'close';
    rows += `<tr>
      <td>${t.ts||''}</td>
      <td class="${sideCls}">${t.side||''}</td>
      <td>${t.symbol||''}</td>
      <td class="num">${t.qty||''}</td>
      <td class="num">${t.price ? parseFloat(t.price).toFixed(5) : '—'}</td>
      <td class="num">${t.stop  ? parseFloat(t.stop ).toFixed(5) : '—'}</td>
      <td class="num">${t.tp1   ? parseFloat(t.tp1  ).toFixed(5) : '—'}</td>
      <td class="num">${t.tp2   ? parseFloat(t.tp2  ).toFixed(5) : '—'}</td>
      <td class="num">${t.risk  ? parseFloat(t.risk ).toFixed(5) : '—'}</td>
      <td class="num ${pnlCls}">${pnlStr}</td>
      <td>${t.status||''}</td>
    </tr>`;
  });
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Time</th><th>Side</th><th>Symbol</th>
      <th style="text-align:right">Qty</th>
      <th style="text-align:right">Fill</th>
      <th style="text-align:right">Stop</th>
      <th style="text-align:right">TP1</th>
      <th style="text-align:right">TP2</th>
      <th style="text-align:right">Risk</th>
      <th style="text-align:right">P&L</th>
      <th>Status</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLog(lines) {
  allLogLines = lines;
  filterLog();
}

function filterLog() {
  const f = document.getElementById('log-filter').value.toLowerCase();
  const body = document.getElementById('log-body');
  const filtered = f ? allLogLines.filter(l => l.toLowerCase().includes(f)) : allLogLines;
  body.innerHTML = filtered.slice(-200).map(l => {
    let cls = 'info';
    if (l.includes('SELL_SKIPPED_NO_BALANCE') || l.includes('SIZING_CALC')) cls = 'err';
    else if (l.includes('ERROR') || l.includes('FAIL'))     cls = 'err';
    else if (l.includes('HALT') || l.includes('WARN'))      cls = 'warn';
    else if (l.includes('FILLED') || l.includes('TRADE') || l.includes('TP') || l.includes('STOP_HIT')) cls = 'trade';
    else if (l.includes('SWITCH') || l.includes('SCAN'))    cls = 'switch';
    const esc = l.replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return `<div class="log-line ${cls}">${esc}</div>`;
  }).join('');
  scrollBottom();
}

function clearFilter() {
  document.getElementById('log-filter').value = '';
  filterLog();
}

function scrollBottom() {
  const b = document.getElementById('log-body');
  b.scrollTop = b.scrollHeight;
}

function setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ── Market order buttons ──────────────────────────────────
async function marketOrder(side) {
  const label = side === 'BUY' ? '▲ Market BUY' : '▼ Market SELL';
  if (!confirm(`Send ${label} signal to Raptor FleetEdge?\nThis will execute immediately at market price.`)) return;
  const r = await fetch('/instruction', {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action: side, source: 'dashboard'}),
  }).catch(() => null);
  if (r && r.ok) {
    const flash = document.createElement('div');
    flash.textContent = `${label} sent`;
    flash.style.cssText = 'position:fixed;top:16px;right:20px;background:'+(side==='BUY'?'#00e676':'#ff1744')+';color:'+(side==='BUY'?'#000':'#fff')+';padding:8px 18px;border-radius:6px;font-family:monospace;font-size:.8rem;font-weight:bold;z-index:9999';
    document.body.appendChild(flash);
    setTimeout(() => flash.remove(), 2500);
  }
}

// ── Resume Auto button ───────────────────────────────────
async function resumeAuto() {
  const r = await fetch('/instruction', {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action: 'RESUME_AUTO', source: 'dashboard'}),
  }).catch(() => null);
  if (r && r.ok) {
    const flash = document.createElement('div');
    flash.textContent = '⟳ Auto-scanner enabled';
    flash.style.cssText = 'position:fixed;top:16px;right:20px;background:#00e5ff;color:#000;padding:8px 18px;border-radius:6px;font-family:monospace;font-size:.8rem;font-weight:bold;z-index:9999';
    document.body.appendChild(flash);
    setTimeout(() => flash.remove(), 3000);
  }
}

// ── Force BTC button ─────────────────────────────────────
async function forceBTC() {
  if (!confirm('Close any open position and switch to BTCUSDT immediately?')) return;
  const r = await fetch('/instruction', {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action: 'FORCE_BTC', source: 'dashboard'}),
  }).catch(() => null);
  if (r && r.ok) {
    const flash = document.createElement('div');
    flash.textContent = '₿ Switching to BTC…';
    flash.style.cssText = 'position:fixed;top:16px;right:20px;background:#f7931a;color:#000;padding:8px 18px;border-radius:6px;font-family:monospace;font-size:.8rem;font-weight:bold;z-index:9999';
    document.body.appendChild(flash);
    setTimeout(() => flash.remove(), 3000);
  }
}

// ── Stop / Resume buttons ─────────────────────────────────
let _halted = false;
async function stopAgent() {
  if (!confirm('Stop Raptor FleetEdge trading? (can resume without restart)')) return;
  await fetch('/instruction', {
    method:'POST',
    headers:{'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action:'HALT', source:'dashboard'}),
  }).catch(()=>{});
  _halted = true;
  document.getElementById('stop-btn').style.display='none';
  document.getElementById('start-btn').style.display='inline';
}
async function startAgent() {
  await fetch('/instruction', {
    method:'POST',
    headers:{'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action:'RESUME', source:'dashboard'}),
  }).catch(()=>{});
  _halted = false;
  document.getElementById('stop-btn').style.display='inline';
  document.getElementById('start-btn').style.display='none';
}

// sync button state with halt flag from SSE
function syncHaltButtons(halt) {
  document.getElementById('stop-btn').style.display  = halt ? 'none'   : 'inline';
  document.getElementById('start-btn').style.display = halt ? 'inline' : 'none';
}

// ── Market Scanner ────────────────────────────────────────
function renderScanner(s) {
  const rows = (s.scanner_ranked || []).slice(0, 4);
  const best = s.scanner_best || '';
  const active = s.symbol || '';
  const ts = s.scanner_ts ? new Date(s.scanner_ts*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—';
  document.getElementById('scanner-ts').textContent = 'last scan: ' + ts;
  const tbody = document.getElementById('scanner-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="color:#fff;padding:10px 8px">No scan data yet — runs every 60s</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r,i) => {
    const isBest   = r.symbol === best;
    const isActive = r.symbol === active;
    const bg       = isActive ? '#0a1f0a' : isBest ? '#0d1520' : '';
    const badge    = isActive ? '<span style="background:#00e676;color:#000;padding:1px 6px;border-radius:3px;font-size:.6rem;margin-left:4px">TRADING</span>'
                   : isBest   ? '<span style="background:#00b0ff;color:#000;padding:1px 6px;border-radius:3px;font-size:.6rem;margin-left:4px">NEXT</span>' : '';
    const trendClr = r.trend && r.trend.includes('BULL') ? '#00e676' : r.trend && r.trend.includes('BEAR') ? '#ff1744' : '#888';
    const chgClr   = r.chg_pct >= 0 ? '#00e676' : '#ff1744';
    const regClr   = r.regime === 'TRENDING' ? '#00e676' : r.regime === 'VOLATILE' ? '#ff6d00' : '#888';
    return `<tr style="border-bottom:1px solid #141414;background:${bg}">
      <td style="padding:5px 8px;color:#fff">${i+1}</td>
      <td style="padding:5px 8px;font-weight:bold;color:#fff">${r.symbol.replace('USDT','')}${badge}</td>
      <td style="padding:5px 8px;text-align:right;color:#ffd600">${r.score.toFixed(2)}</td>
      <td style="padding:5px 8px;text-align:right;color:#82b1ff">${r.atr_pct ? r.atr_pct.toFixed(3)+'%' : '—'}</td>
      <td style="padding:5px 8px;text-align:right;color:${chgClr}">${r.chg_pct >= 0 ? '+' : ''}${r.chg_pct}%</td>
      <td style="padding:5px 8px;text-align:right;color:#fff">$${r.vol_m}M</td>
      <td style="padding:5px 8px;text-align:center;color:${trendClr}">${r.trend || '—'}</td>
      <td style="padding:5px 8px;text-align:center;color:${regClr};font-weight:bold;font-size:.68rem">${r.regime || '—'}</td>
      <td style="padding:5px 8px;text-align:center">
        <button onclick="selectCoin('${r.symbol}')" style="background:#161616;color:#00e5ff;border:1px solid #00e5ff;padding:2px 8px;border-radius:3px;font-size:.65rem;cursor:pointer">Switch</button>
      </td>
    </tr>`;
  }).join('');
}

// ── Binance Orders ────────────────────────────────────────
async function loadOrders() {
  document.getElementById('orders-refresh').textContent = '↻ loading…';
  try {
    const r = await fetch('/api/orders');
    const d = await r.json();
    const openWrap = document.getElementById('open-orders-wrap');
    const histWrap = document.getElementById('order-history-wrap');
    if (d.error) {
      openWrap.innerHTML = `<span style="color:#ff6d6d">${d.error}</span>`;
      histWrap.innerHTML = '';
    } else {
      openWrap.innerHTML = d.open.length
        ? d.open.map(o => `<div style="border-bottom:1px solid #1a1a1a;padding:4px 0">
            <span style="color:${o.side==='BUY'?'#00e676':'#ff1744'};font-weight:bold">${o.side}</span>
            &nbsp;${o.qty} @ <span style="color:#00e5ff">${parseFloat(o.price)>0?'$'+parseFloat(o.price).toLocaleString():'MARKET'}</span>
            &nbsp;<span style="color:#fff">${o.status}</span>
            &nbsp;<span style="color:#444;font-size:.62rem">#${o.id}</span>
          </div>`).join('')
        : '<span style="color:#fff">No open orders</span>';
      histWrap.innerHTML = d.history.length
        ? d.history.map(o => `<div style="border-bottom:1px solid #1a1a1a;padding:4px 0">
            <span style="color:#444;font-size:.62rem">${o.ts}</span>
            &nbsp;<span style="color:${o.side==='BUY'?'#00e676':'#ff1744'};font-weight:bold">${o.side}</span>
            &nbsp;${o.qty} @ <span style="color:#00e5ff">$${parseFloat(o.price).toLocaleString()}</span>
            &nbsp;<span style="color:#fff;font-size:.62rem">${o.status}</span>
          </div>`).join('')
        : '<span style="color:#fff">No fills yet</span>';
    }
  } catch(e) { console.error('orders fetch:', e); }
  document.getElementById('orders-refresh').textContent = '⟳ refresh';
}

// ── Rule Analyst ──────────────────────────────────────────
let _aiEnabled = false;

async function toggleAnalyst() {
  const newState = !_aiEnabled;
  _aiEnabled = newState;
  const btn = document.getElementById('ai-toggle-btn');
  const panel = document.getElementById('ai-panel');
  if (btn) {
    btn.textContent = newState ? 'Disable' : 'Enable';
    btn.style.background = newState ? '#0a1f0a' : '#111';
    btn.style.borderColor = newState ? '#00e676' : '#333';
    btn.style.color = newState ? '#00e676' : '#aaa';
  }
  if (panel) panel.style.display = newState ? 'block' : 'none';
  const action = newState ? 'ANALYST_ON' : 'ANALYST_OFF';
  await fetch('/instruction', {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action, source:'dashboard'})
  });
}

function renderAnalyst(s) {
  const enabled = s.analyst_enabled || false;
  const btn = document.getElementById('ai-toggle-btn');
  const panel = document.getElementById('ai-panel');
  if (!btn || !panel) return;
  _aiEnabled = enabled;
  btn.textContent = enabled ? 'Disable' : 'Enable';
  btn.style.background = enabled ? '#0a1f0a' : '#111';
  btn.style.borderColor = enabled ? '#00e676' : '#333';
  btn.style.color = enabled ? '#00e676' : '#aaa';
  panel.style.display = enabled ? 'block' : 'none';
  if (!enabled) return;
  const a = s.analysis || {};
  if (!a.sentiment) return;
  const sentColors = {BULLISH:'#00e676', NEUTRAL:'#ffd600', BEARISH:'#ff1744'};
  const entryColors = {EXCELLENT:'#00e676', GOOD:'#82ff82', POOR:'#ffd600', AVOID:'#ff1744'};
  const riskColors  = {LOW:'#00e676', MEDIUM:'#ffd600', HIGH:'#ff1744'};
  const el = (id) => document.getElementById(id);
  el('ai-sentiment').textContent = a.sentiment || '—';
  el('ai-sentiment').style.color = sentColors[a.sentiment] || '#fff';
  el('ai-confidence').textContent = a.confidence != null ? a.confidence + '%' : '—';
  el('ai-trend').textContent = a.trend_strength || '—';
  el('ai-momentum').textContent = a.momentum || '—';
  el('ai-entry').textContent = a.entry_quality || '—';
  el('ai-entry').style.color = entryColors[a.entry_quality] || '#fff';
  el('ai-risk').textContent = a.risk_level || '—';
  el('ai-risk').style.color = riskColors[a.risk_level] || '#fff';
  el('ai-support').textContent = a.support ? a.support.toLocaleString('en',{maximumSignificantDigits:6}) : '—';
  el('ai-resistance').textContent = a.resistance ? a.resistance.toLocaleString('en',{maximumSignificantDigits:6}) : '—';
  el('ai-insight').textContent = a.insight || '—';
  el('ai-watch').textContent = a.watch || '—';
  el('ai-ts').textContent = a.ts ? 'updated ' + new Date(a.ts*1000).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '';
}

// ── Theme toggle ──────────────────────────────────────────
const _CHART_DARK = { bg:'#0d0d0d', text:'#fff', grid:'#141414', border:'#1e1e1e' };
const _CHART_DAY  = { bg:'#f4f5f7', text:'#333', grid:'#dde0e6', border:'#d4d8de' };
function _applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = theme === 'day' ? '☾ Night' : '☀ Day';
  const c = theme === 'day' ? _CHART_DAY : _CHART_DARK;
  if (typeof chart !== 'undefined' && chart) {
    chart.applyOptions({
      layout: { background: { color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border },
    });
  }
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'night';
  const next = cur === 'day' ? 'night' : 'day';
  localStorage.setItem('rfe-theme', next);
  _applyTheme(next);
}
(function() {
  const saved = localStorage.getItem('rfe-theme') || 'night';
  _applyTheme(saved);
})();
// ── Boot ──────────────────────────────────────────────────
initChart();
// Initialise default-on sub-charts so they're ready when first chart data arrives
_initSubChart('mac');
renderCoinSelector([], 'auto');
renderStrategySelector(_strategyKeys);
refresh();
connectSSE();
loadOrders();
setInterval(() => { if (_currentSym) refreshChart(_currentSym); }, 60 * 1000); // fallback chart refresh every 60s
setInterval(loadOrders, 30000);
</script>
</body>
</html>
"""

TX_ROW = """<tr>
  <td>{ts}</td><td class="{sc}">{side}</td><td>{symbol}</td>
  <td class="num">{qty}</td><td class="num">{price}</td>
  <td class="num">{stop}</td><td class="num">{tp1}</td><td class="num">{tp2}</td>
  <td class="num">{risk}</td><td class="num {pc}">{pnl}</td><td>{status}</td>
</tr>"""


def _auth(request: web.Request) -> bool:
    token = request.headers.get("X-Agent-Token", "")
    if token == "internal":   # dashboard self-control
        return True
    if not cfg.AUTHORIZED_AGENT_TOKENS:
        return False
    return token in cfg.AUTHORIZED_AGENT_TOKENS


def update_state(**kwargs):
    _state.update(kwargs)


def push_log(entry: str):
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%H:%M:%S")
    _state["last_log"].append(f"{ts} {entry}")
    if len(_state["last_log"]) > 200:
        _state["last_log"].pop(0)


def push_transaction(tx: dict):
    tx["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # When a close comes in, update the matching open entry so it no longer
    # appears as an active position (prevents stale "OPEN" rows after stop/TP)
    side = tx.get("side", "")
    if "CLOSE" in side:
        sym = tx.get("symbol", "")
        for existing in _state["transactions"]:
            if existing.get("symbol") == sym and existing.get("status") == "OPEN":
                existing["status"] = tx.get("status", "CLOSED")
                existing["pnl"]    = tx.get("pnl", "")
                break
    _state["transactions"].insert(0, tx)
    if len(_state["transactions"]) > 200:
        _state["transactions"].pop()


def update_chart(symbol: str, candles: list, **series):
    """Push OHLCV + all indicator series for chart rendering. Called from agent each cycle."""
    _state["chart_data"][symbol] = {"candles": candles, **series}
    _state["chart_ts"] = time.time()


def update_open_pos(pos_info: dict | None):
    """Store current open position info for chart overlays."""
    _state["open_pos"] = pos_info


URGENT_ACTIONS = {"FORCE_BTC", "HALT", "CLOSE_ALL", "SWITCH_COIN", "RESUME_AUTO", "SELL", "SET_STRATEGY"}  # wake the main loop immediately

# Module-level wake event — agent imports and awaits this during its sleep
wake_event: asyncio.Event | None = None

def set_wake_event(ev: asyncio.Event):
    global wake_event
    wake_event = ev


class InstructionServer:
    def __init__(self, signal_queue: asyncio.Queue):
        self._queue = signal_queue
        self._app   = web.Application()
        self._app.router.add_get("/",             self._dashboard)
        self._app.router.add_get("/api/stream",   self._stream)
        self._app.router.add_get("/api/state",    self._api_state)
        self._app.router.add_get("/api/chart",    self._chart)
        self._app.router.add_get("/api/orders",   self._orders)
        self._app.router.add_post("/instruction", self._handle)
        self._app.router.add_get("/status",       self._status)
        self._app.router.add_get("/api/pool",     self._pool_state)

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={
            "Content-Type":      "text/event-stream",
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        })
        await resp.prepare(request)
        try:
            while True:
                # Refresh pool state from file each SSE tick
                try:
                    import json as _j
                    from pathlib import Path as _P
                    _pf = _P("/tmp/rfe_equity_pool.json")
                    if _pf.exists():
                        _state["pool_state"] = _j.loads(_pf.read_text())
                except Exception:
                    pass
                # Exclude chart_data — served separately via /api/chart
                payload = {k: v for k, v in _state.items()
                           if k not in ("chart_data", "last_log")}
                payload["last_log"] = _state["last_log"][-80:]
                data = json.dumps(payload)
                await resp.write(f"data: {data}\n\n".encode())
                await asyncio.sleep(1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _chart(self, request: web.Request) -> web.Response:
        sym  = request.query.get("symbol", _state.get("symbol", ""))
        data = _state.get("chart_data", {}).get(sym, {})
        return web.json_response(data)

    async def _orders(self, request: web.Request) -> web.Response:
        """Fetch open orders + recent fills directly from Binance."""
        import hmac as _hmac, hashlib as _hs, time as _t
        import aiohttp as _aio
        import datetime as _dt
        def _sign(params):
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            return _hmac.new(cfg.BINANCE_API_SECRET.encode(), qs.encode(), _hs.sha256).hexdigest()
        sym = _state.get("symbol", cfg.SYMBOL) or cfg.SYMBOL
        hdrs = {"X-MBX-APIKEY": cfg.BINANCE_API_KEY}
        result = {"open": [], "history": []}
        try:
            async with _aio.ClientSession(headers=hdrs) as s:
                # Open orders
                p = {"symbol": sym, "timestamp": int(_t.time()*1000), "recvWindow": 5000}
                p["signature"] = _sign(p)
                async with s.get(cfg.SPOT_BASE_URL + "/api/v3/openOrders", params=p) as r:
                    r.raise_for_status()
                    for o in await r.json():
                        result["open"].append({
                            "id": o["orderId"], "side": o["side"],
                            "qty": o["origQty"], "filled": o["executedQty"],
                            "price": o["price"], "status": o["status"],
                            "ts": _dt.datetime.fromtimestamp(o["time"]//1000).strftime("%Y-%m-%d %H:%M"),
                        })
                # Recent order history (last 20)
                p2 = {"symbol": sym, "timestamp": int(_t.time()*1000), "recvWindow": 5000, "limit": 20}
                p2["signature"] = _sign(p2)
                async with s.get(cfg.SPOT_BASE_URL + "/api/v3/allOrders", params=p2) as r:
                    r.raise_for_status()
                    for o in reversed(await r.json()):
                        if o["status"] not in ("FILLED", "PARTIALLY_FILLED"):
                            continue
                        fill_price = o.get("cummulativeQuoteQty", "0")
                        exec_qty   = float(o.get("executedQty", 0))
                        avg_price  = (float(fill_price) / exec_qty) if exec_qty else float(o["price"])
                        result["history"].append({
                            "id": o["orderId"], "side": o["side"],
                            "qty": o["executedQty"],
                            "price": f"{avg_price:.2f}",
                            "status": o["status"],
                            "ts": _dt.datetime.fromtimestamp(o["time"]//1000).strftime("%Y-%m-%d %H:%M"),
                        })
        except Exception as e:
            result["error"] = str(e)
        return web.json_response(result)

    async def _dashboard(self, request: web.Request) -> web.Response:
        _mode_map = {
            "testnet": ("TESTNET", "testnet", "testnet.binance.vision"),
            "demo":    ("DEMO",    "demo",    "demo-api.binance.com"),
            "live":    ("⚠ LIVE", "live",    "api.binance.com"),
        }
        mode, mode_cls, api_url = _mode_map.get(cfg.TRADING_MODE, ("TESTNET","testnet","testnet.binance.vision"))
        if _state["halt"]:
            mode, mode_cls = "HALTED", "halt"
        # Re-parse DASHBOARD_HTML from disk on each request so CSS/HTML changes
        # take effect on browser refresh without an agent restart.
        try:
            import re as _re
            src = open(__file__).read()
            live_html = _re.search(r'DASHBOARD_HTML = r"""(.*?)^"""', src, _re.S | _re.M).group(1)
        except Exception:
            live_html = DASHBOARD_HTML
        html = (live_html
                .replace("__MODE__",       mode)
                .replace("__MODE_CLASS__", mode_cls)
                .replace("__API_URL__",    api_url)
                .replace("__AGENT_NAME__", cfg.AGENT_NAME)
                .replace("__STARTED_AT__", str(int(_state["started_at"]))))
        return web.Response(text=html, content_type="text/html")

    async def _api_state(self, request: web.Request) -> web.Response:
        payload = {k: v for k, v in _state.items() if k != "last_log"}
        payload["last_log"] = _state["last_log"][-150:]
        return web.json_response(payload)

    async def _handle(self, request: web.Request) -> web.Response:
        if not _auth(request):
            log("INSTRUCTION_SERVER", "AUTH_FAIL", ip=request.remote)
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        action = body.get("action", "").upper()
        if action not in VALID_ACTIONS:
            return web.json_response({"error": f"unknown action: {action}"}, status=400)
        instruction = {
            "action":   action,
            "symbol":   body.get("symbol", cfg.SYMBOL),
            "qty_pct":  float(body.get("qty_pct", 1.0)),
            "source":   body.get("source", "unknown"),
            "strategy": body.get("strategy", ""),
        }
        log("INSTRUCTION_SERVER", "INSTRUCTION_RECEIVED",
            instr_action=instruction["action"], source=instruction["source"],
            symbol=instruction.get("symbol"), qty_pct=instruction.get("qty_pct"))
        await self._queue.put(instruction)
        # Wake the main loop immediately for urgent actions
        if action in URGENT_ACTIONS and wake_event is not None:
            wake_event.set()
        return web.json_response({"status": "queued", "action": action})

    async def _status(self, request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"agent": "raptor-fleetedge", "status": "running"})

    async def _pool_state(self, request: web.Request) -> web.Response:
        import json as _j
        from pathlib import Path as _P
        try:
            with open("/tmp/rfe_equity_pool.json") as f:
                return web.json_response(_j.load(f))
        except Exception:
            return web.json_response({"slots": {str(i): None for i in range(4)}, "ts": 0})

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, cfg.INSTRUCTION_SERVER_HOST, cfg.INSTRUCTION_SERVER_PORT)
        await site.start()
        log("INSTRUCTION_SERVER", "STARTED",
            host=cfg.INSTRUCTION_SERVER_HOST, port=cfg.INSTRUCTION_SERVER_PORT)
