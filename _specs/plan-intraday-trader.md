---
title: 'Intraday Paper Trader: build plan and handoff'
type: 'feature'
created: '2026-11-16'
status: 'awaiting-user'
owner_request: 'user, 2026-11-15'
---

# Intraday Paper Trader: plan and handoff

Read this first if you are picking the work up. It says what the user asked for, what is already built and proven, what is left, and the rules for working in this repository. It was written early on 2026-11-16 and updated the same afternoon after T1 to T8 were carried out. Check `git status` before trusting the "current state" section.

## 1. What the user asked for

In the user's words, condensed:

- A **skill for intraday trading on NSE**: find the stocks with high volume that day, then trade them using FVG and ICT concepts, based on the one-hour chart.
- Analyse the **1-hour, daily and weekly** charts; take trades on the **5-minute** chart.
- **₹1,00,000** starting capital. **₹25 brokerage per trade.** At most **₹10,000 of capital per trade**, with **3x intraday leverage**, so ₹30,000 of stock per trade.
- A **daily report**. The user will run it as a **GitHub Action**, and it should publish to a **website like the existing swing reports site**.
- **Proper documentation with examples**, written so the user can learn the method.
- Use **multiple agents** to write the skill.

## 2. What was decided, and where it is written down

Do not re-derive these. The learning guide and `config.py` are the source of truth.

| Decision | Where |
|---|---|
| End-of-day **replay**, not live trading: after the close, walk the session candle by candle with no lookahead. GitHub's scheduler is too late and too irregular for 5-minute entries. | guide section 12 |
| Universe: about 213 NSE F&O stocks | `intraday_trader/universe_fno.csv` |
| Step 1, volume scan: time-of-day relative volume at 10:15, 11:15, 12:15, 13:15 | guide section 4, `volume.py` |
| Step 2, bias: last break of structure on weekly, daily, 1-hour. Trade only when 1-hour and daily agree and weekly does not oppose | guide section 5, `ict.structure_bias`, `ict.combine_bias` |
| Step 3, levels (PDH/PDL, PWH/PWL, opening range, swings) and 1-hour fair value gaps | guide section 6, `ict.liquidity_levels`, `ict.find_fvgs` |
| Step 4, 5-minute entry: sweep or 1-hour gap tap, displacement, structure shift, 5-minute FVG, limit order at the gap midpoint | guide section 7, `ict.detect_setup` |
| Step 5, orders and exits: 30-minute order life, stop / target / 15:15 square-off, pessimistic when a candle is ambiguous | guide section 8, `engine.run_day` |
| Money: ₹10,000 margin x 3, flat ₹25 per completed trade, max 5 open, max 8 per day, one per stock, 2% daily loss limit, 1% risk cap | guide section 2, `config.py`, `portfolio.py` |
| 1-hour candles are rebuilt from the 5-minute data on the NSE clock (09:15, 10:15, ...) | `data.to_hourly`, `data.hourly_asof` |
| Reports: HTML is published to the `reports` branch only; `main` gets only `data/intraday_portfolio.json` | `.gitignore`, workflow |
| One shared index page for intraday and swing reports | `intraday_trader/site_index.py`, both workflows |
| The Claude skill is a project skill, separate from the Agensi listings under `skills/` | `.claude/skills/intraday-trader/` |

## 3. Current state

All work is **uncommitted on `main`**. Nothing has been pushed. `data/intraday_portfolio.json` does not exist yet, on purpose: the first real run creates it.

### Files

| Path | What | State |
|---|---|---|
| `intraday_trader/config.py` | every tunable number | done |
| `intraday_trader/models.py` | dataclasses and the documented dict shapes (the contract) | done |
| `intraday_trader/data.py` | Yahoo download, cleaning, 1-hour and weekly resampling | done, tested |
| `intraday_trader/volume.py` | relative-volume scan and watchlist | done, tested |
| `intraday_trader/ict.py` | structure, levels, FVGs, the 5-minute setup | done, tested |
| `intraday_trader/portfolio.py` | sizing, charges, persistent state, summary | done, tested |
| `intraday_trader/engine.py` | the bar-by-bar session replay | done, tested |
| `intraday_trader/runner.py` | `run_live`, `run_backtest` (download, simulate, save, report). Since 2026-11-16: a live run made before that day's 15:30 close is recorded as partial, and a backtest leaves out today's unfinished session | done, tested |
| `intraday_trader/__main__.py` | CLI: `run`, `backtest`, `--json` | done, tested |
| `intraday_trader/report.py` | daily and backtest HTML, candlestick and equity SVGs | done, tested, fixtures checked in headless Chrome |
| `intraday_trader/site_index.py` | standalone index builder for the Pages site | done, tested |
| `tests/test_intraday_*.py` | 8 files, 300 tests | all pass |
| `docs/intraday-trader/README.md` | the learning guide | complete; section 11 has the real 13 Nov trades and the backtest |
| `docs/intraday-trader/img/*.svg` | five teaching diagrams, plus `real-angelone-2026-11-13.svg` and `real-pnb-2026-11-13.svg` | all viewed on light and dark pages; fixed |
| `.claude/skills/intraday-trader/SKILL.md` | the skill | written and reviewed |
| `.claude/skills/intraday-trader/references/*.md` | field guide and rules lookup | reviewed against the code on 2026-11-16 |
| `.github/workflows/intraday-trader.yml` | the Action | publish step simulated locally; **never run on GitHub** |
| `.github/workflows/swing-trader.yml` | one step changed to use the shared index builder | changed, YAML parses |
| `.gitignore` | ignores `data/reports/INTRADAY-*.html` and `.json` | done |

### Evidence so far

- `python -m unittest discover -s tests -p "test_intraday_*.py"`: **300 tests, OK** on Python 3.9.2 with pandas 2.2.3, and on Python 3.13.11 with pandas 3.1.1, numpy 2.5.5 and yfinance 1.8.0, the newest versions `requirements.txt` lets CI install as of 2026-11-16. A real replay of 13 Nov on those newest versions gave identical signals and trades.
- `tests/test_intraday_integration.py` runs the real pipeline on a seeded synthetic market and requires identical results when later sessions, or later candles of the same day, are removed. Since T7 it cuts the day right after every candle on which something happened and compares everything up to that candle. All 19 lookahead and hindsight mutations from the T7 audit now fail at least one test (section 4, T7); before T7, 13 of them passed the whole suite.
- Real data with the final code (T1, run 2026-11-16 around 12:15 IST): dry run for 2026-11-13: 213 symbols, 212 usable, 14 in play, 3 signals, 2 paper trades (ANGELONE target +₹390.86, PNB square-off −₹256.84; PAGEIND skipped, share price above the per-trade limit), net **+₹134.02**.
- 45-session backtest 2026-09-08 to 2026-11-13 (16 Nov left out because the market was open): 39 trades, 17 wins and 22 losses (43.6%), gross +₹1,950.99, charges ₹975.00, net **+₹975.99 (+0.98%)**, profit factor 1.3, expectancy ₹25.03 per trade, max drawdown ₹1,298.64 (1.29%). By direction: longs 14 trades, +₹1,318.33; shorts 25 trades, −₹342.34. By exit: 6 targets, 14 stops, 19 square-offs. 54 sessions were usable. 39 trades is too few to conclude anything.
- Both worked examples in the guide (sections 9 and 10) are reproduced exactly by tests in `test_intraday_ict.py`, `test_intraday_engine.py` and `test_intraday_portfolio.py`.

## 4. What is left

In order. Each task says how to know it is done. Status as of 2026-11-16, 15:30 IST: **T1 to T5 and T7 done, T6 done locally (the GitHub part needs the user), T8 reported to the user the same day.** What remains is the user's: section 6 and T6 steps 1 to 4.

### T1. Re-run on real data with the final code

**Done 2026-11-16.** Results are in section 3. Found and fixed on the way: run during market hours, a backtest replayed today's unfinished session as if complete, and a live run after about 14:15 would have recorded the unfinished day as full (60+ candles), so the post-close run would have answered `already_recorded`. `runner._in_progress` now marks such a day partial and the backtest leaves it out; three tests in `RunnerTests` cover it, and the clock is pinned in `setUp`.

```bash
PYTHONIOENCODING=utf-8 python -m intraday_trader run --dry-run --json > day.json 2> day.log
PYTHONIOENCODING=utf-8 python -m intraday_trader backtest --days 45 --json > bt.json 2> bt.log
```

Use `--dry-run` so the paper portfolio is not created by a local test. Outputs land in `data/reports/` (git-ignored). Yahoo keeps 60 days of 5-minute candles, so the backtest window moves forward every day.

Done when: both exit 0 and you have the new summary numbers written down for T2 and the final report.

### T2. Fill section 11 of the guide with real trades

**Done 2026-11-16.** Sections 11.1 to 11.4: ANGELONE, PNB, PAGEIND and the backtest table. Every figure was checked by script against the two JSON files; the only numbers not in them are labelled arithmetic (0.43%, 1.07%, 39 × ₹50). The two chart files are `report.trade_chart_svg` output with `width="760" height="320"` set on the root, because a percentage width does not size reliably inside a markdown `<img>`.

`docs/intraday-trader/README.md` contains the marker `<!-- REAL-EXAMPLES -->` under "## 11. Real trades from the replay". Replace the marker with:

1. Two or three real trades taken from a DayResult JSON (`data/reports/INTRADAY-DAILY-<date>-DRYRUN.json`): one that hit its target, one that lost or was squared off, and one signal that was **not** traded with its reason. On 2026-11-13 these were ANGELONE (target), PNB (squared off at a loss) and PAGEIND (skipped: share price above the ₹30,000 per-trade limit).
2. For each trade: the weekly, daily and 1-hour reads, the `story` sentences as the program wrote them, a small money table, and its chart. Make the chart with `report.trade_chart_svg(trade)`, which returns a complete standalone SVG, and save it as `docs/intraday-trader/img/real-<symbol>-<date>.svg`.
3. A short table of the T1 backtest summary, with the date range, the trade count, and a sentence that this sample is too small to prove anything.

Every number must be copied from the JSON. Do not round differently from the JSON and do not invent a level.

Done when: the marker is gone, the images render on GitHub, and each figure in the section can be found in the JSON.

### T3. Look at the five teaching diagrams

**Done 2026-11-16.** All five parse and their numbers match the guide. Fixed: `fvg.svg`, `structure-bos.svg` and `entry-model.svg` drew up candles filled and down candles hollow, the reverse of every report chart; they now use up = hollow, down = filled. Check 1's text in `entry-model.svg` ran into check 4, and the "close" label in `structure-bos.svg` overlapped the next candle; both moved or shortened.

`docs/intraday-trader/img/day-timeline.svg`, `structure-bos.svg`, `fvg.svg`, `top-down.svg`, `entry-model.svg`. The agent that drew them was cut off before it could check them. Headless Chrome is installed on this machine (the reports agent used it); wrap each SVG in a small HTML page with a white and a dark background, screenshot it, and read the PNG.

Check: each file parses as XML; the numbers match guide sections 3, 7 and 9 (FVG 611.60 to 612.60 with midpoint 612.10; bearish 1,482.10 to 1,483.50 with midpoint 1,482.80; entry-model candles from the table in section 9, PDL 610.20, stop 609.65, target 618.90, opening range high 615.40); no overlapping or clipped labels; legible on both backgrounds.

Done when: all five have been viewed and any defect fixed.

### T4. Look at the real reports

**Done 2026-11-16.** Daily and backtest reports viewed at 1280px and in a true 390px viewport (Chrome will not make a window narrower than about 500px, so the page was loaded in a 390px iframe). No defects. Wide tables and the charts scroll sideways on phones by design (`.tablewrap`, `.chart svg{min-width:700px}`). The index was built from the local `origin/reports` content plus the new files: 1 daily, 1 backtest, 7 swing, with `-DRYRUN` hidden, fine at both widths.

Screenshot `data/reports/INTRADAY-DAILY-<date>-DRYRUN.html` and `INTRADAY-BACKTEST-<date>.html` at 1280px and about 390px wide. The fixtures were checked; real data has not been. Look for long company names, five-digit prices, many watchlist rows, and days with no trades. Fix in `report.py` and keep `tests/test_intraday_report.py` green.

Also build the index once against a scratch folder to see it with real files:

```bash
python intraday_trader/site_index.py <scratch-site-dir> --repo jakemo007/mcp-trading-agent
```

### T5. Review the two skill reference files against the code

**Done 2026-11-16.** `reading-results.md` gained `name`, `resolved_time` and `trade_id` on signals, `context_time` on trades, the rounding note, the `skip_reason` wording for EXPIRED and INVALIDATED, the two extra `NO_SETUP` cases, and the new WatchItem `bias` meaning. `rules.md` gained the rounding and market-open notes. `SKILL.md`, the guide and `models.py` were corrected where they said a backtest covers at most about 45 sessions (54 were usable) and where they described `partial`.

`.claude/skills/intraday-trader/references/reading-results.md` and `rules.md` were written from the contracts before the code existed, and their author was cut off before the final read-through. Compare every field, status and rule with `models.py` (the comment block at the bottom), `engine.py`, `runner.py` and `config.py`. Already corrected: the meaning of `INVALIDATED` (only "opened at or beyond the stop"; there is no "closed through the gap" case) and the run-time note in `SKILL.md`.

Things that changed after those files were written, so check they are described correctly:

- Trades now carry `context_time`.
- Signal records carry `resolved_time`, and `trade_id` when filled.
- A WatchItem's `bias` is the read behind the stock's last signal, otherwise the last read taken inside the entry window.
- `ict.detect_setup` rounds entry, stop and target to paise before computing risk and RR.

### T6. Workflow: prove the publish step, then the first run on GitHub

**Local part done 2026-11-16.** The publish step's script was taken verbatim from the YAML (only `REMOTE_URL` redirected) and run against a local bare remote seeded from `origin/reports`. Three cases passed: a competing push between clone and push (attempt 1 rejected, attempt 2 published on top, both files kept), no `reports` branch yet (created as an orphan), and an identical re-run (no empty commit). The `-DRYRUN` file was never published. The commit-to-main loop is the same `pull --rebase` and push the swing workflow already runs daily. The CI library versions were tested too (section 3). Steps 1 to 4 below still need the user.

How the simulation was done, if it needs repeating: in a scratch folder with throwaway git repositories (a bare "remote" with a `reports` branch, and a second clone that pushes in between attempts). Never run it against the real remote.

The first real run needs things only the user can do or approve:

1. Commit and push to `main`. **Do not commit or push unless the user asks.** Scheduled workflows only run from the default branch.
2. On GitHub: Settings, Pages, source = branch `reports`, folder `/`. Settings, Actions, General, workflow permissions = read and write.
3. First run as a dry run: `gh workflow run "Intraday Trader" -f mode=daily -f dry_run=true`, then read the job summary and download the `intraday-reports` artifact.
4. Then a normal run, and check the site: `https://jakemo007.github.io/mcp-trading-agent/`.

CI installs the newest pandas allowed by `requirements.txt` (`pandas>=2.2.0`) on Python 3.11. The tests run as the first step of the job and will stop a broken build from publishing. The suite passes with pandas 3.1.1 (section 3).

### T7. Optional: an independent review

**Done 2026-11-16.** A fresh agent audited `ict.py`, `engine.py`, `volume.py` and the tests. It found no lookahead in the code. It reported four findings and a set of test gaps, all acted on:

- **A. Same-candle order across stocks (fixed).** A stop on one stock and a fill on another in the same candle were processed in watchlist order, so the fill could use the slot, margin or profit the stop had just freed, or ignore its loss for the daily limit. The result depended on the order the stocks were listed in. The engine now does all exits first and holds back what they free until the next candle (section 7). Four tests in `SameCandleTests`, each run with both watchlist orders.
- **B. Data that ends early (fixed).** A trade closed because Yahoo's data stopped at, say, 11:55 was described as a 15:15 square-off. It now says what happened, in the story and in the day's notes. Two tests in `DataEndTests`.
- **C. Drawdown label (fixed).** The drawdown is measured on end-of-day capital; the report tile, the CLI line and the guide now say so.
- **D. `MAX_TRADES_PER_SYMBOL` comment (fixed).** It said "one attempt per stock"; only a fill uses up the stock's one trade, so a stock can signal again after an order expires, is invalidated or is skipped. `config.py` and `rules.md` now say so.
- **Timing tests** in `test_intraday_ict.py` failed once under load. They gate the GitHub Action, so the limits were raised to 20 ms per `detect_setup` call and 50 ms per `find_fvgs` call; real figures are well under 1 ms.
- **Test gaps.** The auditor injected 19 lookahead and hindsight mutations; 13 passed the whole suite. Four changes close them, and all 19 now fail at least one test: the integration test cuts the day right after every candle on which something happened and compares everything up to that candle (catches the one-candle peeks in stops, targets, fills, square-off prices and the volume scan); an engine test checks what the engine hands the 1-hour and level helpers (catches a 1-hour read and levels that see future candles); an ICT test requires a 5-minute swing low to be breached after it formed; an engine test checks that an order is invalidated on the candle that opens through the stop, not one earlier. The mutation harness lived in the session scratchpad and is not in the repository.

After the fixes, both real runs were repeated at 15:26 IST on 2026-11-16, with the market still open so the backtest window was unchanged. The 13 Nov dry run and the 45-session backtest (2026-09-08 to 2026-11-13) came out identical to the earlier runs, trade for trade and in every summary figure: the same-candle case did not occur in that window. Guide section 11 therefore stands. The suite is 300 tests, OK on both Python 3.9.2 and the CI versions.

### T8. Report to the user

State plainly: what was built, the test count, the real-data results with their caveats, what is not modelled (guide section 12), and the open decisions in section 6 below.

## 5. Rules for working here

- **Do not touch the stash.** `stash@{0}` ("grow", on `feature/groww-live-trading`) holds the user's unfinished work: `skills/` (the Agensi build tooling), `docs/prds/`, `docs/research/`, `data/*.csv`, `data/lessons.md`, `data/paper_trading.json`, several `swing_trader/*.py` files and `.claude/settings.json`. Never pop, apply or drop it. Do not create files at those paths, or the user's `git stash pop` will fail.
- **Do not commit, push, or switch branches unless the user asks.**
- **Do not create `data/intraday_portfolio.json` by hand or with a non-dry local run** unless the user wants the paper portfolio to start from that session.
- `config.py` and `models.py` are the contract. If you change a rule, change the guide (`docs/intraday-trader/README.md`) and the skill references in the same pass, then run the tests.
- Keep the code Python 3.9 compatible (`from __future__ import annotations`, no `match`, no runtime `X | Y`). The default `python` here is 3.9.2; the console is cp1252, so prefix ad-hoc commands with `PYTHONIOENCODING=utf-8`. A Python 3.13 is at `C:\Users\gunuj\miniconda3\python.exe`.
- Run the whole suite after every change: `PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p "test_intraday_*.py"`.
- Never state a price, quantity or P&L that the program did not produce.
- Do not tune `config.py` to make the backtest look better. If the user asks for a change, change one value and report the backtest before and after.
- **Go easy on parallel agents.** Seven forked agents at once hit the account's 5-hour usage limit twice in about fifteen minutes and four of them died mid-task each time. Do small tasks inline; if agents are wanted, run one or two at a time.
- Text written so far uses plain sentences, no emoji and no em dashes. Keep new text consistent.

## 6. Decisions that belong to the user

Raise these in the final report; do not decide them.

1. **Brokerage.** `CHARGE_PER_TRADE = 25` is per completed trade, as the user wrote it. Most Indian brokers charge per order, which makes a round trip ₹50, and statutory charges add roughly ₹15 to ₹20 more on a ₹30,000 round trip. In the backtest, charges were half of the gross profit (₹975.00 of ₹1,950.99); at ₹50 a round trip the same 39 trades would have netted about ₹1.
2. **How often it trades.** The first 45-session replay took 39 trades, under one a day, and about half the sessions had none. The settings that most restrict it are the weekly veto in `ict.combine_bias`, `MAX_TRADES_PER_SYMBOL = 1` and `MIN_RVOL = 1.5`. Whether to loosen any of them is the user's call.
3. **Committing.** Everything is uncommitted on `main`. The user may prefer a feature branch and a pull request.
4. **Short selling.** `ALLOW_SHORT = True`. Real intraday shorting is not available in every stock every day. In the 45-session backtest the 25 shorts lost ₹342.34 net while the 14 longs made ₹1,318.33; too few trades to act on, but worth showing the user.

## 7. Things an implementer should know

Interpretations the agents made where the contract was silent. They are in the code and the tests; listed here so nobody "fixes" them by accident.

- **Relative volume** averages only over earlier sessions that have bars before the checkpoint, and `RVOL_MIN_SESSIONS` counts those. A session with a morning data hole is left out, not counted as zero.
- **`DOWNLOAD_RETRIES`** means retries after the first attempt. A chunk is retried only when the request fails or comes back mostly empty.
- **Order prices** are rounded to two decimals inside `ict.detect_setup` before risk and RR are computed. The minimum-distance stop is rounded away from the entry so it is never closer than 0.30%.
- **A sweep** counts only when it is the first trade through that level today, and that first breach is inside the 12-candle window.
- **A 1-hour gap tap** needs the 5-minute candle to start at or after the gap's `known_at`. This stops the candle that created the gap from counting as a tap of it.
- **Orders:** there is no "closed through the gap" cancellation. The limit sits inside the gap, so a fall through the gap fills the order first and the stop takes the loss.
- **Fixed 2R target** is used whenever no liquidity level is at least 2R away, including when nearer levels exist.
- **Constants defined in modules rather than `config.py`:** `data.BAR_MINUTES`, `data.HOUR_MINUTES`, `data.RETRY_BACKOFF_SEC`, `volume.CRORE`, `ict.M5_MINUTES`, `ict.MIN_DISPLACEMENT_BARS`, `engine.CHART_BARS_BEFORE`, `engine.CHART_BARS_AFTER`.
- **Yahoo quirks seen on real data:** sessions have 71 to 74 five-minute bars, never a clean 75; the 15:15 bar is often a stub; about one zero-volume bar per symbol per session; 5-minute highs and lows differ slightly from the daily bar, so PDH and PDL are taken from the 5-minute data.
- **High-priced stocks** (above ₹30,000 a share, for example PAGEIND) can appear on the watchlist and produce a signal that is then skipped. That is intended and shows in the report.
- **Runs during market hours.** `runner._in_progress(day)` compares with the IST wall clock through `runner._now_ist()`, which tests patch. The engine reads the clock only for the `generated_at` stamp, so its results stay reproducible.
- **Candle drawing convention** everywhere (reports and teaching diagrams): up = hollow, down = filled.
- **Same candle, different stocks.** Inside one 5-minute candle the engine first processes the exits of every stock, then the orders and signals of every stock. What a close frees (a position slot, margin, a profit) reaches fills only from the next candle; a loss counts against the daily limit at once (`_Book.release`, `_Book.realised_for_fills`). This keeps the result independent of the watchlist order, except that when two orders fill in the same candle and one slot is left, the stock higher on the watchlist gets it. `SameCandleTests` covers it.
- **Data that ends early.** If a stock's 5-minute data stops before the square-off, its open trade is closed at the last candle there is. When that candle started before 15:00 (`engine.REGULAR_SQUARE_OFF_FROM`), the story and the day's notes say so instead of calling it a square-off.
- **The 1% risk cap does not bind at the default settings.** A trade's exposure is at most ₹30,000 and its stop at most 1.2% away, so it risks at most ₹360, against a cap of ₹1,000 at ₹1,00,000. The cap starts to bite only below about ₹36,000 of capital.
- **Re-recording a past day** re-chains every later day's capital (`portfolio.record_day`) but does not re-simulate their trades, which were sized from the old capital. Sizing depends on capital only through free cash and the 1% cap, so this matters only when capital is low.
- **The drawdown** in the summary is measured on end-of-day capital, not trade by trade inside a day. The labels say so.

## 8. How to run things

```bash
# All tests (offline, about 40 seconds)
PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p "test_intraday_*.py"

# Replay the latest session without touching the portfolio
PYTHONIOENCODING=utf-8 python -m intraday_trader run --dry-run

# Replay a given session
PYTHONIOENCODING=utf-8 python -m intraday_trader run --date 2026-11-13 --dry-run --json

# Backtest
PYTHONIOENCODING=utf-8 python -m intraday_trader backtest --days 45

# Rebuild a site index in a folder that has data/reports/ in it
python intraday_trader/site_index.py <site-dir> --portfolio data/intraday_portfolio.json --repo jakemo007/mcp-trading-agent
```

The existing reports site is the `reports` branch (`index.html`, `.nojekyll`, `data/reports/SWING-WEEKLY-*.html`), served by GitHub Pages. Remote: `https://github.com/jakemo007/mcp-trading-agent.git`.
