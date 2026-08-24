import os
from pathlib import Path
from datetime import time

# ── Capital rules ──────────────────────────────────────────────────────────────
CAPITAL_INITIAL: float = 100_000.0
MAX_TRADE_PCT:   float = 0.25   # 25% of capital per trade → ₹25,000
MAX_RISK_PCT:    float = 0.02   # 2%  of capital as max loss → ₹2,000
MIN_RR:          float = 2.0
MAX_POSITIONS:   int   = 4

# ── Entry window (IST) ─────────────────────────────────────────────────────────
ENTRY_START = time(9, 30)
ENTRY_END   = time(14, 30)

# ── Paths ──────────────────────────────────────────────────────────────────────
_ROOT        = Path(__file__).parent.parent
DATA_DIR     = _ROOT / "data"
REPORTS_DIR  = DATA_DIR / "reports"
PORTFOLIO_FILE = DATA_DIR / "swing_portfolio.json"

# ── Watchlist: Nifty 100 HIGH + MODERATE breakout stocks (2026-08-24 scan) ────
WATCHLIST = [
    "JSWSTEEL.NS",
    "TATASTEEL.NS",
    "HINDALCO.NS",
    "VEDL.NS",
    "COALINDIA.NS",
    "NTPC.NS",
    "POWERGRID.NS",
    "BPCL.NS",
    "IOC.NS",
    "ONGC.NS",
    "GAIL.NS",
    "TATAPOWER.NS",
    "ADANIPORTS.NS",
    "ADANIENT.NS",
    "ADANIGREEN.NS",
    "WIPRO.NS",
    "TECHM.NS",
    "HCLTECH.NS",
    "INFY.NS",
    "TCS.NS",
    "LTIM.NS",
    "MPHASIS.NS",
    "SBIN.NS",
    "BANKBARODA.NS",
    "PNB.NS",
    "CANBK.NS",
    "UNIONBANK.NS",
    "IRFC.NS",
]
