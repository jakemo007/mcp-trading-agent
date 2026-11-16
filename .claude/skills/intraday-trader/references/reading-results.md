# Reading the results

Field-by-field guide to everything `intraday_trader` returns or writes. Use it to find the right field before you quote a number, and to translate the status codes into plain words.

The shapes are defined in `intraday_trader/models.py` (dataclasses at the top, dict shapes in the comment block at the bottom) and in the docstrings of `runner.py`, `engine.py` and `portfolio.py`. If a field here is missing from the JSON you are reading, trust the JSON and those files.

## Contents

1. Conventions
2. What the two commands print
3. DayResult: one replayed session
4. WatchItem: a stock in play
5. Bias: the trend reads
6. SignalRecord: a setup, traded or not
7. Trade: one completed round trip
8. PortfolioState: the paper account
9. Level names
10. Printing a result without the chart candles
11. Worked example: explaining a trade

## 1. Conventions

- **Times** are ISO strings in IST, for example `2026-11-13T10:40:00+05:30`. Clock-only values are `HH:MM`.
- **A time is the start of a 5-minute candle.** A candle stamped 10:40 covers 10:40 to 10:45 and is known only at 10:45. So `signal_time` 10:40 means the setup was complete at 10:45 and the order could fill from the 10:45 candle onward. When you tell the story, say "in the 10:40 candle", not "at 10:40 exactly".
- **Prices** are rupees per share, two decimals. **Amounts** (`exposure`, `margin`, `risk_inr`, every `*_pnl`, `charges`, capital) are rupees.
- **Direction** is `LONG` or `SHORT`. **Bias** is `BULL`, `BEAR` or `NEUTRAL`.
- **BSL** is buy-side liquidity (stop orders above a high). **SSL** is sell-side liquidity (stop orders below a low).
- **R** is the money lost if the stop is hit, before charges. `r_multiple` is the net result divided by that.

## 2. What the two commands print

With `--json`, stdout is one JSON document.

### `python -m intraday_trader run --json`

| Field | Present when | Meaning |
|---|---|---|
| `status` | always | `ok`, `already_recorded`, `no_data`, or `error` |
| `date` | `ok`, `already_recorded`, and `no_data` when a date was asked for | The session, `YYYY-MM-DD` |
| `dry_run` | `ok` | True when `--dry-run` was passed; the portfolio file was not changed |
| `report_path` | `ok` | The HTML report |
| `json_path` | `ok` | The DayResult file (section 3). Read this for the trades |
| `start_capital`, `end_capital` | `ok` | Paper capital before and after the session |
| `net_pnl` | `ok` | The day's result after charges |
| `n_trades`, `wins`, `losses` | `ok` | Completed trades and how they ended |
| `watchlist` | `ok` | Number of stocks that were in play (a count, not the list) |
| `partial` | `ok` | True when the session had fewer than 60 five-minute candles, or the run was made on the session's own day before the 15:30 close. The result is provisional and the next run after the close replaces it |
| `error` | `error` | The exception message. The traceback is on stderr |

| `status` | Exit code | Plain meaning |
|---|---|---|
| `ok` | 0 | The session was replayed. Unless it was a dry run, the portfolio now includes it |
| `already_recorded` | 0 | The latest session with data is already recorded as a full day. Nothing was written. Normal on weekends and holidays. `--force` re-runs it |
| `no_data` | 2 | Nothing could be downloaded (no `date`), or there are no candles for the requested day (`date` present) |
| `error` | 1 | The program crashed |

A given `--date` is always replayed, recorded or not. Without `--dry-run` it replaces that day's record, and the capital of every later day is recalculated from it.

### `python -m intraday_trader backtest --days N --json`

| Field | Meaning |
|---|---|
| `status` | `ok` or `no_data` (nothing usable was downloaded) |
| `requested_days`, `actual_days` | Sessions asked for and sessions replayed. They differ when Yahoo's 60-day window is too short. While the market is open, today's unfinished session is left out |
| `first_date`, `last_date` | The period covered |
| `report_path` | The HTML backtest report |
| `json_path` | A file with `summary`, `days` (same rows as PortfolioState `days`) and `trades` (no chart candles) |
| `summary` | The totals below |

`summary` is `portfolio.summarize` of the backtest's own fresh portfolio:

| Field | Meaning |
|---|---|
| `sessions` | Days replayed |
| `n_trades`, `wins`, `losses`, `win_rate_pct` | Trade counts. A win is a trade with net P&L above zero |
| `gross_pnl`, `charges`, `net_pnl` | Totals. `net_pnl` is after the flat charge on every trade |
| `net_pnl_pct` | Net P&L as a percentage of the starting capital |
| `capital` | Capital at the end |
| `avg_win`, `avg_loss` | Average net result of winning and losing trades |
| `profit_factor` | Total won divided by total lost. `null` when there were no losing trades: show a dash, not infinity |
| `expectancy_inr` | Average net rupees per trade |
| `avg_r` | Average `r_multiple` per trade |
| `max_drawdown_inr`, `max_drawdown_pct` | Largest fall from a peak in end-of-day capital |
| `best_day`, `worst_day` | `{date, net_pnl}` each, or `null` |
| `equity` | `[{date, capital}]`, one point per session |

## 3. DayResult: one replayed session

Written to `data/reports/INTRADAY-DAILY-<date>.json` (with `-DRYRUN` before `.json` for a dry run). The GitHub Action publishes the same file on the `reports` branch; dry runs are never published.

| Field | Meaning |
|---|---|
| `date`, `generated_at` | The session, and when the replay ran |
| `partial` | True when the data stopped early (fewer than 60 candles), or the run was made before that day's close. A note in `notes` says which |
| `universe_size` | Symbols in `intraday_trader/universe_fno.csv` |
| `scanned` | Symbols that returned usable data. A gap between the two means Yahoo returned nothing for some symbols |
| `start_capital`, `end_capital` | Capital before and after. `end = start + net_pnl` |
| `gross_pnl`, `charges`, `net_pnl` | The day's totals |
| `n_trades`, `wins`, `losses` | Completed trades |
| `watchlist` | List of WatchItems (section 4), in the order they joined |
| `signals` | Every SignalRecord (section 6), traded or not, in time order |
| `trades` | Every Trade (section 7), in exit order, each with a `chart` |
| `notes` | Sentences about the day as a whole, such as the daily loss limit being hit or the watchlist being empty |
| `config` | The settings in force for this run. When explaining an older day, quote rules from here, because `config.py` may have changed since |

A day with an empty `watchlist` or no `trades` is a normal outcome. Say so, using the `notes`.

## 4. WatchItem: a stock in play

| Field | Meaning |
|---|---|
| `symbol`, `name` | NSE symbol and company name |
| `eligible_from` | The checkpoint (`10:15`, `11:15`, `12:15` or `13:15`) at which it first qualified. Signals are looked for from then on. `as_of` repeats the same time |
| `rank` | Its position in that checkpoint's ranking by relative volume |
| `rvol` | Relative volume at that checkpoint: volume so far today divided by the usual volume by the same time. It is not updated later in the day |
| `cum_volume`, `avg_cum_volume` | The two numbers behind `rvol` |
| `turnover_cr` | Value traded so far that day, in crore rupees |
| `last_price`, `prev_close`, `day_change_pct` | Price at the checkpoint, yesterday's close, and the change between them |
| `bias` | The trend reads (section 5) that mattered for this stock: the read behind its last signal if it had one, otherwise the last read taken inside the entry window, otherwise the read at the end of the session. For a trade, use the trade's own `bias` |
| `status` | See below |
| `note` | One sentence explaining the status |

| `status` | Plain meaning |
|---|---|
| `TRADED` | At least one trade was taken in this stock |
| `SIGNAL_NOT_FILLED` | A setup formed but no trade followed. The reason is on the matching entry in `signals`, and the `note` repeats it |
| `NO_SETUP` | A direction was allowed at some point, but no 5-minute candle completed all six entry checks. Also used when the stock joined too late for any candle inside the entry window, or had no candles that day; the `note` says which |
| `NOT_ALIGNED` | At every candle checked, the three timeframes did not give a direction, so the program stood aside |

## 5. Bias: the trend reads

`bias` holds three reads and the decision made from them.

| Field | Meaning |
|---|---|
| `weekly`, `daily`, `hourly` | One read each (below) |
| `direction` | `LONG`, `SHORT`, or `null` for stand aside |
| `aligned` | How many of the three timeframes agree with `direction`: 2 or 3, or 0 when there is no direction |
| `reason` | The decision in words |

Each read:

| Field | Meaning |
|---|---|
| `timeframe` | `W`, `D` or `H1` |
| `bias` | `BULL`, `BEAR` or `NEUTRAL`: the direction of the most recent break of structure on that timeframe |
| `reason` | One sentence with the real numbers. Quote it |
| `last_bos_time`, `last_bos_level` | When structure last broke, and the swing price that was closed beyond |
| `swing_high`, `swing_low` | The latest swing points that no candle has closed beyond yet |

The rule: `LONG` when the 1-hour and daily are both `BULL` and the weekly is not `BEAR`. `SHORT` is the mirror. Anything else is no direction.

## 6. SignalRecord: a setup, traded or not

A signal is produced when a 5-minute candle completes a setup. It becomes a limit order from the next candle.

| Field | Meaning |
|---|---|
| `symbol`, `name`, `direction` | What and which way |
| `signal_time` | Start of the candle that completed the 5-minute gap |
| `contexts` | Why price was worth watching there. See the tags below. Never empty |
| `context_time` | Start of the earliest candle that swept the level or tapped the 1-hour gap |
| `swing_extreme` | The lowest low (long) or highest high (short) between the context candle and the signal candle. The stop is measured from it |
| `mss_level` | The 5-minute swing high (long) or low (short) that price closed beyond: the structure shift |
| `fvg` | The 5-minute gap that is the entry zone: `kind` (`BULL` or `BEAR`), `top`, `bottom`, `mid`, `created_time`, `known_at` |
| `entry` | The limit price. With the default setting it is `fvg.mid` rounded to paise. Entry, stop and target are rounded to two decimals before `risk_per_share` and `rr` are worked out, so those two match the stored prices exactly |
| `stop` | The stop price actually used |
| `structural_stop` | The stop the chart alone would give (0.05% beyond `swing_extreme`). When it differs from `stop`, the stop was moved out to the 0.30% minimum distance so that brokerage stays small against the risk |
| `target`, `target_kind` | The target price and what it is. See below |
| `risk_per_share`, `rr` | Distance from entry to stop, and planned reward divided by that risk |
| `sweep_level` | The level that was swept: `name`, `price`, `side`, `time`. `null` when the context was only a 1-hour gap |
| `h1_fvg` | The 1-hour gap that was tapped, same shape as `fvg`. `null` when the context was only a sweep |
| `story` | The setup as ordered sentences with the real numbers |
| `rvol`, `bias` | Copied from the stock's watchlist entry and trend reads at signal time |
| `status`, `skip_reason` | What became of the order. See below. `skip_reason` is `null` for `FILLED` and a sentence for every other status |
| `resolved_time` | Start of the candle on which the status was decided: the fill, the refusal, the expiry or the invalidation |
| `trade_id` | The `id` of the Trade, present only when `status` is `FILLED` |

| Context tag | Plain meaning |
|---|---|
| `SWEEP:<level>` | Price traded beyond that level (below a low for a long, above a high for a short) for the first time that day, and was back on the right side of it when the signal formed. `SWEEP:PDL` is a sweep of the previous day's low |
| `H1_FVG` | Price dipped into a still-valid 1-hour fair value gap in the trade's direction without closing through it |

A signal can carry both tags.

| `status` | Plain meaning |
|---|---|
| `FILLED` | Price came back to the entry and the trade was opened. There is a matching Trade |
| `EXPIRED` | Price did not return to the entry within 6 candles (30 minutes), or the order was still waiting at 14:30. `skip_reason` says which: "not filled within 30 minutes" or "entry window closed at 14:30" |
| `INVALIDATED` | Before the order filled, a candle opened at or beyond the stop (`skip_reason` "price opened through the stop"). A fall straight through the gap fills the order first, so that case shows up as a `FILLED` signal and a `STOP` trade, not here |
| `SKIPPED` | Price reached the entry, but an account rule refused the trade. `skip_reason` holds the sentence, which you quote as written: 5 positions already open, 8 trades already taken that day, the daily loss limit, no free margin, a share price above the ₹30,000 per-trade limit, or a stop too wide for the 1% risk limit. A slot or margin freed by another trade closing in the same candle does not count until the next candle, so a `SKIPPED` can carry the same time as a close |

| `target_kind` | Plain meaning |
|---|---|
| a level name such as `PDH` or `PDL` | The nearest liquidity level at least 2R away. Section 9 lists the names |
| `CAP_4R` | The nearest suitable level was more than 4R away, so the target was pulled in to 4R |
| `FIXED_2R` | No liquidity level was at least 2R away, so the target is a plain 2R |

The numbers in `CAP_4R` and `FIXED_2R` come from `MAX_RR` and `FALLBACK_RR` in `config.py`, so they change if those settings do.

## 7. Trade: one completed round trip

A Trade carries the signal's `contexts`, `target_kind`, `fvg`, `sweep_level`, `h1_fvg`, `story`, `rvol` and `bias`, plus:

| Field | Meaning |
|---|---|
| `id` | `<date>-<symbol>-<n>`, for example `2026-11-13-RELIANCE-1` |
| `date`, `symbol`, `name`, `direction` | Which session, what, which way |
| `context_time` | Start of the candle that swept the level or tapped the 1-hour gap: the reason to look. The chart starts 12 candles before it |
| `signal_time`, `entry_time`, `exit_time` | When the setup completed, when the order filled, when the trade closed |
| `entry`, `stop`, `target`, `exit_price` | The four prices. `entry` is the fill price; it can be better than the signal's limit when a candle opened beyond it |
| `qty` | Shares |
| `exposure` | `qty × entry`: the value of stock bought or sold |
| `margin` | `exposure / 3`: the user's own money blocked |
| `risk_inr` | `qty × distance from entry to stop`: 1R in rupees |
| `rr_planned` | The signal's `rr` |
| `exit_reason` | See below |
| `gross_pnl` | Before charges |
| `charges` | The flat brokerage, ₹25 by default |
| `net_pnl` | `gross_pnl − charges` |
| `r_multiple` | `net_pnl / risk_inr` |
| `outcome` | `WIN` when `net_pnl` is above zero, otherwise `LOSS`. A small gross gain that brokerage wipes out is a `LOSS` |
| `chart` | Only inside a DayResult: `{"bars": [[time, open, high, low, close], ...]}`, the candles around the trade. Used by the HTML report. Strip it before printing |

| `exit_reason` | Plain meaning |
|---|---|
| `TARGET` | A candle reached the target |
| `STOP` | A candle reached the stop. Also used when one candle touched both stop and target, because a 5-minute candle cannot say which came first and the simulation assumes the worse |
| `EOD` | Still open at 15:15 and squared off at the last price before then. The result can be a gain or a loss. If the stock's 5-minute data ended earlier, the exit is at the last candle there is; when that last candle started before 15:00, the trade's story and a line in the day's `notes` say so |

A stopped trade loses more than 1R after charges (the worked example in the guide is −1.21R). That is expected, not an error.

## 8. PortfolioState: the paper account

`data/intraday_portfolio.json`. Committed to `main` by the GitHub Action.

| Field | Meaning |
|---|---|
| `initial_capital` | ₹1,00,000 unless the setting was changed |
| `capital` | Capital after the latest recorded session |
| `updated_at` | When it was last written |
| `days` | One row per session, sorted by date: `date`, `start_capital`, `end_capital`, `gross_pnl`, `charges`, `net_pnl`, `n_trades`, `wins`, `losses`, `partial` |
| `trades` | Every Trade, without `chart`, sorted by date then exit time |

There is no watchlist and no record of untraded signals here. Those exist only in a DayResult.

## 9. Level names

| Name | What it is | Side |
|---|---|---|
| `PDH` / `PDL` | Previous day's high / low | BSL / SSL |
| `PWH` / `PWL` | Previous completed week's high / low | BSL / SSL |
| `ORH` / `ORL` | Opening range high / low: the extremes of 09:15 to 10:15 | BSL / SSL |
| `HOD` / `LOD` | The day's high / low so far. Used as targets only, never as a sweep | BSL / SSL |
| `H1_SH` / `H1_SL` | A 1-hour swing high / low that no hourly candle has closed beyond | BSL / SSL |
| `D_SH` / `D_SL` | A daily swing high / low that no daily candle has closed beyond | BSL / SSL |
| `M5_SH` / `M5_SL` | One of the day's own 5-minute swing highs / lows. Used as a sweep only, never as a target | BSL / SSL |

A long sweeps a low (SSL) and targets a high (BSL). A short sweeps a high and targets a low.

## 10. Printing a result without the chart candles

Reading a DayResult whole spends most of the output on candle arrays. Print it without them:

```bash
PYTHONIOENCODING=utf-8 python - data/reports/INTRADAY-DAILY-2026-11-13.json <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for t in d.get("trades", []):
    t.pop("chart", None)
print(json.dumps(d, indent=1, ensure_ascii=False))
EOF
```

For one stock, add a symbol and print only its rows:

```bash
PYTHONIOENCODING=utf-8 python - data/reports/INTRADAY-DAILY-2026-11-13.json RELIANCE <<'EOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
sym = sys.argv[2].upper()
for t in d.get("trades", []):
    t.pop("chart", None)
out = {key: [row for row in d.get(key, []) if row.get("symbol") == sym]
       for key in ("watchlist", "signals", "trades")}
out["notes"] = d.get("notes", [])
print(json.dumps(out, indent=1, ensure_ascii=False))
EOF
```

Three empty lists mean the stock was never on the watchlist that day.

For the trades of one date from the portfolio file, when there is no DayResult:

```bash
PYTHONIOENCODING=utf-8 python - data/intraday_portfolio.json 2026-11-13 <<'EOF'
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
day = [r for r in s.get("days", []) if r.get("date") == sys.argv[2]]
trades = [t for t in s.get("trades", []) if t.get("date") == sys.argv[2]]
print(json.dumps({"day": day, "trades": trades}, indent=1, ensure_ascii=False))
EOF
```

These snippets only select and print. Do not add arithmetic to them.

## 11. Worked example: explaining a trade

This uses the made-up ABC trade from section 9 of `docs/intraday-trader/README.md`. The date and the stock are invented; the point is the mapping from fields to sentences. A real Trade has the same fields.

The trade, trimmed:

```json
{
  "id": "2026-11-13-ABC-1",
  "symbol": "ABC", "direction": "LONG",
  "contexts": ["SWEEP:PDL"],
  "sweep_level": {"name": "PDL", "price": 610.20, "side": "SSL"},
  "h1_fvg": null,
  "fvg": {"timeframe": "M5", "kind": "BULL", "bottom": 611.60, "top": 612.60, "mid": 612.10},
  "signal_time": "2026-11-13T10:40:00+05:30",
  "entry_time": "2026-11-13T10:50:00+05:30",
  "exit_time": "2026-11-13T12:40:00+05:30",
  "entry": 612.10, "stop": 609.65, "target": 618.90, "exit_price": 618.90,
  "target_kind": "PDH", "rr_planned": 2.78,
  "qty": 49, "exposure": 29992.90, "margin": 9997.63, "risk_inr": 120.05,
  "exit_reason": "TARGET",
  "gross_pnl": 333.20, "charges": 25.0, "net_pnl": 308.20, "r_multiple": 2.57,
  "outcome": "WIN", "rvol": 2.30,
  "bias": {
    "direction": "LONG", "aligned": 3,
    "weekly": {"bias": "BULL", "reason": "Closed above the 598.00 swing high; last break of structure is up."},
    "daily":  {"bias": "BULL", "reason": "Closed above the 607.50 swing high on 2026-11-10; last break of structure is up."},
    "hourly": {"bias": "BULL", "reason": "Closed above the 611.20 swing high; last break of structure is up."}
  },
  "story": ["..."]
}
```

How to say it:

> **ABC, long, paper trade on 13 November 2026.**
>
> 1. **In play.** ABC joined the watchlist with relative volume 2.30: it had traded 2.3 times its usual volume for that time of day.
> 2. **Trend.** Weekly, daily and 1-hour structure were all bullish (3/3 aligned). On the daily chart it had closed above the 607.50 swing high on 10 November. So the program looked for longs only.
> 3. **Reason to look.** Price traded below the previous day's low, 610.20, and came back above it. That is a sweep of sell-side liquidity: the stop orders resting under yesterday's low were triggered, and price did not stay down there.
> 4. **Entry.** The move back up left a 5-minute fair value gap from 611.60 to 612.60. The setup was complete with the 10:40 candle. A buy limit went in at the middle of the gap, 612.10, and filled in the 10:50 candle when price dipped back into it.
> 5. **Risk.** The stop was 609.65, just under the low of the sweep. With 49 shares, a ₹29,992.90 position on ₹9,997.63 of margin, the risk was ₹120.05.
> 6. **Target.** The previous day's high, 618.90, a planned 2.78 times the risk.
> 7. **Result.** The target was reached in the 12:40 candle. Gross +₹333.20, charges ₹25.00, net **+₹308.20**, which is +2.57R.
>
> What it teaches: the entry was not the dip below yesterday's low but the evidence that the dip had failed, a fast move back up that left a gap. Buying the middle of that gap, not the breakout candle, is what kept the risk to ₹120.05 and made a 2.78R target reachable.
>
> *This is an educational analysis, not financial advice. Trade at your own risk.*

What made this explanation safe:

- Every number is a field of the trade. Reading `rvol` 2.30 as "2.3 times" is the only rewording. If you want the stop distance per share, take `risk_per_share` from the matching entry in `signals`; do not subtract prices yourself.
- The sweep is described as what price did. Nothing is claimed about who bought.
- It says "paper trade" at the top.
- The teaching paragraph adds meaning, not new numbers.

When the trade is a loss, use the same seven steps and the same tone. For step 7 say which exit it was and what it cost, for example "The stop at 609.65 was reached in the 11:05 candle. Gross −₹120.05, charges ₹25.00, net −₹145.05, which is −1.21R." Then say what the method could not have known at entry, without rewriting history to make the loss look avoidable.
