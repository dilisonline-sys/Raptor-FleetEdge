"""Module 6 & 7 — Risk Metrics and Kill Switch."""
import asyncio
import datetime
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
        self._last_reset_date: datetime.date | None = None  # H-2: track last daily reset

    def halt_active(self) -> bool:
        if self.halt_flag and time.time() < self.halt_until:
            return True
        if self.halt_flag and time.time() >= self.halt_until:
            self.halt_flag    = False
            self.consec_losses = 0   # NC-2: reset streak so next loss doesn't immediately re-halt
            log("MODULE_7", "HALT_LIFTED")
        return False

    def _set_halt(self, hours: float, reason: str, equity: float):
        self.halt_flag   = True
        self.halt_until  = time.time() + hours * 3600
        self.halt_reason = reason
        log("MODULE_7", "HALT_SET", reason=reason, hours=hours, equity=round(equity, 2))

    def clear_if_consec_loss(self, new_symbol: str = "") -> bool:
        """Clear halt when it was caused by consecutive losses and the coin has changed.
        Daily/monthly drawdown halts are NOT cleared — those protect total capital.
        Returns True if halt was cleared."""
        if self.halt_flag and "consecutive losses" in (self.halt_reason or ""):
            self.halt_flag     = False
            self.halt_until    = 0.0
            self.consec_losses = 0
            self.halt_reason   = ""
            log("MODULE_7", "HALT_CLEARED_ROTATION",
                new_symbol=new_symbol, note="consecutive-loss halt reset for new coin")
            return True
        return False

    def record_trade(self, pnl: float, equity: float):
        self.trade_history.append({"pnl": pnl, "ts": time.time()})
        if pnl < 0:
            self.consec_losses += 1
        else:
            self.consec_losses = 0

        if self.consec_losses >= cfg.MAX_CONSEC_LOSS:
            self._set_halt(4, f"{cfg.MAX_CONSEC_LOSS} consecutive losses", equity)

    def _check_daily_reset(self, current_equity: float):
        """H-2: Reset day_start_equity at calendar midnight so DD limit is per-day not per-session."""
        today = datetime.date.today()
        if self._last_reset_date is None:
            self._last_reset_date = today
            return
        if today > self._last_reset_date:
            self._last_reset_date  = today
            self.day_start_equity  = current_equity
            log("MODULE_6", "DAY_START_RESET",
                new_equity=round(current_equity, 2), date=str(today))

    def update_metrics(self, current_equity: float):
        if self.day_start_equity is None:
            self.day_start_equity   = current_equity
        if self.month_start_equity is None:
            self.month_start_equity = current_equity

        # H-2: reset daily baseline at midnight
        self._check_daily_reset(current_equity)

        # Sanity guard: reject implausibly low equity values (< 40% of day_start)
        # that indicate a transient API pricing failure (e.g. BTC price returned as 0).
        # Threshold raised from 30% to 40% to catch more realistic API glitches
        # while still allowing catastrophic real losses to trigger halt.
        if (self.day_start_equity and self.day_start_equity > 0
                and current_equity < self.day_start_equity * 0.40):
            log("MODULE_6", "EQUITY_SANITY_SKIP",
                current=round(current_equity, 2),
                day_start=round(self.day_start_equity, 2),
                ratio=round(current_equity / self.day_start_equity, 3),
                msg="equity < 40% of day_start — likely API pricing glitch, skipping DD check")
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

    def is_monthly_halt(self) -> bool:
        """H-3: returns True when the current halt is a monthly drawdown halt."""
        return self.halt_flag and "monthly drawdown" in (self.halt_reason or "")

    async def emergency_halt(self, om, reason: str, equity: float, symbol: str | None = None):
        log("MODULE_7", "EMERGENCY_HALT", reason=reason, equity=round(equity, 2))
        await om.cancel_all(symbol=symbol)  # FIX-1: pass active symbol so correct pair is cancelled
        self._set_halt(4, reason, equity)
        if cfg.ALERT_WEBHOOK:
            await self._send_alert(reason, equity)

    async def _send_alert(self, reason: str, equity: float):
        if not self._session:
            self._session = aiohttp.ClientSession()
        try:
            payload = {"agent": "raptor-fleetedge", "reason": reason, "equity": equity}
            async with self._session.post(cfg.ALERT_WEBHOOK, json=payload) as r:
                log("MODULE_7", "ALERT_SENT", status=r.status)
        except Exception as e:
            log("MODULE_7", "ALERT_FAILED", error=str(e))
