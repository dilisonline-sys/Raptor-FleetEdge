"""
Agent Monitor — checks all running dipu agents every 30 minutes for issues.

Checks performed:
  - LOOP_ERROR bursts (numpy/json crashes, unexpected exceptions)
  - DD halt active (agent stuck waiting for halt to lift)
  - Dead slot (no heartbeat in pool for > 90 s)
  - High daily drawdown (> 8% — warning before 10% halt)
  - Stale log (agent not logging — possible freeze)

Writes a report to /tmp/dipu_monitor.log and emails it if issues found
(or always, so you get a clean 'all OK' heartbeat every 30 min).
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

POOL_FILE     = Path("/tmp/dipu_equity_pool.json")
LOG_DIR       = Path("/tmp")
MONITOR_LOG   = Path("/tmp/dipu_monitor.log")
SLOT_NAMES    = {0: "dipu-live", 1: "dipu-live-1", 2: "dipu-live-2", 3: "dipu-live-3"}
WINDOW_SECS   = 30 * 60           # look back 30 min for log issues
STALE_SECS    = 120               # agent log silent for 2 min = stale
ERROR_BURST   = 3                 # ≥3 LOOP_ERRORs in window = issue
DD_WARN_PCT   = 8.0               # warn before 10% halt threshold
SLOT_TTL      = 90                # pool heartbeat TTL (seconds)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_ts(ts_str: str) -> float:
    """ISO8601 → unix timestamp."""
    try:
        return datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return 0.0


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
    log_path = LOG_DIR / f"dipu_{name}.log"
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
        name   = SLOT_NAMES[slot_id]
        s      = slots.get(str(slot_id))
        log    = _scan_agent_log(name, since)

        entry = {
            "name":        name,
            "symbol":      s["symbol"]      if s else "—",
            "open_usdt":   s["open_usdt"]   if s else 0.0,
            "daily_pnl":   s["daily_pnl"]   if s else 0.0,
            "loop_errors": log["loop_errors"],
            "halted":      log["halted"],
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

    return {
        "ts":      _now_utc(),
        "issues":  issues,
        "slots":   slot_report,
        "healthy": len(issues) == 0,
    }


def write_report(result: dict) -> str:
    """Append structured report to MONITOR_LOG, return text summary."""
    lines = [
        "=" * 64,
        f"  DIPU AGENT HEALTH CHECK  —  {result['ts']}",
        "=" * 64,
    ]

    if result["healthy"]:
        lines.append("  STATUS: ALL OK — no issues detected")
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

    if result["issues"]:
        lines.append("")
        lines.append("  ISSUES")
        lines.append("  " + "-" * 60)
        for iss in result["issues"]:
            lines.append(f"  [{iss['severity']:8s}] Slot {iss['slot']} {iss['name']}: {iss['msg']}")

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

        status_color = "#00e676" if result["healthy"] else "#ff1744"
        status_text  = "ALL OK" if result["healthy"] else f"{len(result['issues'])} ISSUE(S)"

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
                f'<td style="padding:5px 10px;color:#aaa">{sid}</td>'
                f'<td style="padding:5px 10px;color:#00e5ff">{s["symbol"]}</td>'
                f'<td style="padding:5px 10px;color:{pnl_color}">{pnl_str}</td>'
                f'<td style="padding:5px 10px">{tag_html}</td>'
                f'</tr>'
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
                    f'<span style="color:#ddd">{iss["name"]}: {iss["msg"]}</span>'
                    f'</li>'
                )
            issue_html = (
                f'<p style="color:#888;margin:16px 0 6px">Issues detected:</p>'
                f'<ul style="margin:0;padding-left:18px">{issue_items}</ul>'
            )

        html = _em._style_wrap(f"""
          <h2 style="color:{status_color};margin:0 0 4px">&#x1F916; Agent Health Check</h2>
          <p style="color:#555;font-size:12px;margin:0 0 16px">{result['ts']}</p>
          <p style="margin:0 0 14px">
            Status:&nbsp;<strong style="color:{status_color}">{status_text}</strong>
          </p>
          <table style="width:100%;border-collapse:collapse;margin-bottom:14px">
            <thead>
              <tr style="border-bottom:1px solid #1e1e1e">
                <th style="padding:5px 10px;text-align:left;color:#888">Slot</th>
                <th style="padding:5px 10px;text-align:left;color:#888">Symbol</th>
                <th style="padding:5px 10px;text-align:left;color:#888">P&amp;L</th>
                <th style="padding:5px 10px;text-align:left;color:#888">State</th>
              </tr>
            </thead>
            <tbody>{slot_rows}</tbody>
          </table>
          {issue_html}
          <p style="color:#444;font-size:11px;margin:16px 0 0">
            Log: /tmp/dipu_monitor.log &nbsp;|&nbsp; Check interval: 30 min
          </p>""")

        subject = (
            f"[dipu] Health OK — {result['ts']}"
            if result["healthy"]
            else f"[dipu] ⚠ {len(result['issues'])} issue(s) detected — {result['ts']}"
        )
        return _em._send(subject, html)
    except Exception as e:
        print(f"[monitor] email failed: {e}")
        return False


def check_and_report() -> dict:
    """Run check, write log, send email. Called by the manager scheduler."""
    result = run_check()
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
