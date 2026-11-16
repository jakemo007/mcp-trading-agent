"""Tests for intraday_trader.data. Synthetic frames only: nothing here touches the network.

Run from the repo root:
    python -m unittest discover -s tests -p "test_intraday_data.py"
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, time
from unittest import mock

import numpy as np
import pandas as pd

from intraday_trader import config, data

IST = "Asia/Kolkata"
FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def session(day: str, n: int = 75, base: float = 100.0, vol: float = 1000.0, start: str = "09:15") -> pd.DataFrame:
    """A clean 5m session: `n` bars from `start`, prices drifting up 0.1 a bar."""
    idx = pd.date_range("%s %s" % (day, start), periods=n, freq="5min", tz=IST)
    close = base + np.arange(n) * 0.1
    return pd.DataFrame(
        {"Open": close - 0.05, "High": close + 0.2, "Low": close - 0.2, "Close": close, "Volume": float(vol)},
        index=idx,
    )


def random_session(day: str, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("%s 09:15" % day, periods=75, freq="5min", tz=IST)
    close = 500 + rng.normal(0, 1, 75).cumsum()
    spread = rng.uniform(0.1, 1.0, 75)
    return pd.DataFrame(
        {
            "Open": close + rng.normal(0, 0.2, 75),
            "High": close + spread,
            "Low": close - spread,
            "Close": close,
            "Volume": rng.integers(0, 50_000, 75).astype(float),
        },
        index=idx,
    )


def raw_utc(rows) -> pd.DataFrame:
    """Raw single-ticker Yahoo frame. rows: [(utc stamp, close, volume)]."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0], tz="UTC") for r in rows])
    close = np.array([r[1] for r in rows], dtype=float)
    return pd.DataFrame(
        {
            "Open": close, "High": close + 1, "Low": close - 1, "Close": close,
            "Adj Close": close, "Volume": [r[2] for r in rows],
        },
        index=idx,
    )


def raw_multi(per_ticker: dict, ticker_first: bool = True) -> pd.DataFrame:
    """Raw multi-ticker Yahoo frame from {ticker: single-ticker raw frame}."""
    parts = {}
    for ticker, frame in per_ticker.items():
        for field in frame.columns:
            parts[(ticker, field) if ticker_first else (field, ticker)] = frame[field]
    out = pd.DataFrame(parts)
    out.columns = pd.MultiIndex.from_tuples(out.columns)
    return out


class Clean5mTests(unittest.TestCase):
    def test_converts_to_ist_and_keeps_only_regular_session_bars_on_the_grid(self):
        raw = raw_utc([
            ("2026-11-13 09:55", 110.0, 50),      # 15:25 IST: last regular bar
            ("2026-11-13 03:40", 99.0, 10),       # 09:10 IST: pre-open, dropped
            ("2026-11-13 03:45", 100.0, 20),      # 09:15 IST
            ("2026-11-13 03:47", 100.5, 30),      # 09:17 IST: off the 5-minute grid, dropped
            ("2026-11-13 03:50", 101.0, 40),      # 09:20 IST
            ("2026-11-13 10:00", 111.0, 60),      # 15:30 IST: after the close, dropped
        ])
        out = data.clean_5m(raw)

        self.assertEqual(str(out.index.tz), IST)
        self.assertEqual(list(out.columns), data.OHLCV)
        self.assertEqual(
            [ts.strftime("%Y-%m-%d %H:%M") for ts in out.index],
            ["2026-11-13 09:15", "2026-11-13 09:20", "2026-11-13 15:25"],
        )
        self.assertEqual(list(out["Close"]), [100.0, 101.0, 110.0])
        self.assertTrue(out.index.is_monotonic_increasing)
        self.assertTrue(all(str(t) == "float64" for t in out.dtypes))

    def test_drops_rows_without_prices_fills_missing_volume_and_dedupes(self):
        raw = raw_utc([
            ("2026-11-13 03:45", 100.0, 20),
            ("2026-11-13 03:50", np.nan, 30),     # no prices: dropped
            ("2026-11-13 03:55", 102.0, np.nan),  # no volume: kept as 0
            ("2026-11-13 04:00", 103.0, 40),
            ("2026-11-13 04:00", 104.0, 45),      # same stamp again: the later row wins
        ])
        out = data.clean_5m(raw)

        self.assertEqual([ts.strftime("%H:%M") for ts in out.index], ["09:15", "09:25", "09:30"])
        self.assertEqual(list(out["Volume"]), [20.0, 0.0, 45.0])
        self.assertEqual(out["Close"].iloc[-1], 104.0)
        self.assertFalse(out.index.has_duplicates)
        self.assertFalse(out.isna().any().any())

    def test_accepts_both_multiindex_column_layouts(self):
        one = raw_utc([("2026-11-13 03:45", 100.0, 20), ("2026-11-13 03:50", 101.0, 30)])
        for ticker_first in (True, False):
            multi = raw_multi({"X.NS": one}, ticker_first=ticker_first)
            out = data.clean_5m(multi)
            self.assertEqual(list(out.columns), data.OHLCV)
            self.assertEqual(list(out["Close"]), [100.0, 101.0])

    def test_naive_index_is_read_as_utc(self):
        raw = raw_utc([("2026-11-13 03:45", 100.0, 20)])
        raw.index = raw.index.tz_localize(None)
        out = data.clean_5m(raw)
        self.assertEqual(out.index[0], pd.Timestamp("2026-11-13 09:15", tz=IST))

    def test_unusable_input_gives_an_empty_ist_frame(self):
        no_close = raw_utc([("2026-11-13 03:45", 100.0, 20)]).drop(columns=["Close"])
        all_nan = raw_utc([("2026-11-13 03:45", np.nan, np.nan)])
        for bad in (None, pd.DataFrame(), no_close, all_nan):
            out = data.clean_5m(bad)
            self.assertTrue(out.empty)
            self.assertEqual(list(out.columns), data.OHLCV)
            self.assertEqual(str(out.index.tz), IST)


class CleanDailyTests(unittest.TestCase):
    def _raw(self, idx):
        close = np.arange(len(idx), dtype=float) + 100
        return pd.DataFrame(
            {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Adj Close": close, "Volume": 5.0},
            index=idx,
        )

    def test_tz_aware_index_becomes_naive_session_dates(self):
        idx = pd.DatetimeIndex(["2026-11-12", "2026-11-13"]).tz_localize(IST)
        out = data.clean_daily(self._raw(idx))
        self.assertIsNone(out.index.tz)
        self.assertEqual(list(out.index), [pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-13")])
        self.assertEqual(list(out.columns), data.OHLCV)

    def test_utc_stamps_map_to_the_ist_session_date(self):
        # 18:30 UTC on the 12th is midnight IST on the 13th.
        idx = pd.DatetimeIndex([pd.Timestamp("2026-11-12 18:30", tz="UTC")])
        out = data.clean_daily(self._raw(idx))
        self.assertEqual(list(out.index), [pd.Timestamp("2026-11-13")])

    def test_naive_index_sorted_deduped_and_nan_rows_dropped(self):
        idx = pd.DatetimeIndex(["2026-11-13", "2026-11-11", "2026-11-12", "2026-11-12"])
        raw = self._raw(idx)
        raw.iloc[1, raw.columns.get_loc("Close")] = np.nan      # the 11th has no close
        out = data.clean_daily(raw)
        self.assertEqual(list(out.index), [pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-13")])
        self.assertEqual(out.loc[pd.Timestamp("2026-11-12"), "Close"], 103.0)   # later duplicate wins

    def test_unusable_input_gives_an_empty_frame(self):
        for bad in (None, pd.DataFrame()):
            out = data.clean_daily(bad)
            self.assertTrue(out.empty)
            self.assertEqual(list(out.columns), data.OHLCV)


class SplitByTickerTests(unittest.TestCase):
    def setUp(self):
        self.a = raw_utc([("2026-11-13 03:45", 100.0, 20)])
        self.b = raw_utc([("2026-11-13 03:45", 200.0, 30)])

    def test_both_layouts_and_absent_tickers(self):
        for ticker_first in (True, False):
            raw = raw_multi({"A.NS": self.a, "B.NS": self.b}, ticker_first=ticker_first)
            got = data.split_by_ticker(raw, ["A.NS", "B.NS", "NOPE.NS"])
            self.assertEqual(sorted(got), ["A.NS", "B.NS"])
            self.assertEqual(float(got["B.NS"]["Close"].iloc[0]), 200.0)

    def test_flat_columns_belong_to_the_single_requested_ticker(self):
        self.assertEqual(list(data.split_by_ticker(self.a, ["A.NS"])), ["A.NS"])
        self.assertEqual(data.split_by_ticker(self.a, ["A.NS", "B.NS"]), {})

    def test_empty_download(self):
        self.assertEqual(data.split_by_ticker(None, ["A.NS"]), {})
        self.assertEqual(data.split_by_ticker(pd.DataFrame(), ["A.NS"]), {})


class FetchTests(unittest.TestCase):
    """fetch_5m / fetch_daily with the Yahoo request replaced by a fake."""

    def setUp(self):
        self.calls = []
        patches = [
            mock.patch.object(data._time, "sleep", lambda s: None),
            mock.patch.object(config, "DOWNLOAD_CHUNK", 2),
            mock.patch.object(config, "DOWNLOAD_RETRIES", 3),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake(self, empty=(), fail_first=0):
        """A downloader that knows every ticker except those in `empty`."""
        state = {"failures_left": fail_first}

        def download(tickers, period, interval):
            self.calls.append((list(tickers), period, interval))
            if state["failures_left"] > 0:
                state["failures_left"] -= 1
                raise RuntimeError("rate limited")
            frames = {}
            for i, t in enumerate(tickers):
                close = np.nan if t in empty else 100.0 + i
                frames[t] = raw_utc([("2026-11-13 03:45", close, 10), ("2026-11-13 03:50", close, 20)])
            return raw_multi(frames)

        return download

    def test_chunks_requests_and_keys_frames_by_bare_symbol(self):
        with mock.patch.object(data, "_download_raw", self._fake()):
            got = data.fetch_5m(["RELIANCE", "M&M", "BAJAJ-AUTO", "SBIN", "TCS"], period="1mo")

        self.assertEqual(list(got), ["RELIANCE", "M&M", "BAJAJ-AUTO", "SBIN", "TCS"])
        self.assertEqual(
            [c[0] for c in self.calls],
            [["RELIANCE.NS", "M&M.NS"], ["BAJAJ-AUTO.NS", "SBIN.NS"], ["TCS.NS"]],
        )
        self.assertTrue(all(c[1:] == ("1mo", "5m") for c in self.calls))
        self.assertEqual(str(got["M&M"].index.tz), IST)
        self.assertEqual(list(got["M&M"].columns), data.OHLCV)

    def test_symbol_without_data_is_left_out_and_logged_not_raised(self):
        with mock.patch.object(config, "DOWNLOAD_CHUNK", 8), \
                mock.patch.object(data, "_download_raw", self._fake(empty={"BOGUS.NS"})), \
                self.assertLogs("intraday_trader.data", level="WARNING") as logs:
            got = data.fetch_5m(["A", "B", "C", "D", "E", "F", "G", "BOGUS"])

        self.assertEqual(list(got), ["A", "B", "C", "D", "E", "F", "G"])
        self.assertEqual(len(self.calls), 1)        # a mostly good response is not retried
        self.assertTrue(any("BOGUS" in line for line in logs.output))

    def test_failed_request_is_retried_then_succeeds(self):
        with mock.patch.object(data, "_download_raw", self._fake(fail_first=2)), \
                self.assertLogs("intraday_trader.data", level="WARNING"):
            got = data.fetch_5m(["A", "B"])
        self.assertEqual(list(got), ["A", "B"])
        self.assertEqual(len(self.calls), 3)

    def test_chunk_that_keeps_failing_is_skipped_after_the_retries(self):
        with mock.patch.object(data, "_download_raw", self._fake(fail_first=99)), \
                self.assertLogs("intraday_trader.data", level="WARNING"):
            got = data.fetch_5m(["A", "B"])
        self.assertEqual(got, {})
        self.assertEqual(len(self.calls), 1 + config.DOWNLOAD_RETRIES)

    def test_empty_response_is_retried_only_for_the_missing_symbols(self):
        with mock.patch.object(data, "_download_raw", self._fake(empty={"A.NS", "B.NS"})), \
                self.assertLogs("intraday_trader.data", level="WARNING"):
            got = data.fetch_5m(["A", "B"])
        self.assertEqual(got, {})
        self.assertEqual(len(self.calls), 1 + config.DOWNLOAD_RETRIES)

    def test_duplicate_and_blank_symbols_are_requested_once(self):
        with mock.patch.object(data, "_download_raw", self._fake()):
            got = data.fetch_5m(["A", " A ", "", "B"])
        self.assertEqual(list(got), ["A", "B"])
        self.assertEqual([c[0] for c in self.calls], [["A.NS", "B.NS"]])

    def test_fetch_daily_uses_the_daily_interval_and_naive_dates(self):
        def download(tickers, period, interval):
            self.calls.append((list(tickers), period, interval))
            idx = pd.DatetimeIndex(["2026-11-12", "2026-11-13"])
            frame = pd.DataFrame(
                {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Adj Close": 1.5, "Volume": 9.0}, index=idx
            )
            return raw_multi({t: frame for t in tickers})

        with mock.patch.object(data, "_download_raw", download):
            got = data.fetch_daily(["RELIANCE"], period="2y")
        self.assertEqual(self.calls, [(["RELIANCE.NS"], "2y", "1d")])
        self.assertIsNone(got["RELIANCE"].index.tz)
        self.assertEqual(list(got["RELIANCE"].index), [pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-13")])


class UniverseTests(unittest.TestCase):
    def test_yahoo_ticker(self):
        self.assertEqual(data.yahoo_ticker("M&M"), "M&M.NS")
        self.assertEqual(data.yahoo_ticker("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        self.assertEqual(data.yahoo_ticker(" reliance "), "RELIANCE.NS")
        self.assertEqual(data.yahoo_ticker("SBIN.NS"), "SBIN.NS")

    def test_shipped_universe_loads(self):
        universe = data.load_universe()
        symbols = [u["symbol"] for u in universe]
        self.assertGreater(len(universe), 150)
        self.assertEqual(len(symbols), len(set(symbols)))
        for expected in ("RELIANCE", "M&M", "BAJAJ-AUTO"):
            self.assertIn(expected, symbols)
        self.assertTrue(all(u["name"] for u in universe))
        self.assertFalse(any(s.startswith("NIFTY") for s in symbols))

    def test_blank_and_duplicate_rows_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "u.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("symbol,name\nAAA,Alpha Ltd\n,No Symbol\nBBB,\nAAA,Alpha Again\n")
            self.assertEqual(
                data.load_universe(path),
                [{"symbol": "AAA", "name": "Alpha Ltd"}, {"symbol": "BBB", "name": "BBB"}],
            )


class SessionHelperTests(unittest.TestCase):
    def setUp(self):
        self.df = pd.concat([session("2026-11-12"), session("2026-11-13", n=20), session("2026-11-16", n=5)])

    def test_session_dates(self):
        self.assertEqual(
            data.session_dates(self.df), [date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)]
        )
        self.assertTrue(all(type(d) is date for d in data.session_dates(self.df)))
        self.assertEqual(data.session_dates(self.df.iloc[:0]), [])

    def test_day_bars(self):
        got = data.day_bars(self.df, date(2026, 11, 13))
        self.assertEqual(len(got), 20)
        self.assertEqual(got.index[0], pd.Timestamp("2026-11-13 09:15", tz=IST))
        self.assertEqual(got.index[-1], pd.Timestamp("2026-11-13 10:50", tz=IST))
        self.assertTrue(data.day_bars(self.df, date(2026, 11, 14)).empty)     # a Saturday
        self.assertTrue(data.day_bars(self.df.iloc[:0], date(2026, 11, 13)).empty)

    def test_day_bars_on_a_utc_index_still_cuts_on_the_ist_day(self):
        got = data.day_bars(self.df.tz_convert("UTC"), date(2026, 11, 13))
        self.assertEqual(len(got), 20)

    def test_clock_parts(self):
        today = data.day_number(date(2026, 11, 13))
        # 03:45 UTC is 09:15 IST; 19:00 UTC is already 00:30 IST on the next day.
        utc = pd.DatetimeIndex(["2026-11-13 03:45", "2026-11-13 19:00"], tz="UTC")
        days, minutes = data.clock_parts(utc)
        self.assertEqual(list(minutes), [9 * 60 + 15, 30])
        self.assertEqual(list(days), [today, today + 1])
        # A tz-naive index is taken as IST wall-clock time.
        days, minutes = data.clock_parts(pd.DatetimeIndex(["2026-11-13 15:25"]))
        self.assertEqual((list(days), list(minutes)), ([today], [15 * 60 + 25]))
        self.assertEqual(data.minute_of_day(time(10, 15)), 615)


class ToHourlyTests(unittest.TestCase):
    def test_full_session_gives_six_hours_and_a_short_last_bucket(self):
        df = session("2026-11-13", vol=1000.0)
        h = data.to_hourly(df)

        self.assertEqual(
            [ts.strftime("%H:%M") for ts in h.index],
            ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"],
        )
        self.assertEqual(str(h.index.tz), IST)
        self.assertEqual(list(h.columns), data.OHLCV)
        first = df.iloc[:12]                         # 09:15 .. 10:10
        self.assertEqual(h["Open"].iloc[0], first["Open"].iloc[0])
        self.assertEqual(h["High"].iloc[0], first["High"].max())
        self.assertEqual(h["Low"].iloc[0], first["Low"].min())
        self.assertEqual(h["Close"].iloc[0], first["Close"].iloc[-1])
        self.assertEqual(h["Volume"].iloc[0], 12_000.0)
        tail = df.iloc[72:]                          # 15:15, 15:20, 15:25
        self.assertEqual(h["Open"].iloc[-1], tail["Open"].iloc[0])
        self.assertEqual(h["Close"].iloc[-1], tail["Close"].iloc[-1])
        self.assertEqual(h["Volume"].iloc[-1], 3_000.0)
        self.assertEqual(h["Volume"].sum(), df["Volume"].sum())

    def test_sessions_are_never_merged(self):
        df = pd.concat([session("2026-11-12"), session("2026-11-13")])
        h = data.to_hourly(df)
        self.assertEqual(len(h), 14)
        self.assertEqual(h.index[6], pd.Timestamp("2026-11-12 15:15", tz=IST))
        self.assertEqual(h.index[7], pd.Timestamp("2026-11-13 09:15", tz=IST))
        self.assertEqual(h["Volume"].iloc[6], 3_000.0)
        self.assertEqual(h["Volume"].iloc[7], 12_000.0)

    def test_hours_without_any_bar_are_not_emitted(self):
        df = session("2026-11-13")
        clock = df.index.strftime("%H:%M")
        df = df[(clock < "11:15") | (clock >= "12:15")]      # Yahoo lost a whole hour
        h = data.to_hourly(df)
        self.assertEqual(
            [ts.strftime("%H:%M") for ts in h.index], ["09:15", "10:15", "12:15", "13:15", "14:15", "15:15"]
        )

    def test_partial_hour_is_still_bucketed_on_the_0915_grid(self):
        df = session("2026-11-13", n=14)             # 09:15 .. 10:20
        h = data.to_hourly(df)
        self.assertEqual([ts.strftime("%H:%M") for ts in h.index], ["09:15", "10:15"])
        self.assertEqual(h["Volume"].iloc[1], 2_000.0)

        late = session("2026-11-13", n=3, start="10:40")     # first bar of the day is mid-bucket
        self.assertEqual([ts.strftime("%H:%M") for ts in data.to_hourly(late).index], ["10:15"])

    def test_matches_a_pandas_resample_on_random_sessions(self):
        df = pd.concat([random_session("2026-11-11", 1), random_session("2026-11-12", 2), random_session("2026-11-13", 3)])
        df = df.drop(df.index[[5, 17, 18, 40, 100, 180, 224]])          # holes like Yahoo's
        expected = (
            df.resample("60min", offset="15min")
            .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
            .dropna(subset=["Open"])
        )
        got = data.to_hourly(df)
        self.assertEqual(list(got.index), list(expected.index))
        np.testing.assert_allclose(got.to_numpy(), expected.to_numpy())

    def test_utc_index_gives_the_same_buckets(self):
        df = session("2026-11-13")
        pd.testing.assert_frame_equal(data.to_hourly(df.tz_convert("UTC")), data.to_hourly(df), check_freq=False)

    def test_empty_input(self):
        h = data.to_hourly(session("2026-11-13").iloc[:0])
        self.assertTrue(h.empty)
        self.assertEqual(list(h.columns), data.OHLCV)
        self.assertTrue(data.to_hourly(None).empty)


class HourlyAsofTests(unittest.TestCase):
    """The no-lookahead foundation: only finished 1h candles, built from finished 5m bars."""

    DAY = "2026-11-13"

    def setUp(self):
        self.yesterday = session("2026-11-12", base=90.0)
        self.today = session(self.DAY, base=100.0)
        self.full = pd.concat([self.yesterday, self.today, session("2026-11-16", base=120.0)])

    def _ts(self, clock: str) -> pd.Timestamp:
        return pd.Timestamp("%s %s" % (self.DAY, clock), tz=IST)

    def _today_buckets(self, df, clock: str):
        h = data.hourly_asof(df, self._ts(clock))
        return [ts.strftime("%H:%M") for ts in h.index if ts.strftime("%Y-%m-%d") == self.DAY]

    def test_examples_from_the_contract(self):
        upto_1010 = pd.concat([self.yesterday, self.today.iloc[:12]])        # last bar 10:10
        self.assertEqual(self._today_buckets(upto_1010, "10:15"), ["09:15"])
        self.assertEqual(self._today_buckets(upto_1010, "10:14"), [])
        upto_1115 = pd.concat([self.yesterday, self.today.iloc[:25]])        # last bar 11:15
        self.assertEqual(self._today_buckets(upto_1115, "11:20"), ["09:15", "10:15"])
        # Yesterday's seven candles are always there.
        self.assertEqual(len(data.hourly_asof(upto_1010, self._ts("10:14"))), 7)

    def test_before_the_first_hour_ends_today_has_no_candle(self):
        for clock in ("09:15", "09:20", "10:00", "10:14"):
            self.assertEqual(self._today_buckets(self.full, clock), [], clock)

    def test_a_candle_appears_exactly_when_its_hour_ends(self):
        expected = {
            "10:15": 1, "10:19": 1, "11:14": 1, "11:15": 2, "12:15": 3, "13:15": 4,
            "14:15": 5, "15:14": 5, "15:15": 6, "15:25": 6, "15:29": 6, "15:30": 7,
        }
        for clock, n in expected.items():
            self.assertEqual(len(self._today_buckets(self.full, clock)), n, clock)

    def test_the_short_last_bucket_needs_the_session_close(self):
        self.assertEqual(self._today_buckets(self.full, "15:29")[-1], "14:15")
        self.assertEqual(self._today_buckets(self.full, "15:30")[-1], "15:15")
        h = data.hourly_asof(self.full, self._ts("15:30"))
        self.assertEqual(h["Volume"].iloc[-1], 3_000.0)

    def test_last_candle_values_come_only_from_closed_bars(self):
        h = data.hourly_asof(self.full, self._ts("10:15"))
        first_hour = self.today.iloc[:12]
        self.assertEqual(h.index[-1], self._ts("09:15"))
        self.assertEqual(h["Close"].iloc[-1], first_hour["Close"].iloc[-1])      # the 10:10 bar
        self.assertEqual(h["High"].iloc[-1], first_hour["High"].max())
        self.assertEqual(h["Volume"].iloc[-1], 12_000.0)

    def test_future_rows_never_change_the_answer(self):
        """Same result from the whole history, from history cut at as_of, and after corrupting the future."""
        clocks = ["09:15", "09:40", "10:14", "10:15", "10:17", "10:20", "11:15", "12:40", "14:15",
                  "15:10", "15:15", "15:29", "15:30"]
        for clock in clocks:
            as_of = self._ts(clock)
            from_full = data.hourly_asof(self.full, as_of)

            known = self.full[self.full.index + pd.Timedelta(minutes=5) <= as_of]
            pd.testing.assert_frame_equal(from_full, data.hourly_asof(known, as_of), check_freq=False)

            corrupted = self.full.copy()
            future = corrupted.index + pd.Timedelta(minutes=5) > as_of
            corrupted.loc[future, ["Open", "High", "Low", "Close"]] = 9_999.0
            corrupted.loc[future, "Volume"] = 1e12
            pd.testing.assert_frame_equal(from_full, data.hourly_asof(corrupted, as_of), check_freq=False)

            # Every returned candle had fully ended, and none carries the corrupt values.
            self.assertTrue((from_full.index + pd.Timedelta(minutes=15) <= as_of).all(), clock)
            self.assertLess(from_full["High"].max(), 9_999.0)

    def test_unclosed_5m_bar_is_not_used(self):
        # At 11:17 the 11:15 bar is still forming; the 10:15 hour is complete, the 11:15 hour is not.
        h = data.hourly_asof(self.full, self._ts("11:17"))
        self.assertEqual(h.index[-1], self._ts("10:15"))
        self.assertEqual(h["Close"].iloc[-1], self.today["Close"].iloc[23])      # the 11:10 bar

    def test_hour_with_a_missing_last_bar_is_still_complete_by_the_clock(self):
        clock = self.today.index.strftime("%H:%M")
        holed = pd.concat([self.yesterday, self.today[clock != "11:10"]])
        h = data.hourly_asof(holed, self._ts("11:15"))
        self.assertEqual(h.index[-1], self._ts("10:15"))
        self.assertEqual(h["Volume"].iloc[-1], 11_000.0)

    def test_naive_as_of_is_read_as_ist(self):
        naive = pd.Timestamp("%s 11:15" % self.DAY)
        pd.testing.assert_frame_equal(
            data.hourly_asof(self.full, naive), data.hourly_asof(self.full, self._ts("11:15")), check_freq=False
        )

    def test_utc_as_of_is_converted(self):
        utc = self._ts("11:15").tz_convert("UTC")
        pd.testing.assert_frame_equal(
            data.hourly_asof(self.full, utc), data.hourly_asof(self.full, self._ts("11:15")), check_freq=False
        )

    def test_empty_and_too_early(self):
        self.assertTrue(data.hourly_asof(self.full.iloc[:0], self._ts("11:15")).empty)
        before_everything = pd.Timestamp("2026-11-12 09:15", tz=IST)
        self.assertTrue(data.hourly_asof(self.full, before_everything).empty)


class ToWeeklyTests(unittest.TestCase):
    def _daily(self, days):
        idx = pd.DatetimeIndex(days)
        n = len(idx)
        close = 100.0 + np.arange(n)
        return pd.DataFrame(
            {"Open": close - 0.5, "High": close + 2, "Low": close - 2, "Close": close, "Volume": 10.0}, index=idx
        )

    def test_weeks_end_on_friday_partial_week_kept_empty_week_dropped(self):
        days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",   # full week
                # the week of the 9th has no data at all
                "2026-11-16", "2026-11-17", "2026-11-18"]                                 # partial week
        daily = self._daily(days)
        w = data.to_weekly(daily)

        self.assertEqual(list(w.index), [pd.Timestamp("2026-11-06"), pd.Timestamp("2026-11-20")])
        self.assertIsNone(w.index.tz)
        self.assertEqual(list(w.columns), data.OHLCV)
        self.assertEqual(w["Open"].iloc[0], daily["Open"].iloc[0])
        self.assertEqual(w["High"].iloc[0], daily["High"].iloc[:5].max())
        self.assertEqual(w["Low"].iloc[0], daily["Low"].iloc[:5].min())
        self.assertEqual(w["Close"].iloc[0], daily["Close"].iloc[4])
        self.assertEqual(w["Volume"].iloc[0], 50.0)
        self.assertEqual(w["Open"].iloc[1], daily["Open"].iloc[5])
        self.assertEqual(w["Close"].iloc[1], daily["Close"].iloc[7])
        self.assertEqual(w["Volume"].iloc[1], 30.0)

    def test_friday_belongs_to_its_own_week_and_monday_to_the_next(self):
        w = data.to_weekly(self._daily(["2026-11-13", "2026-11-16"]))
        self.assertEqual(list(w.index), [pd.Timestamp("2026-11-13"), pd.Timestamp("2026-11-20")])

    def test_empty_input(self):
        self.assertTrue(data.to_weekly(self._daily([])).empty)
        self.assertTrue(data.to_weekly(None).empty)


if __name__ == "__main__":
    unittest.main()
