"""Tests for intraday_trader/site_index.py. No network.

Run from the repo root:
    python -m unittest discover -s tests -p "test_intraday_site_index.py"
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser

from intraday_trader import site_index

SCRIPT = os.path.abspath(site_index.__file__)

PORTFOLIO = {
    "initial_capital": 100000.0,
    "capital": 100235.0,
    "days": [
        {"date": "2026-11-11", "start_capital": 100000.0, "end_capital": 100060.2, "net_pnl": 60.2,
         "n_trades": 2, "wins": 1, "losses": 1},
        {"date": "2026-11-12", "start_capital": 100060.2, "end_capital": 99916.6, "net_pnl": -143.6,
         "n_trades": 1, "wins": 0, "losses": 1},
        {"date": "2026-11-13", "start_capital": 99916.6, "end_capital": 100235.0, "net_pnl": 318.4,
         "n_trades": 3, "wins": 2, "losses": 1},
    ],
    "trades": [],
}

FILES = [
    "INTRADAY-DAILY-2026-11-11.html",
    "INTRADAY-DAILY-2026-11-13.html",
    "INTRADAY-DAILY-2026-11-12.html",
    "INTRADAY-DAILY-2026-11-13-DRYRUN.html",     # never listed
    "INTRADAY-DAILY-2026-11-13.json",            # data file, not a report page
    "INTRADAY-BACKTEST-2026-11-14.html",
    "INTRADAY-BACKTEST-2026-10-31.html",
    "SWING-WEEKLY-2026-10-30.html",
    "SWING-WEEKLY-2026-11-13.html",
    "SWING-WEEKLY-2026-11-06.html",
    "NOTES.html",                                 # unknown file, ignored
    "INTRADAY-DAILY-latest.html",                 # no date, ignored
]


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href"))
        if tag not in ("meta", "br", "hr", "link", "img"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(tag)
        else:
            self.stack.pop()


def _parse(text: str) -> _Links:
    parser = _Links()
    parser.feed(text)
    parser.close()
    return parser


class SiteIndexTests(unittest.TestCase):
    def setUp(self):
        self.site = tempfile.mkdtemp(prefix="site-index-test-")
        self.addCleanup(shutil.rmtree, self.site, True)
        self.reports = os.path.join(self.site, "data", "reports")
        os.makedirs(self.reports)
        for name in FILES:
            with open(os.path.join(self.reports, name), "w", encoding="utf-8") as fh:
                fh.write("x")
        os.makedirs(os.path.join(self.reports, "INTRADAY-DAILY-2026-11-10.html"))   # a folder is not a report
        self.portfolio_path = os.path.join(self.site, "portfolio.json")
        with open(self.portfolio_path, "w", encoding="utf-8") as fh:
            json.dump(PORTFOLIO, fh)

    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = site_index.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def _index(self) -> str:
        with open(os.path.join(self.site, "index.html"), encoding="utf-8") as fh:
            return fh.read()

    # ── listing ──────────────────────────────────────────────────────────────

    def test_lists_each_kind_newest_first(self):
        code, out, _ = self._run(self.site)
        self.assertEqual(code, 0)
        self.assertIn("3 daily, 2 backtest, 3 swing", out)
        page = self._index()
        parser = _parse(page)
        self.assertEqual(parser.errors, [])
        self.assertEqual(parser.stack, [])
        self.assertEqual(parser.hrefs, [
            "data/reports/INTRADAY-DAILY-2026-11-13.html",      # the button
            "data/reports/SWING-WEEKLY-2026-11-13.html",        # the secondary link
            "data/reports/INTRADAY-DAILY-2026-11-13.html",
            "data/reports/INTRADAY-DAILY-2026-11-12.html",
            "data/reports/INTRADAY-DAILY-2026-11-11.html",
            "data/reports/INTRADAY-BACKTEST-2026-11-14.html",
            "data/reports/INTRADAY-BACKTEST-2026-10-31.html",
            "data/reports/SWING-WEEKLY-2026-11-13.html",
            "data/reports/SWING-WEEKLY-2026-11-06.html",
            "data/reports/SWING-WEEKLY-2026-10-30.html",
        ])
        self.assertNotIn("DRYRUN", page)
        self.assertNotIn("NOTES", page)
        self.assertNotIn(".json", page)
        self.assertNotIn("2026-11-10", page)
        self.assertIn("Open latest intraday report", page)
        self.assertIn("Latest swing report: week ending 13 Nov 2026", page)
        self.assertLess(page.index("Open latest intraday report"), page.index("Latest swing report"))
        for heading in ("Intraday: daily reports", "Intraday: backtests", "Swing: weekly reports"):
            self.assertIn("<h2>{}</h2>".format(heading), page)
        self.assertIn("Fri, 13 Nov 2026", page)             # daily rows carry the weekday
        self.assertIn("Week ending 06 Nov 2026", page)
        self.assertNotIn("No reports yet", page)

    def test_links_are_relative_and_page_is_self_contained(self):
        self._run(self.site, "--portfolio", self.portfolio_path)
        page = self._index()
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotRegex(page, r"<(script|link|img|iframe)\b")
        self.assertNotRegex(page, r"@import|url\(")
        self.assertNotRegex(page, r'href="/')
        self.assertIn('name="viewport"', page)
        for colour in ("#0d1117", "#c9d1d9", "#58a6ff"):
            self.assertIn(colour, page)
        self.assertNotIn("—", page)

    def test_writes_nojekyll_and_keeps_an_existing_one(self):
        marker = os.path.join(self.site, ".nojekyll")
        self.assertFalse(os.path.exists(marker))
        self._run(self.site)
        self.assertTrue(os.path.isfile(marker))
        self._run(self.site)
        self.assertTrue(os.path.isfile(marker))

    def test_sections_with_no_files(self):
        for name in os.listdir(self.reports):
            path = os.path.join(self.reports, name)
            if os.path.isfile(path) and not name.startswith("SWING-"):
                os.remove(path)
        self.assertEqual(self._run(self.site)[0], 0)
        page = self._index()
        self.assertEqual(page.count("No reports yet"), 2)
        self.assertNotIn("Open latest intraday report", page)
        self.assertIn("Latest swing report: week ending 13 Nov 2026", page)

    def test_empty_site_and_missing_reports_folder(self):
        shutil.rmtree(os.path.join(self.site, "data"))
        code, out, _ = self._run(self.site)
        self.assertEqual(code, 0)
        self.assertIn("0 daily, 0 backtest, 0 swing", out)
        page = self._index()
        self.assertEqual(page.count("No reports yet"), 3)
        self.assertEqual(_parse(page).hrefs, [])

    # ── portfolio ────────────────────────────────────────────────────────────

    def test_portfolio_headline_and_rows(self):
        self.assertEqual(self._run(self.site, "--portfolio", self.portfolio_path)[0], 0)
        page = self._index()
        for needle in ("Paper capital", "₹100,235.00", "started at ₹100,000", "+₹235.00", "+0.24%",
                       "Sessions", "latest 13 Nov 2026", "3 won, 50.0% win rate"):
            self.assertIn(needle, page, needle)
        rows = re.findall(r"<li>(.*?)</li>", page, flags=re.S)
        daily = [r for r in rows if "INTRADAY-DAILY" in r]
        self.assertEqual(len(daily), 3)
        self.assertIn("3 trades", daily[0])
        self.assertIn("+₹318.40", daily[0])
        self.assertIn('class="pos"', daily[0])
        self.assertIn("1 trade ", daily[1].replace("&middot;", " "))
        self.assertIn("-₹143.60", daily[1])
        self.assertIn('class="neg"', daily[1])
        self.assertIn("2 trades", daily[2])
        self.assertIn("+₹60.20", daily[2])

    def test_rows_without_a_portfolio_entry_show_no_numbers(self):
        with open(self.portfolio_path, "w", encoding="utf-8") as fh:
            json.dump(dict(PORTFOLIO, days=PORTFOLIO["days"][:1]), fh)
        self._run(self.site, "--portfolio", self.portfolio_path)
        rows = [r for r in re.findall(r"<li>(.*?)</li>", self._index(), flags=re.S) if "INTRADAY-DAILY" in r]
        self.assertNotIn("trade", rows[0])
        self.assertNotIn("trade", rows[1])
        self.assertIn("2 trades", rows[2])

    def test_no_headline_without_a_portfolio_or_without_days(self):
        self._run(self.site)
        self.assertNotIn("Paper capital", self._index())
        with open(self.portfolio_path, "w", encoding="utf-8") as fh:
            json.dump({"initial_capital": 100000.0, "capital": 100000.0, "days": [], "trades": []}, fh)
        self._run(self.site, "--portfolio", self.portfolio_path)
        page = self._index()
        self.assertNotIn("Paper capital", page)
        self.assertIn("Fri, 13 Nov 2026", page)

    def test_broken_or_missing_portfolio_is_ignored(self):
        for content in ("{not json", "[1, 2, 3]", "", '{"days": "nope"}', '{"days": [1, null, {"x": 1}]}'):
            with open(self.portfolio_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            code, _, err = self._run(self.site, "--portfolio", self.portfolio_path)
            self.assertEqual(code, 0, content)
            self.assertEqual(err, "")
            page = self._index()
            self.assertNotIn("Paper capital", page)
            self.assertIn("Open latest intraday report", page)
        code, _, _ = self._run(self.site, "--portfolio", os.path.join(self.site, "nope.json"))
        self.assertEqual(code, 0)
        self.assertNotIn("Paper capital", self._index())

    def test_odd_portfolio_values_do_not_break_the_page(self):
        odd = {"initial_capital": None, "capital": "abc",
               "days": [{"date": "2026-11-13", "net_pnl": "x", "n_trades": None, "wins": float("nan")}]}
        with open(self.portfolio_path, "w", encoding="utf-8") as fh:
            json.dump(odd, fh)
        self.assertEqual(self._run(self.site, "--portfolio", self.portfolio_path)[0], 0)
        self.assertEqual(_parse(self._index()).errors, [])

    # ── guide link ───────────────────────────────────────────────────────────

    def test_repo_adds_the_guide_link(self):
        self._run(self.site, "--repo", "jakemo007/mcp-trading-agent")
        page = self._index()
        self.assertIn(
            'href="https://github.com/jakemo007/mcp-trading-agent/blob/main/docs/intraday-trader/README.md"', page)
        self.assertEqual(page.count("https://"), 1)

    def test_malformed_repo_is_ignored(self):
        for repo in ("nonsense", "a/b/c", '"><script>', "owner/na me", ""):
            self._run(self.site, "--repo", repo)
            page = self._index()
            self.assertNotIn("github.com", page)
            self.assertNotIn("<script", page)

    # ── command line ─────────────────────────────────────────────────────────

    def test_missing_site_dir_exits_2(self):
        missing = os.path.join(self.site, "does-not-exist")
        code, out, err = self._run(missing)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("does not exist", err)
        self.assertFalse(os.path.exists(missing))

    def test_a_file_as_site_dir_exits_2(self):
        self.assertEqual(self._run(self.portfolio_path)[0], 2)

    def test_runs_as_a_copied_standalone_script(self):
        """The swing workflow copies the file elsewhere and runs it with no repo around it."""
        elsewhere = tempfile.mkdtemp(prefix="site-index-copy-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        copy = os.path.join(elsewhere, "site_index.py")
        shutil.copy(SCRIPT, copy)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("PYTHONPATH", None)
        done = subprocess.run(
            [sys.executable, copy, self.site, "--portfolio", self.portfolio_path, "--repo", "o/n"],
            cwd=elsewhere, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("₹100,235.00", self._index())
        gone = subprocess.run([sys.executable, copy, os.path.join(elsewhere, "nope")],
                              cwd=elsewhere, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(gone.returncode, 2)

    def test_imports_only_the_standard_library(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        allowed = {"__future__", "argparse", "html", "json", "math", "os", "re", "sys", "datetime", "typing"}
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                used.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "relative import")
                used.add((node.module or "").split(".")[0])
        self.assertLessEqual(used, allowed, used - allowed)

    def test_build_index_html_is_pure(self):
        before = sorted(os.listdir(self.site))
        page = site_index.build_index_html(self.site, PORTFOLIO, "o/n")
        self.assertEqual(sorted(os.listdir(self.site)), before)
        self.assertTrue(page.lower().startswith("<!doctype html>"))
        self.assertIn("<title>Paper Trading Reports</title>", page)


if __name__ == "__main__":
    unittest.main()
