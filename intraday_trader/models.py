"""Shared data shapes. Every module exchanges these, so they are the contract.

Conventions used everywhere
---------------------------
* Bars are pandas DataFrames with columns Open, High, Low, Close, Volume
  (floats), sorted by time, no duplicate index values.
* Intraday bars (5m, 1h) have a tz-aware DatetimeIndex in Asia/Kolkata.
  The index value is the bar's START time. A 5m bar stamped 10:15 covers
  10:15:00-10:19:59 and is only "known" at 10:20.
* Daily and weekly bars have a tz-naive DatetimeIndex (the session date;
  for weekly bars the Friday that ends the week).
* Times stored in dicts / JSON are ISO strings in IST, e.g.
  "2026-11-13T10:35:00+05:30". Clock-only values are "HH:MM".
* Prices are rounded to 2 decimals only when stored or shown, never while
  computing.
* Direction is "LONG" or "SHORT". Bias is "BULL", "BEAR" or "NEUTRAL".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

LONG = "LONG"
SHORT = "SHORT"
BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"

# Liquidity sides: BSL = buy stops resting above a high, SSL = sell stops below a low.
BSL = "BSL"
SSL = "SSL"


@dataclass
class BiasRead:
    """What one timeframe's market structure says."""

    timeframe: str                      # "W" | "D" | "H1"
    bias: str                           # BULL | BEAR | NEUTRAL
    reason: str                         # one plain sentence with the real numbers in it
    last_bos_time: Optional[str] = None     # bar whose close broke structure (ISO or YYYY-MM-DD)
    last_bos_level: Optional[float] = None  # the swing price that was broken
    swing_high: Optional[float] = None      # latest confirmed swing high not yet closed above
    swing_low: Optional[float] = None       # latest confirmed swing low not yet closed below

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Bias:
    """The three timeframe reads combined into a trade direction."""

    weekly: BiasRead
    daily: BiasRead
    hourly: BiasRead
    direction: Optional[str]            # LONG | SHORT | None (None = stand aside)
    aligned: int                        # how many of the 3 timeframes agree with `direction` (0 when None)
    reason: str                         # e.g. "1h and daily bullish, weekly neutral -> longs only"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "weekly": self.weekly.to_dict(),
            "daily": self.daily.to_dict(),
            "hourly": self.hourly.to_dict(),
            "direction": self.direction,
            "aligned": self.aligned,
            "reason": self.reason,
        }


@dataclass
class Level:
    """A price where stop orders are likely resting."""

    name: str        # PDH PDL PWH PWL ORH ORL HOD LOD H1_SH H1_SL D_SH D_SL M5_SH M5_SL
    price: float
    side: str        # BSL (a high) | SSL (a low)
    time: Optional[str] = None   # when the level was formed (ISO), None if not meaningful

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FVG:
    """Fair value gap: three candles where candle 1 and candle 3 do not overlap."""

    timeframe: str   # "H1" | "M5"
    kind: str        # BULL (gap below price, support) | BEAR (gap above price, resistance)
    top: float
    bottom: float
    created_time: str   # start time of candle 3 (ISO)
    known_at: str       # close time of candle 3 (ISO): the gap may be used only from here on

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mid"] = self.mid
        return d


@dataclass
class Signal:
    """A complete 5-minute setup, produced at the close of bar `signal_time`."""

    symbol: str
    direction: str                      # LONG | SHORT
    signal_time: str                    # start time of the bar that completed the 5m FVG
    contexts: List[str]                 # e.g. ["SWEEP:PDL", "H1_FVG"]; never empty
    context_time: str                   # start time of the earliest sweep / tap bar used
    swing_extreme: float                # lowest low (LONG) / highest high (SHORT) from context bar to signal bar
    mss_level: float                    # the 5m high (LONG) / low (SHORT) that price closed beyond
    fvg: FVG                            # the 5m gap that is the entry zone
    entry: float                        # limit price
    stop: float
    target: float
    target_kind: str                    # level name, "CAP_4R" or "FIXED_2R"
    structural_stop: float              # stop before the MIN_STOP_PCT widening
    risk_per_share: float               # abs(entry - stop)
    rr: float                           # abs(target - entry) / risk_per_share
    sweep_level: Optional[Level] = None     # the level that was swept, if any
    h1_fvg: Optional[FVG] = None            # the 1h gap that was tapped, if any
    story: List[str] = field(default_factory=list)  # ordered plain-English steps with numbers

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fvg"] = self.fvg.to_dict()
        d["sweep_level"] = self.sweep_level.to_dict() if self.sweep_level else None
        d["h1_fvg"] = self.h1_fvg.to_dict() if self.h1_fvg else None
        return d


# ── Plain-dict shapes (documented here, built by engine.py / portfolio.py) ────
#
# WatchItem  (volume.build_watchlist, then enriched by engine.run_day)
#   symbol, name, eligible_from "HH:MM", rank, rvol, cum_volume, avg_cum_volume,
#   turnover_cr, last_price, prev_close, day_change_pct
#   + added by the engine:
#   bias     Bias.to_dict(): the read behind the stock's last signal, else the last read
#            taken inside the entry window, else the read at the end of the session
#   status   "TRADED" | "SIGNAL_NOT_FILLED" | "NO_SETUP" | "NOT_ALIGNED"
#   note     one sentence shown in the report
#
# SignalRecord = Signal.to_dict() plus
#   status       "FILLED" | "EXPIRED" | "INVALIDATED" | "SKIPPED"
#   skip_reason  None or a sentence ("no free margin", "daily trade cap reached", ...)
#   resolved_time  ISO start of the candle on which the status was decided
#   trade_id     the Trade id, only when FILLED
#   rvol, bias   copied from the WatchItem at signal time
#
# Trade  (one completed round trip)
#   id               "2026-11-13-RELIANCE-1"
#   date             "YYYY-MM-DD"
#   symbol, name, direction, contexts, target_kind
#   context_time     ISO start of the sweep / tap candle that gave the reason to look
#   signal_time, entry_time, exit_time          ISO
#   entry, stop, target, exit_price             floats, 2 dp
#   qty                                         int
#   exposure         qty * entry                (what was bought/sold)
#   margin           exposure / LEVERAGE        (own money blocked)
#   risk_inr         qty * abs(entry - stop)
#   rr_planned       Signal.rr
#   exit_reason      "TARGET" | "STOP" | "EOD"
#   gross_pnl        LONG: (exit - entry) * qty ; SHORT: (entry - exit) * qty
#   charges          CHARGE_PER_TRADE
#   net_pnl          gross_pnl - charges
#   r_multiple       net_pnl / risk_inr
#   outcome          "WIN" if net_pnl > 0 else "LOSS"
#   rvol, bias, story, fvg, sweep_level, h1_fvg  copied from the signal
#   chart            only inside a DayResult, stripped before saving the portfolio:
#                    {"bars": [[iso, o, h, l, c], ...]}  5m bars from 12 bars before
#                    context_time to 6 bars after exit_time (same session only)
#
# DayResult  (engine.run_day)
#   date, generated_at, partial (bool: fewer than MIN_BARS_FOR_FULL_DAY bars seen;
#                                runner.run_live also sets it when the run was made on
#                                that day before SESSION_CLOSE)
#   universe_size, scanned (symbols with usable data)
#   start_capital, end_capital, gross_pnl, charges, net_pnl
#   n_trades, wins, losses
#   watchlist  [WatchItem]
#   signals    [SignalRecord]   every signal, taken or not, in time order
#   trades     [Trade]          in exit-time order
#   notes      [str]            e.g. "Daily loss limit hit at 12:40, no further entries"
#   config     config.snapshot()
#
# PortfolioState  (portfolio.py, saved as data/intraday_portfolio.json)
#   initial_capital, capital (after the latest day), updated_at
#   days    [ {date, start_capital, end_capital, gross_pnl, charges, net_pnl,
#              n_trades, wins, losses, partial} ]   sorted by date
#   trades  [Trade without "chart"]                 sorted by date, then exit_time
