---
name: intraday-trader
description: Runs and explains the standalone NSE intraday paper trader in this repository, the intraday_trader Python module. It scans about 210 F&O stocks for unusual volume, reads weekly, daily and 1-hour market structure, takes 5-minute fair value gap entries, and keeps a simulated ₹1,00,000 portfolio (₹10,000 margin per trade at 3x leverage, ₹25 per trade). Use when the user asks to run the intraday paper trader, do an intraday paper trade run, see or explain today's intraday trades, open the intraday report, backtest the intraday strategy, check the intraday paper portfolio, trigger the Intraday Trader GitHub Action, asks why a stock was skipped or not traded, or mentions the high-volume FVG intraday strategy. This is an end-of-day replay of a past NSE session on Yahoo Finance data. It is a paper-trading simulation, not live trading, and places no orders. Do not use for the CLAUDE.md commands intraday TICKER, demotrading or backtest intraday TICKER, which use the MCP server tools.
---

# Intraday Paper Trader

This repository contains a standalone Python module, `intraday_trader`, that replays one NSE session candle by candle after the close and trades it on paper. A run scans about 210 F&O stocks for unusual volume, reads the weekly, daily and 1-hour market structure of the stocks in play, looks for 5-minute fair value gap (FVG) entries in the agreed direction, and books the results into a simulated ₹1,00,000 portfolio: at most ₹10,000 of margin per trade at 3x leverage, ₹25 flat per completed trade. It then writes an HTML report.

Your job is to run it, read what it returned, and explain it. The module does all the measuring. You never work out a price level, a quantity or a P&L figure yourself.

It replays a session that has already happened, using Yahoo Finance candles. It places no orders and is not live trading.

The method is taught in `docs/intraday-trader/README.md`, with worked examples. Read it when the user wants a concept explained (liquidity sweep, FVG, break of structure, RVOL, R multiples).

## Do not confuse this with the CLAUDE.md commands

`CLAUDE.md` defines commands that drive the MCP server tools. They sound similar and share nothing with this skill.

| | CLAUDE.md commands | This skill |
|---|---|---|
| Asked for as | `intraday TICKER`, `demotrading`, `backtest intraday TICKER DAYS` | "intraday paper trader", "today's intraday trades", "backtest the intraday strategy" |
| Runs | MCP tools such as `get_intraday_ohlc`, `paper_trade_*`, `run_intraday_backtest` | `python -m intraday_trader`; no MCP tools |
| Scope | one ticker, or 25 Nifty stocks for `demotrading` | about 210 NSE F&O stocks, volume scan first |
| Money | R-multiples, or `data/paper_trading.json` (0.5% risk, ₹50 in and ₹50 out) | `data/intraday_portfolio.json` (₹10,000 margin at 3x, ₹25 per trade) |

A ticker straight after the word `intraday`, as in `intraday NIFTY`, is the CLAUDE.md command. Mentions of the paper trader, the volume scan, the daily report, the website or the GitHub Action mean this skill. If you cannot tell which is meant, ask. Never combine figures from the two portfolios.

## Before running anything

- Run from the repository root, otherwise `intraday_trader` cannot be imported.
- The module needs `yfinance`, `pandas`, `numpy` and `pytz`, all listed in `requirements.txt`. Check once per session with `python -c "import yfinance, pandas, numpy, pytz"`. If that fails, say which package is missing and ask before installing.
- A run downloads 5-minute and daily candles for about 210 symbols. On a normal connection a daily run or a 45-session backtest finishes in under a minute, but Yahoo can be slow or make the module retry, so set the command timeout to 10 minutes (600000 ms).
- With `--json`, stdout carries exactly one JSON document and the log goes to stderr. Send stderr to a file so the JSON stays clean, and read that log only when something went wrong. In PowerShell write `2>$env:TEMP\intraday-run.log` in place of `2>/tmp/intraday-run.log`.
- This Windows console is cp1252. The module switches its own output to UTF-8, but ad-hoc Python that prints `₹` needs `PYTHONIOENCODING=utf-8` in front of it or it raises UnicodeEncodeError.

## 1. Run a session

```bash
python -m intraday_trader run --json 2>/tmp/intraday-run.log
```

| Flag | Effect |
|---|---|
| none | Replays the latest session that has data and records it in the paper portfolio |
| `--date YYYY-MM-DD` | Replays that session and replaces any earlier record of it |
| `--dry-run` | Leaves the portfolio file alone; the report files get a `-DRYRUN` suffix |
| `--force` | Re-runs the latest session even though it is already recorded |

A normal run changes `data/intraday_portfolio.json`. Add `--dry-run` when the user only wants to see what would have happened, when re-checking a past date (Yahoo revises candles, so a re-run can differ from the day that was recorded), and during market hours (the replay would cover part of a session).

The JSON always has a `status`:

| `status` | Exit code | What happened | What to tell the user |
|---|---|---|---|
| `ok` | 0 | The session in `date` was replayed | The result, in the daily format below |
| `already_recorded` | 0 | The latest session with data (`date`) is already in the portfolio as a full day. Nothing was written. This is the normal answer on a weekend or holiday | There is no new session, and which date was the last one. Offer to explain that day, or to re-run it with `--force` |
| `no_data` | 2 | No `date` in the JSON: nothing could be downloaded. With `date`: Yahoo has no 5-minute candles for that day | No date: a Yahoo or network problem, try again later. With date: a holiday or weekend, a future date, or a day older than the roughly one month of 5-minute history a run fetches |
| `error` | 1 | The program crashed; `error` holds the message | The message, plus the traceback from the log file. Do not guess a result |

With `ok` the JSON also has `date`, `dry_run`, `report_path`, `json_path`, `start_capital`, `end_capital`, `net_pnl`, `n_trades`, `wins`, `losses`, `watchlist` (a count) and `partial`. The trades are in the file at `json_path`. Read it as described in section 2 before you describe any trade.

State the session date you are reporting. On a Saturday, "today's trades" are Friday's.

`partial: true` means the session was not complete when it was replayed: the run happened on that day before the 15:30 close, or Yahoo returned fewer than 60 five-minute candles. Say the result is provisional. The next plain run after the close replaces it.

The module does not open the report. Give the path, and open it with `Start-Process <path>` in PowerShell when the user asks to see it.

## 2. Explain a day's trades

Find the day's data in this order:

1. `data/reports/INTRADAY-DAILY-<date>.json`, or `INTRADAY-DAILY-<date>-DRYRUN.json` from a dry run. This is the full DayResult: the watchlist, every signal and every trade. It is on this machine only when the run was made here.
2. The same file on the `reports` branch, where the GitHub Action publishes it. In the Bash tool: `git fetch origin reports`, then `git show origin/reports:data/reports/INTRADAY-DAILY-<date>.json > /tmp/intraday-day.json`.
3. If neither exists, the trades with that `date` in `data/intraday_portfolio.json`. They carry the same story, bias and prices, but there is no watchlist and no record of signals that were not traded.

A DayResult is large because every trade embeds the candles for its chart. Do not Read the whole file. Print it without the charts using the snippet in `references/reading-results.md`.

Explain each trade in the order the method works, from the trade's own fields:

1. In play: `rvol`, the relative volume when the stock joined the watchlist.
2. Trend: `bias.weekly.reason`, `bias.daily.reason`, `bias.hourly.reason`, and `bias.aligned` out of 3.
3. Reason to look: `contexts`, with `sweep_level` (which level was taken out, and its price) or `h1_fvg` (the 1-hour gap that was tapped).
4. Entry: the 5-minute gap in `fvg` and the limit price `entry`.
5. Risk: `stop`, `qty`, `exposure`, `margin`, `risk_inr`.
6. Target: `target`, what it is (`target_kind`), and `rr_planned`.
7. Outcome: `exit_reason`, `exit_price`, `exit_time`, then `gross_pnl`, `charges`, `net_pnl` and `r_multiple`.

The `story` list holds these steps as sentences the program wrote from the real candles. Use it as the backbone and add the teaching around it: what a sweep is, why the order sits at the middle of the gap, why the stop is where it is. `references/reading-results.md` explains every field and code and has a complete worked example.

## 3. "Why was X not traded?"

This needs the DayResult (sources 1 and 2 in section 2), because the portfolio file records only trades. If there is none for the date, create one with `python -m intraday_trader run --date <date> --dry-run --json` (possible for about the last three weeks) and say that you re-ran the day.

Look X up in this order:

1. `watchlist`. If X is there, its `status` and `note` are the answer.
   - `NOT_ALIGNED`: the 1-hour and daily structure did not agree, or the weekly opposed them, at every candle that was checked. Quote the three reads from `bias`.
   - `NO_SETUP`: a direction was allowed, but no 5-minute candle completed all six entry checks.
   - `SIGNAL_NOT_FILLED`: there was a setup. Continue to step 2.
   - `TRADED`: it was traded. Explain the trade.
2. `signals` for X: `status` and `skip_reason`.
   - `EXPIRED`: price did not come back to the entry within 30 minutes, or it reached 14:30.
   - `INVALIDATED`: a candle opened at or beyond the stop before the order filled.
   - `SKIPPED`: price reached the entry but an account rule refused the trade. `skip_reason` says which.
3. X is not in `watchlist`. Either it was not in play at any checkpoint (relative volume under 1.5, under ₹5 crore traded, outside the top 10 by relative volume, or the list was already full), or it is not in the universe: check `intraday_trader/universe_fno.csv`. The DayResult keeps no figures for stocks that missed the list, so name the possible causes and do not state a relative volume for X.

## 4. Backtest

```bash
python -m intraday_trader backtest --days 30 --json 2>/tmp/intraday-backtest.log
```

This replays the last N sessions with a fresh ₹1,00,000 and never touches the live portfolio file. Yahoo keeps about 60 days of 5-minute candles and each session needs at least 5 earlier sessions as a volume baseline, so roughly 50 sessions is the most it can cover (54 on 16 November 2026). During market hours today's unfinished session is left out. When `actual_days` is less than `requested_days`, say so.

The JSON has `status` (`ok` or `no_data`), `requested_days`, `actual_days`, `first_date`, `last_date`, `report_path`, `json_path` and `summary`. Report the summary as it is. A losing backtest is a finding to state plainly, not something to soften. The trade list is in the file at `json_path`, under `trades`.

A few dozen sessions is a small sample. Say how many trades the figures rest on, and do not call the result proof that the strategy works or fails.

## 5. Portfolio status

Read `data/intraday_portfolio.json`: `initial_capital`, `capital`, `updated_at`, one row per session in `days`, and every trade in `trades`. You may add up and count those rows (total net P&L, wins out of trades); derive nothing else. Call it the paper portfolio. If the file does not exist, no session has been recorded yet.

## 6. Reports, the website and the GitHub Action

| What | Where |
|---|---|
| Daily report | `data/reports/INTRADAY-DAILY-<date>.html` and `.json` |
| Backtest report | `data/reports/INTRADAY-BACKTEST-<date>.html` and `.json` |
| Paper portfolio | `data/intraday_portfolio.json` |
| Website | the `reports` branch, served by GitHub Pages at `https://<owner>.github.io/<repo>/`; take owner and repo from `git remote get-url origin` |
| Automation | `.github/workflows/intraday-trader.yml`, Monday to Friday at about 16:00 IST |

The Action runs the same module, commits the portfolio file to `main` and pushes the report (HTML and JSON) to the `reports` branch. Its inputs are `mode`, `date`, `days`, `dry_run` and `force`. To start it by hand:

```bash
gh workflow run "Intraday Trader" -f mode=daily
gh workflow run "Intraday Trader" -f mode=daily -f date=2026-11-13 -f dry_run=true
gh workflow run "Intraday Trader" -f mode=backtest -f days=30
gh run list --workflow "Intraday Trader" --limit 3
```

Run `gh workflow run` only when the user asks for it, because it commits to `main` and publishes to a public site. A dry run through the Action commits and publishes nothing; its report is attached to the run as the `intraday-reports` artifact. If a command is rejected, check the input names in the workflow file, which is the source of truth. A workflow can be started this way only once it is on the default branch.

## Rules for talking about results

1. Every price, quantity, time and P&L figure you quote comes from the JSON. Someone may act on a level you state, so an estimate presented as a result can cost them money. Show a missing value as a dash.
2. Call it what it is: a paper trade, a simulated result, a replay of a past session. It is not live trading and it does not predict tomorrow.
3. Describe what price did, not who did it. "Price traded below yesterday's low and closed back above it" is in the data. Who was buying is not.
4. When a result looks good, name what the simulation leaves out: statutory charges (STT, exchange fees, GST, stamp duty), brokerage charged per order instead of per trade, slippage on stops, limit orders that are touched but not filled, 3x leverage and short selling not being available in every stock, and Yahoo data quality. Section 12 of the guide has the full table.
5. Report losses, skipped signals and empty days as plainly as wins. The record is useful only if it is complete.
6. Do not edit `intraday_trader/config.py` unless the user asks. When they do, change one value and suggest a backtest before and after, so the effect is measured. Tuning several values until the last 40 sessions look good fits the strategy to those sessions.
7. End every reply with: *This is an educational analysis, not financial advice. Trade at your own risk.*

## Reply formats

Show money with ₹, Indian digit grouping and two decimals (₹1,00,412.50), and a sign on every P&L figure. The HTML report groups digits the western way (₹100,412.50); the amounts are the same. Times are IST, shown as HH:MM. Fill every bracket from the JSON.

Daily run:

```
INTRADAY PAPER TRADER - NSE - [date] (simulated[, dry run][, partial session])
In play  : [watchlist count] stocks of [scanned] scanned
Trades   : [n_trades] ([wins] won, [losses] lost)    Signals not traded: [n]
Day P&L  : [+/-]₹[net_pnl] net (gross [+/-]₹[gross_pnl], charges ₹[charges])
Capital  : ₹[start_capital] -> ₹[end_capital]
---------------------------------------------------------------
[SYMBOL] [LONG/SHORT]  [contexts]  [aligned]/3 aligned
  Entry [entry]  Stop [stop]  Target [target] ([target_kind])  Qty [qty]
  Exit [exit_price] [exit_reason] at [HH:MM]  Net [+/-]₹[net_pnl] ([r_multiple]R)
(one block per trade, or "No trades today: [reason from notes]")
Not traded: [SYMBOL] [status] - [reason]    (one line each, or "none")
---------------------------------------------------------------
Report: [report_path]
```

Backtest:

```
INTRADAY PAPER BACKTEST - NSE - [first_date] to [last_date] ([actual_days] sessions, simulated)
Net P&L      : [+/-]₹[net_pnl] ([net_pnl_pct]% of starting capital)   Capital ₹[capital]
Trades       : [n_trades]   Win rate [win_rate_pct]%   Average [avg_r]R per trade
Expectancy   : [+/-]₹[expectancy_inr] per trade   Profit factor [profit_factor]
Charges      : ₹[charges]   Max drawdown ₹[max_drawdown_inr] ([max_drawdown_pct]%)
Best / worst : [best_day.date] [+/-]₹[net_pnl] / [worst_day.date] [+/-]₹[net_pnl]
Not modelled : statutory charges, per-order brokerage, slippage, missed limit fills
Report: [report_path]
```

One trade:

```
[SYMBOL] [LONG/SHORT] - paper trade on [date]
1. In play : relative volume [rvol] when it joined the watchlist
2. Trend   : weekly [bias], daily [bias], 1-hour [bias] ([aligned]/3 aligned)
3. Reason  : [the level swept or the 1-hour gap tapped, with its price]
4. Entry   : 5-minute gap [fvg.bottom] to [fvg.top], limit [entry], filled in the [HH:MM] candle
5. Risk    : stop [stop], [qty] shares, ₹[exposure] position, ₹[margin] margin, ₹[risk_inr] at risk
6. Target  : [target] ([target_kind]), planned [rr_planned]R
7. Result  : [exit_reason] at [exit_price] in the [HH:MM] candle. Gross [+/-]₹[gross_pnl], charges ₹[charges], net [+/-]₹[net_pnl] ([r_multiple]R)
```

Follow a single-trade block with two or three sentences on what the trade teaches.

## Reference files

- `references/reading-results.md`: every field, status and code in the JSON, the snippet for printing a DayResult without its chart candles, and a worked trade explanation. Read it before explaining a day for the first time in a session, and whenever a field or code is unfamiliar.
- `references/rules.md`: the strategy rules in compact form, each with the `config.py` constant that controls it and the guide section that teaches it. Read it when the user asks why the program did or did not do something, or asks what a setting does.
