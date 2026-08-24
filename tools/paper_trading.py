"""
tools/paper_trading.py -- Paper trading simulation layer.

Manages:
  - Portfolio state (data/paper_trading.json)
  - Intraday breakout scanning (Nifty 50 universe)
  - Position sizing (0.5% risk rule)
  - SL / target / EOD exit management
  - 3-month backtest with INR accounting

All file paths are resolved relative to project DATA_DIR (mcp-trading-agent/data/).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from tools.market_data import run_intraday_backtest

logger = logging.getLogger(__name__)

# ── Path setup (mirrors persistence.py pattern) ──────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR      = _PROJECT_ROOT / "data"
PT_STATE_FILE = DATA_DIR / "paper_trading.json"

# ── Constants ─────────────────────────────────────────────────────────────────
INITIAL_CAPITAL  = 100_000.0
RISK_PCT         = 0.005       # 0.5% risk per trade
CHARGE_PER_TRADE = 50.0        # INR, charged separately on entry AND exit
MIN_RR           = 2.0

# yfinance interval validation
_VALID_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}

# ── Nifty 50 universe: 25 most liquid constituents (Yahoo Finance .NS format) ─
NIFTY50_SCAN_UNIVERSE: list[str] = [
    "RELIANCE.NS",
    "TCS.NS",
    "INFY.NS",
    "HDFCBANK.NS",
    "ICICIBANK.NS",
    "HINDUNILVR.NS",
    "SBIN.NS",
    "BHARTIARTL.NS",
    "ITC.NS",
    "KOTAKBANK.NS",
    "LT.NS",
    "AXISBANK.NS",
    "ASIANPAINT.NS",
    "BAJFINANCE.NS",
    "MARUTI.NS",
    "WIPRO.NS",
    "ULTRACEMCO.NS",
    "ONGC.NS",
    "TITAN.NS",
    "NTPC.NS",
    "POWERGRID.NS",
    "SUNPHARMA.NS",
    "TECHM.NS",
    "HCLTECH.NS",
    "MM.NS",  # Mahindra & Mahindra — yfinance uses MM.NS not M&M.NS
]


# ── IST time helpers ──────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    """Return current time as a naive datetime in IST (+05:30)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(timezone.utc).astimezone(ist).replace(tzinfo=None)


def _is_market_open() -> bool:
    """Return True when IST time is between 09:15 and 15:00 (exclusive)."""
    now = _now_ist()
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


def _is_eod() -> bool:
    """Return True when IST time is at or past 15:00."""
    return _now_ist().hour >= 15


# ── State I/O ─────────────────────────────────────────────────────────────────

def _default_state() -> dict[str, Any]:
    return {
        "initialized_at":    _now_ist().isoformat(),
        "initial_capital":   INITIAL_CAPITAL,
        "current_capital":   INITIAL_CAPITAL,
        "risk_per_trade_pct": RISK_PCT * 100,
        "charge_per_trade":  CHARGE_PER_TRADE,
        "min_rr":            MIN_RR,
        "open_positions":    [],
        "closed_trades":     [],
        "session_history":   [],
    }


def _load_state() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PT_STATE_FILE.exists():
        state = _default_state()
        PT_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info("Initialised fresh paper_trading.json at %s", PT_STATE_FILE)
        return state
    return json.loads(PT_STATE_FILE.read_text(encoding="utf-8"))


def _save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PT_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Position sizing helper ────────────────────────────────────────────────────

def _calc_position_size(
    current_capital: float,
    entry: float,
    stop_loss: float,
) -> dict[str, Any]:
    """
    Calculate quantity using the 0.5% fixed-risk rule.
    Returns feasible=False when entry_value would exceed current_capital.
    """
    risk_capital  = current_capital * RISK_PCT
    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit <= 0:
        return {"feasible": False, "reason": "entry price equals stop_loss"}
    quantity    = max(1, int(risk_capital / risk_per_unit))
    entry_value = round(quantity * entry, 2)
    feasible    = entry_value <= current_capital
    return {
        "feasible":     feasible,
        "quantity":     quantity,
        "entry_value":  entry_value,
        "risk_capital": round(risk_capital, 2),
        "reason":       None if feasible else (
            f"entry_value ₹{entry_value:,.0f} exceeds available capital ₹{current_capital:,.0f}"
        ),
    }


# ── Intraday setup scanner (private) ─────────────────────────────────────────

def _fetch_today_candles(ticker: str, interval: str, days: int) -> list[dict]:
    """
    Download intraday OHLCV for *ticker* and return only today's candles.
    Returns empty list on any error.
    """
    interval = interval.lower().strip()
    if interval not in _VALID_INTERVALS:
        interval = "15m"
    try:
        df: pd.DataFrame = yf.download(
            ticker,
            period=f"{days}d",
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.warning("yfinance download failed for %s: %s", ticker, exc)
        return []

    if df.empty:
        return []

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index)

    # Isolate today's session (or most recent trading day as fallback)
    ist = timezone(timedelta(hours=5, minutes=30))
    today_str = datetime.now(ist).strftime("%Y-%m-%d")
    today_df = df[df.index.strftime("%Y-%m-%d") == today_str]
    if today_df.empty:
        most_recent = df.index[-1].strftime("%Y-%m-%d")
        today_df = df[df.index.strftime("%Y-%m-%d") == most_recent]

    candles = []
    for dt, row in today_df.iterrows():
        candles.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
            "open":  round(float(row["Open"]),  2),
            "high":  round(float(row["High"]),  2),
            "low":   round(float(row["Low"]),   2),
            "close": round(float(row["Close"]), 2),
        })
    return candles


def _scan_today_for_setups(
    ticker: str,
    today_candles: list[dict],
    min_rr: float,
) -> list[dict[str, Any]]:
    """
    Apply BSL/SSL sweep + FVG detection on today's candles only.
    Replicates the core logic of run_intraday_backtest (HALF=5, ATR-adaptive SL).
    Returns signals where rr >= min_rr (no walk-forward simulation — live signals).
    """
    if len(today_candles) < 12:
        return []

    highs  = np.array([c["high"]  for c in today_candles], dtype=float)
    lows   = np.array([c["low"]   for c in today_candles], dtype=float)
    closes = np.array([c["close"] for c in today_candles], dtype=float)
    times  = [c["datetime"] for c in today_candles]
    n      = len(today_candles)

    HALF    = 5
    SL_BUFF = 0.0015
    ATR_PERIOD = 14

    # Wilder ATR
    _hs = pd.Series(highs)
    _ls = pd.Series(lows)
    _cs = pd.Series(closes)
    _pc = _cs.shift(1).fillna(_cs)
    _tr = pd.concat([_hs - _ls, (_hs - _pc).abs(), (_ls - _pc).abs()], axis=1).max(axis=1)
    atr_arr = _tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean().values

    # Swing detection
    swing_high_idx: list[int] = []
    swing_low_idx:  list[int] = []
    for i in range(HALF, n - HALF):
        if highs[i] == float(np.max(highs[i - HALF: i + HALF + 1])):
            swing_high_idx.append(i)
        if lows[i] == float(np.min(lows[i - HALF: i + HALF + 1])):
            swing_low_idx.append(i)

    signals: list[dict[str, Any]] = []

    for i in range(HALF + 1, n - HALF - 3):

        # ── SHORT: BSL Sweep → Bearish FVG ──────────────────────────
        prior_sh = [idx for idx in swing_high_idx if idx < i - 1]
        if prior_sh:
            sh_level = highs[prior_sh[-1]]
            if highs[i] > sh_level and closes[i] < sh_level:
                for j in range(i, min(i + 5, n - 1)):
                    if j >= 1 and (j + 1) < n and lows[j - 1] > highs[j + 1]:
                        fvg_top   = lows[j - 1]
                        fvg_bot   = highs[j + 1]
                        fvg_entry = round((fvg_top + fvg_bot) / 2, 2)
                        _sl_buf   = max(float(highs[i]) * SL_BUFF, float(atr_arr[i]) * 0.25)
                        sl_price  = round(float(highs[i]) + _sl_buf, 2)
                        ssl_cands = [lows[idx] for idx in swing_low_idx if idx < i and lows[idx] < fvg_entry]
                        if ssl_cands:
                            tgt   = round(max(ssl_cands), 2)
                            risk  = sl_price - fvg_entry
                            rwd   = fvg_entry - tgt
                            if risk > 0:
                                rr = round(rwd / risk, 2)
                                if rr >= min_rr:
                                    signals.append({
                                        "ticker":      ticker,
                                        "direction":   "SHORT",
                                        "setup_type":  "BSL_SWEEP_FVG",
                                        "signal_time": times[i],
                                        "entry":       fvg_entry,
                                        "stop_loss":   sl_price,
                                        "target":      tgt,
                                        "rr":          rr,
                                        "fvg_zone":    [round(fvg_top, 2), round(fvg_bot, 2)],
                                        "swept_level": round(sh_level, 2),
                                        "atr":         round(float(atr_arr[i]), 2),
                                    })
                        break

        # ── LONG: SSL Sweep → Bullish FVG ───────────────────────────
        prior_sl = [idx for idx in swing_low_idx if idx < i - 1]
        if prior_sl:
            sl_level = lows[prior_sl[-1]]
            if lows[i] < sl_level and closes[i] > sl_level:
                for j in range(i, min(i + 5, n - 1)):
                    if j >= 1 and (j + 1) < n and highs[j - 1] < lows[j + 1]:
                        fvg_bot   = highs[j - 1]
                        fvg_top   = lows[j + 1]
                        fvg_entry = round((fvg_top + fvg_bot) / 2, 2)
                        _sl_buf   = max(float(lows[i]) * SL_BUFF, float(atr_arr[i]) * 0.25)
                        sl_price  = round(float(lows[i]) - _sl_buf, 2)
                        bsl_cands = [highs[idx] for idx in swing_high_idx if idx < i and highs[idx] > fvg_entry]
                        if bsl_cands:
                            tgt  = round(min(bsl_cands), 2)
                            risk = fvg_entry - sl_price
                            rwd  = tgt - fvg_entry
                            if risk > 0:
                                rr = round(rwd / risk, 2)
                                if rr >= min_rr:
                                    signals.append({
                                        "ticker":      ticker,
                                        "direction":   "LONG",
                                        "setup_type":  "SSL_SWEEP_FVG",
                                        "signal_time": times[i],
                                        "entry":       fvg_entry,
                                        "stop_loss":   sl_price,
                                        "target":      tgt,
                                        "rr":          rr,
                                        "fvg_zone":    [round(fvg_bot, 2), round(fvg_top, 2)],
                                        "swept_level": round(sl_level, 2),
                                        "atr":         round(float(atr_arr[i]), 2),
                                    })
                        break

    return signals


# ── Public functions (each wrapped as one MCP tool) ───────────────────────────

def paper_trade_portfolio() -> dict[str, Any]:
    """
    Load (or initialise) the paper trading portfolio.
    Returns capital summary, open positions, closed trade stats, and session info.
    """
    state   = _load_state()
    capital = state["current_capital"]
    initial = state["initial_capital"]
    closed  = state["closed_trades"]
    open_p  = state["open_positions"]

    wins   = [t for t in closed if t.get("outcome") == "WIN"]
    losses = [t for t in closed if t.get("outcome") == "LOSS"]
    total_net_pnl = sum(t.get("pnl_net", 0.0) for t in closed)

    return {
        "status":           "success",
        "initialized_at":   state.get("initialized_at"),
        "initial_capital":  initial,
        "current_capital":  round(capital, 2),
        "total_pnl_net":    round(total_net_pnl, 2),
        "pnl_pct":          round((capital - initial) / initial * 100, 2),
        "open_positions":   len(open_p),
        "closed_trades":    len(closed),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate_pct":     round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "positions":        open_p,
        "recent_closed":    closed[-5:],
        "is_market_open":   _is_market_open(),
        "current_ist_time": _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
        "risk_per_trade_pct": state.get("risk_per_trade_pct", RISK_PCT * 100),
        "charge_per_trade":   state.get("charge_per_trade", CHARGE_PER_TRADE),
    }


def paper_trade_scan_breakouts(
    interval: str = "15m",
    days: int = 5,
) -> dict[str, Any]:
    """
    Scan the 25-stock Nifty 50 universe for intraday BSL/SSL + FVG signals
    on today's candles only. Returns signals with RR >= 2.0, sorted by RR.
    Returns market_closed status if called outside 09:15-15:00 IST.
    """
    if not _is_market_open():
        return {
            "status":  "market_closed",
            "message": (
                f"Market closed — IST time: {_now_ist().strftime('%H:%M')}. "
                "Trading hours: 09:15–15:00."
            ),
            "signals": [],
        }

    signals: list[dict[str, Any]] = []
    errors:  list[str] = []

    for ticker in NIFTY50_SCAN_UNIVERSE:
        try:
            today_candles = _fetch_today_candles(ticker, interval, days)
            if len(today_candles) < 12:
                continue
            ticker_signals = _scan_today_for_setups(ticker, today_candles, MIN_RR)
            signals.extend(ticker_signals)
        except Exception as exc:
            errors.append(f"{ticker}: {exc}")
            logger.warning("Scan error for %s: %s", ticker, exc)

    signals.sort(key=lambda s: s["rr"], reverse=True)
    return {
        "status":         "success",
        "scanned":        len(NIFTY50_SCAN_UNIVERSE),
        "signals_found":  len(signals),
        "min_rr_filter":  MIN_RR,
        "interval":       interval,
        "signals":        signals,
        "scan_errors":    errors,
        "scanned_at_ist": _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
    }


def paper_trade_open(
    ticker:    str,
    direction: str,
    entry:     float,
    stop_loss: float,
    target:    float,
    rr:        float,
) -> dict[str, Any]:
    """
    Open a new paper trade position.
    Validates RR >= 2.0, market is open, and capital is sufficient.
    Deducts ₹50 entry charge immediately from current_capital.
    """
    direction = direction.upper().strip()
    if direction not in ("LONG", "SHORT"):
        return {"status": "rejected", "reason": f"Invalid direction '{direction}'. Use LONG or SHORT."}

    if not _is_market_open():
        return {"status": "rejected", "reason": "Market is closed (outside 09:15-15:00 IST)."}

    if rr < MIN_RR:
        return {"status": "rejected", "reason": f"RR {rr:.2f} is below minimum {MIN_RR:.1f}."}

    state   = _load_state()
    capital = state["current_capital"]

    sizing = _calc_position_size(capital, entry, stop_loss)
    if not sizing["feasible"]:
        return {"status": "rejected", "reason": sizing["reason"]}

    # Deduct entry charge
    capital -= CHARGE_PER_TRADE
    if capital < 0:
        return {"status": "rejected", "reason": "Insufficient capital to cover entry charge."}

    position: dict[str, Any] = {
        "trade_id":      str(uuid.uuid4()),
        "ticker":        ticker.upper(),
        "direction":     direction,
        "entry":         round(float(entry), 2),
        "stop_loss":     round(float(stop_loss), 2),
        "target":        round(float(target), 2),
        "rr":            round(float(rr), 2),
        "quantity":      sizing["quantity"],
        "entry_value":   sizing["entry_value"],
        "risk_capital":  sizing["risk_capital"],
        "opened_at":     _now_ist().isoformat(),
        "entry_charges": CHARGE_PER_TRADE,
    }

    state["open_positions"].append(position)
    state["current_capital"] = round(capital, 2)
    _save_state(state)

    logger.info(
        "Paper trade opened: %s %s qty=%d entry=%.2f sl=%.2f tgt=%.2f rr=%.2f",
        position["ticker"], direction, sizing["quantity"], entry, stop_loss, target, rr,
    )

    return {
        "status":        "opened",
        "trade_id":      position["trade_id"],
        "ticker":        position["ticker"],
        "direction":     direction,
        "entry":         position["entry"],
        "stop_loss":     position["stop_loss"],
        "target":        position["target"],
        "rr":            position["rr"],
        "quantity":      position["quantity"],
        "entry_value":   position["entry_value"],
        "risk_capital":  position["risk_capital"],
        "entry_charges": CHARGE_PER_TRADE,
        "capital_after": round(capital, 2),
    }


def paper_trade_check_positions() -> dict[str, Any]:
    """
    Fetch latest prices for all open positions and close those that hit SL or target.
    At 15:00 IST (EOD) all remaining positions are force-closed at last price.
    Deducts ₹50 exit charge per closed position.
    """
    state = _load_state()
    if not state["open_positions"]:
        return {
            "status":          "success",
            "message":         "No open positions.",
            "closed_now":      [],
            "still_open":      0,
            "current_capital": state["current_capital"],
            "eod_forced_close": False,
        }

    closed_now: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = []
    is_eod_now  = _is_eod()
    capital     = state["current_capital"]

    for pos in state["open_positions"]:
        ticker = pos["ticker"]
        try:
            today_candles = _fetch_today_candles(ticker, "1m", 1)
            if not today_candles:
                still_open.append(pos)
                continue
            last_c     = today_candles[-1]
            last_high  = last_c["high"]
            last_low   = last_c["low"]
            last_close = last_c["close"]
        except Exception as exc:
            logger.warning("Price fetch error for %s: %s", ticker, exc)
            still_open.append(pos)
            continue

        direction  = pos["direction"]
        sl         = pos["stop_loss"]
        tgt        = pos["target"]
        qty        = pos["quantity"]
        entry_val  = pos["entry_value"]

        exit_reason: str | None  = None
        exit_price:  float | None = None

        if is_eod_now:
            exit_reason = "EOD_CLOSE"
            exit_price  = last_close
        elif direction == "LONG":
            if last_low <= sl:
                exit_reason, exit_price = "SL_HIT", sl
            elif last_high >= tgt:
                exit_reason, exit_price = "TARGET_HIT", tgt
        else:  # SHORT
            if last_high >= sl:
                exit_reason, exit_price = "SL_HIT", sl
            elif last_low <= tgt:
                exit_reason, exit_price = "TARGET_HIT", tgt

        if exit_reason and exit_price is not None:
            exit_value  = qty * exit_price
            pnl_gross   = (exit_value - entry_val) if direction == "LONG" else (entry_val - exit_value)
            pnl_net     = pnl_gross - CHARGE_PER_TRADE   # exit charge only (entry was deducted on open)
            outcome     = "WIN" if pnl_net > 0 else "LOSS"

            # capital += returned position value less exit charge
            capital = round(capital + pnl_gross - CHARGE_PER_TRADE, 2)

            closed_trade: dict[str, Any] = {
                **pos,
                "exit_price":   round(exit_price, 2),
                "exit_reason":  exit_reason,
                "exit_charges": CHARGE_PER_TRADE,
                "pnl_gross":    round(pnl_gross, 2),
                "pnl_net":      round(pnl_net, 2),
                "closed_at":    _now_ist().isoformat(),
                "outcome":      outcome,
            }
            state["closed_trades"].append(closed_trade)
            closed_now.append(closed_trade)
            logger.info(
                "Paper trade closed: %s %s @ %.2f (%s) PnL net=₹%.2f",
                ticker, direction, exit_price, exit_reason, pnl_net,
            )
        else:
            still_open.append(pos)

    state["open_positions"]  = still_open
    state["current_capital"] = capital
    _save_state(state)

    return {
        "status":            "success",
        "checked":           len(closed_now) + len(still_open),
        "closed_now":        closed_now,
        "still_open":        len(still_open),
        "current_capital":   capital,
        "eod_forced_close":  is_eod_now and len(closed_now) > 0,
    }


def paper_trade_reset() -> dict[str, Any]:
    """Reset the paper trading portfolio to a fresh ₹1,00,000 starting balance."""
    state = _default_state()
    _save_state(state)
    logger.info("Paper trading portfolio reset to ₹%.0f", INITIAL_CAPITAL)
    return {
        "status":          "reset",
        "message":         "Paper trading portfolio reset to ₹1,00,000.",
        "initial_capital": INITIAL_CAPITAL,
        "reset_at":        state["initialized_at"],
    }


def run_paper_backtest(days: int = 90) -> dict[str, Any]:
    """
    Run a NIFTY intraday backtest with full ₹ paper trading rules.

    Reuses run_intraday_backtest internally (same SMC detection, min_rr=2.0),
    then applies 0.5% position sizing and ₹50 entry + exit charges to each
    trade, reporting P&L in INR rather than R-multiples.

    yfinance caps 15m data at 60 days; if days > 60 the backtest is
    automatically truncated and truncation_note is set.
    """
    raw = run_intraday_backtest("NIFTY", days=days, interval="15m", min_rr=MIN_RR)
    if "error" in raw:
        return raw

    trades_raw = raw.get("trades", [])
    capital    = INITIAL_CAPITAL
    equity_inr: list[float] = [capital]
    processed:  list[dict[str, Any]] = []

    for t in trades_raw:
        entry     = float(t["entry"])
        sl        = float(t["stop_loss"])
        tgt       = float(t["target"])
        direction = t["direction"]
        outcome   = t["outcome"]

        if outcome == "OPEN":
            processed.append({**t, "skipped": True, "skip_reason": "OPEN — no confirmed exit"})
            continue

        sizing = _calc_position_size(capital, entry, sl)
        if not sizing["feasible"]:
            processed.append({**t, "skipped": True, "skip_reason": sizing["reason"]})
            continue

        # Entry charge
        capital -= CHARGE_PER_TRADE

        qty       = sizing["quantity"]
        entry_val = sizing["entry_value"]
        exit_px   = tgt if outcome == "WIN" else sl

        exit_val   = qty * exit_px
        pnl_gross  = (exit_val - entry_val) if direction == "LONG" else (entry_val - exit_val)
        pnl_net    = pnl_gross - CHARGE_PER_TRADE   # exit charge

        capital = max(0.0, round(capital + pnl_gross - CHARGE_PER_TRADE, 2))

        processed.append({
            **t,
            "quantity":       qty,
            "entry_value":    entry_val,
            "exit_price":     round(exit_px, 2),
            "pnl_gross_inr":  round(pnl_gross, 2),
            "pnl_net_inr":    round(pnl_net, 2),
            "capital_after":  capital,
            "skipped":        False,
        })
        equity_inr.append(capital)

    completed     = [t for t in processed if not t.get("skipped")]
    wins          = [t for t in completed if t["outcome"] == "WIN"]
    losses        = [t for t in completed if t["outcome"] == "LOSS"]
    total_net     = sum(t["pnl_net_inr"] for t in completed)
    total_charges = len(completed) * CHARGE_PER_TRADE * 2  # entry + exit per trade

    # Max drawdown in INR
    peak_eq = INITIAL_CAPITAL
    max_dd  = 0.0
    for eq in equity_inr:
        if eq > peak_eq:
            peak_eq = eq
        dd = peak_eq - eq
        if dd > max_dd:
            max_dd = dd

    best_trade  = max(completed, key=lambda t: t["pnl_net_inr"], default=None)
    worst_trade = min(completed, key=lambda t: t["pnl_net_inr"], default=None)

    return {
        "status":              "success",
        "ticker":              "NIFTY",
        "interval":            "15m",
        "requested_days":      days,
        "actual_days":         raw.get("actual_days"),
        "truncated":           raw.get("truncated"),
        "truncation_note":     raw.get("truncation_note"),
        "initial_capital":     INITIAL_CAPITAL,
        "final_capital":       round(capital, 2),
        "total_net_pnl_inr":   round(total_net, 2),
        "total_gross_pnl_inr": round(sum(t["pnl_gross_inr"] for t in completed), 2),
        "total_charges_inr":   round(total_charges, 2),
        "pnl_pct":             round((capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2),
        "total_trades":        len(completed),
        "wins":                len(wins),
        "losses":              len(losses),
        "win_rate_pct":        round(len(wins) / len(completed) * 100, 1) if completed else 0.0,
        "max_drawdown_inr":    round(max_dd, 2),
        "best_trade_pnl":      round(best_trade["pnl_net_inr"], 2) if best_trade else 0.0,
        "worst_trade_pnl":     round(worst_trade["pnl_net_inr"], 2) if worst_trade else 0.0,
        "equity_curve_inr":    equity_inr,
        "trades":              processed,
    }
