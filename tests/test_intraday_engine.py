"""Offline tests for the session replay (engine), the runner and the command line.

The engine is exercised on hand-built 5-minute frames. The analysis modules are
replaced with small fakes so each test controls exactly which signal appears
and when; the engine's own job (orders, fills, exits, account limits) is what
is under test here.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

from intraday_trader import __main__ as cli
from intraday_trader import config, engine, portfolio, runner
from intraday_trader.models import BEAR, BULL, FVG, LONG, SHORT, Bias, BiasRead, Signal

IST = "Asia/Kolkata"
PREV = date(2026, 11, 12)
DAY = date(2026, 11, 13)
NEXT = date(2026, 11, 16)


# ── Builders ──────────────────────────────────────────────────────────────────

def clock_range(start="09:15", end="15:25"):
    return [ts.strftime("%H:%M") for ts in pd.date_range("2026-01-01 " + start, "2026-01-01 " + end, freq="5min")]


def session(day, base, overrides=None, drop=()):
    """Flat candles at `base` every 5 minutes; `overrides` maps "HH:MM" to (o, h, l, c)."""
    overrides = overrides or {}
    rows, stamps = [], []
    for hm in clock_range():
        if hm in drop:
            continue
        o, h, l, c = overrides.get(hm, (base, base, base, base))
        stamps.append(pd.Timestamp("{} {}".format(day.isoformat(), hm), tz=IST))
        rows.append((o, h, l, c, 1000.0))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.DatetimeIndex(stamps))


def frame(base, overrides=None, drop=(), with_next=False):
    parts = [session(PREV, base), session(DAY, base, overrides, drop)]
    if with_next:
        parts.append(session(NEXT, base * 1.05))
    return pd.concat(parts)


def daily_frame(base):
    idx = pd.bdate_range(end=pd.Timestamp(NEXT), periods=40)
    return pd.DataFrame({"Open": base, "High": base, "Low": base, "Close": base, "Volume": 1e6}, index=idx)


def iso(hm, day=DAY):
    return pd.Timestamp("{} {}".format(day.isoformat(), hm), tz=IST).isoformat()


def signal(symbol, direction, hm, entry, stop, target, gap, context_hm=None, kind="PDH"):
    bottom, top = gap
    risk = abs(entry - stop)
    return Signal(
        symbol=symbol, direction=direction, signal_time=iso(hm), contexts=["SWEEP:PDL"],
        context_time=iso(context_hm or hm), swing_extreme=stop, mss_level=entry,
        fvg=FVG("M5", BULL if direction == LONG else BEAR, top, bottom, iso(hm), iso(hm)),
        entry=entry, stop=stop, target=target, target_kind=kind, structural_stop=stop,
        risk_per_share=risk, rr=abs(target - entry) / risk, story=["Test setup."],
    )


def guide_long(symbol="ABC", hm="10:40"):
    return signal(symbol, LONG, hm, 612.10, 609.65, 618.90, (611.60, 612.60), context_hm="10:25")


def guide_short(symbol="XYZ", hm="11:35"):
    return signal(symbol, SHORT, hm, 1482.80, 1487.94, 1468.40, (1482.10, 1483.50), context_hm="11:20", kind="PDL")


def watch(symbol, eligible="10:15", rank=1):
    return {"symbol": symbol, "name": symbol + " Ltd", "eligible_from": eligible, "rank": rank, "rvol": 2.3,
            "cum_volume": 1, "avg_cum_volume": 1.0, "turnover_cr": 100.0, "last_price": 1.0,
            "prev_close": 1.0, "day_change_pct": 0.0}


class Harness:
    """Runs engine.run_day with the analysis modules replaced by fakes."""

    def __init__(self, plan, watchlist, direction=LONG):
        self.plan = plan                    # {symbol: {"HH:MM": Signal}}
        self.watchlist = watchlist
        self.direction = direction          # LONG | SHORT | None (not aligned)
        self.detect_calls = []              # (symbol, "HH:MM" of the window's last bar)
        self.window_ends = []               # last candle of each 5-minute window handed to detect_setup
        self.level_ends = []                # last candle of "today" handed to liquidity_levels
        self.hourly_args = []               # (last candle handed to hourly_asof, its as_of)
        self.hourly_calls = 0

    def _detect(self, symbol, m5, day, direction, levels, h1_fvgs):
        hm = m5.index[-1].tz_convert(IST).strftime("%H:%M")
        self.detect_calls.append((symbol, hm))
        self.window_ends.append(m5.index[-1])
        return self.plan.get(symbol, {}).get(hm)

    def _hourly(self, df5, as_of):
        self.hourly_calls += 1
        self.hourly_args.append((df5.index[-1], as_of))
        return df5.iloc[:0]

    def _levels(self, prev5, daily_before, h1_done, today):
        self.level_ends.append(today.index[-1])
        return []

    def _combine(self, weekly, daily, hourly):
        return Bias(weekly, daily, hourly, self.direction, 3 if self.direction else 0, "test bias")

    def run(self, bars5, start_capital=100_000.0, daily=None, **cfg):
        tone = BEAR if self.direction == SHORT else BULL
        with contextlib.ExitStack() as stack:
            p = lambda obj, name, new: stack.enter_context(mock.patch.object(obj, name, new))
            p(engine.volume, "build_watchlist", lambda b, d, names=None: [dict(w) for w in self.watchlist])
            p(engine.ict, "structure_bias", lambda df, tf: BiasRead(tf, tone, "test read"))
            p(engine.ict, "combine_bias", self._combine)
            p(engine.ict, "liquidity_levels", self._levels)
            p(engine.ict, "find_fvgs", lambda *a, **k: [])
            p(engine.ict, "detect_setup", self._detect)
            p(engine.data, "hourly_asof", self._hourly)
            p(engine.data, "to_weekly", lambda df: df)
            for name, value in cfg.items():
                p(config, name, value)
            symbols = {w["symbol"] for w in self.watchlist}
            return engine.run_day(DAY, bars5, daily or {s: daily_frame(100.0) for s in symbols}, start_capital)


def run_long(overrides, drop=(), start_capital=100_000.0, **cfg):
    h = Harness({"ABC": {"10:40": guide_long()}}, [watch("ABC")])
    return h.run({"ABC": frame(613.0, overrides, drop)}, start_capital=start_capital, **cfg), h


FILL = {"10:50": (613.0, 613.2, 612.00, 612.9)}


# ── Fills and exits ───────────────────────────────────────────────────────────

class LongTradeTests(unittest.TestCase):
    def test_target_hit_reproduces_the_guide(self):
        result, _ = run_long(dict(FILL, **{"12:40": (618.0, 619.10, 617.9, 618.8)}))
        self.assertEqual(result["n_trades"], 1)
        t = result["trades"][0]
        self.assertEqual(t["id"], "2026-11-13-ABC-1")
        self.assertEqual((t["entry"], t["stop"], t["target"], t["exit_price"]), (612.10, 609.65, 618.90, 618.90))
        self.assertEqual((t["qty"], t["exposure"], t["margin"], t["risk_inr"]), (49, 29_992.90, 9_997.63, 120.05))
        self.assertEqual((t["gross_pnl"], t["charges"], t["net_pnl"]), (333.20, 25.0, 308.20))
        self.assertEqual((t["exit_reason"], t["outcome"], t["r_multiple"]), ("TARGET", "WIN", 2.57))
        self.assertEqual((t["entry_time"], t["exit_time"]), (iso("10:50"), iso("12:40")))
        self.assertEqual((result["net_pnl"], result["end_capital"], result["wins"], result["losses"]),
                         (308.20, 100_308.20, 1, 0))
        self.assertEqual(result["signals"][0]["status"], "FILLED")
        self.assertEqual(result["watchlist"][0]["status"], "TRADED")

    def test_stop_hit(self):
        result, _ = run_long(dict(FILL, **{"11:00": (612.5, 612.6, 609.60, 610.0)}))
        t = result["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["net_pnl"], t["r_multiple"], t["outcome"]),
                         ("STOP", 609.65, -145.05, -1.21, "LOSS"))
        self.assertEqual(result["end_capital"], 99_854.95)

    def test_candle_touching_stop_and_target_counts_as_stop(self):
        result, _ = run_long(dict(FILL, **{"11:00": (613.0, 619.5, 609.0, 615.0)}))
        self.assertEqual(result["trades"][0]["exit_reason"], "STOP")
        self.assertEqual(result["trades"][0]["exit_price"], 609.65)

    def test_stop_on_the_fill_candle(self):
        result, _ = run_long({"10:50": (613.0, 613.1, 609.0, 610.0)})
        t = result["trades"][0]
        self.assertEqual((t["entry"], t["exit_reason"], t["exit_price"]), (612.10, "STOP", 609.65))
        self.assertEqual(t["entry_time"], t["exit_time"])

    def test_target_is_not_credited_on_the_fill_candle(self):
        result, _ = run_long({"10:50": (613.0, 620.0, 612.0, 619.5)})
        t = result["trades"][0]
        self.assertEqual(t["exit_reason"], "EOD")                 # later candles are flat at 613
        self.assertEqual((t["exit_price"], t["net_pnl"]), (613.0, 19.10))

    def test_gap_open_below_the_limit_fills_at_the_open(self):
        result, _ = run_long({"10:50": (611.50, 612.0, 611.2, 611.8)})
        self.assertEqual(result["trades"][0]["entry"], 611.50)

    def test_gap_through_the_stop_exits_at_the_open(self):
        result, _ = run_long(dict(FILL, **{"11:00": (608.00, 608.5, 607.5, 608.2)}))
        t = result["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["net_pnl"]), ("STOP", 608.00, -225.90))

    def test_gap_above_the_target_exits_at_the_open(self):
        result, _ = run_long(dict(FILL, **{"11:00": (620.00, 620.5, 619.5, 620.2)}))
        t = result["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("TARGET", 620.00))

    def test_square_off_uses_the_last_candle_before_1515(self):
        result, _ = run_long(dict(FILL, **{"15:10": (614.0, 614.5, 613.8, 614.20),
                                           "15:15": (700.0, 700.0, 700.0, 700.0)}))
        t = result["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["exit_time"]), ("EOD", 614.20, iso("15:10")))

    def test_square_off_when_the_1510_candle_is_missing(self):
        result, _ = run_long(dict(FILL, **{"15:05": (614.0, 614.5, 613.8, 614.40)}), drop=("15:10",))
        t = result["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_price"], t["exit_time"]), ("EOD", 614.40, iso("15:05")))

    def test_chart_window(self):
        result, _ = run_long(dict(FILL, **{"12:40": (618.0, 619.10, 617.9, 618.8)}))
        bars = result["trades"][0]["chart"]["bars"]
        self.assertEqual(bars[0][0], iso("09:25"))       # 12 candles before the 10:25 context candle
        self.assertEqual(bars[-1][0], iso("13:10"))      # 6 candles after the 12:40 exit
        self.assertTrue(all(len(b) == 5 for b in bars))


class OrderLifeTests(unittest.TestCase):
    def test_order_expires_after_the_ttl(self):
        result, _ = run_long({})
        s = result["signals"][0]
        self.assertEqual((s["status"], result["n_trades"]), ("EXPIRED", 0))
        self.assertEqual(s["resolved_time"], iso("11:15"))       # 6 candles may fill; the 7th expires it
        self.assertEqual(result["watchlist"][0]["status"], "SIGNAL_NOT_FILLED")

    def test_order_fills_on_the_last_candle_of_its_life(self):
        result, _ = run_long({"11:10": (613.0, 613.2, 612.00, 612.9)})
        self.assertEqual(result["trades"][0]["entry_time"], iso("11:10"))

    def test_signal_candle_itself_never_fills(self):
        h = Harness({"ABC": {"10:40": guide_long()}}, [watch("ABC")])
        result = h.run({"ABC": frame(613.0, {"10:40": (613.0, 613.2, 612.00, 612.9)})})
        self.assertEqual(result["n_trades"], 0)

    def test_open_through_the_stop_invalidates_the_order(self):
        result, _ = run_long({"10:45": (609.0, 609.5, 608.5, 609.2)})
        s = result["signals"][0]
        self.assertEqual((s["status"], s["skip_reason"]), ("INVALIDATED", "price opened through the stop"))
        self.assertEqual(result["n_trades"], 0)

    def test_the_order_is_invalidated_on_the_candle_that_opens_through_the_stop(self):
        # 10:45 is an ordinary candle; 10:50 opens below the stop. The order lives through
        # 10:45 and is cancelled at 10:50, not a candle early.
        result, _ = run_long({"10:50": (609.0, 609.5, 608.5, 609.2)})
        s = result["signals"][0]
        self.assertEqual((s["status"], s["resolved_time"]), ("INVALIDATED", iso("10:50")))

    def test_order_is_cancelled_at_entry_end(self):
        h = Harness({"ABC": {"14:20": guide_long(hm="14:20")}}, [watch("ABC")])
        result = h.run({"ABC": frame(613.0, {"14:30": (613.0, 613.2, 612.00, 612.9)})})
        s = result["signals"][0]
        self.assertEqual((s["status"], s["resolved_time"]), ("EXPIRED", iso("14:30")))
        self.assertIn("entry window closed", s["skip_reason"])
        self.assertEqual(result["n_trades"], 0)


class ShortTradeTests(unittest.TestCase):
    def run_short(self, overrides, drop=()):
        h = Harness({"XYZ": {"11:35": guide_short()}}, [watch("XYZ")], direction=SHORT)
        return h.run({"XYZ": frame(1481.0, overrides, drop)})

    FILL = {"11:45": (1481.0, 1483.0, 1480.5, 1481.5)}

    def test_square_off_reproduces_the_guide(self):
        result = self.run_short(dict(self.FILL, **{"15:10": (1475.0, 1475.5, 1474.0, 1474.30)}))
        t = result["trades"][0]
        self.assertEqual((t["direction"], t["entry"], t["qty"], t["risk_inr"]), ("SHORT", 1482.80, 20, 102.80))
        self.assertEqual((t["exit_reason"], t["exit_price"], t["gross_pnl"], t["net_pnl"], t["r_multiple"]),
                         ("EOD", 1474.30, 170.0, 145.0, 1.41))

    def test_short_stop_and_target(self):
        stopped = self.run_short(dict(self.FILL, **{"12:00": (1486.0, 1488.5, 1485.0, 1487.0)}))["trades"][0]
        self.assertEqual((stopped["exit_reason"], stopped["exit_price"], stopped["net_pnl"]), ("STOP", 1487.94, -127.80))
        won = self.run_short(dict(self.FILL, **{"12:00": (1470.0, 1470.5, 1468.0, 1469.0)}))["trades"][0]
        self.assertEqual((won["exit_reason"], won["exit_price"], won["net_pnl"]), ("TARGET", 1468.40, 263.0))

    def test_short_both_touched_counts_as_stop(self):
        t = self.run_short(dict(self.FILL, **{"12:00": (1481.0, 1489.0, 1460.0, 1470.0)}))["trades"][0]
        self.assertEqual(t["exit_reason"], "STOP")

    def test_short_gap_fill_and_open_through_stop(self):
        t = self.run_short({"11:45": (1484.00, 1484.5, 1483.5, 1484.2)})["trades"][0]
        self.assertEqual(t["entry"], 1484.00)                      # opened above the sell limit: better price
        result = self.run_short({"11:40": (1488.5, 1489.0, 1488.0, 1488.2)})
        self.assertEqual(result["signals"][0]["status"], "INVALIDATED")


# ── Account limits ────────────────────────────────────────────────────────────

class LimitTests(unittest.TestCase):
    def two_symbols(self, second_hm="10:40"):
        plan = {"ABC": {"10:40": guide_long("ABC")}, "DEF": {second_hm: guide_long("DEF", second_hm)}}
        return Harness(plan, [watch("ABC"), watch("DEF", rank=2)])

    def test_max_open_positions(self):
        bars = {"ABC": frame(613.0, FILL), "DEF": frame(613.0, FILL)}
        result = self.two_symbols().run(bars, MAX_OPEN_POSITIONS=1)
        self.assertEqual([t["symbol"] for t in result["trades"]], ["ABC"])
        skipped = [s for s in result["signals"] if s["symbol"] == "DEF"][0]
        self.assertEqual((skipped["status"], skipped["skip_reason"]), ("SKIPPED", "max open positions reached"))

    def test_max_trades_per_day(self):
        bars = {"ABC": frame(613.0, dict(FILL, **{"11:00": (612.5, 612.6, 609.60, 610.0)})),
                "DEF": frame(613.0, {"11:40": (613.0, 613.2, 612.00, 612.9)})}
        result = self.two_symbols("11:30").run(bars, MAX_TRADES_PER_DAY=1)
        skipped = [s for s in result["signals"] if s["symbol"] == "DEF"][0]
        self.assertEqual((skipped["status"], skipped["skip_reason"]), ("SKIPPED", "daily trade cap reached"))
        self.assertEqual(result["n_trades"], 1)

    def test_daily_loss_limit(self):
        bars = {"ABC": frame(613.0, dict(FILL, **{"11:00": (612.5, 612.6, 609.60, 610.0)})),
                "DEF": frame(613.0, {"11:40": (613.0, 613.2, 612.00, 612.9)})}
        result = self.two_symbols("11:30").run(bars, DAILY_LOSS_LIMIT_PCT=0.001)      # 100 rupees
        skipped = [s for s in result["signals"] if s["symbol"] == "DEF"][0]
        self.assertEqual((skipped["status"], skipped["skip_reason"]), ("SKIPPED", "daily loss limit reached"))
        self.assertTrue(any("Daily loss limit" in note for note in result["notes"]))

    def test_one_trade_per_symbol(self):
        result, h = run_long(dict(FILL, **{"11:00": (612.5, 612.6, 609.60, 610.0)}))
        self.assertEqual(result["n_trades"], 1)
        self.assertEqual(max(hm for _, hm in h.detect_calls), "10:40")     # never looked again after trading

    def test_share_price_above_the_limit_is_skipped(self):
        sig = signal("BIG", LONG, "10:40", 31_000.0, 30_900.0, 31_300.0, (30_990.0, 31_010.0))
        h = Harness({"BIG": {"10:40": sig}}, [watch("BIG")])
        result = h.run({"BIG": frame(31_100.0, {"10:50": (31_100.0, 31_120.0, 30_995.0, 31_050.0)})})
        s = result["signals"][0]
        self.assertEqual((s["status"], s["skip_reason"]), ("SKIPPED", "share price above the per-trade limit"))

    def test_margin_is_released_when_a_trade_closes(self):
        # Capital for one position only; the second trade opens after the first has closed.
        bars = {"ABC": frame(613.0, dict(FILL, **{"11:00": (613.0, 619.5, 612.5, 619.0)})),
                "DEF": frame(613.0, {"11:40": (613.0, 613.2, 612.00, 612.9)})}
        result = self.two_symbols("11:30").run(bars, start_capital=10_000.0, MAX_RISK_PCT_PER_TRADE=1.0)
        self.assertEqual([(t["symbol"], t["qty"]) for t in result["trades"]], [("ABC", 49), ("DEF", 49)])

    def test_no_free_margin_while_the_first_trade_is_open(self):
        bars = {"ABC": frame(613.0, FILL), "DEF": frame(613.0, FILL)}
        result = self.two_symbols().run(bars, start_capital=10_000.0, MAX_RISK_PCT_PER_TRADE=1.0)
        self.assertEqual([t["symbol"] for t in result["trades"]], ["ABC"])
        skipped = [s for s in result["signals"] if s["symbol"] == "DEF"][0]
        self.assertEqual((skipped["status"], skipped["skip_reason"]), ("SKIPPED", "no free margin"))

    def test_risk_cap_shrinks_the_position_on_a_small_account(self):
        # 1% of 10,000 = 100 of risk; 2.45 per share -> 40 shares instead of 49
        result, _ = run_long(FILL, start_capital=10_000.0)
        self.assertEqual(result["trades"][0]["qty"], 40)


class SameCandleTests(unittest.TestCase):
    """ABC's open long is stopped in the 11:00 candle and DEF's order is touched in that same
    candle. A 5-minute candle cannot say which came first, so whatever the stop gives back
    is not available to DEF until the next candle, the loss counts at once, and the result
    must not depend on the order of the watchlist."""

    STOP_1100 = (612.5, 612.6, 609.60, 610.0)
    TOUCH = (613.0, 613.2, 612.00, 612.9)

    def run_pair(self, order, fill_at="11:00", start_capital=100_000.0, **cfg):
        plan = {"ABC": {"10:40": guide_long("ABC")}, "DEF": {"10:55": guide_long("DEF", "10:55")}}
        items = {"ABC": watch("ABC"), "DEF": watch("DEF", rank=2)}
        bars = {"ABC": frame(613.0, dict(FILL, **{"11:00": self.STOP_1100})),
                "DEF": frame(613.0, {fill_at: self.TOUCH})}
        result = Harness(plan, [items[s] for s in order]).run(bars, start_capital=start_capital, **cfg)
        trades = [(t["symbol"], t["exit_reason"]) for t in result["trades"]]
        others = [(s["symbol"], s["status"], s["skip_reason"]) for s in result["signals"] if s["status"] != "FILLED"]
        return trades, others

    def assert_both_orders(self, expected_skip, start_capital=100_000.0, **cfg):
        for order in (["ABC", "DEF"], ["DEF", "ABC"]):
            trades, others = self.run_pair(order, start_capital=start_capital, **cfg)
            self.assertEqual(trades, [("ABC", "STOP")], order)
            self.assertEqual(others, [("DEF", "SKIPPED", expected_skip)], order)

    def test_a_slot_freed_in_the_same_candle_is_not_used(self):
        self.assert_both_orders("max open positions reached", MAX_OPEN_POSITIONS=1)

    def test_a_loss_in_the_same_candle_counts_against_the_loss_limit(self):
        self.assert_both_orders("daily loss limit reached", DAILY_LOSS_LIMIT_PCT=0.001)   # limit 100, loss 145.05

    def test_margin_freed_in_the_same_candle_is_not_used(self):
        self.assert_both_orders("no free margin", start_capital=10_000.0, MAX_RISK_PCT_PER_TRADE=1.0)

    def test_what_a_close_frees_is_available_from_the_next_candle(self):
        for order in (["ABC", "DEF"], ["DEF", "ABC"]):
            trades, others = self.run_pair(order, fill_at="11:05", MAX_OPEN_POSITIONS=1)
            self.assertEqual(sorted(trades), [("ABC", "STOP"), ("DEF", "EOD")], order)
            self.assertEqual(others, [], order)


class DataEndTests(unittest.TestCase):
    def test_data_that_stops_early_is_not_called_a_square_off(self):
        result, _ = run_long(FILL, drop=tuple(clock_range("12:00", "15:25")))
        t = result["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_time"]), ("EOD", iso("11:55")))
        self.assertIn("No 5-minute candles after the 11:55 candle", t["story"][-2])
        self.assertIn("ABC: no 5-minute data after 11:55", " ".join(result["notes"]))

    def test_a_regular_square_off_keeps_its_wording(self):
        result, _ = run_long(FILL)
        t = result["trades"][0]
        self.assertEqual((t["exit_reason"], t["exit_time"]), ("EOD", iso("15:10")))
        self.assertTrue(t["story"][-2].startswith("Still open at square-off"))
        self.assertFalse([n for n in result["notes"] if "no 5-minute data" in n])


# ── What the engine looks at, and when ────────────────────────────────────────

class TimingTests(unittest.TestCase):
    def test_entry_window(self):
        _, h = run_long({})
        clocks = sorted({hm for _, hm in h.detect_calls})
        self.assertEqual(clocks[0], "10:15")
        self.assertLess(clocks[-1], "14:30")

    def test_eligible_from_is_respected(self):
        h = Harness({}, [watch("ABC", eligible="11:15")])
        h.run({"ABC": frame(613.0)})
        self.assertEqual(min(hm for _, hm in h.detect_calls), "11:15")

    def test_no_detection_while_an_order_is_pending(self):
        _, h = run_long({})
        seen = [hm for _, hm in h.detect_calls]
        for hm in ("10:45", "10:50", "10:55", "11:00", "11:05", "11:10"):
            self.assertNotIn(hm, seen)
        self.assertIn("11:15", seen)                               # looks again once the order has expired

    def test_hourly_analysis_is_cached_per_completed_hour(self):
        _, h = run_long({})
        self.assertLessEqual(h.hourly_calls, 6)
        self.assertGreaterEqual(h.hourly_calls, 4)

    def test_helpers_are_given_candles_up_to_the_current_one(self):
        # The 1-hour helper gets the 5-minute candles up to the current one and as_of equal to
        # that candle's close; the level helper gets today's candles up to the same candle that
        # ends the entry window. The next session is in the data, so a peek would show.
        h = Harness({}, [watch("ABC")])
        h.run({"ABC": frame(613.0, with_next=True)})
        self.assertTrue(h.hourly_args)
        for last, as_of in h.hourly_args:
            self.assertEqual(last.tz_convert(IST).date(), DAY)
            self.assertEqual(as_of - last, pd.Timedelta(minutes=5))
        self.assertTrue(h.level_ends)
        self.assertEqual(h.level_ends, h.window_ends)

    def test_later_sessions_do_not_change_the_result(self):
        overrides = dict(FILL, **{"12:40": (618.0, 619.10, 617.9, 618.8)})
        h1 = Harness({"ABC": {"10:40": guide_long()}}, [watch("ABC")])
        h2 = Harness({"ABC": {"10:40": guide_long()}}, [watch("ABC")])
        a = h1.run({"ABC": frame(613.0, overrides)})
        b = h2.run({"ABC": frame(613.0, overrides, with_next=True)})
        a.pop("generated_at"); b.pop("generated_at")
        self.assertEqual(a, b)

    def test_window_never_contains_a_future_bar(self):
        seen = []

        def detect(symbol, m5, day, direction, levels, h1_fvgs):
            seen.append((m5.index[-1], len(m5)))
            return None

        h = Harness({}, [watch("ABC")])
        h._detect = detect
        h.run({"ABC": frame(613.0, with_next=True)})
        self.assertTrue(all(ts.tz_convert(IST).date() == DAY for ts, _ in seen))
        self.assertTrue(all(n <= config.M5_WINDOW_BARS for _, n in seen))
        self.assertEqual([ts for ts, _ in seen], sorted(ts for ts, _ in seen))


class StatusTests(unittest.TestCase):
    def test_empty_watchlist(self):
        result = Harness({}, []).run({"ABC": frame(613.0)})
        self.assertEqual((result["n_trades"], result["net_pnl"], result["end_capital"]), (0, 0.0, 100_000.0))
        self.assertTrue(any("volume scan" in note for note in result["notes"]))
        self.assertEqual((result["watchlist"], result["signals"], result["trades"]), ([], [], []))

    def test_not_aligned(self):
        h = Harness({"ABC": {"10:40": guide_long()}}, [watch("ABC")], direction=None)
        result = h.run({"ABC": frame(613.0, FILL)})
        self.assertEqual(result["watchlist"][0]["status"], "NOT_ALIGNED")
        self.assertEqual((h.detect_calls, result["signals"]), ([], []))

    def test_no_setup(self):
        result = Harness({}, [watch("ABC")]).run({"ABC": frame(613.0)})
        item = result["watchlist"][0]
        self.assertEqual(item["status"], "NO_SETUP")
        self.assertEqual(item["bias"]["direction"], LONG)
        self.assertTrue(item["note"])

    def test_result_shape(self):
        result, _ = run_long(dict(FILL, **{"12:40": (618.0, 619.10, 617.9, 618.8)}))
        for key in ("date", "generated_at", "partial", "universe_size", "scanned", "start_capital", "end_capital",
                    "gross_pnl", "charges", "net_pnl", "n_trades", "wins", "losses", "watchlist", "signals",
                    "trades", "notes", "config"):
            self.assertIn(key, result)
        self.assertEqual((result["date"], result["partial"]), ("2026-11-13", False))
        json.dumps(result)                                          # everything is JSON-serialisable
        trade = result["trades"][0]
        self.assertFalse([k for k in trade if k.startswith("_")])
        self.assertEqual(trade["story"][0], "Test setup.")
        self.assertGreater(len(trade["story"]), 1)

    def test_partial_day(self):
        drop = tuple(clock_range("11:00", "15:25"))
        result = Harness({}, [watch("ABC")]).run({"ABC": frame(613.0, drop=drop)})
        self.assertTrue(result["partial"])


# ── Runner and command line ───────────────────────────────────────────────────

def fake_day(day, bars5, daily, start_capital, names=None):
    trade = {"id": "{}-ABC-1".format(day.isoformat()), "date": day.isoformat(), "symbol": "ABC", "net_pnl": 100.0,
             "gross_pnl": 125.0, "charges": 25.0, "r_multiple": 1.0, "exit_time": iso("12:00", day),
             "chart": {"bars": []}}
    return {"date": day.isoformat(), "generated_at": "x", "partial": False, "universe_size": 1, "scanned": 1,
            "start_capital": start_capital, "end_capital": start_capital + 100.0, "gross_pnl": 125.0,
            "charges": 25.0, "net_pnl": 100.0, "n_trades": 1, "wins": 1, "losses": 0, "watchlist": [{}],
            "signals": [], "trades": [trade], "notes": [], "config": {}}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reports = Path(self.tmp.name) / "reports"
        self.pfile = Path(self.tmp.name) / "portfolio.json"
        days = [d.date() for d in pd.bdate_range(end=pd.Timestamp(DAY), periods=8)]
        bars = pd.concat([session(d, 100.0) for d in days])
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        p = lambda obj, name, new: stack.enter_context(mock.patch.object(obj, name, new))
        p(runner.data, "load_universe", lambda *a, **k: [{"symbol": "ABC", "name": "ABC Ltd"}])
        p(runner.data, "fetch_5m", lambda symbols, period=None: {"ABC": bars})
        p(runner.data, "fetch_daily", lambda symbols, period=None: {"ABC": daily_frame(100.0)})
        self.run_day = p(runner.engine, "run_day", mock.Mock(side_effect=fake_day))
        p(runner.report, "build_daily_html", lambda result, state: "<html>daily</html>")
        p(runner.report, "build_backtest_html", lambda results, state: "<html>backtest</html>")
        self.clock = p(runner, "_now_ist", mock.Mock(return_value=self.at(DAY, 18, 0)))   # after the close
        self.days = days

    @staticmethod
    def at(day, hour, minute):
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def live(self, **kwargs):
        return runner.run_live(reports_dir=self.reports, portfolio_file=self.pfile, **kwargs)

    def test_run_while_the_market_is_open_is_partial_and_replaced_later(self):
        self.clock.return_value = self.at(DAY, 14, 40)       # enough bars to look like a full day
        out = self.live()
        self.assertEqual((out["status"], out["date"], out["partial"]), ("ok", "2026-11-13", True))
        saved = json.loads((self.reports / "INTRADAY-DAILY-2026-11-13.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["partial"])
        self.assertIn("14:40 IST", saved["notes"][-1])
        self.assertTrue(portfolio.load_state(self.pfile)["days"][0]["partial"])
        self.clock.return_value = self.at(DAY, 16, 0)
        again = self.live()                                   # not "already_recorded"
        self.assertEqual((again["status"], again["partial"]), ("ok", False))
        state = portfolio.load_state(self.pfile)
        self.assertEqual((len(state["days"]), state["days"][0]["partial"], state["capital"]), (1, False, 100_100.0))

    def test_an_earlier_session_is_not_partial_while_today_is_open(self):
        self.clock.return_value = self.at(NEXT, 11, 0)
        out = self.live(day=DAY)
        self.assertFalse(out["partial"])

    def test_backtest_leaves_out_the_session_still_in_progress(self):
        self.clock.return_value = self.at(DAY, 12, 15)
        out = runner.run_backtest(days=30, reports_dir=self.reports)
        self.assertEqual(out["last_date"], self.days[-2].isoformat())
        self.assertEqual(out["actual_days"], len(self.days) - 1 - config.RVOL_MIN_SESSIONS)
        self.assertNotIn(DAY, [c.args[0] for c in self.run_day.call_args_list])

    def test_run_live_records_the_latest_session(self):
        out = self.live()
        self.assertEqual((out["status"], out["date"], out["dry_run"]), ("ok", "2026-11-13", False))
        self.assertEqual((out["start_capital"], out["end_capital"], out["n_trades"]), (100_000.0, 100_100.0, 1))
        self.assertTrue((self.reports / "INTRADAY-DAILY-2026-11-13.html").exists())
        saved = json.loads((self.reports / "INTRADAY-DAILY-2026-11-13.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["date"], "2026-11-13")
        state = portfolio.load_state(self.pfile)
        self.assertEqual((state["capital"], len(state["days"]), len(state["trades"])), (100_100.0, 1, 1))

    def test_second_run_is_a_no_op_unless_forced(self):
        self.live()
        again = self.live()
        self.assertEqual(again, {"status": "already_recorded", "date": "2026-11-13"})
        self.assertEqual(self.run_day.call_count, 1)
        forced = self.live(force=True)
        self.assertEqual(forced["status"], "ok")
        state = portfolio.load_state(self.pfile)
        self.assertEqual((state["capital"], len(state["days"]), len(state["trades"])), (100_100.0, 1, 1))

    def test_dry_run_leaves_the_portfolio_alone(self):
        out = self.live(dry_run=True)
        self.assertEqual((out["status"], out["dry_run"]), ("ok", True))
        self.assertFalse(self.pfile.exists())
        self.assertTrue((self.reports / "INTRADAY-DAILY-2026-11-13-DRYRUN.html").exists())
        self.assertTrue((self.reports / "INTRADAY-DAILY-2026-11-13-DRYRUN.json").exists())
        self.assertFalse((self.reports / "INTRADAY-DAILY-2026-11-13.html").exists())

    def test_explicit_date_and_capital_chain(self):
        self.live(day=self.days[-2])
        out = self.live(day=self.days[-1])
        self.assertEqual((out["start_capital"], out["end_capital"]), (100_100.0, 100_200.0))

    def test_date_without_bars_is_no_data(self):
        out = self.live(day=date(2026, 11, 14))            # a Saturday
        self.assertEqual(out, {"status": "no_data", "date": "2026-11-14"})
        self.assertFalse(self.pfile.exists())

    def test_nothing_downloaded_is_no_data(self):
        with mock.patch.object(runner.data, "fetch_5m", lambda symbols, period=None: {}):
            self.assertEqual(self.live(), {"status": "no_data"})

    def test_backtest_uses_a_fresh_portfolio_and_never_the_live_file(self):
        with mock.patch.object(runner.portfolio, "load_state", side_effect=AssertionError("live file read")), \
                mock.patch.object(runner.portfolio, "save_state", side_effect=AssertionError("live file written")):
            out = runner.run_backtest(days=30, reports_dir=self.reports)
        usable = len(self.days) - config.RVOL_MIN_SESSIONS
        self.assertEqual((out["status"], out["requested_days"], out["actual_days"]), ("ok", 30, usable))
        self.assertEqual((out["first_date"], out["last_date"]),
                         (self.days[-usable].isoformat(), self.days[-1].isoformat()))
        self.assertEqual(out["summary"]["net_pnl"], 100.0 * usable)
        self.assertEqual([c.args[3] for c in self.run_day.call_args_list],
                         [100_000.0 + 100.0 * k for k in range(usable)])
        self.assertTrue(Path(out["report_path"]).exists())
        payload = json.loads(Path(out["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(sorted(payload), ["days", "summary", "trades"])
        self.assertTrue(all("chart" not in t for t in payload["trades"]))
        self.assertFalse(self.pfile.exists())


class CliTests(unittest.TestCase):
    def call(self, argv, run_live=None, run_backtest=None):
        out = io.StringIO()
        with mock.patch.object(cli.runner, "run_live", run_live or mock.Mock()), \
                mock.patch.object(cli.runner, "run_backtest", run_backtest or mock.Mock()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(argv)
        return code, out.getvalue()

    OK = {"status": "ok", "date": "2026-11-13", "dry_run": True, "report_path": "r.html", "json_path": "r.json",
          "start_capital": 100_000.0, "end_capital": 100_308.2, "net_pnl": 308.2, "n_trades": 1, "wins": 1,
          "losses": 0, "watchlist": 14, "partial": False}

    def test_json_mode_prints_exactly_one_document(self):
        live = mock.Mock(return_value=dict(self.OK))
        code, text = self.call(["run", "--json", "--date", "2026-11-13", "--dry-run", "--force"], run_live=live)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text), self.OK)
        live.assert_called_once_with(day=date(2026, 11, 13), dry_run=True, force=True)

    def test_human_summary(self):
        code, text = self.call(["run"], run_live=mock.Mock(return_value=dict(self.OK)))
        self.assertEqual(code, 0)
        self.assertIn("2026-11-13", text)
        self.assertIn("+₹308.20", text)
        self.assertIn(config.DISCLAIMER, text)

    def test_exit_codes(self):
        recorded = mock.Mock(return_value={"status": "already_recorded", "date": "2026-11-13"})
        self.assertEqual(self.call(["run", "--json"], run_live=recorded)[0], 0)
        self.assertEqual(self.call(["run", "--json"], run_live=mock.Mock(return_value={"status": "no_data"}))[0], 2)
        code, text = self.call(["run", "--json"], run_live=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual((code, json.loads(text)), (1, {"status": "error", "error": "boom"}))

    def test_backtest_command(self):
        summary = portfolio.summarize(portfolio.new_state())
        result = {"status": "ok", "requested_days": 30, "actual_days": 30, "first_date": "2026-10-01",
                  "last_date": "2026-11-13", "report_path": "b.html", "json_path": "b.json", "summary": summary}
        back = mock.Mock(return_value=result)
        code, text = self.call(["backtest", "--days", "30", "--json"], run_backtest=back)
        self.assertEqual((code, json.loads(text)["actual_days"]), (0, 30))
        back.assert_called_once_with(days=30)
        code, text = self.call(["backtest"], run_backtest=mock.Mock(return_value=result))
        self.assertEqual(code, 0)
        self.assertIn("BACKTEST", text)


if __name__ == "__main__":
    unittest.main()
