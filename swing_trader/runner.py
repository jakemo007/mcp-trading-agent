from __future__ import annotations

import logging
import time
from datetime import date, datetime

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


def update_positions() -> None:
    state = load()
    if not state["open_positions"]:
        return

    changed = False
    for pos in list(state["open_positions"]):
        ticker = pos["ticker"]
        try:
            df = yf.download(ticker, period="2d", interval="1d",
                             progress=False, auto_adjust=True)
        except Exception as exc:
            logger.warning("yfinance error updating %s: %s — keeping position", ticker, exc)
            continue

        if df is None or df.empty:
            logger.warning("%s: no data — keeping position open", ticker)
            continue

        last = df.iloc[-1]
        day_high  = float(last["High"])
        day_low   = float(last["Low"])
        day_close = float(last["Close"])

        update_unrealised(state, ticker, day_close)

        if day_low <= pos["stop_loss"]:
            close_position(state, pos["id"], pos["stop_loss"], "SL_HIT")
            logger.info("SL_HIT %s  exit=%.2f", ticker, pos["stop_loss"])
            changed = True
        elif day_high >= pos["target"]:
            close_position(state, pos["id"], pos["target"], "TARGET_HIT")
            logger.info("TARGET_HIT %s  exit=%.2f", ticker, pos["target"])
            changed = True
        else:
            changed = True  # unrealised updated

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
