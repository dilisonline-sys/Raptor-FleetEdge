"""Email notification module for dipu trading alerts and P&L reports."""
import json
import os
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

EMAIL_CONFIG_FILE = "/tmp/dipu_email_config.json"

_DEFAULT_CONFIG: dict = {
    "enabled": False,
    "recipient": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_password": "",
    "notifications": {
        "order_fills":   True,
        "pnl_report":    True,
        "coin_traded":   True,
    },
}


def load_config() -> dict:
    try:
        if os.path.exists(EMAIL_CONFIG_FILE):
            with open(EMAIL_CONFIG_FILE) as f:
                cfg = json.load(f)
            merged = {**_DEFAULT_CONFIG, **cfg}
            merged["notifications"] = {
                **_DEFAULT_CONFIG["notifications"],
                **cfg.get("notifications", {}),
            }
            return merged
    except Exception:
        pass
    return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with open(EMAIL_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def _send(subject: str, body_html: str) -> tuple[bool, str]:
    """Send an email. Returns (True, '') on success or (False, reason) on failure."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return False, "notifications disabled"
    if not cfg.get("recipient") or not cfg.get("smtp_user") or not cfg.get("smtp_password"):
        return False, "incomplete SMTP config — recipient, smtp_user, or smtp_password missing"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["smtp_user"]
        msg["To"]      = cfg["recipient"]
        msg.attach(MIMEText(body_html, "html"))
        port = int(cfg["smtp_port"])
        ctx  = ssl.create_default_context()
        if port == 465:
            # Direct SSL (SMTP_SSL) — works where STARTTLS on 587 is blocked
            with smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx, timeout=20) as s:
                s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.sendmail(cfg["smtp_user"], cfg["recipient"], msg.as_string())
        else:
            # STARTTLS on port 587 (or custom port)
            with smtplib.SMTP(cfg["smtp_host"], port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(cfg["smtp_user"], cfg["smtp_password"])
                s.sendmail(cfg["smtp_user"], cfg["recipient"], msg.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError:
        reason = "authentication failed — check App Password"
        print(f"[email] send failed: {reason}")
        return False, reason
    except smtplib.SMTPRecipientsRefused as e:
        reason = f"recipient refused: {e}"
        print(f"[email] send failed: {reason}")
        return False, reason
    except smtplib.SMTPResponseException as e:
        # Surface the full SMTP server message (e.g. daily limit exceeded)
        reason = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        reason = reason.replace("\n", " ").strip()
        print(f"[email] send failed: {reason}")
        return False, reason
    except Exception as e:
        reason = str(e)
        print(f"[email] send failed: {reason}")
        return False, reason


def _style_wrap(inner: str) -> str:
    return f"""<div style="font-family:'Courier New',monospace;background:#0a0a0a;color:#fff;
padding:24px;border-radius:10px;max-width:560px;margin:0 auto">{inner}</div>"""


def _row(label: str, value: str, color: str = "#fff") -> str:
    return (f'<tr><td style="padding:5px 14px 5px 0;color:#fff">{label}</td>'
            f'<td style="padding:5px 0;color:{color};font-weight:bold">{value}</td></tr>')


def notify_fill(agent_name: str, symbol: str, side: str, qty: float, price: float) -> bool:
    cfg = load_config()
    if not cfg.get("notifications", {}).get("order_fills"):
        return False
    side_color = "#00e676" if side == "BUY" else "#ff1744"
    value = qty * price
    html = _style_wrap(f"""
      <h2 style="color:#00e5ff;margin:0 0 14px">&#x1F4CB; Order Fill</h2>
      <table style="border-collapse:collapse">
        {_row("Agent",  agent_name)}
        {_row("Symbol", symbol)}
        {_row("Side",   side,   side_color)}
        {_row("Qty",    f"{qty:.6f}")}
        {_row("Price",  f"${price:.5f}")}
        {_row("Value",  f"${value:.2f}")}
        {_row("Time",   _ts(), "#777")}
      </table>""")
    ok, _ = _send(f"[dipu] {agent_name}: {side} {symbol} {qty:.4f} @ ${price:.5f}", html)
    return ok


def notify_coin_traded(agent_name: str, symbol: str, regime: str, signal: str) -> bool:
    cfg = load_config()
    if not cfg.get("notifications", {}).get("coin_traded"):
        return False
    regime_color = {"TRENDING": "#00e676", "RANGING": "#ffd600", "VOLATILE": "#ff1744"}.get(regime, "#aaa")
    html = _style_wrap(f"""
      <h2 style="color:#00e5ff;margin:0 0 14px">&#x1F4B9; Active Coin Update</h2>
      <table style="border-collapse:collapse">
        {_row("Agent",  agent_name)}
        {_row("Symbol", symbol,  "#00e5ff")}
        {_row("Regime", regime,  regime_color)}
        {_row("Signal", signal)}
        {_row("Time",   _ts(), "#777")}
      </table>""")
    ok, _ = _send(f"[dipu] {agent_name}: now trading {symbol} ({regime})", html)
    return ok


def send_pnl_report(slots_data: dict) -> bool:
    cfg = load_config()
    if not cfg.get("notifications", {}).get("pnl_report"):
        return False
    rows = ""
    total_pnl = 0.0
    for slot_id in sorted(slots_data.keys()):
        s = slots_data[slot_id]
        if not s:
            rows += (f'<tr><td style="padding:6px 12px;color:#fff">Slot {slot_id}</td>'
                     f'<td colspan="3" style="padding:6px 12px;color:#333">empty</td></tr>')
            continue
        pnl = s.get("daily_pnl", 0)
        total_pnl += pnl
        pnl_color = "#00e676" if pnl >= 0 else "#ff1744"
        pnl_str   = f"{'+'if pnl>=0 else ''}${pnl:.2f}"
        rows += (f'<tr>'
                 f'<td style="padding:6px 12px;color:#fff">Slot {slot_id}</td>'
                 f'<td style="padding:6px 12px;color:#00e5ff">{s.get("symbol","—")}</td>'
                 f'<td style="padding:6px 12px;color:#fff">${s.get("open_usdt",0):.2f}</td>'
                 f'<td style="padding:6px 12px;color:{pnl_color};font-weight:bold">{pnl_str}</td>'
                 f'</tr>')
    total_color = "#00e676" if total_pnl >= 0 else "#ff1744"
    total_str   = f"{'+'if total_pnl>=0 else ''}${total_pnl:.2f}"
    html = _style_wrap(f"""
      <h2 style="color:#00e5ff;margin:0 0 4px">&#x1F4CA; 4-Hour P&amp;L Report</h2>
      <p style="color:#fff;font-size:12px;margin:0 0 18px">{_ts()}</p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
        <thead>
          <tr style="border-bottom:1px solid #1e1e1e">
            <th style="padding:6px 12px;text-align:left;color:#fff">Slot</th>
            <th style="padding:6px 12px;text-align:left;color:#fff">Coin</th>
            <th style="padding:6px 12px;text-align:left;color:#fff">Open ($)</th>
            <th style="padding:6px 12px;text-align:left;color:#fff">Daily P&amp;L</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <div style="border-top:1px solid #1e1e1e;padding-top:12px">
        <span style="color:#fff">Total Daily P&amp;L:&nbsp;</span>
        <span style="color:{total_color};font-size:1.15em;font-weight:bold">{total_str}</span>
      </div>""")
    ok, _ = _send(f"[dipu] 4h P&L Report — Total: {total_str}", html)
    return ok


def send_test_email(recipient: str) -> tuple[bool, str]:
    """Returns (True, '') on success or (False, reason) on failure."""
    html = _style_wrap(f"""
      <h2 style="color:#00e5ff;margin:0 0 14px">&#x2705; dipu email test</h2>
      <p style="color:#fff;margin:0 0 12px">Email notifications are working correctly.</p>
      <p style="color:#fff;font-size:12px">{_ts()}</p>""")
    cfg = load_config()
    original = cfg.get("recipient", "")
    cfg["recipient"] = recipient
    save_config(cfg)
    ok, reason = _send("[dipu] Test email — notifications active", html)
    cfg["recipient"] = original
    save_config(cfg)
    return ok, reason
