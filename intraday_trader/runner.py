"""Orchestration with side effects: download, simulate, save, write reports.

run_live   replays one session, updates the paper portfolio and writes the
           daily report. Meant to run after the close.
run_backtest replays the last N sessions with a fresh in-memory portfolio and
           never touches the live portfolio file.
"""
from __future__ import annotations

import copy
import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from intraday_trader import config, data, engine, portfolio, report

logger = logging.getLogger(__name__)

_IST = "Asia/Kolkata"
_IST_TZ = timezone(timedelta(hours=5, minutes=30))
DRY_RUN_SUFFIX = "-DRYRUN"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    """The wall clock in IST. A function so tests can fix the time."""
    return datetime.now(_IST_TZ)


def _in_progress(day: date) -> bool:
    """True when `day` is today in IST and the market has not closed yet.

    The bar count alone cannot tell: after about 14:15 an unfinished session
    already has more than MIN_BARS_FOR_FULL_DAY bars.
    """
    now = _now_ist()
    return day == now.date() and now.time() < config.SESSION_CLOSE


def _session_counts(bars5: Dict[str, pd.DataFrame]) -> "Counter[date]":
    """How many symbols have at least one 5m bar on each calendar date (IST)."""
    counts: "Counter[date]" = Counter()
    for df in bars5.values():
        if df is None or len(df) == 0:
            continue
        index = df.index
        if getattr(index, "tz", None) is not None:
            index = index.tz_convert(_IST).tz_localize(None)
        for stamp in index.normalize().unique():
            counts[stamp.date()] += 1
    return counts


def _well_covered_sessions(bars5: Dict[str, pd.DataFrame]) -> List[date]:
    """Dates on which at least half of the downloaded symbols have bars, ascending."""
    counts = _session_counts(bars5)
    n_symbols = sum(1 for df in bars5.values() if df is not None and len(df))
    return sorted(d for d, n in counts.items() if n * 2 >= n_symbols)


def _write_json(payload: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def _universe() -> "tuple[List[str], Dict[str, str]]":
    rows = data.load_universe()
    symbols = [row["symbol"] for row in rows]
    names = {row["symbol"]: row.get("name") or row["symbol"] for row in rows}
    return symbols, names


def _download(period_5m: str) -> "tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], Dict[str, str]]":
    symbols, names = _universe()
    logger.info("Universe: %d symbols. Downloading 5-minute bars (%s)...", len(symbols), period_5m)
    bars5 = data.fetch_5m(symbols, period_5m) or {}
    logger.info("5-minute bars: %d of %d symbols, %d rows", len(bars5), len(symbols),
                sum(len(df) for df in bars5.values()))
    logger.info("Downloading daily bars (%s)...", config.DAILY_PERIOD)
    daily = data.fetch_daily(symbols, config.DAILY_PERIOD) or {}
    logger.info("Daily bars: %d of %d symbols, %d rows", len(daily), len(symbols),
                sum(len(df) for df in daily.values()))
    return bars5, daily, names


# ── Live (end-of-day) run ─────────────────────────────────────────────────────

def run_live(
    day: Optional[date] = None,
    dry_run: bool = False,
    force: bool = False,
    reports_dir: Path = config.REPORTS_DIR,
    portfolio_file: Path = config.PORTFOLIO_FILE,
) -> dict:
    """Simulate one session and publish its report. Meant to run after the close.

    Statuses: "ok", "no_data" (nothing downloaded, or `day` has no bars) and
    "already_recorded" (no `day` given, the latest session is already stored as
    a full day and `force` is off: what a holiday or weekend run returns).
    In a dry run the portfolio file is untouched and the report files carry a
    "-DRYRUN" suffix. A run made while today's market is still open records
    the day as partial, so the next run after the close replaces it.
    """
    bars5, daily, names = _download(config.LIVE_5M_PERIOD)
    if not any(df is not None and len(df) for df in bars5.values()):
        logger.warning("No 5-minute data was downloaded.")
        return {"status": "no_data"}

    if day is not None:
        target = day
        if _session_counts(bars5).get(target, 0) == 0:
            logger.warning("No 5-minute bars for %s.", target.isoformat())
            return {"status": "no_data", "date": target.isoformat()}
    else:
        sessions = _well_covered_sessions(bars5)
        if not sessions:
            logger.warning("No session is covered by at least half of the symbols.")
            return {"status": "no_data"}
        target = sessions[-1]

    state = portfolio.load_state(portfolio_file)
    if day is None and not force:
        for entry in state.get("days", []):
            if entry.get("date") == target.isoformat() and not entry.get("partial", False):
                logger.info("Session %s is already recorded; nothing to do.", target.isoformat())
                return {"status": "already_recorded", "date": target.isoformat()}

    start_capital = portfolio.start_capital_for(state, target)
    logger.info("Target session %s, start capital %.2f%s", target.isoformat(), start_capital,
                " (dry run)" if dry_run else "")
    result = engine.run_day(target, bars5, daily, start_capital, names)
    if _in_progress(target) and not result.get("partial"):
        # Recorded as partial so the run after the close replaces it instead of
        # finding it "already recorded".
        result["partial"] = True
        result.setdefault("notes", []).append(
            "The market was still open when this ran ({} IST), so this is a partial day; "
            "a run after the close replaces it.".format(_now_ist().strftime("%H:%M")))
    logger.info("Watchlist %d, signals %d, trades %d (%d wins, %d losses), net %+.2f, end capital %.2f",
                len(result["watchlist"]), len(result["signals"]), result["n_trades"], result["wins"],
                result["losses"], result["net_pnl"], result["end_capital"])

    # The report shows the portfolio as it stands after this day, also in a dry run.
    state_after = portfolio.record_day(copy.deepcopy(state) if dry_run else state, result)
    html = report.build_daily_html(result, state_after)

    if not dry_run:
        portfolio.save_state(state_after, portfolio_file)
        logger.info("Portfolio saved: %s (capital %.2f)", portfolio_file, state_after["capital"])

    suffix = DRY_RUN_SUFFIX if dry_run else ""
    stem = "{}{}{}".format(config.DAILY_REPORT_PREFIX, target.isoformat(), suffix)
    report_path = report.save_report(html, stem + ".html", reports_dir)
    json_path = _write_json(result, Path(reports_dir) / (stem + ".json"))
    logger.info("Report: %s", report_path)

    return {
        "status": "ok",
        "date": target.isoformat(),
        "dry_run": bool(dry_run),
        "report_path": str(report_path),
        "json_path": str(json_path),
        "start_capital": result["start_capital"],
        "end_capital": result["end_capital"],
        "net_pnl": result["net_pnl"],
        "n_trades": result["n_trades"],
        "wins": result["wins"],
        "losses": result["losses"],
        "watchlist": len(result["watchlist"]),
        "partial": bool(result["partial"]),
    }


# ── Backtest ──────────────────────────────────────────────────────────────────

def run_backtest(days: int = 30, reports_dir: Path = config.REPORTS_DIR) -> dict:
    """Replay the last `days` sessions with a fresh in-memory portfolio.

    A session is usable when at least RVOL_MIN_SESSIONS earlier sessions exist.
    Today's session is left out while the market is still open.
    Never reads or writes the live portfolio file.
    """
    requested = max(1, int(days))
    bars5, daily, names = _download(config.BACKTEST_5M_PERIOD)
    sessions = _well_covered_sessions(bars5)
    if sessions and _in_progress(sessions[-1]):
        logger.info("Leaving out %s: the market is still open.", sessions[-1].isoformat())
        sessions = sessions[:-1]
    usable = sessions[int(config.RVOL_MIN_SESSIONS):]
    chosen = usable[-requested:]
    if not chosen:
        logger.warning("Not enough history for a backtest (%d sessions downloaded).", len(sessions))
        return {"status": "no_data"}
    logger.info("Backtest: %d sessions requested, %d usable, replaying %s to %s",
                requested, len(usable), chosen[0].isoformat(), chosen[-1].isoformat())

    state = portfolio.new_state()
    results: List[dict] = []
    for session in chosen:
        start_capital = portfolio.start_capital_for(state, session)
        result = engine.run_day(session, bars5, daily, start_capital, names)
        portfolio.record_day(state, result)
        results.append(result)
        logger.info("%s  watchlist %2d  trades %d (%dW/%dL)  net %+9.2f  capital %.2f",
                    session.isoformat(), len(result["watchlist"]), result["n_trades"], result["wins"],
                    result["losses"], result["net_pnl"], state["capital"])

    summary = portfolio.summarize(state)
    logger.info("Backtest done: %d sessions, %d trades, win rate %.1f%%, net %+.2f, max drawdown %.2f",
                summary["sessions"], summary["n_trades"], summary["win_rate_pct"], summary["net_pnl"],
                summary["max_drawdown_inr"])

    today = _now_ist().date().isoformat()
    stem = "{}{}".format(config.BACKTEST_REPORT_PREFIX, today)
    html = report.build_backtest_html(results, state)
    report_path = report.save_report(html, stem + ".html", reports_dir)
    json_path = _write_json(
        {"summary": summary, "days": state["days"], "trades": state["trades"]},
        Path(reports_dir) / (stem + ".json"),
    )
    logger.info("Report: %s", report_path)

    return {
        "status": "ok",
        "requested_days": requested,
        "actual_days": len(chosen),
        "first_date": chosen[0].isoformat(),
        "last_date": chosen[-1].isoformat(),
        "report_path": str(report_path),
        "json_path": str(json_path),
        "summary": summary,
    }
