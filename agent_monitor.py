"""
Agent Monitor — checks all running Raptor FleetEdge agents every 30 minutes for issues
and auto-resolves halts when it is safe to do so.

Auto-resolution (runs before the health check each cycle):
  - Queries each agent's /api/state for live halt status
  - If halt=True AND today's daily drawdown < AUTO_RESUME_DD_THRESHOLD (5%):
      1. Sends RESUME instruction to clear halt_flag / halt_until
      2. Waits 2 s, then sends RESET_DAY_START to reset risk baselines to
         current equity (prevents immediate re-halt on next metrics update)
      3. Resets /tmp/rfe_portfolio_day.json baseline to current total assets
  - Skips auto-resume when today's DD is still ≥ threshold (active loss)

Checks performed:
  - LOOP_ERROR bursts (numpy/json crashes, unexpected exceptions)
  - DD halt active (agent stuck waiting for halt to lift)
  - Dead slot (no heartbeat in pool for > 90 s)
  - High daily drawdown (> 8% — warning before 10% halt)
  - Stale log (agent not logging — possible freeze)

Writes a report to /tmp/rfe_monitor.log and always emails a report —
clean 'all OK' heartbeat every 30 min, or flagged report with resolutions.
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

POOL_FILE     = Path("/tmp/rfe_equity_pool.json")
LOG_DIR       = Path("/tmp")
MONITOR_LOG   = Path("/tmp/rfe_monitor.log")
SLOT_NAMES    = {0: "fleetedge1", 1: "fleetedge2", 2: "fleetedge3", 3: "fleetedge4"}
AGENT_PORTS   = {0: 7434, 1: 7435, 2: 7436, 3: 7437}
WINDOW_SECS   = 30 * 60           # look back 30 min for log issues
STALE_SECS    = 120               # agent log silent for 2 min = stale
ERROR_BURST   = 3                 # ≥3 LOOP_ERRORs in window = issue
DD_WARN_PCT   = 8.0               # warn before 10% halt threshold
SLOT_TTL      = 90                # pool heartbeat TTL (seconds)
AUTO_RESUME_DD_THRESHOLD = 5.0    # auto-resume halted agent only if today's DD < this %


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_ts(ts_str: str) -> float:
    """ISO8601 → unix timestamp."""
    try:
        return datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return 0.0


def _get_agent_state(port: int) -> dict | None:
    """Fetch live agent state via HTTP /api/state."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/state",
            headers={"X-Agent-Token": "internal"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _send_instruction(port: int, action: str, **kwargs) -> bool:
    """POST an instruction to a running agent; returns True on success."""
    try:
        payload = json.dumps({"action": action, "source": "auto_monitor", **kwargs}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/instruction",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Agent-Token": "internal",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _reset_portfolio_day_start() -> float:
    """Reset /tmp/rfe_portfolio_day.json to today's total assets. Returns new baseline."""
    try:
        import portfolio_tracker as _pt
        state = _pt.get_portfolio_state()
        total = state.get("total_assets", 0.0)
        if total <= 0:
            return 0.0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_file = Path("/tmp/rfe_portfolio_day.json")
        existing: dict = {}
        if day_file.exists():
            try:
                with open(day_file) as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing.update({"assets": total, "ts": time.time(), "date": today})
        with open(day_file, "w") as f:
            json.dump(existing, f)
        return total
    except Exception:
        return 0.0


def auto_resolve() -> list[dict]:
    """
    Inspect each agent via HTTP and auto-resume any that are halted but whose
    today's daily drawdown is below AUTO_RESUME_DD_THRESHOLD.

    Steps per halted agent:
      1. Send RESUME to clear halt_flag / halt_until.
      2. Wait 2 s for the instruction to be consumed.
      3. Send RESET_DAY_START to reset risk baselines to current equity so
         the agent doesn't immediately re-halt on the next metrics update.

    After all resumes, resets the portfolio day-start file so the new
    baseline is persisted for future restarts.

    Returns a list of resolution-result dicts (one per inspected slot).
    """
    resolved: list[dict] = []
    any_resumed = False

    for slot_id in range(4):
        name = SLOT_NAMES[slot_id]
        port = AGENT_PORTS[slot_id]

        state = _get_agent_state(port)
        if state is None:
            resolved.append({
                "slot": slot_id, "name": name,
                "action": "UNREACHABLE",
                "reason": "Agent did not respond — may be stopped",
            })
            continue

        if not state.get("halt"):
            continue  # healthy — nothing to do

        daily_dd = state.get("daily_dd", 0.0)  # already in percent units

        if daily_dd >= AUTO_RESUME_DD_THRESHOLD:
            resolved.append({
                "slot": slot_id, "name": name,
                "action": "SKIPPED",
                "reason": (
                    f"Daily DD {daily_dd:.1f}% still above {AUTO_RESUME_DD_THRESHOLD}% threshold"
                    f" — not auto-resuming (active loss situation)"
                ),
            })
            continue

        resume_ok = _send_instruction(port, "RESUME")
        time.sleep(2)
        reset_ok  = _send_instruction(port, "RESET_DAY_START")
        any_resumed = True

        resolved.append({
            "slot":       slot_id,
            "name":       name,
            "action":     "RESUMED",
            "resume_ok":  resume_ok,
            "reset_ok":   reset_ok,
            "daily_dd_pct": round(daily_dd, 2),
            "reason": (
                f"Auto-resumed: halt active but today's DD {daily_dd:.1f}% "
                f"is below {AUTO_RESUME_DD_THRESHOLD}% safe threshold"
            ),
        })

    if any_resumed:
        _reset_portfolio_day_start()

    return resolved


def _read_pool() -> dict:
    try:
        with open(POOL_FILE) as f:
            return json.load(f)
    except Exception:
        return {"slots": {}}


def _scan_agent_log(name: str, since: float) -> dict:
    """
    Scan the agent's log file for issues within the last WINDOW_SECS.
    Returns a dict of findings.
    """
    log_path = LOG_DIR / f"rfe_{name}.log"
    findings = {
        "loop_errors":   0,
        "halted":        False,
        "last_log_ts":   0.0,
        "log_exists":    log_path.exists(),
    }
    if not findings["log_exists"]:
        return findings

    try:
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                # HTTP access log lines — extract timestamp from leading bracket
                continue

            ts = _parse_ts(entry.get("ts", ""))
            if ts > findings["last_log_ts"]:
                findings["last_log_ts"] = ts

            if ts < since:
                continue  # outside window

            action = entry.get("action", "")
            if action == "LOOP_ERROR":
                findings["loop_errors"] += 1
            if action == "HALTED":
                findings["halted"] = True

    except Exception:
        pass

    return findings


def run_check() -> dict:
    """
    Run one full health check across all slots.
    Returns {
        'ts':       str,
        'issues':   [ {slot, name, severity, msg} ],
        'slots':    { slot_id: {symbol, open_usdt, daily_pnl, dd_pct, ...} },
        'healthy':  bool,
    }
    """
    now   = time.time()
    since = now - WINDOW_SECS
    pool  = _read_pool()
    slots = pool.get("slots", {})

    issues      = []
    slot_report = {}

    for slot_id in range(4):
        name     = SLOT_NAMES[slot_id]
        port     = AGENT_PORTS[slot_id]
        s        = slots.get(str(slot_id))
        log      = _scan_agent_log(name, since)
        live     = _get_agent_state(port)

        # Prefer live HTTP state for halt flag — more accurate than log scan
        live_halt = live.get("halt", False) if live else log["halted"]

        entry = {
            "name":        name,
            "symbol":      s["symbol"]      if s else (live.get("symbol", "—") if live else "—"),
            "open_usdt":   s["open_usdt"]   if s else 0.0,
            "daily_pnl":   s["daily_pnl"]   if s else 0.0,
            "daily_dd":    live.get("daily_dd", 0.0) if live else 0.0,
            "loop_errors": log["loop_errors"],
            "halted":      live_halt,
            "log_exists":  log["log_exists"],
        }

        # ── Dead slot (no pool heartbeat) ────────────────────────────
        if not s:
            issues.append({
                "slot": slot_id, "name": name,
                "severity": "CRITICAL",
                "msg": "Not registered in equity pool — agent may be down",
            })
        elif now - s.get("ts", 0) > SLOT_TTL:
            age = int(now - s.get("ts", 0))
            issues.append({
                "slot": slot_id, "name": name,
                "severity": "CRITICAL",
                "msg": f"Pool heartbeat stale ({age}s) — agent frozen or crashed",
            })

        # ── Log missing / stale ──────────────────────────────────────
        if not log["log_exists"]:
            issues.append({
                "slot": slot_id, "name": name,
                "severity": "WARNING",
                "msg": "Log file missing — agent never started or log was deleted",
            })
        elif log["last_log_ts"] > 0 and now - log["last_log_ts"] > STALE_SECS:
            silent = int(now - log["last_log_ts"])
            issues.append({
                "slot": slot_id, "name": name,
                "severity": "WARNING",
                "msg": f"Log silent for {silent}s — agent may be frozen",
            })

        # ── LOOP_ERROR burst ─────────────────────────────────────────
        if log["loop_errors"] >= ERROR_BURST:
            issues.append({
                "slot": slot_id, "name": name,
                "severity": "ERROR",
                "msg": f"{log['loop_errors']} LOOP_ERRORs in last 30 min — check logger/numpy compatibility",
            })

        # ── Halt active ──────────────────────────────────────────────
        if log["halted"]:
            pnl_str = f"${entry['daily_pnl']:.2f}"
            issues.append({
                "slot": slot_id, "name": name,
                "severity": "WARNING",
                "msg": f"Halt active (daily P&L {pnl_str}) — waiting for 4h halt window to expire",
            })

        # ── High drawdown warning ────────────────────────────────────
        # Only meaningful when daily_pnl loss is at least $2 (avoids false alarms on dust)
        daily_pnl = entry["daily_pnl"]
        if s and daily_pnl < -2.0:
            day_start_approx = entry["open_usdt"] - daily_pnl  # open + loss = start
            if day_start_approx > 5.0:
                dd_pct = abs(daily_pnl) / day_start_approx * 100
                entry["dd_pct"] = round(dd_pct, 2)
                if dd_pct >= DD_WARN_PCT:
                    issues.append({
                        "slot": slot_id, "name": name,
                        "severity": "WARNING",
                        "msg": f"Daily drawdown {dd_pct:.1f}% — approaching 10% halt threshold",
                    })
            else:
                entry["dd_pct"] = 0.0
        else:
            entry["dd_pct"] = 0.0

        slot_report[slot_id] = entry

    try:
        import portfolio_tracker as _pt
        pf = _pt.get_portfolio_state()
    except Exception:
        pf = {}

    return {
        "ts":        _now_utc(),
        "issues":    issues,
        "slots":     slot_report,
        "healthy":   len(issues) == 0,
        "portfolio": pf,
    }


def write_report(result: dict) -> str:
    """Append structured report to MONITOR_LOG, return text summary."""
    lines = [
        "=" * 64,
        f"  DIPU AGENT HEALTH CHECK  —  {result['ts']}",
        "=" * 64,
    ]

    resumed = result.get("auto_resumed", 0)
    if result["healthy"] and not resumed:
        lines.append("  STATUS: ALL OK — no issues detected")
    elif resumed:
        lines.append(f"  STATUS: {resumed} AGENT(S) AUTO-RESUMED — see Resolutions below")
    else:
        lines.append(f"  STATUS: {len(result['issues'])} ISSUE(S) FOUND")

    lines.append("")
    lines.append("  SLOT SUMMARY")
    lines.append("  " + "-" * 60)
    for sid, s in result["slots"].items():
        pnl_sign = "+" if s["daily_pnl"] >= 0 else ""
        halted_tag = " [HALTED]" if s["halted"] else ""
        errs_tag   = f" [LOOP_ERR×{s['loop_errors']}]" if s["loop_errors"] else ""
        lines.append(
            f"  Slot {sid} {s['name']:14s}  {s['symbol']:12s} "
            f"P&L {pnl_sign}${s['daily_pnl']:.2f}  open ${s['open_usdt']:.2f}"
            f"{halted_tag}{errs_tag}"
        )

    pf = result.get("portfolio", {})
    if pf and pf.get("total_assets"):
        pnl_sign = "+" if pf.get("pnl_usdt", 0) >= 0 else ""
        lines.append("")
        lines.append("  PORTFOLIO")
        lines.append("  " + "-" * 60)
        lines.append(
            f"  Total ${pf['total_assets']:.2f}  "
            f"USDT ${pf.get('usdt_free', 0):.2f}  "
            f"Spot ${pf.get('coin_value', 0):.2f}  "
            f"Earn ${pf.get('earn_value', 0):.2f}  "
            f"Day P&L {pnl_sign}${pf['pnl_usdt']:.2f} ({pnl_sign}{pf.get('pnl_pct', 0):.2f}%)  "
            f"Start ${pf.get('day_start', 0):.2f}"
        )

    if result["issues"]:
        lines.append("")
        lines.append("  ISSUES")
        lines.append("  " + "-" * 60)
        for iss in result["issues"]:
            lines.append(f"  [{iss['severity']:8s}] Slot {iss['slot']} {iss['name']}: {iss['msg']}")

    resolutions = result.get("resolutions", [])
    acted = [r for r in resolutions if r.get("action") in ("RESUMED", "SKIPPED")]
    if acted:
        lines.append("")
        lines.append("  AUTO-RESOLUTION")
        lines.append("  " + "-" * 60)
        for r in acted:
            tag = "RESUMED " if r["action"] == "RESUMED" else "SKIPPED "
            lines.append(f"  [{tag}] Slot {r['slot']} {r['name']}: {r['reason']}")

    lines.append("")
    text = "\n".join(lines) + "\n"

    try:
        with open(MONITOR_LOG, "a") as f:
            f.write(text)
        # Keep monitor log from growing unbounded — cap at ~500 KB
        size = MONITOR_LOG.stat().st_size
        if size > 500_000:
            content = MONITOR_LOG.read_text()
            # Drop the oldest half
            mid = len(content) // 2
            MONITOR_LOG.write_text(content[content.find("\n", mid) + 1:])
    except Exception:
        pass

    return text


def send_monitor_email(result: dict) -> bool:
    """Send health report via email_notifier."""
    try:
        import email_notifier as _em
        cfg = _em.load_config()
        if not cfg.get("enabled") or not cfg.get("recipient"):
            return False

        resumed_count = result.get("auto_resumed", 0)
        if result["healthy"] and not resumed_count:
            status_color = "#00e676"
            status_text  = "ALL OK"
        elif resumed_count:
            status_color = "#ffd600"
            status_text  = f"{resumed_count} AGENT(S) AUTO-RESUMED"
        else:
            status_color = "#ff1744"
            status_text  = f"{len(result['issues'])} ISSUE(S)"

        # Slot rows
        slot_rows = ""
        for sid, s in result["slots"].items():
            pnl = s["daily_pnl"]
            pnl_color = "#00e676" if pnl >= 0 else "#ff1744"
            pnl_str   = f"{'+'if pnl>=0 else ''}${pnl:.2f}"
            tags = []
            if s["halted"]:
                tags.append('<span style="color:#ffd600">HALTED</span>')
            if s["loop_errors"]:
                tags.append(f'<span style="color:#ff1744">LOOP_ERR×{s["loop_errors"]}</span>')
            tag_html = " ".join(tags) if tags else '<span style="color:#444">OK</span>'
            slot_rows += (
                f'<tr>'
                f'<td style="padding:5px 10px;color:#fff">{sid}</td>'
                f'<td style="padding:5px 10px;color:#00e5ff">{s["symbol"]}</td>'
                f'<td style="padding:5px 10px;color:{pnl_color}">{pnl_str}</td>'
                f'<td style="padding:5px 10px">{tag_html}</td>'
                f'</tr>'
            )

        # Portfolio summary row
        pf = result.get("portfolio", {})
        portfolio_html = ""
        if pf and pf.get("total_assets"):
            pnl_color = "#00e676" if pf.get("pnl_usdt", 0) >= 0 else "#ff1744"
            pnl_sign  = "+" if pf.get("pnl_usdt", 0) >= 0 else ""
            pnl_str   = f"{pnl_sign}${pf['pnl_usdt']:.2f} ({pnl_sign}{pf.get('pnl_pct', 0):.2f}%)"
            portfolio_html = (
                f'<div style="background:#0d1a0d;border:1px solid #1a2e1a;border-radius:6px;'
                f'padding:10px 14px;margin-bottom:14px;font-size:13px">'
                f'<span style="color:#fff;text-transform:uppercase;font-size:11px;letter-spacing:.08em">Portfolio</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#fff">Total:</span> <strong style="color:#00e676">${pf["total_assets"]:.2f}</strong>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#fff">USDT:</span> <span style="color:#fff">${pf.get("usdt_free", 0):.2f}</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#fff">Spot:</span> <span style="color:#00e5ff">${pf.get("coin_value", 0):.2f}</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#fff">Earn:</span> <span style="color:#ffd600">${pf.get("earn_value", 0):.2f}</span>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#fff">Day P&amp;L:</span> <strong style="color:{pnl_color}">{pnl_str}</strong>'
                f'&nbsp;&nbsp;'
                f'<span style="color:#444">Start: ${pf.get("day_start", 0):.2f}</span>'
                f'</div>'
            )

        # Issue rows
        issue_html = ""
        if result["issues"]:
            sev_colors = {"CRITICAL": "#ff1744", "ERROR": "#ff6d00", "WARNING": "#ffd600"}
            issue_items = ""
            for iss in result["issues"]:
                c = sev_colors.get(iss["severity"], "#aaa")
                issue_items += (
                    f'<li style="margin:6px 0">'
                    f'<span style="color:{c};font-weight:bold">[{iss["severity"]}]</span> '
                    f'<span style="color:#fff">{iss["name"]}: {iss["msg"]}</span>'
                    f'</li>'
                )
            issue_html = (
                f'<p style="color:#fff;margin:16px 0 6px">Issues detected:</p>'
                f'<ul style="margin:0;padding-left:18px">{issue_items}</ul>'
            )

        # Auto-resolution rows
        resolution_html = ""
        resolutions = result.get("resolutions", [])
        acted = [r for r in resolutions if r.get("action") in ("RESUMED", "SKIPPED")]
        if acted:
            res_items = ""
            for r in acted:
                if r["action"] == "RESUMED":
                    icon  = "&#x2705;"  # ✅
                    color = "#00e676"
                    label = "RESUMED"
                else:
                    icon  = "&#x26A0;"  # ⚠
                    color = "#ffd600"
                    label = "SKIPPED"
                res_items += (
                    f'<li style="margin:6px 0">'
                    f'<span style="color:{color};font-weight:bold">{icon} [{label}]</span> '
                    f'<span style="color:#fff">{r["name"]}: {r["reason"]}</span>'
                    f'</li>'
                )
            resolution_html = (
                f'<p style="color:#fff;margin:16px 0 6px">&#x1F527; Auto-resolution actions:</p>'
                f'<ul style="margin:0;padding-left:18px">{res_items}</ul>'
            )

        html = _em._style_wrap(f"""
          <h2 style="color:{status_color};margin:0 0 4px">&#x1F916; Agent Health Check</h2>
          <p style="color:#fff;font-size:12px;margin:0 0 16px">{result['ts']}</p>
          <p style="margin:0 0 14px">
            Status:&nbsp;<strong style="color:{status_color}">{status_text}</strong>
          </p>
          {portfolio_html}
          <table style="width:100%;border-collapse:collapse;margin-bottom:14px">
            <thead>
              <tr style="border-bottom:1px solid #1e1e1e">
                <th style="padding:5px 10px;text-align:left;color:#fff">Slot</th>
                <th style="padding:5px 10px;text-align:left;color:#fff">Symbol</th>
                <th style="padding:5px 10px;text-align:left;color:#fff">P&amp;L</th>
                <th style="padding:5px 10px;text-align:left;color:#fff">State</th>
              </tr>
            </thead>
            <tbody>{slot_rows}</tbody>
          </table>
          {issue_html}
          {resolution_html}
          <p style="color:#444;font-size:11px;margin:16px 0 0">
            Log: /tmp/rfe_monitor.log &nbsp;|&nbsp; Check interval: 30 min
          </p>""")

        if result.get("auto_resumed"):
            subject = f"[Raptor FleetEdge] ✅ {result['auto_resumed']} agent(s) auto-resumed — {result['ts']}"
        elif result["healthy"]:
            subject = f"[Raptor FleetEdge] Health OK — {result['ts']}"
        else:
            subject = f"[Raptor FleetEdge] ⚠ {len(result['issues'])} issue(s) detected — {result['ts']}"
        ok, reason = _em._send(subject, html)
        if not ok:
            print(f"[monitor] email failed: {reason}")
        return ok
    except Exception as e:
        print(f"[monitor] email failed: {e}")
        return False


def check_and_report() -> dict:
    """
    Run auto-resolution, health check, write log, send email.
    Called by the manager scheduler every 30 minutes.
    """
    resolutions = auto_resolve()
    result = run_check()
    result["resolutions"] = resolutions

    # If anything was auto-resolved, mark as not fully healthy so the
    # email subject is flagged — operator should know action was taken.
    resumed_count = sum(1 for r in resolutions if r.get("action") == "RESUMED")
    if resumed_count:
        result["auto_resumed"] = resumed_count

    write_report(result)
    send_monitor_email(result)
    return result


if __name__ == "__main__":
    # Standalone run for testing
    r = check_and_report()
    n = len(r["issues"])
    print(f"[monitor] Check complete — {'OK' if r['healthy'] else str(n) + ' issues'}")
    for iss in r["issues"]:
        print(f"  [{iss['severity']}] {iss['name']}: {iss['msg']}")
