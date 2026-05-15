"""
Multi-agent instruction interface for dipu.
"""
import asyncio
import json
import time
from aiohttp import web
from logger import log
import config as cfg

VALID_ACTIONS = {"BUY", "SELL", "CLOSE_ALL", "HALT", "RESUME", "STATUS", "SWITCH_MODE", "SWITCH_COIN"}

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
    "claude_thesis": "",
    "coin_mode":     "auto",   # "auto" | "<SYMBOL>"
}

# ── HTML template ──────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dipu — trading agent</title>
<style>#sse-status{font-size:.7rem;margin-left:12px;vertical-align:middle}</style>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#ddd;font-family:'Courier New',monospace;padding:20px}
h1{color:#00e5ff;font-size:1.5rem;margin-bottom:3px}
h2{color:#444;font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;margin:22px 0 8px}
.sub{color:#444;font-size:.78rem;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:6px}
.card{background:#111;border:1px solid #1e1e1e;border-radius:7px;padding:14px}
.card .lbl{color:#444;font-size:.65rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
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
.chart-meta{font-size:.7rem;color:#555}
#chart{width:100%;height:380px}
.legend{display:flex;gap:16px;padding:6px 14px;background:#0d0d0d;font-size:.68rem}
.legend span{display:flex;align-items:center;gap:5px}
.dot{width:10px;height:3px;border-radius:1px}
/* transactions */
table{width:100%;border-collapse:collapse;font-size:.73rem;margin-bottom:20px}
thead th{background:#111;color:#444;font-size:.65rem;text-transform:uppercase;letter-spacing:.07em;padding:7px 9px;border-bottom:1px solid #1e1e1e;text-align:left}
tbody tr{border-bottom:1px solid #141414}
tbody tr:hover{background:#111}
td{padding:6px 9px;vertical-align:middle}
td.buy{color:#00e676;font-weight:bold}
td.sell{color:#ff1744;font-weight:bold}
td.close{color:#888}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pp{color:#00e676}.pn{color:#ff1744}
/* log */
.log-wrap{background:#080808;border:1px solid #1a1a1a;border-radius:8px;display:flex;flex-direction:column;height:300px}
.log-toolbar{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid #1a1a1a;background:#0e0e0e;border-radius:8px 8px 0 0}
.log-toolbar input{background:#0a0a0a;border:1px solid #222;color:#aaa;padding:3px 8px;border-radius:4px;font-family:inherit;font-size:.7rem;flex:1}
.log-toolbar button{background:#161616;border:1px solid #222;color:#666;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:.68rem;font-family:inherit}
.log-toolbar button:hover{color:#aaa}
.log-body{flex:1;overflow-y:auto;padding:10px 12px}
.log-line{font-size:.69rem;line-height:1.7;white-space:pre-wrap;word-break:break-all;border-bottom:1px solid #0f0f0f;padding:1px 0}
.log-line.err{color:#ff6d6d}
.log-line.warn{color:#ffd600}
.log-line.info{color:#7b7b7b}
.log-line.trade{color:#00e676}
.log-line.switch{color:#00e5ff}
.none{color:#333;font-size:.73rem;padding:10px 0}
/* coin selector */
.cbtn{background:#111;border:1px solid #222;color:#666;padding:4px 11px;border-radius:4px;cursor:pointer;font-size:.72rem;font-family:inherit;transition:all .15s}
.cbtn:hover{border-color:#444;color:#aaa}
.cbtn.active{background:#00e5ff;border-color:#00e5ff;color:#000;font-weight:bold}
.cbtn.auto{background:#161616;border-color:#333;color:#00e5ff}
.cbtn.auto.active{background:#00e5ff;color:#000}
</style>
</head>
<body>
<h1>&#9654; __AGENT_NAME__ <span class="badge __MODE_CLASS__">__MODE__</span><span id="sse-status" style="color:#555">○ connecting…</span></h1>
<div class="sub">
  <span style="color:#00e5ff;font-family:monospace">&#128279; __API_URL__</span>
  &nbsp;·&nbsp; autonomous crypto trading agent
  &nbsp;·&nbsp;
  <button id="stop-btn" onclick="stopAgent()" style="background:#ff1744;color:#fff;border:none;padding:3px 12px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;margin-left:6px">&#9646;&#9646; Stop Trading</button>
  <button id="start-btn" onclick="startAgent()" style="background:#00e676;color:#000;border:none;padding:3px 12px;border-radius:4px;font-family:inherit;font-size:.72rem;cursor:pointer;margin-left:4px;display:none">&#9654; Resume Trading</button>
</div>

<div class="grid" id="cards">
  <div class="card"><div class="lbl">Equity (USDT)</div><div class="val" id="c-eq">—</div></div>
  <div class="card"><div class="lbl">Active Coin</div><div class="val g" id="c-sym">—</div></div>
  <div class="card"><div class="lbl">Mode</div><div class="val" id="c-mode">—</div></div>
  <div class="card"><div class="lbl">Regime</div><div class="val y" id="c-reg">—</div></div>
  <div class="card"><div class="lbl">Open Positions</div><div class="val" id="c-pos">—</div></div>
  <div class="card"><div class="lbl">Daily Drawdown</div><div class="val" id="c-dd">—</div></div>
  <div class="card"><div class="lbl">Last Signal</div><div class="val" id="c-sig">—</div></div>
  <div class="card"><div class="lbl">Uptime</div><div class="val g" id="c-up">—</div></div>
  <div class="card"><div class="lbl">Fear &amp; Greed</div><div class="val" id="c-fg" style="font-size:1rem">—</div></div>
</div>

<!-- Coin selector -->
<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
  <span style="color:#444;font-size:.65rem;text-transform:uppercase;letter-spacing:.1em">Coin</span>
  <div id="coin-selector" style="display:flex;gap:6px;flex-wrap:wrap"></div>
</div>

<div id="thesis-box" style="background:#080808;border:1px solid #1a1a1a;border-radius:8px;padding:12px 16px;font-size:.74rem;color:#9e9e9e;min-height:42px;margin-bottom:4px;font-style:italic;line-height:1.6">
  <span style="color:#333;font-size:.65rem;font-style:normal;text-transform:uppercase;letter-spacing:.1em;margin-right:8px">&#9670; Claude</span><span id="thesis-text" style="color:#2a2a2a">add ANTHROPIC_API_KEY to enable AI thesis</span>
</div>

<h2>&#9646; Chart — <span id="chart-sym-title">loading…</span></h2>
<div class="chart-wrap">
  <div class="chart-header">
    <span class="chart-sym" id="chart-sym-label">—</span>
    <span class="chart-meta" id="chart-meta">1H candles · EMA 9/21 · entry / stop / TP levels</span>
  </div>
  <div id="chart"></div>
  <div class="legend">
    <span><div class="dot" style="background:#00e5ff"></div>EMA 9</span>
    <span><div class="dot" style="background:#ffd600"></div>EMA 21</span>
    <span><div class="dot" style="background:#00e676;width:2px;height:12px;border-radius:0"></div>Entry</span>
    <span><div class="dot" style="background:#ff1744;width:2px;height:12px;border-radius:0"></div>Stop</span>
    <span><div class="dot" style="background:#00bcd4;width:2px;height:12px;border-radius:0"></div>TP1/TP2</span>
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
let startedAt = __STARTED_AT__;
let allLogLines = [];
let _currentSym  = '';
let _lastOpenPos = null;
let _coinMode    = 'auto';   // 'auto' | 'BTCUSDT' etc.
let _topCoins    = [];       // last known top coins list

// ── Chart init ────────────────────────────────────────────
function initChart() {
  const el = document.getElementById('chart');
  chart = LightweightCharts.createChart(el, {
    width: el.clientWidth,
    height: 380,
    layout: { background: { color: '#0d0d0d' }, textColor: '#555' },
    grid: { vertLines: { color: '#141414' }, horzLines: { color: '#141414' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#1e1e1e' },
    timeScale: { borderColor: '#1e1e1e', timeVisible: true, secondsVisible: false },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: '#00e676', downColor: '#ff1744',
    borderUpColor: '#00e676', borderDownColor: '#ff1744',
    wickUpColor: '#00e676', wickDownColor: '#ff1744',
  });
  ema9Series  = chart.addLineSeries({ color: '#00e5ff', lineWidth: 1, priceLineVisible: false });
  ema21Series = chart.addLineSeries({ color: '#ffd600', lineWidth: 1, priceLineVisible: false });
  window.addEventListener('resize', () => chart.applyOptions({ width: el.clientWidth }));
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

// ── Chart data — polled separately (heavy payload) ────────
async function refreshChart(sym) {
  if (!sym || sym === '—') return;
  try {
    const r  = await fetch('/api/chart?symbol=' + encodeURIComponent(sym));
    const cd = await r.json();
    if (!cd || !cd.candles || !cd.candles.length) return;
    candleSeries.setData(cd.candles);
    if (cd.ema9  && cd.ema9.length)  ema9Series.setData(cd.ema9);
    if (cd.ema21 && cd.ema21.length) ema21Series.setData(cd.ema21);
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
      updatePosLines(_lastOpenPos);
      renderTx(s.transactions || []);
      renderLog(s.last_log || []);
      syncHaltButtons(s.halt || false);
      // Refresh chart immediately on symbol switch
      const sym = s.symbol || '';
      if (sym && sym !== _currentSym) {
        _currentSym = sym;
        refreshChart(sym);
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

function renderCards(s) {
  const upSec = Math.floor(Date.now()/1000 - startedAt);
  const h = Math.floor(upSec/3600), m = Math.floor((upSec%3600)/60);
  setEl('c-eq',  s.equity ? s.equity.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—');
  setEl('c-sym', s.symbol || '—');
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
  setEl('c-sig', s.last_signal || '—');
  setEl('c-up',  `${h}h ${m}m`);
  document.getElementById('chart-sym-title').textContent = s.symbol || '—';
  document.getElementById('chart-sym-label').textContent = (s.symbol||'—') + ' · 1H';
  // Refresh coin buttons only when the list itself changes — never touch _coinMode from server
  const incoming = (s.top_coins || []).join(',');
  if (incoming !== _topCoins.join(',')) {
    _topCoins = s.top_coins || [];
    renderCoinSelector(_topCoins, _coinMode);   // preserve user's current selection
  }
  // Fear & Greed
  const fg = s.fear_greed || {value: 50, label: 'Neutral'};
  const fgEl = document.getElementById('c-fg');
  if (fgEl) {
    fgEl.textContent = fg.value + ' · ' + (fg.label || 'Neutral');
    const v = fg.value;
    fgEl.style.color = v <= 20 ? '#ff1744' : v <= 40 ? '#ff9800' : v <= 60 ? '#ffd600' : v <= 80 ? '#66bb6a' : '#00e676';
  }
  // Claude thesis — only update when there's real content
  const t = (s.claude_thesis || '').trim();
  const tEl = document.getElementById('thesis-text');
  if (tEl && t) {
    tEl.style.color = '#9e9e9e';
    tEl.textContent = t;
  }
}

function renderTx(txs) {
  const wrap = document.getElementById('tx-wrap');
  if (!txs.length) {
    wrap.innerHTML = '<p class="none">No transactions yet — dipu is scanning the market.</p>';
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
      <td class="num">${t.price ? parseFloat(t.price).toLocaleString('en',{minimumFractionDigits:2}) : '—'}</td>
      <td class="num">${t.stop  ? parseFloat(t.stop ).toFixed(2) : '—'}</td>
      <td class="num">${t.tp1   ? parseFloat(t.tp1  ).toFixed(2) : '—'}</td>
      <td class="num">${t.tp2   ? parseFloat(t.tp2  ).toFixed(2) : '—'}</td>
      <td class="num">${t.risk  ? parseFloat(t.risk ).toFixed(2) : '—'}</td>
      <td class="num ${pnlCls}">${pnlStr}</td>
      <td>${t.status||''}</td>
    </tr>`;
  });
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Time (UTC)</th><th>Side</th><th>Symbol</th>
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
    if (l.includes('ERROR') || l.includes('FAIL'))          cls = 'err';
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

// ── Stop / Resume buttons ─────────────────────────────────
let _halted = false;
async function stopAgent() {
  if (!confirm('Stop dipu trading? (can resume without restart)')) return;
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

// ── Boot ──────────────────────────────────────────────────
initChart();
renderCoinSelector([], 'auto');                              // show Auto button immediately on load
refresh();                                                   // immediate first load
connectSSE();                                               // then switch to real-time stream
setInterval(() => { if (_currentSym) refreshChart(_currentSym); }, 30000); // chart refresh every 30s
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
    _state["last_log"].append(entry)
    if len(_state["last_log"]) > 200:
        _state["last_log"].pop(0)


def push_transaction(tx: dict):
    tx["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    _state["transactions"].insert(0, tx)
    if len(_state["transactions"]) > 200:
        _state["transactions"].pop()


def update_chart(symbol: str, candles: list, ema9: list, ema21: list):
    """Push OHLCV + EMA data for chart rendering. Called from agent each cycle."""
    _state["chart_data"][symbol] = {
        "candles": candles,
        "ema9":    ema9,
        "ema21":   ema21,
    }


def update_open_pos(pos_info: dict | None):
    """Store current open position info for chart overlays."""
    _state["open_pos"] = pos_info


class InstructionServer:
    def __init__(self, signal_queue: asyncio.Queue):
        self._queue = signal_queue
        self._app   = web.Application()
        self._app.router.add_get("/",             self._dashboard)
        self._app.router.add_get("/api/stream",   self._stream)
        self._app.router.add_get("/api/state",    self._api_state)
        self._app.router.add_get("/api/chart",    self._chart)
        self._app.router.add_post("/instruction", self._handle)
        self._app.router.add_get("/status",       self._status)

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
                # Exclude chart_data — served separately via /api/chart
                payload = {k: v for k, v in _state.items()
                           if k not in ("chart_data", "last_log")}
                payload["last_log"] = _state["last_log"][-80:]
                data = json.dumps(payload)
                await resp.write(f"data: {data}\n\n".encode())
                await asyncio.sleep(2)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def _chart(self, request: web.Request) -> web.Response:
        sym  = request.query.get("symbol", _state.get("symbol", ""))
        data = _state.get("chart_data", {}).get(sym, {})
        return web.json_response(data)

    async def _dashboard(self, request: web.Request) -> web.Response:
        _mode_map = {
            "testnet": ("TESTNET", "testnet", "testnet.binance.vision"),
            "demo":    ("DEMO",    "demo",    "demo-api.binance.com"),
            "live":    ("⚠ LIVE", "live",    "api.binance.com"),
        }
        mode, mode_cls, api_url = _mode_map.get(cfg.TRADING_MODE, ("TESTNET","testnet","testnet.binance.vision"))
        if _state["halt"]:
            mode, mode_cls = "HALTED", "halt"
        html = (DASHBOARD_HTML
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
            "action":  action,
            "symbol":  body.get("symbol", cfg.SYMBOL),
            "qty_pct": float(body.get("qty_pct", 1.0)),
            "source":  body.get("source", "unknown"),
        }
        log("INSTRUCTION_SERVER", "INSTRUCTION_RECEIVED", **instruction)
        await self._queue.put(instruction)
        return web.json_response({"status": "queued", "action": action})

    async def _status(self, request: web.Request) -> web.Response:
        if not _auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"agent": "dipu", "status": "running"})

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, cfg.INSTRUCTION_SERVER_HOST, cfg.INSTRUCTION_SERVER_PORT)
        await site.start()
        log("INSTRUCTION_SERVER", "STARTED",
            host=cfg.INSTRUCTION_SERVER_HOST, port=cfg.INSTRUCTION_SERVER_PORT)
