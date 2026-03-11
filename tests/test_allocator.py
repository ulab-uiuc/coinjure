from __future__ import annotations

from decimal import Decimal

from coinjure.trading.allocator import AllocationCandidate, allocate_capital


def test_weighted_allocation_by_pnl():
    """Profitable strategies get more capital, proportional to PnL."""
    budgets = allocate_capital(
        Decimal('1000'),
        [
            AllocationCandidate(strategy_id='high', backtest_pnl=90.0),
            AllocationCandidate(strategy_id='low', backtest_pnl=10.0),
        ],
    )
    assert budgets['high'] > budgets['low']
    # Total should not exceed deployable (90% of 1000 = 900)
    assert sum(budgets.values()) <= Decimal('900')


def test_unprofitable_gets_minimum():
    """Strategies with zero or negative PnL get min_budget."""
    budgets = allocate_capital(
        Decimal('1000'),
        [
            AllocationCandidate(strategy_id='good', backtest_pnl=50.0),
            AllocationCandidate(strategy_id='bad', backtest_pnl=-5.0),
            AllocationCandidate(strategy_id='zero', backtest_pnl=0.0),
        ],
        min_budget=Decimal('10'),
    )
    assert budgets['bad'] == Decimal('10')
    assert budgets['zero'] == Decimal('10')
    assert budgets['good'] > Decimal('10')


def test_max_budget_cap():
    """No single strategy should exceed max_budget_pct."""
    budgets = allocate_capital(
        Decimal('10000'),
        [
            AllocationCandidate(strategy_id='dominant', backtest_pnl=1000.0),
            AllocationCandidate(strategy_id='tiny', backtest_pnl=1.0),
        ],
        max_budget_pct=Decimal('0.4'),
    )
    # max is 40% of deployable (9000) = 3600
    assert budgets['dominant'] <= Decimal('3600')


def test_empty_candidates():
    assert allocate_capital(Decimal('1000'), []) == {}


def test_all_unprofitable():
    """All strategies get minimum budget when none are profitable."""
    budgets = allocate_capital(
        Decimal('1000'),
        [
            AllocationCandidate(strategy_id='a', backtest_pnl=0.0),
            AllocationCandidate(strategy_id='b', backtest_pnl=-10.0),
        ],
        min_budget=Decimal('10'),
    )
    assert budgets['a'] == Decimal('10')
    assert budgets['b'] == Decimal('10')


def test_single_strategy_gets_full_allocation():
    budgets = allocate_capital(
        Decimal('1000'),
        [AllocationCandidate(strategy_id='only', backtest_pnl=50.0)],
    )
    # Should get up to max_budget_pct of deployable
    assert budgets['only'] > Decimal('0')
    assert budgets['only'] <= Decimal('400')  # 40% of 900
