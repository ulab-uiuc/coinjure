from __future__ import annotations

from decimal import Decimal

from coinjure.ticker import Ticker
from coinjure.trading.llm_sizing import (
    OpportunitySizingRequest,
    compute_opportunity_sizing_llm,
)
from coinjure.trading.position import PositionManager


def compute_trade_size(
    position_manager: PositionManager,
    edge: Decimal,
    *,
    collateral: Ticker | None = None,
    kelly_fraction: Decimal = Decimal('0.1'),
    edge_cap: Decimal = Decimal('0.10'),
    min_size: Decimal = Decimal('1'),
    max_size: Decimal = Decimal('100'),
) -> Decimal:
    """Compute trade size weighted by edge relative to available capital.

    Formula::

        available = sum(cash positions matching collateral)
        weight    = min(edge / edge_cap, 1)
        raw       = available * kelly_fraction * weight
        result    = clamp(raw, min_size, max_size)

    Args:
        position_manager: Source of current cash balances.
        edge: Detected price edge (e.g. 0.03 for a 3% gap).
        collateral: If provided, only count cash for this collateral ticker.
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
        if collateral is not None and pos.ticker != collateral:
            continue
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


def _available_capital(position_manager: PositionManager) -> Decimal:
    available = Decimal('0')
    for pos in position_manager.get_cash_positions():
        available += pos.quantity
    return available


def _current_exposure(position_manager: PositionManager) -> Decimal:
    exposure = Decimal('0')
    for pos in position_manager.get_non_cash_positions():
        if pos.quantity > 0:
            exposure += pos.quantity * pos.average_cost
    return exposure


async def compute_trade_size_with_llm(
    position_manager: PositionManager,
    edge: Decimal,
    *,
    strategy_id: str,
    strategy_type: str,
    relation_type: str,
    llm_trade_sizing: bool = False,
    llm_model: str | None = None,
    kelly_fraction: Decimal = Decimal('0.1'),
    edge_cap: Decimal = Decimal('0.10'),
    min_size: Decimal = Decimal('1'),
    max_size: Decimal = Decimal('100'),
) -> Decimal:
    quant_size = compute_trade_size(
        position_manager,
        edge,
        kelly_fraction=kelly_fraction,
        edge_cap=edge_cap,
        min_size=min_size,
        max_size=max_size,
    )
    if not llm_trade_sizing:
        return quant_size

    available = _available_capital(position_manager)
    exposure = _current_exposure(position_manager)
    total = available + exposure
    utilization = exposure / total if total > 0 else Decimal('0')
    request = OpportunitySizingRequest(
        strategy_id=strategy_id,
        strategy_type=strategy_type,
        relation_type=relation_type,
        edge=edge,
        available_capital=available,
        current_exposure=exposure,
        position_count=len(position_manager.get_non_cash_positions()),
        portfolio_utilization=utilization,
        quant_size=quant_size,
        kelly_fraction=kelly_fraction,
        max_size=max_size,
    )
    llm_size = await compute_opportunity_sizing_llm(
        request,
        model=llm_model or 'gpt-4.1-mini',
    )
    if llm_size is None:
        return quant_size
    if llm_size < min_size:
        return min_size
    if llm_size > max_size:
        return max_size
    return llm_size
