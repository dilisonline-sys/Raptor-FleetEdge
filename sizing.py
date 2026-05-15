"""Module 2 — Position Sizing."""
from logger import log
import config as cfg


class PositionSizer:

    @staticmethod
    def calculate(equity: float, entry_price: float, stop_distance: float,
                  size_mult: float, correlated: bool = False) -> float | None:

        risk_amount = equity * cfg.RISK_PCT
        if stop_distance <= 0:
            log("MODULE_2", "SIZING_ABORT", reason="stop_distance <= 0")
            return None

        base_qty = risk_amount / stop_distance

        # Volatility adjustment
        vol_ratio = stop_distance / entry_price
        if vol_ratio > 0.04:
            vol_mult = 0.5
        elif vol_ratio > 0.025:
            vol_mult = 0.75
        else:
            vol_mult = 1.0

        adjusted_qty = base_qty * vol_mult * size_mult

        # Correlation reduction
        if correlated:
            adjusted_qty *= 0.5

        # Hard caps
        max_by_trade = (equity * cfg.MAX_TRADE_PCT) / entry_price
        final_qty    = min(adjusted_qty, max_by_trade)

        if final_qty * entry_price < 10.0:
            log("MODULE_2", "SIZING_ABORT", reason="order below 10 USDT minimum")
            return None

        log("MODULE_2", "SIZING_CALC",
            equity=equity, risk_amount=risk_amount,
            base_qty=round(base_qty, 6), vol_mult=vol_mult,
            size_mult=size_mult, final_qty=round(final_qty, 6))

        return final_qty
