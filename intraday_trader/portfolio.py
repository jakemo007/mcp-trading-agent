"""Money: position sizing, charges and the persistent paper portfolio.

No network. Shapes are in models.py. Config values are read at call time so a
change in config.py (or a test patch) takes effect immediately.
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from intraday_trader import config

_IST = timezone(timedelta(hours=5, minutes=30))
_EPS = 1e-9


def _r2(value: float) -> float:
    """Round to paise; `+ 0.0` turns -0.0 into 0.0 so JSON never shows "-0.0"."""
    return round(float(value), 2) + 0.0


def _now_iso() -> str:
    return datetime.now(_IST).isoformat(timespec="seconds")


def _day_key(day: Any) -> str:
    """Accept a date, a datetime or an ISO string and return "YYYY-MM-DD"."""
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    return str(day)[:10]


# ── Sizing and P&L ────────────────────────────────────────────────────────────

def position_size(entry: float, stop: float, free_cash: float, capital: float) -> Tuple[int, Optional[str]]:
    """How many shares to trade. Returns (qty, None) or (0, reason).

    margin   = min(MAX_MARGIN_PER_TRADE, free_cash)          -> reason "no free margin" if <= 0
    qty      = floor(margin * LEVERAGE / entry)
    risk cap = floor(capital * MAX_RISK_PCT_PER_TRADE / abs(entry - stop)); qty = min(qty, risk cap)
    qty < 1  -> reason "share price above the per-trade limit", or "no free margin"
                when free_cash (not the per-trade cap) was the binding limit, or
                "stop too wide for the per-trade risk limit" when the risk cap
                is what brought the quantity to zero.
    Invalid prices (entry <= 0 or entry == stop) -> (0, "invalid prices").
    """
    try:
        entry = float(entry)
        stop = float(stop)
        free_cash = float(free_cash)
        capital = float(capital)
    except (TypeError, ValueError):
        return 0, "invalid prices"
    if not (math.isfinite(entry) and math.isfinite(stop)) or entry <= 0 or entry == stop:
        return 0, "invalid prices"

    margin = min(config.MAX_MARGIN_PER_TRADE, free_cash)
    if not math.isfinite(margin) or margin <= 0:
        return 0, "no free margin"

    by_margin = int(math.floor(margin * config.LEVERAGE / entry + _EPS))
    by_risk = int(math.floor(capital * config.MAX_RISK_PCT_PER_TRADE / abs(entry - stop) + _EPS))
    qty = min(by_margin, by_risk)
    if qty >= 1:
        return qty, None
    if by_margin < 1:
        if free_cash < config.MAX_MARGIN_PER_TRADE:
            return 0, "no free margin"
        return 0, "share price above the per-trade limit"
    return 0, "stop too wide for the per-trade risk limit"


def trade_pnl(direction: str, entry: float, exit_price: float, qty: int) -> Tuple[float, float, float]:
    """(gross_pnl, charges, net_pnl) for one completed trade.

    gross = (exit - entry) * qty for LONG, (entry - exit) * qty for SHORT.
    charges = CHARGE_PER_TRADE (flat, once per trade). net = gross - charges.
    """
    move = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
    gross = _r2(move * qty)
    charges = _r2(config.CHARGE_PER_TRADE)
    return gross, charges, _r2(gross - charges)


# ── Persistent state ──────────────────────────────────────────────────────────

def new_state() -> dict:
    """A fresh PortfolioState."""
    return {
        "initial_capital": float(config.INITIAL_CAPITAL),
        "capital": float(config.INITIAL_CAPITAL),
        "days": [],
        "trades": [],
        "updated_at": _now_iso(),
    }


def load_state(path: Path = config.PORTFOLIO_FILE) -> dict:
    """Read the portfolio JSON; a missing file yields new_state() (nothing is written)."""
    path = Path(path)
    if not path.exists():
        return new_state()
    state = json.loads(path.read_text(encoding="utf-8"))
    fresh = new_state()
    for key, value in fresh.items():
        state.setdefault(key, value)
    return state


def save_state(state: dict, path: Path = config.PORTFOLIO_FILE) -> None:
    """Write atomically (temp file then replace), UTF-8, indent 2, parent dirs created."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def start_capital_for(state: dict, day: date) -> float:
    """Capital available at the open of `day`: initial_capital plus the net P&L of
    every recorded day strictly before `day`."""
    key = _day_key(day)
    capital = float(state.get("initial_capital", config.INITIAL_CAPITAL))
    for entry in state.get("days", []):
        if str(entry.get("date", "")) < key:
            capital += float(entry.get("net_pnl", 0.0))
    return _r2(capital)


def record_day(state: dict, result: dict) -> dict:
    """Store a DayResult in the state and return the state.

    Idempotent per date: an existing entry (and its trades) for result["date"]
    is replaced. Trades are stored without their "chart" key. Afterwards days
    are sorted by date and the chain is recomputed: each day's start_capital =
    previous end_capital (initial for the first), end_capital = start + net_pnl,
    state["capital"] = last end_capital, updated_at = now (ISO, IST).
    """
    key = _day_key(result["date"])
    state.setdefault("initial_capital", float(config.INITIAL_CAPITAL))
    days: List[dict] = [d for d in state.get("days", []) if str(d.get("date")) != key]
    trades: List[dict] = [t for t in state.get("trades", []) if str(t.get("date")) != key]

    days.append({
        "date": key,
        "start_capital": 0.0,   # filled in by the chain below
        "end_capital": 0.0,
        "gross_pnl": _r2(result.get("gross_pnl", 0.0)),
        "charges": _r2(result.get("charges", 0.0)),
        "net_pnl": _r2(result.get("net_pnl", 0.0)),
        "n_trades": int(result.get("n_trades", 0)),
        "wins": int(result.get("wins", 0)),
        "losses": int(result.get("losses", 0)),
        "partial": bool(result.get("partial", False)),
    })
    for trade in result.get("trades", []):
        stored = {k: v for k, v in trade.items() if k != "chart"}
        stored.setdefault("date", key)
        trades.append(stored)

    days.sort(key=lambda d: str(d["date"]))
    trades.sort(key=lambda t: (str(t.get("date", "")), str(t.get("exit_time", ""))))

    capital = float(state["initial_capital"])
    for entry in days:
        entry["start_capital"] = _r2(capital)
        capital = capital + float(entry["net_pnl"])
        entry["end_capital"] = _r2(capital)

    state["days"] = days
    state["trades"] = trades
    state["capital"] = _r2(capital)
    state["updated_at"] = _now_iso()
    return state


def summarize(state: dict) -> dict:
    """Totals for reports (see the field list in the module contract)."""
    initial = float(state.get("initial_capital", config.INITIAL_CAPITAL))
    days: List[dict] = sorted(state.get("days", []), key=lambda d: str(d.get("date", "")))
    trades: List[dict] = list(state.get("trades", []))

    n_trades = len(trades)
    nets = [float(t.get("net_pnl", 0.0)) for t in trades]
    win_nets = [n for n in nets if n > 0]
    loss_nets = [n for n in nets if n <= 0]
    wins, losses = len(win_nets), len(loss_nets)

    gross = sum(float(d.get("gross_pnl", 0.0)) for d in days)
    charges = sum(float(d.get("charges", 0.0)) for d in days)
    net = sum(float(d.get("net_pnl", 0.0)) for d in days)

    r_values = [float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]
    loss_total = abs(sum(loss_nets))
    profit_factor = round(sum(win_nets) / loss_total, 2) if loss_total > 0 else None

    peak = initial
    max_dd_inr = 0.0
    max_dd_pct = 0.0
    equity = []
    for entry in days:
        cap = float(entry.get("end_capital", initial))
        equity.append({"date": entry["date"], "capital": _r2(cap)})
        peak = max(peak, cap)
        drawdown = peak - cap
        if drawdown > max_dd_inr:
            max_dd_inr = drawdown
        if peak > 0 and drawdown / peak * 100.0 > max_dd_pct:
            max_dd_pct = drawdown / peak * 100.0

    best = max(days, key=lambda d: float(d.get("net_pnl", 0.0))) if days else None
    worst = min(days, key=lambda d: float(d.get("net_pnl", 0.0))) if days else None

    return {
        "sessions": len(days),
        "n_trades": n_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / n_trades * 100.0, 1) if n_trades else 0.0,
        "gross_pnl": _r2(gross),
        "charges": _r2(charges),
        "net_pnl": _r2(net),
        "net_pnl_pct": round(net / initial * 100.0, 2) + 0.0 if initial else 0.0,
        "capital": _r2(state.get("capital", initial + net)),
        "avg_win": _r2(sum(win_nets) / wins) if wins else 0.0,
        "avg_loss": _r2(sum(loss_nets) / losses) if losses else 0.0,
        "profit_factor": profit_factor,
        "expectancy_inr": _r2(sum(nets) / n_trades) if n_trades else 0.0,
        "avg_r": round(sum(r_values) / len(r_values), 2) + 0.0 if r_values else 0.0,
        "max_drawdown_inr": _r2(max_dd_inr),
        "max_drawdown_pct": round(max_dd_pct, 2) + 0.0,
        "best_day": {"date": best["date"], "net_pnl": _r2(best.get("net_pnl", 0.0))} if best else None,
        "worst_day": {"date": worst["date"], "net_pnl": _r2(worst.get("net_pnl", 0.0))} if worst else None,
        "equity": equity,
    }
