from __future__ import annotations

from decimal import Decimal

from coinjure.ticker import CashTicker
from coinjure.trading.position import PositionManager


def compute_trade_size(
    position_manager: PositionManager,
    edge: Decimal,
    *,
    kelly_fraction: Decimal = Decimal('0.1'),
    edge_cap: Decimal = Decimal('0.10'),
    min_size: Decimal = Decimal('1'),
    max_size: Decimal = Decimal('100'),
) -> Decimal:
    """Compute trade size weighted by edge relative to available capital.

    Formula::

        available = sum(cash positions)
        weight    = min(edge / edge_cap, 1)
        raw       = available * kelly_fraction * weight
        result    = clamp(raw, min_size, max_size)

    Args:
        position_manager: Source of current cash balances.
        edge: Detected price edge (e.g. 0.03 for a 3% gap).
        kelly_fraction: Conservative Kelly multiplier (default 0.1 = 1/10 Kelly).
        edge_cap: Edges above this are treated equally (prevents huge bets on
            anomalous data).
        min_size: Floor — don't trade dust.
        max_size: Ceiling — per-trade cap.

    Returns:
        Trade size as a Decimal, or ``min_size`` if capital is insufficient.
    """
    available = Decimal('0')
    for pos in position_manager.get_cash_positions():
        available += pos.quantity

    if available <= 0 or edge <= 0:
        return min_size

    weight = min(edge / edge_cap, Decimal('1'))
    raw = available * kelly_fraction * weight

    # Clamp
    if raw < min_size:
        return min_size
    if raw > max_size:
        return max_size
    # Round down to integer contracts (Kalshi requires whole contracts)
    return raw.quantize(Decimal('1'))
