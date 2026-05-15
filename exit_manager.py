"""Module 5 — Exits, Stops, Trailing, Take-Profits."""
import time
from dataclasses import dataclass, field
from logger import log
import config as cfg

# imported lazily to avoid circular import
def _push_tx(pos, close_price: float, reason: str):
    try:
        from instruction_server import push_transaction
        pnl = (close_price - pos.avg_entry) * pos.qty * (1 if pos.side == "BUY" else -1)
        push_transaction({
            "side":   f"{pos.side} CLOSE",
            "symbol": cfg.SYMBOL,
            "qty":    round(pos.qty, 5),
            "price":  round(close_price, 2),
            "stop":   round(pos.stop, 2),
            "tp1":    round(pos.tp1, 2),
            "tp2":    round(pos.tp2, 2),
            "risk":   round(pos.initial_risk, 2),
            "pnl":    round(pnl, 2),
            "status": reason,
        })
    except Exception:
        pass


@dataclass
class Position:
    side:             str
    avg_entry:        float
    qty:              float
    stop:             float
    tp1:              float
    tp2:              float
    tp3:              float
    initial_risk:     float
    entry_ts:         float = field(default_factory=time.time)
    highest_price:    float = 0.0
    lowest_price:     float = float("inf")
    tp1_hit:          bool  = False
    tp2_hit:          bool  = False
    breakeven_set:    bool  = False


class ExitManager:
    def __init__(self):
        self.positions: list[Position] = []

    def attach_exits(self, order: dict, ind: dict) -> Position | None:
        if not order or order.get("status") not in ("FILLED", "PARTIALLY_FILLED"):
            return None
        side       = order["side"]
        entry      = float(order.get("fills", [{}])[0].get("price", order.get("price", 0))) if order.get("fills") else float(order.get("price", 0))
        qty        = float(order["executedQty"])
        atr        = ind["atr14"]
        stop       = (entry - atr * cfg.ATR_STOP_MULT) if side == "BUY" else (entry + atr * cfg.ATR_STOP_MULT)
        risk       = abs(entry - stop) * qty
        direction  = 1 if side == "BUY" else -1
        tp1 = entry + direction * atr * cfg.ATR_STOP_MULT * cfg.TP1_R
        tp2 = entry + direction * atr * cfg.ATR_STOP_MULT * cfg.TP2_R
        tp3 = entry + direction * atr * cfg.ATR_STOP_MULT * cfg.TP3_R

        pos = Position(side=side, avg_entry=entry, qty=qty, stop=stop,
                       tp1=tp1, tp2=tp2, tp3=tp3, initial_risk=risk,
                       highest_price=entry, lowest_price=entry)
        self.positions.append(pos)
        log("MODULE_5", "EXITS_ATTACHED", side=side, entry=entry, stop=round(stop, 2),
            tp1=round(tp1, 2), tp2=round(tp2, 2), tp3=round(tp3, 2), risk_usdt=round(risk, 2))
        return pos

    def manage_open_positions(self, current_price: float, ind: dict) -> list[str]:
        actions = []
        atr = ind["atr14"]
        for pos in list(self.positions):
            direction  = 1 if pos.side == "BUY" else -1
            upnl       = (current_price - pos.avg_entry) * pos.qty * direction
            r_multiple = upnl / pos.initial_risk if pos.initial_risk else 0

            # Track extremes
            if pos.side == "BUY":
                pos.highest_price = max(pos.highest_price, current_price)
            else:
                pos.lowest_price = min(pos.lowest_price, current_price)

            # Hard stop
            hit_stop = (pos.side == "BUY" and current_price <= pos.stop) or \
                       (pos.side == "SELL" and current_price >= pos.stop)
            if hit_stop:
                log("MODULE_5", "STOP_HIT", side=pos.side, stop=pos.stop, price=current_price)
                _push_tx(pos, current_price, "STOP_HIT")
                self.positions.remove(pos)
                actions.append(f"CLOSE:{pos.side}:STOP")
                continue

            # Break-even
            if not pos.breakeven_set and r_multiple >= 1.0:
                buf = pos.avg_entry * 0.0005
                pos.stop        = pos.avg_entry + (buf if pos.side == "BUY" else -buf)
                pos.breakeven_set = True
                log("MODULE_5", "STOP_MOVED_TO_BREAKEVEN", new_stop=round(pos.stop, 2))

            # Trailing stop
            if r_multiple >= 1.5:
                if pos.side == "BUY":
                    trail = pos.highest_price - atr * cfg.ATR_TRAIL_MULT
                    if trail > pos.stop:
                        pos.stop = trail
                        log("MODULE_5", "TRAIL_STOP_UPDATE", stop=round(pos.stop, 2))
                else:
                    trail = pos.lowest_price + atr * cfg.ATR_TRAIL_MULT
                    if trail < pos.stop:
                        pos.stop = trail
                        log("MODULE_5", "TRAIL_STOP_UPDATE", stop=round(pos.stop, 2))

            # TP1
            if not pos.tp1_hit:
                hit = (pos.side == "BUY" and current_price >= pos.tp1) or \
                      (pos.side == "SELL" and current_price <= pos.tp1)
                if hit:
                    pos.qty     *= (1 - cfg.TP1_PCT)
                    pos.tp1_hit  = True
                    log("MODULE_5", "TP1_HIT", price=current_price, remaining_qty=round(pos.qty, 5))
                    actions.append(f"PARTIAL_CLOSE:{pos.side}:TP1")

            # TP2
            elif not pos.tp2_hit:
                hit = (pos.side == "BUY" and current_price >= pos.tp2) or \
                      (pos.side == "SELL" and current_price <= pos.tp2)
                if hit:
                    pos.qty    *= (1 - cfg.TP2_PCT / (1 - cfg.TP1_PCT))
                    pos.tp2_hit = True
                    log("MODULE_5", "TP2_HIT", price=current_price, remaining_qty=round(pos.qty, 5))
                    actions.append(f"PARTIAL_CLOSE:{pos.side}:TP2")

            # Time exit
            hours_held = (time.time() - pos.entry_ts) / 3600
            max_hours  = cfg.MAX_TRADE_HOURS_FUTURES
            if hours_held > max_hours and r_multiple < 0.5:
                log("MODULE_5", "TIME_EXIT_TRIGGERED", hours=round(hours_held, 1), r=round(r_multiple, 2))
                _push_tx(pos, current_price, "TIME_EXIT")
                self.positions.remove(pos)
                actions.append(f"CLOSE:{pos.side}:TIME")

            # Signal-reversal exit
            rsi = ind.get("rsi14", 50)
            macd_cross_down = ind.get("macd", 0) < ind.get("macd_signal", 0)
            price_below_ema9 = current_price < ind.get("ema9", current_price)
            if pos.side == "BUY" and rsi > cfg.RSI_EXIT_LONG and macd_cross_down and price_below_ema9:
                log("MODULE_5", "SIGNAL_REVERSAL_EXIT", side="BUY", rsi=rsi)
                _push_tx(pos, current_price, "SIGNAL_REVERSAL")
                self.positions.remove(pos)
                actions.append("CLOSE:BUY:SIGNAL_REVERSAL")

        return actions
