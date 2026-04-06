"""Integration tests: verify each builtin strategy type triggers trades.

For each strategy type, tests simulate the exact price conditions
that should fire entry and exit signals, confirming:
  1. The strategy enters a position when conditions are met
  2. The strategy exits when conditions reverse
  3. decisions + executed counters match expected behavior

Strategy types covered:
  - ImplicationArbStrategy  (violation: price_A > price_B)
  - GroupArbStrategy        (sum deviation: sum_yes != 1.0)
  - CointSpreadStrategy     (spread deviation from calibrated mean)
  - ConditionalArbStrategy  (price outside conditional bounds)
  - StructuralArbStrategy   (residual from structural relationship)
  - LeadLagStrategy         (leader moves, follower follows)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coinjure.data.manager import DataManager
from coinjure.data.order_book import Level
from coinjure.events import OrderBookEvent, PriceChangeEvent
from coinjure.market.relations import MarketRelation, RelationStore
from coinjure.ticker import KalshiTicker, PolyMarketTicker
from coinjure.trading.position import PositionManager
from coinjure.trading.types import PlaceOrderResult, TradeSide

# ── Shared helpers ──────────────────────────────────────────────────


def _ticker(idx: int, side: str = 'yes', market_id: str = '') -> PolyMarketTicker:
    mid = market_id or f'market-{idx}'
    return PolyMarketTicker(
        symbol=f'{side}-tok-{idx}',
        name=f'Market {idx}',
        token_id=f'{side}-tok-{idx}',
        market_id=mid,
        event_id='event-1',
        side=side,
    )


def _price_event(ticker: PolyMarketTicker, price: float) -> PriceChangeEvent:
    return PriceChangeEvent(
        ticker=ticker,
        price=Decimal(str(price)),
        timestamp=datetime.now(timezone.utc),
    )


def _relation(
    n: int,
    spread_type: str,
    relation_id: str = 'test-rel',
    **extra_fields,
) -> MarketRelation:
    markets = []
    for i in range(n):
        m = {
            'id': f'market-{i}',
            'event_id': 'event-1',
            'question': f'Market {i}?',
            'token_ids': [f'yes-tok-{i}', f'no-tok-{i}'],
        }
        m.update(extra_fields)
        markets.append(m)
    return MarketRelation(
        relation_id=relation_id,
        markets=markets,
        spread_type=spread_type,
        confidence=0.9,
    )


def _mock_trader(
    ask_prices: dict[str, Decimal] | None = None,
    bid_prices: dict[str, Decimal] | None = None,
) -> MagicMock:
    """Mock trader with configurable ask/bid prices by market_id."""
    asks = ask_prices or {}
    bids = bid_prices or {}
    trader = MagicMock()
    dm = MagicMock(spec=DataManager)

    def get_best_ask(ticker):
        mid = ticker.identifier
        price = asks.get(mid)
        if price is not None and price > 0:
            return Level(price=price, size=Decimal('1000'))
        return None

    def get_best_bid(ticker):
        mid = ticker.identifier
        price = bids.get(mid)
        if price is None:
            # Default: bid = ask - 0.01
            ap = asks.get(mid)
            if ap is not None and ap > Decimal('0.01'):
                price = ap - Decimal('0.01')
        if price is not None and price > 0:
            return Level(price=price, size=Decimal('1000'))
        return None

    def find_complement(ticker):
        """Return NO ticker for a YES ticker."""
        if ticker.side == 'yes':
            return PolyMarketTicker(
                symbol=ticker.symbol.replace('yes-', 'no-'),
                name=ticker.name,
                token_id=ticker.token_id.replace('yes-', 'no-'),
                market_id=ticker.market_id,
                event_id=getattr(ticker, 'event_id', ''),
                side='no',
            )
        return None

    def find_ticker_by_market(market_id, side='yes'):
        prefix = 'yes' if side == 'yes' else 'no'
        idx = market_id.replace('market-', '')
        return PolyMarketTicker(
            symbol=f'{prefix}-tok-{idx}',
            name=f'Market {idx}',
            token_id=f'{prefix}-tok-{idx}',
            market_id=market_id,
            event_id='event-1',
            side=side,
        )

    dm.get_best_ask = MagicMock(side_effect=get_best_ask)
    dm.get_best_bid = MagicMock(side_effect=get_best_bid)
    dm.find_complement = MagicMock(side_effect=find_complement)
    dm.find_ticker_by_market = MagicMock(side_effect=find_ticker_by_market)
    trader.market_data = dm

    pm = MagicMock(spec=PositionManager)
    pm.get_cash_positions.return_value = [MagicMock(quantity=Decimal('100000'))]
    pm.get_non_cash_positions.return_value = []
    pm.get_position.return_value = None
    pm.positions = {}
    trader.position_manager = pm
    trader.orders = []

    trader.place_order = AsyncMock(
        return_value=PlaceOrderResult(
            failure_reason=None,
        )
    )

    return trader


# =====================================================================
# 1. ImplicationArbStrategy
# =====================================================================


class TestImplicationArb:
    """Implication: A ≤ B. Entry when price_A > price_B + min_edge."""

    def _build(self, rel: MarketRelation, **kw):
        from coinjure.strategy.builtin.implication_arb_strategy import (
            ImplicationArbStrategy,
        )

        with patch.object(RelationStore, 'get', return_value=rel):
            return ImplicationArbStrategy(relation_id=rel.relation_id, **kw)

    @pytest.mark.asyncio
    async def test_entry_on_violation(self):
        """price_A > price_B + min_edge → ENTER_ARB."""
        rel = _relation(2, 'implication')
        strat = self._build(rel, min_edge=0.01)

        t0 = _ticker(0)  # slot A (earlier date)
        t1 = _ticker(1)  # slot B (later date)

        asks = {'market-0': Decimal('0.50'), 'market-1': Decimal('0.40')}
        bids = {'market-0': Decimal('0.49'), 'market-1': Decimal('0.39')}
        trader = _mock_trader(asks, bids)

        # Feed prices: A=0.50, B=0.40 → violation=0.10 > min_edge=0.01
        await strat.process_event(_price_event(t0, 0.50), trader)
        await strat.process_event(_price_event(t1, 0.40), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected ENTER_ARB trade'
        decisions = strat.get_decisions()
        assert any(d.action == 'ENTER_ARB' and d.executed for d in decisions)

    @pytest.mark.asyncio
    async def test_no_entry_when_constraint_holds(self):
        """price_A < price_B → HOLD, no trade."""
        rel = _relation(2, 'implication')
        strat = self._build(rel, min_edge=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        trader = _mock_trader(
            {'market-0': Decimal('0.30'), 'market-1': Decimal('0.50')}
        )

        # A=0.30 < B=0.50 → no violation
        await strat.process_event(_price_event(t0, 0.30), trader)
        await strat.process_event(_price_event(t1, 0.50), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] == 0

    @pytest.mark.asyncio
    async def test_exit_on_constraint_restored(self):
        """After entry, constraint restored → EXIT_ARB."""
        rel = _relation(2, 'implication')
        strat = self._build(rel, min_edge=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        asks = {'market-0': Decimal('0.50'), 'market-1': Decimal('0.40')}
        bids = {'market-0': Decimal('0.49'), 'market-1': Decimal('0.39')}
        trader = _mock_trader(asks, bids)

        # Enter
        await strat.process_event(_price_event(t0, 0.50), trader)
        await strat.process_event(_price_event(t1, 0.40), trader)
        assert strat._position_state == 'short_a_long_b'

        # Constraint restored: A=0.30 < B=0.50
        await strat.process_event(_price_event(t0, 0.30), trader)
        await strat.process_event(_price_event(t1, 0.50), trader)

        decisions = strat.get_decisions()
        assert any(d.action == 'EXIT_ARB' and d.executed for d in decisions)
        assert strat._position_state == 'flat'

    @pytest.mark.asyncio
    async def test_edge_exactly_at_threshold(self):
        """violation == min_edge → no entry (needs strict >)."""
        rel = _relation(2, 'implication')
        strat = self._build(rel, min_edge=0.10)

        t0 = _ticker(0)
        t1 = _ticker(1)
        trader = _mock_trader(
            {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        )

        # A=0.60, B=0.50 → violation=0.10 == min_edge → no entry
        await strat.process_event(_price_event(t0, 0.60), trader)
        await strat.process_event(_price_event(t1, 0.50), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] == 0


# =====================================================================
# 2. GroupArbStrategy
# =====================================================================


class TestGroupArb:
    """Complementary/exclusivity: sum deviation from 1.0."""

    def _build(self, rel: MarketRelation, **kw):
        from coinjure.strategy.builtin.group_arb_strategy import GroupArbStrategy

        with patch.object(RelationStore, 'get', return_value=rel):
            return GroupArbStrategy(
                relation_id=rel.relation_id,
                warmup_seconds=0,
                cooldown_seconds=0,
                **kw,
            )

    @pytest.mark.asyncio
    async def test_buy_yes_when_sum_below_one(self):
        """sum_yes < 1 - fees → BUY_YES on all legs."""
        rel = _relation(3, 'complementary')
        strat = self._build(rel, min_edge=0.01)

        # Register tickers
        for i in range(3):
            strat._tickers[f'market-{i}'] = _ticker(i)

        # sum = 0.20 + 0.25 + 0.30 = 0.75 → edge_buy_yes = 1-0.75-0.015 = 0.235
        asks = {
            f'market-{i}': p
            for i, p in enumerate(
                [
                    Decimal('0.20'),
                    Decimal('0.25'),
                    Decimal('0.30'),
                ]
            )
        }
        trader = _mock_trader(asks)

        await strat._check_arb(trader)

        assert strat._held_direction == 'BUY_YES'
        decisions = strat.get_decisions()
        buy_yes = [d for d in decisions if d.action == 'BUY_YES' and d.executed]
        assert len(buy_yes) == 3, f'Expected 3 BUY_YES legs, got {len(buy_yes)}'

    @pytest.mark.asyncio
    async def test_buy_no_when_sum_above_one(self):
        """sum_yes > 1 + fees → BUY_NO on all legs."""
        rel = _relation(3, 'complementary')
        strat = self._build(rel, min_edge=0.01)

        for i in range(3):
            strat._tickers[f'market-{i}'] = _ticker(i)

        # sum = 0.40 + 0.40 + 0.40 = 1.20 → edge_buy_no = 1.20-1-0.015 = 0.185
        asks = {f'market-{i}': Decimal('0.40') for i in range(3)}
        trader = _mock_trader(asks)

        await strat._check_arb(trader)

        assert strat._held_direction == 'BUY_NO'
        decisions = strat.get_decisions()
        buy_no = [d for d in decisions if d.action == 'BUY_NO' and d.executed]
        assert len(buy_no) == 3

    @pytest.mark.asyncio
    async def test_exclusivity_blocks_buy_yes(self):
        """Exclusivity type → BUY_YES disabled, only BUY_NO allowed."""
        rel = _relation(3, 'exclusivity')
        strat = self._build(rel, min_edge=0.01)

        for i in range(3):
            strat._tickers[f'market-{i}'] = _ticker(i)

        # sum = 0.75 → would be BUY_YES for complementary, but exclusivity blocks it
        asks = {
            f'market-{i}': p
            for i, p in enumerate(
                [
                    Decimal('0.20'),
                    Decimal('0.25'),
                    Decimal('0.30'),
                ]
            )
        }
        trader = _mock_trader(asks)

        await strat._check_arb(trader)

        assert (
            strat._held_direction is None
        ), 'BUY_YES should be blocked for exclusivity'

    @pytest.mark.asyncio
    async def test_no_trade_when_sum_near_one(self):
        """sum ≈ 1.0 → no edge → no trade."""
        rel = _relation(3, 'complementary')
        strat = self._build(rel, min_edge=0.01)

        for i in range(3):
            strat._tickers[f'market-{i}'] = _ticker(i)

        # sum = 0.33 + 0.33 + 0.34 = 1.00 → edges ≈ 0 after fees
        asks = {
            f'market-{i}': p
            for i, p in enumerate(
                [
                    Decimal('0.33'),
                    Decimal('0.33'),
                    Decimal('0.34'),
                ]
            )
        }
        trader = _mock_trader(asks)

        await strat._check_arb(trader)

        assert strat._held_direction is None


# =====================================================================
# 3. CointSpreadStrategy
# =====================================================================


class TestCointSpread:
    """Correlated: spread deviates from calibrated mean."""

    def _build(self, rel: MarketRelation, **kw):
        from coinjure.strategy.builtin.coint_spread_strategy import (
            CointSpreadStrategy,
        )

        with patch.object(RelationStore, 'get', return_value=rel):
            return CointSpreadStrategy(relation_id=rel.relation_id, **kw)

    @pytest.mark.asyncio
    async def test_entry_on_spread_deviation(self):
        """After warmup, spread deviation triggers entry."""
        rel = _relation(2, 'correlated')
        strat = self._build(rel, warmup=5, entry_mult=0.3, exit_mult=0.1)

        t0 = _ticker(0)
        t1 = _ticker(1)
        asks = {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        bids = {'market-0': Decimal('0.49'), 'market-1': Decimal('0.49')}
        trader = _mock_trader(asks, bids)

        # Warmup: feed 5 events with varying spread so std > 0
        warmup_spreads = [
            (0.50, 0.49),  # spread=0.01
            (0.51, 0.50),  # spread=0.01
            (0.49, 0.50),  # spread=-0.01
            (0.52, 0.50),  # spread=0.02
            (0.48, 0.50),  # spread=-0.02
        ]
        for a_p, b_p in warmup_spreads:
            await strat.process_event(_price_event(t0, a_p), trader)
            await strat.process_event(_price_event(t1, b_p), trader)

        # Now inject a big spread deviation: A jumps to 0.70, B stays 0.50
        await strat.process_event(_price_event(t0, 0.70), trader)
        await strat.process_event(_price_event(t1, 0.50), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected spread deviation to trigger entry'

    @pytest.mark.asyncio
    async def test_no_entry_during_warmup(self):
        """No trades before warmup completes."""
        rel = _relation(2, 'correlated')
        strat = self._build(rel, warmup=10, entry_mult=0.3)

        t0 = _ticker(0)
        t1 = _ticker(1)
        trader = _mock_trader(
            {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        )

        # Only 3 events — still in warmup
        for _ in range(3):
            await strat.process_event(_price_event(t0, 0.60), trader)
            await strat.process_event(_price_event(t1, 0.40), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] == 0

    @pytest.mark.asyncio
    async def test_exit_on_spread_convergence(self):
        """Spread returns to mean → exit position."""
        rel = _relation(2, 'correlated')
        # Use large exit_mult so exit threshold is generous
        strat = self._build(rel, warmup=5, entry_mult=2.0, exit_mult=1.5)

        t0 = _ticker(0)
        t1 = _ticker(1)
        asks = {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        bids = {'market-0': Decimal('0.49'), 'market-1': Decimal('0.49')}
        trader = _mock_trader(asks, bids)

        # Warmup with variance
        warmup_spreads = [
            (0.50, 0.49),
            (0.51, 0.50),
            (0.49, 0.50),
            (0.52, 0.50),
            (0.48, 0.50),
        ]
        for a_p, b_p in warmup_spreads:
            await strat.process_event(_price_event(t0, a_p), trader)
            await strat.process_event(_price_event(t1, b_p), trader)

        # Force position state to simulate a held position
        strat._position_state = 'short_spread'

        # Spread converges to exactly the mean (~0.002): A-B = 0.502-0.50
        await strat.process_event(_price_event(t0, 0.502), trader)
        await strat.process_event(_price_event(t1, 0.50), trader)

        decisions = strat.get_decisions()
        closes = [d for d in decisions if 'CLOSE' in d.action]
        assert len(closes) >= 1, 'Expected CLOSE_SPREAD on convergence'
        assert strat._position_state == 'flat'


# =====================================================================
# 4. ConditionalArbStrategy
# =====================================================================


class TestConditionalArb:
    """Conditional: price_A outside bounds f(price_B)."""

    def _build(self, rel: MarketRelation, **kw):
        from coinjure.strategy.builtin.conditional_arb_strategy import (
            ConditionalArbStrategy,
        )

        with patch.object(RelationStore, 'get', return_value=rel):
            return ConditionalArbStrategy(relation_id=rel.relation_id, **kw)

    @pytest.mark.asyncio
    async def test_short_a_when_overpriced(self):
        """price_A > upper_bound + min_edge → SHORT_A."""
        rel = _relation(2, 'conditional')
        # cond_lower=0.0, cond_upper=1.0 → bounds = [0, B + (1-B)] = [0, 1]
        # With tighter bounds: cond_upper=0.5 → upper = 0.5*B + (1-B) = 1-0.5*B
        # If B=0.50 → upper = 0.75
        strat = self._build(rel, cond_lower=0.0, cond_upper=0.5, min_edge=0.01)

        t0 = _ticker(0)  # A (conditional)
        t1 = _ticker(1)  # B (conditioning)
        asks = {'market-0': Decimal('0.85'), 'market-1': Decimal('0.50')}
        bids = {'market-0': Decimal('0.84'), 'market-1': Decimal('0.49')}
        trader = _mock_trader(asks, bids)

        # A=0.85 > upper(0.75) + 0.01 → SHORT_A
        await strat.process_event(_price_event(t1, 0.50), trader)
        await strat.process_event(_price_event(t0, 0.85), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected SHORT_A when A overpriced'
        decisions = strat.get_decisions()
        assert any(d.executed and 'SHORT' in d.action for d in decisions)

    @pytest.mark.asyncio
    async def test_long_a_when_underpriced(self):
        """price_A < lower_bound - min_edge → LONG_A."""
        rel = _relation(2, 'conditional')
        # cond_lower=0.5 → lower = 0.5*B. If B=0.80 → lower = 0.40
        strat = self._build(rel, cond_lower=0.5, cond_upper=1.0, min_edge=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        asks = {'market-0': Decimal('0.30'), 'market-1': Decimal('0.80')}
        bids = {'market-0': Decimal('0.29'), 'market-1': Decimal('0.79')}
        trader = _mock_trader(asks, bids)

        # A=0.30 < lower(0.40) - 0.01 → LONG_A
        await strat.process_event(_price_event(t1, 0.80), trader)
        await strat.process_event(_price_event(t0, 0.30), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected LONG_A when A underpriced'
        decisions = strat.get_decisions()
        assert any(d.executed and 'LONG' in d.action for d in decisions)

    @pytest.mark.asyncio
    async def test_no_trade_when_in_bounds(self):
        """price_A within bounds → no trade."""
        rel = _relation(2, 'conditional')
        strat = self._build(rel, cond_lower=0.0, cond_upper=1.0, min_edge=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        trader = _mock_trader(
            {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        )

        await strat.process_event(_price_event(t1, 0.50), trader)
        await strat.process_event(_price_event(t0, 0.50), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] == 0


# =====================================================================
# 5. StructuralArbStrategy
# =====================================================================


class TestStructuralArb:
    """Structural: residual = price_A - (slope*price_B + intercept)."""

    def _build(self, rel: MarketRelation, **kw):
        from coinjure.strategy.builtin.structural_arb_strategy import (
            StructuralArbStrategy,
        )

        with patch.object(RelationStore, 'get', return_value=rel):
            return StructuralArbStrategy(relation_id=rel.relation_id, **kw)

    @pytest.mark.asyncio
    async def test_short_a_when_overpriced(self):
        """residual > min_edge → SHORT_A (A overpriced vs structural model)."""
        rel = _relation(2, 'structural')
        # slope=1.0, intercept=0.0 → expected_a = price_b
        strat = self._build(rel, slope=1.0, intercept=0.0, min_edge=0.01)

        t0 = _ticker(0)  # A
        t1 = _ticker(1)  # B
        asks = {'market-0': Decimal('0.60'), 'market-1': Decimal('0.40')}
        bids = {'market-0': Decimal('0.59'), 'market-1': Decimal('0.39')}
        trader = _mock_trader(asks, bids)

        # A=0.60, B=0.40 → expected_a=0.40 → residual=0.20 > 0.01 → SHORT_A
        await strat.process_event(_price_event(t1, 0.40), trader)
        await strat.process_event(_price_event(t0, 0.60), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected SHORT_A when residual > min_edge'

    @pytest.mark.asyncio
    async def test_long_a_when_underpriced(self):
        """residual < -min_edge → LONG_A (A underpriced)."""
        rel = _relation(2, 'structural')
        strat = self._build(rel, slope=1.0, intercept=0.0, min_edge=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        asks = {'market-0': Decimal('0.30'), 'market-1': Decimal('0.50')}
        bids = {'market-0': Decimal('0.29'), 'market-1': Decimal('0.49')}
        trader = _mock_trader(asks, bids)

        # A=0.30, B=0.50 → expected_a=0.50 → residual=-0.20 < -0.01 → LONG_A
        await strat.process_event(_price_event(t1, 0.50), trader)
        await strat.process_event(_price_event(t0, 0.30), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected LONG_A when residual < -min_edge'

    @pytest.mark.asyncio
    async def test_no_trade_when_aligned(self):
        """residual ≈ 0 → no trade."""
        rel = _relation(2, 'structural')
        strat = self._build(rel, slope=1.0, intercept=0.0, min_edge=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        trader = _mock_trader(
            {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        )

        await strat.process_event(_price_event(t1, 0.50), trader)
        await strat.process_event(_price_event(t0, 0.50), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] == 0

    @pytest.mark.asyncio
    async def test_with_slope_and_intercept(self):
        """Non-trivial structural relationship: A = 0.5*B + 0.2."""
        rel = _relation(2, 'structural')
        strat = self._build(rel, slope=0.5, intercept=0.2, min_edge=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        asks = {'market-0': Decimal('0.60'), 'market-1': Decimal('0.40')}
        bids = {'market-0': Decimal('0.59'), 'market-1': Decimal('0.39')}
        trader = _mock_trader(asks, bids)

        # expected_a = 0.5*0.40 + 0.20 = 0.40, A=0.60 → residual=0.20 > 0.01
        await strat.process_event(_price_event(t1, 0.40), trader)
        await strat.process_event(_price_event(t0, 0.60), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1


# =====================================================================
# 6. LeadLagStrategy
# =====================================================================


class TestLeadLag:
    """Temporal: leader moves, follower should follow."""

    def _build(self, rel: MarketRelation, **kw):
        from coinjure.strategy.builtin.lead_lag_strategy import LeadLagStrategy

        with patch.object(RelationStore, 'get', return_value=rel):
            return LeadLagStrategy(relation_id=rel.relation_id, **kw)

    @pytest.mark.asyncio
    async def test_buy_follower_when_leader_moves_up(self):
        """Leader price jumps up → buy follower YES."""
        rel = _relation(2, 'temporal')
        rel.lead_lag = 1  # slot 0 leads
        strat = self._build(rel, warmup=5, entry_threshold=0.01)

        t0 = _ticker(0)  # leader
        t1 = _ticker(1)  # follower
        asks = {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        bids = {'market-0': Decimal('0.49'), 'market-1': Decimal('0.49')}
        trader = _mock_trader(asks, bids)

        # Set follower price first (required before entry check)
        await strat.process_event(_price_event(t1, 0.50), trader)

        # Warmup: stable leader prices around 0.50
        for _ in range(5):
            await strat.process_event(_price_event(t0, 0.50), trader)

        # Leader jumps to 0.55 → should buy follower
        await strat.process_event(_price_event(t0, 0.55), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected buy follower when leader moves up'

    @pytest.mark.asyncio
    async def test_sell_follower_when_leader_moves_down(self):
        """Leader price drops → sell follower (buy NO)."""
        rel = _relation(2, 'temporal')
        rel.lead_lag = 1
        strat = self._build(rel, warmup=5, entry_threshold=0.01)

        t0 = _ticker(0)
        t1 = _ticker(1)
        asks = {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        bids = {'market-0': Decimal('0.49'), 'market-1': Decimal('0.49')}
        trader = _mock_trader(asks, bids)

        # Set follower price first
        await strat.process_event(_price_event(t1, 0.50), trader)

        # Warmup
        for _ in range(5):
            await strat.process_event(_price_event(t0, 0.50), trader)

        # Leader drops to 0.45
        await strat.process_event(_price_event(t0, 0.45), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] >= 1, 'Expected sell follower when leader moves down'

    @pytest.mark.asyncio
    async def test_no_trade_during_warmup(self):
        """No trades before warmup completes."""
        rel = _relation(2, 'temporal')
        rel.lead_lag = 1
        strat = self._build(rel, warmup=10, entry_threshold=0.01)

        t0 = _ticker(0)
        trader = _mock_trader(
            {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        )

        # Only 3 leader events — still warming up
        for _ in range(3):
            await strat.process_event(_price_event(t0, 0.60), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] == 0

    @pytest.mark.asyncio
    async def test_no_trade_when_leader_stable(self):
        """Leader stable → no signal → no trade."""
        rel = _relation(2, 'temporal')
        rel.lead_lag = 1
        strat = self._build(rel, warmup=5, entry_threshold=0.05)

        t0 = _ticker(0)
        trader = _mock_trader(
            {'market-0': Decimal('0.50'), 'market-1': Decimal('0.50')}
        )

        # Warmup + stable prices
        for _ in range(8):
            await strat.process_event(_price_event(t0, 0.50), trader)

        stats = strat.get_decision_stats()
        assert stats['executed'] == 0
