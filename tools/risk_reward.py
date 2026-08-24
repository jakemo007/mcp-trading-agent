"""
tools/risk_reward.py — Trade-structuring utility.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_risk_to_reward_setup(
    entry: float,
    stop_loss: float,
    target: float,
) -> dict[str, Any]:
    """
    Calculate Risk-to-Reward metrics for a proposed trade setup.

    Parameters
    ----------
    entry : float
        Intended entry price.
    stop_loss : float
        Stop-loss price.
    target : float
        Take-profit / target price.

    Returns
    -------
    dict  — JSON-serialisable result with:
        direction, risk_points, reward_points, risk_reward_ratio,
        risk_percent, reward_percent, verdict.
    """
    if entry <= 0 or stop_loss <= 0 or target <= 0:
        return {"error": "All prices must be positive numbers."}

    if entry == stop_loss:
        return {"error": "Entry and stop-loss cannot be the same price."}

    # Direction inference
    if entry > stop_loss and target > entry:
        direction = "LONG"
    elif entry < stop_loss and target < entry:
        direction = "SHORT"
    else:
        # Ambiguous — still compute
        direction = "LONG" if target > entry else "SHORT"

    risk_points   = abs(entry - stop_loss)
    reward_points = abs(target - entry)

    rr_ratio = round(reward_points / risk_points, 2) if risk_points else 0.0
    risk_pct   = round((risk_points / entry) * 100, 3)
    reward_pct = round((reward_points / entry) * 100, 3)

    # Quality verdict
    if rr_ratio >= 3.0:
        verdict = "EXCELLENT — High RR setup, strongly favourable"
    elif rr_ratio >= 2.0:
        verdict = "GOOD — Acceptable institutional-grade RR"
    elif rr_ratio >= 1.0:
        verdict = "MARGINAL — Consider tightening stop or extending target"
    else:
        verdict = "POOR — Risk outweighs reward; avoid this setup"

    return {
        "direction":        direction,
        "entry":            round(entry, 2),
        "stop_loss":        round(stop_loss, 2),
        "target":           round(target, 2),
        "risk_points":      round(risk_points, 2),
        "reward_points":    round(reward_points, 2),
        "risk_reward_ratio": rr_ratio,
        "risk_percent":     risk_pct,
        "reward_percent":   reward_pct,
        "verdict":          verdict,
    }
