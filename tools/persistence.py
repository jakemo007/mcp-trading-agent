"""
tools/persistence.py -- Stateful persistence layer for the trading agent.

Manages:
  - HTML report generation & 30-day auto-cleanup
  - lessons.md knowledge base (read / append)

All file paths are resolved relative to the project DATA_DIR
(mcp-trading-agent/data/) so the server stays self-contained.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Resolve persistent data directories relative to server root ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR      = _PROJECT_ROOT / "data"
REPORTS_DIR   = DATA_DIR / "reports"
LESSONS_FILE  = DATA_DIR / "lessons.md"

# How many days to keep HTML reports before auto-purge
REPORT_RETENTION_DAYS = 30

# Sidecar file storing SHA-256 fingerprints of persisted lesson texts.
# Using a dedicated file avoids parsing the full markdown on every sync call.
LESSONS_HASHES_FILE = DATA_DIR / "lessons.hashes"


def _lesson_fingerprint(lesson: str) -> str:
    """Return a 16-char hex fingerprint of *lesson* (normalised, first 120 chars).

    Normalisation (lowercase + collapse whitespace) makes the fingerprint
    robust to minor rewording while still catching true duplicates.
    """
    normalised = " ".join(lesson.lower().split())[:120]
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def _load_fingerprints() -> set[str]:
    """Load existing lesson fingerprints from the sidecar hash file."""
    if not LESSONS_HASHES_FILE.exists():
        return set()
    return set(LESSONS_HASHES_FILE.read_text(encoding="utf-8").splitlines())


def _save_fingerprints(fps: set[str]) -> None:
    """Persist the full fingerprint set to the sidecar hash file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LESSONS_HASHES_FILE.write_text("\n".join(sorted(fps)), encoding="utf-8")

# ── Default lessons.md template ──────────────────────────────────
_LESSONS_TEMPLATE = """\
# Trading Lessons Learned

> This file is automatically maintained by the MCP Trading Agent.
> New insights from backtests and live analysis are appended below.
> **Do not delete the section headers.**

---

## Backtested Rules

| # | Date | Stock | Setup | Outcome | Rule / Insight |
|---|------|-------|-------|---------|----------------|

---

## General Market Observations

_(Append new observations below this line.)_

---

## Stock-Specific Notes

_(Append stock-specific lessons below this line.)_

---
"""


# ══════════════════════════════════════════════════════════════
#  manage_html_report
# ══════════════════════════════════════════════════════════════

def manage_html_report(
    stock_name: str,
    html_content: str,
) -> dict[str, Any]:
    """
    Save an HTML analysis report and purge stale reports (>30 days).

    1. Ensures ``data/reports/`` exists.
    2. Scans for any ``.html`` files older than REPORT_RETENTION_DAYS
       and deletes them.
    3. Writes *html_content* to ``<stock_name>_<YYYY-MM-DD>.html``.

    Returns
    -------
    dict with saved_path, deleted_files count, and status.
    """
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        # ── Cleanup stale reports ────────────────────────────────
        cutoff = datetime.now() - timedelta(days=REPORT_RETENTION_DAYS)
        deleted: list[str] = []

        for f in REPORTS_DIR.glob("*.html"):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
                    deleted.append(f.name)
                    logger.info("Purged stale report: %s", f.name)
            except OSError as exc:
                logger.warning("Could not delete %s: %s", f.name, exc)

        # ── Sanitise stock name for filename ─────────────────────
        safe_name = re.sub(r"[^\w\-]", "_", stock_name.strip().upper())
        today_str = datetime.now().strftime("%Y-%m-%d")
        filename  = f"{safe_name}_{today_str}.html"
        filepath  = REPORTS_DIR / filename

        filepath.write_text(html_content, encoding="utf-8")
        logger.info("Saved report: %s", filepath)

        # Auto-open the report in the user's default browser.
        # filepath.as_uri() produces the correct file:/// URI on all platforms
        # (e.g. file:///C:/path/file.html on Windows).
        browser_opened = False
        try:
            webbrowser.open(filepath.as_uri())
            browser_opened = True
            logger.info("Opened report in browser: %s", filepath.as_uri())
        except Exception as exc:
            logger.warning("Could not auto-open browser: %s", exc)

        return {
            "status":        "success",
            "saved_path":    str(filepath),
            "filename":      filename,
            "deleted_count": len(deleted),
            "deleted_files": deleted,
            "browser_opened": browser_opened,
        }

    except Exception as exc:
        logger.exception("Failed to manage HTML report for %s", stock_name)
        return {"error": f"Report save failed: {exc}"}


# ══════════════════════════════════════════════════════════════
#  read_lessons_learned
# ══════════════════════════════════════════════════════════════

def read_lessons_learned() -> dict[str, Any]:
    """
    Read and return the full contents of ``data/lessons.md``.

    If the file does not exist, it is created with a starter
    markdown schema (headers for Stock, Date, Setup, Outcome, Rule).

    Returns
    -------
    dict with file_path, content (str), and line_count.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not LESSONS_FILE.exists():
            LESSONS_FILE.write_text(_LESSONS_TEMPLATE, encoding="utf-8")
            logger.info("Created fresh lessons.md at %s", LESSONS_FILE)

        content = LESSONS_FILE.read_text(encoding="utf-8")
        return {
            "status":     "success",
            "file_path":  str(LESSONS_FILE),
            "line_count": content.count("\n") + 1,
            "content":    content,
        }

    except Exception as exc:
        logger.exception("Failed to read lessons.md")
        return {"error": f"Could not read lessons.md: {exc}"}


# ══════════════════════════════════════════════════════════════
#  update_lessons_learned
# ══════════════════════════════════════════════════════════════

def update_lessons_learned(new_content: str) -> dict[str, Any]:
    """
    Append *new_content* to ``data/lessons.md``.

    The content is timestamped and separated by a horizontal rule
    so the LLM can later parse discrete entries.

    Returns
    -------
    dict with status, file_path, and bytes_written.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Ensure file exists with template if brand new
        if not LESSONS_FILE.exists():
            LESSONS_FILE.write_text(_LESSONS_TEMPLATE, encoding="utf-8")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"\n\n---\n\n"
            f"### Entry: {timestamp}\n\n"
            f"{new_content.strip()}\n"
        )

        with LESSONS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(entry)

        logger.info("Appended %d chars to lessons.md", len(entry))

        return {
            "status":        "success",
            "file_path":     str(LESSONS_FILE),
            "bytes_written": len(entry),
            "timestamp":     timestamp,
        }

    except Exception as exc:
        logger.exception("Failed to update lessons.md")
        return {"error": f"Could not update lessons.md: {exc}"}


# ══════════════════════════════════════════════════════════════
#  sync_trading_knowledge
# ══════════════════════════════════════════════════════════════

def sync_trading_knowledge(new_rules: list[dict]) -> dict[str, Any]:
    """
    Deduplicate and append high-conviction backtest rules to lessons.md.

    Each rule must contain:
      - stock      : ticker symbol (e.g. "NIFTY")
      - interval   : candle interval (e.g. "15m")
      - setup_type : SMC pattern (e.g. "BSL_SWEEP_FVG")
      - rr         : achieved RR as a string (e.g. "3.4")
      - lesson     : the actionable insight (1-2 sentences)

    Deduplication: a rule is skipped if the first 60 chars of its
    *lesson* text (lowercased) already appear in lessons.md, preventing
    near-identical rules from accumulating across backtest runs.

    Returns
    -------
    dict with rules_added, rules_skipped, and per-skip reasons.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not LESSONS_FILE.exists():
            LESSONS_FILE.write_text(_LESSONS_TEMPLATE, encoding="utf-8")

        # SHA-256 hash-based deduplication — robust to partial text matches
        # that fool substring-search approaches.
        known_fps   = _load_fingerprints()
        session_fps: set[str] = set()  # fingerprints added during this call

        added:   list[str]  = []
        skipped: list[dict] = []

        for rule in new_rules:
            stock      = str(rule.get("stock",      "")).upper().strip()
            interval   = str(rule.get("interval",   "")).strip()
            setup_type = str(rule.get("setup_type", "")).strip()
            rr         = str(rule.get("rr",         "")).strip()
            lesson     = str(rule.get("lesson",     "")).strip()

            if not all([stock, setup_type, lesson]):
                skipped.append({
                    "rule":   rule,
                    "reason": "missing required fields (stock / setup_type / lesson)",
                })
                continue

            fp = _lesson_fingerprint(lesson)
            if fp in known_fps or fp in session_fps:
                skipped.append({
                    "stock":      stock,
                    "setup_type": setup_type,
                    "reason":     "duplicate (fingerprint match)",
                })
                continue

            session_fps.add(fp)
            row = f"| {stock} | {interval} | {setup_type} | {rr} | {lesson} |"
            added.append(row)

        if added:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            block = (
                f"\n\n---\n\n"
                f"### Synced Knowledge — {timestamp}\n\n"
                f"| Stock | Interval | Setup Type | RR | Key Lesson |\n"
                f"|-------|----------|------------|----|-----------|\n"
                + "\n".join(added)
                + "\n"
            )
            with LESSONS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(block)
            _save_fingerprints(known_fps | session_fps)
            logger.info("sync_trading_knowledge: added %d rules to lessons.md", len(added))

        return {
            "status":        "success",
            "rules_added":   len(added),
            "rules_skipped": len(skipped),
            "skipped":       skipped,
            "timestamp":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "file_path":     str(LESSONS_FILE),
        }

    except Exception as exc:
        logger.exception("Failed to sync trading knowledge")
        return {"error": f"sync_trading_knowledge failed: {exc}"}
