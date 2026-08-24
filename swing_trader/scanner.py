import logging
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from swing_trader.config import MIN_RR

logger = logging.getLogger(__name__)


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _find_bsl(df: pd.DataFrame, lookback: int = 20) -> float | None:
    """Nearest buy-side liquidity: highest swing high in lookback."""
    highs = df["High"].iloc[-lookback:]
    return float(highs.max()) if not highs.empty else None


def _find_fvg(df: pd.DataFrame) -> dict | None:
    """Most recent bullish FVG on daily bars."""
    lows  = df["Low"].to_numpy()
    highs = df["High"].to_numpy()
    for i in range(len(df) - 1, 1, -1):
        if lows[i] > highs[i - 2]:
            fvg_low  = float(highs[i - 2])
            fvg_high = float(lows[i])
            entry    = round((fvg_low + fvg_high) / 2, 2)
            return {"fvg_low": fvg_low, "fvg_high": fvg_high, "entry": entry}
    return None


def scan_entry(ticker: str) -> dict[str, Any] | None:
    """Scan daily bars for a swing entry signal.

    Returns a signal dict or None when no valid setup exists.
    """
    try:
        df = yf.download(ticker, period="90d", interval="1d", progress=False, auto_adjust=True)
    except Exception as exc:
        logger.warning("yfinance error for %s: %s", ticker, exc)
        return None

    if df is None or len(df) < 30:
        logger.warning("Insufficient data for %s", ticker)
        return None

    df = df.copy()
    # yfinance ≥0.2.x returns MultiIndex columns for single tickers — flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # ── EMA-20 trend filter ────────────────────────────────────────────────────
    df["ema20"] = _ema(df["Close"], 20)
    last_close = float(df["Close"].to_numpy()[-1])
    last_ema20 = float(df["ema20"].to_numpy()[-1])
    if last_close <= last_ema20:
        logger.debug("%s: below EMA-20 — skip", ticker)
        return None

    # ── ATR-14 stop loss ──────────────────────────────────────────────────────
    df["atr14"] = _atr(df, 14)
    atr = float(df["atr14"].to_numpy()[-1])

    # ── FVG detection ─────────────────────────────────────────────────────────
    fvg = _find_fvg(df)
    if fvg is None:
        logger.debug("%s: no bullish FVG found — skip", ticker)
        return None

    entry     = fvg["entry"]
    stop_loss = round(fvg["fvg_low"] - atr * 0.5, 2)  # SL below FVG low by half ATR

    if stop_loss >= entry:
        logger.debug("%s: stop_loss ≥ entry — skip", ticker)
        return None

    # ── BSL target ────────────────────────────────────────────────────────────
    target = _find_bsl(df)
    if target is None or target <= entry:
        logger.debug("%s: no valid BSL target — skip", ticker)
        return None

    # ── RR gate ───────────────────────────────────────────────────────────────
    risk   = entry - stop_loss
    reward = target - entry
    rr     = round(reward / risk, 2) if risk > 0 else 0.0

    if rr < MIN_RR:
        logger.info("%s: RR gate failed (%.2f < %.1f)", ticker, rr, MIN_RR)
        return None

    return {
        "ticker":    ticker,
        "entry":     entry,
        "stop_loss": stop_loss,
        "target":    target,
        "rr":        rr,
        "fvg_low":   fvg["fvg_low"],
        "fvg_high":  fvg["fvg_high"],
        "atr14":     round(atr, 2),
        "ema20":     round(last_ema20, 2),
        "close":     round(last_close, 2),
    }
