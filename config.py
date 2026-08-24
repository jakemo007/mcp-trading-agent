"""
config.py — Centralised configuration for the MCP Trading Agent server.

All tuneable knobs live here so the server module stays clean.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerConfig:
    """Immutable runtime configuration loaded once at startup."""

    # ── Server identity ──────────────────────────────────────────
    server_name: str = "ICT-SMC Trading Agent"
    server_version: str = "2.0.0"

    # ── Market data defaults ─────────────────────────────────────
    default_ohlc_days: int = 60          # look-back window for OHLCV
    swing_lookback_days: int = 90        # range for liquidity-pool scan
    swing_window: int = 5               # rolling window for swing detection

    # ── Backtest defaults ────────────────────────────────────────
    backtest_max_days: int = 365         # max look-back for backtest data

    # ── News defaults ────────────────────────────────────────────
    default_max_news: int = 8
    news_time_limit: str = "w"          # DuckDuckGo time filter: d/w/m/y

    # ── Report retention ─────────────────────────────────────────
    report_retention_days: int = 30      # auto-purge HTML reports older than this

    # ── Transport ────────────────────────────────────────────────
    transport: str = field(
        default_factory=lambda: os.getenv("MCP_TRANSPORT", "stdio")
    )
    host: str = "0.0.0.0"
    port: int = int(os.getenv("MCP_PORT", "8000"))


# Singleton used by the server
settings = ServerConfig()
