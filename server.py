"""
server.py -- MCP Trading Agent Server v2.0
===========================================
Stateful Model Context Protocol server exposing ICT / SMC market-data
tools, news sentiment tools, persistent report management, and a
lessons-learned knowledge base.

Transports
----------
  - stdio           -- Claude Desktop / Claude Code (default)
  - streamable-http -- MCP Inspector / web clients (MCP_TRANSPORT=streamable-http)

Usage
-----
    # stdio (Claude Desktop)
    python server.py

    # HTTP (dev / Inspector)
    MCP_TRANSPORT=streamable-http python server.py

    # Dev inspector
    mcp dev server.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# -- Ensure the project root is on sys.path so local imports work --
PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from config import settings              # noqa: E402

# -- Original tools --
from tools.market_data import (           # noqa: E402
    get_daily_ohlc               as _get_daily_ohlc,
    get_intraday_ohlc            as _get_intraday_ohlc,
    identify_liquidity_pools     as _identify_liquidity_pools,
    get_historical_backtest_data as _get_historical_backtest_data,
    run_intraday_backtest        as _run_intraday_backtest,
    get_multi_timeframe_data     as _get_multi_timeframe_data,
    scan_for_breakout            as _scan_for_breakout,
    run_ict_backtest             as _run_ict_backtest,
    scan_for_reversal            as _scan_for_reversal,
    scan_intraday_reversal       as _scan_intraday_reversal,
)
from tools.news import (                  # noqa: E402
    fetch_market_news         as _fetch_market_news,
)
from tools.risk_reward import (           # noqa: E402
    get_risk_to_reward_setup  as _get_risk_to_reward_setup,
)

# -- New stateful tools --
from tools.persistence import (           # noqa: E402
    manage_html_report        as _manage_html_report,
    read_lessons_learned      as _read_lessons_learned,
    update_lessons_learned    as _update_lessons_learned,
    sync_trading_knowledge    as _sync_trading_knowledge,
)

# -- Paper trading tools --
from tools.paper_trading import (         # noqa: E402
    paper_trade_portfolio       as _paper_trade_portfolio,
    paper_trade_scan_breakouts  as _paper_trade_scan_breakouts,
    paper_trade_open            as _paper_trade_open,
    paper_trade_check_positions as _paper_trade_check_positions,
    paper_trade_reset           as _paper_trade_reset,
    run_paper_backtest          as _run_paper_backtest,
)


# ------------------------------------------------------------------
#  Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,           # keep stdout clean for stdio transport
)
logger = logging.getLogger("mcp-trading-agent")


# ------------------------------------------------------------------
#  FastMCP Server Instance
# ------------------------------------------------------------------
mcp = FastMCP(
    name=settings.server_name,
    json_response=True,          # structured JSON in tool results
)


# ==================================================================
#  TOOL REGISTRATIONS -- Original (v1)
# ==================================================================

@mcp.tool()
def get_daily_ohlc(ticker: str, days: int = 60) -> str:
    """
    Fetch daily OHLCV (Open-High-Low-Close-Volume) price data for a
    given ticker symbol over the specified look-back period.

    Use this to establish the current **market structure** -- higher
    highs / lower lows, trend direction, recent range, and key
    price levels.

    Parameters
    ----------
    ticker : str
        The asset symbol (e.g. "NIFTY", "AAPL", "BTC", "GOLD").
        Common aliases like NIFTY, BANKNIFTY, SPX, DXY are
        automatically resolved.
    days : int, default 60
        Number of trading days to look back.

    Returns
    -------
    JSON string with candles list and summary statistics.
    """
    result = _get_daily_ohlc(ticker, days)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_intraday_ohlc(
    ticker: str,
    interval: str = "15m",
    days: int = 5,
) -> str:
    """
    Fetch intraday OHLCV (Open-High-Low-Close-Volume) data at a
    sub-daily interval for **live session analysis**.

    Use this instead of get_daily_ohlc when the user asks for an
    intraday view -- it provides today's developing candles so the
    agent can see the current session's structure, Order Blocks,
    Fair Value Gaps, and immediate liquidity pools rather than
    yesterday's closed daily candle.

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC", "GOLD").
    interval : str, default "15m"
        Candle interval. Supported values:
          "1m"  -- 1-minute  (max 7 days of history)
          "2m"  -- 2-minute  (max 60 days)
          "5m"  -- 5-minute  (max 60 days)  ← precision entries
          "15m" -- 15-minute (max 60 days)  ← default / recommended
          "30m" -- 30-minute (max 60 days)
          "60m" / "1h" -- Hourly (max 730 days)
    days : int, default 5
        Calendar days of intraday history to retrieve.
        Automatically clamped to the yfinance maximum for the
        chosen interval.

    Returns
    -------
    JSON string with all candles, today_candles (live session only),
    and a session_summary containing live_price, session_high/low,
    session_open, candle_count, and a note on whether the last
    candle is still developing.
    """
    result = _get_intraday_ohlc(ticker, interval, days)
    return json.dumps(result, indent=2)


@mcp.tool()
def identify_liquidity_pools(ticker: str) -> str:
    """
    Identify **buy-side** and **sell-side** liquidity pools by
    detecting recent swing highs (un-swept buy-side liquidity above
    current price) and swing lows (un-swept sell-side liquidity below
    current price).

    This is the core ICT / SMC concept of *liquidity draws* -- price
    tends to seek out resting orders above old highs and below old
    lows before reversing.

    Parameters
    ----------
    ticker : str
        The asset symbol (e.g. "NIFTY", "AAPL", "BTC", "GOLD").

    Returns
    -------
    JSON string with current_price, buy_side_liquidity (swing highs
    above price), sell_side_liquidity (swing lows below price), and
    the full set of detected swing points.
    """
    result = _identify_liquidity_pools(ticker)
    return json.dumps(result, indent=2)


@mcp.tool()
def fetch_market_news(ticker: str, max_results: int = 8) -> str:
    """
    Fetch the latest financial news headlines and snippets for a
    ticker symbol using web search.

    The raw text is returned so the **LLM can determine the
    fundamental bias** (Bullish / Bearish / Neutral) based on
    sentiment analysis of the headlines.

    Parameters
    ----------
    ticker : str
        The asset or company name (e.g. "NIFTY", "TSLA", "Bitcoin").
    max_results : int, default 8
        Maximum number of articles to retrieve.

    Returns
    -------
    JSON string with a list of articles containing title, snippet,
    url, and source domain.
    """
    result = _fetch_market_news(ticker, max_results)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_risk_to_reward_setup(
    entry: float,
    stop_loss: float,
    target: float,
) -> str:
    """
    Calculate the Risk-to-Reward ratio and quality verdict for a
    proposed trade setup.

    Use this *after* identifying a trade idea from liquidity pools
    and news bias to formalise the exact entry, stop, and target.

    Parameters
    ----------
    entry : float
        Intended entry price.
    stop_loss : float
        Stop-loss price (below entry for longs, above for shorts).
    target : float
        Take-profit / target price.

    Returns
    -------
    JSON string with direction, RR ratio, risk/reward in points
    and percent, and a quality verdict.
    """
    result = _get_risk_to_reward_setup(entry, stop_loss, target)
    return json.dumps(result, indent=2)


# ==================================================================
#  TOOL REGISTRATIONS -- New (v2) -- Stateful / Backtest / Reports
# ==================================================================

@mcp.tool()
def get_historical_backtest_data(ticker: str, days: int = 180) -> str:
    """
    Fetch extended historical OHLCV data enriched with pre-computed
    swing-point flags and daily statistics for **backtesting**.

    Unlike get_daily_ohlc (which returns ~60 recent candles for
    current-state analysis), this tool returns a *dense* dataset
    over a longer horizon so the agent can simulate past ICT/SMC
    setups and evaluate whether they would have hit targets or
    stop-losses.

    Each candle includes:
      - daily_change_pct  (close-to-close % move)
      - range_pct         (intraday range as % of open)
      - is_swing_high     (boolean)
      - is_swing_low      (boolean)

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC").
    days : int, default 180
        Number of trading days of history (clamped 10-500).

    Returns
    -------
    JSON string with enriched candles and period summary including
    up/down day counts and average daily range.
    """
    result = _get_historical_backtest_data(ticker, days)
    return json.dumps(result, indent=2)


@mcp.tool()
def manage_html_report(stock_name: str, html_content: str) -> str:
    """
    Save a generated HTML analysis report to the local `data/reports/`
    directory as `<STOCK_NAME>_<YYYY-MM-DD>.html`.

    **Auto-cleanup**: Before saving, this function scans the reports
    directory and **deletes any .html files older than 30 days** to
    prevent unbounded disk usage.

    Parameters
    ----------
    stock_name : str
        The stock or asset name used in the filename
        (e.g. "NIFTY", "AAPL", "BTC").
    html_content : str
        The complete HTML string to write. The agent should generate
        a fully formatted, self-contained HTML page with inline CSS.

    Returns
    -------
    JSON string with saved_path, filename, deleted_count (stale
    files purged), and status.
    """
    result = _manage_html_report(stock_name, html_content)
    return json.dumps(result, indent=2)


@mcp.tool()
def read_lessons_learned() -> str:
    """
    Read and return the full contents of the persistent
    `data/lessons.md` knowledge base.

    This file stores **backtested insights**, trading rules, and
    stock-specific observations that the agent has learned over time.
    If the file does not exist, a blank one is created with a
    starter markdown schema.

    The agent should call this at the START of every /analyze and
    /entry workflow to retrieve historical context before making
    new decisions.

    Returns
    -------
    JSON string with file_path, line_count, and the full markdown
    content of lessons.md.
    """
    result = _read_lessons_learned()
    return json.dumps(result, indent=2)


@mcp.tool()
def update_lessons_learned(new_content: str) -> str:
    """
    Append new backtested insights, trading rules, or observations
    to the persistent `data/lessons.md` knowledge base.

    Each entry is automatically timestamped. The agent should call
    this after completing a /backtest workflow to permanently save
    what it learned.

    Parameters
    ----------
    new_content : str
        Markdown-formatted text to append. Should include:
        - Which stock/ticker was analysed
        - The setup type (e.g. Turtle Soup, OTE, FVG)
        - Whether it would have hit target or stop-loss
        - The derived rule or insight

    Returns
    -------
    JSON string with status, file_path, bytes_written, and timestamp.
    """
    result = _update_lessons_learned(new_content)
    return json.dumps(result, indent=2)


@mcp.tool()
def run_intraday_backtest(
    ticker: str,
    days: int = 90,
    interval: str = "15m",
    min_rr: float = 3.0,
) -> str:
    """
    Fetch intraday OHLCV data and scan for SMC/ICT setups
    (Liquidity Sweep → Fair Value Gap) filtered to Risk:Reward >= min_rr.

    The engine detects swing highs/lows, identifies BSL/SSL sweeps
    with bearish/bullish FVG formations, computes entry/SL/target for
    each setup, then walks forward bar-by-bar to determine WIN / LOSS /
    OPEN outcomes.

    yfinance data caps
    ------------------
    1m : 7 days  |  5m/15m/30m : 60 days  |  60m : 730 days
    If *days* exceeds the cap the tool fetches the maximum available
    and sets ``truncated = True`` with a human-readable note.

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC").
    days : int, default 90
        Requested look-back in calendar days (clamped to yfinance limit).
    interval : str, default "15m"
        Candle interval. Use "5m" for tighter setups, "15m" for standard
        intraday, "60m" for swing-intraday.
    min_rr : float, default 3.0
        Minimum Risk:Reward ratio to include a setup. Lower this to 2.0
        to surface 1:2 setups, or raise to 4.0+ for ultra-selective mode.

    Returns
    -------
    JSON string with per-trade records, aggregate stats (win rate,
    average RR), R-multiple equity curve, and a truncation note
    if the requested period was shortened.
    """
    result = _run_intraday_backtest(ticker, days, interval, min_rr)
    return json.dumps(result, indent=2)


@mcp.tool()
def sync_trading_knowledge(new_rules: list) -> str:
    """
    Deduplicate and persist high-conviction backtest rules to lessons.md.

    Call this after ``run_intraday_backtest`` to extract the key
    market behaviours and save them as structured knowledge. Rules that
    already exist (matched by the first 60 chars of the lesson text)
    are silently skipped to prevent knowledge bloat.

    Schema for each rule dict
    -------------------------
    {
      "stock"      : "NIFTY",        -- ticker symbol
      "interval"   : "15m",          -- candle interval
      "setup_type" : "BSL_SWEEP_FVG",-- SMC pattern label
      "rr"         : "3.4",          -- achieved RR (string)
      "lesson"     : "..."           -- actionable insight (1-2 sentences)
    }

    Parameters
    ----------
    new_rules : list[dict]
        List of rule dicts conforming to the schema above.

    Returns
    -------
    JSON string with rules_added, rules_skipped, and per-skip reasons.
    """
    result = _sync_trading_knowledge(new_rules)
    return json.dumps(result, indent=2)


# ==================================================================
#  TOOL REGISTRATIONS -- Breakout Scanner (v3)
# ==================================================================

@mcp.tool()
def get_multi_timeframe_data(ticker: str) -> str:
    """
    Fetch OHLCV data simultaneously across Monthly, Weekly, and Daily
    timeframes for multi-timeframe breakout analysis.

    Retrieves up to 24 months of monthly bars, 52 weeks of weekly bars,
    and 65 days of daily bars in a single call, returning each timeframe
    with candles and a period summary (latest close, period high/low,
    avg volume, and period change %).

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC", "GOLD").

    Returns
    -------
    JSON string with monthly, weekly, and daily sub-dicts, each
    containing candles and summary. Any timeframe that fails returns
    an error sub-dict rather than crashing the whole call.
    """
    result = _get_multi_timeframe_data(ticker)
    return json.dumps(result, indent=2)


@mcp.tool()
def scan_for_breakout(ticker: str) -> str:
    """
    Score a ticker on a 1–10 Multi-Timeframe Breakout scale using
    SMC/ICT criteria across Monthly, Weekly, and Daily charts.

    Scoring breakdown (10 pts max)
    --------------------------------
    Monthly (3 pts) — macro context:
      +1  Price above EMA-6 monthly
      +1  Approaching monthly BSL within 5%
      +1  2/3 recent months closed bullish

    Weekly (3 pts) — trend strength:
      +1  Price above EMA-20 weekly
      +1  Volatility contraction (4-wk ATR < 80% of 12-wk avg)
      +1  Coiling below weekly BSL within 3%

    Daily (4 pts) — precision trigger:
      +1  Price above EMA-20 daily
      +1  Bullish displacement candle in last 10 bars (range > 1.5× ATR-14)
      +1  Unmitigated bullish FVG in last 15 bars
      +1  Volume spike: 5-day avg ≥ 1.3× 20-day avg

    Conviction tiers
    ----------------
    8–10  HIGH     — ``high_conviction_confirmed=True`` when Monthly + Weekly both BULLISH
    5–7   MODERATE — Watch and Wait
    1–4   LOW      — No confluence, skip

    The ``trigger_price`` field is the nearest daily Buy-Side Liquidity
    level above the current price — the sweep of this level confirms
    the breakout.

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC", "GOLD").

    Returns
    -------
    JSON string with breakout_score, conviction, high_conviction_confirmed,
    trigger_price, why_now (one-line summary), criteria_met (list of
    individual criteria that passed), and timeframe_alignment matrix
    (monthly / weekly / daily status dicts with trend, EMA, score).
    """
    result = _scan_for_breakout(ticker)
    return json.dumps(result, indent=2)


# ==================================================================
#  ENTRYPOINT
# ==================================================================

@mcp.tool()
def run_ict_backtest(
    ticker: str,
    days: int = 60,
    interval: str = "15m",
    min_rr: float = 3.0,
) -> str:
    """
    Comprehensive ICT (Inner Circle Trader) intraday backtest.

    Scans three ICT setup families in a single pass:

    1. KZ_SWEEP_FVG — SSL/BSL liquidity sweep occurring inside an ICT
       Kill Zone (Opening 09:15–10:15 IST, Midday 12:30–13:30 IST,
       Closing 14:30–15:30 IST), followed by a 3-candle Fair Value Gap.
       Entry at FVG midpoint.  Kill-zone timestamps are IST-corrected
       (yfinance UTC + 5:30).

    2. ORDER_BLOCK — A displacement candle (range > 1.5× ATR-14) creates
       a Bullish or Bearish Order Block (last opposite-colour candle
       immediately before the displacement).  Entry at OB 50% when price
       retraces into the OB zone within 30 bars.

    3. TURTLE_SOUP — Sweep of a prior swing high/low by ≥ 0.3× ATR that
       immediately reverses: the sweep candle's close is back inside the
       prior range.  Entry = close of the sweep candle (instant reversal,
       no FVG wait required).

    All patterns share:
      • ATR-adaptive SL : max(price × 0.15%, ATR × 0.25) beyond wick
      • Walk-forward simulation for WIN / LOSS / OPEN
      • Deduplication: same candle cannot trigger two setup types
      • RR gate: only setups with reward/risk ≥ min_rr are recorded

    Parameters
    ----------
    ticker   : str          — Asset symbol or alias (e.g. "NIFTY", "AAPL").
    days     : int          — Look-back in calendar days (clamped to yfinance
                              cap for the interval).
    interval : str          — Candle interval: "5m", "15m", "30m", "60m".
    min_rr   : float        — Minimum RR gate (default 3.0).

    Returns
    -------
    JSON string with per-trade records, aggregate stats (win rate, avg RR,
    profit factor, max drawdown, expectancy), equity curve, pattern_breakdown
    (per setup type), kill_zone_breakdown (per ICT time window), and a
    truncation note if the requested period was shortened by yfinance.
    """
    result = _run_ict_backtest(ticker, days, interval, min_rr)
    return json.dumps(result, indent=2)


@mcp.tool()
def scan_for_reversal(ticker: str) -> str:
    """
    Score a ticker on a 1–10 Multi-Timeframe Bearish-to-Bullish Reversal
    scale using SMC/ICT criteria across Monthly, Weekly, and Daily charts.

    Finds BEARISH stocks showing early signs of institutional accumulation
    and bottom formation. Opposite of scan_for_breakout — designed for
    catching reversals from downtrends.

    Scoring breakdown (10 pts max)
    --------------------------------
    Monthly (3 pts):
      +1  Price BELOW Monthly EMA-6 + near Monthly SSL within 5%
      +1  Most recent monthly candle closed BULLISH (demand emerging)
      +1  Price within 20% of 12-month low (capitulation / value zone)

    Weekly (3 pts):
      +1  Weekly SSL swept in last 3 weeks (stop hunt complete)
      +1  At least 1 of last 2 weeks closed bullish (recovery underway)
      +1  Bullish weekly with volume ≥ 1.3× 20-week avg (accumulation)

    Daily (4 pts):
      +1  Daily SSL swept in last 5 sessions (micro stop hunt)
      +1  Bullish displacement candle last 5 sessions (range > 1.5× ATR-14)
      +1  Unmitigated bullish FVG in last 10 bars
      +1  Volume spike on bullish day ≥ 1.5× 20-day avg

    Conviction tiers
    ----------------
    8–10  HIGH     — ``high_reversal_confirmed=True`` when monthly below EMA-6
                     + weekly SSL swept + daily SSL swept (all three align)
    5–7   MODERATE — Watch for 1-2 missing confirmations
    1–4   LOW      — No reversal confluence, avoid

    Parameters
    ----------
    ticker : str
        Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC", "GOLD").

    Returns
    -------
    JSON string with reversal_score, conviction, high_reversal_confirmed,
    entry_zone (bullish FVG or current price), stop_level (below sweep wick),
    target_level (nearest daily BSL), why_now summary, criteria_met list,
    and timeframe_alignment matrix.
    """
    result = _scan_for_reversal(ticker)
    return json.dumps(result, indent=2)


@mcp.tool()
def scan_intraday_reversal(
    ticker:   str,
    interval: str = "15m",
    days:     int = 5,
) -> str:
    """
    Scan intraday candles for LIVE bearish-to-bullish reversal setups.

    Designed for use during active trading sessions. Detects when an
    intraday downtrend is reversing via three ICT patterns (LONG only):

    1. TURTLE_SOUP_LONG  — Sweep of prior intraday swing low + immediate
                           reversal close. Entry = close of sweep candle.

    2. SSL_FVG_LONG      — SSL sweep + bullish FVG in next 5 bars.
                           Entry = FVG midpoint.

    3. BULLISH_OB_LONG   — Bullish displacement candle creates an Order
                           Block (last bearish candle before displacement).
                           Entry = OB midpoint on retracement.

    All signals filtered to RR ≥ 2.0. Kill Zone context is attached
    (Opening 09:15–10:15 IST, Midday 12:30–13:30, Closing 14:30–15:30).

    Parameters
    ----------
    ticker   : str   — Asset symbol or alias (e.g. "NIFTY", "AAPL", "BTC").
    interval : str   — Candle interval, default "15m". Use "5m" for tighter
                       precision. Supported: 1m/5m/15m/30m/60m.
    days     : int   — Calendar days of intraday history (default 5,
                       clamped to yfinance limit for the interval).

    Returns
    -------
    JSON string with:
      reversal_signals   — list of LONG setups with entry/SL/target/RR/time
      best_setup         — highest-RR signal
      session_context    — live_price, session_trend, is_reversal_candidate
      intraday_ssl_levels — nearest swing lows (watch for sweeps here)
      intraday_bsl_levels — nearest swing highs (take-profit targets)
      is_in_kill_zone    — current ICT Kill Zone name or null
      reversal_score     — 0-10 composite strength rating
    """
    result = _scan_intraday_reversal(ticker, interval, days)
    return json.dumps(result, indent=2)


# ==================================================================
#  TOOL REGISTRATIONS -- Paper Trading (demotrading)
# ==================================================================

@mcp.tool()
def paper_trade_portfolio() -> str:
    """
    Return the current paper trading portfolio state.

    Loads (or initialises) data/paper_trading.json with ₹1,00,000 starting
    capital. Returns current_capital, open_positions, closed_trades summary,
    win rate, total net P&L, and current IST time / market open status.

    Call this at the START of every demotrading session to see current state.

    Returns
    -------
    JSON string with capital summary, position list, and session stats.
    """
    return json.dumps(_paper_trade_portfolio(), ensure_ascii=False)


@mcp.tool()
def paper_trade_scan_breakouts(
    interval: str = "15m",
    days: int = 5,
) -> str:
    """
    Scan 25 liquid Nifty 50 stocks for intraday BSL/SSL sweep + FVG signals
    using today's session candles only. Returns signals with RR >= 2.0,
    sorted by RR descending.

    Returns market_closed status outside 09:15–15:00 IST.

    Parameters
    ----------
    interval : str, default "15m"
        Candle interval for scanning (5m, 15m, 30m).
    days : int, default 5
        Calendar days of intraday history to provide swing-detection context.

    Returns
    -------
    JSON string with signals list (ticker, direction, entry, stop_loss, target,
    rr, setup_type, signal_time), scan_errors, and metadata.
    """
    return json.dumps(_paper_trade_scan_breakouts(interval, days), ensure_ascii=False)


@mcp.tool()
def paper_trade_open(
    ticker:    str,
    direction: str,
    entry:     float,
    stop_loss: float,
    target:    float,
    rr:        float,
) -> str:
    """
    Open a new paper trade position.

    Validates RR >= 2.0, market is open (09:15–15:00 IST), and capital
    is sufficient. Calculates quantity via 0.5% risk rule. Deducts ₹50
    entry charge from current_capital immediately.

    Parameters
    ----------
    ticker    : str   — Stock symbol (e.g. "RELIANCE.NS").
    direction : str   — "LONG" or "SHORT".
    entry     : float — Entry price.
    stop_loss : float — Stop-loss price.
    target    : float — Take-profit target price.
    rr        : float — Risk:Reward ratio (must be >= 2.0).

    Returns
    -------
    JSON string with trade_id, quantity, entry_value, risk_capital,
    capital_after — or rejection reason if any validation fails.
    """
    return json.dumps(
        _paper_trade_open(ticker, direction, entry, stop_loss, target, rr),
        ensure_ascii=False,
    )


@mcp.tool()
def paper_trade_check_positions() -> str:
    """
    Check all open paper trade positions against current market prices.

    Fetches 1-minute intraday data for each open position and closes those
    that hit stop-loss or target. At 15:00 IST (EOD) all remaining positions
    are force-closed at last price. Deducts ₹50 exit charge per closed trade.

    Call this BEFORE paper_trade_scan_breakouts so capital is up-to-date.

    Returns
    -------
    JSON string with closed_now list (exit_reason, pnl_net, outcome),
    still_open count, updated current_capital, and eod_forced_close flag.
    """
    return json.dumps(_paper_trade_check_positions(), ensure_ascii=False)


@mcp.tool()
def paper_trade_reset() -> str:
    """
    Reset the paper trading portfolio to a fresh ₹1,00,000 starting balance.

    Clears all open positions and closed trade history. Overwrites
    data/paper_trading.json with default settings.

    Returns
    -------
    JSON string confirming reset with new initial_capital and reset_at timestamp.
    """
    return json.dumps(_paper_trade_reset(), ensure_ascii=False)


@mcp.tool()
def run_paper_backtest(days: int = 90) -> str:
    """
    Run a NIFTY intraday backtest with ₹ paper trading rules (not R-multiples).

    Reuses the same SMC detection engine as run_intraday_backtest but applies
    0.5% position sizing and ₹50 entry + ₹50 exit charges per trade. Reports
    final_capital, net P&L in INR, and an equity curve in INR.

    yfinance caps 15m data at 60 days; if days > 60 the backtest is
    automatically truncated and truncation_note is set.

    Parameters
    ----------
    days : int, default 90
        Look-back in calendar days (effective max 60 for 15m interval).

    Returns
    -------
    JSON string with final_capital, total_net_pnl_inr, total_charges_inr,
    win_rate_pct, max_drawdown_inr, equity_curve_inr, and per-trade records.
    """
    return json.dumps(_run_paper_backtest(days), ensure_ascii=False)


if __name__ == "__main__":
    transport = settings.transport
    logger.info(
        "Starting %s v%s  (transport=%s, tools=22)",
        settings.server_name,
        settings.server_version,
        transport,
    )

    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=settings.host, port=settings.port)
    else:
        # Default: stdio for Claude Desktop / Claude Code
        mcp.run(transport="stdio")
