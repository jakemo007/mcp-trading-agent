"""Market data: download from Yahoo Finance and reshape. The only module that touches the network.

Everything downstream relies on two promises made here:

* frames are clean (see fetch_5m / fetch_daily for the exact shape), and
* hourly_asof never returns a 1h candle that had not finished at the time asked
  about, which is what keeps the 1h analysis free of lookahead.
"""
from __future__ import annotations

import csv
import logging
import time as _time
from datetime import date, time, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from intraday_trader import config

logger = logging.getLogger(__name__)

IST = "Asia/Kolkata"
OHLCV = ["Open", "High", "Low", "Close", "Volume"]

BAR_MINUTES = 5              # width of the intraday bars this module produces
HOUR_MINUTES = 60
RETRY_BACKOFF_SEC = 2.0      # wait this many seconds times the attempt number before a retry

_EPOCH = date(1970, 1, 1)
_NS_PER_MINUTE = 60 * 1_000_000_000
_MINUTES_PER_DAY = 24 * 60


# ── Small clock helpers ───────────────────────────────────────────────────────

def minute_of_day(t: time) -> int:
    """09:15 -> 555."""
    return t.hour * 60 + t.minute


def day_number(day: date) -> int:
    """Days since 1970-01-01; matches the first array returned by clock_parts."""
    return (pd.Timestamp(day).date() - _EPOCH).days


def clock_parts(index: pd.DatetimeIndex) -> Tuple[np.ndarray, np.ndarray]:
    """(day_number, minute_of_day) of every stamp on the IST wall clock, as int64 arrays.

    A tz-aware index is converted to IST first; a tz-naive one is taken to be
    IST wall-clock time already.
    """
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST).tz_localize(None)
    minutes = idx.as_unit("ns").asi8 // _NS_PER_MINUTE
    return minutes // _MINUTES_PER_DAY, minutes % _MINUTES_PER_DAY


def _empty_5m() -> pd.DataFrame:
    return pd.DataFrame(
        {c: pd.Series(dtype=float) for c in OHLCV},
        index=pd.DatetimeIndex([], tz=IST),
    )


def _empty_daily() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=float) for c in OHLCV}, index=pd.DatetimeIndex([]))


# ── Universe ──────────────────────────────────────────────────────────────────

def load_universe(path: Path = config.UNIVERSE_FILE) -> List[Dict[str, str]]:
    """Read the universe CSV. Returns [{"symbol": "RELIANCE", "name": "Reliance Industries Ltd"}, ...]."""
    out: List[Dict[str, str]] = []
    seen = set()
    with open(str(path), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            symbol = clean.get("symbol", "")
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            out.append({"symbol": symbol, "name": clean.get("name", "") or symbol})
    return out


def yahoo_ticker(symbol: str) -> str:
    """NSE symbol -> Yahoo ticker: "M&M" -> "M&M.NS"."""
    s = symbol.strip().upper()
    return s if s.endswith(".NS") else s + ".NS"


# ── Cleaning (pure, tested without the network) ───────────────────────────────

def _ohlcv(raw: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """The five columns as floats, rows without a full O/H/L/C dropped. None if unusable."""
    if raw is None or len(raw) == 0:
        return None
    df = raw
    if isinstance(df.columns, pd.MultiIndex):
        # Yahoo returns (ticker, field) or (field, ticker); keep the level that holds the fields.
        for level in range(df.columns.nlevels):
            if "Close" in df.columns.get_level_values(level):
                df = df.copy()
                df.columns = df.columns.get_level_values(level)
                break
    if any(c not in df.columns for c in OHLCV[:4]):
        return None
    out = df.reindex(columns=OHLCV).apply(pd.to_numeric, errors="coerce").astype(float)
    out = out.dropna(subset=OHLCV[:4])
    out["Volume"] = out["Volume"].fillna(0.0)
    out.columns.name = None
    return out


def clean_5m(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Turn one ticker's raw Yahoo 5m frame into the shape promised by fetch_5m.

    A tz-naive index is taken to be UTC (Yahoo's intraday default).
    """
    df = _ohlcv(raw)
    if df is None or df.empty:
        return _empty_5m()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df.index = idx.tz_convert(IST)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    _, minute = clock_parts(df.index)
    open_min = minute_of_day(config.SESSION_OPEN)
    close_min = minute_of_day(config.SESSION_CLOSE)
    keep = (
        (minute >= open_min)
        & (minute < close_min)
        & ((minute - open_min) % BAR_MINUTES == 0)
        & (df.index.second == 0)
    )
    df = df[keep]
    df.index.name = "Datetime"
    return df if not df.empty else _empty_5m()


def clean_daily(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Turn one ticker's raw Yahoo daily frame into the shape promised by fetch_daily."""
    df = _ohlcv(raw)
    if df is None or df.empty:
        return _empty_daily()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST).tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "Date"
    return df


def split_by_ticker(raw: Optional[pd.DataFrame], tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """Cut a Yahoo multi-ticker download into {ticker: frame}; tickers with no columns are absent."""
    if raw is None or len(raw) == 0:
        return {}
    if not isinstance(raw.columns, pd.MultiIndex):
        return {tickers[0]: raw} if len(tickers) == 1 else {}
    wanted = set(tickers)
    for level in range(raw.columns.nlevels):
        present = set(raw.columns.get_level_values(level)) & wanted
        if present:
            return {t: raw.xs(t, axis=1, level=level) for t in tickers if t in present}
    return {tickers[0]: raw} if len(tickers) == 1 else {}


# ── Download ──────────────────────────────────────────────────────────────────

def _download_raw(tickers: List[str], period: str, interval: str) -> pd.DataFrame:
    """One Yahoo request. Kept tiny so tests can replace it."""
    import yfinance as yf  # imported here: slow to import, and only this function needs it

    return yf.download(
        tickers,
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )


def _fetch_chunk(
    chunk: List[str],
    period: str,
    interval: str,
    clean: Callable[[Optional[pd.DataFrame]], pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    """Download one chunk, retrying while the response looks like a failed request."""
    got: Dict[str, pd.DataFrame] = {}
    pending = list(chunk)
    attempts = 1 + max(0, int(config.DOWNLOAD_RETRIES))
    for attempt in range(1, attempts + 1):
        tickers = [yahoo_ticker(s) for s in pending]
        try:
            frames = split_by_ticker(_download_raw(tickers, period, interval), tickers)
        except Exception as exc:  # network, rate limit, parser errors inside yfinance
            logger.warning("Yahoo %s request failed (attempt %d/%d): %s", interval, attempt, attempts, exc)
            frames = {}
        for symbol, ticker in zip(pending, tickers):
            try:
                df = clean(frames.get(ticker))
            except Exception as exc:
                logger.warning("%s: could not clean %s data: %s", symbol, interval, exc)
                continue
            if not df.empty:
                got[symbol] = df
        pending = [s for s in pending if s not in got]
        if not pending:
            break
        # A few names missing from an otherwise good response have no data on Yahoo
        # (renamed, delisted). A mostly empty response is a failed request: retry it.
        mostly_ok = bool(got) and len(pending) <= max(1, len(chunk) // 4)
        if mostly_ok or attempt == attempts:
            break
        _time.sleep(RETRY_BACKOFF_SEC * attempt)
    return got


def _fetch(
    symbols: List[str],
    period: str,
    interval: str,
    clean: Callable[[Optional[pd.DataFrame]], pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    wanted = list(dict.fromkeys(s.strip() for s in symbols if s and s.strip()))
    size = max(1, int(config.DOWNLOAD_CHUNK))
    got: Dict[str, pd.DataFrame] = {}
    for start in range(0, len(wanted), size):
        got.update(_fetch_chunk(wanted[start:start + size], period, interval, clean))
    missing = [s for s in wanted if s not in got]
    if missing:
        logger.warning("No %s data for %d of %d symbols: %s", interval, len(missing), len(wanted), ", ".join(missing))
    return {s: got[s] for s in wanted if s in got}


def fetch_5m(symbols: List[str], period: str = config.LIVE_5M_PERIOD) -> Dict[str, pd.DataFrame]:
    """Download 5-minute bars. Returns {symbol: DataFrame} keyed by the bare NSE symbol.

    Each frame: tz-aware Asia/Kolkata index of bar START times, columns OHLCV as
    float, sorted, no duplicate stamps, rows with a missing O/H/L/C dropped,
    Volume NaN -> 0, and only regular-session bars kept (09:15 <= start < 15:30,
    on the 5-minute grid). Uses auto_adjust=False. Downloads config.DOWNLOAD_CHUNK
    tickers per request, retries a failed chunk config.DOWNLOAD_RETRIES times with
    a short backoff, and leaves out symbols that return nothing (logged at
    WARNING, never raised).
    """
    return _fetch(symbols, period, "5m", clean_5m)


def fetch_daily(symbols: List[str], period: str = config.DAILY_PERIOD) -> Dict[str, pd.DataFrame]:
    """Download daily bars. Returns {symbol: DataFrame} with a tz-naive, normalized
    DatetimeIndex (the session date), columns OHLCV as float, auto_adjust=False.
    Same chunking / retry / omission rules as fetch_5m.
    """
    return _fetch(symbols, period, "1d", clean_daily)


# ── Reshaping ─────────────────────────────────────────────────────────────────

def session_dates(df5: pd.DataFrame) -> List[date]:
    """Sorted list of the calendar dates (IST) that have at least one bar."""
    if df5 is None or len(df5) == 0:
        return []
    days, _ = clock_parts(df5.index)
    return [_EPOCH + timedelta(days=int(d)) for d in np.unique(days)]


def _day_bounds(index: pd.DatetimeIndex, day: date) -> Tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(pd.Timestamp(day).date())
    if index.tz is not None:
        start = start.tz_localize(IST).tz_convert(index.tz)
    return start, start + pd.Timedelta(days=1)


def day_bars(df5: pd.DataFrame, day: date) -> pd.DataFrame:
    """The 5m bars of one session (may be empty)."""
    if df5 is None:
        return _empty_5m()
    if len(df5) == 0:
        return df5
    start, end = _day_bounds(df5.index, day)
    lo = df5.index.searchsorted(start, side="left")
    hi = df5.index.searchsorted(end, side="left")
    return df5.iloc[lo:hi]


def to_hourly(df5: pd.DataFrame) -> pd.DataFrame:
    """Resample 5m bars into 60-minute bars anchored on the NSE open.

    Buckets start at 09:15, 10:15, ..., 14:15 and a final short 15:15 bucket.
    A bucket never spans two sessions. Open = first, High = max, Low = min,
    Close = last, Volume = sum. Buckets with no 5m bar are not emitted.
    Index = bucket start (tz-aware IST). Bars outside the regular session are
    ignored. The input must be sorted by time.
    """
    if df5 is None or len(df5) == 0:
        return _empty_5m()
    if df5.index.tz is not None and str(df5.index.tz) != IST:
        df5 = df5.tz_convert(IST)

    open_min = minute_of_day(config.SESSION_OPEN)
    close_min = minute_of_day(config.SESSION_CLOSE)
    days, minute = clock_parts(df5.index)
    in_session = (minute >= open_min) & (minute < close_min)
    if not in_session.all():
        df5, days, minute = df5[in_session], days[in_session], minute[in_session]
        if len(df5) == 0:
            return _empty_5m()

    # Start of each bar's bucket, in minutes of the day, then one key per (day, bucket).
    bucket_min = open_min + ((minute - open_min) // HOUR_MINUTES) * HOUR_MINUTES
    key = days * _MINUTES_PER_DAY + bucket_min
    first = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])   # first row of every bucket
    last = np.r_[first[1:], len(key)] - 1                      # last row of every bucket

    starts = df5.index[first] - pd.to_timedelta(minute[first] - bucket_min[first], unit="m")
    out = pd.DataFrame(
        {
            "Open": df5["Open"].to_numpy()[first],
            "High": np.maximum.reduceat(df5["High"].to_numpy(), first),
            "Low": np.minimum.reduceat(df5["Low"].to_numpy(), first),
            "Close": df5["Close"].to_numpy()[last],
            "Volume": np.add.reduceat(df5["Volume"].to_numpy(), first),
        },
        index=pd.DatetimeIndex(starts),
    )
    out.index.name = df5.index.name
    return out


def hourly_asof(df5: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Completed 1h bars as they were known at `as_of` (tz-aware IST).

    Built only from 5m bars that had closed by `as_of` (start + 5min <= as_of),
    and the last bucket is dropped unless it had ended by `as_of`
    (bucket start + 60min <= as_of, or + 15min for the 15:15 bucket).
    This is what makes the 1h analysis free of lookahead.
    """
    if df5 is None or len(df5) == 0:
        return _empty_5m()
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize(IST)
    # Compare on the index's own clock (a tz-naive index is IST wall-clock time).
    as_of = as_of.tz_convert(df5.index.tz) if df5.index.tz is not None else as_of.tz_convert(IST).tz_localize(None)

    # Bars with start <= as_of - 5min had closed by as_of.
    n_closed = df5.index.searchsorted(as_of - pd.Timedelta(minutes=BAR_MINUTES), side="right")
    hourly = to_hourly(df5.iloc[:n_closed])
    if hourly.empty:
        return hourly

    # A bucket ends an hour after it starts, or at the session close if that comes first.
    _, start_min = clock_parts(hourly.index)
    length = np.minimum(HOUR_MINUTES, minute_of_day(config.SESSION_CLOSE) - start_min)
    ended = hourly.index + pd.to_timedelta(length, unit="m") <= as_of
    return hourly[ended]


def to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Resample daily bars to weeks ending Friday ("W-FRI"). The last row may be a
    partial week. Index = the week-ending Friday (tz-naive). Empty weeks dropped.
    """
    if daily is None or len(daily) == 0:
        return _empty_daily()
    weekly = daily.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return weekly.dropna(subset=OHLCV[:4])
