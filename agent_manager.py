"""
dipu Agent Manager — port 7430
Spawns, stops, and monitors independent dipu instances per trading mode.
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

MANAGER_PORT = 7430
STATE_FILE   = Path("/tmp/dipu_manager_state.json")
AGENT_SCRIPT = Path(__file__).parent / "agent.py"

# mode → (default port, API label, badge colour)
MODE_META = {
    "testnet": (7432, "testnet.binance.vision",  "#ffd600", "#000"),
    "demo":    (7433, "demo-api.binance.com",     "#00bcd4", "#000"),
    "live":    (7434, "api.binance.com",           "#ff1744", "#fff"),
}

# In-memory registry  {name: {pid, port, mode, started_at, log_file}}
_agents: dict[str, dict] = {}


def _save_state():
    try:
        STATE_FILE.write_text(json.dumps({
            k: {**v, "pid": v.get("pid", 0)} for k, v in _agents.items()
        }, indent=2))
    except Exception:
        pass


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
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _agent_status(name: str) -> str:
    info = _agents.get(name)
    if not info:
        return "stopped"
    if _pid_alive(info.get("pid", 0)):
        return "running"
    return "stopped"


def _spawn(name: str, mode: str, port: int, symbol: str = "BTCUSDT") -> dict | None:
    log_file = f"/tmp/dipu_{name}.log"
    env = {**os.environ,
           "TRADING_MODE":  mode,
           "AGENT_NAME":    name,
           "AGENT_PORT":    str(port),
           "AGENT_SYMBOL":  symbol.upper()}
    try:
        proc = subprocess.Popen(
            [sys.executable, str(AGENT_SCRIPT)],
            env=env,
            stdout=open(log_file, "w"),
            stderr=subprocess.STDOUT,
        )
        info = {"pid": proc.pid, "port": port, "mode": mode, "symbol": symbol.upper(),
                "started_at": time.time(), "log_file": log_file, "name": name}
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
<title>dipu — agent manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080808;color:#ddd;font-family:'Courier New',monospace;padding:28px}
h1{color:#00e5ff;font-size:1.5rem;margin-bottom:4px}
.sub{color:#fff;font-size:.78rem;margin-bottom:28px}
h2{color:#fff;font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;margin:24px 0 10px}
/* spawn form */
.spawn-form{background:#0e0e0e;border:1px solid #1e1e1e;border-radius:8px;padding:20px;margin-bottom:28px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:5px}
.field label{font-size:.68rem;color:#fff;text-transform:uppercase;letter-spacing:.08em}
.field input,.field select{background:#0a0a0a;border:1px solid #222;color:#ddd;padding:7px 10px;border-radius:5px;font-family:inherit;font-size:.8rem;min-width:160px}
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
</style>
</head>
<body>
<h1>&#9654; dipu — agent manager</h1>
<div class="sub">Spawn and control independent dipu trading instances per environment</div>

<h2>&#9646; Spawn new agent</h2>
<div class="spawn-form">
  <div class="field">
    <label>Agent name</label>
    <input id="f-name" type="text" placeholder="dipu-demo" value="dipu-demo">
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
    <label>Starting coin</label>
    <input id="f-coin" type="text" list="coin-list" placeholder="BTCUSDT" value="BTCUSDT" style="min-width:120px">
    <datalist id="coin-list">
      <option value="BTCUSDT"><option value="ETHUSDT"><option value="BNBUSDT">
      <option value="SOLUSDT"><option value="XRPUSDT"><option value="ADAUSDT">
      <option value="DOGEUSDT"><option value="AVAXUSDT"><option value="DOTUSDT">
      <option value="LINKUSDT"><option value="LTCUSDT"><option value="MATICUSDT">
    </datalist>
  </div>
  <div class="field">
    <label>Dashboard port</label>
    <input id="f-port" type="number" value="7433" min="1024" max="65535" style="min-width:100px">
  </div>
  <button class="btn btn-spawn" onclick="spawnAgent()">&#9654; Spawn</button>
  <span id="spawn-msg" style="font-size:.72rem;color:#00e676;align-self:center"></span>
</div>

<h2>&#9646; Running agents</h2>
<div class="agents" id="agents-grid">
  <div class="no-agents">Loading…</div>
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

async function stopAgent(name) {
  if (!confirm(`Stop agent "${name}"?`)) return;
  await fetch('/api/stop', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name}),
  });
  loadAgents();
}

async function startAgent(name, mode, port, symbol) {
  const r = await fetch('/api/spawn', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, mode, port, symbol: symbol || 'BTCUSDT'}),
  });
  loadAgents();
}

loadAgents();
setInterval(loadAgents, 5000);
</script>
</body>
</html>
"""


class AgentManager:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._app = web.Application()
        self._app.router.add_get("/",                    self._dashboard)
        self._app.router.add_get("/api/agents",          self._list)
        self._app.router.add_post("/api/spawn",          self._spawn)
        self._app.router.add_post("/api/stop",           self._stop)
        self._app.router.add_route("*", "/agent/{name}/{path:.*}", self._proxy)

    async def _dashboard(self, _):
        return web.Response(text=MANAGER_HTML, content_type="text/html")

    async def _list(self, _):
        result = []
        for name, info in list(_agents.items()):
            status = _agent_status(name)
            result.append({**info, "status": status, "name": name})
        return web.json_response(result)

    async def _spawn(self, request: web.Request):
        body   = await request.json()
        name   = body.get("name", "").strip()
        mode   = body.get("mode", "testnet")
        port   = int(body.get("port", MODE_META.get(mode, (7432,))[0]))
        symbol = body.get("symbol", "BTCUSDT").upper().strip() or "BTCUSDT"

        if not name:
            return web.json_response({"error": "name required"}, status=400)
        if mode not in MODE_META:
            return web.json_response({"error": f"unknown mode: {mode}"}, status=400)
        if name in _agents and _pid_alive(_agents[name].get("pid", 0)):
            return web.json_response({"error": f"{name} already running"}, status=409)

        info = _spawn(name, mode, port, symbol)
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

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", MANAGER_PORT)
        await site.start()
        print(f"[manager] running on :{MANAGER_PORT}")


async def main():
    _load_state()
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
