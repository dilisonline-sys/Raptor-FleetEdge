"""Module 2 — Position Sizing."""
from logger import log
import config as cfg


class PositionSizer:

    @staticmethod
    def calculate(equity: float, entry_price: float, stop_distance: float,
                  size_mult: float, correlated: bool = False,
                  usdt_available: float | None = None,
                  pool_budget: float | None = None) -> float | None:

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

        # Pool budget cap: shared equity pool limits per-agent deployment
        if pool_budget is not None and pool_budget > 0:
            max_by_pool = pool_budget / entry_price * 0.98
            final_qty   = min(final_qty, max_by_pool)

        # Spot BUY: cap to free USDT (portfolio equity includes held base asset value)
        if usdt_available is not None and usdt_available > 0:
            max_by_usdt = (usdt_available * 0.98) / entry_price  # 2% buffer for fees
            final_qty   = min(final_qty, max_by_usdt)

        if final_qty * entry_price < 10.0:
            log("MODULE_2", "SIZING_ABORT", reason="order below 10 USDT minimum",
                final_qty=round(final_qty, 8), entry_price=round(entry_price, 2),
                order_value=round(final_qty * entry_price, 4),
                equity=round(equity, 2), stop_d=round(stop_distance, 4),
                size_mult=size_mult, vol_mult=vol_mult,
                usdt_avail=round(usdt_available, 2) if usdt_available else None,
                max_by_trade=round(max_by_trade, 8),
                max_by_usdt=round((usdt_available * 0.98) / entry_price, 8) if usdt_available else None)
            return None

        log("MODULE_2", "SIZING_CALC",
            equity=equity, risk_amount=risk_amount,
            base_qty=round(base_qty, 6), vol_mult=vol_mult,
            size_mult=size_mult, final_qty=round(final_qty, 6),
            usdt_cap=round(usdt_available, 2) if usdt_available else None,
            pool_cap=round(pool_budget, 2) if pool_budget else None)

        return final_qty
