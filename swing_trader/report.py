from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from swing_trader.config import CAPITAL_INITIAL, REPORTS_DIR


def build_html(state: dict) -> str:
    capital     = state.get("capital", CAPITAL_INITIAL)
    open_pos    = state.get("open_positions", [])
    closed      = state.get("closed_trades", [])
    total_pnl   = sum(t.get("pnl", 0) for t in closed)
    wins        = sum(1 for t in closed if t.get("pnl", 0) > 0)
    losses      = len(closed) - wins
    win_rate    = round(wins / len(closed) * 100, 1) if closed else 0.0
    pnl_pct     = round(total_pnl / CAPITAL_INITIAL * 100, 2)
    generated   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    def _row_color(pnl: float) -> str:
        return "#1a4a1a" if pnl > 0 else "#4a1a1a"

    open_rows = ""
    for p in open_pos:
        unr = p.get("unrealised_pnl", 0)
        color = "#1a3a4a" if unr >= 0 else "#4a1a1a"
        open_rows += f"""
        <tr style="background:{color}">
          <td>{p['ticker']}</td>
          <td>₹{p['entry']:,.2f}</td>
          <td>₹{p['stop_loss']:,.2f}</td>
          <td>₹{p['target']:,.2f}</td>
          <td>{p['qty']}</td>
          <td>₹{p['capital_used']:,.2f}</td>
          <td>{p['rr']}</td>
          <td>₹{unr:+,.2f}</td>
          <td>{p['opened_at'][:10]}</td>
        </tr>"""

    closed_rows = ""
    for t in reversed(closed):
        pnl = t.get("pnl", 0)
        closed_rows += f"""
        <tr style="background:{_row_color(pnl)}">
          <td>{t['ticker']}</td>
          <td>₹{t['entry']:,.2f}</td>
          <td>₹{t.get('exit_price', 0):,.2f}</td>
          <td>{t['qty']}</td>
          <td>₹{pnl:+,.2f}</td>
          <td>{t.get('exit_reason','—')}</td>
          <td>{t['opened_at'][:10]}</td>
          <td>{t.get('closed_at','')[:10]}</td>
        </tr>"""

    th = "style='padding:8px 12px;text-align:left;border-bottom:1px solid #333;color:#aaa'"
    td_style = "padding:8px 12px;border-bottom:1px solid #222"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swing Trader — Weekly P&L Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0d0d; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; padding: 24px; }}
  h1 {{ color: #f0c040; font-size: 1.6rem; margin-bottom: 4px; }}
  .sub {{ color: #666; font-size: 0.85rem; margin-bottom: 28px; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
  .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 16px 24px; min-width: 140px; }}
  .card-label {{ color: #888; font-size: 0.75rem; text-transform: uppercase; letter-spacing: .05em; }}
  .card-value {{ font-size: 1.4rem; font-weight: 700; margin-top: 4px; }}
  .green {{ color: #4caf50; }} .red {{ color: #f44336; }} .yellow {{ color: #f0c040; }}
  h2 {{ color: #ccc; font-size: 1rem; text-transform: uppercase; letter-spacing:.08em; margin: 28px 0 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
  th {{ {th} }}
  td {{ {td_style} }}
  tr:last-child td {{ border-bottom: none; }}
  .disclaimer {{ margin-top: 36px; color: #555; font-size: 0.75rem; border-top: 1px solid #222; padding-top: 12px; }}
</style>
</head>
<body>
<h1>Swing Trader — Weekly P&L Report</h1>
<div class="sub">Generated {generated} &nbsp;|&nbsp; Nifty 100 Watchlist &nbsp;|&nbsp; Capital ₹{CAPITAL_INITIAL:,.0f}</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Available Capital</div>
    <div class="card-value yellow">₹{capital:,.0f}</div>
  </div>
  <div class="card">
    <div class="card-label">Total P&L</div>
    <div class="card-value {'green' if total_pnl >= 0 else 'red'}">₹{total_pnl:+,.0f} ({pnl_pct:+.2f}%)</div>
  </div>
  <div class="card">
    <div class="card-label">Trades Closed</div>
    <div class="card-value">{len(closed)}</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value {'green' if win_rate >= 50 else 'red'}">{win_rate}%</div>
  </div>
  <div class="card">
    <div class="card-label">Wins / Losses</div>
    <div class="card-value"><span class="green">{wins}W</span> / <span class="red">{losses}L</span></div>
  </div>
  <div class="card">
    <div class="card-label">Open Positions</div>
    <div class="card-value yellow">{len(open_pos)}</div>
  </div>
</div>

<h2>Open Positions ({len(open_pos)})</h2>
<table>
  <thead><tr>
    <th>Ticker</th><th>Entry</th><th>Stop</th><th>Target</th>
    <th>Qty</th><th>Capital</th><th>RR</th><th>Unrealised</th><th>Opened</th>
  </tr></thead>
  <tbody>{open_rows or "<tr><td colspan='9' style='color:#555;padding:16px'>No open positions</td></tr>"}</tbody>
</table>

<h2>Closed Trades ({len(closed)})</h2>
<table>
  <thead><tr>
    <th>Ticker</th><th>Entry</th><th>Exit</th><th>Qty</th>
    <th>P&L</th><th>Reason</th><th>Opened</th><th>Closed</th>
  </tr></thead>
  <tbody>{closed_rows or "<tr><td colspan='8' style='color:#555;padding:16px'>No closed trades yet</td></tr>"}</tbody>
</table>

<div class="disclaimer">
  This is an automated paper trading simulation for educational purposes only.
  Not financial advice. Trade at your own risk.
</div>
</body>
</html>"""


def save_report(html: str, filename: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / filename
    out.write_text(html, encoding="utf-8")
    return out
