"""Tests for intraday_trader.volume. Synthetic frames only: nothing here touches the network.

Run from the repo root:
    python -m unittest discover -s tests -p "test_intraday_volume.py"
"""
from __future__ import annotations

import unittest
from datetime import date, time
from unittest import mock

import numpy as np
import pandas as pd

from intraday_trader import config, volume

IST = "Asia/Kolkata"

# Twelve consecutive weekdays. DAY has ten sessions before it and one after it.
SESSIONS = [d.date() for d in pd.bdate_range("2026-10-26", periods=12)]
PREV = SESSIONS[:10]
DAY = SESSIONS[10]
LATER = SESSIONS[11]

T1015 = time(10, 15)
T1115 = time(11, 15)


def bars(day: date, per_bar_volume, close=500.0, n: int = 75, start: str = "09:15") -> pd.DataFrame:
    """One 5m session. per_bar_volume and close may be scalars or arrays of length n."""
    idx = pd.date_range("%s %s" % (day.isoformat(), start), periods=n, freq="5min", tz=IST)
    c = np.broadcast_to(np.asarray(close, dtype=float), (n,)).copy()
    v = np.broadcast_to(np.asarray(per_bar_volume, dtype=float), (n,)).copy()
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": v}, index=idx)


def history(prev_volume, today_volume, close=500.0, prev_days=PREV, today=DAY, today_bars: int = 75) -> pd.DataFrame:
    """`prev_days` sessions at prev_volume a bar, then `today` at today_volume a bar."""
    frames = [bars(d, prev_volume, close) for d in prev_days]
    frames.append(bars(today, today_volume, close, n=today_bars))
    return pd.concat(frames)


def second_hour_surge(first_hour: float, later: float) -> np.ndarray:
    """Per-bar volumes for a session that is quiet until 10:15 and busy afterwards."""
    v = np.full(75, later, dtype=float)
    v[:12] = first_hour
    return v


def by_symbol(rows):
    return {r["symbol"]: r for r in rows}


class RvolTableTests(unittest.TestCase):
    def test_hand_computed_example(self):
        # Ten earlier sessions trade 1000, 1100, ... 1900 shares a bar: the first hour
        # (12 bars before 10:15) averages 12 * 1450 = 17,400. Today trades 3,000 a bar,
        # so 36,000 by 10:15 and RVOL = 36,000 / 17,400 = 2.069.
        frames = [bars(d, 1000 + 100 * i, close=480.0) for i, d in enumerate(PREV)]
        frames[-1].iloc[-1, frames[-1].columns.get_loc("Close")] = 490.0         # yesterday's close
        today_close = np.full(75, 500.0)
        today_close[11] = 502.0                                                   # the 10:10 bar
        today_close[12:] = 510.0                                                  # after the checkpoint
        frames.append(bars(DAY, 3000, close=today_close))
        table = volume.rvol_table({"AAA": pd.concat(frames)}, DAY, T1015, names={"AAA": "Alpha Ltd"})

        self.assertEqual(len(table), 1)
        row = table[0]
        self.assertEqual(
            sorted(row),
            sorted(["symbol", "name", "as_of", "rvol", "cum_volume", "avg_cum_volume", "turnover_cr",
                    "last_price", "prev_close", "day_change_pct"]),
        )
        self.assertEqual(row["symbol"], "AAA")
        self.assertEqual(row["name"], "Alpha Ltd")
        self.assertEqual(row["as_of"], "10:15")
        self.assertEqual(row["cum_volume"], 36_000)
        self.assertAlmostEqual(row["avg_cum_volume"], 17_400.0)
        self.assertAlmostEqual(row["rvol"], 2.07)
        # 11 bars at 500 and one at 502, 3,000 shares each = 18,006,000 rupees = 1.80 crore.
        self.assertAlmostEqual(row["turnover_cr"], 1.80)
        self.assertEqual(row["last_price"], 502.0)
        self.assertEqual(row["prev_close"], 490.0)
        self.assertAlmostEqual(row["day_change_pct"], round((502.0 / 490.0 - 1) * 100, 2))

    def test_later_checkpoint_compares_with_the_same_clock_window(self):
        table = volume.rvol_table({"AAA": history(1000, 3000)}, DAY, T1115)
        row = table[0]
        self.assertEqual(row["as_of"], "11:15")
        self.assertEqual(row["cum_volume"], 24 * 3000)
        self.assertAlmostEqual(row["avg_cum_volume"], 24 * 1000.0)
        self.assertAlmostEqual(row["rvol"], 3.0)

    def test_only_the_last_ten_sessions_feed_the_average(self):
        older = [d.date() for d in pd.bdate_range(end=PREV[0] - pd.Timedelta(days=1), periods=3)]
        frames = [bars(d, 1_000_000) for d in older]                 # ancient heavy days: must be ignored
        frames += [bars(d, 1000) for d in PREV]
        frames.append(bars(DAY, 2000))
        row = volume.rvol_table({"AAA": pd.concat(frames)}, DAY, T1015)[0]
        self.assertAlmostEqual(row["avg_cum_volume"], 12_000.0)
        self.assertAlmostEqual(row["rvol"], 2.0)

    def test_needs_the_minimum_number_of_earlier_sessions(self):
        four = history(1000, 2000, prev_days=PREV[-4:])
        five = history(1000, 2000, prev_days=PREV[-5:])
        table = volume.rvol_table({"FOUR": four, "FIVE": five}, DAY, T1015)
        self.assertEqual([r["symbol"] for r in table], ["FIVE"])
        self.assertEqual(config.RVOL_MIN_SESSIONS, 5)

    def test_earlier_session_with_no_bars_in_the_window_is_not_averaged_as_zero(self):
        # Yesterday's data starts at 12:00 (a hole in the morning): by 10:15 it has no bars.
        frames = [bars(d, 1000) for d in PREV[:-1]]
        frames.append(bars(PREV[-1], 1000, n=42, start="12:00"))
        frames.append(bars(DAY, 2000))
        row = volume.rvol_table({"AAA": pd.concat(frames)}, DAY, T1015)[0]
        self.assertAlmostEqual(row["avg_cum_volume"], 12_000.0)       # mean of nine sessions, not ten
        self.assertAlmostEqual(row["rvol"], 2.0)
        self.assertEqual(row["prev_close"], 500.0)

    def test_symbols_that_cannot_be_rated_are_left_out(self):
        table = volume.rvol_table(
            {
                "ZEROAVG": history(0, 2000),                                   # average volume is zero
                "NOTODAY": pd.concat([bars(d, 1000) for d in PREV]),           # no bars on DAY
                "LATEOPEN": pd.concat([bars(d, 1000) for d in PREV] + [bars(DAY, 2000, n=10, start="10:15")]),
                "EMPTY": bars(DAY, 1000).iloc[:0],
                "NOHISTORY": bars(DAY, 2000),                                  # nothing before DAY
                "OK": history(1000, 2000),
            },
            DAY,
            T1015,
        )
        self.assertEqual([r["symbol"] for r in table], ["OK"])

    def test_zero_volume_today_is_rated_zero_not_dropped(self):
        row = volume.rvol_table({"QUIET": history(1000, 0)}, DAY, T1015)[0]
        self.assertEqual(row["rvol"], 0.0)
        self.assertEqual(row["cum_volume"], 0)

    def test_sorted_by_rvol_descending_then_symbol_and_name_fallback(self):
        table = volume.rvol_table(
            {
                "LOW": history(1000, 1500),
                "TIE_B": history(1000, 3000),
                "HIGH": history(1000, 4000),
                "TIE_A": history(2000, 6000),
            },
            DAY,
            T1015,
            names={"HIGH": "High Ltd"},
        )
        self.assertEqual([r["symbol"] for r in table], ["HIGH", "TIE_A", "TIE_B", "LOW"])
        self.assertEqual([r["rvol"] for r in table], [4.0, 3.0, 3.0, 1.5])
        self.assertEqual(table[0]["name"], "High Ltd")
        self.assertEqual(table[1]["name"], "TIE_A")              # unknown name falls back to the symbol

    def test_accepts_a_utc_index(self):
        ist = history(1000, 3000)
        self.assertEqual(
            volume.rvol_table({"AAA": ist.tz_convert("UTC")}, DAY, T1015),
            volume.rvol_table({"AAA": ist}, DAY, T1015),
        )


class LookaheadGuardTests(unittest.TestCase):
    """Nothing at or after the checkpoint, and no later session, may change a row."""

    def setUp(self):
        self.base = {
            "AAA": history(1000, 3000),
            "BBB": history(20_000, 50_000, close=800.0),
            "CCC": history(5000, 4000, close=120.0),
        }

    def _corrupt_from(self, frames, stamp: pd.Timestamp):
        out = {}
        for symbol, df in frames.items():
            df = df.copy()
            future = df.index >= stamp
            df.loc[future, "Volume"] = df.loc[future, "Volume"] * 1000 + 7
            df.loc[future, ["Open", "High", "Low", "Close"]] = 9_999.0
            out[symbol] = df
        return out

    def test_bars_at_or_after_as_of_do_not_matter(self):
        for as_of in (T1015, T1115, time(13, 15)):
            stamp = pd.Timestamp("%s %s" % (DAY.isoformat(), as_of.strftime("%H:%M")), tz=IST)
            self.assertEqual(
                volume.rvol_table(self._corrupt_from(self.base, stamp), DAY, as_of),
                volume.rvol_table(self.base, DAY, as_of),
                as_of,
            )

    def test_the_bar_just_before_as_of_does_matter(self):
        changed = {k: v.copy() for k, v in self.base.items()}
        stamp = pd.Timestamp("%s 10:10" % DAY.isoformat(), tz=IST)
        changed["AAA"].loc[stamp, "Volume"] += 60_000
        before = by_symbol(volume.rvol_table(self.base, DAY, T1015))["AAA"]
        after = by_symbol(volume.rvol_table(changed, DAY, T1015))["AAA"]
        self.assertEqual(after["cum_volume"], before["cum_volume"] + 60_000)
        self.assertGreater(after["rvol"], before["rvol"])

    def test_later_sessions_do_not_matter(self):
        with_future = {
            symbol: pd.concat([df, bars(LATER, 9_000_000, close=9_999.0)]) for symbol, df in self.base.items()
        }
        for as_of in (T1015, T1115):
            self.assertEqual(
                volume.rvol_table(with_future, DAY, as_of), volume.rvol_table(self.base, DAY, as_of)
            )
        self.assertEqual(volume.build_watchlist(with_future, DAY), volume.build_watchlist(self.base, DAY))

    def test_a_day_in_the_middle_of_a_long_history_equals_the_truncated_history(self):
        rng = np.random.default_rng(7)
        days = [d.date() for d in pd.bdate_range("2026-09-01", periods=40)]
        full = {}
        for i in range(12):
            frames = []
            for d in days:
                v = rng.integers(1_000, 60_000, 75).astype(float) * (1 + (rng.random() > 0.8) * 3)
                frames.append(bars(d, v, close=200.0 + 50 * i + rng.normal(0, 1, 75).cumsum()))
            full["S%02d" % i] = pd.concat(frames)

        for target in (days[12], days[25], days[39]):
            end = pd.Timestamp(target.isoformat(), tz=IST) + pd.Timedelta(days=1)
            truncated = {s: df[df.index < end] for s, df in full.items()}
            self.assertEqual(volume.build_watchlist(full, target), volume.build_watchlist(truncated, target))
            for as_of in config.SCAN_CHECKPOINTS:
                self.assertEqual(
                    volume.rvol_table(full, target, as_of), volume.rvol_table(truncated, target, as_of)
                )
        # The random universe really does produce watchlists, so the comparison above is not vacuous.
        self.assertTrue(any(volume.build_watchlist(full, d) for d in days[12:]))


class BuildWatchlistTests(unittest.TestCase):
    # At 500 a share, 25,000 shares a bar is 15 crore of turnover in the first hour.
    GOOD = dict(prev_volume=10_000, today_volume=25_000)

    def test_item_shape_and_the_three_filters(self):
        frames = {
            "GOOD": history(**self.GOOD),                                # rvol 2.5, 15 cr, price 500
            "LOWRVOL": history(10_000, 12_000),                          # rvol 1.2
            "LOWTURN": history(100, 300),                                # rvol 3.0 but 0.18 cr traded
            "CHEAP": history(1_000_000, 3_000_000, close=20.0),          # rvol 3.0, 72 cr, price 20
        }
        watch = volume.build_watchlist(frames, DAY, names={"GOOD": "Good Ltd"})

        self.assertEqual([w["symbol"] for w in watch], ["GOOD"])
        item = watch[0]
        self.assertEqual(
            sorted(item),
            sorted(["symbol", "name", "as_of", "eligible_from", "rank", "rvol", "cum_volume",
                    "avg_cum_volume", "turnover_cr", "last_price", "prev_close", "day_change_pct"]),
        )
        self.assertEqual(item["name"], "Good Ltd")
        self.assertEqual(item["eligible_from"], "10:15")
        self.assertEqual(item["rank"], 1)
        self.assertEqual(item["rvol"], 2.5)
        self.assertEqual(item["cum_volume"], 300_000)
        self.assertEqual(item["turnover_cr"], 15.0)

        # The rejected names really were rejected by one filter each.
        table = by_symbol(volume.rvol_table(frames, DAY, T1015))
        self.assertLess(table["LOWRVOL"]["rvol"], config.MIN_RVOL)
        self.assertGreaterEqual(table["LOWTURN"]["rvol"], config.MIN_RVOL)
        self.assertLess(table["LOWTURN"]["turnover_cr"], config.MIN_TURNOVER_CR)
        self.assertGreaterEqual(table["CHEAP"]["turnover_cr"], config.MIN_TURNOVER_CR)
        self.assertLess(table["CHEAP"]["last_price"], config.MIN_PRICE)

    def test_thresholds_are_inclusive(self):
        with mock.patch.object(config, "MIN_TURNOVER_CR", 9.0), mock.patch.object(config, "MIN_PRICE", 500.0):
            # rvol exactly 1.5, turnover exactly 500 * 180,000 = 9 crore, price exactly 500.
            watch = volume.build_watchlist({"EDGE": history(10_000, 15_000)}, DAY)
        self.assertEqual([w["symbol"] for w in watch], ["EDGE"])
        self.assertEqual(watch[0]["rvol"], 1.5)

    def test_ranked_by_rvol_with_one_based_ranks(self):
        frames = {
            "TWO": history(10_000, 20_000),
            "FOUR": history(10_000, 40_000),
            "THREE": history(10_000, 30_000),
        }
        watch = volume.build_watchlist(frames, DAY)
        self.assertEqual([(w["symbol"], w["rank"], w["rvol"]) for w in watch],
                         [("FOUR", 1, 4.0), ("THREE", 2, 3.0), ("TWO", 3, 2.0)])
        self.assertTrue(all(w["eligible_from"] == "10:15" for w in watch))

    def test_checkpoint_needs_a_bar_at_or_after_it(self):
        good = self.GOOD
        # Session has only reached the 10:10 bar: the 10:15 checkpoint has not happened yet.
        self.assertEqual(volume.build_watchlist({"GOOD": history(today_bars=12, **good)}, DAY), [])

        # One more bar (10:15) and the first checkpoint runs. That bar itself is not counted.
        frame = history(today_bars=13, **good)
        frame.iloc[-1, frame.columns.get_loc("Volume")] = 5e9
        watch = volume.build_watchlist({"GOOD": frame}, DAY)
        self.assertEqual([(w["symbol"], w["eligible_from"], w["cum_volume"]) for w in watch],
                         [("GOOD", "10:15", 300_000)])

    def test_later_checkpoints_are_skipped_until_the_session_reaches_them(self):
        surge = second_hour_surge(first_hour=10_000, later=200_000)       # rvol 1.0 at 10:15, 10.5 at 11:15
        upto_1110 = {
            "GOOD": history(today_bars=24, **self.GOOD),
            "LATE": pd.concat([bars(d, 10_000) for d in PREV] + [bars(DAY, surge).iloc[:24]]),
        }
        self.assertEqual([w["symbol"] for w in volume.build_watchlist(upto_1110, DAY)], ["GOOD"])

        upto_1115 = {
            "GOOD": history(today_bars=25, **self.GOOD),
            "LATE": pd.concat([bars(d, 10_000) for d in PREV] + [bars(DAY, surge).iloc[:25]]),
        }
        watch = volume.build_watchlist(upto_1115, DAY)
        self.assertEqual([(w["symbol"], w["eligible_from"], w["rank"]) for w in watch],
                         [("GOOD", "10:15", 1), ("LATE", "11:15", 1)])
        self.assertEqual(watch[1]["as_of"], "11:15")
        self.assertEqual(watch[1]["cum_volume"], 12 * 10_000 + 12 * 200_000)

    def test_the_session_progress_is_judged_across_all_symbols(self):
        # LAGGARD's data stops at 10:10 but another symbol has an 11:15 bar, so both checkpoints run.
        frames = {
            "LAGGARD": history(today_bars=12, **self.GOOD),
            "OTHER": history(10_000, 10_000, today_bars=25),
        }
        watch = volume.build_watchlist(frames, DAY)
        self.assertEqual([(w["symbol"], w["eligible_from"]) for w in watch], [("LAGGARD", "10:15")])

    def test_a_listed_symbol_keeps_its_first_checkpoint_row(self):
        watch = volume.build_watchlist({"GOOD": history(**self.GOOD)}, DAY)
        self.assertEqual(len(watch), 1)
        self.assertEqual((watch[0]["eligible_from"], watch[0]["as_of"], watch[0]["cum_volume"]),
                         ("10:15", "10:15", 300_000))

    def test_only_the_top_n_of_each_checkpoint_can_join(self):
        surge = second_hour_surge(first_hour=10_000, later=400_000)
        frames = {
            "A": history(10_000, 40_000),
            "B": history(10_000, 30_000),
            "C": history(10_000, 20_000),            # third at every checkpoint: never in the top two
            "LATE": pd.concat([bars(d, 10_000) for d in PREV] + [bars(DAY, surge)]),
        }
        with mock.patch.object(config, "WATCHLIST_PER_CHECKPOINT", 2):
            watch = volume.build_watchlist(frames, DAY)
        self.assertEqual([(w["symbol"], w["eligible_from"], w["rank"]) for w in watch],
                         [("A", "10:15", 1), ("B", "10:15", 2), ("LATE", "11:15", 1)])

    def test_watchlist_max_caps_the_total(self):
        surge1 = second_hour_surge(first_hour=10_000, later=400_000)
        surge2 = second_hour_surge(first_hour=10_000, later=300_000)
        frames = {
            "A": history(10_000, 40_000),
            "B": history(10_000, 30_000),
            "LATE1": pd.concat([bars(d, 10_000) for d in PREV] + [bars(DAY, surge1)]),
            "LATE2": pd.concat([bars(d, 10_000) for d in PREV] + [bars(DAY, surge2)]),
        }
        with mock.patch.object(config, "WATCHLIST_PER_CHECKPOINT", 2), mock.patch.object(config, "WATCHLIST_MAX", 3):
            watch = volume.build_watchlist(frames, DAY)
        self.assertEqual([w["symbol"] for w in watch], ["A", "B", "LATE1"])

        with mock.patch.object(config, "WATCHLIST_PER_CHECKPOINT", 2), mock.patch.object(config, "WATCHLIST_MAX", 1):
            self.assertEqual([w["symbol"] for w in volume.build_watchlist(frames, DAY)], ["A"])

    def test_default_caps(self):
        frames = {"S%02d" % i: history(10_000, 20_000 + 1_000 * i) for i in range(30)}
        watch = volume.build_watchlist(frames, DAY)
        # Uniform volume keeps the same top ten all day, so only the first checkpoint adds names.
        self.assertEqual(len(watch), config.WATCHLIST_PER_CHECKPOINT)
        self.assertEqual(watch[0]["symbol"], "S29")
        self.assertEqual([w["rank"] for w in watch], list(range(1, 11)))
        self.assertLessEqual(len(watch), config.WATCHLIST_MAX)

    def test_no_data_for_the_day(self):
        self.assertEqual(volume.build_watchlist({}, DAY), [])
        self.assertEqual(volume.build_watchlist({"AAA": history(10_000, 25_000)}, LATER), [])
        self.assertEqual(volume.build_watchlist({"AAA": bars(DAY, 25_000).iloc[:0]}, DAY), [])


if __name__ == "__main__":
    unittest.main()
