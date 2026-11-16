"""Tests for intraday_trader/report.py. No network, no market data.

Run from the repo root:
    python -m unittest discover -s tests -p "test_intraday_report.py"

The fixture is the learning guide's two worked examples (the ABC long that hits
its target for +308.20 and the XYZ short squared off for +145.00) plus one
losing trade, as a DayResult and the PortfolioState after it.
"""
from __future__ import annotations

import copy
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from intraday_trader import config, portfolio, report

DAY = "2026-11-13"
SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'


# ── Fixture ───────────────────────────────────────────────────────────────────

def _iso(hhmm: str, day: str = DAY) -> str:
    return "{}T{}:00+05:30".format(day, hhmm)


def _clock(start: str, count: int):
    h, m = [int(x) for x in start.split(":")]
    total = h * 60 + m
    return ["{:02d}:{:02d}".format((total + 5 * k) // 60, (total + 5 * k) % 60) for k in range(count)]


def _path(start: str, closes, wiggle: float, overrides=None):
    """Bars [[iso, o, h, l, c]] walking through `closes`; `overrides` = {hhmm: (o, h, l, c)}."""
    overrides = overrides or {}
    bars = []
    prev = closes[0] + 0.4 * (1 if len(closes) > 1 and closes[1] < closes[0] else -1)
    for k, (hhmm, close) in enumerate(zip(_clock(start, len(closes)), closes)):
        if hhmm in overrides:
            o, h, l, c = overrides[hhmm]
        else:
            o, c = prev, close
            h = max(o, c) + wiggle * (1 + (k % 3) * 0.5)
            l = min(o, c) - wiggle * (1 + ((k + 1) % 3) * 0.5)
        bars.append([_iso(hhmm), round(o, 2), round(h, 2), round(l, 2), round(c, 2)])
        prev = c
    return bars


def _abc_bars():
    closes = [613.30, 612.90, 612.60, 612.10, 611.80, 611.40, 611.50, 611.80, 611.70, 611.20, 610.90, 610.80,
              610.90, 611.50, 613.30, 613.90, 613.10, 612.80, 613.70]
    rally = [613.90 + (618.40 - 613.90) * k / 20.0 + (0.25 if k % 2 else -0.15) for k in range(1, 21)]
    after = [618.80, 618.50, 618.90, 618.60, 618.30, 618.55, 618.20]
    overrides = {
        "10:05": (611.80, 612.20, 611.50, 611.70),
        "10:25": (610.80, 611.00, 609.95, 610.90),
        "10:30": (610.90, 611.60, 610.70, 611.50),
        "10:35": (611.50, 613.40, 611.40, 613.30),
        "10:40": (613.30, 614.10, 612.60, 613.90),
        "10:45": (613.90, 614.00, 612.90, 613.10),
        "10:50": (613.10, 613.30, 612.00, 612.80),
        "12:40": (618.40, 619.10, 618.20, 618.80),
    }
    return _path("09:25", closes + rally + after, 0.18, overrides)


def _xyz_bars():
    before = [1480.2, 1481.0, 1480.4, 1481.6, 1482.3, 1481.8, 1482.9, 1483.4, 1482.8, 1483.9, 1484.6, 1485.2]
    setup = [1485.4, 1484.2, 1481.6, 1480.2, 1481.4, 1482.2]
    drift = [1482.2 + (1474.3 - 1482.2) * k / 41.0 + (0.5 if k % 2 else -0.4) for k in range(1, 41)] + [1474.30]
    after = [1474.6, 1474.1, 1474.4]
    overrides = {
        "11:20": (1485.20, 1487.20, 1484.90, 1485.40),
        "11:25": (1485.40, 1486.10, 1483.50, 1484.20),
        "11:30": (1484.00, 1484.00, 1481.20, 1481.60),
        "11:35": (1481.60, 1482.10, 1479.60, 1480.20),
        "11:45": (1481.40, 1483.00, 1481.10, 1482.20),
    }
    return _path("10:20", before + setup + drift + after, 0.35, overrides)


def _lmn_bars():
    before = [246.10, 246.00, 245.90, 246.05, 245.80, 245.70, 245.75, 245.60, 245.50, 245.40, 245.30, 245.20]
    setup = [245.10, 245.15, 245.75, 245.95, 245.80, 245.60, 245.20, 244.80, 244.50]
    after = [244.40, 244.55, 244.30, 244.45, 244.35, 244.25]
    overrides = {
        "12:40": (245.20, 245.25, 244.70, 245.10),
        "12:45": (245.10, 245.30, 245.00, 245.15),
        "12:50": (245.15, 245.80, 245.10, 245.75),
        "12:55": (245.75, 246.05, 245.70, 245.95),
        "13:05": (245.80, 245.85, 245.40, 245.60),
        "13:20": (244.80, 244.90, 244.40, 244.50),
    }
    return _path("11:40", before + setup + after, 0.06, overrides)


def _read(tf: str, bias: str, reason: str) -> dict:
    return {"timeframe": tf, "bias": bias, "reason": reason, "last_bos_time": None,
            "last_bos_level": None, "swing_high": None, "swing_low": None}


def _bias(w: str, d: str, h: str, direction, aligned: int, reason: str) -> dict:
    return {
        "weekly": _read("W", w, "Weekly structure: last break was {}.".format(w.lower())),
        "daily": _read("D", d, "Daily structure: last break was {}.".format(d.lower())),
        "hourly": _read("H1", h, "1h structure: last break was {}.".format(h.lower())),
        "direction": direction, "aligned": aligned, "reason": reason,
    }


ABC_BIAS = _bias("BULL", "BULL", "BULL", "LONG", 3, "1h, daily and weekly bullish: longs only")
ABC_BIAS["daily"]["reason"] = "Closed above the 607.50 swing high on 2026-11-10; last break of structure is up."
XYZ_BIAS = _bias("NEUTRAL", "BEAR", "BEAR", "SHORT", 2, "1h and daily bearish, weekly neutral: shorts only")
LMN_BIAS = _bias("BULL", "BULL", "BULL", "LONG", 3, "1h, daily and weekly bullish: longs only")


def abc_trade() -> dict:
    return {
        "id": "2026-11-13-ABC-1", "date": DAY, "symbol": "ABC", "name": "ABC Industries Ltd",
        "direction": "LONG", "contexts": ["SWEEP:PDL"], "target_kind": "PDH",
        "signal_time": _iso("10:40"), "entry_time": _iso("10:50"), "exit_time": _iso("12:40"),
        "entry": 612.10, "stop": 609.65, "target": 618.90, "exit_price": 618.90,
        "qty": 49, "exposure": 29992.90, "margin": 9997.63, "risk_inr": 120.05, "rr_planned": 2.78,
        "exit_reason": "TARGET", "gross_pnl": 333.20, "charges": 25.0, "net_pnl": 308.20,
        "r_multiple": 2.57, "outcome": "WIN", "rvol": 2.30, "bias": ABC_BIAS,
        "story": [
            "10:25 traded down to 609.95, below the previous day low at 610.20, and closed back above it.",
            "10:35 was a displacement candle: body 1.80 against an average of 0.55.",
            "10:35 closed at 613.30, above the 612.20 swing high: structure shifted up.",
            "10:40 left a fair value gap from 611.60 to 612.60.",
            "Buy limit at the midpoint 612.10, stop 609.65, target 618.90 (PDH), reward to risk 2.78.",
            "Filled at 10:50; target hit at 12:40.",
        ],
        "fvg": {"timeframe": "M5", "kind": "BULL", "top": 612.60, "bottom": 611.60, "mid": 612.10,
                "created_time": _iso("10:40"), "known_at": _iso("10:45")},
        "sweep_level": {"name": "PDL", "price": 610.20, "side": "SSL", "time": _iso("11:05", "2026-11-12")},
        "h1_fvg": None,
        "chart": {"bars": _abc_bars()},
    }


def xyz_trade() -> dict:
    return {
        "id": "2026-11-13-XYZ-1", "date": DAY, "symbol": "XYZ", "name": "XYZ Motors Ltd",
        "direction": "SHORT", "contexts": ["H1_FVG"], "target_kind": "PDL",
        "signal_time": _iso("11:35"), "entry_time": _iso("11:45"), "exit_time": _iso("15:10"),
        "entry": 1482.80, "stop": 1487.94, "target": 1468.40, "exit_price": 1474.30,
        "qty": 20, "exposure": 29656.00, "margin": 9885.33, "risk_inr": 102.80, "rr_planned": 2.80,
        "exit_reason": "EOD", "gross_pnl": 170.00, "charges": 25.0, "net_pnl": 145.00,
        "r_multiple": 1.41, "outcome": "WIN", "rvol": 1.84, "bias": XYZ_BIAS,
        "story": [
            "11:20 rallied to 1,487.20, into the bearish 1h gap at 1,486.00 to 1,489.50, without closing above it.",
            "11:30 was a large down candle that closed below the last 5m swing low.",
            "11:35 left a bearish gap from 1,482.10 to 1,483.50.",
            "Sell limit at 1,482.80, stop 1,487.94, target 1,468.40 (PDL).",
            "Filled at 11:45; still open at 15:15 and squared off at 1,474.30.",
        ],
        "fvg": {"timeframe": "M5", "kind": "BEAR", "top": 1483.50, "bottom": 1482.10, "mid": 1482.80,
                "created_time": _iso("11:35"), "known_at": _iso("11:40")},
        "sweep_level": None,
        "h1_fvg": {"timeframe": "H1", "kind": "BEAR", "top": 1489.50, "bottom": 1486.00, "mid": 1487.75,
                   "created_time": _iso("13:15", "2026-11-12"), "known_at": _iso("14:15", "2026-11-12")},
        "chart": {"bars": _xyz_bars()},
    }


def lmn_trade() -> dict:
    return {
        "id": "2026-11-13-LMN-1", "date": DAY, "symbol": "LMN", "name": "L&M <Test> Ltd",
        "direction": "LONG", "contexts": ["SWEEP:ORL", "H1_FVG"], "target_kind": "FIXED_2R",
        "signal_time": _iso("12:55"), "entry_time": _iso("13:05"), "exit_time": _iso("13:20"),
        "entry": 245.50, "stop": 244.60, "target": 247.30, "exit_price": 244.60,
        "qty": 122, "exposure": 29951.00, "margin": 9983.67, "risk_inr": 109.80, "rr_planned": 2.00,
        "exit_reason": "STOP", "gross_pnl": -109.80, "charges": 25.0, "net_pnl": -134.80,
        "r_multiple": -1.23, "outcome": "LOSS", "rvol": 3.05, "bias": LMN_BIAS,
        "story": [
            "12:40 dipped to 244.70, below the opening range low 244.90 & into the 1h gap <244.70 to 245.20>.",
            "12:50 displaced upward and 12:55 left a gap from 245.30 to 245.70.",
            "Filled at 245.50 at 13:05; stopped at 244.60 at 13:20.",
        ],
        "fvg": {"timeframe": "M5", "kind": "BULL", "top": 245.70, "bottom": 245.30, "mid": 245.50,
                "created_time": _iso("12:55"), "known_at": _iso("13:00")},
        "sweep_level": {"name": "ORL", "price": 244.90, "side": "SSL", "time": _iso("09:40")},
        "h1_fvg": {"timeframe": "H1", "kind": "BULL", "top": 245.20, "bottom": 244.70, "mid": 244.95,
                   "created_time": _iso("14:15", "2026-11-12"), "known_at": _iso("15:15", "2026-11-12")},
        "chart": {"bars": _lmn_bars()},
    }


def _watch(symbol, name, at, rank, rvol, turnover, change, bias, status, note) -> dict:
    return {"symbol": symbol, "name": name, "eligible_from": at, "rank": rank, "rvol": rvol,
            "cum_volume": 1840000, "avg_cum_volume": 800000.0, "turnover_cr": turnover,
            "last_price": 612.4, "prev_close": 615.0, "day_change_pct": change,
            "bias": bias, "status": status, "note": note}


def _signal(symbol, hhmm, direction, contexts, entry, stop, target, rr, status, skip_reason=None) -> dict:
    return {"symbol": symbol, "direction": direction, "signal_time": _iso(hhmm), "contexts": contexts,
            "context_time": _iso(hhmm), "entry": entry, "stop": stop, "target": target, "target_kind": "PDH",
            "rr": rr, "status": status, "skip_reason": skip_reason, "rvol": 2.0}


def day_result() -> dict:
    trades = [abc_trade(), lmn_trade(), xyz_trade()]
    return {
        "date": DAY, "generated_at": "2026-11-13T16:02:11+05:30", "partial": False,
        "universe_size": 213, "scanned": 209,
        "start_capital": 99916.60, "end_capital": 100235.00,
        "gross_pnl": 393.40, "charges": 75.0, "net_pnl": 318.40,
        "n_trades": 3, "wins": 2, "losses": 1,
        "watchlist": [
            _watch("ABC", "ABC Industries Ltd", "10:15", 1, 2.30, 112.6, -0.42, ABC_BIAS, "TRADED",
                   "Swept the previous day low and hit the target."),
            _watch("LMN", "L&M <Test> Ltd", "10:15", 2, 3.05, 18.4, 1.10, LMN_BIAS, "TRADED", "Stopped out."),
            _watch("XYZ", "XYZ Motors Ltd", "11:15", 1, 1.84, 64.2, -1.35, XYZ_BIAS, "TRADED", "Squared off at 15:15."),
            _watch("DEF", "DEF Bank Ltd", "10:15", 3, 1.92, 41.0, 0.65, LMN_BIAS, "SIGNAL_NOT_FILLED",
                   "One setup, price never came back to the entry."),
            _watch("MNO", "MNO Pharma Ltd", "12:15", 2, 1.71, 22.9, -0.80, XYZ_BIAS, "SIGNAL_NOT_FILLED",
                   "One setup, the gap failed before the fill."),
            _watch("RST", "RST Power Ltd", "13:15", 1, 2.64, 9.3, 2.05, LMN_BIAS, "SIGNAL_NOT_FILLED",
                   "Would have filled but the trade was refused."),
            _watch("GHI", "GHI Steel Ltd", "10:15", 4, 1.66, 7.7, 0.12, LMN_BIAS, "NO_SETUP",
                   "Aligned for longs but no 5m setup formed."),
            _watch("JKL", "JKL Cement Ltd", "11:15", 3, 1.58, 12.1, -0.05,
                   _bias("BEAR", "BULL", "BEAR", None, 0, "1h and daily disagree: stand aside"),
                   "NOT_ALIGNED", "1h bearish while the daily is bullish."),
        ],
        "signals": [
            _signal("ABC", "10:40", "LONG", ["SWEEP:PDL"], 612.10, 609.65, 618.90, 2.78, "FILLED"),
            _signal("DEF", "11:05", "LONG", ["H1_FVG"], 884.40, 881.20, 892.60, 2.56, "EXPIRED"),
            _signal("XYZ", "11:35", "SHORT", ["H1_FVG"], 1482.80, 1487.94, 1468.40, 2.80, "FILLED"),
            _signal("MNO", "12:30", "SHORT", ["SWEEP:PDH"], 1210.50, 1215.10, 1199.00, 2.50, "INVALIDATED"),
            _signal("LMN", "12:55", "LONG", ["SWEEP:ORL", "H1_FVG"], 245.50, 244.60, 247.30, 2.00, "FILLED"),
            _signal("RST", "13:40", "LONG", ["SWEEP:M5_SL"], 318.20, 317.10, 321.50, 3.00, "SKIPPED",
                    "daily trade cap reached"),
        ],
        "trades": trades,
        "notes": ["Entries stopped at 14:30 as planned."],
        "config": config.snapshot(),
    }


def _old_trade(tid, day, symbol, direction, net, gross, reason, r) -> dict:
    return {"id": tid, "date": day, "symbol": symbol, "name": symbol + " Ltd", "direction": direction,
            "contexts": ["SWEEP:PDL"] if direction == "LONG" else ["SWEEP:PDH"], "target_kind": "PDH",
            "signal_time": _iso("10:40", day), "entry_time": _iso("10:55", day), "exit_time": _iso("12:10", day),
            "entry": 500.0, "stop": 498.0, "target": 505.0, "exit_price": 505.0 if net > 0 else 498.0,
            "qty": 60, "exposure": 30000.0, "margin": 10000.0, "risk_inr": 120.0, "rr_planned": 2.5,
            "exit_reason": reason, "gross_pnl": gross, "charges": 25.0, "net_pnl": net, "r_multiple": r,
            "outcome": "WIN" if net > 0 else "LOSS"}


def portfolio_state() -> dict:
    today = day_result()
    trades = [
        _old_trade("2026-11-11-AAA-1", "2026-11-11", "AAA", "LONG", 210.40, 235.40, "TARGET", 1.75),
        _old_trade("2026-11-11-BBB-1", "2026-11-11", "BBB", "SHORT", -150.20, -125.20, "STOP", -1.25),
        _old_trade("2026-11-12-CCC-1", "2026-11-12", "CCC", "LONG", -143.60, -118.60, "STOP", -1.20),
    ] + [{k: v for k, v in t.items() if k != "chart"} for t in today["trades"]]
    return {
        "initial_capital": 100000.0, "capital": 100235.00, "updated_at": "2026-11-13T16:02:12+05:30",
        "days": [
            {"date": "2026-11-11", "start_capital": 100000.0, "end_capital": 100060.20, "gross_pnl": 110.20,
             "charges": 50.0, "net_pnl": 60.20, "n_trades": 2, "wins": 1, "losses": 1, "partial": False},
            {"date": "2026-11-12", "start_capital": 100060.20, "end_capital": 99916.60, "gross_pnl": -118.60,
             "charges": 25.0, "net_pnl": -143.60, "n_trades": 1, "wins": 0, "losses": 1, "partial": False},
            {"date": DAY, "start_capital": 99916.60, "end_capital": 100235.00, "gross_pnl": 393.40,
             "charges": 75.0, "net_pnl": 318.40, "n_trades": 3, "wins": 2, "losses": 1, "partial": False},
        ],
        "trades": trades,
    }


def backtest_results() -> list:
    state = portfolio_state()
    out = []
    for day in state["days"][:2]:
        trades = [t for t in state["trades"] if t["date"] == day["date"]]
        out.append({"date": day["date"], "generated_at": "2026-11-13T16:05:00+05:30", "partial": False,
                    "universe_size": 213, "scanned": 209, "start_capital": day["start_capital"],
                    "end_capital": day["end_capital"], "gross_pnl": day["gross_pnl"], "charges": day["charges"],
                    "net_pnl": day["net_pnl"], "n_trades": day["n_trades"], "wins": day["wins"],
                    "losses": day["losses"], "watchlist": [], "signals": [], "trades": trades, "notes": [],
                    "config": config.snapshot()})
    out.append(day_result())
    return out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reference_summarize(state: dict) -> dict:
    """Used only if portfolio.summarize is still a stub when these tests run."""
    days = sorted(state.get("days", []), key=lambda d: d.get("date", ""))
    trades = state.get("trades", [])
    nets = [float(t.get("net_pnl", 0)) for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    initial = float(state.get("initial_capital", 100000.0))
    net = sum(float(d.get("net_pnl", 0)) for d in days)
    return {
        "sessions": len(days), "n_trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "gross_pnl": sum(float(d.get("gross_pnl", 0)) for d in days),
        "charges": sum(float(d.get("charges", 0)) for d in days), "net_pnl": net,
        "net_pnl_pct": net / initial * 100.0 if initial else 0.0, "capital": initial + net,
        "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else None,
        "expectancy_inr": net / len(trades) if trades else 0.0, "avg_r": 0.0,
        "max_drawdown_inr": 0.0, "max_drawdown_pct": 0.0, "best_day": None, "worst_day": None,
        "equity": [{"date": d.get("date"), "capital": d.get("end_capital")} for d in days],
    }


_PATCH = None


def setUpModule():
    global _PATCH
    try:
        portfolio.summarize(portfolio_state())
    except NotImplementedError:
        _PATCH = mock.patch.object(portfolio, "summarize", _reference_summarize)
        _PATCH.start()


def tearDownModule():
    if _PATCH is not None:
        _PATCH.stop()


VOID = {"meta", "br", "hr", "img", "input", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _Balance(HTMLParser):
    """Records every tag that is closed out of order or left open."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append("unexpected </{}> with open {}".format(tag, self.stack[-3:]))
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


def _check_html(case: unittest.TestCase, text: str) -> _Balance:
    parser = _Balance()
    parser.feed(text)
    parser.close()
    case.assertEqual(parser.errors, [], "mismatched tags")
    case.assertEqual(parser.stack, [], "unclosed tags")
    return parser


def _svgs(text: str) -> list:
    return re.findall(r"<svg\b.*?</svg>", text, flags=re.S)


def _viewbox(svg: str):
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    return float(m.group(1)), float(m.group(2))


def _check_geometry(case: unittest.TestCase, svg: str):
    """Every coordinate is a finite number inside the viewBox."""
    case.assertNotRegex(svg.lower(), r'"[^"]*\b(nan|inf)\b[^"]*"')
    w, h = _viewbox(svg)
    tol = 0.6
    for attr, limit in (("x", w), ("x1", w), ("x2", w), ("cx", w), ("y", h), ("y1", h), ("y2", h), ("cy", h)):
        for raw in re.findall(r'\s{}="(-?[\d.]+)"'.format(attr), svg):
            value = float(raw)
            case.assertTrue(-tol <= value <= limit + tol, "{}={} outside 0..{}".format(attr, raw, limit))
    for points in re.findall(r'points="([^"]+)"', svg):
        for pair in points.split():
            x, y = [float(v) for v in pair.split(",")]
            case.assertTrue(-tol <= x <= w + tol and -tol <= y <= h + tol, "point {} outside".format(pair))
    for rect in re.findall(r"<rect\b[^>]*>", svg):
        attrs = dict(re.findall(r'(\w+)="(-?[\d.]+)"', rect))
        if {"x", "y", "width", "height"} <= set(attrs):
            case.assertGreaterEqual(float(attrs["width"]), 0)
            case.assertGreaterEqual(float(attrs["height"]), 0)
            case.assertLessEqual(float(attrs["x"]) + float(attrs["width"]), w + tol, rect)
            case.assertLessEqual(float(attrs["y"]) + float(attrs["height"]), h + tol, rect)
    # Left-anchored text must end inside the picture (rough width: 0.6 em per character).
    for x, size, body in re.findall(r'<text x="([\d.]+)" y="[\d.]+" font-size="([\d.]+)"[^>]*>(.*?)</text>', svg):
        plain = re.sub(r"<[^>]+>", "", body)
        case.assertLessEqual(float(x) + len(plain) * float(size) * 0.6, w + 2, "text runs off: " + plain)


def _line_label_ys(svg: str) -> dict:
    out = {}
    for y, name in re.findall(r'<text x="[\d.]+" y="([\d.]+)" font-size="11.5"[^>]*><tspan[^>]*>(\w+)</tspan>', svg):
        out[name] = float(y)
    return out


# ── Tests ─────────────────────────────────────────────────────────────────────

class DailyReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = day_result()
        cls.state = portfolio_state()
        cls.html = report.build_daily_html(cls.result, cls.state)

    def test_parses_without_unclosed_or_crossed_tags(self):
        parser = _check_html(self, self.html)
        names = [t for t, _ in parser.tags]
        self.assertEqual(names.count("html"), 1)
        self.assertEqual(names.count("svg"), 4)   # three trade charts and the equity curve
        self.assertIn("style", names)
        self.assertNotIn("script", names)

    def test_trade_ids_symbols_and_key_numbers_appear(self):
        for needle in (
            "Intraday Paper Trader", "Friday, 13 Nov 2026", "2026-11-13 16:02 IST",
            "2026-11-13-ABC-1", "2026-11-13-XYZ-1", "2026-11-13-LMN-1",
            "ABC Industries Ltd", "XYZ Motors Ltd",
            "+₹308.20", "+₹145.00", "-₹134.80",            # net per trade
            "+₹333.20", "+₹170.00", "-₹109.80",            # gross per trade
            "+2.57R", "+1.41R", "-1.23R",
            "612.10", "609.65", "618.90",                   # ABC entry, stop, target
            "1,482.80", "1,487.94", "1,468.40", "1,474.30",  # XYZ entry, stop, target, exit
            "₹29,992.90", "₹9,997.63", "₹120.05", "2.78",   # ABC size and risk
            "₹100,235.00", "₹99,916.60", "+₹318.40", "+0.32%", "₹75.00",
            "+₹235.00",                                      # net since start
            "2.30x", "3.05x", "₹112.6 cr",
            "213", "209",
            "Daily trade cap reached",
            "PDL 610.20 swept",
            "Entries stopped at 14:30 as planned.",
        ):
            self.assertIn(needle, self.html, needle)

    def test_every_status_is_worded(self):
        for words in ("Traded", "Signal, not filled", "No setup", "Not aligned", "Expired", "Invalidated", "Skipped"):
            self.assertIn(">{}<".format(words), self.html)
        self.assertIn("Stand aside", self.html)

    def test_outcomes_and_directions_carry_words_and_arrows(self):
        self.assertIn("&#9650;</span> WIN", self.html)
        self.assertIn("&#9660;</span> LOSS", self.html)
        self.assertIn("&#9650;</span> LONG", self.html)
        self.assertIn("&#9660;</span> SHORT", self.html)
        self.assertIn("&#9650;</span> Bull", self.html)
        self.assertIn("&#9660;</span> Bear", self.html)

    def test_text_is_escaped(self):
        self.assertIn("L&amp;M &lt;Test&gt; Ltd", self.html)
        self.assertNotIn("<Test>", self.html)
        self.assertIn("&amp; into the 1h gap &lt;244.70 to 245.20&gt;", self.html)
        result = day_result()
        result["trades"][0]["story"] = ['<script>alert("x")</script>']
        result["watchlist"][0]["note"] = "<img src=x onerror=alert(1)>"
        result["notes"] = ["<b>bold</b>"]
        text = report.build_daily_html(result, self.state)
        self.assertNotIn("<script", text)
        self.assertNotIn("<img", text)
        self.assertNotIn("<b>bold</b>", text)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", text)

    def test_self_contained(self):
        self.assertNotRegex(self.html, r"<(script|link|img|iframe|object|embed)\b")
        self.assertNotRegex(self.html, r"@import|url\(")
        self.assertNotRegex(self.html, r'\bsrc\s*=')
        self.assertNotRegex(self.html, r'\bhref\s*=')          # the daily report links to nothing
        without_ns = self.html.replace(SVG_NS, "")
        self.assertNotIn("http://", without_ns)
        self.assertNotIn("https://", without_ns)

    def test_sections_come_in_the_documented_order(self):
        order = ["<h1>Intraday Paper Trader</h1>", "Capital at close", "<h2>How today was traded</h2>",
                 "<h2>Trades</h2>", "<h2>Stocks in play</h2>", "<h2>Signals not traded</h2>",
                 "<h2>Equity curve since inception</h2>", "Last 20 sessions", "<h2>Rules in force</h2>",
                 config.DISCLAIMER]
        positions = [self.html.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))

    def test_trade_card_reads_as_header_reads_story_chart_money(self):
        start = self.html.index("2026-11-13-ABC-1")
        card = self.html[self.html.rindex("<article", 0, start):start]
        order = ['class="trade-head"', "Top-down read", "What happened, in order", "The 5-minute chart", "The money"]
        positions = [card.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("3/3 aligned", card)
        self.assertIn("Closed above the 607.50 swing high on 2026-11-10", card)
        self.assertEqual(card.count("<li>"), 6)

    def test_untaken_signals_only(self):
        section = self.html[self.html.index("<h2>Signals not traded</h2>"):self.html.index("<h2>Equity curve")]
        self.assertEqual(section.count("<tr>"), 1 + 3)
        for symbol in ("DEF", "MNO", "RST"):
            self.assertIn("<b>{}</b>".format(symbol), section)
        self.assertNotIn("<b>ABC</b>", section)

    def test_signals_section_omitted_when_all_filled_or_none(self):
        result = day_result()
        result["signals"] = [s for s in result["signals"] if s["status"] == "FILLED"]
        self.assertNotIn("Signals not traded", report.build_daily_html(result, self.state))
        result["signals"] = []
        self.assertNotIn("Signals not traded", report.build_daily_html(result, self.state))

    def test_no_em_dashes_in_report_text(self):
        self.assertNotIn("\u2014", self.html)
        self.assertNotIn("\u2013", self.html)

    def test_empty_trades(self):
        result = day_result()
        result.update(trades=[], n_trades=0, wins=0, losses=0, gross_pnl=0.0, charges=0.0, net_pnl=0.0,
                      end_capital=result["start_capital"])
        text = report.build_daily_html(result, self.state)
        _check_html(self, text)
        self.assertIn("No trades today", text)
        self.assertIn("6 setups appeared but none became a trade", text)

    def test_empty_watchlist_and_signals(self):
        result = day_result()
        result.update(trades=[], watchlist=[], signals=[], n_trades=0, wins=0, losses=0, net_pnl=0.0)
        text = report.build_daily_html(result, self.state)
        _check_html(self, text)
        self.assertIn("No stock passed the volume scan, so there was nothing to trade.", text)
        self.assertIn("No stock passed the volume scan today.", text)

    def test_watchlist_without_signals(self):
        result = day_result()
        result.update(trades=[], signals=[], n_trades=0)
        text = report.build_daily_html(result, self.state)
        self.assertIn("8 stocks were in play, but none completed a valid 5-minute setup", text)

    def test_empty_state(self):
        for state in ({}, None, {"initial_capital": 100000.0, "capital": 100000.0, "days": [], "trades": []}):
            text = report.build_daily_html(self.result, state)
            _check_html(self, text)
            self.assertIn("No sessions recorded yet", text)

    def test_partial_day_banner(self):
        result = day_result()
        self.assertNotIn("Partial session", self.html)
        result["partial"] = True
        text = report.build_daily_html(result, self.state)
        _check_html(self, text)
        self.assertIn("Partial session", text)

    def test_nothing_at_all(self):
        for result in ({}, None, {"date": DAY}, {"trades": None, "watchlist": None, "signals": None, "config": None}):
            text = report.build_daily_html(result, {})
            _check_html(self, text)
            self.assertIn("No trades today", text)

    def test_trade_missing_optional_keys_still_renders(self):
        result = day_result()
        result["trades"] = [
            {"symbol": "BARE", "net_pnl": 12.5},
            {"symbol": "NONE", "direction": "SHORT", "bias": None, "story": None, "chart": None, "fvg": None,
             "sweep_level": None, "h1_fvg": None, "contexts": None, "entry": None, "stop": None, "target": None,
             "net_pnl": None, "outcome": None, "exit_reason": None},
            {"symbol": "ODD", "entry": "not a number", "net_pnl": float("nan"), "qty": "x",
             "chart": {"bars": [["bad"], None, [_iso("10:15"), 1, 2, 0.5, 1.5]]}, "bias": {"weekly": "oops"}},
        ]
        text = report.build_daily_html(result, self.state)
        _check_html(self, text)
        for needle in ("BARE", "NONE", "ODD", "+₹12.50", "No chart candles were recorded",
                       "No step-by-step story was recorded", "reads were not recorded"):
            self.assertIn(needle, text)
        self.assertNotIn("nan", text.lower().replace("financial", ""))

    def test_inputs_are_not_modified(self):
        result, state = day_result(), portfolio_state()
        before = (copy.deepcopy(result), copy.deepcopy(state))
        report.build_daily_html(result, state)
        self.assertEqual((result, state), before)


class TradeChartTests(unittest.TestCase):
    def test_returns_empty_string_without_bars(self):
        for trade in ({}, None, {"chart": None}, {"chart": {}}, {"chart": {"bars": []}},
                      {"chart": {"bars": [["x", "a", "b", "c", "d"], [1, 2]]}}):
            self.assertEqual(report.trade_chart_svg(trade), "")

    def test_structure(self):
        svg = report.trade_chart_svg(abc_trade())
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
        self.assertIn('viewBox="0 0 760 320"', svg)
        self.assertIn('width="100%"', svg)
        self.assertIn("<title>ABC long trade on 5-minute candles</title>", svg)
        self.assertIn("<desc>Entry 612.10, stop 609.65, target 618.90, exit 618.90 (Target hit).</desc>", svg)
        self.assertIn('role="img"', svg)
        bars = len(abc_trade()["chart"]["bars"])
        self.assertEqual(len(re.findall(r"<g><title>\d\d:\d\d  O ", svg)), bars)
        self.assertIn("10:35  O 611.50  H 613.40  L 611.40  C 613.30  (up)", svg)
        self.assertIn("10:45  O 613.90  H 614.00  L 612.90  C 613.10  (down)", svg)
        self.assertIn("5m fair value gap 611.60 to 612.60", svg)
        self.assertIn("PDL 610.20 swept", svg)
        self.assertNotIn("1h fair value gap", svg)
        for label in ("sweep", "displacement", "gap complete", "entry", "exit", "5m gap", "swept level",
                      "up candle", "down candle"):
            self.assertIn(">{}</text>".format(label), svg)
        for badge in ("1", "2", "3", "E", "X"):
            self.assertRegex(svg, r'font-weight="700"[^>]*>{}</text>'.format(badge))
        self.assertRegex(svg, r">\d\d:\d\d</text>")
        _check_geometry(self, svg)

    def test_up_candles_are_hollow_and_down_candles_filled(self):
        svg = report.trade_chart_svg(abc_trade())
        up = svg[svg.index("10:35  O 611.50"):]
        up = up[:up.index("</g>")]
        self.assertIn('fill="{}" stroke="{}"'.format(report.SURFACE, report.UP), up)
        down = svg[svg.index("10:45  O 613.90"):]
        down = down[:down.index("</g>")]
        self.assertIn('fill="{}"/>'.format(report.DOWN), down)
        self.assertNotIn('stroke="{}" stroke-width="1.2"'.format(report.DOWN), down)

    def test_short_trade_with_1h_gap_and_eod_exit(self):
        svg = report.trade_chart_svg(xyz_trade())
        self.assertIn("1h fair value gap 1,486.00 to 1,489.50", svg)
        self.assertIn(">tap of 1h gap</text>", svg)
        self.assertIn(">1h gap</text>", svg)
        labels = _line_label_ys(svg)
        self.assertEqual(set(labels), {"Target", "Entry", "Stop", "Exit"})
        self.assertLess(labels["Stop"], labels["Entry"])      # a short: stop above, target below
        self.assertLess(labels["Entry"], labels["Target"])
        _check_geometry(self, svg)

    def test_labels_do_not_overlap_when_prices_are_close(self):
        trade = lmn_trade()                                   # entry, stop and gap within 0.4%
        trade["target"] = 245.62                              # and a target right on top of the entry
        trade["exit_price"] = 245.55
        trade["exit_reason"] = "EOD"
        svg = report.trade_chart_svg(trade)
        ys = sorted(_line_label_ys(svg).values())
        self.assertEqual(len(ys), 4)
        for a, b in zip(ys, ys[1:]):
            self.assertGreaterEqual(b - a, 14.9)
        _check_geometry(self, svg)

    def test_sweep_and_tap_together(self):
        svg = report.trade_chart_svg(lmn_trade())
        self.assertIn(">sweep and tap</text>", svg)
        self.assertIn("ORL 244.90 swept", svg)
        _check_geometry(self, svg)

    def test_large_prices_and_many_bars_stay_inside(self):
        trade = abc_trade()
        scale = 41.6
        trade["chart"]["bars"] = [[b[0]] + [round(v * scale, 2) for v in b[1:]] for b in trade["chart"]["bars"]]
        for key in ("entry", "stop", "target", "exit_price"):
            trade[key] = round(trade[key] * scale, 2)
        for key in ("top", "bottom"):
            trade["fvg"][key] = round(trade["fvg"][key] * scale, 2)
        trade["sweep_level"]["price"] = round(trade["sweep_level"]["price"] * scale, 2)
        _check_geometry(self, report.trade_chart_svg(trade))

        long_day = xyz_trade()
        bars = long_day["chart"]["bars"]
        long_day["chart"]["bars"] = [[_iso(t)] + bars[k % len(bars)][1:] for k, t in enumerate(_clock("09:15", 75))]
        svg = report.trade_chart_svg(long_day)
        self.assertEqual(len(re.findall(r"<g><title>\d\d:\d\d  O ", svg)), 75)
        _check_geometry(self, svg)

    def test_minimal_trade_draws_candles_only(self):
        svg = report.trade_chart_svg({"chart": {"bars": [[_iso("10:15"), 100, 101, 99, 100.5],
                                                         [_iso("10:20"), 100.5, 100.5, 100.5, 100.5]]}})
        self.assertIn("(flat)", svg)
        _check_geometry(self, svg)

    def test_other_sizes(self):
        svg = report.trade_chart_svg(abc_trade(), width=560, height=300)
        self.assertIn('viewBox="0 0 560 300"', svg)
        _check_geometry(self, svg)
        self.assertEqual(report.trade_chart_svg(abc_trade(), width=150, height=60), "")


class EquityCurveTests(unittest.TestCase):
    def test_returns_empty_string_without_days(self):
        for days in ([], None, [{}], [{"date": DAY}]):
            self.assertEqual(report.equity_curve_svg(days, 100000.0), "")

    def test_structure(self):
        days = portfolio_state()["days"]
        svg = report.equity_curve_svg(days, 100000.0)
        self.assertIn('viewBox="0 0 900 280"', svg)
        self.assertIn("<title>Paper capital at the close of each session</title>", svg)
        self.assertIn("3 sessions from 2026-11-11 to 2026-11-13", svg)
        self.assertIn(">₹100,235.00</text>", svg)                 # last value labelled
        self.assertIn(">Start ₹100,000</text>", svg)
        self.assertIn('stroke-dasharray="5 4"', svg)              # the baseline
        self.assertEqual(svg.count("<polyline"), 1)
        self.assertEqual(len(re.findall(r"<g><title>", svg)), 3)  # one mark per day
        self.assertIn("Wednesday, 11 Nov 2026: capital ₹100,060.20, day +₹60.20, 2 trades", svg)
        self.assertIn("Thursday, 12 Nov 2026: capital ₹99,916.60, day -₹143.60, 1 trade", svg)
        marks = svg[svg.index("<g><title>"):svg.rindex("</g>")]
        self.assertEqual(marks.count('fill="{}" stroke'.format(report.UP)), 2)     # two up days
        self.assertEqual(marks.count('fill="{}" stroke'.format(report.DOWN)), 1)   # one down day
        for label in ("day closed up", "day closed down", "flat day"):
            self.assertIn(label, svg)
        _check_geometry(self, svg)

    def test_flat_and_single_day(self):
        svg = report.equity_curve_svg([{"date": DAY, "end_capital": 100000.0, "net_pnl": 0.0, "n_trades": 0}], 100000.0)
        self.assertIn("<circle", svg)
        _check_geometry(self, svg)

    def test_many_sessions_drop_the_per_day_marks(self):
        days = []
        capital = 100000.0
        for k in range(130):
            net = 180.0 if k % 3 else -260.0
            capital += net
            days.append({"date": "2026-{:02d}-{:02d}".format(1 + k // 28, 1 + k % 28), "end_capital": capital,
                         "net_pnl": net, "n_trades": 2})
        svg = report.equity_curve_svg(days, 100000.0)
        self.assertNotIn("<g><title>", svg)
        self.assertNotIn("day closed up", svg)
        _check_geometry(self, svg)
        forty = report.equity_curve_svg(days[:40], 100000.0)
        self.assertEqual(len(re.findall(r"<g><title>", forty)), 40)
        _check_geometry(self, forty)


class BacktestReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = backtest_results()
        cls.state = portfolio_state()
        cls.html = report.build_backtest_html(cls.results, cls.state)

    def test_parses_and_is_self_contained(self):
        _check_html(self, self.html)
        self.assertNotRegex(self.html, r"<(script|link|img|iframe)\b")
        without_ns = self.html.replace(SVG_NS, "")
        self.assertNotIn("http://", without_ns)
        self.assertNotIn("https://", without_ns)
        self.assertNotIn("\u2014", self.html)

    def test_header_and_headline(self):
        for needle in (
            "Intraday Paper Trader: backtest", "Wednesday, 11 Nov 2026 to Friday, 13 Nov 2026", "3 sessions",
            "replay of past sessions", "flat charge of ₹25.00 per trade and no slippage",
            "₹100,235.00", "+₹235.00", "+0.24%", "50.00%", "₹150.00", "3 wins", "3 losses",
        ):
            self.assertIn(needle, self.html, needle)

    def test_sections_in_order(self):
        order = ["<h2>Equity curve</h2>", "<h2>Where the results came from</h2>", "By reason for the trade",
                 "By direction", "By exit", "By hour of entry (IST)", "<h2>Day by day</h2>", "<h2>Every trade</h2>",
                 "<h2>The three best trades</h2>", "<h2>The three worst trades</h2>", "<h2>Rules in force</h2>",
                 config.DISCLAIMER]
        positions = [self.html.index(x) for x in order]
        self.assertEqual(positions, sorted(positions))

    def test_breakdowns(self):
        section = self.html[self.html.index("Where the results came from"):self.html.index("<h2>Day by day</h2>")]
        for label in ("Sweep only", "1h gap tap only", "Sweep and 1h gap", "Long", "Short",
                      "Target hit", "Stop hit", "Square-off at end of day", "10:00 to 10:59", "11:00 to 11:59",
                      "13:00 to 13:59"):
            self.assertIn(">{}<".format(label), section, label)
        # Sweep only: AAA +210.40, BBB -150.20, CCC -143.60, ABC +308.20 -> 4 trades, 2 wins, +224.80
        row = section[section.index(">Sweep only<"):]
        row = row[:row.index("</tr>")]
        self.assertIn(">4<", row)
        self.assertIn(">50.00%<", row)
        self.assertIn("+₹224.80", row)

    def test_trade_log_has_every_trade(self):
        log = self.html[self.html.index("<h2>Every trade</h2>"):self.html.index("<h2>The three best trades</h2>")]
        self.assertEqual(log.count("<tr>"), 1 + 6)
        for symbol in ("AAA", "BBB", "CCC", "ABC", "XYZ", "LMN"):
            self.assertIn("<b>{}</b>".format(symbol), log)

    def test_best_and_worst_cards(self):
        best = self.html[self.html.index("<h2>The three best trades</h2>"):self.html.index("<h2>The three worst trades</h2>")]
        worst = self.html[self.html.index("<h2>The three worst trades</h2>"):self.html.index("<h2>Rules in force</h2>")]
        self.assertEqual(best.count("<article"), 3)
        self.assertEqual(worst.count("<article"), 3)
        self.assertLess(best.index("2026-11-13-ABC-1"), best.index("2026-11-11-AAA-1"))    # +308.20 before +210.40
        self.assertLess(worst.index("2026-11-11-BBB-1"), worst.index("2026-11-12-CCC-1"))  # -150.20 before -143.60
        self.assertEqual(best.count("<svg"), 2)      # ABC and XYZ carry chart bars, AAA does not
        self.assertIn("No chart candles were recorded", best)

    def test_empty_inputs(self):
        for results, state in (([], {}), (None, None), ([], portfolio.new_state()), ([{}], {})):
            text = report.build_backtest_html(results, state)
            _check_html(self, text)
            self.assertIn("The replay took no trades.", text)
            self.assertNotIn("The three best trades", text)

    def test_falls_back_to_state_trades(self):
        text = report.build_backtest_html([], self.state)
        self.assertIn("<b>AAA</b>", text)
        self.assertIn("<b>XYZ</b>", text)


class SaveReportTests(unittest.TestCase):
    def test_writes_utf8_and_creates_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "reports"
            path = report.save_report("<p>₹1</p>", "INTRADAY-DAILY-2026-11-13.html", target)
            self.assertEqual(path, target / "INTRADAY-DAILY-2026-11-13.html")
            self.assertEqual(path.read_bytes(), "<p>₹1</p>".encode("utf-8"))
            again = report.save_report("<p>2</p>", "INTRADAY-DAILY-2026-11-13.html", str(target))
            self.assertEqual(again.read_text(encoding="utf-8"), "<p>2</p>")


if __name__ == "__main__":
    unittest.main()
