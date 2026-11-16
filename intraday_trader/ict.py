"""Steps 2-4: market structure, liquidity levels, fair value gaps and the 5-minute setup.

Pure functions over DataFrames: no network, no files, no clock. A function may
only use the rows it is given, so the caller prevents lookahead by passing
frames that end at the decision time.

SHORT rules are the exact mirror of the LONG rules written in the docstrings.
The mirror is not a second copy of the code: detect_setup negates the prices
of a SHORT (High becomes -Low, Low becomes -High), runs the LONG logic on that
"long space" view, and flips the answer back.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from intraday_trader import config
from intraday_trader.models import (
    BEAR,
    BSL,
    BULL,
    FVG,
    LONG,
    NEUTRAL,
    SHORT,
    SSL,
    Bias,
    BiasRead,
    Level,
    Signal,
)

M5_MINUTES = 5                 # length of the entry-timeframe bar, used for FVG.known_at
MIN_DISPLACEMENT_BARS = 5      # fewest earlier bars needed to judge a displacement candle

_NS_PER_MIN = 60_000_000_000
_NS_PER_DAY = 86_400_000_000_000
_EPOCH = datetime(1970, 1, 1)
_EPOCH_DATE = date(1970, 1, 1)
_EPS = 1e-9

SWEEP_LEVEL_NAMES = ("PDL", "PWL", "ORL", "H1_SL", "D_SL", "M5_SL")       # LONG; mirror for SHORT
TARGET_LEVEL_NAMES = ("PDH", "PWH", "ORH", "HOD", "H1_SH", "D_SH")       # LONG; mirror for SHORT

_MIRROR_NAME = {
    "PDH": "PDL", "PDL": "PDH",
    "PWH": "PWL", "PWL": "PWH",
    "ORH": "ORL", "ORL": "ORH",
    "HOD": "LOD", "LOD": "HOD",
    "H1_SH": "H1_SL", "H1_SL": "H1_SH",
    "D_SH": "D_SL", "D_SL": "D_SH",
    "M5_SH": "M5_SL", "M5_SL": "M5_SH",
}

_LEVEL_PHRASE = {
    "PDH": "the previous day's high",
    "PDL": "the previous day's low",
    "PWH": "the previous week's high",
    "PWL": "the previous week's low",
    "ORH": "the opening range high",
    "ORL": "the opening range low",
    "HOD": "today's high so far",
    "LOD": "today's low so far",
    "H1_SH": "a 1-hour swing high",
    "H1_SL": "a 1-hour swing low",
    "D_SH": "a daily swing high",
    "D_SL": "a daily swing low",
    "M5_SH": "an earlier 5-minute swing high today",
    "M5_SL": "an earlier 5-minute swing low today",
}

_BIAS_WORD = {BULL: "bullish", BEAR: "bearish", NEUTRAL: "neutral"}


# ── Small helpers ─────────────────────────────────────────────────────────────

def _wall_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Bar start times as wall-clock nanoseconds: the timezone is dropped, local time kept."""
    if index.tz is not None:
        index = index.tz_localize(None)
    return index.values.astype("datetime64[ns]").astype("int64")


def _stamp_wall_ns(stamp: Optional[str], tz) -> Optional[int]:
    """An ISO string (with or without offset, or a bare date) as wall-clock ns in `tz`."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        dt = pd.Timestamp(stamp).to_pydatetime()
    if dt.tzinfo is not None:
        if tz is not None:
            dt = dt.astimezone(tz)
        dt = dt.replace(tzinfo=None)
    delta = dt - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _fmt(price: float) -> str:
    return f"{price:,.2f}"


def _floor2(value: float) -> float:
    """Round DOWN to 2 decimals (the tiny offset absorbs float noise such as 610.2599999)."""
    return math.floor(value * 100.0 + 1e-7) / 100.0


def _col(df: pd.DataFrame, name: str) -> np.ndarray:
    return df[name].to_numpy(dtype=float)


def _swing_idx(a: np.ndarray, n: int) -> np.ndarray:
    """Positions i where a[i] is strictly greater than the n values on each side."""
    m = len(a)
    if n < 1 or m < 2 * n + 1:
        return np.empty(0, dtype=np.int64)
    center = a[n:m - n]
    ok = np.ones(m - 2 * n, dtype=bool)
    for k in range(1, n + 1):
        ok &= center > a[n - k:m - n - k]
        ok &= center > a[n + k:m - n + k]
    return np.nonzero(ok)[0] + n


def _level_text(name: str, price: float) -> str:
    phrase = _LEVEL_PHRASE.get(name, name)
    sep = " at " if phrase.startswith(("a ", "an ")) else " "
    return f"{phrase}{sep}{_fmt(price)}"


# ── Swings and structure ──────────────────────────────────────────────────────

def find_swings(df: pd.DataFrame, n: int) -> Dict[str, List[Tuple[int, float]]]:
    """Fractal swing points.

    Bar i is a swing high when High[i] is strictly greater than the High of the
    `n` bars before it and the `n` bars after it; swing low likewise on Low.
    Only swings fully confirmed inside df are returned (i + n <= len(df) - 1).
    Returns {"highs": [(i, price), ...], "lows": [(i, price), ...]} with i the
    integer position, ascending.
    """
    if df is None or len(df) == 0:
        return {"highs": [], "lows": []}
    h = _col(df, "High")
    l = _col(df, "Low")
    return {
        "highs": [(int(i), float(h[i])) for i in _swing_idx(h, n)],
        "lows": [(int(i), float(l[i])) for i in _swing_idx(-l, n)],
    }


def _bar_label(index: pd.DatetimeIndex, pos: int, timeframe: str) -> Tuple[str, str]:
    """(value stored in last_bos_time, phrase used in the reason sentence) for one bar."""
    ts = index[pos]
    if index.tz is not None:
        return ts.isoformat(), f"in the {ts.strftime('%Y-%m-%d %H:%M')} candle"
    day = ts.strftime("%Y-%m-%d")
    if timeframe == "W":
        return day, f"in the week ending {day}"
    return day, f"on {day}"


def structure_bias(df: pd.DataFrame, timeframe: str) -> BiasRead:
    """Direction of the most recent break of structure (BOS) on one timeframe.

    Uses n = config.SWING_N[timeframe] and the last config.BIAS_LOOKBACK_BARS[timeframe]
    rows. Walk the bars in order. A swing at position i becomes usable at bar
    i + n. Track the latest usable swing high and swing low that have not been
    broken. When a bar CLOSES above the tracked swing high that is a bullish
    BOS (bias BULL, that high is consumed); when a bar closes below the tracked
    swing low that is a bearish BOS (bias BEAR, that low is consumed). The bias
    is the direction of the last BOS, NEUTRAL when there was none.

    `reason` is one sentence with the numbers, e.g. "Closed above the 1,342.50
    swing high on 2026-11-10; last break of structure is up." swing_high /
    swing_low are the latest usable swings not yet closed beyond (None if none).
    Fewer than 2*n + 2 rows -> NEUTRAL with reason "not enough history".
    """
    n = config.SWING_N[timeframe]
    lookback = config.BIAS_LOOKBACK_BARS[timeframe]
    if df is None or len(df) < 2 * n + 2:
        return BiasRead(timeframe=timeframe, bias=NEUTRAL, reason="not enough history")
    if len(df) > lookback:
        df = df.iloc[-lookback:]
    m = len(df)
    if m < 2 * n + 2:
        return BiasRead(timeframe=timeframe, bias=NEUTRAL, reason="not enough history")

    h = _col(df, "High")
    l = _col(df, "Low")
    c = _col(df, "Close")
    # A swing at i is confirmed by bar i + n, so it can first be broken by the bar after that.
    high_ready = {int(i) + n: float(h[i]) for i in _swing_idx(h, n)}
    low_ready = {int(i) + n: float(l[i]) for i in _swing_idx(-l, n)}

    tracked_high: Optional[float] = None
    tracked_low: Optional[float] = None
    bias = NEUTRAL
    bos_pos: Optional[int] = None
    bos_level: Optional[float] = None
    for j in range(m):
        cj = c[j]
        if tracked_high is not None and cj > tracked_high:
            bias, bos_pos, bos_level = BULL, j, tracked_high
            tracked_high = None
        elif tracked_low is not None and cj < tracked_low:
            bias, bos_pos, bos_level = BEAR, j, tracked_low
            tracked_low = None
        if j in high_ready:
            tracked_high = high_ready[j]
        if j in low_ready:
            tracked_low = low_ready[j]

    if bos_pos is None:
        return BiasRead(
            timeframe=timeframe,
            bias=NEUTRAL,
            reason=f"No close beyond a swing high or low in the last {m} candles; no break of structure yet.",
            swing_high=tracked_high,
            swing_low=tracked_low,
        )
    stored, phrase = _bar_label(df.index, bos_pos, timeframe)
    if bias == BULL:
        reason = f"Closed above the {_fmt(bos_level)} swing high {phrase}; last break of structure is up."
    else:
        reason = f"Closed below the {_fmt(bos_level)} swing low {phrase}; last break of structure is down."
    return BiasRead(
        timeframe=timeframe,
        bias=bias,
        reason=reason,
        last_bos_time=stored,
        last_bos_level=bos_level,
        swing_high=tracked_high,
        swing_low=tracked_low,
    )


def combine_bias(weekly: BiasRead, daily: BiasRead, hourly: BiasRead) -> Bias:
    """Top-down rule: the 1h and the daily must agree, and the weekly must not oppose.

    LONG  when hourly BULL and daily BULL and weekly is not BEAR.
    SHORT when hourly BEAR and daily BEAR and weekly is not BULL
          (and config.ALLOW_SHORT; otherwise direction None).
    Else direction None. `aligned` counts the timeframes equal to the trade's
    bias (2 or 3), 0 when direction is None. `reason` says it in words.
    """
    h, d, w = hourly.bias, daily.bias, weekly.bias
    hw, dw, ww = _BIAS_WORD.get(h, "neutral"), _BIAS_WORD.get(d, "neutral"), _BIAS_WORD.get(w, "neutral")
    direction: Optional[str] = None
    aligned = 0

    if h == BULL and d == BULL:
        if w == BEAR:
            reason = "1h and daily bullish but the weekly is bearish: stand aside."
        else:
            direction = LONG
            aligned = 2 + (1 if w == BULL else 0)
            reason = f"1h and daily bullish, weekly {ww}: longs only ({aligned}/3 aligned)."
    elif h == BEAR and d == BEAR:
        if w == BULL:
            reason = "1h and daily bearish but the weekly is bullish: stand aside."
        elif not config.ALLOW_SHORT:
            reason = "1h and daily bearish, but short selling is switched off: stand aside."
        else:
            direction = SHORT
            aligned = 2 + (1 if w == BEAR else 0)
            reason = f"1h and daily bearish, weekly {ww}: shorts only ({aligned}/3 aligned)."
    elif h == NEUTRAL and d == NEUTRAL:
        reason = "1h and daily both neutral: no trend to trade, stand aside."
    else:
        reason = f"1h {hw}, daily {dw}, weekly {ww}: the 1h and daily do not agree, stand aside."

    return Bias(weekly=weekly, daily=daily, hourly=hourly, direction=direction, aligned=aligned, reason=reason)


# ── Fair value gaps and levels ────────────────────────────────────────────────

def find_fvgs(
    df: pd.DataFrame,
    timeframe: str,
    min_gap_pct: float,
    bar_minutes: int,
    lookback: Optional[int] = None,
) -> List[FVG]:
    """Fair value gaps in df that are still valid at df's last bar.

    Bullish gap at position i: Low[i] > High[i-2]; zone bottom = High[i-2],
    top = Low[i]. Bearish gap: High[i] < Low[i-2]; zone top = Low[i-2],
    bottom = High[i]. The three candles must belong to the same session for
    intraday frames. Keep a gap only if (top - bottom) / Close[i] >= min_gap_pct.
    A bullish gap is invalid once any later bar closes below its bottom; a
    bearish gap once any later bar closes above its top. created_time = start
    of candle i, known_at = that start + bar_minutes. `lookback` limits the
    scan to the last N rows. Returned oldest first.
    """
    if df is None or len(df) < 3:
        return []
    if lookback is not None and len(df) > lookback:
        df = df.iloc[-lookback:]
    m = len(df)
    if m < 3:
        return []
    h = _col(df, "High")
    l = _col(df, "Low")
    c = _col(df, "Close")

    bull = l[2:] > h[:-2]
    bear = h[2:] < l[:-2]
    hits = np.nonzero(bull | bear)[0] + 2
    if hits.size == 0:
        return []

    intraday = bar_minutes < 24 * 60
    day_no = _wall_ns(df.index) // _NS_PER_DAY if intraday else None
    # later_min[i] / later_max[i]: extreme close among bars i, i+1, ..., m-1
    later_min = np.minimum.accumulate(c[::-1])[::-1]
    later_max = np.maximum.accumulate(c[::-1])[::-1]
    step = pd.Timedelta(minutes=bar_minutes)

    out: List[FVG] = []
    for i in hits:
        i = int(i)
        if intraday and day_no[i] != day_no[i - 2]:
            continue
        is_bull = bool(l[i] > h[i - 2])
        if is_bull:
            bottom, top = float(h[i - 2]), float(l[i])
        else:
            top, bottom = float(l[i - 2]), float(h[i])
        ref = abs(c[i])
        if ref <= 0 or (top - bottom) / ref < min_gap_pct:
            continue
        if i + 1 < m:
            if is_bull and later_min[i + 1] < bottom:
                continue
            if not is_bull and later_max[i + 1] > top:
                continue
        ts = df.index[i]
        out.append(
            FVG(
                timeframe=timeframe,
                kind=BULL if is_bull else BEAR,
                top=top,
                bottom=bottom,
                created_time=ts.isoformat(),
                known_at=(ts + step).isoformat(),
            )
        )
    return out


def _unbroken_swings(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> Tuple[List[int], List[int]]:
    """Positions of confirmed swing highs / lows that no later close has gone beyond."""
    m = len(h)
    if m < 2 * n + 1:
        return [], []
    later_max = np.maximum.accumulate(c[::-1])[::-1]
    later_min = np.minimum.accumulate(c[::-1])[::-1]
    highs = [int(i) for i in _swing_idx(h, n) if i + 1 >= m or later_max[i + 1] <= h[i]]
    lows = [int(i) for i in _swing_idx(-l, n) if i + 1 >= m or later_min[i + 1] >= l[i]]
    return highs, lows


def liquidity_levels(
    prev_session5: pd.DataFrame,
    daily_before: pd.DataFrame,
    h1_done: pd.DataFrame,
    today5: pd.DataFrame,
) -> List[Level]:
    """Every level the setup may sweep or target, from data known right now.

    prev_session5  5m bars of the previous session        -> PDH, PDL
    daily_before   daily bars strictly before today       -> PWH, PWL (the last
                   COMPLETED Mon-Fri week before today's week), and D_SH / D_SL:
                   confirmed daily swings (n = SWING_N["D"]) within the last
                   LEVEL_LOOKBACK_DAILY_BARS rows that no later daily close has broken
    h1_done        completed 1h bars up to now            -> H1_SH / H1_SL: confirmed
                   1h swings (n = SWING_N["H1"]) that no later 1h close has broken
    today5         today's 5m bars up to and including the current bar ->
                   ORH, ORL (high / low of bars starting before FIRST_HOUR_END,
                   only once a bar starting at or after FIRST_HOUR_END exists)
                   and HOD, LOD (extremes of today's bars BEFORE the current bar)

    Highs are side BSL, lows side SSL. Levels with identical name and price are
    de-duplicated.
    """
    out: List[Level] = []
    seen = set()

    def add(name: str, price: float, side: str, when: Optional[str]) -> None:
        key = (name, price)
        if key in seen:
            return
        seen.add(key)
        out.append(Level(name=name, price=price, side=side, time=when))

    has_prev = prev_session5 is not None and len(prev_session5) > 0
    has_daily = daily_before is not None and len(daily_before) > 0
    has_h1 = h1_done is not None and len(h1_done) > 0
    has_today = today5 is not None and len(today5) > 0

    # Previous day
    if has_prev:
        ph = _col(prev_session5, "High")
        pl = _col(prev_session5, "Low")
        i, j = int(ph.argmax()), int(pl.argmin())
        add("PDH", float(ph[i]), BSL, prev_session5.index[i].isoformat())
        add("PDL", float(pl[j]), SSL, prev_session5.index[j].isoformat())

    # "Today" as a day number, for the previous-week lookup
    today_no: Optional[int] = None
    if has_today:
        today_no = (today5.index[-1].date() - _EPOCH_DATE).days
    elif has_daily:
        today_no = (daily_before.index[-1].date() - _EPOCH_DATE).days + 1

    if has_daily:
        d_idx = daily_before.index
        d_no = _wall_ns(d_idx) // _NS_PER_DAY
        dh = _col(daily_before, "High")
        dl = _col(daily_before, "Low")
        dc = _col(daily_before, "Close")

        def d_label(pos: int) -> str:
            return d_idx[pos].strftime("%Y-%m-%d")

        # Previous week: day 0 of the epoch was a Thursday, so (n + 3) % 7 is 0 on a Monday.
        if today_no is not None:
            this_monday = today_no - ((today_no + 3) % 7)
            end = int(np.searchsorted(d_no, this_monday, side="left"))
            if end > 0:
                last_no = int(d_no[end - 1])
                week_monday = last_no - ((last_no + 3) % 7)
                start = int(np.searchsorted(d_no, week_monday, side="left"))
                i = start + int(dh[start:end].argmax())
                j = start + int(dl[start:end].argmin())
                add("PWH", float(dh[i]), BSL, d_label(i))
                add("PWL", float(dl[j]), SSL, d_label(j))

        # Daily swings nobody has closed beyond
        base = max(0, len(daily_before) - config.LEVEL_LOOKBACK_DAILY_BARS)
        highs, lows = _unbroken_swings(dh[base:], dl[base:], dc[base:], config.SWING_N["D"])
        for i in highs:
            add("D_SH", float(dh[base + i]), BSL, d_label(base + i))
        for i in lows:
            add("D_SL", float(dl[base + i]), SSL, d_label(base + i))

    # 1-hour swings nobody has closed beyond
    if has_h1:
        hh = _col(h1_done, "High")
        hl = _col(h1_done, "Low")
        hc = _col(h1_done, "Close")
        highs, lows = _unbroken_swings(hh, hl, hc, config.SWING_N["H1"])
        for i in highs:
            add("H1_SH", float(hh[i]), BSL, h1_done.index[i].isoformat())
        for i in lows:
            add("H1_SL", float(hl[i]), SSL, h1_done.index[i].isoformat())

    # Today: opening range, and the day's extremes before the current bar
    if has_today:
        wall = _wall_ns(today5.index)
        day_no = wall // _NS_PER_DAY
        first = int(np.searchsorted(day_no, day_no[-1], side="left"))   # ignore any earlier session rows
        th = _col(today5, "High")[first:]
        tl = _col(today5, "Low")[first:]
        minute = (wall[first:] % _NS_PER_DAY) // _NS_PER_MIN
        cutoff = config.FIRST_HOUR_END.hour * 60 + config.FIRST_HOUR_END.minute
        in_range = np.nonzero(minute < cutoff)[0]
        if in_range.size and in_range.size < len(th):
            i = int(in_range[th[in_range].argmax()])
            j = int(in_range[tl[in_range].argmin()])
            add("ORH", float(th[i]), BSL, today5.index[first + i].isoformat())
            add("ORL", float(tl[j]), SSL, today5.index[first + j].isoformat())
        if len(th) > 1:
            i = int(th[:-1].argmax())
            j = int(tl[:-1].argmin())
            add("HOD", float(th[i]), BSL, today5.index[first + i].isoformat())
            add("LOD", float(tl[j]), SSL, today5.index[first + j].isoformat())

    return out


# ── The 5-minute setup ────────────────────────────────────────────────────────

def detect_setup(
    symbol: str,
    m5: pd.DataFrame,
    day: date,
    direction: str,
    levels: List[Level],
    h1_fvgs: List[FVG],
) -> Optional[Signal]:
    """Is the LAST bar of `m5` the bar that completes a valid setup? (LONG shown.)

    m5 holds up to config.M5_WINDOW_BARS 5m bars ending at the bar that just
    closed; "today" means bars dated `day`. Let t be the last position.

    1. Trigger gap. Bars t-2, t-1, t are all today's. Low[t] > High[t-2] and the
       gap is at least M5_FVG_MIN_PCT of Close[t].
    2. Displacement. Bar t-1 closes above its open and its body is at least
       DISPLACEMENT_BODY_MULT times the mean body of the DISPLACEMENT_AVG_BARS
       bars before it (fewer if the window is shorter, minimum 5).
    3. Context. Among today's bars s in [t - CONTEXT_LOOKBACK_BARS, t - 1], at
       least one of:
       a. SWEEP:<name>  Low[s] < L for a level L whose name is in
          SWEEP_LEVEL_NAMES, L had not been traded below earlier today (after
          the level's own time), and Close[t] > L (price is back above it).
          M5_SL levels are computed here: today's 5m swing lows (n = SWING_N["M5"])
          confirmed before bar s. If several levels qualify, report the lowest.
       b. H1_FVG  a BULL gap g in h1_fvgs with bar s starting at or after
          g.known_at, Low[s] <= g.top, and no close below g.bottom from s to t.
       contexts lists every kind that fired, sweep first. context_time is the
       earliest such s. swing_extreme = lowest Low from that s to t.
    4. Structure shift. ref = the most recent 5m swing high (n = SWING_N["M5"],
       confirmed by bar t) located before the bar that made swing_extreme; if
       none exists today, the highest High of the MSS_FALLBACK_BARS bars before
       that bar. Some close after the swing_extreme bar, up to t, is above ref.
       mss_level = ref.
    5. Prices. entry = gap midpoint (ENTRY_AT "ce") or gap top ("edge").
       structural_stop = swing_extreme * (1 - SL_BUFFER_PCT). stop =
       min(structural_stop, entry * (1 - MIN_STOP_PCT)). Reject if
       (entry - stop) / entry > MAX_STOP_PCT.
    6. Target. Among levels named in TARGET_LEVEL_NAMES with price > entry, the
       nearest one whose reward / risk >= MIN_RR (target_kind = its name). If
       that RR exceeds MAX_RR, target = entry + MAX_RR * risk and target_kind =
       "CAP_4R" (built from MAX_RR). If no level qualifies, target = entry +
       FALLBACK_RR * risk and target_kind = "FIXED_2R" (built from FALLBACK_RR).

    Order prices are whole paise. entry, structural_stop, stop and target are
    rounded to 2 decimals FIRST; risk_per_share, rr, every accept / reject
    decision in steps 5 and 6 and every number in the story are then computed
    from those rounded prices, so the story can be checked by hand against the
    stored prices. The MIN_STOP_PCT stop is rounded away from the entry so the
    stored stop is never closer than MIN_STOP_PCT.

    Returns a Signal with a `story` (5-8 short sentences, real numbers, in the
    order the events happened) or None. Must depend only on the rows of m5 and
    the arguments, so calling it on a longer history truncated at the same bar
    gives the same answer.
    """
    if m5 is None or direction not in (LONG, SHORT):
        return None
    n_rows = len(m5)
    if n_rows < 3:
        return None
    is_long = direction == LONG
    sign = 1.0 if is_long else -1.0
    t = n_rows - 1

    # "Long space": for a SHORT every price is negated, so highs and lows swap roles
    # and the LONG rules below apply unchanged.
    if is_long:
        h, l = _col(m5, "High"), _col(m5, "Low")
    else:
        h, l = -_col(m5, "Low"), -_col(m5, "High")

    # 1. Trigger gap (checked first: most bars stop here)
    gap_bottom = h[t - 2]
    gap_top = l[t]
    if not gap_top > gap_bottom:
        return None
    idx = m5.index
    if isinstance(day, datetime):
        day = day.date()
    if idx[t].date() != day or idx[t - 2].date() != day:
        return None
    o = sign * _col(m5, "Open")
    c = sign * _col(m5, "Close")
    ct = c[t]
    ref_price = abs(ct)
    if ref_price <= 0 or (gap_top - gap_bottom) / ref_price < config.M5_FVG_MIN_PCT:
        return None

    # 2. Displacement
    body = c[t - 1] - o[t - 1]
    if not body > 0:
        return None
    b0 = max(0, t - 1 - config.DISPLACEMENT_AVG_BARS)
    prior_bodies = np.abs(c[b0:t - 1] - o[b0:t - 1])
    if prior_bodies.size < MIN_DISPLACEMENT_BARS:
        return None
    avg_body = float(prior_bodies.mean())
    if body < config.DISPLACEMENT_BODY_MULT * avg_body:
        return None

    # 3. Context
    wall = _wall_ns(idx)
    day_no = (day - _EPOCH_DATE).days
    first_today = int(np.searchsorted(wall // _NS_PER_DAY, day_no, side="left"))
    lo_s = max(first_today, t - config.CONTEXT_LOOKBACK_BARS)
    if lo_s > t - 1:
        return None
    tz = idx.tz
    n5 = config.SWING_N["M5"]
    window_low = l[lo_s:t].min()

    # 3a. Sweeps. Each candidate is (price in long space, first bar that may breach it, Level or swing position).
    best_sweep = None          # (price, breach bar, Level or None, swing position or None)
    earliest_s: Optional[int] = None

    def consider(price: float, start_k: int, level: Optional[Level], swing_pos: Optional[int]) -> None:
        nonlocal best_sweep, earliest_s
        if start_k >= t:
            return
        below = l[start_k:t] < price
        if not below.any():
            return
        k = start_k + int(below.argmax())      # the FIRST trade below the level today
        if k < lo_s:
            return                             # traded through earlier than the context window
        if best_sweep is None or price < best_sweep[0]:
            best_sweep = (price, k, level, swing_pos)
        if earliest_s is None or k < earliest_s:
            earliest_s = k

    for lv in levels:
        name = lv.name if is_long else _MIRROR_NAME.get(lv.name, lv.name)
        if name not in SWEEP_LEVEL_NAMES:
            continue
        price = sign * lv.price
        if not (window_low < price < ct):
            continue
        formed = _stamp_wall_ns(lv.time, tz)
        if formed is None or formed < wall[first_today]:
            start_k = first_today
        else:
            start_k = int(np.searchsorted(wall, formed, side="right"))
        consider(price, start_k, lv, None)

    for i in _swing_idx(-l[first_today:t + 1], n5):
        pos = first_today + int(i)
        price = l[pos]
        if window_low < price < ct:
            consider(price, pos + 1, None, pos)

    # 3b. Tap of a 1-hour fair value gap
    tap = None                 # (bar, FVG)
    if h1_fvgs:
        want = BULL if is_long else BEAR
        later_min_close = np.minimum.accumulate(c[lo_s:t + 1][::-1])[::-1][:-1]   # min close from s to t
        lows_w = l[lo_s:t]
        wall_w = wall[lo_s:t]
        for g in h1_fvgs:
            if g.kind != want:
                continue
            g_top, g_bottom = (g.top, g.bottom) if is_long else (-g.bottom, -g.top)
            ok = (lows_w <= g_top) & (later_min_close >= g_bottom)
            known = _stamp_wall_ns(g.known_at, tz)
            if known is not None:
                ok &= wall_w >= known
            if ok.any():
                s = lo_s + int(ok.argmax())
                if tap is None or s < tap[0]:
                    tap = (s, g)

    if best_sweep is None and tap is None:
        return None
    s0 = t
    if earliest_s is not None:
        s0 = min(s0, earliest_s)
    if tap is not None:
        s0 = min(s0, tap[0])
    x = s0 + int(l[s0:t + 1].argmin())          # the bar that made the swing extreme
    extreme = l[x]

    # 4. Structure shift
    swing_highs = _swing_idx(h[first_today:t + 1], n5) + first_today
    swing_highs = swing_highs[swing_highs < x]
    if swing_highs.size:
        ref = h[int(swing_highs[-1])]
        ref_bars = 0
    else:
        f0 = max(0, x - config.MSS_FALLBACK_BARS)
        if f0 >= x:
            return None
        ref = h[f0:x].max()
        ref_bars = x - f0
    after = c[x + 1:t + 1]
    if after.size == 0:
        return None
    broke = after > ref
    if not broke.any():
        return None
    mss_pos = x + 1 + int(broke.argmax())

    # 5. Prices
    # Order prices are rounded to paise first; everything after uses the rounded prices.
    at_mid = config.ENTRY_AT != "edge"
    entry = round((gap_top + gap_bottom) / 2.0 if at_mid else gap_top, 2)
    structural = round(extreme - abs(extreme) * config.SL_BUFFER_PCT, 2)
    floor_stop = _floor2(entry - abs(entry) * config.MIN_STOP_PCT)   # rounded away from the entry
    widened = floor_stop < structural
    stop = floor_stop if widened else structural
    risk = round(entry - stop, 2)
    if risk <= 0 or risk / abs(entry) > config.MAX_STOP_PCT:
        return None

    # 6. Target
    nearest = None             # (price, Level)
    for lv in levels:
        name = lv.name if is_long else _MIRROR_NAME.get(lv.name, lv.name)
        if name not in TARGET_LEVEL_NAMES:
            continue
        price = round(sign * lv.price, 2)
        if price <= entry or (price - entry) / risk < config.MIN_RR - _EPS:
            continue
        if nearest is None or price < nearest[0]:
            nearest = (price, lv)
    capped_level: Optional[Level] = None
    target_level: Optional[Level] = None
    if nearest is None:
        target = round(entry + config.FALLBACK_RR * risk, 2)
        target_kind = "FIXED_%gR" % config.FALLBACK_RR
    elif (nearest[0] - entry) / risk > config.MAX_RR + _EPS:
        target = round(entry + config.MAX_RR * risk, 2)
        target_kind = "CAP_%gR" % config.MAX_RR
        capped_level = nearest[1]
    else:
        target = nearest[0]
        target_level = nearest[1]
        target_kind = target_level.name
    rr = (target - entry) / risk

    # ── Back to real prices ───────────────────────────────────────────────────
    a_entry, a_stop, a_target = float(sign * entry), float(sign * stop), float(sign * target)
    a_structural, a_extreme, a_ref = float(sign * structural), float(sign * extreme), float(sign * ref)
    if is_long:
        fvg_top, fvg_bottom = float(gap_top), float(gap_bottom)
    else:
        fvg_top, fvg_bottom = float(-gap_bottom), float(-gap_top)
    t_stamp = idx[t]
    fvg = FVG(
        timeframe="M5",
        kind=BULL if is_long else BEAR,
        top=fvg_top,
        bottom=fvg_bottom,
        created_time=t_stamp.isoformat(),
        known_at=(t_stamp + pd.Timedelta(minutes=M5_MINUTES)).isoformat(),
    )

    contexts: List[str] = []
    sweep_level: Optional[Level] = None
    sweep_bar: Optional[int] = None
    if best_sweep is not None:
        _, sweep_bar, lv, swing_pos = best_sweep
        if lv is not None:
            sweep_level = lv
        else:
            sweep_level = Level(
                name="M5_SL" if is_long else "M5_SH",
                price=float(sign * l[swing_pos]),
                side=SSL if is_long else BSL,
                time=idx[swing_pos].isoformat(),
            )
        contexts.append(f"SWEEP:{sweep_level.name}")
    h1_fvg: Optional[FVG] = None
    if tap is not None:
        h1_fvg = tap[1]
        contexts.append("H1_FVG")

    # ── The story, in the order things happened ───────────────────────────────
    def hm(pos: int) -> str:
        return idx[pos].strftime("%H:%M")

    w = _WORDS[LONG if is_long else SHORT]
    events: List[Tuple[int, int, str]] = []     # (bar, tie-break, sentence)

    if sweep_level is not None and sweep_bar is not None:
        level_price = sign * sweep_level.price
        back = sweep_bar + int((c[sweep_bar:t + 1] > level_price).argmax())
        text = (
            f"{hm(sweep_bar)} price {w['dipped']} to {_fmt(sign * l[sweep_bar])}, {w['below']} "
            f"{_level_text(sweep_level.name, sweep_level.price)}"
        )
        if back == sweep_bar:
            text += f", and closed back {w['above']} it."
        else:
            text += f"; by {hm(back)} it had closed back {w['above']} it."
        events.append((sweep_bar, 0, text))

    if tap is not None:
        events.append((
            tap[0], 1,
            f"{hm(tap[0])} price {w['tapped']} the 1-hour fair value gap "
            f"({_fmt(h1_fvg.bottom)} to {_fmt(h1_fvg.top)}) without closing {w['below']} it.",
        ))

    ratio = f"body {body / avg_body:.1f}x the recent average" if avg_body > 0 else "far larger than recent candles"
    if ref_bars:
        ref_text = f"{_fmt(a_ref)}, the {w['highest']} price of the {ref_bars} candles before the {w['low']}"
    else:
        ref_text = f"the {_fmt(a_ref)} swing {w['high']}"
    if mss_pos == t - 1:
        events.append((t - 1, 2, f"{hm(t - 1)} a strong {w['up']} candle ({ratio}) closed {w['above']} {ref_text}."))
    else:
        events.append((
            mss_pos, 2,
            f"{hm(mss_pos)} a candle closed {w['above']} {ref_text}, turning the 5-minute structure {w['up']}.",
        ))
        events.append((t - 1, 3, f"{hm(t - 1)} a strong {w['up']} candle ({ratio}) showed {w['side']} in control."))

    events.append((t, 4, f"{hm(t)} left a fair value gap between {_fmt(fvg_bottom)} and {_fmt(fvg_top)}."))
    events.sort(key=lambda e: (e[0], e[1]))
    story = [e[2] for e in events]

    where = "the gap midpoint" if at_mid else f"the {w['edge']} of the gap"
    if widened:
        stop_note = (
            f"set {config.MIN_STOP_PCT * 100:.2f}% {w['under']} the entry because the "
            f"{_fmt(a_extreme)} {w['low']} was closer than that"
        )
    else:
        stop_note = f"{config.SL_BUFFER_PCT * 100:.2f}% {w['under']} the {_fmt(a_extreme)} {w['low']}"
    story.append(
        f"{w['order']} limit at {where} {_fmt(a_entry)}, stop {_fmt(a_stop)} ({stop_note}), "
        f"risk {_fmt(risk)} per share."
    )
    if target_level is not None:
        story.append(
            f"Target {_fmt(a_target)}, {_LEVEL_PHRASE.get(target_level.name, target_level.name)}, "
            f"{rr:.2f} times the risk."
        )
    elif capped_level is not None:
        story.append(
            f"Target {_fmt(a_target)}, capped at {config.MAX_RR:g} times the risk; the nearest level, "
            f"{_level_text(capped_level.name, capped_level.price)}, is further away."
        )
    else:
        story.append(
            f"Target {_fmt(a_target)}, a plain {config.FALLBACK_RR:g} times the risk, because no liquidity "
            f"level sits at least {config.MIN_RR:g} times the risk {w['above']} the entry."
        )

    return Signal(
        symbol=symbol,
        direction=direction,
        signal_time=t_stamp.isoformat(),
        contexts=contexts,
        context_time=idx[s0].isoformat(),
        swing_extreme=a_extreme,
        mss_level=a_ref,
        fvg=fvg,
        entry=a_entry,
        stop=a_stop,
        target=a_target,
        target_kind=target_kind,
        structural_stop=a_structural,
        risk_per_share=float(risk),
        rr=float(rr),
        sweep_level=sweep_level,
        h1_fvg=h1_fvg,
        story=story,
    )


# Direction-specific words for the story. Keys are written from the LONG point of view.
_WORDS = {
    LONG: {
        "dipped": "dipped", "below": "below", "above": "above", "tapped": "dipped into",
        "up": "up", "side": "buyers", "high": "high", "low": "low", "highest": "highest",
        "under": "under", "order": "Buy", "edge": "top",
    },
    SHORT: {
        "dipped": "pushed up", "below": "above", "above": "below", "tapped": "rallied into",
        "up": "down", "side": "sellers", "high": "low", "low": "high", "highest": "lowest",
        "under": "above", "order": "Sell", "edge": "bottom",
    },
}
