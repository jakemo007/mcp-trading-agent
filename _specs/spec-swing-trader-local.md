---
title: 'Local Swing Trader Module'
type: 'feature'
created: '2026-08-24'
status: 'done'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The MCP trading agent has no local swing trading capability — existing paper trading is intraday-only (0.5% risk, EOD forced close). The user needs a standalone swing trade runner that persists multi-day positions, enforces 2% risk / 25%-capital rules, and publishes a weekly HTML P&L report to a browsable URL with no external accounts.

**Approach:** Add a `swing_trader/` package to the existing workspace. It reuses yfinance + pandas already present; adds a daily-TF scanner (EMA-20, ATR-14, FVG detection, BSL/SSL levels), a separate JSON portfolio file, a position-sizing module, and a GitHub Actions workflow that runs on schedule, commits the weekly HTML report to an orphan `reports` branch, and optionally serves it via GitHub Pages.

## Boundaries & Constraints

**Always:**
- Capital ₹1,00,000; max trade allocation 25% (₹25,000); max risk per trade 2% (₹2,000).
- Quantity = min(floor(₹2,000 / risk_per_share), floor(₹25,000 / entry)).
- Minimum RR 1:2 — skip any signal below this gate.
- Entry scans only during 09:30–14:30 IST, Monday–Friday.
- Swing positions held multi-day — no EOD forced close.
- State persists in `data/swing_portfolio.json` on the `main` branch — committed back by the workflow after each run.
- Weekly HTML report committed to orphan `reports` branch — `main` branch never contains report files.
- Module is standalone: `python -m swing_trader` starts the runner locally; no MCP server needed.
- Watchlist = Nifty 100 HIGH + MODERATE BO stocks from the 2026-08-24 scan (hardcoded in config).
- All new files are git-tracked; no binary blobs.

**Ask First:**
- If > 4 open swing positions exist when a new signal fires — skip new entry and log the cap reason.

**Never:**
- Do not modify existing `tools/paper_trading.py` (intraday) or `server.py`.
- Do not use a database — JSON state only.
- Do not close swing positions at EOD — only on SL hit, target hit, or manual reset.
- Do not add a web server or UI.
- Do not use external credentials (no Google, no SMTP) — only `GITHUB_TOKEN` (auto-injected by Actions).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Valid signal, capital OK | RR ≥ 2.0, qty > 0, capital available | Position opened, portfolio.json updated, log line printed | — |
| RR gate fail | Computed RR < 2.0 | Signal skipped; "RR gate failed" logged | — |
| Capital exhausted | qty × entry > available capital | Signal skipped; "insufficient capital" logged | — |
| SL hit overnight | Latest daily low ≤ stop_loss | Position closed at SL, P&L calculated, capital updated | yfinance error → keep position open, log warning |
| Target hit overnight | Latest daily high ≥ target | Position closed at target, WIN recorded | — |
| Weekend / holiday | weekday ≥ 5 or no data | Scan skipped; "market closed" logged | — |
| Outside entry window | IST < 09:30 or > 14:30 | No new entries; position update still runs | — |
| Reports branch missing | First Friday run | Workflow creates orphan `reports` branch, pushes HTML | — |
| yfinance error during scan | Network timeout | Ticker skipped; error appended to log | — |

</frozen-after-approval>

## Code Map

- `swing_trader/__init__.py` — package marker
- `swing_trader/config.py` — all constants: capital rules, watchlist, paths
- `swing_trader/scanner.py` — `scan_entry(ticker) → signal | None`; daily OHLCV; EMA-20 trend; ATR-14 SL; FVG detection; BSL/SSL targets; RR gate
- `swing_trader/portfolio.py` — `load()`, `save()`, `open_position()`, `close_position()`, `update_unrealised()` on `data/swing_portfolio.json`
- `swing_trader/sizing.py` — `calc_quantity(capital, entry, stop) → (qty, capital_used)` with both 2% and 25% rules
- `swing_trader/report.py` — `build_html(portfolio) → str`; `save_report(html_str, filename) → Path`; dark-theme self-contained HTML
- `swing_trader/runner.py` — `run_daily_scan()`, `update_positions()`, `main_loop()`; IST window gating
- `swing_trader/__main__.py` — `python -m swing_trader` entry point; startup banner; KeyboardInterrupt handling
- `.github/workflows/swing-trader.yml` — two scheduled jobs: `scan` (09:25 IST Mon–Fri) + `report` (15:15 IST Friday); portfolio.json committed to `main`; HTML committed to `reports` branch via `GITHUB_TOKEN`
- `data/swing_portfolio.json` — runtime state on `main` branch (auto-created, committed by workflow)
- `requirements.txt` — append `pytz>=2024.1` if not present

## Tasks & Acceptance

**Execution:**
- [ ] `swing_trader/__init__.py` — create empty package marker
- [ ] `swing_trader/config.py` — define CAPITAL, MAX_TRADE_PCT, MAX_RISK_PCT, MIN_RR, MAX_POSITIONS, ENTRY_START/END, WATCHLIST, DATA_DIR, PORTFOLIO_FILE, REPORTS_DIR
- [ ] `swing_trader/scanner.py` — implement scan_entry with EMA-20 trend filter, ATR-14 SL, FVG zone, BSL/SSL targets, RR gate
- [ ] `swing_trader/portfolio.py` — implement load/save/open_position/close_position/update_unrealised; atomic JSON writes
- [ ] `swing_trader/sizing.py` — implement calc_quantity with two-rule min and available-capital check
- [ ] `swing_trader/report.py` — dark-theme HTML builder; save_report writes to `data/reports/`; no external dependencies
- [ ] `swing_trader/runner.py` — daily scan at 09:30 (once/day), hourly position update, Friday 15:00 report trigger; 60 s poll loop
- [ ] `swing_trader/__main__.py` — call main_loop(); print banner on start; `--dry-run` flag exits after scan summary
- [ ] `.github/workflows/swing-trader.yml` — `scan` job: checkout, pip install, run scan, commit portfolio.json back to main; `report` job (Friday only): build HTML, switch to orphan `reports` branch, commit HTML, push via GITHUB_TOKEN
- [ ] `requirements.txt` — add pytz if missing
- [ ] `.gitignore` — add `data/swing_portfolio.json` (managed by Actions, not manually committed)

**Acceptance Criteria:**
- Given runner starts before 09:30 IST on a weekday, when 09:30 passes, then `run_daily_scan()` is called exactly once that calendar day.
- Given a signal with RR = 1.8, when scanner returns it, then runner skips it and logs "RR gate failed (1.80 < 2.0)".
- Given a signal with RR = 2.4 and sufficient capital, when `run_daily_scan()` runs, then `data/swing_portfolio.json` contains a new open position with correct qty, entry, stop_loss, target, status=OPEN.
- Given an open position whose daily low breached the SL overnight, when `update_positions()` runs next, then the position moves to closed_trades with exit_reason=SL_HIT and capital is credited correctly.
- Given the Friday GH Actions `report` job runs, then an HTML file appears committed to the `reports` branch and is accessible at `github.com/<user>/<repo>/blob/reports/data/reports/SWING-WEEKLY-<date>.html`.
- Given `python -m swing_trader`, then the process starts, prints a startup banner showing capital and watchlist count, and enters the main loop without crashing.

## Design Notes

**FVG detection (daily bars):**
```python
# bar[i].Low > bar[i-2].High  ← bullish gap = FVG
if df['Low'].iloc[i] > df['High'].iloc[i-2]:
    fvg_low  = df['High'].iloc[i-2]
    fvg_high = df['Low'].iloc[i]
    entry    = (fvg_low + fvg_high) / 2
```

**Two-rule position sizing:**
```python
qty = min(
    int(capital * MAX_RISK_PCT / risk_per_share),  # 2% risk rule  → ₹2,000
    int(capital * MAX_TRADE_PCT / entry),           # 25% cap rule → ₹25,000
)
```

**Reports branch — no external accounts needed:**

The `reports` branch is an orphan (no shared history with `main`). The workflow uses the auto-injected `GITHUB_TOKEN` — zero setup, zero secrets to manage.

```yaml
# .github/workflows/swing-trader.yml (report job, Friday 15:15 IST = 09:45 UTC)
- name: Build report
  run: python -c "from swing_trader.report import save_report; from swing_trader.portfolio import load; save_report(build_html(load()), 'SWING-WEEKLY-${{ env.DATE }}.html')"

- name: Push to reports branch
  run: |
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git fetch origin reports 2>/dev/null || git checkout --orphan reports
    git checkout reports 2>/dev/null || true
    git add data/reports/
    git commit -m "report: weekly P&L ${{ env.DATE }}"
    git push origin reports
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

**Access the report:** `github.com/<user>/<repo>/blob/reports/data/reports/SWING-WEEKLY-<date>.html`

**Optional GitHub Pages:** Repo Settings → Pages → Source: `reports` branch → `/` root. Every weekly report becomes a live URL at `https://<user>.github.io/<repo>/data/reports/SWING-WEEKLY-<date>.html` — bookmarkable, mobile-friendly, no login.

**portfolio.json commit-back (scan job):**
```yaml
- name: Commit portfolio state
  run: |
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add data/swing_portfolio.json
    git diff --cached --quiet || git commit -m "chore: swing portfolio update ${{ env.DATE }}"
    git push origin main
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## Verification

**Commands:**
- `python -m swing_trader --dry-run` — expected: prints banner + scan summary, exits 0, no positions written.
- `python -c "from swing_trader.scanner import scan_entry; print(scan_entry('JSWSTEEL.NS'))"` — expected: signal dict or None, no exception.
- `python -c "from swing_trader.portfolio import load; print(load()['capital'])"` — expected: 100000.0 on fresh state.
