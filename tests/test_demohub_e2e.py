"""End-to-end test: demo hub → HubDataSource → strategy → trades.

Connects to a running demo hub, feeds events to each strategy type,
and verifies trades fire within a time budget.

Run with:  conda run -n coinjure python tests/test_demohub_e2e.py
Requires:  demo hub running (coinjure hub start --demo --detach)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.WARNING, format='%(name)s: %(message)s')

from coinjure.data.manager import DataManager
from coinjure.data.order_book import Level
from coinjure.engine.trader.paper import PaperTrader
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.hub.subscriber import HubDataSource
from coinjure.market.relations import MarketRelation, RelationStore
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import PolyMarketTicker
from coinjure.trading.position import PositionManager
from coinjure.trading.risk import NoRiskManager

HUB_SOCK = Path.home() / '.coinjure' / 'hub.sock'
TIMEOUT = 30  # seconds per strategy


def find_relation(spread_type: str) -> MarketRelation | None:
    """Find first backtest_passed relation of given type."""
    store = RelationStore()
    for r in store.list():
        if r.spread_type == spread_type and r.status == 'backtest_passed':
            return r
    return None


def get_token_ids(rel: MarketRelation) -> list[str]:
    """Extract YES token IDs from relation."""
    tids = []
    for m in rel.markets:
        token_ids = m.get('token_ids', [])
        tid = token_ids[0] if token_ids else m.get('token_id', '')
        if tid:
            tids.append(tid)
    return tids


def build_strategy(spread_type: str, rel: MarketRelation) -> Strategy:
    """Build appropriate strategy for relation type."""
    rid = rel.relation_id
    if spread_type == 'implication':
        from coinjure.strategy.builtin.implication_arb_strategy import (
            ImplicationArbStrategy,
        )

        return ImplicationArbStrategy(relation_id=rid, min_edge=0.005)
    elif spread_type in ('complementary', 'exclusivity'):
        from coinjure.strategy.builtin.group_arb_strategy import GroupArbStrategy

        return GroupArbStrategy(
            relation_id=rid,
            min_edge=0.001,
            warmup_seconds=1.0,
            cooldown_seconds=1,
        )
    elif spread_type == 'correlated':
        from coinjure.strategy.builtin.coint_spread_strategy import CointSpreadStrategy

        return CointSpreadStrategy(relation_id=rid, warmup=5, entry_mult=0.3)
    elif spread_type == 'conditional':
        from coinjure.strategy.builtin.conditional_arb_strategy import (
            ConditionalArbStrategy,
        )

        return ConditionalArbStrategy(relation_id=rid, min_edge=0.005)
    elif spread_type == 'structural':
        from coinjure.strategy.builtin.structural_arb_strategy import (
            StructuralArbStrategy,
        )

        return StructuralArbStrategy(relation_id=rid, min_edge=0.005)
    elif spread_type == 'temporal':
        from coinjure.strategy.builtin.lead_lag_strategy import LeadLagStrategy

        return LeadLagStrategy(relation_id=rid, warmup=5, entry_threshold=0.005)
    else:
        raise ValueError(f'Unknown spread type: {spread_type}')


async def test_strategy(spread_type: str) -> dict:
    """Run one strategy against the demo hub for TIMEOUT seconds."""
    rel = find_relation(spread_type)
    if rel is None:
        return {
            'type': spread_type,
            'status': 'SKIP',
            'reason': 'no backtest_passed relation',
        }

    tids = get_token_ids(rel)
    if not tids:
        return {'type': spread_type, 'status': 'SKIP', 'reason': 'no token IDs'}

    strategy = build_strategy(spread_type, rel)
    dm = DataManager()
    pm = PositionManager()
    # Seed cash position
    from coinjure.ticker import CashTicker
    from coinjure.trading.position import Position

    pm.update_position(
        Position(
            ticker=CashTicker.POLYMARKET_USDC,
            quantity=Decimal('10000'),
            average_cost=Decimal('1'),
            realized_pnl=Decimal('0'),
        )
    )
    from coinjure.trading.risk import StandardRiskManager

    budget = Decimal('10000')
    risk = StandardRiskManager(
        position_manager=pm,
        market_data=dm,
        max_position_size=budget,
        max_total_exposure=budget * 2,
        max_single_trade_size=budget / 2,
        max_drawdown_pct=Decimal('0.2'),
    )
    trader = PaperTrader(
        position_manager=pm,
        market_data=dm,
        risk_manager=risk,
        min_fill_rate=Decimal('0.5'),
        max_fill_rate=Decimal('1.0'),
        commission_rate=Decimal('0.0'),
    )

    # Connect to hub
    hub = HubDataSource(socket_path=HUB_SOCK, tickers=tids)

    # Also watch NO tokens
    for m in rel.markets:
        token_ids = m.get('token_ids', [])
        if len(token_ids) > 1:
            hub._tickers.add(token_ids[1])

    await hub.start()

    events_seen = 0
    price_events = 0
    violations_seen = 0
    max_violation = 0.0
    start = time.monotonic()
    result = {
        'type': spread_type,
        'relation_id': rel.relation_id,
        'tokens': len(tids),
    }

    try:
        while time.monotonic() - start < TIMEOUT:
            event = await hub.get_next_event()
            if event is None:
                continue
            events_seen += 1

            # Feed to data manager (same as MultiStrategyEngine._apply_market_event)
            if isinstance(event, PriceChangeEvent):
                price_events += 1
                dm.process_price_change_event(event)
            elif isinstance(event, OrderBookEvent):
                dm.process_orderbook_event(event)

            # Feed to strategy
            strategy.bind_context(event, trader)
            await strategy.process_event(event, trader)

            # Track violations for implication
            if spread_type == 'implication' and isinstance(event, PriceChangeEvent):
                pa = getattr(strategy, '_price_a', None)
                pb = getattr(strategy, '_price_b', None)
                if pa is not None and pb is not None:
                    v = float(pa - pb)
                    if v > max_violation:
                        max_violation = v
                    if v > 0.005:
                        violations_seen += 1

            stats = strategy.get_decision_stats()
            if stats.get('executed', 0) > 0:
                elapsed = time.monotonic() - start
                result.update(
                    {
                        'status': 'PASS',
                        'time_to_first_trade': f'{elapsed:.1f}s',
                        'events': events_seen,
                        'decisions': stats['decisions'],
                        'executed': stats['executed'],
                    }
                )
                break
        else:
            # Timed out
            stats = strategy.get_decision_stats()
            result.update(
                {
                    'status': 'FAIL',
                    'reason': f'no trade in {TIMEOUT}s',
                    'events': events_seen,
                    'price_events': price_events,
                    'decisions': stats['decisions'],
                    'executed': stats['executed'],
                }
            )
            if spread_type == 'implication':
                result['max_violation'] = f'{max_violation:.4f}'
                result['violations_above_edge'] = violations_seen
    finally:
        await hub.stop()

    return result


async def main():
    # Check hub is running
    if not HUB_SOCK.exists():
        print(
            'ERROR: demo hub not running. Start with: coinjure hub start --demo --detach'
        )
        sys.exit(1)

    types_to_test = ['implication', 'complementary', 'correlated', 'conditional']

    # Check which types have relations
    store = RelationStore()
    available = defaultdict(int)
    for r in store.list():
        if r.status == 'backtest_passed':
            available[r.spread_type] += 1

    print(f'\nAvailable backtest_passed relations: {dict(available)}')
    print(f'Testing: {types_to_test}\n')
    print(f'{"Type":<16} {"Status":<8} {"Details"}')
    print('-' * 70)

    for stype in types_to_test:
        result = await test_strategy(stype)
        status = result.get('status', '?')
        details_parts = []
        for k, v in result.items():
            if k in ('type', 'status'):
                continue
            details_parts.append(f'{k}={v}')
        details = ' | '.join(details_parts)
        mark = '✓' if status == 'PASS' else '✗' if status == 'FAIL' else '—'
        print(f'{stype:<16} {mark} {status:<6} {details}')

    print()


if __name__ == '__main__':
    asyncio.run(main())
