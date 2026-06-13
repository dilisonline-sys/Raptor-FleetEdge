"""
Raptor FleetEdge Agent Manager — port 7430
Spawns, stops, and monitors independent Raptor FleetEdge instances per trading mode.
Each instance gets its own port, name, log file, and dashboard.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import aiohttp
from aiohttp import web

MANAGER_PORT      = 7430
STATE_FILE        = Path("/tmp/rfe_manager_state.json")
AGENT_SCRIPT      = Path(__file__).parent / "agent.py"
EMAIL_CONFIG_FILE = Path("/tmp/rfe_email_config.json")
ENV_FILE          = Path(__file__).parent / ".env"

# mode → (default port, API label, badge colour)
MODE_META = {
    "testnet": (7432, "testnet.binance.vision",  "#ffd600", "#000"),
    "demo":    (7433, "demo-api.binance.com",     "#00bcd4", "#000"),
    "live":    (7434, "api.binance.com",           "#ff1744", "#fff"),
}

# In-memory registry  {name: {pid, port, mode, started_at, log_file}}
_agents: dict[str, dict] = {}

# Available coins fetched from Binance at startup — sorted by 24h volume
_available_coins: list[str] = []
BINANCE_PUBLIC = "https://api.binance.com"
LIVE_PORTS  = {0: 7434, 1: 7435, 2: 7436, 3: 7437}  # slot → dashboard port
POOL_FILE   = "/tmp/rfe_equity_pool.json"
COIN_BLACKLIST  = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT"}


_ENV_ALLOWED = frozenset({
    "TRADING_MODE",
    "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET",
    "BINANCE_DEMO_API_KEY",    "BINANCE_DEMO_API_SECRET",
    "BINANCE_LIVE_API_KEY",    "BINANCE_LIVE_API_SECRET",
    "DIPU_ALERT_WEBHOOK",      "DIPU_AUTHORIZED_AGENT_TOKENS",
    "TZ",
})
_ENV_SENSITIVE    = frozenset({"SECRET", "KEY", "TOKEN"})
_MODE_KEY_PREFIX  = {"testnet": "BINANCE_TESTNET", "demo": "BINANCE_DEMO", "live": "BINANCE_LIVE"}


def _api_keys_set(mode: str) -> bool:
    prefix = _MODE_KEY_PREFIX.get(mode)
    if not prefix:
        return False
    env = _env_read()
    return bool(
        env.get(f"{prefix}_API_KEY", "").strip() and
        env.get(f"{prefix}_API_SECRET", "").strip()
    )


def _env_is_sensitive(key: str) -> bool:
    ku = key.upper()
    return any(s in ku for s in _ENV_SENSITIVE)


def _env_read() -> dict[str, str]:
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith('#') and '=' in s:
            k, _, v = s.partition('=')
            result[k.strip()] = v.strip()
    return result


def _env_write(updates: dict[str, str]) -> None:
    lines: list[str] = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    key_idx: dict[str, int] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith('#') and '=' in s:
            key_idx[s.partition('=')[0].strip()] = i
    new_lines = list(lines)
    for k, v in updates.items():
        if k in key_idx:
            new_lines[key_idx[k]] = f"{k}={v}"
        else:
            new_lines.append(f"{k}={v}")
    ENV_FILE.write_text('\n'.join(new_lines).rstrip('\n') + '\n')


async def _fetch_available_coins():
    """Fetch all active USDT spot pairs from Binance, sorted by 24h quote volume."""
    global _available_coins
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(BINANCE_PUBLIC + "/api/v3/ticker/24hr",
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                data = await r.json()
        coins = []
        for t in data:
            sym = t.get("symbol", "")
            if (sym.endswith("USDT")
                    and sym not in COIN_BLACKLIST
                    and t.get("status", "TRADING") != "BREAK"):
                try:
                    vol = float(t.get("quoteVolume", 0))
                    if vol >= 1_000_000:          # min $1 M daily volume
                        coins.append((sym, vol))
                except ValueError:
                    pass
        coins.sort(key=lambda x: x[1], reverse=True)
        _available_coins = [c[0] for c in coins]
        print(f"[manager] loaded {len(_available_coins)} tradeable USDT pairs from Binance")
    except Exception as e:
        print(f"[manager] coin fetch failed: {e} — using fallback list")
        _available_coins = [
            "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
            "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
        ]


def _save_state():
    try:
        STATE_FILE.write_text(json.dumps({
            k: {**v, "pid": v.get("pid", 0)} for k, v in _agents.items()
        }, indent=2))
    except Exception:
        pass


def _startup_cleanup():
    """
    Clear stale runtime files on every manager startup so agents always pull
    fresh data. Preserves user config and per-slot position files (agents
    reconcile open positions against Binance on their own startup).

    Cleared:
      - equity pool  — stale slot registrations from the previous session
      - portfolio day baseline — recalculated fresh on first agent spawn
      - all *.log files in /tmp — truncated to zero for a clean session

    Preserved:
      - rfe_positions_<slot>.json — open position records for recovery
      - rfe_email_config.json     — user email settings
    """
    _tmp = Path("/tmp")

    # Reset equity pool
    pool = Path(POOL_FILE)
    pool.write_text(json.dumps({"slots": {}, "ts": 0, "usdt_free": 0, "earn_value": 0}))

    # Remove portfolio day baseline — portfolio_tracker will recreate it
    (_tmp / "rfe_portfolio_day.json").unlink(missing_ok=True)

    # Truncate all rfe log files (preserves the file so tail -f keeps working)
    for log_file in sorted(_tmp.glob("rfe_*.log")):
        try:
            log_file.write_text("")
        except Exception:
            pass

    print("[manager] startup cleanup complete — stale cache cleared, positions preserved")


def _load_state():
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        for name, info in data.items():
            pid = info.get("pid", 0)
            if pid and _pid_alive(pid):
                _agents[name] = info
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        # Zombies respond to kill(0) but are effectively dead
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:") and "Z" in line:
                    return False
        return True
    except (ProcessLookupError, PermissionError, FileNotFoundError, OSError):
        return False


def _agent_status(name: str) -> str:
    info = _agents.get(name)
    if not info:
        return "stopped"
    if _pid_alive(info.get("pid", 0)):
        return "running"
    return "stopped"


def _spawn(name: str, mode: str, port: int, symbol: str = "BTCUSDT", slot: int = 0) -> dict | None:
    log_file = f"/tmp/rfe_{name}.log"
    env = {**os.environ,
           "TRADING_MODE":  mode,
           "AGENT_NAME":    name,
           "AGENT_PORT":    str(port),
           "AGENT_SYMBOL":  symbol.upper(),
           "AGENT_SLOT":    str(slot)}
    try:
        proc = subprocess.Popen(
            [sys.executable, str(AGENT_SCRIPT)],
            env=env,
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,   # detach from manager — prevents zombie accumulation
        )
        info = {"pid": proc.pid, "port": port, "mode": mode, "symbol": symbol.upper(),
                "started_at": time.time(), "log_file": log_file, "name": name, "slot": slot}
        _agents[name] = info
        _save_state()
        return info
    except Exception as e:
        return None


def _stop(name: str) -> bool:
    info = _agents.get(name)
    if not info:
        return False
    pid = info.get("pid", 0)
    if pid and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    _agents.pop(name, None)
    _save_state()
    return True


# ── HTML ─────────────────────────────────────────────────────────────────────
MANAGER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Raptor FleetEdge — Agent Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080808;color:#fff;font-family:'Courier New',monospace;padding:28px}
h1{color:#00e5ff;font-size:1.5rem;margin-bottom:4px}
.sub{color:#fff;font-size:.78rem;margin-bottom:28px}
h2{color:#fff;font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;margin:24px 0 10px}
/* spawn form */
.spawn-form{background:#0e0e0e;border:1px solid #1e1e1e;border-radius:8px;padding:20px;margin-bottom:28px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:.68rem;color:#fff;text-transform:uppercase;letter-spacing:.08em}
.field input,.field select{background:#0a0a0a;border:1px solid #222;color:#fff;padding:7px 10px;border-radius:5px;font-family:inherit;font-size:.8rem;min-width:160px}
.btn{padding:8px 18px;border-radius:5px;border:none;cursor:pointer;font-family:inherit;font-size:.78rem;font-weight:bold;transition:.15s}
.btn-spawn{background:#00e5ff;color:#000}.btn-spawn:hover{background:#00b8cc}
.btn-stop{background:#ff1744;color:#fff}.btn-stop:hover{background:#cc0033}
.btn-open{background:#161616;color:#00e5ff;border:1px solid #00e5ff}.btn-open:hover{background:#0d1f22}
/* agent cards */
.agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.agent-card{background:#0e0e0e;border:1px solid #1e1e1e;border-radius:9px;padding:18px;position:relative}
.agent-card.running{border-color:#1a3a2a}
.agent-card.stopped{opacity:.6}
.card-header{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.agent-name{font-size:1.05rem;font-weight:bold;color:#00e5ff}
.badge{display:inline-block;padding:2px 9px;border-radius:4px;font-size:.65rem;font-weight:bold}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.dot.on{background:#00e676}.dot.off{background:#555}
.meta{font-size:.7rem;color:#fff;margin-bottom:14px;line-height:1.8}
.meta span{color:#fff}
.api-label{font-size:.68rem;color:#fff;background:#111;border:1px solid #1e1e1e;border-radius:4px;padding:3px 8px;display:inline-block;margin-bottom:10px;font-family:monospace}
.card-actions{display:flex;gap:8px;flex-wrap:wrap}
.status-bar{margin-top:14px;font-size:.65rem;color:#fff;border-top:1px solid #141414;padding-top:10px}
.no-agents{color:#fff;font-size:.8rem;padding:20px 0}
/* Fleet panel */
.fleet-panel{background:#080808;border:1px solid #1a1a1a;border-radius:9px;padding:18px;margin-bottom:28px}
.fleet-header{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.fleet-title{color:#ff1744;font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;font-weight:bold}
.fleet-eq{color:#00e5ff;font-size:.95rem;font-weight:bold;margin-left:4px}
.fleet-dd{font-size:.72rem;color:#fff}
.fleet-controls{margin-left:auto;display:flex;gap:8px}
.slot-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media(max-width:900px){.slot-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:500px){.slot-grid{grid-template-columns:1fr}}
.slot-card{background:#0e0e0e;border:1px solid #1e1e1e;border-radius:8px;padding:14px;min-height:130px;position:relative}
.slot-card.live{border-color:#1a3a2a}
.slot-card.empty{opacity:.55}
.slot-num{font-size:.62rem;color:#fff;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px}
.slot-sym{font-size:1.1rem;font-weight:bold;color:#fff;margin-bottom:6px}
.slot-sym.active{color:#00e676}
.slot-meta{font-size:.68rem;color:#fff;line-height:1.8;margin-bottom:10px}
.slot-meta .g{color:#00e676}.slot-meta .r{color:#ff1744}
.slot-actions{display:flex;gap:6px;flex-wrap:wrap}
/* ── Theme toggle button ── */
#theme-btn{position:fixed;top:12px;right:16px;z-index:9999;background:#1e1e2e;border:1px solid #444;color:#e0e0ff;border-radius:20px;padding:5px 14px;cursor:pointer;font-family:'Courier New',monospace;font-size:.72rem;font-weight:bold;letter-spacing:.04em;box-shadow:0 2px 8px rgba(0,0,0,.5);transition:.2s}
#theme-btn:hover{background:#2a2a3e;border-color:#00e5ff;color:#00e5ff}
/* ── Day theme overrides ── */
[data-theme="day"]{background:#f4f5f7;color:#1a1a2e}
[data-theme="day"] h1,[data-theme="day"] h2{color:#0a0a1e}
[data-theme="day"] .sub{color:#333}
[data-theme="day"] #theme-btn{background:#e8e8f0;border-color:#aaa;color:#333;box-shadow:0 2px 8px rgba(0,0,0,.15)}
[data-theme="day"] #theme-btn:hover{background:#d8d8ee;border-color:#0077bb;color:#0077bb}
[data-theme="day"] .spawn-form{background:#eef0f3;border-color:#d4d8de}
[data-theme="day"] .field input,[data-theme="day"] .field select{background:#fff;border-color:#ccc;color:#111}
[data-theme="day"] .btn-open{background:#e4e6ea;color:#0077bb;border-color:#0077bb}
[data-theme="day"] .btn-open:hover{background:#d4e4f0}
[data-theme="day"] .agent-card{background:#fff;border-color:#dde0e6}
[data-theme="day"] .agent-card.running{border-color:#aad4bb}
[data-theme="day"] .agent-name{color:#0077bb}
[data-theme="day"] .api-label{background:#eef0f3;border-color:#d4d8de;color:#333}
[data-theme="day"] .status-bar{border-color:#e4e6ea;color:#444}
[data-theme="day"] .fleet-panel{background:#eef0f3;border-color:#d4d8de}
[data-theme="day"] .fleet-eq{color:#0077bb}
[data-theme="day"] .slot-card{background:#fff;border-color:#dde0e6}
[data-theme="day"] .slot-card.live{border-color:#aad4bb}
[data-theme="day"] .slot-sym{color:#1a1a2e}
[data-theme="day"] .slot-sym.active{color:#00875a}
[data-theme="day"] .chk-label{color:#333}
[data-theme="day"] .dot.off{background:#bbb}
/* ── Env settings panel ── */
.env-panel{background:#0e0e0e;border:1px solid #1e1e1e;border-radius:8px;padding:20px;margin-bottom:28px}
.env-group{margin-bottom:20px}
.env-group-title{font-size:.68rem;color:#00e5ff;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #1a1a1a}
.env-row{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:8px}
.env-note{font-size:.72rem;color:#aaa;margin-bottom:18px;padding:9px 13px;background:#0a0a0a;border:1px solid #222;border-left:3px solid #ff9800;border-radius:4px}
.env-sub{font-size:.62rem;color:#555;text-transform:none;letter-spacing:0;margin-left:6px;font-weight:normal}
.env-input{background:#0a0a0a;border:1px solid #222;color:#fff;padding:7px 10px;border-radius:5px;font-family:'Courier New',monospace;font-size:.75rem;min-width:220px}
[data-theme="day"] .env-panel{background:#fff;border-color:#dde0e6}
[data-theme="day"] .env-note{background:#fff8e8;border-color:#e0c070;color:#666}
[data-theme="day"] .env-group-title{color:#0077bb;border-color:#e4e6ea}
[data-theme="day"] .env-sub{color:#999}
[data-theme="day"] .env-input{background:#fff;border-color:#ccc;color:#111}
</style>
</head>
<body>
<button id="theme-btn" onclick="toggleTheme()">☀ Day</button>
<h1>&#9654; Raptor FleetEdge — Agent Manager</h1>
<div class="sub">Spawn and control independent Raptor FleetEdge trading instances per environment</div>

<h2>&#9646; Spawn new agent</h2>
<div class="spawn-form">
  <div class="field">
    <label>Agent name</label>
    <input id="f-name" type="text" placeholder="rfe-demo" value="rfe-demo">
  </div>
  <div class="field">
    <label>Trading mode</label>
    <select id="f-mode">
      <option value="testnet">Testnet  (testnet.binance.vision)</option>
      <option value="demo" selected>Demo  (demo-api.binance.com)</option>
      <option value="live">⚠ Live  (api.binance.com)</option>
    </select>
  </div>
  <div class="field">
    <label>Starting coin <span id="coin-count" style="color:#00e5ff;font-size:.6rem"></span></label>
    <input id="f-coin" type="text" list="coin-list" placeholder="Loading…" style="min-width:130px">
    <datalist id="coin-list"></datalist>
  </div>
  <div class="field">
    <label>Dashboard port</label>
    <input id="f-port" type="number" value="7433" min="1024" max="65535" style="min-width:100px">
  </div>
  <button class="btn btn-spawn" onclick="spawnAgent()">&#9654; Spawn</button>
  <span id="spawn-msg" style="font-size:.72rem;color:#00e676;align-self:center"></span>
</div>

<h2>&#9646; Live Fleet</h2>
<div class="fleet-panel">
  <div class="fleet-header">
    <span class="fleet-title">&#9889; Live Trading Pool</span>
    <span class="fleet-eq" id="fleet-eq">—</span>
    <span class="fleet-dd" id="fleet-dd"></span>
    <div class="fleet-controls">
      <button class="btn btn-spawn" onclick="spawnFleet()">&#9889; Launch Fleet</button>
      <button class="btn btn-stop"  onclick="stopAllLive()">&#9646;&#9646; Stop All Live</button>
    </div>
  </div>
  <div class="slot-grid" id="fleet-slots">
    <div style="color:#fff;font-size:.75rem;grid-column:1/-1">Loading fleet state…</div>
  </div>
</div>

<h2>&#9646; Running agents</h2>
<div class="agents" id="agents-grid">
  <div class="no-agents">Loading…</div>
</div>

<h2>&#9993; Email Notifications</h2>
<div class="email-panel" id="email-panel">
  <div class="email-row">
    <div class="field">
      <label>Recipient email</label>
      <input id="em-recipient" type="email" placeholder="you@gmail.com" style="min-width:220px">
    </div>
    <div class="field">
      <label>Sender (Gmail)</label>
      <input id="em-user" type="email" placeholder="sender@gmail.com" style="min-width:220px">
    </div>
    <div class="field">
      <label>App password</label>
      <input id="em-pass" type="password" placeholder="xxxx xxxx xxxx xxxx" style="min-width:180px">
    </div>
  </div>
  <div class="email-row" style="margin-top:12px">
    <span style="color:#fff;font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;margin-right:16px">Notify on:</span>
    <label class="chk-label"><input type="checkbox" id="chk-fills"     checked> Order fills</label>
    <label class="chk-label"><input type="checkbox" id="chk-coin"      checked> Coin traded (on change)</label>
    <label class="chk-label"><input type="checkbox" id="chk-pnl"       checked> 4h P&amp;L report</label>
  </div>
  <div class="email-row" style="margin-top:14px;gap:10px">
    <label class="chk-label" style="margin-right:16px">
      <input type="checkbox" id="em-enabled"> <span style="color:#00e676">Enable email notifications</span>
    </label>
    <button class="btn btn-spawn" onclick="saveEmailConfig()" style="padding:7px 16px">&#128190; Save</button>
    <button class="btn btn-open"  onclick="testEmail()"       style="padding:7px 16px">&#9993; Test email</button>
    <button class="btn btn-spawn" onclick="sendPnlNow()"      style="padding:7px 16px;background:#1a1a1a;color:#00e5ff;border:1px solid #00e5ff">&#128202; Send P&amp;L now</button>
    <span id="email-msg" style="font-size:.72rem;color:#00e676;align-self:center;margin-left:8px"></span>
  </div>
</div>

<style>
.email-panel{background:#0e0e0e;border:1px solid #1e1e1e;border-radius:8px;padding:20px;margin-bottom:28px}
.email-row{display:flex;flex-wrap:wrap;gap:14px;align-items:center}
.chk-label{display:flex;align-items:center;gap:6px;font-size:.72rem;color:#fff;cursor:pointer;user-select:none}
.chk-label input{accent-color:#00e676;width:14px;height:14px;cursor:pointer}
</style>

<h2>&#9881; Environment Settings</h2>
<div class="env-panel" id="env-panel">
  <div class="env-note">&#9888; Values are saved to .env immediately. Changes take effect after stopping and relaunching the fleet.</div>

  <div class="env-group">
    <div class="env-group-title">Trading</div>
    <div class="env-row">
      <div class="field">
        <label>Trading Mode</label>
        <select id="ev-TRADING_MODE" style="min-width:240px">
          <option value="testnet">testnet — testnet.binance.vision</option>
          <option value="demo">demo — demo-api.binance.com</option>
          <option value="live">&#9888; live — api.binance.com (real funds)</option>
        </select>
      </div>
    </div>
  </div>

  <div class="env-group">
    <div class="env-group-title">Testnet Credentials <span class="env-sub">testnet.binance.vision</span></div>
    <div class="env-row">
      <div class="field"><label>API Key</label><input id="ev-BINANCE_TESTNET_API_KEY" type="text" placeholder="testnet API key" class="env-input" style="min-width:300px"></div>
      <div class="field"><label>API Secret</label><input id="ev-BINANCE_TESTNET_API_SECRET" type="password" placeholder="unchanged" class="env-input" style="min-width:240px"></div>
    </div>
  </div>

  <div class="env-group">
    <div class="env-group-title">Demo Credentials <span class="env-sub">demo-api.binance.com</span></div>
    <div class="env-row">
      <div class="field"><label>API Key</label><input id="ev-BINANCE_DEMO_API_KEY" type="text" placeholder="demo API key" class="env-input" style="min-width:300px"></div>
      <div class="field"><label>API Secret</label><input id="ev-BINANCE_DEMO_API_SECRET" type="password" placeholder="unchanged" class="env-input" style="min-width:240px"></div>
    </div>
  </div>

  <div class="env-group">
    <div class="env-group-title">Live Credentials <span class="env-sub" style="color:#ff5252">api.binance.com — Trade + Read only, never enable withdrawals</span></div>
    <div class="env-row">
      <div class="field"><label>API Key</label><input id="ev-BINANCE_LIVE_API_KEY" type="text" placeholder="live API key" class="env-input" style="min-width:300px"></div>
      <div class="field"><label>API Secret</label><input id="ev-BINANCE_LIVE_API_SECRET" type="password" placeholder="unchanged" class="env-input" style="min-width:240px"></div>
    </div>
  </div>

  <div class="env-group">
    <div class="env-group-title">System</div>
    <div class="env-row">
      <div class="field">
        <label>Timezone <span class="env-sub">all log and dashboard timestamps</span></label>
        <input id="ev-TZ" type="text" list="tz-list" placeholder="Asia/Dubai" class="env-input" style="min-width:220px">
        <datalist id="tz-list">
          <option value="Asia/Dubai"><option value="Asia/Riyadh"><option value="Asia/Kuwait">
          <option value="Asia/Qatar"><option value="Asia/Bahrain"><option value="Asia/Muscat">
          <option value="Asia/Kolkata"><option value="Asia/Karachi"><option value="Asia/Dhaka">
          <option value="Asia/Singapore"><option value="Asia/Shanghai"><option value="Asia/Tokyo">
          <option value="Asia/Seoul"><option value="Asia/Bangkok"><option value="Asia/Jakarta">
          <option value="Asia/Taipei"><option value="Asia/Hong_Kong"><option value="Asia/Kuala_Lumpur">
          <option value="Europe/London"><option value="Europe/Paris"><option value="Europe/Berlin">
          <option value="Europe/Madrid"><option value="Europe/Rome"><option value="Europe/Amsterdam">
          <option value="Europe/Moscow"><option value="Europe/Istanbul"><option value="Europe/Zurich">
          <option value="America/New_York"><option value="America/Chicago"><option value="America/Denver">
          <option value="America/Los_Angeles"><option value="America/Toronto"><option value="America/Vancouver">
          <option value="America/Sao_Paulo"><option value="America/Mexico_City"><option value="America/Buenos_Aires">
          <option value="Australia/Sydney"><option value="Australia/Melbourne"><option value="Australia/Brisbane">
          <option value="Pacific/Auckland"><option value="Africa/Cairo"><option value="Africa/Johannesburg">
          <option value="Africa/Lagos"><option value="Africa/Nairobi"><option value="UTC">
        </datalist>
      </div>
    </div>
  </div>

  <div class="env-group">
    <div class="env-group-title">Integrations</div>
    <div class="env-row">
      <div class="field" style="flex:1;min-width:300px">
        <label>Alert Webhook URL <span class="env-sub">Discord / Slack / Telegram (optional)</span></label>
        <input id="ev-DIPU_ALERT_WEBHOOK" type="text" placeholder="https://hooks.slack.com/…" class="env-input" style="width:100%;min-width:380px">
      </div>
    </div>
    <div class="env-row" style="margin-top:10px">
      <div class="field" style="flex:1;min-width:300px">
        <label>Authorized Agent Tokens <span class="env-sub">comma-separated, for external API POST access (optional)</span></label>
        <input id="ev-DIPU_AUTHORIZED_AGENT_TOKENS" type="text" placeholder="token1,token2" class="env-input" style="width:100%;min-width:380px">
      </div>
    </div>
  </div>

  <div style="display:flex;align-items:center;gap:12px;margin-top:6px">
    <button class="btn btn-spawn" onclick="saveEnvConfig()">&#128190; Save .env</button>
    <span id="env-msg" style="font-size:.72rem;color:#00e676"></span>
  </div>
</div>

<script>
const MODE_COLOR = {testnet:'#ffd600', demo:'#00bcd4', live:'#ff1744'};
const MODE_TEXT  = {testnet:'TESTNET', demo:'DEMO', live:'⚠ LIVE'};
const MODE_API   = {
  testnet: 'testnet.binance.vision',
  demo:    'demo-api.binance.com',
  live:    'api.binance.com',
};

async function loadAgents() {
  const r = await fetch('/api/agents');
  const data = await r.json();
  const grid = document.getElementById('agents-grid');
  if (!data.length) {
    grid.innerHTML = '<div class="no-agents">No agents running. Spawn one above.</div>';
    return;
  }
  grid.innerHTML = data.map(a => {
    const alive = a.status === 'running';
    const col   = MODE_COLOR[a.mode] || '#888';
    const upSec = alive ? Math.floor(Date.now()/1000 - a.started_at) : 0;
    const h = Math.floor(upSec/3600), m = Math.floor((upSec%3600)/60);
    const upStr = alive ? `${h}h ${m}m` : '—';
    return `<div class="agent-card ${alive ? 'running' : 'stopped'}">
      <div class="card-header">
        <span class="dot ${alive ? 'on' : 'off'}"></span>
        <span class="agent-name">${a.name}</span>
        <span class="badge" style="background:${col};color:${a.mode==='live'?'#fff':'#000'}">${MODE_TEXT[a.mode]||a.mode}</span>
      </div>
      <div class="api-label">&#128279; ${MODE_API[a.mode] || a.mode}</div>
      <div class="meta">
        Port: <span>:${a.port}</span> &nbsp;|&nbsp;
        PID: <span>${a.pid || '—'}</span> &nbsp;|&nbsp;
        Coin: <span>${a.symbol || 'AUTO'}</span> &nbsp;|&nbsp;
        Uptime: <span>${upStr}</span>
      </div>
      <div class="card-actions">
        ${alive
          ? `<button class="btn btn-stop" onclick="stopAgent('${a.name}')">&#9646;&#9646; Stop</button>`
          : `<button class="btn btn-spawn" onclick="startAgent('${a.name}','${a.mode}',${a.port},'${a.symbol||'BTCUSDT'}')">&#9654; Start</button>`
        }
        <button class="btn btn-open" onclick="window.open('/agent/${a.name}/','_blank')">
          &#9654; Open Dashboard
        </button>
      </div>
      <div class="status-bar">Log: ${a.log_file || '—'}</div>
    </div>`;
  }).join('');
}

async function spawnAgent() {
  const name   = document.getElementById('f-name').value.trim();
  const mode   = document.getElementById('f-mode').value;
  const port   = parseInt(document.getElementById('f-port').value);
  const symbol = (document.getElementById('f-coin').value.trim().toUpperCase() || 'BTCUSDT');
  if (!name) { alert('Enter an agent name'); return; }
  const msg = document.getElementById('spawn-msg');
  msg.textContent = 'Spawning…';
  const r = await fetch('/api/spawn', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, mode, port, symbol}),
  });
  const d = await r.json();
  msg.textContent = d.error ? '✗ ' + d.error : `✓ ${name} started on :${port}`;
  setTimeout(() => msg.textContent = '', 4000);
  loadAgents();
}

const _aiStates = {};
async function toggleAI(name, port) {
  const on = !_aiStates[name];
  _aiStates[name] = on;
  const action = on ? 'ANALYST_ON' : 'ANALYST_OFF';
  await fetch(`/agent/${name}/instruction`, {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-Agent-Token':'internal'},
    body: JSON.stringify({action, source:'manager'}),
  });
  const btn = document.getElementById(`ai-btn-${name}`);
  if (btn) {
    btn.style.background    = on ? '#0a2010' : '#0a0f0a';
    btn.style.borderColor   = on ? '#00e676' : '#1a3a1a';
    btn.style.color         = on ? '#00e676' : '#4caf50';
    btn.textContent         = on ? '■ Analyst ON' : '■ Analyst';
  }
}

async function stopAgent(name) {
  if (!confirm(`Stop agent "${name}"?`)) return;
  await fetch('/api/stop', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name}),
  });
  loadAgents(); loadPool();
}

async function startAgent(name, mode, port, symbol) {
  const r = await fetch('/api/spawn', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, mode, port, symbol: symbol || 'BTCUSDT'}),
  });
  loadAgents();
}

async function loadCoins() {
  try {
    const r = await fetch('/api/coins');
    const coins = await r.json();
    const dl = document.getElementById('coin-list');
    dl.innerHTML = coins.map(c => `<option value="${c}">`).join('');
    const inp = document.getElementById('f-coin');
    if (!inp.value) inp.value = coins[0] || 'BTCUSDT';
    document.getElementById('coin-count').textContent = `(${coins.length} pairs)`;
  } catch(e) {
    document.getElementById('f-coin').placeholder = 'BTCUSDT';
  }
}

async function loadPool() {
  try {
    const r = await fetch('/api/pool');
    const d = await r.json();
    renderFleet(d);
  } catch(e) {
    document.getElementById('fleet-slots').innerHTML = '<div style="color:#fff;font-size:.75rem;grid-column:1/-1">Pool offline</div>';
  }
}

function renderFleet(pool) {
  const slots = pool.slots || {};
  let totalOpen = 0, totalPnl = 0, activeCount = 0;
  Object.values(slots).forEach(s => {
    if (s) { totalOpen += s.open_usdt||0; totalPnl += s.daily_pnl||0; activeCount++; }
  });

  const eqEl = document.getElementById('fleet-eq');
  const ddEl = document.getElementById('fleet-dd');
  if (activeCount > 0) {
    eqEl.textContent = 'Open: $' + totalOpen.toFixed(2) + '  ·  ' + activeCount + ' agent' + (activeCount>1?'s':'') + ' running';
    ddEl.textContent = 'Daily P&L: ' + (totalPnl >= 0 ? '+' : '') + '$' + totalPnl.toFixed(2);
    ddEl.style.color  = totalPnl >= 0 ? '#00e676' : '#ff1744';
  } else {
    eqEl.textContent = 'No live agents';
    ddEl.textContent = '';
  }

  const grid = document.getElementById('fleet-slots');
  let html = '';
  for (let i = 0; i < 4; i++) {
    const s = slots[String(i)];
    const port = s ? (s.port || (7434 + (i===0?0:i))) : (7434 + (i===0?0:i));
    const pnl  = s ? (s.daily_pnl||0) : 0;
    const pnlStr = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    const pnlCls = pnl >= 0 ? 'g' : 'r';
    const name   = 'fleetedge' + (i + 1);
    if (s) {
      html += `<div class="slot-card live">
        <div class="slot-num">Slot ${i} &nbsp;&#9679;&nbsp; :${port}</div>
        <div class="slot-sym active">${s.symbol || '—'}</div>
        <div class="slot-meta">
          Open: $${(s.open_usdt||0).toFixed(2)}<br>
          P&L: <span class="${pnlCls}">${pnlStr}</span>
        </div>
        <div class="slot-actions">
          <button class="btn btn-open" onclick="window.open('/agent/${name}/','_blank')">&#9654; Dashboard</button>
          <button id="ai-btn-${name}" class="btn" onclick="toggleAI('${name}','${port}')"
            style="font-size:.65rem;padding:4px 8px;background:#0a0f0a;border:1px solid #1a3a1a;color:#4caf50;cursor:pointer">&#9632; Analyst</button>
          <button class="btn btn-stop" onclick="stopAgent('${name}')" style="padding:6px 10px;font-size:.68rem">&#9646;&#9646;</button>
        </div>
      </div>`;
    } else {
      html += `<div class="slot-card empty">
        <div class="slot-num">Slot ${i} &nbsp;&#9675;&nbsp; :${port}</div>
        <div class="slot-sym" style="color:#333">empty</div>
        <div class="slot-meta" style="color:#333">—<br>—</div>
        <div class="slot-actions">
          <button class="btn btn-spawn" onclick="spawnSlot(${i})" style="font-size:.68rem">&#9654; Spawn</button>
        </div>
      </div>`;
    }
  }
  grid.innerHTML = html;
}

async function spawnFleet() {
  const msg = document.getElementById('spawn-msg');
  msg.textContent = '⚡ Scanning top coins and launching fleet…';
  try {
    const r = await fetch('/api/spawn-fleet', {method:'POST'});
    const d = await r.json();
    if (!d.ok && d.error) {
      msg.textContent = '✗ ' + d.error;
    } else {
      const ok   = d.agents.filter(a => a.status === 'spawned').length;
      const skip = d.agents.filter(a => a.status === 'already_running').length;
      msg.textContent = `✓ Fleet launched — ${ok} spawned, ${skip} already running | top4: ${(d.top4||[]).join(', ')}`;
    }
  } catch(e) {
    msg.textContent = '✗ Fleet launch failed: ' + e.message;
  }
  setTimeout(() => msg.textContent = '', 8000);
  loadAgents(); loadPool();
}

async function spawnSlot(slot) {
  const name = 'fleetedge' + (slot + 1);
  const port = [7434,7435,7436,7437][slot];
  const r = await fetch('/api/spawn-fleet', {method:'POST'});
  const d = await r.json();
  const agent = d.agents.find(a => a.slot === slot);
  if (agent && agent.status === 'spawned') {
    document.getElementById('spawn-msg').textContent = `✓ Slot ${slot} spawned: ${agent.symbol}`;
    setTimeout(() => document.getElementById('spawn-msg').textContent = '', 4000);
  }
  loadAgents(); loadPool();
}

async function stopAllLive() {
  if (!confirm('Stop ALL live trading agents?')) return;
  const r = await fetch('/api/agents');
  const agents = await r.json();
  const live = agents.filter(a => a.mode === 'live' && a.status === 'running');
  for (const a of live) {
    await fetch('/api/stop', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: a.name}),
    });
  }
  loadAgents(); loadPool();
}

loadPool();
setInterval(loadPool, 5000);
loadCoins();
loadAgents();
setInterval(loadAgents, 5000);

async function loadEmailConfig() {
  try {
    const r = await fetch('/api/email-config');
    const d = await r.json();
    document.getElementById('em-recipient').value = d.recipient || '';
    document.getElementById('em-user').value      = d.smtp_user || '';
    document.getElementById('em-pass').value      = d.smtp_password ? '••••••••' : '';
    document.getElementById('em-enabled').checked = !!d.enabled;
    const n = d.notifications || {};
    document.getElementById('chk-fills').checked    = n.order_fills   !== false;
    document.getElementById('chk-coin').checked     = n.coin_traded   !== false;
    document.getElementById('chk-pnl').checked      = n.pnl_report    !== false;
  } catch(e) {}
}

async function saveEmailConfig() {
  const msg = document.getElementById('email-msg');
  const passVal = document.getElementById('em-pass').value;
  const body = {
    recipient:     document.getElementById('em-recipient').value.trim(),
    smtp_user:     document.getElementById('em-user').value.trim(),
    smtp_password: passVal === '••••••••' ? null : passVal,
    enabled:       document.getElementById('em-enabled').checked,
    notifications: {
      order_fills:   document.getElementById('chk-fills').checked,
      coin_traded:   document.getElementById('chk-coin').checked,
      pnl_report:    document.getElementById('chk-pnl').checked,
    },
  };
  msg.textContent = 'Saving…';
  const r = await fetch('/api/email-config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  const d = await r.json();
  msg.textContent = d.ok ? '✓ Saved' : '✗ ' + (d.error || 'error');
  setTimeout(() => msg.textContent = '', 4000);
}

async function testEmail() {
  const msg = document.getElementById('email-msg');
  const recipient = document.getElementById('em-recipient').value.trim();
  if (!recipient) { msg.textContent = '✗ Enter recipient email first'; setTimeout(()=>msg.textContent='',3000); return; }
  msg.textContent = 'Sending test…';
  const r = await fetch('/api/email-test', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({recipient}),
  });
  const d = await r.json();
  msg.textContent = d.ok ? '✓ Test email sent' : '✗ ' + (d.error || 'failed — check SMTP credentials');
  setTimeout(() => msg.textContent = '', 6000);
}

async function sendPnlNow() {
  const msg = document.getElementById('email-msg');
  msg.textContent = 'Sending P&L report…';
  const r = await fetch('/api/email-pnl', {method:'POST'});
  const d = await r.json();
  msg.textContent = d.ok ? '✓ P&L report sent' : '✗ ' + (d.error || 'failed');
  setTimeout(() => msg.textContent = '', 5000);
}

loadEmailConfig();

// ── Env settings ─────────────────────────────────────────
const _ENV_MASKED = new Set([
  'BINANCE_TESTNET_API_KEY','BINANCE_TESTNET_API_SECRET',
  'BINANCE_DEMO_API_KEY','BINANCE_DEMO_API_SECRET',
  'BINANCE_LIVE_API_KEY','BINANCE_LIVE_API_SECRET',
  'DIPU_AUTHORIZED_AGENT_TOKENS',
]);
const _ENV_KEYS = [
  'TRADING_MODE','TZ',
  'BINANCE_TESTNET_API_KEY','BINANCE_TESTNET_API_SECRET',
  'BINANCE_DEMO_API_KEY','BINANCE_DEMO_API_SECRET',
  'BINANCE_LIVE_API_KEY','BINANCE_LIVE_API_SECRET',
  'DIPU_ALERT_WEBHOOK','DIPU_AUTHORIZED_AGENT_TOKENS',
];

async function loadEnvConfig() {
  try {
    const r = await fetch('/api/env');
    const d = await r.json();
    for (const [k, v] of Object.entries(d)) {
      const el = document.getElementById('ev-' + k);
      if (!el) continue;
      if (el.tagName === 'SELECT') {
        el.value = v || 'demo';
      } else if (v === '__set__') {
        el.placeholder = 'unchanged (already set)';
        el.value = '';
      } else {
        el.value = v || '';
      }
    }
  } catch(e) {}
}

async function saveEnvConfig() {
  const msg = document.getElementById('env-msg');
  const body = {};
  for (const k of _ENV_KEYS) {
    const el = document.getElementById('ev-' + k);
    if (!el) continue;
    const v = el.tagName === 'SELECT' ? el.value : el.value.trim();
    if (_ENV_MASKED.has(k) && !v) continue;
    body[k] = v;
  }
  if (!Object.keys(body).length) {
    msg.textContent = '✗ Nothing to save'; setTimeout(() => msg.textContent = '', 4000); return;
  }
  msg.textContent = 'Saving…';
  const r = await fetch('/api/env', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const d = await r.json();
  msg.textContent = d.ok ? '✓ Saved — restart fleet to apply changes' : '✗ ' + (d.error || 'error');
  setTimeout(() => msg.textContent = '', 7000);
}
loadEnvConfig();

// ── Theme toggle ──────────────────────────────────────────
function _applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = theme === 'day' ? '☾ Night' : '☀ Day';
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
</script>
</body>
</html>
"""


class AgentManager:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._app = web.Application()
        self._app.router.add_get("/",                      self._dashboard)
        self._app.router.add_get("/api/agents",            self._list)
        self._app.router.add_get("/api/coins",             self._coins)
        self._app.router.add_post("/api/spawn",            self._spawn)
        self._app.router.add_post("/api/stop",             self._stop)
        self._app.router.add_get("/api/pool",              self._pool)
        self._app.router.add_post("/api/spawn-fleet",      self._spawn_fleet)
        self._app.router.add_get("/api/email-config",      self._email_config_get)
        self._app.router.add_post("/api/email-config",     self._email_config_post)
        self._app.router.add_post("/api/email-test",       self._email_test)
        self._app.router.add_post("/api/email-pnl",        self._email_pnl_now)
        self._app.router.add_get("/api/env",               self._env_get)
        self._app.router.add_post("/api/env",              self._env_post)
        self._app.router.add_route("*", "/agent/{name}/{path:.*}", self._proxy)

    async def _dashboard(self, _):
        try:
            import re as _re
            src = open(__file__).read()
            live_html = _re.search(r'MANAGER_HTML = r"""(.*?)^"""', src, _re.S | _re.M).group(1)
        except Exception:
            live_html = MANAGER_HTML
        return web.Response(text=live_html, content_type="text/html")

    async def _list(self, _):
        # Re-sync from state file on every list call — picks up PIDs updated externally
        _load_state()
        result = []
        for name, info in list(_agents.items()):
            status = _agent_status(name)
            result.append({**info, "status": status, "name": name})
        return web.json_response(result)

    async def _coins(self, _):
        return web.json_response(_available_coins)

    async def _spawn(self, request: web.Request):
        body   = await request.json()
        name   = body.get("name", "").strip()
        mode   = body.get("mode", "testnet")
        port   = int(body.get("port", MODE_META.get(mode, (7432,))[0]))
        symbol = body.get("symbol", "BTCUSDT").upper().strip() or "BTCUSDT"
        slot   = int(body.get("slot", 0))

        if not name:
            return web.json_response({"error": "name required"}, status=400)
        if mode not in MODE_META:
            return web.json_response({"error": f"unknown mode: {mode}"}, status=400)
        if not _api_keys_set(mode):
            return web.json_response({"error": f"No API credentials set for {mode} mode — configure them in Settings first."}, status=400)
        if name in _agents and _pid_alive(_agents[name].get("pid", 0)):
            return web.json_response({"error": f"{name} already running"}, status=409)

        info = _spawn(name, mode, port, symbol, slot=slot)
        if not info:
            return web.json_response({"error": "failed to spawn"}, status=500)
        return web.json_response({"ok": True, **info})

    async def _stop(self, request: web.Request):
        body = await request.json()
        name = body.get("name", "")
        ok   = _stop(name)
        return web.json_response({"ok": ok})

    async def _proxy(self, request: web.Request) -> web.Response | web.StreamResponse:
        name = request.match_info["name"]
        path = request.match_info.get("path", "")
        info = _agents.get(name)
        if not info or not _pid_alive(info.get("pid", 0)):
            _load_state()   # re-sync from file before giving up
            info = _agents.get(name)
        if not info or not _pid_alive(info.get("pid", 0)):
            return web.Response(text=f"Agent '{name}' is not running", status=503)
        port   = info["port"]
        target = f"http://127.0.0.1:{port}/{path}"
        qs     = request.query_string
        if qs:
            target += f"?{qs}"
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=False)
            )
        try:
            method   = request.method
            req_body = await request.read() if method in ("POST", "PUT", "PATCH") else None
            headers  = {k: v for k, v in request.headers.items()
                        if k.lower() not in ("host", "content-length", "transfer-encoding")}

            async with self._session.request(
                method, target, data=req_body, headers=headers,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=None)
            ) as r:
                ct = r.headers.get("Content-Type", "text/html")

                # ── SSE: stream chunk-by-chunk ────────────────────
                if "text/event-stream" in ct:
                    resp = web.StreamResponse(status=r.status, headers={
                        "Content-Type":      "text/event-stream",
                        "Cache-Control":     "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection":        "keep-alive",
                    })
                    await resp.prepare(request)
                    try:
                        async for chunk in r.content.iter_any():
                            await resp.write(chunk)
                    except (ConnectionResetError, asyncio.CancelledError):
                        pass
                    return resp

                # ── Regular response ──────────────────────────────
                body = await r.read()
                if "text/html" in ct:
                    pfx = f"/agent/{name}/".encode()
                    body = (body
                        .replace(b'href="/',         b'href="'  + pfx)
                        .replace(b"fetch('/",         b"fetch('" + pfx)
                        .replace(b'src="/',           b'src="'   + pfx)
                        .replace(b"new EventSource('/", b"new EventSource('" + pfx)
                        .replace(b"method:'POST',\n    headers:{'Content-Type':'application/json','X-Agent-Token':'internal'}",
                                 b"method:'POST',\n    headers:{'Content-Type':'application/json','X-Agent-Token':'internal'}")
                    )
                return web.Response(body=body, status=r.status,
                                    content_type=ct.split(";")[0])
        except Exception as e:
            return web.Response(text=f"Proxy error: {e}", status=502)

    async def _pool(self, _) -> web.Response:
        import json as _j
        try:
            with open(POOL_FILE) as f:
                data = _j.load(f)
            # Enrich with agent port/name from _agents
            for name, info in _agents.items():
                if info.get("mode") == "live":
                    slot = str(info.get("slot", 0))
                    if data["slots"].get(slot):
                        data["slots"][slot]["agent_name"] = name
                        data["slots"][slot]["port"]       = info["port"]
            return web.json_response(data)
        except Exception:
            return web.json_response({"slots": {str(i): None for i in range(4)}, "ts": 0})

    async def _do_spawn_fleet(self) -> dict:
        """Fetch top 4 coins by 1h momentum and spawn live agents for empty slots.

        Returns the result dict used by both the HTTP handler and the auto-spawn
        scheduler so the logic lives in exactly one place.
        """
        if not _api_keys_set("live"):
            return {"ok": False, "agents": [], "top4": [],
                    "error": "No live API credentials configured — go to Settings and enter your Binance Live API Key and Secret, then restart."}
        import json as _j, urllib.parse as _up, aiohttp as _aio
        try:
            async with _aio.ClientSession() as s:
                async with s.get(BINANCE_PUBLIC + "/api/v3/ticker/24hr",
                                 timeout=_aio.ClientTimeout(total=15)) as r:
                    tickers = await r.json()
            BLACKLIST = {"BUSDUSDT","USDCUSDT","TUSDUSDT","FDUSDUSDT","USD1USDT","USDTUSDT"}
            pre = []
            for t in tickers:
                sym = t.get("symbol","")
                if not sym.endswith("USDT") or sym in BLACKLIST or not sym.isascii():
                    continue
                try:
                    vol = float(t["quoteVolume"])
                    if vol < 15_000_000: continue
                    price = float(t["lastPrice"])
                    if price < 0.05: continue
                    bid   = float(t.get("bidPrice") or price)
                    ask   = float(t.get("askPrice") or price)
                    mid   = (bid+ask)/2
                    spread = (ask-bid)/mid*100 if mid else 99
                    if spread > 0.30: continue
                    chg_24h = abs(float(t.get("priceChangePercent",0)))
                    pre.append({"symbol": sym, "vol_24h": vol, "chg_24h": chg_24h})
                except: pass
            by_vol = sorted(pre, key=lambda x: x["vol_24h"], reverse=True)
            by_chg = sorted(pre, key=lambda x: x["chg_24h"], reverse=True)
            seen = set(); batch = []
            for c in by_vol[:60] + by_chg[:60]:
                if c["symbol"] not in seen:
                    seen.add(c["symbol"]); batch.append(c)
            syms = _j.dumps([c["symbol"] for c in batch], separators=(',',':'))
            url  = BINANCE_PUBLIC + "/api/v3/ticker?windowSize=1h&symbols=" + _up.quote(syms)
            async with _aio.ClientSession() as s:
                async with s.get(url, timeout=_aio.ClientTimeout(total=15)) as r:
                    t1h = await r.json()
            results = []
            for t in (t1h if isinstance(t1h, list) else []):
                try:
                    chg = abs(float(t["priceChangePercent"]))
                    vol = float(t["quoteVolume"])
                    h = float(t["highPrice"]); lo = float(t["lowPrice"]); p = float(t["lastPrice"]) or 1
                    if chg < 0.3 or vol < 500_000: continue
                    rng = (h - lo) / p
                    score = chg * (vol / 1e7) * rng
                    results.append({"symbol": t["symbol"], "score": score})
                except: pass
            results.sort(key=lambda x: x["score"], reverse=True)
            top4 = [r["symbol"] for r in results[:4]]
        except Exception as e:
            print(f"[manager] fleet coin fetch failed: {e} — using fallback")
            top4 = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT"]

        # Slot 0 is permanently BTC; fill the other 3 from top momentum picks (excluding BTC)
        top3_others = [s for s in top4 if s != "BTCUSDT"][:3]
        while len(top3_others) < 3:
            top3_others.append("ETHUSDT")
        top4 = ["BTCUSDT"] + top3_others

        spawned = []
        for slot, sym in enumerate(top4):
            port = LIVE_PORTS[slot]
            name = f"fleetedge{slot + 1}"
            if name in _agents and _pid_alive(_agents[name].get("pid", 0)):
                spawned.append({"slot": slot, "name": name, "status": "already_running", "symbol": _agents[name].get("symbol","")})
                continue
            info = _spawn(name, "live", port, sym, slot=slot)
            if info:
                spawned.append({"slot": slot, "name": name, "status": "spawned", "symbol": sym, "port": port})
            else:
                spawned.append({"slot": slot, "name": name, "status": "failed"})

        return {"ok": True, "agents": spawned, "top4": top4}

    async def _spawn_fleet(self, request: web.Request) -> web.Response:
        """HTTP handler — delegates to _do_spawn_fleet."""
        result = await self._do_spawn_fleet()
        return web.json_response(result)

    async def _auto_spawn_scheduler(self):
        """Auto-spawn the live fleet on container startup.

        Waits 5 s for the manager to settle, then spawns any empty live slots.
        Skips if agents are already running (e.g. hot-restart with live PIDs).
        """
        await asyncio.sleep(5)
        if not _api_keys_set("live"):
            print("[manager] auto-spawn skipped — no live API credentials in .env (configure via Settings)")
            return
        # Check whether any live slot is already occupied
        live_running = any(
            info.get("mode") == "live" and _pid_alive(info.get("pid", 0))
            for info in _agents.values()
        )
        if live_running:
            print("[manager] auto-spawn skipped — live agents already running")
            return
        print("[manager] auto-spawning live fleet…")
        try:
            result = await self._do_spawn_fleet()
            ok     = [a for a in result["agents"] if a["status"] == "spawned"]
            skip   = [a for a in result["agents"] if a["status"] == "already_running"]
            fail   = [a for a in result["agents"] if a["status"] == "failed"]
            print(f"[manager] auto-spawn complete — "
                  f"{len(ok)} spawned, {len(skip)} already running, {len(fail)} failed | "
                  f"coins: {result['top4']}")
        except Exception as e:
            print(f"[manager] auto-spawn error: {e}")

    def _load_email_cfg(self) -> dict:
        try:
            if EMAIL_CONFIG_FILE.exists():
                return json.loads(EMAIL_CONFIG_FILE.read_text())
        except Exception:
            pass
        return {"enabled": False, "recipient": "", "smtp_host": "smtp.gmail.com",
                "smtp_port": 465, "smtp_user": "", "smtp_password": "",
                "notifications": {"order_fills": True,
                                   "coin_traded": True, "pnl_report": True}}

    def _save_email_cfg(self, cfg: dict) -> None:
        EMAIL_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    async def _email_config_get(self, _) -> web.Response:
        cfg = self._load_email_cfg()
        # Never expose password in full — just signal presence
        safe = {**cfg, "smtp_password": "set" if cfg.get("smtp_password") else ""}
        return web.json_response(safe)

    async def _email_config_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            cfg  = self._load_email_cfg()
            if body.get("recipient") is not None:
                cfg["recipient"] = body["recipient"]
            if body.get("smtp_user") is not None:
                cfg["smtp_user"] = body["smtp_user"]
            if body.get("smtp_password"):  # null means "keep existing"
                cfg["smtp_password"] = body["smtp_password"]
            if body.get("enabled") is not None:
                cfg["enabled"] = bool(body["enabled"])
            if body.get("notifications"):
                cfg["notifications"] = {**cfg.get("notifications", {}), **body["notifications"]}
            self._save_email_cfg(cfg)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _email_test(self, request: web.Request) -> web.Response:
        import concurrent.futures
        try:
            body      = await request.json()
            recipient = body.get("recipient", "")
            if not recipient:
                return web.json_response({"error": "recipient required"}, status=400)
            import email_notifier as _em
            loop        = asyncio.get_event_loop()
            ok, reason  = await loop.run_in_executor(None, _em.send_test_email, recipient)
            return web.json_response({"ok": ok, "error": None if ok else reason})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _email_pnl_now(self, _) -> web.Response:
        try:
            pool_data = await self._get_pool_data()
            import email_notifier as _em
            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _em.send_pnl_report, pool_data)
            return web.json_response({"ok": result})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _env_get(self, _) -> web.Response:
        raw  = _env_read()
        safe = {k: ("__set__" if (_env_is_sensitive(k) and v) else v) for k, v in raw.items()}
        return web.json_response(safe)

    async def _env_post(self, request: web.Request) -> web.Response:
        try:
            body    = await request.json()
            updates = {k: str(v) for k, v in body.items()
                       if k in _ENV_ALLOWED and v is not None and str(v).strip()}
            if not updates:
                return web.json_response({"error": "no valid fields provided"}, status=400)
            _env_write(updates)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _get_pool_data(self) -> dict:
        try:
            with open(POOL_FILE) as f:
                data = json.load(f)
            return data.get("slots", {})
        except Exception:
            return {}

    async def _pnl_report_scheduler(self):
        """Send a P&L report email every 4 hours."""
        INTERVAL = 4 * 3600
        await asyncio.sleep(INTERVAL)  # first report after 4h, not on startup
        while True:
            try:
                pool_data = await self._get_pool_data()
                import email_notifier as _em
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _em.send_pnl_report, pool_data)
                print("[manager] 4h P&L report sent")
            except Exception as e:
                print(f"[manager] P&L report error: {e}")
            await asyncio.sleep(INTERVAL)

    async def _log_cleaner_scheduler(self):
        """Trim all rfe log files to the last 24 hours, every 6 hours."""
        INTERVAL = 6 * 3600
        await asyncio.sleep(60)  # brief startup delay — let agents write their first lines
        while True:
            try:
                import log_cleaner as _lc
                loop    = asyncio.get_event_loop()
                summary = await loop.run_in_executor(None, _lc.clean_logs)
                total_dropped = sum(v.get("dropped", 0) for v in summary.values() if isinstance(v, dict))
                print(f"[manager] log_cleaner: trimmed {total_dropped} old entries across {len(summary)} files")
            except Exception as e:
                print(f"[manager] log_cleaner error: {e}")
            await asyncio.sleep(INTERVAL)

    async def _monitor_scheduler(self):
        """Check agent health every 30 minutes, write report, send email."""
        INTERVAL = 30 * 60
        await asyncio.sleep(120)  # wait 2 min after startup before first check
        while True:
            try:
                import agent_monitor as _mon
                loop   = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, _mon.check_and_report)
                status = "OK" if result["healthy"] else f"{len(result['issues'])} issue(s)"
                print(f"[manager] monitor: {status}")
            except Exception as e:
                print(f"[manager] monitor error: {e}")
            await asyncio.sleep(INTERVAL)

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", MANAGER_PORT)
        await site.start()
        asyncio.create_task(self._auto_spawn_scheduler())
        asyncio.create_task(self._pnl_report_scheduler())
        asyncio.create_task(self._log_cleaner_scheduler())
        asyncio.create_task(self._monitor_scheduler())
        print(f"[manager] running on :{MANAGER_PORT}")


def _install_sigchld_reaper():
    """Reap zombie children automatically so they don't accumulate."""
    import signal as _sig

    def _reap(*_):
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass

    _sig.signal(_sig.SIGCHLD, _reap)


async def main():
    _install_sigchld_reaper()
    _startup_cleanup()
    _load_state()
    await _fetch_available_coins()
    manager = AgentManager()
    await manager.start()
    print(f"[manager] Agent Manager started on port {MANAGER_PORT}")
    print(f"[manager] Open http://localhost:{MANAGER_PORT}")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
