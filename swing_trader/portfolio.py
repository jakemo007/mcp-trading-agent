import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from swing_trader.config import CAPITAL_INITIAL, PORTFOLIO_FILE


def _default() -> dict:
    return {
        "capital": CAPITAL_INITIAL,
        "open_positions": [],
        "closed_trades": [],
    }


def load() -> dict:
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not PORTFOLIO_FILE.exists():
        state = _default()
        _write(state)
        return state
    return json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))


def save(state: dict) -> None:
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write(state)


def _write(state: dict) -> None:
    tmp = PORTFOLIO_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PORTFOLIO_FILE)


def open_position(
    state: dict,
    ticker: str,
    entry: float,
    stop_loss: float,
    target: float,
    qty: int,
    capital_used: float,
    rr: float,
) -> dict:
    position = {
        "id": str(uuid.uuid4())[:8],
        "ticker": ticker,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "qty": qty,
        "capital_used": capital_used,
        "rr": round(rr, 2),
        "status": "OPEN",
        "opened_at": datetime.utcnow().isoformat(),
        "unrealised_pnl": 0.0,
    }
    state["capital"] = round(state["capital"] - capital_used, 2)
    state["open_positions"].append(position)
    return position


def close_position(
    state: dict,
    position_id: str,
    exit_price: float,
    exit_reason: str,
) -> dict | None:
    for i, pos in enumerate(state["open_positions"]):
        if pos["id"] == position_id:
            pnl = round((exit_price - pos["entry"]) * pos["qty"], 2)
            proceeds = round(pos["capital_used"] + pnl, 2)
            closed = {
                **pos,
                "status": "CLOSED",
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "pnl": pnl,
                "closed_at": datetime.utcnow().isoformat(),
            }
            state["open_positions"].pop(i)
            state["closed_trades"].append(closed)
            state["capital"] = round(state["capital"] + proceeds, 2)
            return closed
    return None


def update_unrealised(state: dict, ticker: str, current_price: float) -> None:
    for pos in state["open_positions"]:
        if pos["ticker"] == ticker:
            pos["unrealised_pnl"] = round(
                (current_price - pos["entry"]) * pos["qty"], 2
            )
