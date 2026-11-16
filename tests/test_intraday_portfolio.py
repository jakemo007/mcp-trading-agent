"""Offline tests for intraday_trader.portfolio: sizing, charges and the persistent state."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from intraday_trader import config, portfolio


def day_result(day, net, trades=()):
    trades = list(trades)
    gross = net + 25.0 * len(trades)
    return {
        "date": day, "gross_pnl": gross, "charges": 25.0 * len(trades), "net_pnl": net,
        "n_trades": len(trades), "wins": sum(1 for t in trades if t["net_pnl"] > 0),
        "losses": sum(1 for t in trades if t["net_pnl"] <= 0), "partial": False, "trades": trades,
    }


def trade(day, symbol, net, r=1.0):
    return {"id": "{}-{}-1".format(day, symbol), "date": day, "symbol": symbol, "net_pnl": net,
            "r_multiple": r, "exit_time": day + "T12:00:00+05:30", "chart": {"bars": [[1, 2, 3, 4, 5]]}}


class PositionSizeTests(unittest.TestCase):
    def test_guide_long_example(self):
        qty, why = portfolio.position_size(612.10, 609.65, 100_000, 100_000)
        self.assertEqual((qty, why), (49, None))
        self.assertAlmostEqual(qty * 612.10, 29_992.90, places=2)
        self.assertAlmostEqual(qty * 612.10 / config.LEVERAGE, 9_997.63, places=2)

    def test_guide_short_example(self):
        self.assertEqual(portfolio.position_size(1482.80, 1487.94, 100_000, 100_000), (20, None))

    def test_never_exceeds_the_exposure_cap(self):
        for price in (49.95, 143.21, 612.10, 2999.0, 29_999.0):
            qty, _ = portfolio.position_size(price, price * 0.995, 100_000, 100_000)
            self.assertLessEqual(qty * price, config.MAX_MARGIN_PER_TRADE * config.LEVERAGE + 1e-6)
            self.assertGreater((qty + 1) * price, config.MAX_MARGIN_PER_TRADE * config.LEVERAGE)

    def test_risk_cap_binds_when_the_stop_is_wide(self):
        # 1% of 100,000 = 1,000 of risk; 10 per share -> 100 shares, not the 300 the margin allows
        self.assertEqual(portfolio.position_size(100.0, 90.0, 100_000, 100_000), (100, None))

    def test_free_cash_limits_the_size(self):
        self.assertEqual(portfolio.position_size(612.10, 609.65, 3_000, 100_000), (14, None))

    def test_refusals(self):
        self.assertEqual(portfolio.position_size(612.10, 609.65, 0, 100_000), (0, "no free margin"))
        self.assertEqual(portfolio.position_size(612.10, 609.65, -50, 100_000), (0, "no free margin"))
        self.assertEqual(portfolio.position_size(31_000.0, 30_900.0, 100_000, 100_000),
                         (0, "share price above the per-trade limit"))
        self.assertEqual(portfolio.position_size(20_000.0, 19_950.0, 5_000, 100_000), (0, "no free margin"))
        self.assertEqual(portfolio.position_size(0.0, 1.0, 100_000, 100_000), (0, "invalid prices"))
        self.assertEqual(portfolio.position_size(100.0, 100.0, 100_000, 100_000), (0, "invalid prices"))


class TradePnlTests(unittest.TestCase):
    def test_long_win_and_loss(self):
        self.assertEqual(portfolio.trade_pnl("LONG", 612.10, 618.90, 49), (333.20, 25.0, 308.20))
        self.assertEqual(portfolio.trade_pnl("LONG", 612.10, 609.65, 49), (-120.05, 25.0, -145.05))

    def test_short_win_and_loss(self):
        self.assertEqual(portfolio.trade_pnl("SHORT", 1482.80, 1474.30, 20), (170.0, 25.0, 145.0))
        self.assertEqual(portfolio.trade_pnl("SHORT", 1482.80, 1487.94, 20), (-102.8, 25.0, -127.8))

    def test_charge_is_flat_per_trade(self):
        for qty in (1, 49, 500):
            self.assertEqual(portfolio.trade_pnl("LONG", 100.0, 100.0, qty), (0.0, config.CHARGE_PER_TRADE,
                                                                               -config.CHARGE_PER_TRADE))


class StateTests(unittest.TestCase):
    def test_new_state(self):
        state = portfolio.new_state()
        self.assertEqual(state["initial_capital"], config.INITIAL_CAPITAL)
        self.assertEqual(state["capital"], config.INITIAL_CAPITAL)
        self.assertEqual((state["days"], state["trades"]), ([], []))

    def test_record_day_chains_capital(self):
        state = portfolio.new_state()
        portfolio.record_day(state, day_result("2026-11-12", 300.0, [trade("2026-11-12", "ABC", 300.0)]))
        portfolio.record_day(state, day_result("2026-11-13", -145.05, [trade("2026-11-13", "XYZ", -145.05)]))
        self.assertEqual([d["start_capital"] for d in state["days"]], [100_000.0, 100_300.0])
        self.assertEqual([d["end_capital"] for d in state["days"]], [100_300.0, 100_154.95])
        self.assertEqual(state["capital"], 100_154.95)
        self.assertEqual(len(state["trades"]), 2)

    def test_record_day_is_idempotent_and_rechains_later_days(self):
        state = portfolio.new_state()
        portfolio.record_day(state, day_result("2026-11-12", 300.0, [trade("2026-11-12", "ABC", 300.0)]))
        portfolio.record_day(state, day_result("2026-11-13", 100.0, [trade("2026-11-13", "XYZ", 100.0)]))
        # Replay the first day with a different outcome
        portfolio.record_day(state, day_result("2026-11-12", -200.0, [trade("2026-11-12", "DEF", -200.0)]))
        self.assertEqual([d["date"] for d in state["days"]], ["2026-11-12", "2026-11-13"])
        self.assertEqual(state["days"][0]["net_pnl"], -200.0)
        self.assertEqual(state["days"][1]["start_capital"], 99_800.0)
        self.assertEqual(state["days"][1]["end_capital"], 99_900.0)
        self.assertEqual(state["capital"], 99_900.0)
        self.assertEqual([t["symbol"] for t in state["trades"]], ["DEF", "XYZ"])

    def test_days_are_sorted_when_recorded_out_of_order(self):
        state = portfolio.new_state()
        portfolio.record_day(state, day_result("2026-11-13", 100.0))
        portfolio.record_day(state, day_result("2026-11-11", 50.0))
        self.assertEqual([d["date"] for d in state["days"]], ["2026-11-11", "2026-11-13"])
        self.assertEqual(state["days"][1]["start_capital"], 100_050.0)

    def test_chart_bars_are_not_stored(self):
        state = portfolio.new_state()
        result = day_result("2026-11-13", 100.0, [trade("2026-11-13", "ABC", 100.0)])
        portfolio.record_day(state, result)
        self.assertNotIn("chart", state["trades"][0])
        self.assertIn("chart", result["trades"][0])      # the caller's copy is untouched

    def test_start_capital_for(self):
        state = portfolio.new_state()
        portfolio.record_day(state, day_result("2026-11-12", 300.0))
        portfolio.record_day(state, day_result("2026-11-13", -100.0))
        self.assertEqual(portfolio.start_capital_for(state, date(2026, 11, 12)), 100_000.0)
        self.assertEqual(portfolio.start_capital_for(state, date(2026, 11, 13)), 100_300.0)
        self.assertEqual(portfolio.start_capital_for(state, date(2026, 11, 16)), 100_200.0)

    def test_save_and_load_round_trip(self):
        state = portfolio.new_state()
        portfolio.record_day(state, day_result("2026-11-13", 308.20, [trade("2026-11-13", "ABC", 308.20)]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "intraday_portfolio.json"
            portfolio.save_state(state, path)
            self.assertEqual(portfolio.load_state(path), state)
            self.assertEqual([p.name for p in path.parent.iterdir()], ["intraday_portfolio.json"])

    def test_load_missing_file_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.json"
            state = portfolio.load_state(path)
            self.assertEqual(state["capital"], config.INITIAL_CAPITAL)
            self.assertFalse(path.exists())


class SummarizeTests(unittest.TestCase):
    def test_empty(self):
        s = portfolio.summarize(portfolio.new_state())
        self.assertEqual((s["sessions"], s["n_trades"], s["wins"], s["losses"]), (0, 0, 0, 0))
        self.assertEqual((s["net_pnl"], s["win_rate_pct"], s["max_drawdown_inr"]), (0.0, 0.0, 0.0))
        self.assertEqual(s["capital"], config.INITIAL_CAPITAL)
        self.assertIsNone(s["profit_factor"])
        self.assertIsNone(s["best_day"])
        self.assertEqual(s["equity"], [])

    def test_totals_and_drawdown(self):
        state = portfolio.new_state()
        portfolio.record_day(state, day_result("2026-11-10", 300.0, [trade("2026-11-10", "A", 300.0, 2.0)]))
        portfolio.record_day(state, day_result("2026-11-11", -500.0, [trade("2026-11-11", "B", -200.0, -1.2),
                                                                         trade("2026-11-11", "C", -300.0, -1.3)]))
        portfolio.record_day(state, day_result("2026-11-12", 100.0, [trade("2026-11-12", "D", 100.0, 0.5)]))
        s = portfolio.summarize(state)
        self.assertEqual((s["sessions"], s["n_trades"], s["wins"], s["losses"]), (3, 4, 2, 2))
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertEqual(s["net_pnl"], -100.0)
        self.assertEqual(s["net_pnl_pct"], -0.1)
        self.assertEqual(s["charges"], 100.0)
        self.assertEqual(s["capital"], 99_900.0)
        self.assertEqual((s["avg_win"], s["avg_loss"]), (200.0, -250.0))
        self.assertEqual(s["profit_factor"], 0.8)
        self.assertEqual(s["expectancy_inr"], -25.0)
        self.assertEqual(s["avg_r"], 0.0)
        self.assertEqual(s["max_drawdown_inr"], 500.0)          # 100,300 -> 99,800
        self.assertEqual(s["max_drawdown_pct"], 0.5)
        self.assertEqual(s["best_day"], {"date": "2026-11-10", "net_pnl": 300.0})
        self.assertEqual(s["worst_day"], {"date": "2026-11-11", "net_pnl": -500.0})
        self.assertEqual([p["capital"] for p in s["equity"]], [100_300.0, 99_800.0, 99_900.0])


if __name__ == "__main__":
    unittest.main()
