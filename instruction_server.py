"""
Multi-agent instruction interface for dipu.

External bots, signal providers, or OpenClaw agents send trade instructions here.
The Binance account used for execution is intentionally EXCLUDED as an instruction source —
instructions must come from authorized external signal tokens only.
"""
import asyncio
import json
from aiohttp import web
from logger import log
import config as cfg


VALID_ACTIONS = {"BUY", "SELL", "CLOSE_ALL", "HALT", "STATUS"}


def _auth(request: web.Request) -> bool:
    token = request.headers.get("X-Agent-Token", "")
    if not cfg.AUTHORIZED_AGENT_TOKENS:
        log("INSTRUCTION_SERVER", "AUTH_WARN", msg="No authorized tokens configured — rejecting all")
        return False
    return token in cfg.AUTHORIZED_AGENT_TOKENS


class InstructionServer:
    def __init__(self, signal_queue: asyncio.Queue):
        self._queue = signal_queue
        self._app   = web.Application()
        self._app.router.add_post("/instruction", self._handle)
        self._app.router.add_get("/status",       self._status)

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
            "qty_pct": float(body.get("qty_pct", 1.0)),  # fraction of max size
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
