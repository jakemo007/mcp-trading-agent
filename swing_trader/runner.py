from __future__ import annotations

import logging
import time
from datetime import date, datetime

import pandas as pd
import pytz
import yfinance as yf

from swing_trader import config
from swing_trader.config import (
    ENTRY_END,
    ENTRY_START,
    MAX_POSITIONS,
    MIN_RR,
    WATCHLIST,
)
from swing_trader.portfolio import (
    close_position,
    load,
    open_position,
    save,
    update_unrealised,
)
from swing_trader.report import build_html, save_report
from swing_trader.scanner import scan_entry
from swing_trader.sizing import calc_quantity

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_scan_done_date:   date | None = None
_report_done_date: date | None = None


def _ist_now() -> datetime:
    return datetime.now(IST)


def _is_market_day() -> bool:
    return _ist_now().weekday() < 5


def _in_entry_window() -> bool:
    t = _ist_now().time()
    return ENTRY_START <= t <= ENTRY_END


def run_daily_scan(dry_run: bool = False) -> None:
    logger.info("Running daily scan — %d tickers", len(WATCHLIST))
    state = load()

    if len(state["open_positions"]) >= MAX_POSITIONS:
        logger.info("Position cap reached (%d) — skipping new entries", MAX_POSITIONS)
        return

    for ticker in WATCHLIST:
        if len(state["open_positions"]) >= MAX_POSITIONS:
            logger.info("Position cap reached mid-scan — stopping")
            break

        if any(p["ticker"] == ticker for p in state["open_positions"]):
            logger.debug("%s already open — skip", ticker)
            continue

        signal = scan_entry(ticker)
        if signal is None:
            continue

        qty, capital_used = calc_quantity(
            state["capital"], signal["entry"], signal["stop_loss"]
        )
        if qty == 0:
            logger.info("%s: insufficient capital — skip", ticker)
            continue

        if dry_run:
            logger.info(
                "[DRY-RUN] %s  entry=%.2f  SL=%.2f  tgt=%.2f  RR=%.2f  qty=%d",
                ticker, signal["entry"], signal["stop_loss"],
                signal["target"], signal["rr"], qty,
            )
            continue

        pos = open_position(
            state,
            ticker=ticker,
            entry=signal["entry"],
            stop_loss=signal["stop_loss"],
            target=signal["target"],
            qty=qty,
            capital_used=capital_used,
            rr=signal["rr"],
        )
        save(state)
        logger.info(
            "OPENED %s  entry=%.2f  SL=%.2f  tgt=%.2f  RR=%.2f  qty=%d  capital_used=₹%.0f",
            ticker, pos["entry"], pos["stop_loss"],
            pos["target"], pos["rr"], pos["qty"], capital_used,
        )


def _bars_since_open(ticker: str, opened_at: str) -> pd.DataFrame | None:
    """Hourly bars from the position's open time to now (yfinance keeps 730d of 1h)."""
    opened = pd.Timestamp(opened_at)
    if opened.tzinfo is None:
        opened = opened.tz_localize("UTC")
    try:
        df = yf.download(ticker, start=opened.date().isoformat(), interval="1h",
                         progress=False, auto_adjust=True)
    except Exception as exc:
        logger.warning("yfinance error updating %s: %s — keeping position", ticker, exc)
        return None

    if df is None or df.empty:
        return None

    # yfinance ≥0.2.x returns MultiIndex columns for single tickers — flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    # Keep bars that close after entry (hourly bar starting before entry still overlaps it)
    return df[idx + pd.Timedelta(hours=1) > opened.tz_convert(idx.tz)]


def update_positions() -> None:
    """Close any open position whose SL or target was touched at any point since entry."""
    state = load()
    if not state["open_positions"]:
        return

    changed = False
    for pos in list(state["open_positions"]):
        ticker = pos["ticker"]
        df = _bars_since_open(ticker, pos["opened_at"])
        if df is None or df.empty:
            logger.warning("%s: no data — keeping position open", ticker)
            continue

        exit_price, exit_reason = None, None
        try:
            for _, bar in df.iterrows():
                o, h, l = float(bar["Open"]), float(bar["High"]), float(bar["Low"])
                # Gap through a level fills at the open; if one bar spans both, assume SL first
                if l <= pos["stop_loss"]:
                    exit_price, exit_reason = min(o, pos["stop_loss"]), "SL_HIT"
                    break
                if h >= pos["target"]:
                    exit_price, exit_reason = max(o, pos["target"]), "TARGET_HIT"
                    break
            last_close = float(df.iloc[-1]["Close"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("%s: unexpected data shape (%s) — keeping position", ticker, exc)
            continue

        if exit_reason:
            close_position(state, pos["id"], round(exit_price, 2), exit_reason)
            logger.info("%s %s  exit=%.2f", exit_reason, ticker, exit_price)
        else:
            update_unrealised(state, ticker, last_close)
        changed = True

    if changed:
        save(state)


def send_weekly_report() -> None:
    state = load()
    html  = build_html(state)
    filename = f"SWING-WEEKLY-{date.today().isoformat()}.html"
    path = save_report(html, filename)
    logger.info("Weekly report saved → %s", path)
    print(f"[REPORT] {path}")


def main_loop(dry_run: bool = False) -> None:
    global _scan_done_date, _report_done_date

    logger.info("Swing trader loop started (dry_run=%s)", dry_run)

    while True:
        now = _ist_now()
        today = now.date()

        if not _is_market_day():
            logger.debug("Market closed (weekend) — sleeping 60s")
            time.sleep(60)
            continue

        # ── Position update: every loop tick on market days ───────────────────
        update_positions()

        # ── Daily scan: once per day inside entry window ──────────────────────
        if _scan_done_date != today and _in_entry_window():
            run_daily_scan(dry_run=dry_run)
            _scan_done_date = today

        # ── Weekly report: Friday after 15:00 IST, once per day ──────────────
        if (
            now.weekday() == 4
            and now.time().hour >= 15
            and _report_done_date != today
        ):
            send_weekly_report()
            _report_done_date = today

        time.sleep(60)
