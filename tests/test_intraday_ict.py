"""Tests for intraday_trader/ict.py. Synthetic frames only, no network.

Run from the repo root:
    python -m unittest discover -s tests -p "test_intraday_ict.py"
"""
from __future__ import annotations

import time as _time
import unittest
from collections import OrderedDict
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

from intraday_trader import config, ict
from intraday_trader.models import BEAR, BSL, BULL, FVG, LONG, NEUTRAL, SHORT, SSL, BiasRead, Level

IST = "Asia/Kolkata"
PREV = date(2026, 11, 12)    # Thursday
DAY = date(2026, 11, 13)     # Friday


# ── Frame builders ────────────────────────────────────────────────────────────

def intraday_frame(rows):
    """rows: [("YYYY-MM-DD HH:MM", open, high, low, close), ...] -> tz-aware IST frame."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows]).tz_localize(IST)
    return pd.DataFrame(
        {
            "Open": [float(r[1]) for r in rows],
            "High": [float(r[2]) for r in rows],
            "Low": [float(r[3]) for r in rows],
            "Close": [float(r[4]) for r in rows],
            "Volume": 1000.0,
        },
        index=idx,
    )


def day_rows(day, bars):
    """bars: {"HH:MM": (o, h, l, c)} -> rows for intraday_frame."""
    return [(f"{day} {hm}",) + tuple(v) for hm, v in bars.items()]


def daily_frame(rows):
    """rows: [("YYYY-MM-DD", high, low, close), ...] -> tz-naive daily frame (Open = Close)."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    return pd.DataFrame(
        {
            "Open": [float(r[3]) for r in rows],
            "High": [float(r[1]) for r in rows],
            "Low": [float(r[2]) for r in rows],
            "Close": [float(r[3]) for r in rows],
            "Volume": 1000.0,
        },
        index=idx,
    )


HIGH_NAMES = {"PDH", "PWH", "ORH", "HOD", "H1_SH", "D_SH", "M5_SH"}


def lv(name, price, when=None):
    return Level(name=name, price=float(price), side=BSL if name in HIGH_NAMES else SSL, time=when)


def empty5():
    return intraday_frame([])


# ── The guide's worked LONG example (docs/intraday-trader/README.md section 9) ─

PREV_TAIL = OrderedDict([     # last four bars of the previous session: bodies 0.90 0.90 0.95 0.95
    ("15:05", (613.00, 614.10, 612.90, 613.90)),
    ("15:10", (613.90, 614.90, 613.80, 614.80)),
    ("15:15", (614.80, 614.90, 613.80, 613.85)),
    ("15:20", (613.85, 614.90, 613.80, 614.80)),
])


def long_bars(**overrides):
    """Today's 5m bars 09:15-10:40 for the hypothetical stock ABC."""
    bars = OrderedDict([
        ("09:15", (614.00, 615.00, 613.60, 614.80)),
        ("09:20", (614.80, 615.40, 614.30, 614.50)),   # opening range high 615.40
        ("09:25", (614.50, 614.70, 613.50, 613.70)),
        ("09:30", (613.70, 613.90, 612.80, 613.00)),
        ("09:35", (613.00, 613.20, 612.10, 612.30)),
        ("09:40", (612.30, 612.50, 611.50, 611.70)),
        ("09:45", (611.70, 611.90, 610.90, 611.10)),
        ("09:50", (611.10, 611.30, 610.60, 611.00)),   # opening range low 610.60
        ("09:55", (611.00, 611.60, 610.90, 611.50)),
        ("10:00", (611.50, 612.00, 611.30, 611.90)),
        ("10:05", (611.90, 612.20, 611.50, 611.70)),   # 5m swing high 612.20
        ("10:10", (611.70, 611.90, 611.20, 611.40)),
        ("10:15", (611.40, 611.60, 610.90, 611.10)),
        ("10:20", (611.10, 611.30, 610.70, 610.80)),
        ("10:25", (610.80, 611.00, 609.95, 610.90)),   # sweeps PDL 610.20
        ("10:30", (610.90, 611.60, 610.70, 611.50)),   # candle 1 of the gap
        ("10:35", (611.50, 613.40, 611.40, 613.30)),   # displacement, closes above 612.20
        ("10:40", (613.30, 614.10, 612.60, 613.90)),   # low 612.60 > 611.60: the gap
    ])
    for key, value in overrides.items():
        bars[key.replace("_", ":")] = value
    return bars


def long_frame(**overrides):
    return intraday_frame(day_rows(PREV, PREV_TAIL) + day_rows(DAY, long_bars(**overrides)))


def long_levels():
    return [
        lv("PDL", 610.20), lv("ORL", 610.60), lv("LOD", 609.95),
        lv("ORH", 615.40), lv("HOD", 615.40), lv("PDH", 618.90),
    ]


def detect_long(levels=None, fvgs=None, **overrides):
    return ict.detect_setup(
        "ABC", long_frame(**overrides), DAY, LONG,
        long_levels() if levels is None else levels, fvgs or [],
    )


# ── The guide's worked SHORT example (section 10) ─────────────────────────────

def short_bars():
    """Today's 5m bars 09:15-11:35 for the hypothetical stock XYZ."""
    bars = OrderedDict([
        ("09:15", (1478.60, 1479.00, 1477.80, 1478.00)),
        ("09:20", (1478.00, 1478.50, 1476.90, 1477.10)),
        ("09:25", (1477.10, 1477.50, 1476.00, 1476.60)),   # low of the day 1,476.00
    ])
    price = 1476.60
    stamp = pd.Timestamp("2026-11-13 09:30")
    for _ in range(17):                                    # 09:30 .. 10:50, a steady climb
        o, c = price, round(price + 0.40, 2)
        bars[stamp.strftime("%H:%M")] = (o, round(c + 0.10, 2), round(o - 0.10, 2), c)
        price = c
        stamp += pd.Timedelta(minutes=5)
    bars.update(OrderedDict([
        ("10:55", (1483.40, 1484.60, 1483.30, 1484.40)),
        ("11:00", (1484.40, 1484.90, 1483.40, 1483.80)),
        ("11:05", (1483.80, 1485.00, 1482.50, 1484.80)),   # 5m swing low 1,482.50
        ("11:10", (1484.80, 1485.60, 1484.60, 1485.40)),
        ("11:15", (1485.40, 1485.90, 1484.60, 1485.20)),
        ("11:20", (1485.20, 1487.20, 1484.90, 1485.90)),   # rallies into the 1h gap
        ("11:25", (1485.90, 1486.10, 1483.50, 1483.90)),   # candle 1 of the gap
        ("11:30", (1483.90, 1484.00, 1481.20, 1481.50)),   # displacement, closes below 1,482.50
        ("11:35", (1481.50, 1482.10, 1479.60, 1480.00)),   # high 1,482.10 < 1,483.50: the gap
    ]))
    return bars


def short_frame():
    return intraday_frame(day_rows(DAY, short_bars()))


def short_levels():
    return [
        lv("PDH", 1495.00), lv("ORH", 1480.30), lv("HOD", 1487.20),
        lv("LOD", 1476.00), lv("ORL", 1476.00), lv("PDL", 1468.40),
    ]


def short_h1_gap(known_at="2026-11-12T14:15:00+05:30"):
    return FVG(
        timeframe="H1", kind=BEAR, top=1489.50, bottom=1486.00,
        created_time="2026-11-12T13:15:00+05:30", known_at=known_at,
    )


# ══════════════════════════════════════════════════════════════════════════════

class FindSwingsTest(unittest.TestCase):
    def frame(self, highs, lows=None):
        lows = lows if lows is not None else [h - 1 for h in highs]
        rows = [(f"2026-01-{i + 1:02d}", h, l, (h + l) / 2) for i, (h, l) in enumerate(zip(highs, lows))]
        return daily_frame(rows)

    def test_basic_high_and_low(self):
        swings = ict.find_swings(self.frame([1, 2, 5, 2, 1], [9, 8, 9, 3, 7]), 1)
        self.assertEqual(swings["highs"], [(2, 5.0)])
        self.assertEqual(swings["lows"], [(1, 8.0), (3, 3.0)])

    def test_strictly_greater_ties_are_not_swings(self):
        self.assertEqual(ict.find_swings(self.frame([1, 5, 5, 1, 0]), 1)["highs"], [])
        self.assertEqual(ict.find_swings(self.frame([1, 2, 5, 2, 5, 1, 0]), 2)["highs"], [])

    def test_needs_n_bars_on_each_side(self):
        # n = 2: the 5 has only one bar to its right, so it is not confirmed yet
        self.assertEqual(ict.find_swings(self.frame([1, 2, 5, 2]), 2)["highs"], [])
        self.assertEqual(ict.find_swings(self.frame([1, 2, 5, 2, 1]), 2)["highs"], [(2, 5.0)])
        # left edge: bar 0 can never be a swing
        self.assertEqual(ict.find_swings(self.frame([9, 2, 1, 0]), 1)["highs"], [])

    def test_confirmation_appears_only_when_the_right_side_exists(self):
        highs = [1, 2, 3, 9, 4, 3, 2]
        for k in range(1, len(highs) + 1):
            found = ict.find_swings(self.frame(highs[:k]), 2)["highs"]
            self.assertEqual(found, [(3, 9.0)] if k >= 6 else [], msg=f"k={k}")

    def test_empty_and_short(self):
        self.assertEqual(ict.find_swings(self.frame([]), 2), {"highs": [], "lows": []})
        self.assertEqual(ict.find_swings(self.frame([1, 2]), 2), {"highs": [], "lows": []})


class StructureBiasTest(unittest.TestCase):
    ROWS = [   # (date, high, low, close)
        ("2026-10-01", 100, 98, 99),
        ("2026-10-02", 101, 99, 100),
        ("2026-10-05", 105, 101, 103),     # swing high 105
        ("2026-10-06", 104, 100, 101),
        ("2026-10-07", 103, 99, 100),      # swing low 99 (confirmed two bars later)
        ("2026-10-08", 106, 100, 104),     # wick above 105, close below: no break
        ("2026-10-09", 107, 103, 106),     # close above 105: bullish BOS
        ("2026-10-12", 108, 105, 107),     # swing high 108
        ("2026-10-13", 107, 102, 103),
        ("2026-10-14", 104, 98.5, 98.8),   # close below 99: bearish BOS
    ]

    def test_short_history_is_neutral(self):
        read = ict.structure_bias(daily_frame(self.ROWS[:5]), "D")
        self.assertEqual(read.bias, NEUTRAL)
        self.assertEqual(read.reason, "not enough history")
        self.assertEqual(ict.structure_bias(daily_frame([]), "D").bias, NEUTRAL)

    def test_wick_above_without_a_close_is_not_a_break(self):
        read = ict.structure_bias(daily_frame(self.ROWS[:6]), "D")
        self.assertEqual(read.bias, NEUTRAL)
        self.assertIsNone(read.last_bos_time)
        self.assertEqual(read.swing_high, 105.0)      # still unbroken and tracked

    def test_close_above_swing_high_is_bullish(self):
        read = ict.structure_bias(daily_frame(self.ROWS[:7]), "D")
        self.assertEqual(read.bias, BULL)
        self.assertEqual(read.timeframe, "D")
        self.assertEqual(read.last_bos_level, 105.0)
        self.assertEqual(read.last_bos_time, "2026-10-09")
        self.assertEqual(read.reason, "Closed above the 105.00 swing high on 2026-10-09; last break of structure is up.")
        self.assertIsNone(read.swing_high)            # consumed by the break
        self.assertEqual(read.swing_low, 99.0)

    def test_later_close_below_swing_low_flips_to_bearish(self):
        read = ict.structure_bias(daily_frame(self.ROWS), "D")
        self.assertEqual(read.bias, BEAR)
        self.assertEqual(read.last_bos_level, 99.0)
        self.assertEqual(read.last_bos_time, "2026-10-14")
        self.assertIn("Closed below the 99.00 swing low on 2026-10-14", read.reason)
        self.assertIn("down", read.reason)
        self.assertEqual(read.swing_high, 108.0)
        self.assertIsNone(read.swing_low)

    def test_only_the_lookback_window_is_used(self):
        # A flat tail longer than the lookback hides the old break entirely.
        tail = [(str((pd.Timestamp("2026-10-15") + pd.Timedelta(days=i)).date()), 100, 100, 100) for i in range(12)]
        frame = daily_frame(self.ROWS + tail)
        with mock.patch.dict(config.BIAS_LOOKBACK_BARS, {"D": 10}):
            self.assertEqual(ict.structure_bias(frame, "D").bias, NEUTRAL)
        self.assertEqual(ict.structure_bias(frame, "D").bias, BEAR)

    def test_weekly_wording_and_swing_n(self):
        rows = [   # weekly frame, index = the Friday; n = 1 for "W"
            ("2026-09-25", 100, 95, 98),
            ("2026-10-02", 110, 99, 104),      # swing high 110 (n = 1)
            ("2026-10-09", 108, 101, 103),
            ("2026-10-16", 112, 102, 111),     # close above 110
        ]
        read = ict.structure_bias(daily_frame(rows), "W")
        self.assertEqual(read.bias, BULL)
        self.assertEqual(read.last_bos_time, "2026-10-16")
        self.assertIn("in the week ending 2026-10-16", read.reason)

    def test_hourly_uses_iso_time_and_clock_in_the_reason(self):
        stamps = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
        vals = [(100, 98, 99), (101, 99, 100), (105, 101, 103), (104, 100, 101),
                (103, 99, 100), (106, 100, 104), (107, 103, 106)]
        frame = intraday_frame([(f"2026-11-12 {s}", v[2], v[0], v[1], v[2]) for s, v in zip(stamps, vals)])
        read = ict.structure_bias(frame, "H1")
        self.assertEqual(read.bias, BULL)
        self.assertEqual(read.last_bos_time, "2026-11-12T15:15:00+05:30")
        self.assertIn("2026-11-12 15:15", read.reason)
        self.assertIn("105.00", read.reason)


class CombineBiasTest(unittest.TestCase):
    @staticmethod
    def read(tf, bias):
        return BiasRead(timeframe=tf, bias=bias, reason="x")

    def combine(self, w, d, h):
        return ict.combine_bias(self.read("W", w), self.read("D", d), self.read("H1", h))

    def test_truth_table(self):
        cases = [
            # weekly, daily, hourly -> direction, aligned
            (BULL, BULL, BULL, LONG, 3),
            (NEUTRAL, BULL, BULL, LONG, 2),
            (BEAR, BULL, BULL, None, 0),          # weekly veto
            (BEAR, BEAR, BEAR, SHORT, 3),
            (NEUTRAL, BEAR, BEAR, SHORT, 2),
            (BULL, BEAR, BEAR, None, 0),          # weekly veto
            (BULL, BULL, BEAR, None, 0),
            (BULL, BEAR, BULL, None, 0),
            (BULL, BULL, NEUTRAL, None, 0),
            (BULL, NEUTRAL, BULL, None, 0),
            (BEAR, NEUTRAL, BEAR, None, 0),
            (NEUTRAL, NEUTRAL, NEUTRAL, None, 0),
        ]
        for w, d, h, direction, aligned in cases:
            bias = self.combine(w, d, h)
            self.assertEqual((bias.direction, bias.aligned), (direction, aligned), msg=f"{w}/{d}/{h}")
            self.assertTrue(bias.reason)
            self.assertNotIn("—", bias.reason)
            self.assertEqual(bias.to_dict()["direction"], direction)

    def test_reason_wording(self):
        self.assertIn("longs only", self.combine(NEUTRAL, BULL, BULL).reason)
        self.assertIn("3/3", self.combine(BEAR, BEAR, BEAR).reason)
        self.assertIn("weekly is bearish", self.combine(BEAR, BULL, BULL).reason)
        self.assertIn("stand aside", self.combine(BULL, BEAR, BULL).reason)

    def test_shorts_can_be_switched_off(self):
        with mock.patch.object(config, "ALLOW_SHORT", False):
            bias = self.combine(BEAR, BEAR, BEAR)
            self.assertIsNone(bias.direction)
            self.assertEqual(bias.aligned, 0)
            self.assertIn("switched off", bias.reason)
            self.assertEqual(self.combine(BULL, BULL, BULL).direction, LONG)


class FindFvgsTest(unittest.TestCase):
    def hourly(self, rows):
        return intraday_frame(rows)

    BULL_ROWS = [   # (stamp, open, high, low, close)
        ("2026-11-12 09:15", 99.0, 100.0, 98.0, 99.5),
        ("2026-11-12 10:15", 99.5, 103.0, 99.4, 102.8),
        ("2026-11-12 11:15", 102.8, 104.0, 101.0, 103.5),   # low 101 > high 100
        ("2026-11-12 12:15", 103.5, 103.6, 99.2, 102.0),    # wick into and through the gap, close above
    ]

    def test_bullish_gap(self):
        gaps = ict.find_fvgs(self.hourly(self.BULL_ROWS), "H1", 0.001, 60)
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertEqual((g.timeframe, g.kind, g.bottom, g.top), ("H1", BULL, 100.0, 101.0))
        self.assertEqual(g.mid, 100.5)
        self.assertEqual(g.created_time, "2026-11-12T11:15:00+05:30")
        self.assertEqual(g.known_at, "2026-11-12T12:15:00+05:30")

    def test_bullish_gap_invalidated_by_a_later_close_below_its_bottom(self):
        rows = self.BULL_ROWS + [("2026-11-12 13:15", 102.0, 102.1, 99.0, 99.5)]
        self.assertEqual(ict.find_fvgs(self.hourly(rows), "H1", 0.001, 60), [])
        # ...but it was valid as of the bar before
        self.assertEqual(len(ict.find_fvgs(self.hourly(rows[:-1]), "H1", 0.001, 60)), 1)

    def test_bearish_gap_and_its_invalidation(self):
        rows = [
            ("2026-11-12 09:15", 201.0, 202.0, 200.0, 200.5),
            ("2026-11-12 10:15", 200.5, 200.6, 197.0, 197.2),
            ("2026-11-12 11:15", 197.2, 199.0, 196.0, 196.5),   # high 199 < low 200
            ("2026-11-12 12:15", 196.5, 200.8, 196.4, 199.5),   # wick above, close inside
        ]
        gaps = ict.find_fvgs(self.hourly(rows), "H1", 0.001, 60)
        self.assertEqual([(g.kind, g.bottom, g.top) for g in gaps], [(BEAR, 199.0, 200.0)])
        rows.append(("2026-11-12 13:15", 199.5, 200.9, 198.5, 200.4))   # close above the top
        self.assertEqual(ict.find_fvgs(self.hourly(rows), "H1", 0.001, 60), [])

    def test_minimum_size(self):
        frame = self.hourly(self.BULL_ROWS)           # gap is 1.00 / 103.5 = 0.97%
        self.assertEqual(len(ict.find_fvgs(frame, "H1", 0.009, 60)), 1)
        self.assertEqual(ict.find_fvgs(frame, "H1", 0.011, 60), [])

    def test_no_gap_across_a_session_boundary(self):
        rows = [
            ("2026-11-12 14:15", 99.0, 100.0, 98.0, 99.5),
            ("2026-11-12 15:15", 99.5, 99.9, 99.0, 99.8),
            ("2026-11-13 09:15", 102.0, 104.0, 101.0, 103.5),   # overnight gap up, not an FVG
        ]
        self.assertEqual(ict.find_fvgs(self.hourly(rows), "H1", 0.001, 60), [])
        # the same three candles inside one session do form a gap
        same = [("2026-11-12 09:15",) + rows[0][1:], ("2026-11-12 10:15",) + rows[1][1:],
                ("2026-11-12 11:15",) + rows[2][1:]]
        self.assertEqual(len(ict.find_fvgs(self.hourly(same), "H1", 0.001, 60)), 1)

    def test_lookback_and_order(self):
        rows = self.BULL_ROWS + [
            ("2026-11-12 13:15", 102.0, 106.0, 101.9, 105.8),
            ("2026-11-12 14:15", 105.8, 108.0, 104.5, 107.0),   # low 104.5 > high 103.6: second gap
        ]
        frame = self.hourly(rows)
        gaps = ict.find_fvgs(frame, "H1", 0.001, 60)
        self.assertEqual([g.bottom for g in gaps], [100.0, 103.6])          # oldest first
        self.assertEqual([g.bottom for g in ict.find_fvgs(frame, "H1", 0.001, 60, lookback=3)], [103.6])

    def test_five_minute_known_at(self):
        frame = long_frame()
        gaps = ict.find_fvgs(frame, "M5", config.M5_FVG_MIN_PCT, 5)
        last = gaps[-1]
        self.assertEqual((last.bottom, last.top), (611.60, 612.60))
        self.assertEqual(last.known_at, "2026-11-13T10:45:00+05:30")

    def test_short_or_empty_frame(self):
        self.assertEqual(ict.find_fvgs(self.hourly(self.BULL_ROWS[:2]), "H1", 0.0, 60), [])
        self.assertEqual(ict.find_fvgs(empty5(), "H1", 0.0, 60), [])


class LiquidityLevelsTest(unittest.TestCase):
    PREV5 = [
        ("2026-11-12 09:15", 612.0, 618.90, 611.5, 618.0),
        ("2026-11-12 12:00", 612.0, 612.5, 610.20, 611.0),
        ("2026-11-12 15:20", 613.85, 614.9, 613.8, 614.8),
    ]
    DAILY = [   # (date, high, low, close)
        ("2026-11-02", 600, 590, 598), ("2026-11-03", 605, 595, 600), ("2026-11-04", 603, 596, 601),
        ("2026-11-05", 608, 599, 606), ("2026-11-06", 604, 597, 603),
        ("2026-11-09", 610, 601, 609), ("2026-11-10", 612, 604, 611), ("2026-11-11", 615, 607, 614),
        ("2026-11-12", 618.9, 610.2, 614.8),
    ]

    @staticmethod
    def by_name(levels):
        out = {}
        for level in levels:
            out.setdefault(level.name, []).append(level)
        return out

    def test_previous_day(self):
        got = self.by_name(ict.liquidity_levels(intraday_frame(self.PREV5), daily_frame([]), empty5(), empty5()))
        self.assertEqual((got["PDH"][0].price, got["PDH"][0].side), (618.90, BSL))
        self.assertEqual((got["PDL"][0].price, got["PDL"][0].side), (610.20, SSL))
        self.assertEqual(got["PDH"][0].time, "2026-11-12T09:15:00+05:30")
        self.assertEqual(got["PDL"][0].time, "2026-11-12T12:00:00+05:30")
        self.assertEqual(set(got), {"PDH", "PDL"})

    def test_previous_week_on_a_friday(self):
        today = intraday_frame(day_rows(DAY, long_bars()))
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame(self.DAILY), empty5(), today))
        self.assertEqual((got["PWH"][0].price, got["PWH"][0].time), (608.0, "2026-11-05"))
        self.assertEqual((got["PWL"][0].price, got["PWL"][0].time), (590.0, "2026-11-02"))

    def test_previous_week_on_a_monday(self):
        daily = daily_frame(self.DAILY + [("2026-11-13", 616, 609, 612)])
        monday = intraday_frame([("2026-11-16 09:15", 612, 613, 611, 612.5)])
        got = self.by_name(ict.liquidity_levels(empty5(), daily, empty5(), monday))
        self.assertEqual((got["PWH"][0].price, got["PWH"][0].time), (618.9, "2026-11-12"))
        self.assertEqual((got["PWL"][0].price, got["PWL"][0].time), (601.0, "2026-11-09"))

    def test_previous_week_when_today_has_no_bars_yet(self):
        # today = the day after the last daily row = Friday 13 Nov, so last week is 2-6 Nov
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame(self.DAILY), empty5(), empty5()))
        self.assertEqual(got["PWH"][0].price, 608.0)
        self.assertEqual(got["PWL"][0].price, 590.0)

    def test_no_previous_week_without_an_earlier_week(self):
        today = intraday_frame(day_rows(DAY, long_bars()))
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame(self.DAILY[5:]), empty5(), today))
        self.assertNotIn("PWH", got)
        self.assertNotIn("PWL", got)

    def test_daily_swings_keep_only_the_unbroken_ones(self):
        highs = [100, 102, 110, 104, 103, 105, 108, 106, 104]
        lows = [95, 97, 101, 99, 96, 98, 100, 99, 97]
        closes = [98, 100, 108, 101, 99, 103, 107, 105, 109]     # 109 closes above the 108 swing high
        rows = [(f"2026-10-{i + 1:02d}", h, l, c) for i, (h, l, c) in enumerate(zip(highs, lows, closes))]
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame(rows), empty5(), empty5()))
        self.assertEqual([(x.price, x.side, x.time) for x in got["D_SH"]], [(110.0, BSL, "2026-10-03")])
        self.assertEqual([(x.price, x.side, x.time) for x in got["D_SL"]], [(96.0, SSL, "2026-10-05")])
        # had the last day closed at 107 instead, the 108 high would still stand
        rows[-1] = rows[-1][:3] + (107,)
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame(rows), empty5(), empty5()))
        self.assertEqual([x.price for x in got["D_SH"]], [110.0, 108.0])
        # a swing needs two later bars: one row fewer and the 108 is not confirmed yet
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame(rows[:-1]), empty5(), empty5()))
        self.assertEqual([x.price for x in got["D_SH"]], [110.0])

    def test_daily_swing_lookback(self):
        highs = [100, 102, 110, 104, 103, 105, 108, 106, 104]
        rows = [(f"2026-10-{i + 1:02d}", h, h - 5, h - 2) for i, h in enumerate(highs)]
        with mock.patch.object(config, "LEVEL_LOOKBACK_DAILY_BARS", 5):
            got = self.by_name(ict.liquidity_levels(empty5(), daily_frame(rows), empty5(), empty5()))
        self.assertEqual([x.price for x in got.get("D_SH", [])], [108.0])     # the 110 is out of range

    def test_hourly_swings(self):
        stamps = [f"2026-11-11 {h}:15" for h in ("09", "10", "11", "12", "13", "14")] + \
                 [f"2026-11-12 {h}:15" for h in ("09", "10", "11")]
        highs = [100, 102, 110, 104, 103, 105, 108, 106, 104]
        lows = [95, 97, 101, 99, 96, 98, 100, 99, 97]
        closes = [98, 100, 108, 101, 99, 103, 107, 105, 109]
        h1 = intraday_frame([(s, c, h, l, c) for s, h, l, c in zip(stamps, highs, lows, closes)])
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame([]), h1, empty5()))
        self.assertEqual([(x.price, x.time) for x in got["H1_SH"]], [(110.0, "2026-11-11T11:15:00+05:30")])
        self.assertEqual([(x.price, x.time) for x in got["H1_SL"]], [(96.0, "2026-11-11T13:15:00+05:30")])

    def test_opening_range_only_after_the_first_hour(self):
        bars = long_bars()
        keys = list(bars)
        upto_1010 = intraday_frame(day_rows(DAY, OrderedDict((k, bars[k]) for k in keys[:12])))
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame([]), empty5(), upto_1010))
        self.assertNotIn("ORH", got)
        self.assertNotIn("ORL", got)
        upto_1015 = intraday_frame(day_rows(DAY, OrderedDict((k, bars[k]) for k in keys[:13])))
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame([]), empty5(), upto_1015))
        self.assertEqual((got["ORH"][0].price, got["ORH"][0].time), (615.40, "2026-11-13T09:20:00+05:30"))
        self.assertEqual((got["ORL"][0].price, got["ORL"][0].time), (610.60, "2026-11-13T09:50:00+05:30"))

    def test_opening_range_ignores_bars_after_the_first_hour(self):
        bars = long_bars()
        bars["10:45"] = (613.9, 620.0, 600.0, 610.0)
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame([]), empty5(), intraday_frame(day_rows(DAY, bars))))
        self.assertEqual(got["ORH"][0].price, 615.40)
        self.assertEqual(got["ORL"][0].price, 610.60)

    def test_day_extremes_exclude_the_current_bar(self):
        bars = long_bars()
        bars["10:45"] = (613.9, 620.0, 600.0, 610.0)       # the current bar makes new extremes
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame([]), empty5(), intraday_frame(day_rows(DAY, bars))))
        self.assertEqual((got["HOD"][0].price, got["HOD"][0].side), (615.40, BSL))
        self.assertEqual((got["LOD"][0].price, got["LOD"][0].side), (609.95, SSL))
        one_bar = intraday_frame([("2026-11-13 09:15", 614, 615, 613.6, 614.8)])
        got = self.by_name(ict.liquidity_levels(empty5(), daily_frame([]), empty5(), one_bar))
        self.assertNotIn("HOD", got)

    def test_everything_together_and_no_duplicates(self):
        today = intraday_frame(day_rows(DAY, long_bars()))
        levels = ict.liquidity_levels(intraday_frame(self.PREV5), daily_frame(self.DAILY), empty5(), today)
        keys = [(x.name, x.price) for x in levels]
        self.assertEqual(len(keys), len(set(keys)))
        got = self.by_name(levels)
        for name in ("PDH", "PDL", "PWH", "PWL", "ORH", "ORL", "HOD", "LOD"):
            self.assertIn(name, got)
        for level in levels:
            self.assertEqual(level.side, BSL if level.name in HIGH_NAMES else SSL)

    def test_all_empty(self):
        self.assertEqual(ict.liquidity_levels(empty5(), daily_frame([]), empty5(), empty5()), [])


class WorkedLongExampleTest(unittest.TestCase):
    """Section 9 of the guide, candle for candle."""

    def check(self, sig):
        self.assertIsNotNone(sig)
        self.assertEqual((sig.symbol, sig.direction), ("ABC", LONG))
        self.assertEqual(sig.contexts, ["SWEEP:PDL"])
        self.assertEqual(sig.sweep_level.name, "PDL")
        self.assertEqual(sig.sweep_level.price, 610.20)
        self.assertIsNone(sig.h1_fvg)
        self.assertEqual(sig.signal_time, "2026-11-13T10:40:00+05:30")
        self.assertEqual(sig.context_time, "2026-11-13T10:25:00+05:30")
        self.assertEqual(sig.swing_extreme, 609.95)
        self.assertEqual(sig.mss_level, 612.20)
        self.assertEqual((sig.fvg.timeframe, sig.fvg.kind, sig.fvg.bottom, sig.fvg.top), ("M5", BULL, 611.60, 612.60))
        self.assertEqual(sig.fvg.created_time, "2026-11-13T10:40:00+05:30")
        self.assertEqual(sig.fvg.known_at, "2026-11-13T10:45:00+05:30")
        # order prices are whole paise, exactly as the guide prints them
        self.assertEqual(sig.entry, 612.10)
        self.assertEqual(sig.structural_stop, 609.65)              # 609.95 less 0.05% = 609.645 -> 609.65
        self.assertEqual(sig.stop, 609.65)                         # no widening needed
        self.assertEqual(sig.target, 618.90)
        self.assertEqual(sig.target_kind, "PDH")
        self.assertEqual(sig.risk_per_share, 2.45)
        self.assertAlmostEqual(sig.rr, 6.80 / 2.45, places=9)
        self.assertEqual(round(sig.rr, 2), 2.78)

    def test_with_explicit_levels(self):
        self.check(detect_long())

    def test_with_levels_from_liquidity_levels(self):
        frame = long_frame()
        today = frame[frame.index.date == DAY]
        levels = ict.liquidity_levels(intraday_frame(LiquidityLevelsTest.PREV5), daily_frame([]), empty5(), today)
        sig = ict.detect_setup("ABC", frame, DAY, LONG, levels, [])
        self.check(sig)
        self.assertEqual(sig.sweep_level.time, "2026-11-12T12:00:00+05:30")

    def test_story(self):
        story = detect_long().story
        self.assertTrue(5 <= len(story) <= 8, story)
        text = " ".join(story)
        self.assertEqual(
            story[0],
            "10:25 price dipped to 609.95, below the previous day's low 610.20, and closed back above it.",
        )
        self.assertEqual(story[1], "10:35 a strong up candle (body 3.3x the recent average) closed above the 612.20 swing high.")
        self.assertEqual(story[2], "10:40 left a fair value gap between 611.60 and 612.60.")
        self.assertEqual(
            story[3],
            "Buy limit at the gap midpoint 612.10, stop 609.65 (0.05% under the 609.95 low), risk 2.45 per share.",
        )
        self.assertEqual(story[4], "Target 618.90, the previous day's high, 2.78 times the risk.")
        self.assertNotIn("—", text)
        self.assertNotIn("–", text)

    def test_to_dict_is_json_ready(self):
        import json
        d = detect_long().to_dict()
        json.dumps(d)
        self.assertAlmostEqual(d["fvg"]["mid"], d["entry"], places=6)
        self.assertEqual(d["sweep_level"]["name"], "PDL")
        self.assertIsNone(d["h1_fvg"])

    def test_both_contexts_sweep_listed_first(self):
        gap = FVG("H1", BULL, top=611.00, bottom=609.50,
                  created_time="2026-11-12T13:15:00+05:30", known_at="2026-11-12T14:15:00+05:30")
        sig = detect_long(fvgs=[gap])
        self.assertEqual(sig.contexts, ["SWEEP:PDL", "H1_FVG"])
        self.assertIs(sig.h1_fvg, gap)
        self.assertEqual(sig.context_time, "2026-11-13T09:45:00+05:30")    # first dip into the 1h gap
        self.assertEqual(sig.swing_extreme, 609.95)
        self.assertEqual(round(sig.stop, 2), 609.65)
        self.assertTrue(sig.story[0].startswith("09:45 price dipped into the 1-hour fair value gap (609.50 to 611.00)"))
        self.assertTrue(5 <= len(sig.story) <= 8)

    def test_gaps_of_the_wrong_kind_are_ignored(self):
        bear = FVG("H1", BEAR, top=611.00, bottom=609.50,
                   created_time="2026-11-12T13:15:00+05:30", known_at="2026-11-12T14:15:00+05:30")
        self.assertEqual(detect_long(fvgs=[bear]).contexts, ["SWEEP:PDL"])

    def test_edge_entry(self):
        with mock.patch.object(config, "ENTRY_AT", "edge"):
            sig = detect_long()
        self.assertEqual(sig.entry, 612.60)
        self.assertIn("top of the gap", " ".join(sig.story))

    def test_direction_must_match(self):
        self.assertIsNone(ict.detect_setup("ABC", long_frame(), DAY, SHORT, long_levels(), []))
        self.assertIsNone(ict.detect_setup("ABC", long_frame(), DAY, "SIDEWAYS", long_levels(), []))


class WorkedShortExampleTest(unittest.TestCase):
    """Section 10 of the guide: tap of a bearish 1-hour gap."""

    def detect(self, gap=None, levels=None):
        return ict.detect_setup("XYZ", short_frame(), DAY, SHORT,
                                short_levels() if levels is None else levels,
                                [short_h1_gap() if gap is None else gap])

    def test_prices(self):
        sig = self.detect()
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, SHORT)
        self.assertEqual(sig.contexts, ["H1_FVG"])
        self.assertIsNone(sig.sweep_level)
        self.assertEqual((sig.h1_fvg.bottom, sig.h1_fvg.top), (1486.00, 1489.50))
        self.assertEqual(sig.signal_time, "2026-11-13T11:35:00+05:30")
        self.assertEqual(sig.context_time, "2026-11-13T11:20:00+05:30")
        self.assertEqual(sig.swing_extreme, 1487.20)
        self.assertEqual(sig.mss_level, 1482.50)
        self.assertEqual((sig.fvg.kind, sig.fvg.bottom, sig.fvg.top), (BEAR, 1482.10, 1483.50))
        self.assertEqual(sig.entry, 1482.80)
        self.assertEqual(sig.structural_stop, 1487.94)             # 1,487.20 plus 0.05% = 1,487.9436
        self.assertEqual(sig.stop, 1487.94)
        self.assertEqual(sig.target, 1468.40)
        self.assertEqual(sig.target_kind, "PDL")
        self.assertEqual(sig.risk_per_share, 5.14)
        self.assertAlmostEqual(sig.rr, 14.40 / 5.14, places=9)
        self.assertEqual(round(sig.rr, 2), 2.80)

    def test_story(self):
        story = self.detect().story
        self.assertTrue(5 <= len(story) <= 8, story)
        self.assertEqual(
            story[0],
            "11:20 price rallied into the 1-hour fair value gap (1,486.00 to 1,489.50) without closing above it.",
        )
        self.assertTrue(story[1].startswith("11:30 a strong down candle (body "))
        self.assertTrue(story[1].endswith("closed below the 1,482.50 swing low."))
        self.assertEqual(story[2], "11:35 left a fair value gap between 1,482.10 and 1,483.50.")
        self.assertEqual(
            story[3],
            "Sell limit at the gap midpoint 1,482.80, stop 1,487.94 (0.05% above the 1,487.20 high), risk 5.14 per share.",
        )
        self.assertEqual(story[4], "Target 1,468.40, the previous day's low, 2.80 times the risk.")

    def test_gap_known_only_after_the_tap_is_not_a_tap(self):
        late = short_h1_gap(known_at="2026-11-13T11:30:00+05:30")
        self.assertIsNone(self.detect(gap=late))
        # known from 11:20 on, the 11:20 bar counts
        on_time = short_h1_gap(known_at="2026-11-13T11:20:00+05:30")
        self.assertEqual(self.detect(gap=on_time).context_time, "2026-11-13T11:20:00+05:30")
        # known from 11:25 on, the 11:25 bar (high 1,486.10) is the tap instead
        later = self.detect(gap=short_h1_gap(known_at="2026-11-13T11:25:00+05:30"))
        self.assertEqual(later.context_time, "2026-11-13T11:25:00+05:30")
        self.assertEqual(later.swing_extreme, 1486.10)

    def test_close_through_the_gap_cancels_the_tap(self):
        # a gap whose top is below the 11:20 close (1,485.90 > 1,485.50) was closed through
        thin = FVG("H1", BEAR, top=1485.50, bottom=1485.00,
                   created_time="2026-11-12T13:15:00+05:30", known_at="2026-11-12T14:15:00+05:30")
        sig = self.detect(gap=thin)
        # the only clean tap left is 11:25 (high 1,486.10 >= 1,485.00, closes 1,483.90 and lower)
        self.assertEqual(sig.context_time, "2026-11-13T11:25:00+05:30")

    def test_level_traded_through_before_the_window_is_not_a_sweep(self):
        # ORH 1,480.30 was first exceeded at 10:15, long before the last 12 candles
        sig = self.detect()
        self.assertEqual(sig.contexts, ["H1_FVG"])
        self.assertIsNone(ict.detect_setup("XYZ", short_frame(), DAY, SHORT, short_levels(), []))


class RejectionTest(unittest.TestCase):
    def test_baseline_is_a_signal(self):
        self.assertIsNotNone(detect_long())

    def test_no_context(self):
        # the 10:25 candle no longer trades below anything
        self.assertIsNone(detect_long(**{"10_25": (610.80, 611.00, 610.70, 610.90)}))

    def test_context_too_old(self):
        with mock.patch.object(config, "CONTEXT_LOOKBACK_BARS", 2):
            self.assertIsNone(detect_long())
        with mock.patch.object(config, "CONTEXT_LOOKBACK_BARS", 3):
            self.assertIsNotNone(detect_long())

    def test_no_displacement_small_body(self):
        self.assertIsNone(detect_long(**{"10_35": (613.20, 613.40, 611.40, 613.30)}))

    def test_no_displacement_down_candle(self):
        self.assertIsNone(detect_long(**{"10_35": (613.35, 613.40, 611.40, 613.30)}))

    def test_displacement_threshold(self):
        with mock.patch.object(config, "DISPLACEMENT_BODY_MULT", 3.2):
            self.assertIsNotNone(detect_long())           # 1.80 / 0.55 = 3.27
        with mock.patch.object(config, "DISPLACEMENT_BODY_MULT", 3.3):
            self.assertIsNone(detect_long())

    def test_displacement_needs_five_earlier_bars(self):
        bars = long_bars()
        keys = list(bars)
        frame = intraday_frame(day_rows(DAY, OrderedDict((k, bars[k]) for k in keys[-6:])))   # 10:15 .. 10:40
        self.assertIsNone(ict.detect_setup("ABC", frame, DAY, LONG, long_levels(), []))
        frame = intraday_frame(day_rows(DAY, OrderedDict((k, bars[k]) for k in keys[-7:])))   # one more bar
        self.assertIsNotNone(ict.detect_setup("ABC", frame, DAY, LONG, long_levels(), []))

    def test_no_structure_shift(self):
        # the swing high before the low is now 614.50, above every later close
        self.assertIsNone(detect_long(**{"10_05": (611.90, 614.50, 611.50, 611.70)}))

    def test_structure_shift_fallback_when_no_swing_high_today(self):
        # only 10:15 .. 10:40 plus yesterday's tail: no 5m swing high today before the low,
        # so the reference is the highest high of the 5 bars before the 10:25 low (614.90, yesterday)
        bars = long_bars()
        keys = list(bars)
        tail = OrderedDict((k, bars[k]) for k in keys[-6:])
        frame = intraday_frame(day_rows(PREV, PREV_TAIL) + day_rows(DAY, tail))
        self.assertIsNone(ict.detect_setup("ABC", frame, DAY, LONG, long_levels(), []))
        with mock.patch.object(config, "MSS_FALLBACK_BARS", 2):          # 10:15 and 10:20 only: 611.60
            sig = ict.detect_setup("ABC", frame, DAY, LONG, long_levels(), [])
        self.assertIsNotNone(sig)
        self.assertEqual(sig.mss_level, 611.60)
        self.assertIn("the highest price of the 2 candles before the low", " ".join(sig.story))

    def test_gap_too_small(self):
        self.assertIsNone(detect_long(**{"10_40": (613.30, 614.10, 611.80, 613.90)}))

    def test_no_gap_at_all(self):
        self.assertIsNone(detect_long(**{"10_40": (613.30, 614.10, 611.60, 613.90)}))

    def test_stop_too_wide(self):
        self.assertIsNone(detect_long(**{"10_25": (610.80, 611.00, 603.00, 610.90)}))

    def test_bars_not_all_today(self):
        rows = day_rows(PREV, OrderedDict([
            ("14:40", (609.0, 609.3, 608.8, 609.1)), ("14:45", (609.1, 609.4, 608.9, 609.2)),
            ("14:50", (609.2, 609.5, 609.0, 609.3)), ("14:55", (609.3, 609.6, 609.1, 609.4)),
            ("15:00", (609.4, 609.7, 609.2, 609.5)), ("15:05", (609.5, 609.8, 609.3, 609.6)),
            ("15:10", (609.6, 609.9, 608.0, 609.7)), ("15:15", (609.7, 610.0, 609.5, 609.9)),
        ])) + day_rows(DAY, OrderedDict([
            ("09:15", (610.5, 613.2, 610.4, 613.0)),
            ("09:20", (613.0, 614.0, 612.0, 613.8)),      # low 612.0 > yesterday's 610.0 high
        ]))
        levels = [lv("PDL", 608.50), lv("PDH", 640.0)]
        self.assertIsNone(ict.detect_setup("ABC", intraday_frame(rows), DAY, LONG, levels, []))
        self.assertIsNone(ict.detect_setup("ABC", long_frame(), PREV, LONG, long_levels(), []))

    def test_min_stop_widening(self):
        sig = detect_long(
            levels=[lv("ORL", 610.90), lv("ORH", 615.40), lv("PDH", 618.90)],
            **{"09_50": (611.10, 611.30, 610.90, 611.00), "10_25": (610.80, 611.00, 610.65, 610.90)},
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.contexts, ["SWEEP:ORL"])
        self.assertEqual(sig.context_time, "2026-11-13T10:20:00+05:30")     # first trade below 610.90
        self.assertEqual(sig.swing_extreme, 610.65)
        self.assertEqual(sig.structural_stop, 610.34)       # 610.65 less 0.05% = 610.3447
        self.assertEqual(sig.stop, 610.26)                  # 612.10 less 0.30% = 610.2637, rounded away from entry
        self.assertEqual(sig.risk_per_share, 1.84)
        self.assertGreaterEqual(sig.risk_per_share / sig.entry, config.MIN_STOP_PCT)
        self.assertLess(sig.risk_per_share / sig.entry, config.MIN_STOP_PCT + 0.01 / sig.entry + 1e-12)
        self.assertEqual(sig.target_kind, "PDH")
        self.assertIn("stop 610.26 (set 0.30% under the entry because the 610.65 low was closer than that), "
                      "risk 1.84 per share.", " ".join(sig.story))

    def test_rr_cap(self):
        sig = detect_long(levels=[lv("PDL", 610.20), lv("ORH", 615.40), lv("PDH", 630.00)])
        self.assertEqual(sig.target_kind, "CAP_4R")
        self.assertEqual(sig.target, 621.90)                # 612.10 + 4 x 2.45
        self.assertAlmostEqual(sig.rr, 4.0, places=9)
        self.assertIn("capped at 4 times the risk", sig.story[-1])
        self.assertIn("630.00", sig.story[-1])

    def test_fallback_target(self):
        sig = detect_long(levels=[lv("PDL", 610.20), lv("ORH", 615.40)])       # ORH is only 1.3R away
        self.assertEqual(sig.target_kind, "FIXED_2R")
        self.assertEqual(sig.target, 617.00)                # 612.10 + 2 x 2.45
        self.assertAlmostEqual(sig.rr, 2.0, places=9)
        self.assertTrue(5 <= len(sig.story) <= 8)

    def test_nearest_qualifying_target_wins(self):
        levels = [lv("PDL", 610.20), lv("ORH", 615.40), lv("D_SH", 625.00), lv("H1_SH", 619.50), lv("PDH", 640.00)]
        sig = detect_long(levels=levels)
        self.assertEqual((sig.target, sig.target_kind), (619.50, "H1_SH"))
        self.assertIn("a 1-hour swing high", sig.story[-1])

    def test_only_target_names_are_targets(self):
        # an M5_SH or an SSL-side name above the entry is never a target
        sig = detect_long(levels=[lv("PDL", 610.20), lv("M5_SH", 618.00), lv("LOD", 619.00)])
        self.assertEqual(sig.target_kind, "FIXED_2R")

    def test_level_already_traded_through_today_is_not_a_sweep(self):
        # 09:45 now ties the 09:50 low, so today has no 5m swing low to sweep either
        tie = {"09_45": (611.70, 611.90, 610.60, 611.10)}
        frame = long_frame(**tie)
        today = frame[frame.index.date == DAY]
        self.assertEqual([p for _, p in ict.find_swings(today, 2)["lows"] if p > 609.95], [])
        # 613.00 was first traded below at 09:30, before the 12-candle window
        self.assertIsNone(detect_long(levels=[lv("PDL", 613.00), lv("PDH", 618.90)], **tie))
        self.assertIsNotNone(detect_long(levels=[lv("PDL", 610.20), lv("PDH", 618.90)], **tie))

    def test_level_must_be_reclaimed(self):
        # price never closes back above 614.20
        self.assertIsNone(detect_long(levels=[lv("PDL", 614.20), lv("PDH", 618.90)],
                                      **{"09_45": (611.70, 611.90, 610.60, 611.10)}))

    def test_sweep_of_todays_five_minute_swing_low(self):
        sig = detect_long(levels=[lv("PDH", 618.90)])
        self.assertEqual(sig.contexts, ["SWEEP:M5_SL"])
        self.assertEqual((sig.sweep_level.name, sig.sweep_level.price, sig.sweep_level.side), ("M5_SL", 610.60, SSL))
        self.assertEqual(sig.sweep_level.time, "2026-11-13T09:50:00+05:30")
        self.assertIn("below an earlier 5-minute swing low today at 610.60", sig.story[0])

    def test_a_five_minute_swing_low_is_breached_only_after_it_formed(self):
        # A wick to 610.40 at 09:20 comes before the 09:50 swing low (610.60) existed, and
        # before the 12-candle window. It is not a breach of that swing: the first breach
        # after the swing formed is the 10:25 candle, so the sweep still counts.
        sig = detect_long(levels=[lv("PDH", 618.90)], **{"09_20": (614.80, 615.40, 610.40, 614.50)})
        self.assertIsNotNone(sig)
        self.assertEqual(sig.contexts, ["SWEEP:M5_SL"])
        self.assertEqual((sig.sweep_level.price, sig.context_time), (610.60, "2026-11-13T10:25:00+05:30"))

    def test_lowest_swept_level_is_reported(self):
        sig = detect_long(levels=[lv("ORL", 610.60), lv("H1_SL", 610.40), lv("PDL", 610.20), lv("PDH", 618.90)])
        self.assertEqual(sig.contexts, ["SWEEP:PDL"])

    def test_level_time_today_starts_the_breach_search_after_it(self):
        # an hourly swing low stamped 10:15 today at 610.75: the 10:20 bar (low 610.70) is the first breach
        levels = [lv("H1_SL", 610.75, "2026-11-13T10:15:00+05:30"), lv("PDH", 618.90)]
        sig = detect_long(levels=levels, **{"09_45": (611.70, 611.90, 610.60, 611.10)})
        self.assertEqual(sig.contexts, ["SWEEP:H1_SL"])
        self.assertEqual(sig.context_time, "2026-11-13T10:20:00+05:30")

    def test_too_few_rows(self):
        frame = long_frame()
        self.assertIsNone(ict.detect_setup("ABC", frame.iloc[:2], DAY, LONG, long_levels(), []))
        self.assertIsNone(ict.detect_setup("ABC", empty5(), DAY, LONG, long_levels(), []))


class RoundedPricesTest(unittest.TestCase):
    """Order prices are whole paise and the story is computed from exactly those prices."""

    FACTOR = 0.563217        # turns the tidy example into awkward prices near 344.7

    def scaled(self, flip=False):
        frame = long_frame()
        f = self.FACTOR
        if not flip:
            out = frame.copy()
            for col in ("Open", "High", "Low", "Close"):
                out[col] = frame[col] * f
            levels = [lv(x.name, x.price * f) for x in long_levels()]
            return out, levels, LONG
        k = 2 * 612.10
        out = pd.DataFrame(
            {"Open": (k - frame["Open"]) * f, "High": (k - frame["Low"]) * f, "Low": (k - frame["High"]) * f,
             "Close": (k - frame["Close"]) * f, "Volume": frame["Volume"]},
            index=frame.index,
        )
        levels = [lv(ict._MIRROR_NAME[x.name], (k - x.price) * f) for x in long_levels()]
        return out, levels, SHORT

    def check(self, flip):
        frame, levels, direction = self.scaled(flip)
        sig = ict.detect_setup("ABC", frame, DAY, direction, levels, [])
        self.assertIsNotNone(sig)
        for value in (sig.entry, sig.stop, sig.target, sig.structural_stop, sig.risk_per_share):
            self.assertEqual(value, round(value, 2))
        self.assertEqual(sig.risk_per_share, round(abs(sig.entry - sig.stop), 2))
        self.assertAlmostEqual(sig.rr, abs(sig.target - sig.entry) / sig.risk_per_share, places=12)
        self.assertGreaterEqual(sig.rr, config.MIN_RR - 1e-9)
        text = " ".join(sig.story)
        self.assertIn(f"risk {abs(sig.entry - sig.stop):.2f} per share", text)
        self.assertIn(f"{abs(sig.target - sig.entry) / abs(sig.entry - sig.stop):.2f} times the risk", text)
        self.assertIn(f"stop {sig.stop:,.2f}", text)
        self.assertIn(f"Target {sig.target:,.2f}", text)
        return sig

    def test_long(self):
        sig = self.check(flip=False)
        self.assertEqual(sig.entry, round(612.10 * self.FACTOR, 2))
        self.assertLess(sig.stop, sig.entry)

    def test_short(self):
        sig = self.check(flip=True)
        self.assertGreater(sig.stop, sig.entry)

    def test_widened_stop_is_never_closer_than_the_minimum(self):
        widened = 0
        for factor in (0.563217, 0.81377, 1.0, 1.31313, 2.7183, 7.77):
            frame = long_frame(**{"09_50": (611.10, 611.30, 610.90, 611.00), "10_25": (610.80, 611.00, 610.65, 610.90)})
            for col in ("Open", "High", "Low", "Close"):
                frame[col] = frame[col] * factor
            top = 2 * 612.10 * factor
            mirror = pd.DataFrame(
                {"Open": top - frame["Open"], "High": top - frame["Low"], "Low": top - frame["High"],
                 "Close": top - frame["Close"], "Volume": frame["Volume"]},
                index=frame.index,
            )
            cases = (
                (LONG, frame, [lv("ORL", 610.90 * factor), lv("PDH", 618.90 * factor)]),
                (SHORT, mirror, [lv("ORH", top - 610.90 * factor), lv("PDL", top - 618.90 * factor)]),
            )
            for direction, fr, lvls in cases:
                sig = ict.detect_setup("ABC", fr, DAY, direction, lvls, [])
                self.assertIsNotNone(sig, msg=f"{direction} x{factor}")
                if sig.stop != sig.structural_stop:
                    widened += 1
                self.assertGreaterEqual(sig.risk_per_share / sig.entry, config.MIN_STOP_PCT - 1e-12,
                                        msg=f"{direction} x{factor}")
        self.assertGreaterEqual(widened, 8)


class MirrorSymmetryTest(unittest.TestCase):
    K = 1224.20      # reflect around 612.10 so the mirrored prices stay near the same level

    def mirrored(self):
        frame = long_frame()
        flipped = pd.DataFrame(
            {
                "Open": self.K - frame["Open"],
                "High": self.K - frame["Low"],
                "Low": self.K - frame["High"],
                "Close": self.K - frame["Close"],
                "Volume": frame["Volume"],
            },
            index=frame.index,
        )
        names = {"PDL": "PDH", "ORL": "ORH", "LOD": "HOD", "ORH": "ORL", "HOD": "LOD", "PDH": "PDL"}
        levels = [lv(names[x.name], self.K - x.price) for x in long_levels()]
        return flipped, levels

    def test_short_is_the_mirror_of_long(self):
        long_sig = detect_long()
        frame, levels = self.mirrored()
        short_sig = ict.detect_setup("ABC", frame, DAY, SHORT, levels, [])
        self.assertIsNotNone(short_sig)
        self.assertEqual(short_sig.direction, SHORT)
        self.assertEqual(short_sig.contexts, ["SWEEP:PDH"])
        self.assertEqual(short_sig.sweep_level.side, BSL)
        self.assertEqual(short_sig.signal_time, long_sig.signal_time)
        self.assertEqual(short_sig.context_time, long_sig.context_time)
        self.assertEqual(short_sig.fvg.kind, BEAR)
        self.assertAlmostEqual(short_sig.fvg.top, self.K - long_sig.fvg.bottom, places=9)
        self.assertAlmostEqual(short_sig.fvg.bottom, self.K - long_sig.fvg.top, places=9)
        self.assertAlmostEqual(short_sig.entry, self.K - long_sig.entry, places=9)
        self.assertAlmostEqual(short_sig.swing_extreme, self.K - long_sig.swing_extreme, places=9)
        self.assertAlmostEqual(short_sig.mss_level, self.K - long_sig.mss_level, places=9)
        self.assertAlmostEqual(short_sig.target, self.K - long_sig.target, places=9)
        self.assertEqual(short_sig.target_kind, "PDL")
        # percentage rules mirror as percentages of the mirrored price: 614.25 plus 0.05% = 614.557
        self.assertEqual(short_sig.stop, round(short_sig.swing_extreme * (1 + config.SL_BUFFER_PCT), 2))
        self.assertEqual(short_sig.stop, 614.56)
        self.assertGreater(short_sig.stop, short_sig.entry)
        self.assertLess(short_sig.target, short_sig.entry)
        self.assertAlmostEqual(short_sig.risk_per_share, short_sig.stop - short_sig.entry, places=12)
        self.assertAlmostEqual(short_sig.rr, long_sig.rr, delta=0.02)
        self.assertEqual(len(short_sig.story), len(long_sig.story))
        self.assertIn("pushed up", short_sig.story[0])
        self.assertIn("Sell limit", short_sig.story[-2])

    def test_mirrored_rejections(self):
        frame, levels = self.mirrored()
        self.assertIsNone(ict.detect_setup("ABC", frame, DAY, LONG, levels, []))
        no_sweep = [x for x in levels if x.name not in ("PDH", "ORH")]
        sig = ict.detect_setup("ABC", frame, DAY, SHORT, no_sweep, [])
        self.assertEqual(sig.contexts, ["SWEEP:M5_SH"])          # today's 5m swing high, mirrored from M5_SL
        self.assertEqual(sig.sweep_level.side, BSL)


class TruncationInvarianceTest(unittest.TestCase):
    """The answer at bar k must not depend on anything after bar k."""

    def rows(self):
        extra = OrderedDict([
            ("10:45", (613.90, 614.20, 612.00, 612.30)), ("10:50", (612.30, 612.90, 611.90, 612.70)),
            ("10:55", (612.70, 614.80, 612.60, 614.60)), ("11:00", (614.60, 615.90, 613.70, 615.60)),
            ("11:05", (615.60, 616.40, 615.10, 616.00)), ("11:10", (616.00, 617.50, 615.80, 617.20)),
            ("11:15", (617.20, 619.10, 617.00, 618.80)), ("11:20", (618.80, 619.00, 616.00, 616.20)),
            ("11:25", (616.20, 616.50, 613.00, 613.20)), ("11:30", (613.20, 613.40, 611.00, 611.30)),
        ])
        bars = long_bars()
        bars.update(extra)
        return day_rows(PREV, PREV_TAIL) + day_rows(DAY, bars)

    def test_detect_setup(self):
        rows = self.rows()
        full = intraday_frame(rows)
        levels = long_levels()
        gap = FVG("H1", BULL, top=611.00, bottom=609.50,
                  created_time="2026-11-12T13:15:00+05:30", known_at="2026-11-12T14:15:00+05:30")
        found = 0
        for direction in (LONG, SHORT):
            for k in range(1, len(rows) + 1):
                fresh = ict.detect_setup("ABC", intraday_frame(rows[:k]), DAY, direction, levels, [gap])
                ict.detect_setup("ABC", full, DAY, direction, levels, [gap])     # must leave no trace
                sliced = ict.detect_setup("ABC", full.iloc[:k], DAY, direction, levels, [gap])
                again = ict.detect_setup("ABC", full.iloc[:k], DAY, direction, levels, [gap])
                a = fresh.to_dict() if fresh else None
                self.assertEqual(a, sliced.to_dict() if sliced else None, msg=f"{direction} k={k}")
                self.assertEqual(a, again.to_dict() if again else None, msg=f"{direction} k={k}")
                found += fresh is not None
        self.assertGreaterEqual(found, 1)
        # the worked example fires exactly at the 10:40 bar (row 4 + 18)
        self.assertIsNotNone(ict.detect_setup("ABC", full.iloc[:22], DAY, LONG, levels, []))

    def test_window_start_does_not_matter_once_today_is_complete(self):
        # dropping yesterday's bars one by one changes nothing while 20 earlier bars remain
        rows = self.rows()[:22]
        base = ict.detect_setup("ABC", intraday_frame(rows), DAY, LONG, long_levels(), []).to_dict()
        many_prev = day_rows(PREV, OrderedDict(
            (f"{13 + i // 12:02d}:{(i % 12) * 5:02d}", (613.0, 613.2, 612.8, 613.1)) for i in range(20)
        ))
        longer = ict.detect_setup("ABC", intraday_frame(many_prev + rows), DAY, LONG, long_levels(), []).to_dict()
        self.assertEqual(base, longer)

    def test_structure_bias_and_fvgs(self):
        rows = StructureBiasTest.ROWS
        full = daily_frame(rows)
        for k in range(1, len(rows) + 1):
            self.assertEqual(ict.structure_bias(daily_frame(rows[:k]), "D").to_dict(),
                             ict.structure_bias(full.iloc[:k], "D").to_dict(), msg=f"k={k}")
        h1 = FindFvgsTest.BULL_ROWS + [("2026-11-12 13:15", 102.0, 102.1, 99.0, 99.5)]
        full = intraday_frame(h1)
        for k in range(1, len(h1) + 1):
            self.assertEqual([g.to_dict() for g in ict.find_fvgs(intraday_frame(h1[:k]), "H1", 0.001, 60)],
                             [g.to_dict() for g in ict.find_fvgs(full.iloc[:k], "H1", 0.001, 60)], msg=f"k={k}")

    def test_inputs_are_not_modified(self):
        frame = long_frame()
        before = frame.copy()
        levels = long_levels()
        snapshot = [x.to_dict() for x in levels]
        ict.detect_setup("ABC", frame, DAY, LONG, levels, [])
        ict.detect_setup("ABC", frame, DAY, SHORT, levels, [])
        pd.testing.assert_frame_equal(frame, before)
        self.assertEqual([x.to_dict() for x in levels], snapshot)


class TimingTest(unittest.TestCase):
    def random_sessions(self, days=("2026-11-11", "2026-11-12", "2026-11-13"), seed=7):
        rng = np.random.RandomState(seed)
        rows = []
        price = 600.0
        for d in days:
            stamp = pd.Timestamp(f"{d} 09:15")
            for _ in range(75):
                o = price
                c = o + rng.normal(0, 0.9)
                h = max(o, c) + abs(rng.normal(0, 0.35))
                l = min(o, c) - abs(rng.normal(0, 0.35))
                rows.append((stamp.strftime("%Y-%m-%d %H:%M"), o, h, l, c))
                price = c
                stamp += pd.Timedelta(minutes=5)
        return intraday_frame(rows)

    def test_speed_of_detect_setup(self):
        walk = self.random_sessions()
        frames = [walk.iloc[k - 150:k] for k in range(151, 226)]         # 150-row windows ending today
        lo, hi = float(walk["Low"].min()), float(walk["High"].max())
        levels = [lv("PDL", lo + (hi - lo) * 0.25), lv("PWL", lo), lv("ORL", lo + (hi - lo) * 0.4),
                  lv("H1_SL", lo + (hi - lo) * 0.3), lv("D_SL", lo + (hi - lo) * 0.1),
                  lv("PDH", lo + (hi - lo) * 0.75), lv("PWH", hi), lv("ORH", lo + (hi - lo) * 0.6),
                  lv("H1_SH", lo + (hi - lo) * 0.7), lv("D_SH", lo + (hi - lo) * 0.9),
                  lv("HOD", lo + (hi - lo) * 0.65), lv("LOD", lo + (hi - lo) * 0.35)]
        mid = (lo + hi) / 2
        gaps = [
            FVG("H1", BULL, top=mid, bottom=mid - 1.5, created_time="2026-11-12T11:15:00+05:30",
                known_at="2026-11-12T12:15:00+05:30"),
            FVG("H1", BEAR, top=mid + 2.5, bottom=mid + 1.0, created_time="2026-11-12T13:15:00+05:30",
                known_at="2026-11-12T14:15:00+05:30"),
        ]
        day = date(2026, 11, 13)
        calls, signals = 2000, 0
        start = _time.perf_counter()
        for i in range(calls):
            frame = frames[i % len(frames)]
            direction = LONG if (i // len(frames)) % 2 == 0 else SHORT
            if ict.detect_setup("RND", frame, day, direction, levels, gaps) is not None:
                signals += 1
        mean_ms = (_time.perf_counter() - start) / calls * 1000.0
        print(f"\n[timing] detect_setup: {mean_ms:.4f} ms per call over {calls} calls on 150-row frames "
              f"({signals} signals)")
        # These tests gate the GitHub Action, so the limits only catch an order-of-magnitude
        # regression (normal: about 0.03 to 0.1 ms) and cannot fail on a slow or busy runner.
        self.assertLess(mean_ms, 20.0)

    def test_speed_of_the_hourly_helpers(self):
        walk = self.random_sessions()
        hourly = walk.iloc[::2].iloc[-150:]          # any 150-row intraday frame will do for timing
        calls = 300
        start = _time.perf_counter()
        for _ in range(calls):
            ict.find_fvgs(hourly, "H1", config.H1_FVG_MIN_PCT, 60, config.H1_FVG_LOOKBACK_BARS)
        fvg_ms = (_time.perf_counter() - start) / calls * 1000.0
        today = walk.iloc[150:]
        prev = walk.iloc[75:150]
        daily = daily_frame(LiquidityLevelsTest.DAILY)
        start = _time.perf_counter()
        for _ in range(calls):
            ict.liquidity_levels(prev, daily, hourly, today)
        lvl_ms = (_time.perf_counter() - start) / calls * 1000.0
        start = _time.perf_counter()
        for _ in range(calls):
            ict.structure_bias(hourly, "H1")
        bias_ms = (_time.perf_counter() - start) / calls * 1000.0
        print(f"[timing] find_fvgs {fvg_ms:.4f} ms, liquidity_levels {lvl_ms:.4f} ms, "
              f"structure_bias {bias_ms:.4f} ms per call")
        self.assertLess(fvg_ms, 50.0)          # normal: about 0.8 ms; see test_speed_of_detect_setup


if __name__ == "__main__":
    unittest.main()
