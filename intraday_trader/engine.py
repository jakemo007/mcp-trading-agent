"""Step 5: replay one session bar by bar, exactly as it would have unfolded.

Pure: no network, no files. Given the same frames it returns the same result
(apart from the generated_at stamp).

No lookahead, by construction
-----------------------------
* Rows dated after `day` are dropped from every 5m frame first (a backtest
  passes frames that contain later sessions). Daily bars dated `day` or later
  are never used.
* A 5m bar stamped ts is acted on at its close. Everything computed at that
  moment uses only bars with start <= ts, and 1h candles from
  data.hourly_asof(df5 up to ts, ts + 5min).

Inside one bar the order is: first the open positions of every symbol (stop,
target, square-off), then the pending order and the signal search of every
symbol. A signal found on bar ts can fill at the earliest on the symbol's next
bar. A 5-minute bar cannot say whether one stock's exit came before another
stock's fill, so what a close releases (a position slot, margin, a profit) is
available to fills from the next bar, while a loss counts against the daily
limit at once. The result therefore does not depend on the watchlist order,
except that when two orders fill in the same bar and only one slot is left,
the stock higher on the watchlist gets it.

Order prices (entry, stop, target) are rounded to the paise when the order is
placed, and every fill and exit price is rounded the same way, so each P&L can
be checked by hand from the numbers in the report.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from intraday_trader import config, data, ict, portfolio, volume
from intraday_trader.models import BEAR, BULL, LONG, NEUTRAL, SHORT, Bias, BiasRead, Signal

logger = logging.getLogger(__name__)

IST = "Asia/Kolkata"
_IST_TZ = timezone(timedelta(hours=5, minutes=30))
BAR_MINUTES = 5
H1_BAR_MINUTES = 60
CHART_BARS_BEFORE = 12      # chart starts this many bars before the context bar
CHART_BARS_AFTER = 6        # and ends this many bars after the exit bar
# An "EOD" exit on a candle that starts before this time means the stock's data ran out
# early (a halt, a data hole, or a run made before the close), not the 15:15 square-off.
REGULAR_SQUARE_OFF_FROM = dtime(15, 0)
_OHLCV = ["Open", "High", "Low", "Close", "Volume"]
_BIAS_WORD = {BULL: "bullish", BEAR: "bearish", NEUTRAL: "neutral"}
_EXIT_PHRASE = {"TARGET": "target hit", "STOP": "stopped out", "EOD": "squared off"}


# ── Small helpers ─────────────────────────────────────────────────────────────

def _r2(value: float) -> float:
    """Round to paise; `+ 0.0` turns -0.0 into 0.0."""
    return round(float(value), 2) + 0.0


def _money(value: float) -> str:
    return "{:,.2f}".format(value)


def _signed(value: float) -> str:
    return "{:+,.2f}".format(value)


def _hm(ts: pd.Timestamp) -> str:
    return ts.strftime("%H:%M")


def _iso(ts: pd.Timestamp) -> str:
    return ts.isoformat()


def _as_date(day: Any) -> date:
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    return pd.Timestamp(day).date()


def _parse_hm(value: Any, default: dtime) -> dtime:
    if isinstance(value, dtime):
        return value
    try:
        hour, minute = str(value).split(":")[:2]
        return dtime(int(hour), int(minute))
    except (ValueError, TypeError):
        return default


def _day_bound(index: pd.DatetimeIndex, day: date, offset_days: int = 0) -> pd.Timestamp:
    """Midnight IST of `day` (+offset) expressed on the index's own clock."""
    bound = pd.Timestamp(day) + pd.Timedelta(days=offset_days)
    if getattr(index, "tz", None) is not None:
        bound = bound.tz_localize(IST).tz_convert(index.tz)
    return bound


def _upto_day(df: pd.DataFrame, day: date) -> pd.DataFrame:
    """Drop every row dated after `day`."""
    return df.iloc[: df.index.searchsorted(_day_bound(df.index, day, 1), side="left")]


def _day_start_pos(df: pd.DataFrame, day: date) -> int:
    return int(df.index.searchsorted(_day_bound(df.index, day), side="left"))


def _ist_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index.tz_convert(IST) if getattr(index, "tz", None) is not None else index


def _round_floats(obj: Any) -> Any:
    """Copy of a JSON-like structure with every float rounded to 2 decimals."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return _r2(obj)
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v) for v in obj]
    return obj


def _neutral_read(timeframe: str, reason: str) -> BiasRead:
    return BiasRead(timeframe=timeframe, bias=NEUTRAL, reason=reason)


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=float) for c in _OHLCV}, index=pd.DatetimeIndex([]))


# ── Per-symbol state ──────────────────────────────────────────────────────────

class _Sym:
    """Everything the loop needs to know about one watchlist symbol."""

    def __init__(self, item: dict, df5: pd.DataFrame, day: date, daily: Optional[pd.DataFrame]):
        self.item = item
        self.symbol: str = item["symbol"]
        self.name: str = item.get("name") or self.symbol
        self.rvol: float = round(float(item.get("rvol") or 0.0), 2)
        self.eligible_from: dtime = _parse_hm(item.get("eligible_from"), config.ENTRY_START)

        self.df5 = df5
        self.start_pos = _day_start_pos(df5, day)
        self.today = df5.iloc[self.start_pos:]
        today_ist = _ist_index(self.today.index)
        self.stamps: List[pd.Timestamp] = list(today_ist)
        self.clocks: List[dtime] = [ts.time() for ts in self.stamps]
        self.pos: Dict[pd.Timestamp, int] = {ts: i for i, ts in enumerate(self.stamps)}
        self.o = self.today["Open"].to_numpy(dtype=float)
        self.h = self.today["High"].to_numpy(dtype=float)
        self.l = self.today["Low"].to_numpy(dtype=float)
        self.c = self.today["Close"].to_numpy(dtype=float)

        before_sq = [i for i, clock in enumerate(self.clocks) if clock < config.SQUARE_OFF]
        self.last_pre_sq: Optional[int] = before_sq[-1] if before_sq else None

        prev_all = df5.iloc[: self.start_pos]
        if len(prev_all):
            last_prev_day = _ist_index(prev_all.index)[-1].date()
            self.prev5 = prev_all.iloc[_day_start_pos(prev_all, last_prev_day):]
        else:
            self.prev5 = prev_all

        if daily is not None and len(daily):
            self.daily_before = daily.iloc[: daily.index.searchsorted(_day_bound_naive(daily.index, day), side="left")]
        else:
            self.daily_before = _empty_bars()
        if len(self.daily_before):
            self.w_read = ict.structure_bias(data.to_weekly(self.daily_before), "W")
            self.d_read = ict.structure_bias(self.daily_before, "D")
        else:
            self.w_read = _neutral_read("W", "No daily history before this session.")
            self.d_read = _neutral_read("D", "No daily history before this session.")

        self.h_cache: Dict[int, Tuple[pd.DataFrame, BiasRead, list, Bias]] = {}
        self.position: Optional[dict] = None
        self.pending: Optional[dict] = None
        self.trades_today = 0
        self.evaluated = 0
        self.directions_seen: List[str] = []
        self.bias_keys_seen: set = set()
        self.last_bias: Optional[Bias] = None
        self.trades: List[dict] = []
        self.records: List[dict] = []


def _day_bound_naive(index: pd.DatetimeIndex, day: date) -> pd.Timestamp:
    """Midnight of `day` for a daily index (tz-naive by contract, tolerated if not)."""
    bound = pd.Timestamp(day)
    if getattr(index, "tz", None) is not None:
        bound = bound.tz_localize(index.tz)
    return bound


class _Book:
    """The account during the session."""

    def __init__(self, start_capital: float):
        self.start_capital = start_capital
        self.realised = 0.0
        self.open_count = 0
        self.margin_in_use = 0.0
        self.trades_opened = 0
        self.loss_limit_noted = False
        self.trades: List[dict] = []
        self.signals: List[dict] = []
        self.notes: List[str] = []
        self.start_candle()

    def start_candle(self) -> None:
        """A 5-minute candle cannot say whether a close on one stock came before a fill
        on another. So what a close in this candle gives back (a position slot, margin,
        a profit) is held back from fills until the next candle; a loss counts at once."""
        self.released_count = 0
        self.released_margin = 0.0
        self.candle_gain = 0.0

    def release(self, margin: float, net: float) -> None:
        self.realised += net
        self.open_count -= 1
        self.margin_in_use -= margin
        self.released_count += 1
        self.released_margin += margin
        if net > 0:
            self.candle_gain += net

    def realised_for_fills(self) -> float:
        return self.realised - self.candle_gain

    def loss_limit_hit(self, realised: Optional[float] = None) -> bool:
        limit = config.DAILY_LOSS_LIMIT_PCT * self.start_capital
        value = self.realised if realised is None else realised
        return limit > 0 and value <= -limit


# ── The replay ────────────────────────────────────────────────────────────────

def run_day(
    day: date,
    bars5: Dict[str, pd.DataFrame],
    daily: Dict[str, pd.DataFrame],
    start_capital: float,
    names: Optional[Dict[str, str]] = None,
) -> dict:
    """Simulate `day` and return a DayResult (shape in models.py). See the module docstring
    and docs/intraday-trader/README.md section 8 for the rules."""
    day = _as_date(day)
    start_capital = float(start_capital)
    bars5 = bars5 or {}
    daily = daily or {}

    # 1. Nothing dated after `day` survives this point.
    trimmed: Dict[str, pd.DataFrame] = {}
    scanned = 0
    longest = 0
    for symbol, df in bars5.items():
        if df is None or len(df) == 0:
            continue
        cut = _upto_day(df, day)
        if len(cut) == 0:
            continue
        trimmed[symbol] = cut
        n_today = len(cut) - _day_start_pos(cut, day)
        if n_today > 0:
            scanned += 1
            longest = max(longest, n_today)

    book = _Book(start_capital)
    watch_items = [dict(item) for item in (volume.build_watchlist(trimmed, day, names) or [])]

    syms: List[_Sym] = []
    by_symbol: Dict[str, _Sym] = {}
    for item in watch_items:
        df5 = trimmed.get(item["symbol"])
        if df5 is None or len(df5) - _day_start_pos(df5, day) <= 0:
            continue
        st = _Sym(item, df5, day, daily.get(item["symbol"]))
        syms.append(st)
        by_symbol[st.symbol] = st

    open_ts = pd.Timestamp.combine(day, config.SESSION_OPEN).tz_localize(IST)
    close_ts = pd.Timestamp.combine(day, config.SESSION_CLOSE).tz_localize(IST)

    def hourly_context(st: _Sym, i: int) -> Tuple[pd.DataFrame, BiasRead, list, Bias]:
        """1h candles, their bias and gaps as known at the close of today's bar i.

        They only change when a 1h candle completes, so they are cached by the
        number of hour boundaries passed (plus one once the short 15:15 bucket
        has ended at the close).
        """
        as_of = st.stamps[i] + pd.Timedelta(minutes=BAR_MINUTES)
        as_of_ist = as_of if as_of.tzinfo is not None else as_of.tz_localize(IST)
        key = max(0, int((as_of_ist - open_ts).total_seconds() // 3600))
        if as_of_ist >= close_ts:
            key += 1
        hit = st.h_cache.get(key)
        if hit is None:
            h1_done = data.hourly_asof(st.df5.iloc[: st.start_pos + i + 1], as_of)
            h_read = ict.structure_bias(h1_done, "H1")
            fvgs = list(ict.find_fvgs(
                h1_done, "H1", config.H1_FVG_MIN_PCT, H1_BAR_MINUTES, config.H1_FVG_LOOKBACK_BARS,
            ) or [])
            bias = ict.combine_bias(st.w_read, st.d_read, h_read)
            hit = (h1_done, h_read, fvgs, bias)
            st.h_cache[key] = hit
        return hit

    def close_position(st: _Sym, i: int, exit_price: float, reason: str) -> None:
        pos = st.position
        sig: Signal = pos["signal"]
        exit_price = _r2(exit_price)
        gross, charges, net = portfolio.trade_pnl(pos["direction"], pos["entry"], exit_price, pos["qty"])
        risk_inr = _r2(pos["qty"] * abs(pos["entry"] - pos["stop"]))
        exposure = _r2(pos["qty"] * pos["entry"])
        ts = st.stamps[i]
        entry_ts = st.stamps[pos["entry_idx"]]

        book.release(pos["margin"], net)
        st.position = None

        story = list(sig.story or [])
        story.append("Limit order filled at {} on the {} candle: {} shares, {} of stock on {} margin.".format(
            _money(pos["entry"]), _hm(entry_ts), pos["qty"], _money(exposure), _money(pos["margin"])))
        if reason == "TARGET":
            story.append("Target {} was reached on the {} candle; exit at {}.".format(
                _money(pos["target"]), _hm(ts), _money(exit_price)))
        elif reason == "STOP":
            story.append("Stop {} was hit on the {} candle; exit at {}.".format(
                _money(pos["stop"]), _hm(ts), _money(exit_price)))
        elif st.clocks[i] < REGULAR_SQUARE_OFF_FROM:
            story.append("No 5-minute candles after the {} candle in the data, so the trade was closed at {}, "
                         "the last price available.".format(_hm(ts), _money(exit_price)))
            book.notes.append("{}: no 5-minute data after {}, so its open trade was closed at the last price "
                              "available.".format(st.symbol, _hm(ts)))
        else:
            story.append("Still open at square-off; closed at {}, the close of the {} candle.".format(
                _money(exit_price), _hm(ts)))
        story.append("Result: {} gross, {} brokerage, {} net.".format(_signed(gross), _money(charges), _signed(net)))

        trade = {
            "id": "{}-{}-{}".format(day.isoformat(), st.symbol, st.trades_today),
            "date": day.isoformat(),
            "symbol": st.symbol,
            "name": st.name,
            "direction": pos["direction"],
            "contexts": list(sig.contexts),
            "target_kind": sig.target_kind,
            "context_time": sig.context_time,
            "signal_time": sig.signal_time,
            "entry_time": _iso(entry_ts),
            "exit_time": _iso(ts),
            "entry": pos["entry"],
            "stop": pos["stop"],
            "target": pos["target"],
            "exit_price": exit_price,
            "qty": pos["qty"],
            "exposure": exposure,
            "margin": _r2(pos["margin"]),
            "risk_inr": risk_inr,
            "rr_planned": round(float(sig.rr), 2),
            "exit_reason": reason,
            "gross_pnl": gross,
            "charges": charges,
            "net_pnl": net,
            "r_multiple": round(net / risk_inr, 2) + 0.0 if risk_inr > 0 else 0.0,
            "outcome": "WIN" if net > 0 else "LOSS",
            "rvol": st.rvol,
            "bias": pos["bias"],
            "story": story,
            "fvg": _round_floats(sig.fvg.to_dict()),
            "sweep_level": _round_floats(sig.sweep_level.to_dict()) if sig.sweep_level else None,
            "h1_fvg": _round_floats(sig.h1_fvg.to_dict()) if sig.h1_fvg else None,
            "chart": {"bars": []},          # filled in after the loop
            "_entry_idx": pos["entry_idx"],  # internal keys, removed before returning
            "_exit_idx": i,
        }
        st.trades.append(trade)
        book.trades.append(trade)
        logger.info("EXIT  %s %s %s at %.2f (%s)  net %+.2f", _hm(ts), st.symbol, pos["direction"],
                    exit_price, reason, net)

        if book.loss_limit_hit() and not book.loss_limit_noted:
            book.loss_limit_noted = True
            book.notes.append("Daily loss limit hit at {}, no further entries.".format(_hm(ts)))

    def resolve(st: _Sym, i: int, status: str, reason: Optional[str]) -> None:
        rec = st.pending["record"]
        rec["status"] = status
        rec["skip_reason"] = reason
        rec["resolved_time"] = _iso(st.stamps[i])
        st.pending = None

    def try_fill(st: _Sym, i: int, fill_price: float) -> bool:
        pend = st.pending
        sig: Signal = pend["signal"]
        fill_price = _r2(fill_price)
        reason = None
        qty = 0
        # The account as a fill in this candle may see it (see _Book.start_candle).
        realised = book.realised_for_fills()
        if book.open_count + book.released_count >= config.MAX_OPEN_POSITIONS:
            reason = "max open positions reached"
        elif book.trades_opened >= config.MAX_TRADES_PER_DAY:
            reason = "daily trade cap reached"
        elif book.loss_limit_hit(realised):
            reason = "daily loss limit reached"
            if not book.loss_limit_noted:
                book.loss_limit_noted = True
                book.notes.append("Daily loss limit hit before {}, no further entries.".format(_hm(st.stamps[i])))
        else:
            capital_now = book.start_capital + realised
            free_cash = capital_now - book.margin_in_use - book.released_margin
            qty, why = portfolio.position_size(fill_price, pend["stop"], free_cash, capital_now)
            if qty < 1:
                reason = why or "position size is zero"
        if reason is not None:
            logger.info("SKIP  %s %s %s: %s", _hm(st.stamps[i]), st.symbol, sig.direction, reason)
            resolve(st, i, "SKIPPED", reason)
            return False

        margin = qty * fill_price / config.LEVERAGE
        st.trades_today += 1
        book.trades_opened += 1
        book.open_count += 1
        book.margin_in_use += margin
        rec = pend["record"]
        rec["trade_id"] = "{}-{}-{}".format(day.isoformat(), st.symbol, st.trades_today)
        st.position = {
            "signal": sig,
            "direction": sig.direction,
            "entry": fill_price,
            "stop": pend["stop"],
            "target": pend["target"],
            "qty": qty,
            "margin": margin,
            "entry_idx": i,
            "bias": pend["bias"],
        }
        resolve(st, i, "FILLED", None)
        logger.info("FILL  %s %s %s %d @ %.2f  stop %.2f  target %.2f", _hm(st.stamps[i]), st.symbol,
                    sig.direction, qty, fill_price, pend["stop"], pend["target"])
        return True

    # 2. Walk the session.
    timeline = sorted({ts for st in syms for ts in st.stamps})
    for ts in timeline:
        book.start_candle()

        # 2.1 Exits first, for every stock: stop first, then target, then the square-off
        # clock. They depend only on prices, so doing them first takes the watchlist order
        # out of the result; what they release reaches fills only from the next candle.
        for st in syms:
            i = st.pos.get(ts)
            if i is None or st.position is None:
                continue
            o, h, l, c = st.o[i], st.h[i], st.l[i], st.c[i]
            pos = st.position
            if pos["direction"] == LONG:
                if l <= pos["stop"]:
                    close_position(st, i, min(pos["stop"], o), "STOP")
                elif h >= pos["target"]:
                    close_position(st, i, max(pos["target"], o), "TARGET")
            else:
                if h >= pos["stop"]:
                    close_position(st, i, max(pos["stop"], o), "STOP")
                elif l <= pos["target"]:
                    close_position(st, i, min(pos["target"], o), "TARGET")
            if st.position is not None and st.last_pre_sq is not None and i >= st.last_pre_sq:
                close_position(st, i, c, "EOD")

        # Then orders and signals, for every stock. When two orders fill in the same candle
        # and only one slot is left, the stock higher on the watchlist gets it.
        for st in syms:
            i = st.pos.get(ts)
            if i is None:
                continue
            o, h, l, c = st.o[i], st.h[i], st.l[i], st.c[i]
            clock = st.clocks[i]

            # 2.2 Pending limit order from an earlier bar.
            if st.pending is not None and i > st.pending["signal_idx"]:
                pend = st.pending
                pend["waited"] += 1
                if clock >= config.ENTRY_END:
                    resolve(st, i, "EXPIRED", "entry window closed at {}".format(config.ENTRY_END.strftime("%H:%M")))
                elif pend["waited"] > config.ORDER_TTL_BARS:
                    resolve(st, i, "EXPIRED", "not filled within {} minutes".format(
                        config.ORDER_TTL_BARS * BAR_MINUTES))
                # The limit price sits inside the gap, so a candle cannot close through the
                # gap without first trading at the limit: there is no separate "gap failed" case.
                elif pend["direction"] == LONG:
                    if o <= pend["stop"]:
                        resolve(st, i, "INVALIDATED", "price opened through the stop")
                    elif l <= pend["entry"]:
                        if try_fill(st, i, min(pend["entry"], o)) and l <= st.position["stop"]:
                            close_position(st, i, st.position["stop"], "STOP")
                else:
                    if o >= pend["stop"]:
                        resolve(st, i, "INVALIDATED", "price opened through the stop")
                    elif h >= pend["entry"]:
                        if try_fill(st, i, max(pend["entry"], o)) and h >= st.position["stop"]:
                            close_position(st, i, st.position["stop"], "STOP")

            # 2.3 New signal.
            if (
                st.position is None
                and st.pending is None
                and st.trades_today < config.MAX_TRADES_PER_SYMBOL
                and config.ENTRY_START <= clock < config.ENTRY_END
                and clock >= st.eligible_from
            ):
                h1_done, h_read, h1_fvgs, bias = hourly_context(st, i)
                st.evaluated += 1
                st.last_bias = bias
                st.bias_keys_seen.add((bias.weekly.bias, bias.daily.bias, bias.hourly.bias))
                if bias.direction not in (LONG, SHORT):
                    continue
                if bias.direction not in st.directions_seen:
                    st.directions_seen.append(bias.direction)

                full = st.start_pos + i
                wanted = BULL if bias.direction == LONG else BEAR
                levels = ict.liquidity_levels(st.prev5, st.daily_before, h1_done, st.today.iloc[: i + 1])
                window = st.df5.iloc[max(0, full + 1 - config.M5_WINDOW_BARS): full + 1]
                sig = ict.detect_setup(
                    st.symbol, window, day, bias.direction, levels,
                    [g for g in h1_fvgs if g.kind == wanted],
                )
                if sig is None:
                    continue
                entry, stop, target = _r2(sig.entry), _r2(sig.stop), _r2(sig.target)
                sane = (stop < entry < target) if sig.direction == LONG else (target < entry < stop)
                if sig.direction != bias.direction or not sane:
                    logger.warning("%s %s: ignoring an inconsistent signal (entry %.2f stop %.2f target %.2f)",
                                   st.symbol, _hm(ts), entry, stop, target)
                    continue

                bias_dict = _round_floats(bias.to_dict())
                rec = _round_floats(sig.to_dict())
                rec.update({
                    "name": st.name,
                    "status": "PENDING",
                    "skip_reason": None,
                    "rvol": st.rvol,
                    "bias": bias_dict,
                })
                st.records.append(rec)
                book.signals.append(rec)
                st.pending = {
                    "signal": sig,
                    "record": rec,
                    "signal_idx": i,
                    "waited": 0,
                    "direction": sig.direction,
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                    "bias": bias_dict,
                }
                logger.info("SIGNAL %s %s %s %s entry %.2f stop %.2f target %.2f (%s)", _hm(ts), st.symbol,
                            sig.direction, "+".join(sig.contexts), entry, stop, target, sig.target_kind)

    # 3. After the last stamp.
    for st in syms:
        if st.position is not None:
            # Only reachable when the fill bar was the symbol's last bar before square-off.
            j = st.last_pre_sq if st.last_pre_sq is not None else len(st.stamps) - 1
            j = max(j, st.position["entry_idx"])
            close_position(st, j, st.c[j], "EOD")
        if st.pending is not None:
            resolve(st, len(st.stamps) - 1, "EXPIRED", "session ended before the order filled")

    # 4. Charts, watchlist statuses, totals.
    for st in syms:
        for trade in st.trades:
            trade["chart"] = {"bars": _chart_bars(st, trade)}
            for key in ("_entry_idx", "_exit_idx"):
                trade.pop(key, None)

    for item in watch_items:
        st = by_symbol.get(item["symbol"])
        if st is None:
            why = "No 5-minute bars for this session."
            item["bias"] = Bias(
                weekly=_neutral_read("W", why), daily=_neutral_read("D", why), hourly=_neutral_read("H1", why),
                direction=None, aligned=0, reason=why,
            ).to_dict()
            item["status"] = "NO_SETUP"
            item["note"] = "No 5-minute bars were available for this session."
            continue
        # Show the read that mattered: the one behind the last signal, else the last one
        # taken inside the entry window, else the end-of-day read.
        final_bias = st.last_bias or hourly_context(st, len(st.stamps) - 1)[3]
        item["bias"] = st.records[-1]["bias"] if st.records else _round_floats(final_bias.to_dict())
        item["status"], item["note"] = _watch_status(st, final_bias)

    trades = sorted(book.trades, key=lambda t: t["exit_time"])
    signals = sorted(book.signals, key=lambda s: s["signal_time"])
    gross = _r2(sum(t["gross_pnl"] for t in trades))
    charges = _r2(sum(t["charges"] for t in trades))
    net = _r2(sum(t["net_pnl"] for t in trades))
    wins = sum(1 for t in trades if t["net_pnl"] > 0)

    notes = list(book.notes)
    if not watch_items:
        notes.append("No stock passed the volume scan, so there were no trades.")
    elif not trades:
        notes.append("{} stocks were in play but none completed a trade.".format(len(watch_items)))

    return {
        "date": day.isoformat(),
        "generated_at": datetime.now(_IST_TZ).isoformat(timespec="seconds"),
        "partial": longest < config.MIN_BARS_FOR_FULL_DAY,
        "universe_size": len(names) if names else len(bars5),
        "scanned": scanned,
        "start_capital": _r2(start_capital),
        "end_capital": _r2(start_capital + net),
        "gross_pnl": gross,
        "charges": charges,
        "net_pnl": net,
        "n_trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "watchlist": watch_items,
        "signals": signals,
        "trades": trades,
        "notes": notes,
        "config": config.snapshot(),
    }


# ── Result helpers ────────────────────────────────────────────────────────────

def _chart_bars(st: _Sym, trade: dict) -> List[list]:
    """The symbol's 5m bars from 12 before the context bar to 6 after the exit bar."""
    n = len(st.stamps)
    ctx_pos = None
    try:
        ctx = pd.Timestamp(trade["context_time"])
        index = st.today.index
        if ctx.tzinfo is None and getattr(index, "tz", None) is not None:
            ctx = ctx.tz_localize(IST)
        if getattr(index, "tz", None) is not None:
            ctx = ctx.tz_convert(index.tz)
        elif ctx.tzinfo is not None:
            ctx = ctx.tz_convert(IST).tz_localize(None)
        if index[0] <= ctx <= index[-1]:
            ctx_pos = int(index.searchsorted(ctx, side="left"))
    except (ValueError, TypeError, KeyError):
        ctx_pos = None
    if ctx_pos is None:      # context outside this session: fall back to the lookback before entry
        ctx_pos = max(0, trade["_entry_idx"] - config.CONTEXT_LOOKBACK_BARS)
    ctx_pos = min(ctx_pos, trade["_entry_idx"])
    lo = max(0, ctx_pos - CHART_BARS_BEFORE)
    hi = min(n - 1, trade["_exit_idx"] + CHART_BARS_AFTER)
    return [
        [_iso(st.stamps[k]), _r2(st.o[k]), _r2(st.h[k]), _r2(st.l[k]), _r2(st.c[k])]
        for k in range(lo, hi + 1)
    ]


def _watch_status(st: _Sym, final_bias: Bias) -> Tuple[str, str]:
    """(status, one-sentence note) for a watchlist row."""
    if st.trades:
        trade = st.trades[-1]
        side = "Long" if trade["direction"] == LONG else "Short"
        return "TRADED", "{} from {} at {}, {} at {}, {} net.".format(
            side, _money(trade["entry"]), trade["entry_time"][11:16],
            _EXIT_PHRASE.get(trade["exit_reason"], trade["exit_reason"].lower()),
            trade["exit_time"][11:16], _signed(trade["net_pnl"]),
        )
    if st.records:
        rec = st.records[-1]
        side = "Long" if rec["direction"] == LONG else "Short"
        return "SIGNAL_NOT_FILLED", "{} setup at {} with entry {} was not traded: {}.".format(
            side, rec["signal_time"][11:16], _money(rec["entry"]), rec.get("skip_reason") or rec["status"].lower(),
        )
    if st.evaluated == 0:
        return "NO_SETUP", "No 5-minute bar fell inside the entry window after it joined the watchlist."
    if not st.directions_seen:
        when = "all session" if len(st.bias_keys_seen) <= 1 else "at the last check"
        return "NOT_ALIGNED", _not_aligned_sentence(st.last_bias or final_bias, when)
    sides = " and ".join("longs" if d == LONG else "shorts" for d in st.directions_seen)
    part = "" if len(st.bias_keys_seen) <= 1 else " for part of the session"
    return "NO_SETUP", "Aligned for {}{}, but no sweep or 1-hour gap tap produced a 5-minute setup.".format(sides, part)


def _not_aligned_sentence(bias: Bias, when: str) -> str:
    w, d, h = bias.weekly.bias, bias.daily.bias, bias.hourly.bias
    dw, hw, ww = _BIAS_WORD.get(d, "neutral"), _BIAS_WORD.get(h, "neutral"), _BIAS_WORD.get(w, "neutral")
    if d == h and d in (BULL, BEAR):
        if d == BEAR and not config.ALLOW_SHORT and w != BULL:
            return "1-hour and daily bearish {}, but short selling is switched off.".format(when)
        return "1-hour and daily {} but weekly {} {}, so no trade direction.".format(dw, ww, when)
    if d == NEUTRAL and h == NEUTRAL:
        return "Daily and 1-hour both neutral {}, so no trade direction.".format(when)
    return "Daily {} but 1-hour {} {}, so no trade direction.".format(dw, hw, when)
