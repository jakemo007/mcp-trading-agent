"""
tools/market_data.py — Price-action & liquidity-pool tools.

Functions in this module are pure data-fetching helpers.
They are registered as MCP tools in server.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from config import settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
#  TICKER ALIAS MAP — maps friendly names to yfinance symbols
# ──────────────────────────────────────────────────────────────
TICKER_ALIASES: dict[str, str] = {
    "NIFTY":    "^NSEI",
    "NIFTY50":  "^NSEI",
    "BANKNIFTY":"^NSEBANK",
    "SENSEX":   "^BSESN",
    "SPX":      "^GSPC",
    "SPY":      "SPY",
    "QQQ":      "QQQ",
    "DXY":      "DX-Y.NYB",
    "GOLD":     "GC=F",
    "CRUDE":    "CL=F",
    "BTC":      "BTC-USD",
    "ETH":      "ETH-USD",
}


def _resolve_ticker(ticker: str) -> str:
    """Resolve user-friendly name → yfinance symbol."""
    return TICKER_ALIASES.get(ticker.upper().strip(), ticker.upper().strip())


# ──────────────────────────────────────────────────────────────
#  get_daily_ohlc
# ──────────────────────────────────────────────────────────────
def get_daily_ohlc(ticker: str, days: int | None = None) -> dict[str, Any]:
    """
    Fetch daily OHLCV data for *ticker* over the last *days* trading days.

    Returns a JSON-serialisable dict containing:
      - ticker / resolved_symbol / period
      - candles: list of {date, open, high, low, close, volume}
      - summary: {latest_close, period_high, period_low, avg_volume}
    """
    days = days or settings.default_ohlc_days
    symbol = _resolve_ticker(ticker)
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))  # buffer for weekends

    logger.info("Fetching OHLC for %s (%s) — %d day look-back", ticker, symbol, days)

    try:
        df: pd.DataFrame = yf.download(
            symbol,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.exception("yfinance download failed for %s", symbol)
        return {"error": f"Failed to fetch data for {symbol}: {exc}"}

    if df.empty:
        return {"error": f"No data returned for {symbol}. Check ticker validity."}

    # Flatten MultiIndex columns if present (yfinance >= 0.2.40)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.tail(days).copy()
    df.index = pd.to_datetime(df.index)

    candles = []
    for dt, row in df.iterrows():
        candles.append({
            "date":   dt.strftime("%Y-%m-%d"),
            "open":   round(float(row["Open"]), 2),
            "high":   round(float(row["High"]), 2),
            "low":    round(float(row["Low"]), 2),
            "close":  round(float(row["Close"]), 2),
            "volume": int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
        })

    return {
        "ticker":          ticker.upper(),
        "resolved_symbol": symbol,
        "period_days":     days,
        "total_candles":   len(candles),
        "candles":         candles,
        "summary": {
            "latest_close": candles[-1]["close"] if candles else None,
            "period_high":  round(float(df["High"].max()), 2),
            "period_low":   round(float(df["Low"].min()), 2),
            "avg_volume":   int(df["Volume"].mean()) if "Volume" in df.columns else 0,
        },
    }


# ──────────────────────────────────────────────────────────────
#  identify_liquidity_pools
# ──────────────────────────────────────────────────────────────
def identify_liquidity_pools(ticker: str) -> dict[str, Any]:
    """
    Scan recent price action for swing highs & swing lows that represent
    un-swept liquidity zones (buy-side & sell-side liquidity).

    Algorithm:
      1. Fetch `swing_lookback_days` candles.
      2. Use a rolling-window approach (width = `swing_window`) to find
         local maxima (swing highs) and minima (swing lows).
      3. Classify them as *buy-side liquidity* (above current price) or
         *sell-side liquidity* (below current price).
    """
    symbol = _resolve_ticker(ticker)
    window = settings.swing_window
    lookback = settings.swing_lookback_days
    end = datetime.now()
    start = end - timedelta(days=int(lookback * 1.6))

    logger.info("Scanning liquidity pools for %s (%s)", ticker, symbol)

    try:
        df: pd.DataFrame = yf.download(
            symbol,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.exception("yfinance download failed for %s", symbol)
        return {"error": f"Failed to fetch data for {symbol}: {exc}"}

    if df.empty:
        return {"error": f"No data returned for {symbol}. Check ticker validity."}

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.tail(lookback).copy()
    df.index = pd.to_datetime(df.index)

    highs = df["High"].values.astype(float)
    lows  = df["Low"].values.astype(float)
    dates = df.index

    swing_highs: list[dict[str, Any]] = []
    swing_lows:  list[dict[str, Any]] = []

    half = window // 2
    for i in range(half, len(highs) - half):
        # Swing High: centre bar's high is the max in its neighbourhood
        local_highs = highs[i - half: i + half + 1]
        if highs[i] == np.max(local_highs):
            swing_highs.append({
                "date":  dates[i].strftime("%Y-%m-%d"),
                "level": round(float(highs[i]), 2),
            })

        # Swing Low: centre bar's low is the min in its neighbourhood
        local_lows = lows[i - half: i + half + 1]
        if lows[i] == np.min(local_lows):
            swing_lows.append({
                "date":  dates[i].strftime("%Y-%m-%d"),
                "level": round(float(lows[i]), 2),
            })

    # De-duplicate consecutive duplicates
    def _dedup(items: list[dict]) -> list[dict]:
        out: list[dict] = []
        for item in items:
            if not out or item["level"] != out[-1]["level"]:
                out.append(item)
        return out

    swing_highs = _dedup(swing_highs)
    swing_lows  = _dedup(swing_lows)

    current_price = round(float(df["Close"].iloc[-1]), 2)

    buy_side  = [s for s in swing_highs if s["level"] > current_price]
    sell_side = [s for s in swing_lows  if s["level"] < current_price]

    # Keep the most relevant zones (closest to price)
    buy_side.sort(key=lambda x: x["level"])
    sell_side.sort(key=lambda x: x["level"], reverse=True)

    return {
        "ticker":          ticker.upper(),
        "resolved_symbol": symbol,
        "current_price":   current_price,
        "buy_side_liquidity":  buy_side[:8],   # nearest highs above
        "sell_side_liquidity": sell_side[:8],   # nearest lows below
        "all_swing_highs":     swing_highs,
        "all_swing_lows":      swing_lows,
        "analysis_period_days": lookback,
    }


# ──────────────────────────────────────────────────────────────
#  get_intraday_ohlc
# ──────────────────────────────────────────────────────────────

# yfinance hard limits per interval
_INTRADAY_MAX_DAYS: dict[str, int] = {
    "1m":  7,
    "2m":  60,
    "5m":  60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1h":  730,
    "90m": 60,
}
_VALID_INTERVALS = frozenset(_INTRADAY_MAX_DAYS)


def get_intraday_ohlc(
    ticker: str,
    interval: str = "15m",
    days: int = 5,
) -> dict[str, Any]:
    """
    Fetch intraday OHLCV data for *ticker* at a sub-daily interval.

    Use this for **live session analysis** -- it returns today's
    developing candles (plus recent history) so the agent can see
    the current intraday structure, forming Order Blocks, FVGs, and
    immediate liquidity pools within the active session.

    yfinance limits
    ---------------
    1m : max 7 days  |  2m/5m/15m/30m/90m : max 60 days
    60m / 1h : max 730 days

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC", "GOLD").
    interval : str, default "15m"
        Candle interval -- one of: 1m, 2m, 5m, 15m, 30m, 60m, 1h, 90m.
        For precision intraday entries use "5m" or "15m".
    days : int, default 5
        Number of calendar days of intraday history to fetch.
        Automatically clamped to the yfinance limit for the interval.

    Returns
    -------
    JSON-serialisable dict with:
      - all candles (datetime, open, high, low, close, volume)
      - today_candles : only today's bars (the live session)
      - session_summary : live price, session high/low, open, candle count
    """
    interval = interval.lower().strip()
    if interval not in _VALID_INTERVALS:
        logger.warning("Invalid interval '%s'; defaulting to 15m", interval)
        interval = "15m"

    max_days = _INTRADAY_MAX_DAYS[interval]
    days = min(max(int(days), 1), max_days)

    symbol = _resolve_ticker(ticker)
    logger.info(
        "Fetching intraday OHLC for %s (%s) — interval=%s, days=%d",
        ticker, symbol, interval, days,
    )

    try:
        df: pd.DataFrame = yf.download(
            symbol,
            period=f"{days}d",
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.exception("yfinance intraday download failed for %s", symbol)
        return {"error": f"Failed to fetch intraday data for {symbol}: {exc}"}

    if df.empty:
        return {
            "error": (
                f"No intraday data returned for {symbol}. "
                "The market may be closed or the ticker invalid."
            )
        }

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index)

    # Build full candle list
    candles: list[dict[str, Any]] = []
    for dt, row in df.iterrows():
        candles.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M"),
            "open":     round(float(row["Open"]),   2),
            "high":     round(float(row["High"]),   2),
            "low":      round(float(row["Low"]),    2),
            "close":    round(float(row["Close"]),  2),
            "volume":   int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
        })

    # Isolate today's session candles
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_candles = [c for c in candles if c["datetime"].startswith(today_str)]

    # Fall back to the most recent trading day if today has no data yet
    # (pre-market or holiday)
    active_candles = today_candles
    active_date    = today_str
    if not today_candles and candles:
        active_date    = candles[-1]["datetime"][:10]
        active_candles = [c for c in candles if c["datetime"].startswith(active_date)]

    # Session statistics
    if active_candles:
        session_open  = active_candles[0]["open"]
        session_high  = max(c["high"]  for c in active_candles)
        session_low   = min(c["low"]   for c in active_candles)
        live_price    = active_candles[-1]["close"]
        candle_count  = len(active_candles)
    else:
        session_open = session_high = session_low = live_price = None
        candle_count = 0

    return {
        "ticker":          ticker.upper(),
        "resolved_symbol": symbol,
        "interval":        interval,
        "period_days":     days,
        "total_candles":   len(candles),
        "candles":         candles,
        "today_candles":   active_candles,
        "session_summary": {
            "session_date":  active_date,
            "session_open":  session_open,
            "session_high":  session_high,
            "session_low":   session_low,
            "live_price":    live_price,
            "candle_count":  candle_count,
            "interval":      interval,
            "note": (
                "Live session data — last candle may be a developing (incomplete) bar."
                if active_date == today_str
                else f"Market closed. Showing most recent session: {active_date}."
            ),
        },
    }


# ──────────────────────────────────────────────────────────────
#  get_historical_backtest_data
# ──────────────────────────────────────────────────────────────
def get_historical_backtest_data(
    ticker: str,
    days: int = 180,
) -> dict[str, Any]:
    """
    Fetch extended historical OHLCV data for backtesting purposes.

    Unlike ``get_daily_ohlc`` (which targets ~60 recent candles for
    current-state analysis), this function returns a *dense* dataset
    over a longer horizon, enriched with pre-computed fields the LLM
    can use to simulate ICT/SMC setups:

      - ``daily_change_pct``: close-to-close % move
      - ``range_pct``: intraday range as % of open
      - ``is_swing_high`` / ``is_swing_low``: boolean flags

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC").
    days : int, default 180
        Number of trading days of history to retrieve (max ~365).

    Returns
    -------
    JSON-serialisable dict with candles list and period summary.
    """
    days = min(max(days, 10), 500)  # clamp to sane range
    symbol = _resolve_ticker(ticker)
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6))

    logger.info(
        "Fetching backtest data for %s (%s) — %d day look-back",
        ticker, symbol, days,
    )

    try:
        df: pd.DataFrame = yf.download(
            symbol,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.exception("yfinance download failed for %s", symbol)
        return {"error": f"Failed to fetch backtest data for {symbol}: {exc}"}

    if df.empty:
        return {"error": f"No data returned for {symbol}. Check ticker validity."}

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.tail(days).copy()
    df.index = pd.to_datetime(df.index)

    # ── Pre-compute swing flags ──────────────────────────────
    window = settings.swing_window
    half = window // 2
    highs = df["High"].values.astype(float)
    lows  = df["Low"].values.astype(float)

    swing_high_flags = [False] * len(df)
    swing_low_flags  = [False] * len(df)

    for i in range(half, len(highs) - half):
        local_h = highs[i - half: i + half + 1]
        if highs[i] == np.max(local_h):
            swing_high_flags[i] = True

        local_l = lows[i - half: i + half + 1]
        if lows[i] == np.min(local_l):
            swing_low_flags[i] = True

    # ── Build enriched candle list ───────────────────────────
    closes = df["Close"].values.astype(float)
    candles = []
    for idx, (dt, row) in enumerate(df.iterrows()):
        prev_close = closes[idx - 1] if idx > 0 else closes[idx]
        daily_chg  = round(((closes[idx] - prev_close) / prev_close) * 100, 3) if prev_close else 0.0
        range_pct  = round(((highs[idx] - lows[idx]) / float(row["Open"])) * 100, 3) if float(row["Open"]) else 0.0

        candles.append({
            "date":             dt.strftime("%Y-%m-%d"),
            "open":             round(float(row["Open"]), 2),
            "high":             round(float(row["High"]), 2),
            "low":              round(float(row["Low"]), 2),
            "close":            round(float(row["Close"]), 2),
            "volume":           int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
            "daily_change_pct": daily_chg,
            "range_pct":        range_pct,
            "is_swing_high":    swing_high_flags[idx],
            "is_swing_low":     swing_low_flags[idx],
        })

    # ── Period statistics ────────────────────────────────────
    period_high  = round(float(df["High"].max()), 2)
    period_low   = round(float(df["Low"].min()), 2)
    avg_volume   = int(df["Volume"].mean()) if "Volume" in df.columns else 0
    total_up     = sum(1 for c in candles if c["daily_change_pct"] > 0)
    total_down   = sum(1 for c in candles if c["daily_change_pct"] < 0)
    avg_range    = round(float(sum(c["range_pct"] for c in candles) / len(candles)), 3) if candles else 0.0

    return {
        "ticker":          ticker.upper(),
        "resolved_symbol": symbol,
        "period_days":     days,
        "total_candles":   len(candles),
        "candles":         candles,
        "summary": {
            "latest_close":       candles[-1]["close"] if candles else None,
            "period_high":        period_high,
            "period_low":         period_low,
            "avg_volume":         avg_volume,
            "up_days":            total_up,
            "down_days":          total_down,
            "avg_daily_range_pct": avg_range,
        },
    }


# ──────────────────────────────────────────────────────────────
#  _simulate_trade  (private walk-forward helper)
# ──────────────────────────────────────────────────────────────

def _simulate_trade(
    direction: str,
    entry: float,
    stop_loss: float,
    target: float,
    highs: "np.ndarray",
    lows:  "np.ndarray",
    start_idx: int,
    n: int,
    max_bars: int = 250,
) -> tuple[str, float | None]:
    """
    Walk forward bar-by-bar from *start_idx*.
    Returns (outcome, exit_price) where outcome is 'WIN', 'LOSS', or 'OPEN'.
    Checks stop before target on each bar to be conservative.
    """
    for k in range(start_idx, min(start_idx + max_bars, n)):
        if direction == "LONG":
            if lows[k] <= stop_loss:
                return "LOSS", stop_loss
            if highs[k] >= target:
                return "WIN", target
        else:  # SHORT
            if highs[k] >= stop_loss:
                return "LOSS", stop_loss
            if lows[k] <= target:
                return "WIN", target
    return "OPEN", None


# ──────────────────────────────────────────────────────────────
#  _get_session_label  (private helper)
# ──────────────────────────────────────────────────────────────

def _get_session_label(dt: datetime) -> str:
    """Map a UTC timestamp to a named IST intraday session window."""
    total_min = (dt.hour * 60 + dt.minute + 330) % (24 * 60)  # UTC → IST (+5:30)
    if total_min < 9 * 60 + 30:
        return "Pre-Market"
    elif total_min < 10 * 60:
        return "Opening (09:15-10:00)"
    elif total_min < 11 * 60 + 30:
        return "Morning (10:00-11:30)"
    elif total_min < 13 * 60:
        return "Midday (11:30-13:00)"
    elif total_min < 14 * 60 + 30:
        return "Afternoon (13:00-14:30)"
    elif total_min < 15 * 60 + 30:
        return "Closing (14:30-15:30)"
    return "Post-Market"


# ──────────────────────────────────────────────────────────────
#  run_intraday_backtest
# ──────────────────────────────────────────────────────────────

def run_intraday_backtest(
    ticker: str,
    days: int = 90,
    interval: str = "15m",
    min_rr: float = 3.0,
) -> dict[str, Any]:
    """
    Fetch intraday OHLCV data and scan for SMC/ICT setups
    (Liquidity Sweep → Fair Value Gap entry) filtered to RR >= min_rr.

    Algorithm
    ---------
    1. Detect swing highs/lows with a ±5-bar rolling window.
    2. For each candle, check for:
       - BSL Sweep (SHORT): high > prior swing high AND close < that level.
       - SSL Sweep (LONG) : low  < prior swing low  AND close > that level.
    3. After a sweep, scan the next 5 candles for a three-candle FVG:
       - Bearish FVG: lows[j-1] > highs[j+1]
       - Bullish FVG: highs[j-1] < lows[j+1]
    4. Entry = FVG midpoint. SL = 0.15% beyond sweep wick.
       Target = nearest opposing swing level beyond entry.
    5. RR filter: record only if reward / risk >= 3.0.
    6. Walk-forward simulation to determine WIN / LOSS / OPEN.

    yfinance caps 15m data at 60 days. If *days* > cap the tool
    fetches the maximum available and sets ``truncated = True``.

    Returns
    -------
    JSON-serialisable dict with per-trade records, win rate,
    average RR, equity curve (R-multiples), and a truncation note.
    """
    interval = interval.lower().strip()
    if interval not in _VALID_INTERVALS:
        logger.warning("Invalid interval '%s'; defaulting to 15m", interval)
        interval = "15m"

    max_days    = _INTRADAY_MAX_DAYS[interval]
    actual_days = min(max(int(days), 5), max_days)
    truncated   = actual_days < int(days)

    symbol = _resolve_ticker(ticker)
    logger.info(
        "Intraday backtest: %s (%s) interval=%s days=%d (requested=%d)",
        ticker, symbol, interval, actual_days, days,
    )

    try:
        df: pd.DataFrame = yf.download(
            symbol,
            period=f"{actual_days}d",
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.exception("yfinance intraday backtest download failed for %s", symbol)
        return {"error": f"Failed to fetch data for {symbol}: {exc}"}

    if df.empty:
        return {"error": f"No intraday data returned for {symbol}. Market may be closed or ticker invalid."}

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index)

    highs  = df["High"].values.astype(float)
    lows   = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    times  = df.index
    n      = len(df)

    HALF    = 5       # swing detection half-window
    MIN_RR  = float(min_rr)
    SL_BUFF = 0.0015  # minimum SL buffer (fallback when ATR is very small)
    ATR_PERIOD = 14

    # ── Wilder ATR via pandas EWM (adaptive SL sizing) ───────────
    _hs  = pd.Series(highs)
    _ls  = pd.Series(lows)
    _cs  = pd.Series(closes)
    _pc  = _cs.shift(1).fillna(_cs)
    _tr  = pd.concat([_hs - _ls, (_hs - _pc).abs(), (_ls - _pc).abs()], axis=1).max(axis=1)
    atr_arr = _tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean().values
    del _hs, _ls, _cs, _pc, _tr

    # ── Detect swing highs / lows ────────────────────────────────
    swing_high_idx: list[int] = []
    swing_low_idx:  list[int] = []

    for i in range(HALF, n - HALF):
        if highs[i] == float(np.max(highs[i - HALF: i + HALF + 1])):
            swing_high_idx.append(i)
        if lows[i] == float(np.min(lows[i - HALF: i + HALF + 1])):
            swing_low_idx.append(i)

    # ── Scan for setups ──────────────────────────────────────────
    trades: list[dict[str, Any]] = []

    for i in range(HALF + 1, n - HALF - 3):

        # ─── SHORT: BSL Sweep → Bearish FVG ────────────────────
        prior_sh_idxs = [idx for idx in swing_high_idx if idx < i - 1]
        if prior_sh_idxs:
            last_sh_idx = prior_sh_idxs[-1]
            sh_level    = highs[last_sh_idx]

            if highs[i] > sh_level and closes[i] < sh_level:
                # Search for bearish FVG near the sweep candle
                fvg_entry: float | None = None
                fvg_top:   float | None = None
                fvg_bot:   float | None = None
                fvg_j:     int   | None = None

                for j in range(i, min(i + 5, n - 1)):
                    if j >= 1 and (j + 1) < n and lows[j - 1] > highs[j + 1]:
                        fvg_top   = lows[j - 1]
                        fvg_bot   = highs[j + 1]
                        fvg_entry = round((fvg_top + fvg_bot) / 2, 2)
                        fvg_j     = j
                        break

                if fvg_entry is not None and fvg_j is not None:
                    # Consecutive sweep: same swing high swept by another bar within 20 bars
                    is_consec_short = any(
                        highs[k] > sh_level and closes[k] < sh_level
                        for k in range(max(HALF + 1, i - 20), i)
                    )
                    # ATR-adaptive SL: at least 0.25× ATR beyond the sweep wick
                    _sl_buf  = max(float(highs[i]) * SL_BUFF, float(atr_arr[i]) * 0.25)
                    sl_price = round(float(highs[i]) + _sl_buf, 2)
                    ssl_candidates = [
                        lows[idx] for idx in swing_low_idx
                        if idx < i and lows[idx] < fvg_entry
                    ]
                    if ssl_candidates:
                        tgt_price = round(max(ssl_candidates), 2)
                        risk   = sl_price - fvg_entry
                        reward = fvg_entry - tgt_price
                        if risk > 0:
                            rr = round(reward / risk, 2)
                            if rr >= MIN_RR:
                                outcome, exit_px = _simulate_trade(
                                    "SHORT", fvg_entry, sl_price, tgt_price,
                                    highs, lows, fvg_j + 1, n,
                                )
                                trades.append({
                                    "setup_type":           "BSL_SWEEP_FVG",
                                    "direction":            "SHORT",
                                    "sweep_time":           times[i].strftime("%Y-%m-%d %H:%M"),
                                    "session_label":        _get_session_label(times[i]),
                                    "hour":                 int(times[i].hour),
                                    "swept_level":          round(sh_level, 2),
                                    "fvg_zone":             [round(fvg_top, 2), round(fvg_bot, 2)],
                                    "entry":                fvg_entry,
                                    "stop_loss":            sl_price,
                                    "target":               tgt_price,
                                    "risk_pts":             round(risk, 2),
                                    "reward_pts":           round(reward, 2),
                                    "rr":                   rr,
                                    "atr_at_setup":         round(float(atr_arr[i]), 2),
                                    "is_consecutive_sweep": is_consec_short,
                                    "outcome":              outcome,
                                    "exit_price":           round(exit_px, 2) if exit_px else None,
                                })

        # ─── LONG: SSL Sweep → Bullish FVG ─────────────────────
        prior_sl_idxs = [idx for idx in swing_low_idx if idx < i - 1]
        if prior_sl_idxs:
            last_sl_idx = prior_sl_idxs[-1]
            sl_level    = lows[last_sl_idx]

            if lows[i] < sl_level and closes[i] > sl_level:
                fvg_entry = None
                fvg_top   = None
                fvg_bot   = None
                fvg_j     = None

                for j in range(i, min(i + 5, n - 1)):
                    if j >= 1 and (j + 1) < n and highs[j - 1] < lows[j + 1]:
                        fvg_bot   = highs[j - 1]
                        fvg_top   = lows[j + 1]
                        fvg_entry = round((fvg_top + fvg_bot) / 2, 2)
                        fvg_j     = j
                        break

                if fvg_entry is not None and fvg_j is not None:
                    # Consecutive sweep: same swing low swept by another bar within 20 bars
                    is_consec_long = any(
                        lows[k] < sl_level and closes[k] > sl_level
                        for k in range(max(HALF + 1, i - 20), i)
                    )
                    # ATR-adaptive SL: at least 0.25× ATR below the sweep wick
                    _sl_buf  = max(float(lows[i]) * SL_BUFF, float(atr_arr[i]) * 0.25)
                    sl_price = round(float(lows[i]) - _sl_buf, 2)
                    bsl_candidates = [
                        highs[idx] for idx in swing_high_idx
                        if idx < i and highs[idx] > fvg_entry
                    ]
                    if bsl_candidates:
                        tgt_price = round(min(bsl_candidates), 2)
                        risk   = fvg_entry - sl_price
                        reward = tgt_price - fvg_entry
                        if risk > 0:
                            rr = round(reward / risk, 2)
                            if rr >= MIN_RR:
                                outcome, exit_px = _simulate_trade(
                                    "LONG", fvg_entry, sl_price, tgt_price,
                                    highs, lows, fvg_j + 1, n,
                                )
                                trades.append({
                                    "setup_type":           "SSL_SWEEP_FVG",
                                    "direction":            "LONG",
                                    "sweep_time":           times[i].strftime("%Y-%m-%d %H:%M"),
                                    "session_label":        _get_session_label(times[i]),
                                    "hour":                 int(times[i].hour),
                                    "swept_level":          round(sl_level, 2),
                                    "fvg_zone":             [round(fvg_bot, 2), round(fvg_top, 2)],
                                    "entry":                fvg_entry,
                                    "stop_loss":            sl_price,
                                    "target":               tgt_price,
                                    "risk_pts":             round(risk, 2),
                                    "reward_pts":           round(reward, 2),
                                    "rr":                   rr,
                                    "atr_at_setup":         round(float(atr_arr[i]), 2),
                                    "is_consecutive_sweep": is_consec_long,
                                    "outcome":              outcome,
                                    "exit_price":           round(exit_px, 2) if exit_px else None,
                                })

    # ── Aggregate stats ──────────────────────────────────────────
    completed = [t for t in trades if t["outcome"] != "OPEN"]
    wins      = [t for t in completed if t["outcome"] == "WIN"]
    losses    = [t for t in completed if t["outcome"] == "LOSS"]

    win_rate = round(len(wins) / len(completed) * 100, 1) if completed else 0.0
    avg_rr   = round(sum(t["rr"] for t in trades) / len(trades), 2) if trades else 0.0

    # R-multiple equity curve (each WIN = +rr, each LOSS = -1)
    equity: list[float] = [0.0]
    for t in completed:
        prev = equity[-1]
        equity.append(round(prev + t["rr"] if t["outcome"] == "WIN" else prev - 1.0, 2))

    # ── Enhanced aggregate metrics ───────────────────────────────
    total_profit_r = sum(t["rr"] for t in completed if t["outcome"] == "WIN")
    total_loss_r   = float(len(losses))
    profit_factor  = round(total_profit_r / total_loss_r, 2) if total_loss_r > 0 else None

    peak = 0.0
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    max_drawdown_r = round(max_dd, 2)

    expectancy_r = round(
        (win_rate / 100.0 * avg_rr) - ((1.0 - win_rate / 100.0) * 1.0), 2
    ) if trades else 0.0

    # ── Session breakdown (setups by time-of-day window) ─────────
    session_breakdown: dict[str, dict] = {}
    for t in trades:
        lbl = t.get("session_label", "Unknown")
        sb  = session_breakdown.setdefault(
            lbl,
            {"total": 0, "wins": 0, "losses": 0, "open_trades": 0, "win_rate_pct": 0.0},
        )
        sb["total"] += 1
        if   t["outcome"] == "WIN":  sb["wins"]        += 1
        elif t["outcome"] == "LOSS": sb["losses"]      += 1
        else:                         sb["open_trades"] += 1

    for lbl, sb in session_breakdown.items():
        closed = sb["wins"] + sb["losses"]
        sb["win_rate_pct"] = round(sb["wins"] / closed * 100, 1) if closed else 0.0

    return {
        "ticker":            ticker.upper(),
        "resolved_symbol":   symbol,
        "interval":          interval,
        "requested_days":    days,
        "actual_days":       actual_days,
        "truncated":         truncated,
        "truncation_note":   (
            f"yfinance caps {interval} data at {max_days} days. "
            f"Requested {days} days — fetched {actual_days} days."
        ) if truncated else None,
        "total_candles":     n,
        "total_setups":      len(trades),
        "completed_setups":  len(completed),
        "wins":              len(wins),
        "losses":            len(losses),
        "open_trades":       len(trades) - len(completed),
        "win_rate_pct":      win_rate,
        "avg_rr":            avg_rr,
        "profit_factor":     profit_factor,
        "max_drawdown_r":    max_drawdown_r,
        "expectancy_r":      expectancy_r,
        "min_rr_filter":     MIN_RR,
        "equity_curve":      equity,
        "session_breakdown": session_breakdown,
        "trades":            trades,
    }


# ──────────────────────────────────────────────────────────────
#  get_multi_timeframe_data
# ──────────────────────────────────────────────────────────────

def get_multi_timeframe_data(ticker: str) -> dict[str, Any]:
    """
    Simultaneously fetch OHLCV data across Monthly, Weekly, and Daily
    timeframes for multi-timeframe breakout analysis.

    Timeframes fetched
    ------------------
    Monthly : last ~24 months  (interval="1mo", period="2y")
    Weekly  : last ~52 weeks   (interval="1wk", period="1y")
    Daily   : last ~65 days    (interval="1d",  period="3mo")

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC").

    Returns
    -------
    JSON-serialisable dict with monthly, weekly, and daily sub-dicts,
    each containing a candles list and a period summary block.
    """
    symbol = _resolve_ticker(ticker)
    logger.info("MTF fetch: %s (%s)", ticker, symbol)

    _CONFIGS = [
        ("monthly", "1mo", "2y",  24),
        ("weekly",  "1wk", "1y",  52),
        ("daily",   "1d",  "3mo", 65),
    ]

    result: dict[str, Any] = {
        "ticker":          ticker.upper(),
        "resolved_symbol": symbol,
        "fetched_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    for tf_name, interval, period, expected_bars in _CONFIGS:
        try:
            df: pd.DataFrame = yf.download(
                symbol,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
            )

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            if df.empty:
                result[tf_name] = {"error": f"No {tf_name} data returned for {symbol}"}
                continue

            df.index = pd.to_datetime(df.index)

            candles: list[dict[str, Any]] = []
            for dt, row in df.iterrows():
                candles.append({
                    "date":   dt.strftime("%Y-%m-%d"),
                    "open":   round(float(row["Open"]),  2),
                    "high":   round(float(row["High"]),  2),
                    "low":    round(float(row["Low"]),   2),
                    "close":  round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]) if not pd.isna(row["Volume"]) else 0,
                })

            closes = df["Close"].values.astype(float)

            result[tf_name] = {
                "interval":      interval,
                "bars_fetched":  len(candles),
                "bars_expected": expected_bars,
                "candles":       candles,
                "summary": {
                    "latest_close":      round(float(closes[-1]), 2),
                    "period_high":       round(float(df["High"].max()), 2),
                    "period_low":        round(float(df["Low"].min()),  2),
                    "avg_volume":        int(df["Volume"].mean()) if "Volume" in df.columns else 0,
                    "period_change_pct": round(
                        (float(closes[-1]) / float(closes[0]) - 1.0) * 100, 2
                    ) if len(closes) > 1 else 0.0,
                },
            }

        except Exception as exc:
            logger.exception("MTF fetch failed: %s / %s", symbol, tf_name)
            result[tf_name] = {"error": f"{tf_name} fetch failed: {exc}"}

    return result


# ──────────────────────────────────────────────────────────────
#  scan_for_breakout
# ──────────────────────────────────────────────────────────────

def scan_for_breakout(ticker: str) -> dict[str, Any]:
    """
    Score a ticker on a 1–10 Multi-Timeframe Breakout scale using
    SMC/ICT criteria across Monthly, Weekly, and Daily charts.

    Scoring breakdown (10 pts max)
    --------------------------------
    Monthly (3 pts):
      +1  Price above EMA-6 monthly      (macro uptrend)
      +1  Approaching Monthly BSL ≤ 5%   (liquidity draw)
      +1  2/3 recent months closed bullish

    Weekly (3 pts):
      +1  Price above EMA-20 weekly       (medium-term trend)
      +1  Volatility contraction          (4-week ATR < 80% of 12-week avg)
      +1  Coiling below Weekly BSL ≤ 3%

    Daily (4 pts):
      +1  Price above EMA-20 daily
      +1  Bullish displacement candle in last 10 bars (range > 1.5× ATR-14)
      +1  Unmitigated bullish FVG in last 15 bars
      +1  Volume spike: 5-day avg ≥ 1.3× 20-day avg

    Conviction tiers
    ----------------
    8–10  HIGH     (high_conviction_confirmed when M + W both BULLISH)
    5–7   MODERATE (Watch and Wait — monitor daily for trigger)
    1–4   LOW      (No confluence — skip)

    Returns
    -------
    JSON dict with breakout_score, conviction, trigger_price, why_now,
    criteria_met list, and a timeframe_alignment matrix.
    """
    symbol = _resolve_ticker(ticker)
    logger.info("Breakout scan: %s (%s)", ticker, symbol)

    mtf = get_multi_timeframe_data(ticker)

    score = 0
    criteria: list[str] = []
    current_price: float | None = None

    monthly_status: dict[str, Any] = {"trend": "UNKNOWN", "score": 0}
    weekly_status:  dict[str, Any] = {"trend": "UNKNOWN", "score": 0}
    daily_status:   dict[str, Any] = {"trend": "UNKNOWN", "score": 0}
    trigger_price:  float | None = None
    fvg_zone:       list[float] | None = None

    # ── Shared helpers (local to avoid polluting module namespace) ─
    def _atr_series(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
        hs = pd.Series(h); ls = pd.Series(l); cs = pd.Series(c)
        pc = cs.shift(1).fillna(cs)
        tr = pd.concat([hs - ls, (hs - pc).abs(), (ls - pc).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1.0 / period, adjust=False).mean().values

    def _swing_high_levels(highs_a: np.ndarray, half: int) -> list[float]:
        n = len(highs_a)
        return [
            float(highs_a[i])
            for i in range(half, n - half)
            if highs_a[i] == float(np.max(highs_a[i - half: i + half + 1]))
        ]

    # ── MONTHLY (max 3 pts) ───────────────────────────────────────
    m_score = 0
    m_data  = mtf.get("monthly", {})
    if "candles" in m_data and len(m_data["candles"]) >= 6:
        mc   = m_data["candles"]
        m_cl = np.array([c["close"] for c in mc], dtype=float)
        m_hi = np.array([c["high"]  for c in mc], dtype=float)
        current_price = float(m_cl[-1])

        m_ema6      = pd.Series(m_cl).ewm(span=6, adjust=False).mean().values
        above_m_ema = bool(m_cl[-1] > m_ema6[-1])
        if above_m_ema:
            m_score += 1
            criteria.append("Monthly: Price above EMA-6 — macro uptrend confirmed")

        m_bsl = [h for h in _swing_high_levels(m_hi, 2)
                 if current_price < h <= current_price * 1.05]
        near_m_bsl = bool(m_bsl)
        if near_m_bsl:
            m_score += 1
            criteria.append(f"Monthly: Approaching BSL at {min(m_bsl):.2f} (within 5%)")

        m_bull_count = sum(1 for c in mc[-3:] if c["close"] > c["open"])
        m_bullish    = m_bull_count >= 2
        if m_bullish:
            m_score += 1
            criteria.append(f"Monthly: {m_bull_count}/3 recent months closed bullish")

        monthly_status = {
            "trend":       (
                "BULLISH" if (above_m_ema and m_bullish) else
                "BEARISH" if (not above_m_ema and not m_bullish) else
                "NEUTRAL"
            ),
            "above_ema6":  above_m_ema,
            "ema6":        round(float(m_ema6[-1]), 2),
            "near_bsl":    near_m_bsl,
            "nearest_bsl": round(min(m_bsl), 2) if m_bsl else None,
            "score":       m_score,
        }

    score += m_score

    # ── WEEKLY (max 3 pts) ────────────────────────────────────────
    w_score = 0
    w_data  = mtf.get("weekly", {})
    if "candles" in w_data and len(w_data["candles"]) >= 12:
        wc   = w_data["candles"]
        w_cl = np.array([c["close"] for c in wc], dtype=float)
        w_hi = np.array([c["high"]  for c in wc], dtype=float)
        w_lo = np.array([c["low"]   for c in wc], dtype=float)
        if current_price is None:
            current_price = float(w_cl[-1])

        w_ema20      = pd.Series(w_cl).ewm(span=20, adjust=False).mean().values
        above_w_ema  = bool(w_cl[-1] > w_ema20[-1])
        if above_w_ema:
            w_score += 1
            criteria.append("Weekly: Price above EMA-20 — medium-term bullish")

        w_atr_arr      = _atr_series(w_hi, w_lo, w_cl, period=4)
        recent_w_atr   = float(np.mean(w_atr_arr[-4:]))
        baseline_w_atr = float(np.mean(w_atr_arr[-12:]))
        vol_contraction = bool(baseline_w_atr > 0 and recent_w_atr < baseline_w_atr * 0.80)
        if vol_contraction:
            w_score += 1
            criteria.append(
                f"Weekly: Volatility contraction — 4-wk ATR {recent_w_atr:.2f} "
                f"< 80% of 12-wk avg {baseline_w_atr:.2f}"
            )

        w_bsl = [h for h in _swing_high_levels(w_hi, 3)
                 if w_cl[-1] < h <= w_cl[-1] * 1.03]
        near_w_bsl = bool(w_bsl)
        if near_w_bsl:
            w_score += 1
            criteria.append(f"Weekly: Coiling below BSL at {min(w_bsl):.2f} (within 3%)")

        weekly_status = {
            "trend":                  "BULLISH" if above_w_ema else "BEARISH",
            "above_ema20":            above_w_ema,
            "ema20":                  round(float(w_ema20[-1]), 2),
            "volatility_contraction": vol_contraction,
            "recent_atr_4w":          round(recent_w_atr, 2),
            "baseline_atr_12w":       round(baseline_w_atr, 2),
            "near_bsl":               near_w_bsl,
            "nearest_bsl":            round(min(w_bsl), 2) if w_bsl else None,
            "score":                  w_score,
        }

    score += w_score

    # ── DAILY (max 4 pts) ─────────────────────────────────────────
    d_score = 0
    d_data  = mtf.get("daily", {})
    if "candles" in d_data and len(d_data["candles"]) >= 20:
        dc   = d_data["candles"]
        d_cl = np.array([c["close"]  for c in dc], dtype=float)
        d_hi = np.array([c["high"]   for c in dc], dtype=float)
        d_lo = np.array([c["low"]    for c in dc], dtype=float)
        d_vo = np.array([c["volume"] for c in dc], dtype=float)
        n_d  = len(dc)
        current_price = float(d_cl[-1])

        d_ema20     = pd.Series(d_cl).ewm(span=20, adjust=False).mean().values
        above_d_ema = bool(d_cl[-1] > d_ema20[-1])
        if above_d_ema:
            d_score += 1
            criteria.append("Daily: Price above EMA-20 — short-term uptrend")

        d_atr14 = _atr_series(d_hi, d_lo, d_cl, period=14)

        # Bullish displacement: candle range > 1.5× ATR AND bullish close
        disp_found = False
        disp_date: str | None = None
        for k in range(max(1, n_d - 10), n_d):
            bar_range = float(d_hi[k] - d_lo[k])
            if bar_range > 1.5 * float(d_atr14[k]) and d_cl[k] > d_cl[k - 1]:
                disp_found = True
                disp_date  = dc[k]["date"]
                break
        if disp_found:
            d_score += 1
            criteria.append(f"Daily: Bullish displacement candle on {disp_date} (range > 1.5× ATR-14)")

        # Bullish FVG: highs[j-1] < lows[j+1]
        fvg_found = False
        for j in range(max(1, n_d - 15), n_d - 1):
            if d_hi[j - 1] < d_lo[j + 1]:
                fvg_found = True
                fvg_zone  = [round(float(d_hi[j - 1]), 2), round(float(d_lo[j + 1]), 2)]
                break
        if fvg_found:
            d_score += 1
            criteria.append(f"Daily: Unmitigated bullish FVG at {fvg_zone}")

        # Volume spike: 5-day avg vs 20-day avg
        vol_20d   = float(np.mean(d_vo[-20:]))
        vol_5d    = float(np.mean(d_vo[-5:])) if n_d >= 5 else 0.0
        vol_ratio = round(vol_5d / vol_20d, 2) if vol_20d > 0 else 0.0
        vol_spike = vol_ratio >= 1.3
        if vol_spike:
            d_score += 1
            criteria.append(f"Daily: Volume surge — 5-day avg is {vol_ratio:.1f}× the 20-day avg")

        # Trigger price: nearest daily BSL above current price
        d_bsl_above = sorted(
            [h for h in _swing_high_levels(d_hi, 5) if h > current_price]
        )
        trigger_price = round(d_bsl_above[0], 2) if d_bsl_above else None

        daily_status = {
            "trend":               "BULLISH" if above_d_ema else "BEARISH",
            "above_ema20":         above_d_ema,
            "ema20":               round(float(d_ema20[-1]), 2),
            "atr14":               round(float(d_atr14[-1]), 2),
            "displacement_candle": disp_found,
            "displacement_date":   disp_date,
            "fvg_present":         fvg_found,
            "fvg_zone":            fvg_zone,
            "volume_spike":        vol_spike,
            "volume_ratio_5d_20d": vol_ratio,
            "score":               d_score,
        }

    score += d_score

    # ── Conviction ────────────────────────────────────────────────
    if score >= 8:
        conviction = "HIGH"
    elif score >= 5:
        conviction = "MODERATE"
    else:
        conviction = "LOW"

    # High-conviction only when M + W both explicitly BULLISH
    high_conviction_confirmed = (
        conviction == "HIGH"
        and monthly_status.get("trend") == "BULLISH"
        and weekly_status.get("trend") == "BULLISH"
    )

    # ── Why Now? one-liner ────────────────────────────────────────
    why_parts: list[str] = []
    if monthly_status.get("trend") == "BULLISH":
        why_parts.append("Monthly macro bullish")
    if weekly_status.get("volatility_contraction"):
        why_parts.append("Weekly coiling (VC)")
    if weekly_status.get("near_bsl"):
        why_parts.append(f"approaching W-BSL {weekly_status.get('nearest_bsl')}")
    if daily_status.get("displacement_candle"):
        why_parts.append(f"Daily displacement ({daily_status.get('displacement_date')})")
    if daily_status.get("fvg_present"):
        why_parts.append("FVG imbalance")
    if daily_status.get("volume_spike"):
        v = daily_status.get("volume_ratio_5d_20d", 0)
        why_parts.append(f"volume surge {v:.1f}×")
    why_now = " + ".join(why_parts) if why_parts else "No strong confluence detected"

    return {
        "ticker":                    ticker.upper(),
        "resolved_symbol":           symbol,
        "current_price":             round(current_price, 2) if current_price is not None else None,
        "breakout_score":            score,
        "max_possible_score":        10,
        "conviction":                conviction,
        "high_conviction_confirmed": high_conviction_confirmed,
        "trigger_price":             trigger_price,
        "why_now":                   why_now,
        "criteria_met":              criteria,
        "timeframe_alignment": {
            "monthly": monthly_status,
            "weekly":  weekly_status,
            "daily":   daily_status,
        },
    }


# ──────────────────────────────────────────────────────────────
#  ICT Kill Zone constants  (all times in IST minutes-from-midnight)
# ──────────────────────────────────────────────────────────────

_ICT_KILL_ZONES: list[tuple[str, int, int]] = [
    ("Opening KZ",   9 * 60 + 15, 10 * 60 + 15),   # 09:15–10:15 IST
    ("Midday KZ",   12 * 60 + 30, 13 * 60 + 30),   # 12:30–13:30 IST
    ("Closing KZ",  14 * 60 + 30, 15 * 60 + 30),   # 14:30–15:30 IST
]
_IST_OFFSET_MIN = 330  # UTC + 5 h 30 min


def _utc_to_ist_min(dt: datetime) -> int:
    """Return IST total minutes since midnight for a UTC datetime."""
    return (dt.hour * 60 + dt.minute + _IST_OFFSET_MIN) % (24 * 60)


def _get_kill_zone(dt: datetime) -> str | None:
    """Return kill-zone name for a UTC timestamp, or None if outside all zones."""
    ist_min = _utc_to_ist_min(dt)
    for name, start, end in _ICT_KILL_ZONES:
        if start <= ist_min < end:
            return name
    return None


# ──────────────────────────────────────────────────────────────
#  run_ict_backtest
# ──────────────────────────────────────────────────────────────

def run_ict_backtest(
    ticker: str,
    days: int = 60,
    interval: str = "15m",
    min_rr: float = 3.0,
) -> dict[str, Any]:
    """
    Comprehensive ICT (Inner Circle Trader) intraday backtest.

    Scans three setup families:
    ────────────────────────────────────────────────────────────
    1. KZ_SWEEP_FVG   — SSL/BSL sweep occurring within an ICT Kill
                        Zone window, followed by a 3-candle FVG
                        formation.  Entry = FVG midpoint.
                        Kill Zones (IST): Opening 09:15–10:15,
                        Midday 12:30–13:30, Closing 14:30–15:30.

    2. ORDER_BLOCK    — Displacement candle (range > 1.5× ATR-14)
                        creates a Bullish or Bearish Order Block
                        (last opposite-colour candle before the
                        displacement).  Entry = OB 50% when price
                        retraces into the zone within 30 bars.

    3. TURTLE_SOUP    — Sweep of a prior swing by ≥ 0.3× ATR that
                        immediately reverses: close of the sweep
                        candle is back inside the prior range.
                        Entry = close of sweep candle (no FVG wait).
    ────────────────────────────────────────────────────────────
    All three patterns share:
      • ATR-adaptive SL : max(price × 0.15%, ATR × 0.25) beyond sweep
      • Walk-forward WIN / LOSS / OPEN simulation
      • RR gate         : only records where reward/risk ≥ min_rr
      • Deduplication   : (trigger_bar, direction) seen-set prevents
                          the same candle being recorded twice

    Parameters
    ----------
    ticker   : str   — Asset symbol or alias.
    days     : int   — Lookback (clamped to yfinance cap for interval).
    interval : str   — Candle interval (1m/5m/15m/30m/60m).
    min_rr   : float — Minimum RR to record a setup (default 3.0).

    Returns
    -------
    JSON-serialisable dict with per-trade records, aggregate stats,
    equity curve, pattern_breakdown, kill_zone_breakdown, and a
    truncation note if the requested period was shortened.
    """
    interval = interval.lower().strip()
    if interval not in _VALID_INTERVALS:
        logger.warning("Invalid interval '%s'; defaulting to 15m", interval)
        interval = "15m"

    max_days    = _INTRADAY_MAX_DAYS[interval]
    actual_days = min(max(int(days), 5), max_days)
    truncated   = actual_days < int(days)

    symbol = _resolve_ticker(ticker)
    logger.info(
        "ICT backtest: %s (%s) interval=%s days=%d (req=%d)",
        ticker, symbol, interval, actual_days, days,
    )

    try:
        df: pd.DataFrame = yf.download(
            symbol,
            period=f"{actual_days}d",
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.exception("yfinance download failed for %s", symbol)
        return {"error": f"Failed to fetch data for {symbol}: {exc}"}

    if df.empty:
        return {"error": f"No intraday data returned for {symbol}."}

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index)

    opens  = df["Open"].values.astype(float)
    highs  = df["High"].values.astype(float)
    lows   = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    times  = df.index
    n      = len(df)

    HALF        = 5      # swing detection half-window (bars each side)
    SL_BUFF     = 0.0015 # price-% SL floor (0.15%)
    ATR_PERIOD  = 14
    MIN_RR      = float(min_rr)
    DISP_MULT   = 1.5    # displacement = bar range > 1.5× ATR
    OB_LOOKBACK = 30     # max bars after displacement to wait for OB mitigation
    SOUP_EXT    = 0.3    # Turtle Soup: sweep extension must be ≥ 0.3× ATR

    # ── Wilder ATR ──────────────────────────────────────────────────
    _hs = pd.Series(highs); _ls = pd.Series(lows); _cs = pd.Series(closes)
    _pc = _cs.shift(1).fillna(_cs)
    _tr = pd.concat([_hs - _ls, (_hs - _pc).abs(), (_ls - _pc).abs()], axis=1).max(axis=1)
    atr_arr = _tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean().values
    del _hs, _ls, _cs, _pc, _tr

    # ── Swing high / low index lists ────────────────────────────────
    swing_high_idx: list[int] = []
    swing_low_idx:  list[int] = []
    for i in range(HALF, n - HALF):
        if highs[i] == float(np.max(highs[i - HALF: i + HALF + 1])):
            swing_high_idx.append(i)
        if lows[i]  == float(np.min(lows [i - HALF: i + HALF + 1])):
            swing_low_idx.append(i)

    trades: list[dict[str, Any]] = []
    seen:   set[tuple[int, str]] = set()  # (trigger_bar_idx, direction)

    # ── IST time string helper ───────────────────────────────────────
    def _ist_str(dt) -> str:
        m = _utc_to_ist_min(dt)
        return f"{m // 60:02d}:{m % 60:02d} IST"

    # ── Shared trade recorder ────────────────────────────────────────
    def _record(
        setup_type: str,
        direction:  str,
        trigger_i:  int,
        entry:      float,
        sl:         float,
        target:     float,
        atr:        float,
        kill_zone:  str | None = None,
        extra:      dict | None = None,
    ) -> None:
        key = (trigger_i, direction)
        if key in seen:
            return
        risk   = abs(entry - sl)
        reward = abs(target - entry)
        if risk <= 0:
            return
        rr = round(reward / risk, 2)
        if rr < MIN_RR:
            return
        seen.add(key)
        outcome, exit_px = _simulate_trade(
            direction, entry, sl, target, highs, lows, trigger_i + 1, n,
        )
        rec: dict[str, Any] = {
            "setup_type":   setup_type,
            "direction":    direction,
            "trigger_time": times[trigger_i].strftime("%Y-%m-%d %H:%M"),
            "ist_time":     _ist_str(times[trigger_i]),
            "kill_zone":    kill_zone,
            "entry":        round(entry,  2),
            "stop_loss":    round(sl,     2),
            "target":       round(target, 2),
            "risk_pts":     round(risk,   2),
            "reward_pts":   round(reward, 2),
            "rr":           rr,
            "atr_at_setup": round(float(atr), 2),
            "outcome":      outcome,
            "exit_price":   round(exit_px, 2) if exit_px is not None else None,
        }
        if extra:
            rec.update(extra)
        trades.append(rec)

    # ════════════════════════════════════════════════════════════════
    #  PATTERN 1 — Kill Zone Sweep + FVG
    # ════════════════════════════════════════════════════════════════
    for i in range(HALF + 1, n - HALF - 3):
        kz = _get_kill_zone(times[i])
        if kz is None:
            continue

        atr = float(atr_arr[i])

        # SHORT: BSL sweep in Kill Zone → bearish FVG
        prior_sh = [idx for idx in swing_high_idx if idx < i - 1]
        if prior_sh:
            sh_lvl = float(highs[prior_sh[-1]])
            if highs[i] > sh_lvl and closes[i] < sh_lvl:
                for j in range(i, min(i + 5, n - 1)):
                    if j >= 1 and (j + 1) < n and lows[j - 1] > highs[j + 1]:
                        fvg_top = float(lows[j - 1]); fvg_bot = float(highs[j + 1])
                        entry   = round((fvg_top + fvg_bot) / 2, 2)
                        sl      = round(float(highs[i]) + max(float(highs[i]) * SL_BUFF, atr * 0.25), 2)
                        cands   = [lows[k] for k in swing_low_idx if k < i and lows[k] < entry]
                        if cands:
                            _record("KZ_BSL_SWEEP_FVG", "SHORT", j, entry, sl,
                                    round(max(cands), 2), atr, kz,
                                    {"swept_level": round(sh_lvl, 2),
                                     "fvg_zone": [round(fvg_top, 2), round(fvg_bot, 2)]})
                        break

        # LONG: SSL sweep in Kill Zone → bullish FVG
        prior_sl = [idx for idx in swing_low_idx if idx < i - 1]
        if prior_sl:
            sl_lvl = float(lows[prior_sl[-1]])
            if lows[i] < sl_lvl and closes[i] > sl_lvl:
                for j in range(i, min(i + 5, n - 1)):
                    if j >= 1 and (j + 1) < n and highs[j - 1] < lows[j + 1]:
                        fvg_bot = float(highs[j - 1]); fvg_top = float(lows[j + 1])
                        entry   = round((fvg_top + fvg_bot) / 2, 2)
                        sl      = round(float(lows[i]) - max(float(lows[i]) * SL_BUFF, atr * 0.25), 2)
                        cands   = [highs[k] for k in swing_high_idx if k < i and highs[k] > entry]
                        if cands:
                            _record("KZ_SSL_SWEEP_FVG", "LONG", j, entry, sl,
                                    round(min(cands), 2), atr, kz,
                                    {"swept_level": round(sl_lvl, 2),
                                     "fvg_zone": [round(fvg_bot, 2), round(fvg_top, 2)]})
                        break

    # ════════════════════════════════════════════════════════════════
    #  PATTERN 2 — Order Block  (displacement → OB → mitigation)
    # ════════════════════════════════════════════════════════════════
    for i in range(ATR_PERIOD + 1, n - 2):
        atr       = float(atr_arr[i])
        bar_range = float(highs[i] - lows[i])

        if bar_range < DISP_MULT * atr:
            continue  # not a displacement candle

        # ── Bullish displacement → Bullish OB = last bearish candle before i ──
        if closes[i] > opens[i] and closes[i] > closes[i - 1]:
            ob_idx: int | None = None
            for k in range(i - 1, max(i - 10, 0), -1):
                if closes[k] < opens[k]:
                    ob_idx = k
                    break
            if ob_idx is not None:
                ob_hi  = float(highs[ob_idx]); ob_lo = float(lows[ob_idx])
                ob_mid = round((ob_hi + ob_lo) / 2, 2)
                for j in range(i + 1, min(i + OB_LOOKBACK, n)):
                    if lows[j] <= ob_hi and closes[j] >= ob_lo:
                        sl    = round(ob_lo - max(ob_lo * SL_BUFF, atr * 0.25), 2)
                        cands = [highs[k2] for k2 in swing_high_idx if k2 < i and highs[k2] > ob_mid]
                        if cands:
                            kz = _get_kill_zone(times[j])
                            _record("ORDER_BLOCK_LONG", "LONG", j, ob_mid, sl,
                                    round(min(cands), 2), atr, kz,
                                    {"ob_zone": [round(ob_lo, 2), round(ob_hi, 2)],
                                     "ob_candle_time": times[ob_idx].strftime("%Y-%m-%d %H:%M"),
                                     "displacement_time": times[i].strftime("%Y-%m-%d %H:%M")})
                        break  # first mitigation only

        # ── Bearish displacement → Bearish OB = last bullish candle before i ──
        if closes[i] < opens[i] and closes[i] < closes[i - 1]:
            ob_idx = None
            for k in range(i - 1, max(i - 10, 0), -1):
                if closes[k] > opens[k]:
                    ob_idx = k
                    break
            if ob_idx is not None:
                ob_hi  = float(highs[ob_idx]); ob_lo = float(lows[ob_idx])
                ob_mid = round((ob_hi + ob_lo) / 2, 2)
                for j in range(i + 1, min(i + OB_LOOKBACK, n)):
                    if highs[j] >= ob_lo and closes[j] <= ob_hi:
                        sl    = round(ob_hi + max(ob_hi * SL_BUFF, atr * 0.25), 2)
                        cands = [lows[k2] for k2 in swing_low_idx if k2 < i and lows[k2] < ob_mid]
                        if cands:
                            kz = _get_kill_zone(times[j])
                            _record("ORDER_BLOCK_SHORT", "SHORT", j, ob_mid, sl,
                                    round(max(cands), 2), atr, kz,
                                    {"ob_zone": [round(ob_lo, 2), round(ob_hi, 2)],
                                     "ob_candle_time": times[ob_idx].strftime("%Y-%m-%d %H:%M"),
                                     "displacement_time": times[i].strftime("%Y-%m-%d %H:%M")})
                        break

    # ════════════════════════════════════════════════════════════════
    #  PATTERN 3 — Turtle Soup  (sweep ≥ 0.3× ATR + immediate reversal)
    # ════════════════════════════════════════════════════════════════
    for i in range(HALF + 1, n - 3):
        atr = float(atr_arr[i])

        # SHORT Turtle Soup: sweep prior swing HIGH, close back below
        prior_sh = [idx for idx in swing_high_idx if idx < i - 1]
        if prior_sh:
            sh_lvl    = float(highs[prior_sh[-1]])
            extension = float(highs[i]) - sh_lvl
            if highs[i] > sh_lvl and closes[i] < sh_lvl and extension >= SOUP_EXT * atr:
                sl    = round(float(highs[i]) + max(float(highs[i]) * SL_BUFF, atr * 0.25), 2)
                entry = round(float(closes[i]), 2)
                cands = [lows[k] for k in swing_low_idx if k < i and lows[k] < entry]
                if cands:
                    kz = _get_kill_zone(times[i])
                    _record("TURTLE_SOUP_SHORT", "SHORT", i, entry, sl,
                            round(max(cands), 2), atr, kz,
                            {"swept_level": round(sh_lvl, 2),
                             "extension_pts": round(extension, 2)})

        # LONG Turtle Soup: sweep prior swing LOW, close back above
        prior_sl = [idx for idx in swing_low_idx if idx < i - 1]
        if prior_sl:
            sl_lvl    = float(lows[prior_sl[-1]])
            extension = sl_lvl - float(lows[i])
            if lows[i] < sl_lvl and closes[i] > sl_lvl and extension >= SOUP_EXT * atr:
                sl    = round(float(lows[i]) - max(float(lows[i]) * SL_BUFF, atr * 0.25), 2)
                entry = round(float(closes[i]), 2)
                cands = [highs[k] for k in swing_high_idx if k < i and highs[k] > entry]
                if cands:
                    kz = _get_kill_zone(times[i])
                    _record("TURTLE_SOUP_LONG", "LONG", i, entry, sl,
                            round(min(cands), 2), atr, kz,
                            {"swept_level": round(sl_lvl, 2),
                             "extension_pts": round(extension, 2)})

    # ── Aggregate stats ──────────────────────────────────────────────
    completed = [t for t in trades if t["outcome"] != "OPEN"]
    wins      = [t for t in completed if t["outcome"] == "WIN"]
    losses    = [t for t in completed if t["outcome"] == "LOSS"]

    win_rate = round(len(wins) / len(completed) * 100, 1) if completed else 0.0
    avg_rr   = round(sum(t["rr"] for t in trades) / len(trades), 2) if trades else 0.0

    equity: list[float] = [0.0]
    for t in completed:
        equity.append(round(equity[-1] + (t["rr"] if t["outcome"] == "WIN" else -1.0), 2))

    total_win_r  = sum(t["rr"] for t in completed if t["outcome"] == "WIN")
    total_loss_r = float(len(losses))
    profit_factor = round(total_win_r / total_loss_r, 2) if total_loss_r > 0 else None

    peak = max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    max_drawdown_r = round(max_dd, 2)

    expectancy_r = round(
        (win_rate / 100.0 * avg_rr) - ((1.0 - win_rate / 100.0) * 1.0), 2
    ) if trades else 0.0

    # ── Breakdown by pattern type ────────────────────────────────────
    pattern_breakdown: dict[str, dict] = {}
    for t in trades:
        pt  = t["setup_type"]
        pb  = pattern_breakdown.setdefault(
            pt, {"total": 0, "wins": 0, "losses": 0, "open_trades": 0,
                 "win_rate_pct": 0.0, "avg_rr": 0.0},
        )
        pb["total"] += 1
        if   t["outcome"] == "WIN":  pb["wins"]        += 1
        elif t["outcome"] == "LOSS": pb["losses"]      += 1
        else:                         pb["open_trades"] += 1

    for pt, pb in pattern_breakdown.items():
        closed = pb["wins"] + pb["losses"]
        pb["win_rate_pct"] = round(pb["wins"] / closed * 100, 1) if closed else 0.0
        pt_rrs = [t["rr"] for t in trades if t["setup_type"] == pt]
        pb["avg_rr"] = round(sum(pt_rrs) / len(pt_rrs), 2) if pt_rrs else 0.0

    # ── Breakdown by kill zone ───────────────────────────────────────
    kz_breakdown: dict[str, dict] = {}
    for t in trades:
        kz = t.get("kill_zone") or "Outside KZ"
        kb = kz_breakdown.setdefault(
            kz, {"total": 0, "wins": 0, "losses": 0, "open_trades": 0, "win_rate_pct": 0.0},
        )
        kb["total"] += 1
        if   t["outcome"] == "WIN":  kb["wins"]        += 1
        elif t["outcome"] == "LOSS": kb["losses"]      += 1
        else:                         kb["open_trades"] += 1

    for kz, kb in kz_breakdown.items():
        closed = kb["wins"] + kb["losses"]
        kb["win_rate_pct"] = round(kb["wins"] / closed * 100, 1) if closed else 0.0

    return {
        "ticker":              ticker.upper(),
        "resolved_symbol":     symbol,
        "interval":            interval,
        "requested_days":      days,
        "actual_days":         actual_days,
        "truncated":           truncated,
        "truncation_note":     (
            f"yfinance caps {interval} data at {max_days} days. "
            f"Requested {days} days — fetched {actual_days} days."
        ) if truncated else None,
        "total_candles":       n,
        "total_setups":        len(trades),
        "completed_setups":    len(completed),
        "wins":                len(wins),
        "losses":              len(losses),
        "open_trades":         len(trades) - len(completed),
        "win_rate_pct":        win_rate,
        "avg_rr":              avg_rr,
        "profit_factor":       profit_factor,
        "max_drawdown_r":      max_drawdown_r,
        "expectancy_r":        expectancy_r,
        "min_rr_filter":       MIN_RR,
        "equity_curve":        equity,
        "patterns_scanned":    ["KZ_SWEEP_FVG", "ORDER_BLOCK", "TURTLE_SOUP"],
        "kill_zones_used":     [name for name, _, _ in _ICT_KILL_ZONES],
        "pattern_breakdown":   pattern_breakdown,
        "kill_zone_breakdown": kz_breakdown,
        "trades":              trades,
    }


# ──────────────────────────────────────────────────────────────
#  scan_for_reversal
# ──────────────────────────────────────────────────────────────

def scan_for_reversal(ticker: str) -> dict[str, Any]:
    """
    Score a ticker on a 1–10 Multi-Timeframe Bearish-to-Bullish Reversal
    scale using SMC/ICT criteria across Monthly, Weekly, and Daily charts.

    Designed to find BEARISH stocks that are showing early signs of
    institutional accumulation and a potential bottom formation.

    Scoring breakdown (10 pts max)
    --------------------------------
    Monthly (3 pts) — confirm bearish trend + early recovery signals:
      +1  Price BELOW Monthly EMA-6 (bearish macro) AND near Monthly SSL ≤ 5%
          (price in bearish trend but sitting at key demand / support zone)
      +1  Most recent monthly candle closed BULLISH (close > open)
          (first sign of monthly demand — institutions beginning to absorb)
      +1  Price within 20% of 12-month low (deep value / capitulation zone)

    Weekly (3 pts) — weekly bottom formation:
      +1  Weekly SSL swept in last 3 weeks (stop hunt complete — sells absorbed)
      +1  Most recent 2 weekly candles: at least 1 closed bullish (recovery started)
      +1  Accumulation volume: bullish week in last 4 weeks with volume ≥ 1.3× 20-week avg

    Daily (4 pts) — immediate trigger signals:
      +1  Daily SSL swept in last 5 sessions (intraday stop hunt — micro bottom)
      +1  Bullish displacement candle in last 5 sessions (range > 1.5× ATR-14, bullish)
      +1  Unmitigated bullish FVG in last 10 bars (imbalance = entry zone)
      +1  Volume spike on a bullish day in last 5 sessions (≥ 1.5× 20-day avg)

    Conviction tiers
    ----------------
    8–10  HIGH     (high_reversal_confirmed when monthly below EMA-6 + weekly SSL swept + daily SSL swept)
    5–7   MODERATE (Watch for confirmation — 1-2 signals missing)
    1–4   LOW      (No reversal confluence — avoid)

    The ``entry_zone`` is the bullish FVG zone or current price.
    The ``stop_level`` is below the daily SSL sweep wick.
    The ``target_level`` is the nearest daily BSL above current price.

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC", "GOLD").

    Returns
    -------
    JSON dict with reversal_score, conviction, high_reversal_confirmed,
    entry_zone, stop_level, target_level, why_now, criteria_met list,
    and timeframe_alignment matrix.
    """
    symbol = _resolve_ticker(ticker)
    logger.info("Reversal scan: %s (%s)", ticker, symbol)

    mtf = get_multi_timeframe_data(ticker)

    score = 0
    criteria:       list[str]          = []
    current_price:  float | None       = None
    entry_zone:     list[float] | None = None
    stop_level:     float | None       = None
    target_level:   float | None       = None
    fvg_zone:       list[float] | None = None

    monthly_status: dict[str, Any] = {"trend": "UNKNOWN", "score": 0}
    weekly_status:  dict[str, Any] = {"trend": "UNKNOWN", "score": 0}
    daily_status:   dict[str, Any] = {"trend": "UNKNOWN", "score": 0}

    def _atr_s(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
        hs = pd.Series(h); ls = pd.Series(l); cs = pd.Series(c)
        pc = cs.shift(1).fillna(cs)
        tr = pd.concat([hs - ls, (hs - pc).abs(), (ls - pc).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1.0 / period, adjust=False).mean().values

    def _sw_lo(lows_a: np.ndarray, half: int) -> list[float]:
        n = len(lows_a)
        return [float(lows_a[i]) for i in range(half, n - half)
                if lows_a[i] == float(np.min(lows_a[i - half: i + half + 1]))]

    def _sw_hi(highs_a: np.ndarray, half: int) -> list[float]:
        n = len(highs_a)
        return [float(highs_a[i]) for i in range(half, n - half)
                if highs_a[i] == float(np.max(highs_a[i - half: i + half + 1]))]

    # ── MONTHLY (max 3 pts) ───────────────────────────────────────
    m_score = 0
    m_data  = mtf.get("monthly", {})
    if "candles" in m_data and len(m_data["candles"]) >= 6:
        mc      = m_data["candles"]
        m_cl    = np.array([c["close"] for c in mc], dtype=float)
        m_lo    = np.array([c["low"]   for c in mc], dtype=float)
        current_price = float(m_cl[-1])

        m_ema6      = pd.Series(m_cl).ewm(span=6, adjust=False).mean().values
        below_m_ema = bool(m_cl[-1] < m_ema6[-1])

        # Criterion 1: below EMA-6 AND near Monthly SSL within 5%
        m_ssl_levels = [l for l in _sw_lo(m_lo, 2)
                        if current_price * 0.95 <= l < current_price]
        near_m_ssl   = bool(m_ssl_levels) and below_m_ema
        if near_m_ssl:
            m_score += 1
            nearest_m_ssl = max(m_ssl_levels)
            criteria.append(
                f"Monthly: Below EMA-6 ({m_ema6[-1]:.2f}) + near SSL {nearest_m_ssl:.2f} "
                f"({abs(current_price - nearest_m_ssl) / current_price * 100:.1f}% away) — demand zone"
            )
        else:
            nearest_m_ssl = None

        # Criterion 2: most recent month closed bullish
        m_recent_bull = bool(mc[-1]["close"] > mc[-1]["open"])
        if m_recent_bull:
            m_score += 1
            criteria.append("Monthly: Most recent month closed BULLISH — institutional demand emerging")

        # Criterion 3: within 20% of 12-month low
        twelve_mo_low = float(np.min(m_lo[-12:])) if len(m_lo) >= 12 else float(np.min(m_lo))
        near_annual_low = bool(current_price <= twelve_mo_low * 1.20)
        if near_annual_low:
            m_score += 1
            pct_above = round((current_price / twelve_mo_low - 1.0) * 100, 1)
            criteria.append(
                f"Monthly: {pct_above}% above 12-month low {twelve_mo_low:.2f} — deep value / capitulation zone"
            )

        monthly_status = {
            "trend":                "BEARISH" if below_m_ema else "BULLISH",
            "reversal_candidate":   below_m_ema,
            "below_ema6":           below_m_ema,
            "ema6":                 round(float(m_ema6[-1]), 2),
            "near_ssl":             near_m_ssl,
            "nearest_ssl":          round(nearest_m_ssl, 2) if nearest_m_ssl else None,
            "recent_monthly_bull":  m_recent_bull,
            "twelve_month_low":     round(twelve_mo_low, 2),
            "near_annual_low":      near_annual_low,
            "score":                m_score,
        }

    score += m_score

    # ── WEEKLY (max 3 pts) ────────────────────────────────────────
    w_score = 0
    w_data  = mtf.get("weekly", {})
    if "candles" in w_data and len(w_data["candles"]) >= 8:
        wc   = w_data["candles"]
        w_cl = np.array([c["close"]  for c in wc], dtype=float)
        w_lo = np.array([c["low"]    for c in wc], dtype=float)
        w_vo = np.array([c["volume"] for c in wc], dtype=float)
        if current_price is None:
            current_price = float(w_cl[-1])

        # Criterion 1: weekly SSL swept in last 3 weeks
        w_ssl_swept       = False
        w_ssl_swept_level: float | None = None
        w_ssl_swept_date:  str   | None = None
        nw = len(wc)
        for k in range(2, nw - 1):
            if w_lo[k] == float(np.min(w_lo[max(0, k - 2): k + 3])):
                for j in range(max(k + 1, nw - 3), nw):
                    if w_lo[j] < w_lo[k] and w_cl[j] > w_lo[k]:
                        w_ssl_swept       = True
                        w_ssl_swept_level = round(float(w_lo[k]), 2)
                        w_ssl_swept_date  = wc[j]["date"]
                        break
            if w_ssl_swept:
                break

        if w_ssl_swept:
            w_score += 1
            criteria.append(
                f"Weekly: SSL swept at {w_ssl_swept_level} on {w_ssl_swept_date} — "
                "stop hunt complete, sells absorbed"
            )

        # Criterion 2: at least 1 of last 2 weekly candles closed bullish
        recent_w_bull = any(
            wc[k]["close"] > wc[k]["open"]
            for k in [-1, -2]
            if abs(k) <= len(wc)
        )
        if recent_w_bull:
            w_score += 1
            criteria.append("Weekly: Recent weekly candle closed bullish — recovery underway")

        # Criterion 3: accumulation volume (bullish week >= 1.3× 20-week avg)
        w_vol20 = float(np.mean(w_vo[-20:])) if len(w_vo) >= 20 else float(np.mean(w_vo))
        w_accum       = False
        w_accum_ratio = 0.0
        for k in range(max(0, nw - 4), nw):
            if wc[k]["close"] > wc[k]["open"] and w_vol20 > 0:
                ratio = float(w_vo[k]) / w_vol20
                if ratio >= 1.3:
                    w_accum       = True
                    w_accum_ratio = round(ratio, 2)
                    break

        if w_accum:
            w_score += 1
            criteria.append(
                f"Weekly: Accumulation volume {w_accum_ratio:.1f}× 20-week avg on bullish week — institutional buying"
            )

        w_ema20 = pd.Series(w_cl).ewm(span=20, adjust=False).mean().values
        weekly_status = {
            "trend":               "BEARISH" if float(w_cl[-1]) < float(w_ema20[-1]) else "BULLISH",
            "ssl_swept":           w_ssl_swept,
            "ssl_swept_level":     w_ssl_swept_level,
            "ssl_swept_date":      w_ssl_swept_date,
            "recent_bullish_close": recent_w_bull,
            "accumulation_volume": w_accum,
            "volume_ratio":        w_accum_ratio,
            "score":               w_score,
        }

    score += w_score

    # ── DAILY (max 4 pts) ─────────────────────────────────────────
    d_score = 0
    d_data  = mtf.get("daily", {})
    if "candles" in d_data and len(d_data["candles"]) >= 20:
        dc   = d_data["candles"]
        d_cl = np.array([c["close"]  for c in dc], dtype=float)
        d_hi = np.array([c["high"]   for c in dc], dtype=float)
        d_lo = np.array([c["low"]    for c in dc], dtype=float)
        d_op = np.array([c["open"]   for c in dc], dtype=float)
        d_vo = np.array([c["volume"] for c in dc], dtype=float)
        nd   = len(dc)
        current_price = float(d_cl[-1])

        d_ema20 = pd.Series(d_cl).ewm(span=20, adjust=False).mean().values
        d_atr14 = _atr_s(d_hi, d_lo, d_cl, 14)

        # Criterion 1: daily SSL swept in last 5 sessions
        swing_lows_d: list[tuple[int, float]] = [
            (k, float(d_lo[k]))
            for k in range(3, nd - 3)
            if d_lo[k] == float(np.min(d_lo[k - 3: k + 4]))
        ]
        d_ssl_swept       = False
        d_ssl_swept_level: float | None = None
        d_ssl_swept_date:  str   | None = None

        for j in range(max(0, nd - 5), nd):
            for (sl_idx, sl_val) in reversed(swing_lows_d):
                if sl_idx < j - 1 and d_lo[j] < sl_val and d_cl[j] > sl_val:
                    d_ssl_swept       = True
                    d_ssl_swept_level = round(sl_val, 2)
                    d_ssl_swept_date  = dc[j]["date"]
                    stop_level = round(
                        float(d_lo[j]) - max(float(d_lo[j]) * 0.001, float(d_atr14[j]) * 0.25),
                        2,
                    )
                    break
            if d_ssl_swept:
                break

        if d_ssl_swept:
            d_score += 1
            criteria.append(
                f"Daily: SSL swept at {d_ssl_swept_level} on {d_ssl_swept_date} — "
                "stop hunt complete, SL below sweep wick"
            )

        # Criterion 2: bullish displacement candle in last 5 sessions
        d_disp_found = False
        d_disp_date: str | None = None
        for k in range(max(1, nd - 5), nd):
            if (float(d_hi[k] - d_lo[k]) > 1.5 * float(d_atr14[k])
                    and float(d_cl[k]) > float(d_op[k])):
                d_disp_found = True
                d_disp_date  = dc[k]["date"]
                break

        if d_disp_found:
            d_score += 1
            criteria.append(
                f"Daily: Bullish displacement candle on {d_disp_date} "
                "(range > 1.5× ATR-14) — institutional buying surge"
            )

        # Criterion 3: bullish FVG in last 10 bars
        d_fvg_found = False
        for j in range(max(1, nd - 10), nd - 1):
            if float(d_hi[j - 1]) < float(d_lo[j + 1]):
                d_fvg_found = True
                fvg_zone    = [round(float(d_hi[j - 1]), 2), round(float(d_lo[j + 1]), 2)]
                entry_zone  = fvg_zone
                break

        if d_fvg_found:
            d_score += 1
            criteria.append(f"Daily: Bullish FVG at {fvg_zone} — imbalance acts as precision entry zone")

        # Criterion 4: volume spike on a bullish day in last 5 sessions
        d_vol20    = float(np.mean(d_vo[-20:])) if nd >= 20 else float(np.mean(d_vo))
        d_vol_spike = False
        d_vol_ratio = 0.0
        for k in range(max(0, nd - 5), nd):
            if float(d_cl[k]) > float(d_op[k]) and d_vol20 > 0:
                ratio = float(d_vo[k]) / d_vol20
                if ratio >= 1.5:
                    d_vol_spike = True
                    d_vol_ratio = round(ratio, 2)
                    break

        if d_vol_spike:
            d_score += 1
            criteria.append(
                f"Daily: Volume surge {d_vol_ratio:.1f}× 20-day avg on bullish candle — buying climax"
            )

        # Target: nearest daily BSL above current price
        d_bsl_above = sorted([
            float(d_hi[k]) for k in range(3, nd - 3)
            if d_hi[k] == float(np.max(d_hi[k - 3: k + 4]))
            and float(d_hi[k]) > current_price
        ])
        target_level = round(d_bsl_above[0], 2) if d_bsl_above else None

        # Fallback entry zone when no FVG
        if entry_zone is None:
            entry_zone = [round(current_price * 0.999, 2), round(current_price * 1.001, 2)]

        daily_status = {
            "trend":               "BEARISH" if float(d_cl[-1]) < float(d_ema20[-1]) else "BULLISH",
            "below_ema20":         bool(float(d_cl[-1]) < float(d_ema20[-1])),
            "ema20":               round(float(d_ema20[-1]), 2),
            "ssl_swept":           d_ssl_swept,
            "ssl_swept_level":     d_ssl_swept_level,
            "ssl_swept_date":      d_ssl_swept_date,
            "displacement_candle": d_disp_found,
            "displacement_date":   d_disp_date,
            "fvg_present":         d_fvg_found,
            "fvg_zone":            fvg_zone,
            "volume_spike":        d_vol_spike,
            "volume_ratio":        d_vol_ratio,
            "score":               d_score,
        }

    score += d_score

    # ── Conviction ────────────────────────────────────────────────
    if score >= 8:
        conviction = "HIGH"
    elif score >= 5:
        conviction = "MODERATE"
    else:
        conviction = "LOW"

    # Confirmed only when all three key reversal signals align
    high_reversal_confirmed = (
        conviction == "HIGH"
        and monthly_status.get("below_ema6", False)
        and (weekly_status.get("ssl_swept", False) or weekly_status.get("recent_bullish_close", False))
        and daily_status.get("ssl_swept", False)
    )

    # Why Now? one-liner
    why_parts: list[str] = []
    if monthly_status.get("below_ema6"):
        why_parts.append("Bearish macro (below M-EMA-6)")
    if monthly_status.get("near_ssl"):
        why_parts.append(f"near M-SSL {monthly_status.get('nearest_ssl')}")
    if monthly_status.get("recent_monthly_bull"):
        why_parts.append("Monthly demand bar")
    if monthly_status.get("near_annual_low"):
        why_parts.append("12-month low zone")
    if weekly_status.get("ssl_swept"):
        why_parts.append(f"W-SSL swept {weekly_status.get('ssl_swept_date')}")
    if weekly_status.get("recent_bullish_close"):
        why_parts.append("Weekly recovery close")
    if daily_status.get("ssl_swept"):
        why_parts.append(f"D-SSL swept {daily_status.get('ssl_swept_date')}")
    if daily_status.get("displacement_candle"):
        why_parts.append(f"Bullish displacement {daily_status.get('displacement_date')}")
    if daily_status.get("fvg_present"):
        why_parts.append("FVG imbalance")
    if daily_status.get("volume_spike"):
        why_parts.append(f"Vol surge {daily_status.get('volume_ratio')}×")
    why_now = " + ".join(why_parts) if why_parts else "Insufficient reversal confluence"

    return {
        "ticker":                     ticker.upper(),
        "resolved_symbol":            symbol,
        "current_price":              round(current_price, 2) if current_price is not None else None,
        "reversal_score":             score,
        "max_possible_score":         10,
        "conviction":                 conviction,
        "high_reversal_confirmed":    high_reversal_confirmed,
        "scan_type":                  "BEARISH_TO_BULLISH_REVERSAL",
        "entry_zone":                 entry_zone,
        "stop_level":                 stop_level,
        "target_level":               target_level,
        "why_now":                    why_now,
        "criteria_met":               criteria,
        "timeframe_alignment": {
            "monthly": monthly_status,
            "weekly":  weekly_status,
            "daily":   daily_status,
        },
    }


# ──────────────────────────────────────────────────────────────
#  scan_intraday_reversal
# ──────────────────────────────────────────────────────────────

def scan_intraday_reversal(
    ticker:   str,
    interval: str = "15m",
    days:     int = 5,
) -> dict[str, Any]:
    """
    Scan intraday candles for live bearish-to-bullish reversal setups.

    Designed for LIVE SESSION use. Detects when an intraday downtrend
    is reversing via three ICT patterns (LONG entries only):

    1. TURTLE_SOUP_LONG  — Sweep of prior intraday swing low by ≥ 0.1× ATR,
                           immediate reversal close above the swept level.
                           Entry = close of sweep candle.

    2. SSL_FVG_LONG      — SSL sweep followed by a 3-candle bullish FVG in
                           the next 5 bars. Entry = FVG midpoint.

    3. BULLISH_OB_LONG   — Bullish displacement candle (range > 1.5× ATR)
                           + last bearish candle before it as Order Block.
                           Entry = OB midpoint if price retraces into OB.

    All signals filtered to RR ≥ 2.0. Kill Zone context is attached to
    every signal (Opening 09:15–10:15 IST, Midday 12:30–13:30 IST,
    Closing 14:30–15:30 IST).

    Parameters
    ----------
    ticker   : str   — Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC").
    interval : str   — Candle interval (default "15m"). Supports all valid
                       yfinance intraday intervals.
    days     : int   — Number of calendar days to fetch (default 5,
                       clamped to yfinance limit per interval).

    Returns
    -------
    JSON dict with:
      reversal_signals  — list of LONG setups (entry, SL, target, RR, time)
      best_setup        — highest-RR signal from the list
      session_context   — trend vs session open, trend vs EMA-20, live price
      intraday_ssl_levels — nearest swing lows below current price (watch levels)
      intraday_bsl_levels — nearest swing highs above current price (targets)
      is_in_kill_zone   — current ICT Kill Zone name (or null)
      reversal_score    — 0-10 composite strength of current reversal signals
    """
    interval = interval.lower().strip()
    if interval not in _VALID_INTERVALS:
        logger.warning("Invalid interval '%s'; defaulting to 15m", interval)
        interval = "15m"

    max_days    = _INTRADAY_MAX_DAYS[interval]
    actual_days = min(max(int(days), 1), max_days)

    symbol = _resolve_ticker(ticker)
    logger.info(
        "Intraday reversal scan: %s (%s) interval=%s days=%d",
        ticker, symbol, interval, actual_days,
    )

    try:
        df: pd.DataFrame = yf.download(
            symbol,
            period=f"{actual_days}d",
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.exception("yfinance intraday reversal download failed for %s", symbol)
        return {"error": f"Failed to fetch data for {symbol}: {exc}"}

    if df.empty:
        return {
            "error": (
                f"No intraday data returned for {symbol}. "
                "Market may be closed or ticker invalid."
            )
        }

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index)

    opens  = df["Open"].values.astype(float)
    highs  = df["High"].values.astype(float)
    lows   = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    times  = df.index
    n      = len(df)

    if n < 10:
        return {"error": f"Insufficient intraday data for {symbol} — only {n} bars available."}

    HALF       = 3
    ATR_PERIOD = 14
    SL_BUFF    = 0.0015
    MIN_RR     = 2.0       # slightly relaxed for reversal entries
    DISP_MULT  = 1.5
    OB_RETRACE = 30        # max bars to wait for OB mitigation

    # ── ATR ─────────────────────────────────────────────────────────
    _hs = pd.Series(highs); _ls = pd.Series(lows); _cs = pd.Series(closes)
    _pc = _cs.shift(1).fillna(_cs)
    _tr = pd.concat([_hs - _ls, (_hs - _pc).abs(), (_ls - _pc).abs()], axis=1).max(axis=1)
    atr_arr = _tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean().values
    del _hs, _ls, _cs, _pc, _tr

    # ── EMA-20 ───────────────────────────────────────────────────────
    ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean().values

    # ── Swing highs / lows ───────────────────────────────────────────
    swing_high_idx: list[int] = []
    swing_low_idx:  list[int] = []
    for i in range(HALF, n - HALF):
        if highs[i] == float(np.max(highs[i - HALF: i + HALF + 1])):
            swing_high_idx.append(i)
        if lows[i]  == float(np.min(lows [i - HALF: i + HALF + 1])):
            swing_low_idx.append(i)

    # ── Today's session context ──────────────────────────────────────
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_idxs = [i for i, t in enumerate(times) if t.strftime("%Y-%m-%d") == today_str]
    if not today_idxs:
        today_str  = times[-1].strftime("%Y-%m-%d")
        today_idxs = [i for i, t in enumerate(times) if t.strftime("%Y-%m-%d") == today_str]

    session_context: dict[str, Any] = {"status": "UNKNOWN"}
    if today_idxs:
        sess_open    = float(opens [today_idxs[0]])
        live_price   = float(closes[today_idxs[-1]])
        sess_high    = float(np.max(highs[today_idxs]))
        sess_low     = float(np.min(lows [today_idxs]))
        vs_open      = "BEARISH" if live_price < sess_open  else "BULLISH"
        vs_ema       = "BEARISH" if live_price < float(ema20[-1]) else "BULLISH"
        sess_trend   = (
            "BEARISH" if vs_open == "BEARISH" and vs_ema == "BEARISH" else
            "BULLISH" if vs_open == "BULLISH" and vs_ema == "BULLISH" else
            "NEUTRAL"
        )
        session_context = {
            "session_date":        today_str,
            "session_open":        round(sess_open,  2),
            "session_high":        round(sess_high,  2),
            "session_low":         round(sess_low,   2),
            "live_price":          round(live_price, 2),
            "ema20":               round(float(ema20[-1]), 2),
            "session_trend":       sess_trend,
            "trend_vs_open":       vs_open,
            "trend_vs_ema20":      vs_ema,
            "is_reversal_candidate": sess_trend == "BEARISH",
            "note": (
                "Session is BEARISH — reversal setups are actionable."
                if sess_trend == "BEARISH"
                else "Session not yet bearish — reversal signals are anticipatory only."
            ),
        }

    # ── Scan for reversal signals ────────────────────────────────────
    reversal_signals: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    scan_start = max(HALF + 1, n - 30)  # look back last 30 bars

    def _ist(dt: Any) -> str:
        m = _utc_to_ist_min(dt)
        return f"{m // 60:02d}:{m % 60:02d} IST"

    for i in range(scan_start, n - 1):
        atr = float(atr_arr[i])
        kz  = _get_kill_zone(times[i])

        # ─── PATTERN 1 & 2: SSL Sweep ─────────────────────────────
        prior_sl = [idx for idx in swing_low_idx if idx < i - 1]
        if prior_sl:
            sl_lvl    = float(lows[prior_sl[-1]])
            extension = sl_lvl - float(lows[i])

            if lows[i] < sl_lvl and closes[i] > sl_lvl and extension >= 0.1 * atr:

                # PATTERN 1 — Turtle Soup Long (entry = close of sweep candle)
                soup_entry = round(float(closes[i]), 2)
                soup_sl    = round(float(lows[i]) - max(float(lows[i]) * SL_BUFF, atr * 0.25), 2)
                bsl_c      = [highs[k] for k in swing_high_idx if k < i and highs[k] > soup_entry]
                if bsl_c and (i, "TURTLE_SOUP_LONG") not in seen:
                    tgt  = round(min(bsl_c), 2)
                    risk = soup_entry - soup_sl
                    rwd  = tgt - soup_entry
                    if risk > 0:
                        rr = round(rwd / risk, 2)
                        if rr >= MIN_RR:
                            seen.add((i, "TURTLE_SOUP_LONG"))
                            reversal_signals.append({
                                "setup_type":   "TURTLE_SOUP_LONG",
                                "signal_time":  times[i].strftime("%Y-%m-%d %H:%M"),
                                "ist_time":     _ist(times[i]),
                                "kill_zone":    kz,
                                "swept_ssl":    round(sl_lvl, 2),
                                "extension_pts": round(extension, 2),
                                "entry":        soup_entry,
                                "stop_loss":    soup_sl,
                                "target":       tgt,
                                "risk_pts":     round(risk, 2),
                                "reward_pts":   round(rwd,  2),
                                "rr":           rr,
                                "atr":          round(atr, 2),
                            })

                # PATTERN 2 — SSL + Bullish FVG (entry = FVG midpoint)
                for j in range(i, min(i + 5, n - 1)):
                    if j >= 1 and (j + 1) < n and highs[j - 1] < lows[j + 1]:
                        fvg_bot   = float(highs[j - 1])
                        fvg_top   = float(lows [j + 1])
                        fvg_entry = round((fvg_top + fvg_bot) / 2, 2)
                        fvg_sl    = round(
                            float(lows[i]) - max(float(lows[i]) * SL_BUFF, atr * 0.25), 2
                        )
                        bsl_c2 = [highs[k] for k in swing_high_idx if k < i and highs[k] > fvg_entry]
                        if bsl_c2 and (j, "SSL_FVG_LONG") not in seen:
                            tgt2  = round(min(bsl_c2), 2)
                            risk2 = fvg_entry - fvg_sl
                            rwd2  = tgt2 - fvg_entry
                            if risk2 > 0:
                                rr2 = round(rwd2 / risk2, 2)
                                if rr2 >= MIN_RR:
                                    seen.add((j, "SSL_FVG_LONG"))
                                    reversal_signals.append({
                                        "setup_type":  "SSL_FVG_LONG",
                                        "signal_time": times[j].strftime("%Y-%m-%d %H:%M"),
                                        "ist_time":    _ist(times[j]),
                                        "kill_zone":   _get_kill_zone(times[j]),
                                        "swept_ssl":   round(sl_lvl, 2),
                                        "fvg_zone":    [round(fvg_bot, 2), round(fvg_top, 2)],
                                        "entry":       fvg_entry,
                                        "stop_loss":   fvg_sl,
                                        "target":      tgt2,
                                        "risk_pts":    round(risk2, 2),
                                        "reward_pts":  round(rwd2,  2),
                                        "rr":          rr2,
                                        "atr":         round(atr, 2),
                                    })
                        break

        # ─── PATTERN 3: Bullish Order Block ───────────────────────
        bar_range = float(highs[i] - lows[i])
        if (bar_range > DISP_MULT * atr
                and float(closes[i]) > float(opens[i])
                and float(closes[i]) > float(closes[i - 1])):
            ob_idx: int | None = None
            for k in range(i - 1, max(i - 8, -1), -1):
                if float(closes[k]) < float(opens[k]):
                    ob_idx = k
                    break
            if ob_idx is not None:
                ob_hi  = float(highs[ob_idx])
                ob_lo  = float(lows [ob_idx])
                ob_mid = round((ob_hi + ob_lo) / 2, 2)
                # Check if price currently inside or touching the OB
                live_px = float(closes[-1])
                if ob_lo <= live_px <= ob_hi or (
                    i >= n - OB_RETRACE and ob_lo <= min(lows[i:]) <= ob_hi
                ):
                    ob_sl  = round(ob_lo - max(ob_lo * SL_BUFF, atr * 0.25), 2)
                    bsl_c3 = [highs[k] for k in swing_high_idx if k < i and highs[k] > ob_mid]
                    if bsl_c3 and (ob_idx, "BULLISH_OB_LONG") not in seen:
                        tgt3  = round(min(bsl_c3), 2)
                        risk3 = ob_mid - ob_sl
                        rwd3  = tgt3 - ob_mid
                        if risk3 > 0:
                            rr3 = round(rwd3 / risk3, 2)
                            if rr3 >= MIN_RR:
                                seen.add((ob_idx, "BULLISH_OB_LONG"))
                                reversal_signals.append({
                                    "setup_type":        "BULLISH_OB_LONG",
                                    "signal_time":       times[i].strftime("%Y-%m-%d %H:%M"),
                                    "ist_time":          _ist(times[i]),
                                    "kill_zone":         kz,
                                    "ob_zone":           [round(ob_lo, 2), round(ob_hi, 2)],
                                    "displacement_time": times[i].strftime("%Y-%m-%d %H:%M"),
                                    "entry":             ob_mid,
                                    "stop_loss":         ob_sl,
                                    "target":            tgt3,
                                    "risk_pts":          round(risk3, 2),
                                    "reward_pts":        round(rwd3,  2),
                                    "rr":                rr3,
                                    "atr":               round(atr, 2),
                                })

    # Sort most-recent first
    reversal_signals.sort(key=lambda x: x["signal_time"], reverse=True)

    best_setup = max(reversal_signals, key=lambda x: x["rr"]) if reversal_signals else None

    # Current SSL / BSL levels around live price
    live_px = float(closes[-1])
    ssl_levels = sorted(
        {round(float(lows[k]), 2) for k in swing_low_idx if float(lows[k]) < live_px},
        reverse=True,
    )[:5]
    bsl_levels = sorted(
        {round(float(highs[k]), 2) for k in swing_high_idx if float(highs[k]) > live_px},
    )[:5]

    current_kz = _get_kill_zone(times[-1]) if len(times) > 0 else None

    # Simple reversal score: quality signals + session context bonus
    qual_signals = [s for s in reversal_signals if s.get("kill_zone")]
    reversal_score = min(
        10,
        len(qual_signals) * 3
        + len([s for s in reversal_signals if s.get("kill_zone") is None]) * 1
        + (2 if session_context.get("is_reversal_candidate") else 0),
    )

    return {
        "ticker":               ticker.upper(),
        "resolved_symbol":      symbol,
        "interval":             interval,
        "period_days":          actual_days,
        "total_candles":        n,
        "current_price":        round(live_px, 2),
        "current_ema20":        round(float(ema20[-1]), 2),
        "is_in_kill_zone":      current_kz,
        "reversal_score":       reversal_score,
        "total_signals_found":  len(reversal_signals),
        "reversal_signals":     reversal_signals,
        "best_setup":           best_setup,
        "session_context":      session_context,
        "intraday_ssl_levels":  ssl_levels,
        "intraday_bsl_levels":  bsl_levels,
        "scan_lookback_bars":   n - scan_start,
        "min_rr_filter":        MIN_RR,
        "patterns_scanned":     ["TURTLE_SOUP_LONG", "SSL_FVG_LONG", "BULLISH_OB_LONG"],
        "kill_zones":           [name for name, _, _ in _ICT_KILL_ZONES],
    }
