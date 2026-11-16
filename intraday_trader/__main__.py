"""Command line entry point.

    python -m intraday_trader run      [--date YYYY-MM-DD] [--dry-run] [--force] [--json]
    python -m intraday_trader backtest [--days N]                             [--json]

* stdout/stderr are reconfigured to UTF-8 first (the rupee sign must print on a
  Windows cp1252 console).
* Logging (INFO) goes to stderr.
* Without --json: a short human-readable summary block on stdout, ending with
  the report path and config.DISCLAIMER.
* With --json: stdout carries exactly one JSON document, the dict returned by
  runner.run_live / runner.run_backtest, and nothing else.
* Exit codes: 0 for status "ok" and "already_recorded"; 2 for "no_data";
  1 for an unexpected exception (traceback on stderr; with --json also
  {"status": "error", "error": "<message>"} on stdout).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import date
from typing import List, Optional

from intraday_trader import config, runner

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_DATA = 2
RULE = "=" * 60


def _utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date as YYYY-MM-DD, got {!r}".format(text))


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a whole number, got {!r}".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m intraday_trader",
        description="NSE intraday paper trader: volume scan, weekly/daily/1h bias, 5-minute FVG entries.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    run = sub.add_parser("run", help="replay one session, update the paper portfolio, write the daily report")
    run.add_argument("--date", type=_iso_date, default=None, help="session to replay (default: the latest)")
    run.add_argument("--dry-run", action="store_true", help="do not touch the portfolio file")
    run.add_argument("--force", action="store_true", help="replay the latest session even if already recorded")
    run.add_argument("--json", action="store_true", help="print the result as one JSON document")

    backtest = sub.add_parser("backtest", help="replay the last N sessions with a fresh portfolio")
    backtest.add_argument("--days", type=_positive_int, default=30, help="sessions to replay (default 30)")
    backtest.add_argument("--json", action="store_true", help="print the result as one JSON document")
    return parser


def _rupees(value: float, signed: bool = False) -> str:
    text = "{:,.2f}".format(abs(value))
    if signed:
        return ("+" if value >= 0 else "-") + "₹" + text
    return ("-" if value < 0 else "") + "₹" + text


def _run_summary(result: dict) -> str:
    status = result.get("status")
    lines = [RULE, "  INTRADAY PAPER TRADER", RULE]
    if status == "ok":
        mode = "  (dry run: portfolio not changed)" if result.get("dry_run") else ""
        lines += [
            "  Session    : {}{}".format(result["date"], mode),
            "  Capital    : {} -> {}".format(_rupees(result["start_capital"]), _rupees(result["end_capital"])),
            "  Net P&L    : {}".format(_rupees(result["net_pnl"], signed=True)),
            "  Trades     : {} ({} wins, {} losses)".format(result["n_trades"], result["wins"], result["losses"]),
            "  In play    : {} stocks".format(result["watchlist"]),
        ]
        if result.get("partial"):
            lines.append("  Note       : the session data was incomplete, so this is a partial day")
        lines.append("  Report     : {}".format(result["report_path"]))
    elif status == "already_recorded":
        lines.append("  Session {} is already recorded. Nothing was changed.".format(result.get("date", "")))
        lines.append("  Use --force to replay it, or --date to pick another session.")
    elif status == "no_data":
        which = " for {}".format(result["date"]) if result.get("date") else ""
        lines.append("  No market data was available{}.".format(which))
    else:
        lines.append("  Status: {}".format(status))
    lines += [RULE, config.DISCLAIMER]
    return "\n".join(lines)


def _backtest_summary(result: dict) -> str:
    status = result.get("status")
    lines = [RULE, "  INTRADAY PAPER TRADER: BACKTEST", RULE]
    if status == "ok":
        s = result["summary"]
        lines += [
            "  Period     : {} to {} ({} sessions, {} requested)".format(
                result["first_date"], result["last_date"], result["actual_days"], result["requested_days"]),
            "  Capital    : {} -> {}".format(_rupees(config.INITIAL_CAPITAL), _rupees(s["capital"])),
            "  Net P&L    : {} ({:+.2f}%)".format(_rupees(s["net_pnl"], signed=True), s["net_pnl_pct"]),
            "  Trades     : {} ({} wins, {} losses, win rate {:.1f}%)".format(
                s["n_trades"], s["wins"], s["losses"], s["win_rate_pct"]),
            "  Charges    : {}".format(_rupees(s["charges"])),
            "  Drawdown   : {} ({:.2f}%, end-of-day capital)".format(_rupees(s["max_drawdown_inr"]),
                                                                    s["max_drawdown_pct"]),
            "  Report     : {}".format(result["report_path"]),
        ]
    elif status == "no_data":
        lines.append("  Not enough market data was available for a backtest.")
    else:
        lines.append("  Status: {}".format(status))
    lines += [RULE, config.DISCLAIMER]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_streams()
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )

    try:
        if args.command == "run":
            result = runner.run_live(day=args.date, dry_run=args.dry_run, force=args.force)
            summary = _run_summary
        else:
            result = runner.run_backtest(days=args.days)
            summary = _backtest_summary
    except Exception as exc:  # anything unexpected: say so and fail the job
        traceback.print_exc(file=sys.stderr)
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc) or exc.__class__.__name__}, ensure_ascii=False))
        return EXIT_ERROR

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    else:
        print(summary(result))

    status = result.get("status")
    if status in ("ok", "already_recorded"):
        return EXIT_OK
    if status == "no_data":
        return EXIT_NO_DATA
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
