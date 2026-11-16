"""End-to-end tests of the real pipeline (volume scan, ICT analysis, engine, portfolio,
reports) on a seeded synthetic market. No network.

The point of this file is the no-lookahead guarantee: a session's result must not
change when later data is removed, neither later sessions nor later candles of the
same session.
"""
from __future__ import annotations

import unittest
from datetime import date, time

import numpy as np
import pandas as pd

from intraday_trader import config, engine, portfolio, report

IST = "Asia/Kolkata"
LAST = date(2026, 11, 13)
SEED = 8                      # chosen because it yields a healthy number of signals and trades


def make_market(seed=SEED, n_symbols=14, n_sessions=16):
    """Random-walk 5m and daily bars: half the symbols trend up, half down, some days carry
    a volume burst so the scan has something to find. Deterministic for a given seed."""
    rng = np.random.default_rng(seed)
    days = [d.date() for d in pd.bdate_range(end=pd.Timestamp(LAST), periods=n_sessions)]
    clocks = pd.date_range("2026-01-01 09:15", "2026-01-01 15:25", freq="5min").strftime("%H:%M")
    bars5, daily = {}, {}
    for k in range(n_symbols):
        symbol = "S%02d" % k
        trend = 1 if k % 2 == 0 else -1
        n_daily = 260
        close = 500.0 * (1 + k * 0.15) * np.exp(np.cumsum(rng.normal(0.0012 * trend, 0.012, n_daily)))
        spread = np.abs(rng.normal(0, 0.008, n_daily)) + 0.004
        d_open = np.r_[close[0], close[:-1]]
        history = pd.DataFrame(
            {"Open": d_open, "High": np.maximum(d_open, close) * (1 + spread),
             "Low": np.minimum(d_open, close) * (1 - spread), "Close": close, "Volume": 1e6},
            index=pd.bdate_range(end=pd.Timestamp(days[0]) - pd.Timedelta(days=1), periods=n_daily),
        )
        rows, stamps, day_rows = [], [], []
        price = float(close[-1])
        for d in days:
            burst = 3.0 if rng.random() < 0.35 else 1.0
            drift = rng.normal(0.00012 * trend, 0.0002)
            day_open, day_high, day_low, day_volume = price, price, price, 0.0
            for j, hm in enumerate(clocks):
                move = rng.normal(drift, 0.0011)
                if rng.random() < 0.06:
                    move += rng.choice([-1, 1]) * rng.uniform(0.003, 0.006)
                o, c = price, price * (1 + move)
                h = max(o, c) * (1 + abs(rng.normal(0, 0.0005)))
                l = min(o, c) * (1 - abs(rng.normal(0, 0.0005)))
                v = float(rng.integers(20000, 60000)) * (2.2 if j < 12 else 1.0) * burst
                stamps.append(pd.Timestamp("%s %s" % (d.isoformat(), hm), tz=IST))
                rows.append((o, h, l, c, v))
                price = c
                day_high, day_low, day_volume = max(day_high, h), min(day_low, l), day_volume + v
            day_rows.append((day_open, day_high, day_low, price, day_volume))
        cols = ["Open", "High", "Low", "Close", "Volume"]
        bars5[symbol] = pd.DataFrame(rows, columns=cols, index=pd.DatetimeIndex(stamps))
        recent = pd.DataFrame(day_rows, columns=cols, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]))
        daily[symbol] = pd.concat([history, recent])
    return days, bars5, daily


def cut_after(bars5, day, clock=None):
    """Drop every bar after `day`; with `clock`, also the bars of `day` starting at or after it."""
    out = {}
    for symbol, df in bars5.items():
        dates = df.index.tz_convert(IST).date
        keep = dates < day
        today = dates == day
        if clock is not None:
            today = today & (df.index.tz_convert(IST).time < clock)
        out[symbol] = df[keep | today]
    return out


def stamp(day, clock):
    return pd.Timestamp.combine(day, clock).tz_localize(IST)


def without(record, *keys):
    return {k: v for k, v in record.items() if k not in keys}


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days, cls.bars5, cls.daily = make_market()
        cls.replay_days = cls.days[-6:]
        cls.results = {d: engine.run_day(d, cls.bars5, cls.daily, 100_000.0) for d in cls.replay_days}

    def test_the_market_actually_exercises_the_pipeline(self):
        signals = sum(len(r["signals"]) for r in self.results.values())
        trades = sum(r["n_trades"] for r in self.results.values())
        self.assertGreaterEqual(signals, 10)
        self.assertGreaterEqual(trades, 5)
        self.assertTrue(any(t["direction"] == "LONG" for r in self.results.values() for t in r["trades"]))
        self.assertTrue(any(t["direction"] == "SHORT" for r in self.results.values() for t in r["trades"]))

    def daily_before(self, day):
        """Daily bars strictly before `day`: what was really known that morning."""
        return {s: df[df.index < pd.Timestamp(day)] for s, df in self.daily.items()}

    def test_later_sessions_do_not_change_a_day(self):
        for day in self.replay_days:
            full = dict(self.results[day])
            only_past = engine.run_day(day, cut_after(self.bars5, day), self.daily_before(day), 100_000.0)
            full.pop("generated_at"); only_past.pop("generated_at")
            self.assertEqual(full, only_past, "result for {} changed when later data was removed".format(day))

    def cuts(self, day):
        """Where to cut the day: right after every candle on which the full run did something
        (a signal, an order filled, expired or was invalidated, a position closed), plus three
        fixed times. A peek at the next candle changes what happens on exactly such a candle,
        so cutting straight after it removes what the peek would have seen."""
        full = self.results[day]
        events = set()
        for s in full["signals"]:
            events.add(s["signal_time"])
            if s.get("resolved_time"):
                events.add(s["resolved_time"])
        for t in full["trades"]:
            events |= {t["entry_time"], t["exit_time"]}
        cuts = {pd.Timestamp(e) + pd.Timedelta(minutes=5) for e in events}
        cuts |= {stamp(day, clock) for clock in (time(11, 30), time(13, 0), time(14, 15))}
        return sorted(cuts)

    def assert_same_history(self, full, part, day, cut):
        """Compare the full-day result with one computed from the candles that started before `cut`.

        The last candle the part-day run saw started at cut - 5 minutes. Everything up to and
        including that candle must be identical: the scan figures, the signals and how each
        one ended, the fills and the exits. Two differences are what cutting the day forces,
        and only those are allowed. On its last candle the part-day run closes what is still
        open (unless the cut is the real square-off), and an order still waiting then expires.
        """
        last_seen = (cut - pd.Timedelta(minutes=5)).isoformat()
        forced_close = cut.time() < config.SQUARE_OFF
        where = "{} cut at {}".format(day, cut.strftime("%H:%M"))

        scan_keys = ("symbol", "eligible_from", "rank", "rvol", "cum_volume", "avg_cum_volume",
                     "turnover_cr", "last_price")
        part_scan = [tuple(w[k] for k in scan_keys) for w in part["watchlist"]]
        full_scan = [tuple(w[k] for k in scan_keys) for w in full["watchlist"]]
        self.assertEqual(part_scan, full_scan[: len(part_scan)], where + ": the scan")

        outcome = ("status", "skip_reason", "resolved_time", "trade_id")
        full_sigs = [s for s in full["signals"] if s["signal_time"] <= last_seen]
        part_sigs = [s for s in part["signals"] if s["signal_time"] <= last_seen]
        self.assertEqual([without(s, *outcome) for s in full_sigs], [without(s, *outcome) for s in part_sigs],
                         where + ": signals")
        for f, p in zip(full_sigs, part_sigs):
            if f.get("resolved_time") and f["resolved_time"] <= last_seen:
                self.assertEqual([f.get(k) for k in outcome], [p.get(k) for k in outcome], where + ": " + f["symbol"])
            else:
                self.assertEqual((p.get("status"), p.get("skip_reason"), p.get("resolved_time")),
                                 ("EXPIRED", "session ended before the order filled", last_seen),
                                 where + ": " + f["symbol"])

        def entries(result):
            return sorted((t["symbol"], t["entry_time"], t["entry"], t["qty"], t["stop"], t["target"])
                          for t in result["trades"] if t["entry_time"] <= last_seen)

        def exits(result, is_part):
            out = []
            for t in result["trades"]:
                if t["exit_time"] > last_seen:
                    continue
                if is_part and forced_close and t["exit_time"] == last_seen and t["exit_reason"] == "EOD":
                    continue          # the cut, not the market, closed this one
                out.append(without(t, "chart"))
            return sorted(out, key=lambda t: (t["exit_time"], t["symbol"]))

        self.assertEqual(entries(full), entries(part), where + ": fills")
        self.assertEqual(exits(full, False), exits(part, True), where + ": exits")
        return len(full_sigs) + len(entries(full))

    def test_later_candles_of_the_same_day_do_not_change_what_already_happened(self):
        """Replay each day again, stopping right after each candle on which something
        happened. Every signal, fill and exit up to that candle must come out the same, with
        the same prices. A one-candle peek ahead anywhere in the replay fails here."""
        checked = runs = 0
        for day in self.replay_days:
            full = self.results[day]
            for cut in self.cuts(day):
                part = engine.run_day(day, cut_after(self.bars5, day, cut.time()), self.daily_before(day), 100_000.0)
                checked += self.assert_same_history(full, part, day, cut)
                runs += 1
        self.assertGreater(runs, 30)
        self.assertGreater(checked, 50)

    def test_every_trade_obeys_the_rules_and_the_money_adds_up(self):
        for day, result in self.results.items():
            window_open = stamp(day, config.ENTRY_START)
            window_close = stamp(day, config.ENTRY_END)
            square_off = stamp(day, config.SQUARE_OFF)
            for t in result["trades"]:
                sign = 1 if t["direction"] == "LONG" else -1
                self.assertGreater(sign * (t["entry"] - t["stop"]), 0)
                self.assertGreater(sign * (t["target"] - t["entry"]), 0)
                self.assertGreaterEqual(t["qty"], 1)
                self.assertLessEqual(t["exposure"], config.MAX_MARGIN_PER_TRADE * config.LEVERAGE + 0.01)
                self.assertLessEqual(t["margin"], config.MAX_MARGIN_PER_TRADE + 0.01)
                self.assertAlmostEqual(t["exposure"], t["qty"] * t["entry"], places=2)
                self.assertAlmostEqual(t["gross_pnl"], sign * (t["exit_price"] - t["entry"]) * t["qty"], places=2)
                self.assertEqual(t["charges"], config.CHARGE_PER_TRADE)
                self.assertAlmostEqual(t["net_pnl"], t["gross_pnl"] - t["charges"], places=2)
                self.assertAlmostEqual(t["risk_inr"], t["qty"] * abs(t["entry"] - t["stop"]), places=2)
                self.assertLessEqual(t["risk_inr"], config.MAX_RISK_PCT_PER_TRADE * result["start_capital"] + 0.01)
                stop_pct = abs(t["entry"] - t["stop"]) / t["entry"]
                self.assertGreaterEqual(stop_pct, config.MIN_STOP_PCT - 1e-4)
                self.assertLessEqual(stop_pct, config.MAX_STOP_PCT + 1e-4)
                self.assertGreaterEqual(t["rr_planned"], config.MIN_RR - 0.02)
                self.assertLessEqual(t["rr_planned"], config.MAX_RR + 0.02)
                self.assertIn(t["exit_reason"], ("TARGET", "STOP", "EOD"))
                if t["exit_reason"] == "TARGET":
                    self.assertGreaterEqual(sign * (t["exit_price"] - t["target"]), 0)
                if t["exit_reason"] == "STOP":
                    self.assertLessEqual(sign * (t["exit_price"] - t["stop"]), 0)
                signal_ts, entry_ts, exit_ts = (pd.Timestamp(t[k]) for k in ("signal_time", "entry_time", "exit_time"))
                self.assertGreaterEqual(signal_ts, window_open)
                self.assertGreater(entry_ts, signal_ts)
                self.assertLess(entry_ts, window_close)
                self.assertGreaterEqual(exit_ts, entry_ts)
                self.assertLess(exit_ts, square_off)
                self.assertTrue(t["story"] and t["contexts"])
            symbols = [t["symbol"] for t in result["trades"]]
            self.assertEqual(len(symbols), len(set(symbols)), "one trade per symbol per day")
            self.assertLessEqual(len(symbols), config.MAX_TRADES_PER_DAY)
            self.assertAlmostEqual(result["net_pnl"], sum(t["net_pnl"] for t in result["trades"]), places=2)
            self.assertAlmostEqual(result["end_capital"], result["start_capital"] + result["net_pnl"], places=2)
            for s in result["signals"]:
                self.assertIn(s["status"], ("FILLED", "EXPIRED", "INVALIDATED", "SKIPPED"))
            self.assertEqual(sum(1 for s in result["signals"] if s["status"] == "FILLED"), result["n_trades"])
            for w in result["watchlist"]:
                self.assertIn(w["status"], ("TRADED", "SIGNAL_NOT_FILLED", "NO_SETUP", "NOT_ALIGNED"))
                self.assertGreaterEqual(w["rvol"], config.MIN_RVOL)

    def test_open_positions_never_exceed_the_cap(self):
        for result in self.results.values():
            events = []
            for t in result["trades"]:
                events.append((t["entry_time"], 1))
                events.append((t["exit_time"], -1))
            open_now = 0
            for _, change in sorted(events, key=lambda e: (e[0], e[1])):
                open_now += change
                self.assertLessEqual(open_now, config.MAX_OPEN_POSITIONS)

    def test_portfolio_chain_and_reports(self):
        state = portfolio.new_state()
        results = []
        for day in self.replay_days:
            result = engine.run_day(day, self.bars5, self.daily, portfolio.start_capital_for(state, day))
            portfolio.record_day(state, result)
            results.append(result)
        summary = portfolio.summarize(state)
        self.assertEqual(summary["sessions"], len(self.replay_days))
        self.assertEqual(summary["n_trades"], sum(r["n_trades"] for r in results))
        self.assertAlmostEqual(state["capital"], 100_000.0 + sum(r["net_pnl"] for r in results), places=2)

        busiest = max(results, key=lambda r: r["n_trades"])
        daily_html = report.build_daily_html(busiest, state)
        self.assertIn("<svg", daily_html)
        for t in busiest["trades"]:
            self.assertIn(t["symbol"], daily_html)
        self.assertIn(config.DISCLAIMER, daily_html)
        backtest_html = report.build_backtest_html(results, state)
        self.assertIn("<svg", backtest_html)
        self.assertIn(config.DISCLAIMER, backtest_html)


if __name__ == "__main__":
    unittest.main()
