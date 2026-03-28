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
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AllocationCandidate:
    """Minimal info needed for allocation."""

    strategy_id: str
    backtest_pnl: float = 0.0  # positive = profitable
    max_drawdown: Optional[float] = None  # max drawdown (negative or zero typically)
    timestamp: Optional[float] = None  # epoch seconds when backtest was produced


def _compute_score(candidate: AllocationCandidate) -> float:
    """Compute risk-adjusted score for a candidate.

    If max_drawdown is available, uses pnl / max(abs(max_drawdown), 0.01)
    to produce a Sharpe-like risk-adjusted score.  Falls back to raw PnL
    when max_drawdown is not provided.
    """
    if candidate.max_drawdown is not None:
        return candidate.backtest_pnl / max(abs(candidate.max_drawdown), 0.01)
    return candidate.backtest_pnl


def _compute_decay_weight(
    candidate: AllocationCandidate,
    now: float,
    half_life_days: float,
) -> float:
    """Exponential decay weight based on backtest age.

    Returns ``exp(-0.693 * age_days / half_life_days)``.
    If the candidate has no timestamp, returns 1.0 (no decay).
    """
    if candidate.timestamp is None:
        return 1.0
    age_seconds = max(now - candidate.timestamp, 0.0)
    age_days = age_seconds / 86400.0
    return math.exp(-0.693 * age_days / half_life_days)


def allocate_capital(
    total_capital: Decimal,
    candidates: list[AllocationCandidate],
    *,
    min_budget: Decimal = Decimal('10'),
    max_budget_pct: Decimal = Decimal('0.4'),
    reserve_pct: Decimal = Decimal('0.1'),
    decay_half_life_days: float = 30.0,
) -> dict[str, Decimal]:
    """Allocate capital across strategy candidates weighted by risk-adjusted score.

    Profitable candidates are scored using a risk-adjusted metric when
    ``max_drawdown`` is available (``pnl / max(abs(max_drawdown), 0.01)``),
    falling back to raw PnL otherwise.  Each score is further weighted by an
    exponential time-decay factor when a backtest ``timestamp`` is present.

    Args:
        total_capital: Total available capital to distribute.
        candidates: Strategy candidates with backtest results.
        min_budget: Minimum capital per strategy (floor).
        max_budget_pct: Maximum share any single strategy can receive.
        reserve_pct: Fraction of capital held in reserve (not allocated).
        decay_half_life_days: Half-life in days for time-decay weighting of
            backtest results.  Older backtests receive proportionally less
            weight.  Ignored when candidates lack a ``timestamp``.

    Returns:
        Dict mapping strategy_id -> allocated capital (Decimal, rounded to int).
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

    # Compute risk-adjusted, time-decayed scores
    now = time.time()
    weighted_scores: list[tuple[AllocationCandidate, float]] = []
    for c in profitable:
        score = _compute_score(c)
        decay = _compute_decay_weight(c, now, decay_half_life_days)
        weighted_scores.append((c, score * decay))

    total_score = sum(s for _, s in weighted_scores)
    if total_score <= 0:
        # All scores decayed to near-zero; give everyone minimum
        for c in profitable:
            budgets[c.strategy_id] = min_budget
        return budgets

    for c, score in weighted_scores:
        weight = Decimal(str(score)) / Decimal(str(total_score))
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
