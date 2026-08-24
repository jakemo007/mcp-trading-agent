import math
from swing_trader.config import MAX_RISK_PCT, MAX_TRADE_PCT


def calc_quantity(capital: float, entry: float, stop: float) -> tuple[int, float]:
    """Return (qty, capital_used) applying both 2% risk and 25% cap rules.

    Returns (0, 0.0) when entry is invalid or qty rounds to zero.
    """
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0 or entry <= 0:
        return 0, 0.0

    qty = min(
        math.floor(capital * MAX_RISK_PCT / risk_per_share),  # 2% risk rule
        math.floor(capital * MAX_TRADE_PCT / entry),          # 25% cap rule
    )
    if qty <= 0:
        return 0, 0.0

    capital_used = round(qty * entry, 2)
    if capital_used > capital:
        return 0, 0.0

    return qty, capital_used
