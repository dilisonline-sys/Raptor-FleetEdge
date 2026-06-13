"""Module 6 & 7 — Risk Metrics and Kill Switch."""
import asyncio
import time
import aiohttp
from logger import log
import config as cfg


class RiskEngine:
    def __init__(self):
        self.halt_flag         = False
        self.halt_until        = 0.0
        self.halt_reason       = ""     # human-readable reason for the current halt
        self.day_start_equity  = None
        self.month_start_equity= None
        self.consec_losses     = 0
        self.trade_history: list[dict] = []
        self._session: aiohttp.ClientSession | None = None

    def halt_active(self) -> bool:
        if self.halt_flag and time.time() < self.halt_until:
            return True
        if self.halt_flag and time.time() >= self.halt_until:
            self.halt_flag = False
            log("MODULE_7", "HALT_LIFTED")
        return False

    def _set_halt(self, hours: float, reason: str, equity: float):
        self.halt_flag   = True
        self.halt_until  = time.time() + hours * 3600
        self.halt_reason = reason
        log("MODULE_7", "HALT_SET", reason=reason, hours=hours, equity=round(equity, 2))

    def record_trade(self, pnl: float, equity: float):
        self.trade_history.append({"pnl": pnl, "ts": time.time()})
        if pnl < 0:
            self.consec_losses += 1
        else:
            self.consec_losses = 0

        if self.consec_losses >= cfg.MAX_CONSEC_LOSS:
            self._set_halt(4, f"{cfg.MAX_CONSEC_LOSS} consecutive losses", equity)

    def update_metrics(self, current_equity: float):
        if self.day_start_equity is None:
            self.day_start_equity   = current_equity
        if self.month_start_equity is None:
            self.month_start_equity = current_equity

        # Sanity guard: reject implausibly low equity values (< 30% of day_start)
        # that indicate a transient API pricing failure (e.g. BTC price returned as 0).
        # Such glitches should not trigger protective halts.
        if (self.day_start_equity and self.day_start_equity > 0
                and current_equity < self.day_start_equity * 0.30):
            log("MODULE_6", "EQUITY_SANITY_SKIP",
                current=round(current_equity, 2),
                day_start=round(self.day_start_equity, 2),
                ratio=round(current_equity / self.day_start_equity, 3),
                msg="equity too low vs day_start — likely API pricing glitch, skipping DD check")
            return

        daily_dd   = ((self.day_start_equity - current_equity) / self.day_start_equity
                      if self.day_start_equity else 0.0)
        monthly_dd = ((self.month_start_equity - current_equity) / self.month_start_equity
                      if self.month_start_equity else 0.0)

        if daily_dd >= cfg.DAILY_DD_LIMIT:
            reason = (f"daily drawdown {daily_dd:.1%} "
                      f"(start ${self.day_start_equity:.2f} → now ${current_equity:.2f})")
            self._set_halt(4, reason, current_equity)
        if monthly_dd >= cfg.MONTHLY_DD_LIMIT:
            reason = (f"monthly drawdown {monthly_dd:.1%} "
                      f"(start ${self.month_start_equity:.2f} → now ${current_equity:.2f})")
            self._set_halt(24 * 30, reason, current_equity)

        log("MODULE_6", "METRICS_UPDATE",
            equity=round(current_equity, 2),
            day_start=round(self.day_start_equity, 2) if self.day_start_equity else None,
            daily_dd=round(daily_dd * 100, 2),
            monthly_dd=round(monthly_dd * 100, 2),
            consec_losses=self.consec_losses)

    async def emergency_halt(self, om, reason: str, equity: float):
        log("MODULE_7", "EMERGENCY_HALT", reason=reason, equity=round(equity, 2))
        await om.cancel_all()
        self._set_halt(4, reason, equity)
        if cfg.ALERT_WEBHOOK:
            await self._send_alert(reason, equity)

    async def _send_alert(self, reason: str, equity: float):
        if not self._session:
            self._session = aiohttp.ClientSession()
        try:
            payload = {"agent": "dipu", "reason": reason, "equity": equity}
            async with self._session.post(cfg.ALERT_WEBHOOK, json=payload) as r:
                log("MODULE_7", "ALERT_SENT", status=r.status)
        except Exception as e:
            log("MODULE_7", "ALERT_FAILED", error=str(e))
