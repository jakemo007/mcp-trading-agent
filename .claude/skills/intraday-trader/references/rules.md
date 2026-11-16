# Strategy rules: quick lookup

The rules the intraday paper trader follows, in compact form. Each rule names the constant in `intraday_trader/config.py` that controls it and the section of `docs/intraday-trader/README.md` (the guide) that teaches it.

`config.py` is the source of truth. If a value here differs from `config.py`, `config.py` is right and this file is out of date. For a past session, the values that were in force are in that DayResult's `config` field.

All rules are written for a long trade. A short trade is the exact mirror: highs for lows, above for below, `BEAR` for `BULL`. Shorts are taken only when `ALLOW_SHORT` is true.

## Contents

1. The session clock
2. Universe
3. Step 1: stocks in play
4. Step 2: trend on three timeframes
5. Step 3: levels and 1-hour gaps
6. Step 4: the six entry checks
7. Step 5: orders and exits
8. Money and account rules
9. What keeps the replay honest
10. Where each rule lives in the code
11. When the user wants to change a rule

## 1. The session clock

Guide section 1. All times IST.

| Time | What happens | Constant |
|---|---|---|
| 09:15 | Session opens. Watch only for the first hour | `SESSION_OPEN` |
| 10:15 | Opening range and first 1-hour candle complete. First volume scan. Signals may begin | `FIRST_HOUR_END`, `ENTRY_START` |
| 10:15, 11:15, 12:15, 13:15 | Volume scan checkpoints | `SCAN_CHECKPOINTS` |
| 14:30 | No new signals, no fills. Waiting orders are cancelled | `ENTRY_END` |
| 15:15 | Every open position is squared off | `SQUARE_OFF` |
| 15:30 | Session closes | `SESSION_CLOSE` |

## 2. Universe

About 210 NSE F&O stocks, listed in `intraday_trader/universe_fno.csv` (`UNIVERSE_FILE`). A stock outside this file is never scanned.

## 3. Step 1: stocks in play

Guide section 4. Code: `volume.py`.

Relative volume (RVOL) = volume traded today before the checkpoint, divided by the average volume traded before the same clock time on the previous sessions.

| Rule | Value | Constant |
|---|---|---|
| Sessions in the average | previous 10 | `RVOL_LOOKBACK_SESSIONS` |
| Fewest earlier sessions needed | 5 | `RVOL_MIN_SESSIONS` |
| RVOL to qualify | 1.5 or more | `MIN_RVOL` |
| Value traded so far that day | ₹5 crore or more | `MIN_TURNOVER_CR` |
| Price | ₹50 or more | `MIN_PRICE` |
| Taken per checkpoint | top 10 by RVOL among those that qualify | `WATCHLIST_PER_CHECKPOINT` |
| Watchlist size | 20 at most | `WATCHLIST_MAX` |

A stock joins at the first checkpoint where it makes the cut (`eligible_from`) and stays for the rest of the day. Its recorded `rvol` is the value at that checkpoint.

## 4. Step 2: trend on three timeframes

Guide sections 3.2 and 5. Code: `ict.structure_bias`, `ict.combine_bias`.

- A swing high is a candle whose high is above the highs of the `n` candles on each side. `n` is 1 on the weekly and 2 on the daily, 1-hour and 5-minute (`SWING_N`).
- A break of structure is a candle that **closes** beyond the latest swing that has not been broken yet. A wick beyond it does not count.
- The bias of a timeframe is the direction of its most recent break: `BULL`, `BEAR`, or `NEUTRAL` when there was none in the lookback (`BIAS_LOOKBACK_BARS`: 60 weekly, 120 daily, 120 hourly candles).
- Weekly and daily use candles up to the day before the session. The 1-hour read is refreshed each time an hourly candle completes. Hourly candles are built from the 5-minute data and start at 09:15, 10:15 and so on.

| 1-hour | Daily | Weekly | Decision |
|---|---|---|---|
| `BULL` | `BULL` | `BULL` or `NEUTRAL` | Longs only |
| `BEAR` | `BEAR` | `BEAR` or `NEUTRAL` | Shorts only |
| anything else | | | Stand aside |

`aligned` is 3 when the weekly agrees too, 2 when it is neutral.

## 5. Step 3: levels and 1-hour gaps

Guide section 6. Code: `ict.liquidity_levels`, `ict.find_fvgs`. The level names are in section 9 of `reading-results.md`.

| Use (long trade) | Levels |
|---|---|
| May be swept | `PDL`, `PWL`, `ORL`, `H1_SL`, `D_SL`, `M5_SL` |
| May be the target | `PDH`, `PWH`, `ORH`, `HOD`, `H1_SH`, `D_SH` |

| Rule | Value | Constant |
|---|---|---|
| 1-hour gaps considered | from the last 70 hourly candles, about 10 sessions | `H1_FVG_LOOKBACK_BARS` |
| Thinnest 1-hour gap | 0.10% of price | `H1_FVG_MIN_PCT` |
| Daily swings used as levels | from the last 120 daily candles | `LEVEL_LOOKBACK_DAILY_BARS` |

A bullish gap is valid until a later candle closes below its bottom. A 1-hour gap can be used only after the hourly candle that created it has closed.

## 6. Step 4: the six entry checks

Guide section 7. Code: `ict.detect_setup`. Asked each time a 5-minute candle closes between 10:15 and 14:30, from the stock's `eligible_from` onward, for a stock with an allowed direction, no open position, no waiting order, and no earlier trade that day (`MAX_TRADES_PER_SYMBOL` is 1). All six must pass.

| # | Check | Rule for a long | Constants |
|---|---|---|---|
| 1 | Reason to be here | Within the last 12 candles, price either traded below a sweepable level that had not been broken earlier that day and is back above it now (`SWEEP:<level>`), or dipped into a valid bullish 1-hour gap without closing below it (`H1_FVG`) | `CONTEXT_LOOKBACK_BARS` |
| 2 | Displacement | The middle candle of the gap closes up, with a body at least 1.2 times the average body of the 20 candles before it | `DISPLACEMENT_BODY_MULT`, `DISPLACEMENT_AVG_BARS` |
| 3 | Structure shift | After the low, a candle closed above the last 5-minute swing high that came before the low. If the day has no such swing yet, above the highest high of the 5 candles before the low | `SWING_N["M5"]`, `MSS_FALLBACK_BARS` |
| 4 | Fair value gap | The low of the candle that just closed is above the high of the candle two back, all three in the same session, and the gap is at least 0.05% of price | `M5_FVG_MIN_PCT` |
| 5 | Sensible stop | Stop is 0.05% below the lowest low of the move. Closer than 0.30% to entry: moved out to 0.30%. Further than 1.2%: no trade | `SL_BUFFER_PCT`, `MIN_STOP_PCT`, `MAX_STOP_PCT` |
| 6 | Room to the target | Target is the nearest target level above entry that is at least 2R away. More than 4R away: pulled in to 4R (`CAP_4R`). No level qualifies: plain 2R (`FIXED_2R`) | `MIN_RR`, `MAX_RR`, `FALLBACK_RR` |

The entry is a limit order at the middle of the 5-minute gap (`ENTRY_AT = "ce"`); `"edge"` would use the nearest edge of the gap.

Entry, stop and target are rounded to paise before the risk and the reward-to-risk are worked out, so every number on a signal agrees with its stored prices. A stop that was moved out to the 0.30% minimum is rounded away from the entry, so it is never closer than 0.30%.

When several levels were swept by the same move, the tag names the lowest one.

## 7. Step 5: orders and exits

Guide section 8. Code: `engine.run_day`.

The order is live from the candle after the signal.

| Event (long) | Result | Constant |
|---|---|---|
| A candle's low reaches the entry | Filled at the entry, or at the candle's open if it opened lower | |
| 6 candles pass with no fill | `EXPIRED` | `ORDER_TTL_BARS` |
| It is 14:30 | `EXPIRED` | `ENTRY_END` |
| A candle opens at or below the stop | `INVALIDATED` | |
| Price reaches the entry but an account rule refuses | `SKIPPED`, with `skip_reason` | section 8 |

| Exit | Rule |
|---|---|
| `STOP` | A candle's low reaches the stop. Exit at the stop, or at the open if it opened lower |
| `TARGET` | A candle's high reaches the target |
| `EOD` | Still open at 15:15 (`SQUARE_OFF`): exit at the close of the last candle that started before 15:15. If the stock's 5-minute data stops earlier, exit at the last candle there is; when that candle started before 15:00, the trade's story and the day's notes say so |

Where a 5-minute candle cannot say what happened first, the worse outcome is assumed:

- A candle that touches both the stop and the target is a `STOP`.
- On the candle that fills the order, the stop is checked and the target is not.
- Across stocks, every exit in a candle is processed before any fill. What a close frees (a position slot, margin, a profit) reaches fills from the next candle; a loss counts against the daily loss limit at once. If two orders fill in the same candle and only one slot is left, the stock higher on the watchlist gets it.

There is no trailing stop, no partial exit and no re-entry.

## 8. Money and account rules

Guide section 2. Code: `portfolio.py`.

| Rule | Value | Constant |
|---|---|---|
| Starting capital | ₹1,00,000 | `INITIAL_CAPITAL` |
| Own money per trade | ₹10,000 at most | `MAX_MARGIN_PER_TRADE` |
| Leverage | 3x, so up to ₹30,000 of stock per trade | `LEVERAGE` |
| Brokerage | ₹25 flat, once per completed trade | `CHARGE_PER_TRADE` |
| Risk per trade | never above 1% of capital | `MAX_RISK_PCT_PER_TRADE` |
| Positions open at once | 5 at most | `MAX_OPEN_POSITIONS` |
| Trades per day | 8 at most | `MAX_TRADES_PER_DAY` |
| Trades per stock per day | 1. Only a fill uses it up: after an order expires, is invalidated or is skipped, the stock can signal again | `MAX_TRADES_PER_SYMBOL` |
| Daily loss limit | no new trades once the day's realised net loss reaches 2% of the capital it started with | `DAILY_LOSS_LIMIT_PCT` |

How the numbers on a trade are built:

- Margin available = the smaller of ₹10,000 and free cash. Free cash = capital at the start of the day, plus the net result of trades closed so far that day, minus the margin of open positions.
- Quantity = whole shares that fit in margin × 3 at the entry price, reduced if needed so the risk stays within 1% of capital. Less than one share means no trade.
- Exposure = quantity × entry. Margin = exposure / 3. Risk (1R) = quantity × distance from entry to stop.
- Gross P&L = quantity × (exit − entry) for a long, (entry − exit) for a short. Net = gross − ₹25. R multiple = net / risk.

The account checks happen at the moment of fill, not when the signal forms. That is why a signal can be `SKIPPED` after waiting. A fill sees the account as it stood before the current candle's closes, apart from their losses (section 7).

The paper portfolio compounds: each day starts with the capital the previous day ended with. Re-running a date replaces that day and recalculates every later day's capital.

## 9. What keeps the replay honest

Guide section 12.

- Each decision uses only candles that had closed by then. The engine hands the analysis a table that ends at the current candle.
- A 1-hour candle is used only after it has completed. Weekly and daily analysis stops at the previous day. A checkpoint's volume scan counts only volume traded before it.
- An automated test replays a day with and without the later data and requires the same result. Another stops the replay right after every candle where something happened and requires everything up to that candle to match.

Not modelled, so real results would be worse: statutory charges (STT, exchange fees, GST, stamp duty), brokerage per order where a broker charges that way, slippage on stops, limit orders that are touched but not filled, the broker's actual intraday margin per stock, short-selling bans and circuit limits, and gaps or revisions in Yahoo's 5-minute data.

Data limits: a run fetches about one month of 5-minute candles (`LIVE_5M_PERIOD`) and a backtest 60 days (`BACKTEST_5M_PERIOD`), which is all Yahoo keeps. A session with fewer than 60 candles is marked partial (`MIN_BARS_FOR_FULL_DAY`). So is a session replayed on its own day before the 15:30 close (`SESSION_CLOSE`), because after about 14:15 the candle count alone looks complete; the next run after the close replaces it. A backtest started during market hours leaves today's session out.

## 10. Where each rule lives in the code

| File | Holds |
|---|---|
| `intraday_trader/config.py` | Every number above |
| `intraday_trader/models.py` | The shapes of signals, trades, day results and the portfolio |
| `intraday_trader/data.py` | Yahoo download, cleaning, building 1-hour and weekly candles |
| `intraday_trader/volume.py` | Step 1 |
| `intraday_trader/ict.py` | Steps 2 to 4 |
| `intraday_trader/engine.py` | Step 5: the candle-by-candle replay of one session |
| `intraday_trader/portfolio.py` | Position size, charges, the portfolio file, summary totals |
| `intraday_trader/runner.py` | Download, replay, save, write reports; the backtest loop |
| `intraday_trader/report.py`, `site_index.py` | The HTML reports and the website index |
| `tests/test_intraday_*.py` | Run with `python -m unittest discover -s tests -p "test_intraday_*.py"` |

When the user asks exactly how a rule is applied and the tables above do not settle it, read the docstring of the function named in that step. The docstrings state the rules precisely.

## 11. When the user wants to change a rule

Edit `config.py` only on request. Change one value at a time and run a backtest before and after, so the effect of that one change is visible. Several values tuned until the last 40 sessions look good give a strategy fitted to those 40 sessions.

Two changes the guide itself suggests for realism: `CHARGE_PER_TRADE = 45` to approximate statutory charges, and `CHARGE_PER_TRADE = 50` when the broker charges ₹25 per order.

After a change, the paper portfolio mixes sessions traded under different rules. Say so when reporting totals across the change.
