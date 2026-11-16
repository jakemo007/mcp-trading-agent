"""Step 1: find the stocks "in play" by relative volume, using only what was known at each checkpoint.

Relative volume (RVOL) compares the volume traded so far today with the average
volume traded by the same clock time on the previous sessions. Rows dated after
`day`, and today's bars at or after the checkpoint, are never read.
"""
from __future__ import annotations

from datetime import date, time
from typing import Dict, List, NamedTuple, Optional

import numpy as np
import pandas as pd

from intraday_trader import config
from intraday_trader.data import clock_parts, day_number, minute_of_day

CRORE = 1e7


class _Window(NamedTuple):
    """One symbol's bars for `day` and the sessions just before it, as plain arrays."""

    session: np.ndarray     # per bar: 0 .. n_sessions-1, the last one is `day`
    minute: np.ndarray      # per bar: minute of the day (IST)
    volume: np.ndarray
    close: np.ndarray
    n_sessions: int         # previous sessions kept + 1
    prev_close: float       # last Close of the session before `day`


def _window(df5: Optional[pd.DataFrame], day: date) -> Optional[_Window]:
    """Cut out `day` and up to RVOL_LOOKBACK_SESSIONS earlier sessions. None if `day` has no bar
    or there is no earlier session. Rows after `day` are dropped here, once, for every caller."""
    if df5 is None or len(df5) == 0:
        return None
    days, minute = clock_parts(df5.index)
    target = day_number(day)
    today_from = int(np.searchsorted(days, target, side="left"))
    today_to = int(np.searchsorted(days, target, side="right"))     # rows after `day` start here
    if today_from == today_to or today_from == 0:
        return None

    earlier = np.unique(days[:today_from])[-int(config.RVOL_LOOKBACK_SESSIONS):]
    lo = int(np.searchsorted(days, earlier[0], side="left"))
    sessions = np.r_[earlier, target]
    return _Window(
        session=np.searchsorted(sessions, days[lo:today_to]),
        minute=minute[lo:today_to],
        volume=df5["Volume"].to_numpy(dtype=float)[lo:today_to],
        close=df5["Close"].to_numpy(dtype=float)[lo:today_to],
        n_sessions=len(sessions),
        prev_close=float(df5["Close"].iloc[today_from - 1]),
    )


def _measure(win: _Window, as_of_min: int) -> Optional[dict]:
    """The unrounded numbers for one symbol at one clock time, or None when it cannot be rated."""
    before = win.minute < as_of_min
    today = win.session == win.n_sessions - 1
    today_before = before & today
    if not today_before.any():
        return None

    volume = np.bincount(win.session[before], weights=win.volume[before], minlength=win.n_sessions)
    bars = np.bincount(win.session[before], minlength=win.n_sessions)
    # Average only over earlier sessions that actually have bars in this time window:
    # a session with a data hole in the morning would otherwise count as zero volume.
    usable = bars[:-1] > 0
    if int(usable.sum()) < int(config.RVOL_MIN_SESSIONS):
        return None
    avg = float(volume[:-1][usable].mean())
    if avg <= 0:
        return None

    cum = float(volume[-1])
    last_price = float(win.close[today_before][-1])
    return {
        "rvol": cum / avg,
        "cum_volume": cum,
        "avg_cum_volume": avg,
        "turnover_cr": float((win.close[today_before] * win.volume[today_before]).sum()) / CRORE,
        "last_price": last_price,
        "prev_close": win.prev_close,
        "day_change_pct": (last_price / win.prev_close - 1.0) * 100.0 if win.prev_close > 0 else 0.0,
    }


def _row(symbol: str, name: str, as_of: time, m: dict) -> dict:
    """Round for storage and display. Filtering and ranking happen on the unrounded values."""
    return {
        "symbol": symbol,
        "name": name,
        "as_of": as_of.strftime("%H:%M"),
        "rvol": round(m["rvol"], 2),
        "cum_volume": int(round(m["cum_volume"])),
        "avg_cum_volume": round(m["avg_cum_volume"], 1),
        "turnover_cr": round(m["turnover_cr"], 2),
        "last_price": round(m["last_price"], 2),
        "prev_close": round(m["prev_close"], 2),
        "day_change_pct": round(m["day_change_pct"], 2),
    }


def _ranked(windows: Dict[str, _Window], as_of: time) -> List[tuple]:
    """[(symbol, measurements)] for every rateable symbol, highest RVOL first (ties by symbol)."""
    as_of_min = minute_of_day(as_of)
    rated = []
    for symbol, win in windows.items():
        m = _measure(win, as_of_min)
        if m is not None:
            rated.append((symbol, m))
    rated.sort(key=lambda sm: (-sm[1]["rvol"], sm[0]))
    return rated


def _windows(bars5: Dict[str, pd.DataFrame], day: date) -> Dict[str, _Window]:
    out = {}
    for symbol, df5 in bars5.items():
        win = _window(df5, day)
        if win is not None:
            out[symbol] = win
    return out


def rvol_table(
    bars5: Dict[str, pd.DataFrame],
    day: date,
    as_of: time,
    names: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Relative volume of every symbol at clock time `as_of` on `day`.

    For each symbol with at least one bar on `day` that started before `as_of`
    and at least config.RVOL_MIN_SESSIONS earlier sessions with bars before
    `as_of`:

      cum_volume      sum of Volume over the day's bars with start < as_of
      avg_cum_volume  mean, over the previous config.RVOL_LOOKBACK_SESSIONS sessions
                      that have such bars, of the same sum (bars with start < as_of)
      rvol            cum_volume / avg_cum_volume   (symbol skipped if the average is 0)
      turnover_cr     sum(Close * Volume) over the same bars of `day`, / 1e7
      last_price      Close of the day's last bar with start < as_of
      prev_close      last Close of the previous session
      day_change_pct  (last_price / prev_close - 1) * 100

    Nothing after `as_of` on `day` may influence any field. Returns rows
    {symbol, name, as_of "HH:MM", rvol, cum_volume, avg_cum_volume, turnover_cr,
    last_price, prev_close, day_change_pct} sorted by rvol descending. `name`
    falls back to the symbol when `names` does not have it.
    """
    names = names or {}
    return [_row(s, names.get(s, s), as_of, m) for s, m in _ranked(_windows(bars5, day), as_of)]


def build_watchlist(
    bars5: Dict[str, pd.DataFrame],
    day: date,
    names: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Run the RVOL ranking at every config.SCAN_CHECKPOINTS time and collect the stocks in play.

    A checkpoint is skipped when the data for `day` has no bar starting at or
    after it (the session has not got that far). At each checkpoint keep rows
    with rvol >= MIN_RVOL, turnover_cr >= MIN_TURNOVER_CR and last_price >=
    MIN_PRICE, take the top WATCHLIST_PER_CHECKPOINT by rvol, and add those not
    already listed until the list holds WATCHLIST_MAX names.

    Returns WatchItems in the order added: the rvol_table row of the checkpoint
    at which the symbol first qualified, plus eligible_from ("HH:MM" of that
    checkpoint) and rank (1-based position among the rows that passed the
    filters at that checkpoint).
    """
    names = names or {}
    windows = _windows(bars5, day)      # sliced once, reused by every checkpoint
    if not windows:
        return []
    # How far the session has got: the latest bar start on `day` across all symbols.
    latest_min = max(int(w.minute[-1]) for w in windows.values())

    watchlist: List[dict] = []
    listed = set()
    for checkpoint in sorted(config.SCAN_CHECKPOINTS):
        if latest_min < minute_of_day(checkpoint) or len(watchlist) >= config.WATCHLIST_MAX:
            break
        in_play = [
            (symbol, m)
            for symbol, m in _ranked(windows, checkpoint)
            if m["rvol"] >= config.MIN_RVOL
            and m["turnover_cr"] >= config.MIN_TURNOVER_CR
            and m["last_price"] >= config.MIN_PRICE
        ]
        for rank, (symbol, m) in enumerate(in_play[: int(config.WATCHLIST_PER_CHECKPOINT)], start=1):
            if symbol in listed:
                continue
            if len(watchlist) >= config.WATCHLIST_MAX:
                break
            item = _row(symbol, names.get(symbol, symbol), checkpoint, m)
            item["eligible_from"] = checkpoint.strftime("%H:%M")
            item["rank"] = rank
            watchlist.append(item)
            listed.add(symbol)
    return watchlist
