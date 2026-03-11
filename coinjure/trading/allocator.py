"""Portfolio-level capital allocator.

Given total capital and a set of strategy candidates (with backtest PnL),
compute per-strategy capital budgets using edge-weighted allocation.

Usage::

    budgets = allocate_capital(
        total_capital=Decimal('1000'),
        candidates=[
            AllocationCandidate(strategy_id='rel-1', backtest_pnl=12.5),
            AllocationCandidate(strategy_id='rel-2', backtest_pnl=3.0),
            AllocationCandidate(strategy_id='rel-3', backtest_pnl=0.5),
        ],
    )
    # {'rel-1': Decimal('781'), 'rel-2': Decimal('187'), 'rel-3': Decimal('31')}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class AllocationCandidate:
    """Minimal info needed for allocation."""

    strategy_id: str
    backtest_pnl: float = 0.0  # positive = profitable


def allocate_capital(
    total_capital: Decimal,
    candidates: list[AllocationCandidate],
    *,
    min_budget: Decimal = Decimal('10'),
    max_budget_pct: Decimal = Decimal('0.4'),
    reserve_pct: Decimal = Decimal('0.1'),
) -> dict[str, Decimal]:
    """Allocate capital across strategy candidates weighted by backtest PnL.

    Only candidates with positive backtest_pnl receive an allocation.
    Candidates with zero/negative PnL get the minimum budget.

    Args:
        total_capital: Total available capital to distribute.
        candidates: Strategy candidates with backtest results.
        min_budget: Minimum capital per strategy (floor).
        max_budget_pct: Maximum share any single strategy can receive.
        reserve_pct: Fraction of capital held in reserve (not allocated).

    Returns:
        Dict mapping strategy_id → allocated capital (Decimal, rounded to int).
    """
    if not candidates:
        return {}

    deployable = total_capital * (Decimal('1') - reserve_pct)
    max_per_strategy = deployable * max_budget_pct

    # Separate profitable from non-profitable
    profitable = [c for c in candidates if c.backtest_pnl > 0]
    unprofitable = [c for c in candidates if c.backtest_pnl <= 0]

    # Unprofitable strategies get minimum budget
    budgets: dict[str, Decimal] = {}
    reserved_for_min = min_budget * Decimal(str(len(unprofitable)))
    for c in unprofitable:
        budgets[c.strategy_id] = min_budget

    if not profitable:
        return budgets

    remaining = deployable - reserved_for_min
    if remaining <= 0:
        # Not enough capital even for minimums
        for c in profitable:
            budgets[c.strategy_id] = min_budget
        return budgets

    # Weight by PnL (linear)
    total_pnl = sum(c.backtest_pnl for c in profitable)
    for c in profitable:
        weight = Decimal(str(c.backtest_pnl)) / Decimal(str(total_pnl))
        raw = remaining * weight
        clamped = min(max(raw, min_budget), max_per_strategy)
        budgets[c.strategy_id] = clamped.quantize(Decimal('1'))

    # Log summary
    allocated_total = sum(budgets.values())
    logger.info(
        'Allocated $%s across %d strategies (reserve $%s)',
        allocated_total,
        len(budgets),
        total_capital - allocated_total,
    )
    for sid, budget in sorted(budgets.items(), key=lambda x: -x[1]):
        logger.info('  %s: $%s', sid[:30], budget)

    return budgets
