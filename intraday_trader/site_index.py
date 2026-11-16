#!/usr/bin/env python3
"""Rebuild index.html for the GitHub Pages reports site.

STANDALONE: standard library only and no imports from this repo, because the
swing workflow copies this single file out of the tree and runs it after
switching to the `reports` branch. Python 3.9+.

Usage
-----
    python intraday_trader/site_index.py SITE_DIR [--portfolio PATH] [--repo OWNER/NAME]

SITE_DIR      root of the checked-out `reports` branch. Reports live in
              SITE_DIR/data/reports/.
--portfolio   path to data/intraday_portfolio.json. When given and readable,
              the page shows the paper portfolio headline (capital, net P&L in
              INR and %, sessions, trades, win rate) and each daily row shows
              that day's trade count and net P&L. A missing or broken file is
              ignored, never an error.
--repo        "owner/name". When given, the page links to the learning guide at
              https://github.com/<owner>/<name>/blob/main/docs/intraday-trader/README.md

Writes SITE_DIR/index.html and SITE_DIR/.nojekyll. The page lists, newest first:
  * Intraday daily reports    data/reports/INTRADAY-DAILY-YYYY-MM-DD.html
                              (files ending -DRYRUN.html are not listed)
  * Intraday backtests        data/reports/INTRADAY-BACKTEST-YYYY-MM-DD.html
  * Swing weekly reports      data/reports/SWING-WEEKLY-YYYY-MM-DD.html
with a prominent "Open latest intraday report" button and, below it, a
secondary link to the latest swing report. A section with no files shows
"No reports yet". Links are relative, so the site works at any base URL.
Same dark look as the existing index (background #0d1117, text #c9d1d9, links
#58a6ff), readable on a phone. Exit code 0 on success, 2 when SITE_DIR does
not exist.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

REPORTS_SUBDIR = ("data", "reports")
REPORTS_URL = "data/reports/"

_DATE = r"(\d{4}-\d{2}-\d{2})"
DAILY_RE = re.compile(r"^INTRADAY-DAILY-" + _DATE + r"\.html$")
BACKTEST_RE = re.compile(r"^INTRADAY-BACKTEST-" + _DATE + r"\.html$")
SWING_RE = re.compile(r"^SWING-WEEKLY-" + _DATE + r"\.html$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")

GUIDE_PATH = "blob/main/docs/intraday-trader/README.md"
DISCLAIMER = (
    "Automated paper-trading simulations for education. "
    "Not financial advice. Trade at your own risk."
)

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#0d1117;color:#c9d1d9;font:16px/1.55 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
.page{max-width:720px;margin:0 auto;padding:36px 16px 56px}
h1{color:#f0f6fc;font-size:1.6rem;line-height:1.25;margin:0 0 6px}
h2{color:#f0f6fc;font-size:1.08rem;margin:36px 0 4px}
p{margin:0 0 10px}
a{color:#58a6ff;text-decoration:none}
a:hover,a:focus{text-decoration:underline}
.sub{color:#8b949e;margin:0 0 22px}
.hint{color:#8b949e;font-size:.88rem;margin:0 0 6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 22px}
.tile{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px;min-width:0}
.tile .label{color:#8b949e;font-size:.78rem}
.tile .value{color:#f0f6fc;font-size:1.2rem;font-weight:650;line-height:1.3;overflow-wrap:anywhere}
.tile .delta{color:#8b949e;font-size:.8rem}
.pos,.tile .value.pos{color:#3fb950}.neg,.tile .value.neg{color:#ff7b72}
.latest{display:inline-block;margin:0 0 10px;padding:11px 18px;background:#238636;color:#fff;border-radius:6px;font-weight:650}
.latest:hover,.latest:focus{background:#2ea043;text-decoration:none}
.second{margin:0 0 4px;font-size:.95rem}
ul{list-style:none;margin:0;padding:0}
li{display:flex;flex-wrap:wrap;gap:2px 14px;align-items:baseline;justify-content:space-between;padding:10px 0;border-bottom:1px solid #21262d}
li .meta{color:#8b949e;font-size:.88rem;font-variant-numeric:tabular-nums;white-space:nowrap}
li .meta b{font-weight:650}
.empty{color:#8b949e;padding:10px 0;border-bottom:1px solid #21262d}
.foot{margin-top:40px;color:#8b949e;font-size:.82rem}
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _money(value: Any, signed: bool = False, decimals: int = 2) -> str:
    v = _num(value)
    if v is None:
        return "-"
    body = "₹{:,.{d}f}".format(abs(v), d=decimals)
    if v < 0 and round(abs(v), decimals) > 0:
        return "-" + body
    if signed and round(v, decimals) > 0:
        return "+" + body
    return body


def _sign_class(value: Any) -> str:
    v = _num(value)
    if v is None or v == 0:
        return ""
    return "pos" if v > 0 else "neg"


def _pretty_date(iso: str, weekday: bool = True) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return d.strftime("%a, %d %b %Y") if weekday else d.strftime("%d %b %Y")


def find_reports(site_dir: str) -> Dict[str, List[Tuple[str, str]]]:
    """{"daily": [(date, filename), ...], "backtest": [...], "swing": [...]}, newest first."""
    found: Dict[str, List[Tuple[str, str]]] = {"daily": [], "backtest": [], "swing": []}
    folder = os.path.join(site_dir, *REPORTS_SUBDIR)
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        if not os.path.isfile(os.path.join(folder, name)):
            continue
        for key, pattern in (("daily", DAILY_RE), ("backtest", BACKTEST_RE), ("swing", SWING_RE)):
            m = pattern.match(name)
            if m:
                found[key].append((m.group(1), name))
                break
    for rows in found.values():
        rows.sort(reverse=True)
    return found


def _days_by_date(portfolio: Optional[dict]) -> Dict[str, dict]:
    if not isinstance(portfolio, dict):
        return {}
    out: Dict[str, dict] = {}
    days = portfolio.get("days")
    for day in days if isinstance(days, list) else []:
        if isinstance(day, dict) and day.get("date"):
            out[str(day["date"])[:10]] = day
    return out


def _headline(portfolio: Optional[dict]) -> str:
    """The paper portfolio in four tiles; "" unless at least one session is recorded."""
    days = _days_by_date(portfolio)
    if not days or not isinstance(portfolio, dict):
        return ""
    initial = _num(portfolio.get("initial_capital"))
    capital = _num(portfolio.get("capital"))
    net = sum(_num(d.get("net_pnl")) or 0.0 for d in days.values())
    if capital is None and initial is not None:
        capital = initial + net
    if capital is not None and initial is not None:
        net = capital - initial
    pct = (net / initial * 100.0) if initial else None
    trades = int(sum(_num(d.get("n_trades")) or 0 for d in days.values()))
    wins = int(sum(_num(d.get("wins")) or 0 for d in days.values()))
    win_rate = "{:.1f}% win rate".format(wins / trades * 100.0) if trades else "no trades yet"
    last_day = max(days)

    def tile(label: str, value: str, delta: str = "", cls: str = "") -> str:
        return '<div class="tile"><div class="label">{}</div><div class="value {}">{}</div>{}</div>'.format(
            _esc(label), cls, _esc(value), '<div class="delta">{}</div>'.format(_esc(delta)) if delta else "")

    tiles = [
        tile("Paper capital", _money(capital), "started at " + _money(initial, decimals=0) if initial is not None else ""),
        tile("Net P&L", _money(net, signed=True), "{:+.2f}%".format(pct) if pct is not None else "", _sign_class(net)),
        tile("Sessions", "{:,d}".format(len(days)), "latest " + _pretty_date(last_day, weekday=False)),
        tile("Trades", "{:,d}".format(trades), "{:,d} won, {}".format(wins, win_rate) if trades else win_rate),
    ]
    return '<section class="tiles" aria-label="Intraday paper portfolio">{}</section>'.format("".join(tiles))


def _section(title: str, hint: str, rows: List[str]) -> str:
    body = "<ul>{}</ul>".format("".join(rows)) if rows else '<p class="empty">No reports yet</p>'
    return "<section><h2>{}</h2><p class=\"hint\">{}</p>{}</section>".format(_esc(title), _esc(hint), body)


def build_index_html(site_dir: str, portfolio: Optional[dict] = None, repo: Optional[str] = None) -> str:
    """Return the full index.html text for the reports found under site_dir."""
    reports = find_reports(site_dir)
    days = _days_by_date(portfolio)

    daily_rows = []
    for iso, name in reports["daily"]:
        meta = ""
        day = days.get(iso)
        if day is not None:
            n = int(_num(day.get("n_trades")) or 0)
            net = day.get("net_pnl")
            meta = '<span class="meta">{} &middot; <b class="{}">{}</b></span>'.format(
                _esc("{:,d} trade{}".format(n, "" if n == 1 else "s")), _sign_class(net), _esc(_money(net, signed=True)))
        daily_rows.append('<li><a href="{}">{}</a>{}</li>'.format(_esc(REPORTS_URL + name), _esc(_pretty_date(iso)), meta))

    backtest_rows = [
        '<li><a href="{}">Backtest run on {}</a></li>'.format(_esc(REPORTS_URL + name), _esc(_pretty_date(iso)))
        for iso, name in reports["backtest"]]
    swing_rows = [
        '<li><a href="{}">Week ending {}</a></li>'.format(_esc(REPORTS_URL + name), _esc(_pretty_date(iso, weekday=False)))
        for iso, name in reports["swing"]]

    parts: List[str] = []
    parts.append("<h1>Paper Trading Reports</h1>")
    parts.append('<p class="sub">NSE intraday and swing strategies, traded on paper and published automatically.</p>')
    parts.append(_headline(portfolio))

    if reports["daily"]:
        iso, name = reports["daily"][0]
        parts.append('<p><a class="latest" href="{}">Open latest intraday report &rarr;</a></p>'.format(_esc(REPORTS_URL + name)))
        parts.append('<p class="second">Latest intraday session: {}</p>'.format(_esc(_pretty_date(iso))))
    if reports["swing"]:
        iso, name = reports["swing"][0]
        parts.append('<p class="second"><a href="{}">Latest swing report: week ending {} &rarr;</a></p>'.format(
            _esc(REPORTS_URL + name), _esc(_pretty_date(iso, weekday=False))))
    if repo and REPO_RE.match(repo):
        parts.append('<p class="second"><a href="https://github.com/{}/{}">How the intraday trader works: the learning guide &rarr;</a></p>'.format(
            _esc(repo), GUIDE_PATH))

    parts.append(_section("Intraday: daily reports", "One report per NSE session, built after the close.", daily_rows))
    parts.append(_section("Intraday: backtests", "Replays of past sessions with a fresh paper portfolio.", backtest_rows))
    parts.append(_section("Swing: weekly reports", "The swing trader's weekly profit and loss.", swing_rows))
    parts.append('<p class="foot">{}</p>'.format(_esc(DISCLAIMER)))

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<title>Paper Trading Reports</title>\n<style>{}</style>\n</head>\n<body>\n<div class=\"page\">\n{}\n</div>\n</body>\n</html>\n"
    ).format(_CSS, "\n".join(p for p in parts if p))


def _load_portfolio(path: Optional[str]) -> Optional[dict]:
    """The portfolio dict, or None when the path is absent, unreadable or not a JSON object."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild index.html for the reports site.")
    parser.add_argument("site_dir", metavar="SITE_DIR", help="root of the checked-out reports branch")
    parser.add_argument("--portfolio", default=None, help="path to data/intraday_portfolio.json")
    parser.add_argument("--repo", default=None, help="owner/name, used for the link to the learning guide")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.site_dir):
        print("site_index: SITE_DIR does not exist: {}".format(args.site_dir), file=sys.stderr)
        return 2

    page = build_index_html(args.site_dir, _load_portfolio(args.portfolio), args.repo)
    with open(os.path.join(args.site_dir, "index.html"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(page)
    with open(os.path.join(args.site_dir, ".nojekyll"), "a", encoding="utf-8"):
        pass
    counts = find_reports(args.site_dir)
    print("site_index: wrote index.html ({} daily, {} backtest, {} swing)".format(
        len(counts["daily"]), len(counts["backtest"]), len(counts["swing"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
