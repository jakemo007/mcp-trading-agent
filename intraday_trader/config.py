"""Every tunable number of the intraday trader lives here.

The learning guide (docs/intraday-trader/README.md) explains why each value
was chosen. Change a value here and both the simulation and the report follow.
"""
from __future__ import annotations

from datetime import time
from pathlib import Path

# ── Money rules (set by the user) ─────────────────────────────────────────────
INITIAL_CAPITAL: float = 100_000.0       # starting paper capital, INR
MAX_MARGIN_PER_TRADE: float = 10_000.0   # own money blocked per trade, INR
LEVERAGE: float = 3.0                    # intraday leverage: 10,000 margin buys 30,000 of stock
CHARGE_PER_TRADE: float = 25.0           # flat brokerage per completed trade (entry + exit), INR

# ── Safety rails (added so one bad day cannot do much damage) ─────────────────
MAX_RISK_PCT_PER_TRADE: float = 0.01     # never risk more than 1% of capital on one stop
MAX_OPEN_POSITIONS: int = 5              # at most 5 trades open at once (50,000 margin)
MAX_TRADES_PER_DAY: int = 8
MAX_TRADES_PER_SYMBOL: int = 1           # one trade per stock per day; only a fill uses it up
DAILY_LOSS_LIMIT_PCT: float = 0.02       # stop opening trades once the day is down 2% of capital

# ── Session clock (IST) ───────────────────────────────────────────────────────
SESSION_OPEN = time(9, 15)
FIRST_HOUR_END = time(10, 15)            # opening range done, first 1h candle closed
ENTRY_START = time(10, 15)               # first bar that may trigger a signal starts here
ENTRY_END = time(14, 30)                 # no signal and no fill at or after this time
SQUARE_OFF = time(15, 15)                # everything still open is closed at this time
SESSION_CLOSE = time(15, 30)
SCAN_CHECKPOINTS = (time(10, 15), time(11, 15), time(12, 15), time(13, 15))

# ── Step 1: volume scan ("stocks in play") ────────────────────────────────────
RVOL_LOOKBACK_SESSIONS: int = 10         # compare today with the previous 10 sessions
RVOL_MIN_SESSIONS: int = 5               # need at least this many earlier sessions
MIN_RVOL: float = 1.5                    # today's volume so far >= 1.5x the usual by this time
MIN_TURNOVER_CR: float = 5.0             # traded value so far today, in crore INR
MIN_PRICE: float = 50.0
WATCHLIST_PER_CHECKPOINT: int = 10       # top N by RVOL at each hourly checkpoint
WATCHLIST_MAX: int = 20

# ── Step 2: market structure on weekly / daily / 1h ───────────────────────────
# A swing high needs `n` lower highs on each side (a "fractal"); same idea for lows.
SWING_N = {"W": 1, "D": 2, "H1": 2, "M5": 2}
BIAS_LOOKBACK_BARS = {"W": 60, "D": 120, "H1": 120}

# ── Step 3: points of interest ────────────────────────────────────────────────
H1_FVG_LOOKBACK_BARS: int = 70           # about 10 sessions of 1h candles
H1_FVG_MIN_PCT: float = 0.0010           # ignore 1h gaps thinner than 0.10% of price
LEVEL_LOOKBACK_DAILY_BARS: int = 120     # daily swing highs/lows used as targets

# ── Step 4: the 5-minute entry ────────────────────────────────────────────────
M5_WINDOW_BARS: int = 150                # bars handed to the detector (about 2 sessions)
M5_FVG_MIN_PCT: float = 0.0005           # ignore 5m gaps thinner than 0.05% of price
DISPLACEMENT_BODY_MULT: float = 1.2      # middle candle body >= 1.2x the recent average body
DISPLACEMENT_AVG_BARS: int = 20
CONTEXT_LOOKBACK_BARS: int = 12          # the sweep / 1h-FVG tap must be within the last hour
MSS_FALLBACK_BARS: int = 5               # if no swing is found, use the 5-bar extreme before the low
ENTRY_AT: str = "ce"                     # "ce" = midpoint of the gap, "edge" = nearest edge
ORDER_TTL_BARS: int = 6                  # limit order lives for 30 minutes
SL_BUFFER_PCT: float = 0.0005            # stop sits 0.05% beyond the swing
MIN_STOP_PCT: float = 0.0030             # stop is never closer than 0.30% (keeps brokerage small vs risk)
MAX_STOP_PCT: float = 0.0120             # skip the trade if the stop must be wider than 1.2%
MIN_RR: float = 2.0
MAX_RR: float = 4.0                      # a target further than 4R is pulled in to 4R
FALLBACK_RR: float = 2.0                 # used only when no liquidity level sits >= MIN_RR away
ALLOW_SHORT: bool = True

# ── Data download ─────────────────────────────────────────────────────────────
DOWNLOAD_CHUNK: int = 40                 # tickers per Yahoo request
DOWNLOAD_RETRIES: int = 3
LIVE_5M_PERIOD: str = "1mo"
BACKTEST_5M_PERIOD: str = "60d"          # Yahoo keeps 60 days of 5-minute bars
DAILY_PERIOD: str = "2y"
MIN_BARS_FOR_FULL_DAY: int = 60          # fewer 5m bars than this marks the day as partial

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
PORTFOLIO_FILE = DATA_DIR / "intraday_portfolio.json"
UNIVERSE_FILE = Path(__file__).with_name("universe_fno.csv")

DAILY_REPORT_PREFIX = "INTRADAY-DAILY-"       # INTRADAY-DAILY-2026-11-16.html
BACKTEST_REPORT_PREFIX = "INTRADAY-BACKTEST-"

DISCLAIMER = (
    "This is an automated paper-trading simulation for education. "
    "It is not financial advice. Trade at your own risk."
)


def snapshot() -> dict:
    """The settings worth printing in a report, as plain JSON values."""
    return {
        "initial_capital": INITIAL_CAPITAL,
        "max_margin_per_trade": MAX_MARGIN_PER_TRADE,
        "leverage": LEVERAGE,
        "charge_per_trade": CHARGE_PER_TRADE,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "daily_loss_limit_pct": DAILY_LOSS_LIMIT_PCT,
        "min_rvol": MIN_RVOL,
        "min_rr": MIN_RR,
        "max_rr": MAX_RR,
        "min_stop_pct": MIN_STOP_PCT,
        "max_stop_pct": MAX_STOP_PCT,
        "entry_window": [ENTRY_START.strftime("%H:%M"), ENTRY_END.strftime("%H:%M")],
        "square_off": SQUARE_OFF.strftime("%H:%M"),
        "scan_checkpoints": [t.strftime("%H:%M") for t in SCAN_CHECKPOINTS],
        "allow_short": ALLOW_SHORT,
    }
