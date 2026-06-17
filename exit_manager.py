"""Module 5 — Exits, Stops, Trailing, Take-Profits."""
import math
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
            "symbol": getattr(pos, "symbol", cfg.SYMBOL),
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
    symbol:           str   = ""
    entry_ts:         float = field(default_factory=time.time)
    highest_price:    float = 0.0
    lowest_price:     float = float("inf")
    tp1_hit:          bool  = False
    tp2_hit:          bool  = False
    tp3_hit:          bool  = False
    breakeven_set:    bool  = False
    exchange_stop_id: int   = 0     # orderId of the exchange-side stop order (0 = none placed)
    pending_close:    bool  = False  # C-1: set True instead of removing; agent removes after SELL confirmed


class ExitManager:
    def __init__(self):
        self.positions: list[Position] = []

    def attach_exits(self, order: dict, ind: dict, symbol: str = "") -> Position | None:
        if not order or order.get("status") not in ("FILLED", "PARTIALLY_FILLED"):
            return None
        side       = order["side"]
        entry      = float(order.get("fills", [{}])[0].get("price", order.get("price", 0))) if order.get("fills") else float(order.get("price", 0))
        qty        = float(order["executedQty"])
        atr        = ind["atr14"]
        # H-13: guard against NaN/zero ATR — don't attach exits with invalid risk parameters
        if not atr or math.isnan(atr) or atr <= 0:
            log("MODULE_5", "ATR_INVALID_ATTACH", atr=atr, symbol=symbol)
            return None
        stop       = (entry - atr * cfg.ATR_STOP_MULT) if side == "BUY" else (entry + atr * cfg.ATR_STOP_MULT)
        risk       = abs(entry - stop) * qty
        direction  = 1 if side == "BUY" else -1
        h1_range   = ind.get("h1_range", 0)
        # ATR-based TPs are the hard floor — h1_range TPs can extend further but never closer
        tp1_atr = entry + direction * atr * cfg.ATR_STOP_MULT * cfg.TP1_R
        tp2_atr = entry + direction * atr * cfg.ATR_STOP_MULT * cfg.TP2_R
        tp3_atr = entry + direction * atr * cfg.ATR_STOP_MULT * cfg.TP3_R
        if h1_range > 0:
            tp1_h1 = entry + direction * h1_range * 0.40
            tp2_h1 = entry + direction * h1_range * 0.80
            tp3_h1 = entry + direction * h1_range * 1.25
            # Take whichever is further from entry — prevents h1_range collapsing TPs in quiet markets
            if direction == 1:
                tp1, tp2, tp3 = max(tp1_h1, tp1_atr), max(tp2_h1, tp2_atr), max(tp3_h1, tp3_atr)
            else:
                tp1, tp2, tp3 = min(tp1_h1, tp1_atr), min(tp2_h1, tp2_atr), min(tp3_h1, tp3_atr)
        else:
            tp1, tp2, tp3 = tp1_atr, tp2_atr, tp3_atr

        pos = Position(side=side, avg_entry=entry, qty=qty, stop=stop,
                       tp1=tp1, tp2=tp2, tp3=tp3, initial_risk=risk,
                       symbol=symbol or cfg.SYMBOL,
                       highest_price=entry, lowest_price=entry)
        self.positions.append(pos)
        log("MODULE_5", "EXITS_ATTACHED", side=side, entry=entry, stop=round(stop, 2),
            tp1=round(tp1, 2), tp2=round(tp2, 2), tp3=round(tp3, 2), risk_usdt=round(risk, 2))
        return pos

    def manage_open_positions(self, current_price: float, ind: dict) -> list[tuple[str, float, float]]:
        """
        Returns list of (action_str, realized_pnl, sell_qty) tuples.
        sell_qty is the quantity to sell on the exchange for this action.
        realized_pnl is 0.0 for non-closing actions (stop moves, etc.).

        C-1: positions are NOT removed here for full closes — they are marked
        pending_close=True. The agent removes them only after SELL is confirmed.
        H-1: TP3 is treated as a full close (pending_close=True) to eliminate zombies.
        """
        actions: list[tuple[str, float, float]] = []
        atr = ind.get("atr14", 0)
        # H-13: if ATR is invalid, fall back to last-known stop distances for trailing/breakeven
        _atr_valid = atr and not math.isnan(atr) and atr > 0
        if not _atr_valid:
            log("MODULE_5", "ATR_INVALID_MANAGE", atr=atr, msg="trailing/breakeven disabled this cycle")

        for pos in list(self.positions):
            # Skip positions already pending close — agent handles them next cycle
            if pos.pending_close:
                continue

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
                pnl = (current_price - pos.avg_entry) * pos.qty * direction
                sell_qty = pos.qty
                _push_tx(pos, current_price, "STOP_HIT")
                # C-1: mark pending instead of removing — agent removes after confirmed SELL
                pos.pending_close = True
                actions.append((f"CLOSE:{pos.side}:STOP", round(pnl, 4), sell_qty))
                continue

            # Break-even
            if not pos.breakeven_set and r_multiple >= 1.0:
                buf = pos.avg_entry * 0.0005
                pos.stop        = pos.avg_entry + (buf if pos.side == "BUY" else -buf)
                pos.breakeven_set = True
                log("MODULE_5", "STOP_MOVED_TO_BREAKEVEN", new_stop=round(pos.stop, 2))

            # Trailing stop (only when ATR is valid)
            if _atr_valid and r_multiple >= 1.5:
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

            # TP1 — record P&L on the sold slice before reducing qty
            if not pos.tp1_hit:
                hit = (pos.side == "BUY" and current_price >= pos.tp1) or \
                      (pos.side == "SELL" and current_price <= pos.tp1)
                if hit:
                    sold_qty    = pos.qty * cfg.TP1_PCT
                    tp1_pnl     = (current_price - pos.avg_entry) * sold_qty * direction
                    pos.qty    *= (1 - cfg.TP1_PCT)
                    pos.tp1_hit = True
                    # Staircase stop: lock stop at TP1 so remaining position never gives back TP1 level
                    if pos.side == "BUY" and pos.tp1 > pos.stop:
                        pos.stop = pos.tp1
                    elif pos.side == "SELL" and pos.tp1 < pos.stop:
                        pos.stop = pos.tp1
                    log("MODULE_5", "TP1_HIT", price=current_price, remaining_qty=round(pos.qty, 5),
                        stop_locked_to=round(pos.stop, 2))
                    # H-15: return actual sold_qty so agent uses tracked qty not live balance
                    actions.append((f"PARTIAL_CLOSE:{pos.side}:TP1", round(tp1_pnl, 4), sold_qty))

            # TP2 — record P&L on the sold slice before reducing qty
            elif not pos.tp2_hit:
                hit = (pos.side == "BUY" and current_price >= pos.tp2) or \
                      (pos.side == "SELL" and current_price <= pos.tp2)
                if hit:
                    # frac adjusts for already-reduced pos.qty so TP2 sells exactly
                    # cfg.TP2_PCT of the ORIGINAL position
                    frac        = cfg.TP2_PCT / (1 - cfg.TP1_PCT)
                    sold_qty    = pos.qty * frac
                    tp2_pnl     = (current_price - pos.avg_entry) * sold_qty * direction
                    pos.qty    *= (1 - frac)
                    pos.tp2_hit = True
                    # Staircase stop: lock stop at TP2
                    if pos.side == "BUY" and pos.tp2 > pos.stop:
                        pos.stop = pos.tp2
                    elif pos.side == "SELL" and pos.tp2 < pos.stop:
                        pos.stop = pos.tp2
                    log("MODULE_5", "TP2_HIT", price=current_price, remaining_qty=round(pos.qty, 5),
                        stop_locked_to=round(pos.stop, 2))
                    # H-15: return actual sold_qty
                    actions.append((f"PARTIAL_CLOSE:{pos.side}:TP2", round(tp2_pnl, 4), sold_qty))

            # TP3 — H-1 fix: mark pending_close so zombie position is eliminated after confirmed SELL
            elif not pos.tp3_hit:
                hit = (pos.side == "BUY" and current_price >= pos.tp3) or \
                      (pos.side == "SELL" and current_price <= pos.tp3)
                if hit:
                    tp3_pnl     = (current_price - pos.avg_entry) * pos.qty * direction
                    sell_qty    = pos.qty
                    pos.tp3_hit     = True
                    pos.pending_close = True   # H-1: agent removes after confirmed SELL
                    log("MODULE_5", "TP3_HIT", price=current_price, qty=round(pos.qty, 5))
                    actions.append((f"PARTIAL_CLOSE:{pos.side}:TP3", round(tp3_pnl, 4), sell_qty))

            # Time exit — E-6 fix: use MAX_TRADE_HOURS_SPOT for spot trading
            hours_held = (time.time() - pos.entry_ts) / 3600
            max_hours  = cfg.MAX_TRADE_HOURS_SPOT
            is_winning = r_multiple >= 0.5
            effective_max = max_hours * 2 if is_winning else max_hours
            if hours_held > effective_max:
                log("MODULE_5", "TIME_EXIT_TRIGGERED", hours=round(hours_held, 1),
                    r=round(r_multiple, 2), winning=is_winning)
                pnl = (current_price - pos.avg_entry) * pos.qty * direction
                sell_qty = pos.qty
                _push_tx(pos, current_price, "TIME_EXIT")
                pos.pending_close = True   # C-1: agent removes after confirmed SELL
                actions.append((f"CLOSE:{pos.side}:TIME", round(pnl, 4), sell_qty))
                continue

            # Signal-reversal exit
            rsi = ind.get("rsi14", 50)
            macd_cross_down = ind.get("macd", 0) < ind.get("macd_signal", 0)
            price_below_ema9 = current_price < ind.get("ema9", current_price)
            if pos.side == "BUY" and rsi > cfg.RSI_EXIT_LONG and macd_cross_down and price_below_ema9:
                log("MODULE_5", "SIGNAL_REVERSAL_EXIT", side="BUY", rsi=rsi)
                pnl = (current_price - pos.avg_entry) * pos.qty * direction
                sell_qty = pos.qty
                _push_tx(pos, current_price, "SIGNAL_REVERSAL")
                pos.pending_close = True   # C-1: agent removes after confirmed SELL
                actions.append(("CLOSE:BUY:SIGNAL_REVERSAL", round(pnl, 4), sell_qty))

        return actions
