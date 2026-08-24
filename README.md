# MCP Trading Agent v3.0 — ICT / SMC + News Sentiment
# ======================================================
#
# A production-ready MCP server exposing 13 market-data, news,
# backtesting, and persistence tools to an LLM agent (Nexus v2).
# The agent learns from backtests and applies those rules to
# live analysis — compounding its edge over time.
#
# Quick Start
# -----------
#
#   1. Install dependencies:
#
#       pip install -r requirements.txt
#
#   2. Run the server (stdio transport for Claude Desktop / Claude Code):
#
#       python server.py
#
#   3. Or run with HTTP transport (for MCP Inspector / web):
#
#       set MCP_TRANSPORT=streamable-http
#       python server.py
#
#   4. Test with the MCP Inspector:
#
#       npx -y @modelcontextprotocol/inspector
#       # Then connect to http://localhost:8000/mcp
#
# Claude Desktop Integration
# --------------------------
#
#   Add this to your Claude Desktop config (~/.claude/config.json):
#
#   {
#     "mcpServers": {
#       "trading-agent": {
#         "command": "python",
#         "args": ["C:\\Github\\ai-company\\mcp-trading-agent\\server.py"]
#       }
#     }
#   }
#
# Project Structure
# -----------------
#
#   mcp-trading-agent/
#   ├── server.py              # 13 MCP tool registrations + entry point
#   ├── config.py              # ServerConfig dataclass (v2.0.0)
#   ├── system_prompt.py       # Nexus v2 persona — 7 command workflows
#   ├── CLAUDE.md              # Auto-loaded by Claude Code (same as above)
#   ├── requirements.txt       # mcp[cli], yfinance, ddgs, pandas, numpy
#   ├── README.md              # This file
#   ├── v2_upgrade_walkthrough.md  # Architecture & evolution docs
#   ├── tools/
#   │   ├── market_data.py     # 7 functions: OHLC, liquidity, backtest,
#   │   │                      #   intraday backtest, MTF fetch, breakout scan
#   │   ├── news.py            # fetch_market_news (DuckDuckGo / ddgs)
#   │   ├── risk_reward.py     # get_risk_to_reward_setup
#   │   └── persistence.py     # HTML reports, lessons.md CRUD,
#   │                          #   sync_trading_knowledge (SHA-256 dedup)
#   └── data/                  # Persistent state (auto-created on first run)
#       ├── lessons.md         # Knowledge base — rules learned from backtests
#       ├── lessons.hashes     # SHA-256 fingerprints for dedup sidecar
#       └── reports/           # HTML analysis reports (30-day auto-purge)
#
# All 13 MCP Tools
# ----------------
#
#   v1 — Original (5 tools)
#   ┌──────────────────────────────┬──────────────────────────────────────────┐
#   │ Tool                         │ Purpose                                  │
#   ├──────────────────────────────┼──────────────────────────────────────────┤
#   │ get_daily_ohlc               │ Daily OHLCV candles (60-day default)     │
#   │ get_intraday_ohlc            │ Sub-daily candles (1m/5m/15m/30m/60m)   │
#   │ identify_liquidity_pools     │ Swing high/low detection (BSL / SSL)     │
#   │ fetch_market_news            │ DuckDuckGo news search (fundamental bias)│
#   │ get_risk_to_reward_setup     │ RR ratio + quality verdict               │
#   └──────────────────────────────┴──────────────────────────────────────────┘
#
#   v2 — Stateful / Backtest (6 tools)
#   ┌──────────────────────────────┬──────────────────────────────────────────┐
#   │ Tool                         │ Purpose                                  │
#   ├──────────────────────────────┼──────────────────────────────────────────┤
#   │ get_historical_backtest_data │ Extended OHLCV w/ swing flags (10-500d) │
#   │ run_intraday_backtest        │ Auto SMC scan: sweep+FVG, RR>=3, w-fwd  │
#   │ manage_html_report           │ Save HTML + auto-open browser + 30d purge│
#   │ read_lessons_learned         │ Read lessons.md knowledge base           │
#   │ update_lessons_learned       │ Append free-form insights to lessons.md  │
#   │ sync_trading_knowledge       │ SHA-256 dedup + persist structured rules │
#   └──────────────────────────────┴──────────────────────────────────────────┘
#
#   v3 — Breakout Scanner (2 tools)
#   ┌──────────────────────────────┬──────────────────────────────────────────┐
#   │ Tool                         │ Purpose                                  │
#   ├──────────────────────────────┼──────────────────────────────────────────┤
#   │ get_multi_timeframe_data     │ Monthly + Weekly + Daily OHLC in one call│
#   │ scan_for_breakout            │ 1-10 score across M/W/D timeframes       │
#   └──────────────────────────────┴──────────────────────────────────────────┘
#
# Agent Commands (plain-text, not slash commands)
# -----------------------------------------------
#
#   Command                                     Data Source
#   ──────────────────────────────────────────────────────────────────
#   backtest [ticker] [days]                    get_historical_backtest_data
#   backtest intraday [ticker] [days] [intv]    run_intraday_backtest
#   analyze [ticker]                            get_daily_ohlc + liquidity
#   entry [ticker]                              (runs analyze silently first)
#   intraday [ticker]                           get_intraday_ohlc — 1:3 RR gate
#   view [ticker]                               get_daily_ohlc — macro swing
#   breakout [ticker or list]                   get_multi_timeframe_data +
#                                               scan_for_breakout
#
#   Example session:
#
#     backtest intraday NIFTY 60 15m
#     → automated SMC scan, equity curve HTML, rules saved to lessons.md
#
#     intraday NIFTY
#     → live 15m analysis with 1:3 RR gate, HTML report auto-opened
#
#     breakout NIFTY, BTC, GOLD
#     → D/W/M alignment matrix, conviction scores, trigger prices
#
#     analyze AAPL
#     → fundamental + technical confluence, HTML report
#
#     entry AAPL
#     → tight trade card: entry / SL / target / RR / verdict
#
# Breakout Scoring (scan_for_breakout)
# -------------------------------------
#
#   Scoring breakdown (10 pts max):
#     Monthly (3 pts): price > EMA-6, near BSL <= 5%, 2/3 months bullish
#     Weekly  (3 pts): price > EMA-20, volatility contraction, near BSL <= 3%
#     Daily   (4 pts): price > EMA-20, displacement candle, FVG present,
#                      volume spike >= 1.3× 20-day avg
#
#   Conviction tiers:
#     8-10  HIGH     (high_conviction_confirmed when M + W both BULLISH)
#     5-7   MODERATE (Watch and Wait)
#     1-4   LOW      (no confluence)
#
#   Trigger price = nearest daily BSL above current price.
#
# Supported Ticker Aliases
# ------------------------
#
#   NIFTY → ^NSEI,  BANKNIFTY → ^NSEBANK,  SENSEX → ^BSESN,
#   SPX → ^GSPC,  SPY → SPY,  QQQ → QQQ,  DXY → DX-Y.NYB,
#   GOLD → GC=F,  CRUDE → CL=F,  BTC → BTC-USD,  ETH → ETH-USD
#
# Intraday Backtest — What run_intraday_backtest Returns
# -------------------------------------------------------
#
#   Per-trade fields:
#     setup_type, direction, sweep_time, session_label, hour,
#     swept_level, fvg_zone, entry, stop_loss, target,
#     risk_pts, reward_pts, rr, atr_at_setup, is_consecutive_sweep,
#     outcome (WIN/LOSS/OPEN), exit_price
#
#   Aggregate stats:
#     win_rate_pct, avg_rr, profit_factor, max_drawdown_r,
#     expectancy_r, equity_curve (R-multiple list), session_breakdown
#
#   session_breakdown keys (Opening/Morning/Midday/Afternoon/Closing):
#     total, wins, losses, open_trades, win_rate_pct
#
# Knowledge Persistence
# ---------------------
#
#   lessons.md is automatically maintained across sessions.
#   sync_trading_knowledge uses SHA-256 fingerprints stored in
#   lessons.hashes to prevent near-duplicate rules accumulating.
#   HTML reports older than 30 days are auto-purged by manage_html_report.
#
# License: MIT
