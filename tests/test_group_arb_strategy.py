"""Tests for GroupArbStrategy edge cases.

Covers:
1. Partial data protection (min_markets derived from relation size)
2. Exclusivity BUY_YES disabled (edge_buy_yes forced to -1)
3. Relation market filtering (_check_arb only uses _relation_market_ids)
4. Fee calculation (_FEE_PER_SIDE * n deducted from edges)
5. Cooldown enforcement (cooldown_seconds blocks rapid trades)
6. NO ticker resolution (_market_no_ticker dict construction)
"""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coinjure.data.manager import DataManager
from coinjure.data.order_book import Level
from coinjure.market.relations import MarketRelation, RelationStore
from coinjure.strategy.builtin.group_arb_strategy import (
    _FEE_PER_SIDE,
    GroupArbStrategy,
)
from coinjure.ticker import PolyMarketTicker
from coinjure.trading.types import PlaceOrderResult

# ── Helpers ──────────────────────────────────────────────────────────


def _make_relation(
    n_markets: int,
    spread_type: str = 'complementary',
    relation_id: str = 'test-rel',
) -> MarketRelation:
    """Build a MarketRelation with *n_markets* markets, each with YES+NO token IDs."""
    markets = []
    for i in range(n_markets):
        markets.append(
            {
                'id': f'market-{i}',
                'event_id': 'event-1',
                'question': f'Outcome {i}?',
                'token_ids': [f'yes-tok-{i}', f'no-tok-{i}'],
            }
        )
    return MarketRelation(
        relation_id=relation_id,
        markets=markets,
        spread_type=spread_type,
        confidence=0.9,
    )


def _build_strategy(
    relation: MarketRelation,
    **kwargs,
) -> GroupArbStrategy:
    """Construct a GroupArbStrategy by patching RelationStore.get to return *relation*."""
    with patch.object(RelationStore, 'get', return_value=relation):
        return GroupArbStrategy(relation_id=relation.relation_id, **kwargs)


def _make_ticker(idx: int, side: str = 'yes') -> PolyMarketTicker:
    token_id = f'{side}-tok-{idx}'
    return PolyMarketTicker(
        symbol=token_id,
        name=f'Outcome {idx}',
        token_id=token_id,
        market_id=f'market-{idx}',
        event_id='event-1',
        side=side,
    )


def _mock_trader(ask_prices: dict[str, Decimal]) -> MagicMock:
    """Return a mock Trader whose market_data.get_best_ask returns prices by market_id."""
    trader = MagicMock()
    dm = MagicMock(spec=DataManager)

    def get_best_ask(ticker):
        mid = ticker.identifier
        price = ask_prices.get(mid)
        if price is not None and price > 0:
            return Level(price=price, size=Decimal('100'))
        return None

    def get_best_bid(ticker):
        mid = ticker.identifier
        price = ask_prices.get(mid)
        if price is not None and price > 0:
            # Bid is slightly below ask
            bid = price - Decimal('0.01')
            if bid > 0:
                return Level(price=bid, size=Decimal('100'))
        return None

    dm.get_best_ask = MagicMock(side_effect=get_best_ask)
    dm.get_best_bid = MagicMock(side_effect=get_best_bid)
    dm.find_complement = MagicMock(return_value=None)
    trader.market_data = dm
    trader.place_order = AsyncMock(return_value=PlaceOrderResult())
    trader.get_position = MagicMock(return_value=Decimal('1000'))

    # PositionManager mock for compute_trade_size
    pm = MagicMock()
    cash_pos = MagicMock()
    cash_pos.quantity = Decimal('1000')
    pm.get_cash_positions.return_value = [cash_pos]
    trader.position_manager = pm

    return trader


# ── Test 1: Partial data protection ──────────────────────────────────


class TestPartialDataProtection:
    """min_markets must equal the relation size; _check_arb must not fire
    until ALL markets have prices."""

    def test_min_markets_equals_relation_size(self):
        rel = _make_relation(5)
        strat = _build_strategy(rel)
        assert (
            strat.min_markets == 5
        ), f'Expected min_markets=5 for a 5-market relation, got {strat.min_markets}'

    @pytest.mark.asyncio
    async def test_check_arb_blocks_with_partial_prices(self):
        """With only 3 of 5 prices available, _check_arb should return without trading."""
        rel = _make_relation(5)
        strat = _build_strategy(rel, min_edge=0.01)

        # Register all 5 tickers so len(self._tickers) == 5
        for i in range(5):
            t = _make_ticker(i)
            strat._tickers[f'market-{i}'] = t

        # But only provide prices for 3 markets
        partial_prices = {f'market-{i}': Decimal('0.15') for i in range(3)}
        trader = _mock_trader(partial_prices)

        await strat._check_arb(trader)

        # No orders should have been placed
        trader.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_arb_fires_with_all_prices(self):
        """With all 5 prices and a big edge, _check_arb should attempt trades."""
        rel = _make_relation(5)
        strat = _build_strategy(rel, min_edge=0.01)

        for i in range(5):
            t = _make_ticker(i)
            strat._tickers[f'market-{i}'] = t

        # sum_yes = 5 * 0.10 = 0.50, edge_buy_yes = 1 - 0.50 - 0.005*5 = 0.475
        all_prices = {f'market-{i}': Decimal('0.10') for i in range(5)}
        trader = _mock_trader(all_prices)

        await strat._check_arb(trader)

        # Orders should have been placed (one per market)
        assert trader.place_order.call_count == 5


# ── Test 2: Exclusivity BUY_YES disabled ────────────────────────────


class TestExclusivityBuyYesDisabled:
    """For exclusivity relations, edge_buy_yes must be forced to -1,
    preventing BUY_YES from ever winning."""

    @pytest.mark.asyncio
    async def test_exclusivity_forces_buy_no(self):
        rel = _make_relation(3, spread_type='exclusivity')
        strat = _build_strategy(rel, min_edge=0.01)

        for i in range(3):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        # sum_yes = 3 * 0.40 = 1.20 -> overpriced
        # edge_buy_no = 1.20 - 1.0 - 0.005*3 = 0.185
        # edge_buy_yes = -1 (disabled)
        prices = {f'market-{i}': Decimal('0.40') for i in range(3)}
        trader = _mock_trader(prices)

        await strat._check_arb(trader)

        # BUY_NO should have been chosen
        assert strat._held_direction == 'BUY_NO'

    @pytest.mark.asyncio
    async def test_exclusivity_blocks_buy_yes_even_when_underpriced(self):
        """Even if sum_yes < 1 (underpriced), exclusivity must NOT trigger BUY_YES."""
        rel = _make_relation(3, spread_type='exclusivity')
        strat = _build_strategy(rel, min_edge=0.01)

        for i in range(3):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        # sum_yes = 3 * 0.20 = 0.60 -> underpriced
        # Without exclusivity: edge_buy_yes = 1 - 0.60 - 0.015 = 0.385 (would trigger)
        # With exclusivity: edge_buy_yes = -1, edge_buy_no = 0.60 - 1 - 0.015 = -0.415
        # Neither exceeds min_edge -> no trade
        prices = {f'market-{i}': Decimal('0.20') for i in range(3)}
        trader = _mock_trader(prices)

        await strat._check_arb(trader)

        # No trade should occur (edge_buy_no is negative)
        trader.place_order.assert_not_called()
        assert strat._held_direction is None


# ── Test 3: Relation market filtering ────────────────────────────────


class TestRelationMarketFiltering:
    """_check_arb must only consider markets in _relation_market_ids,
    ignoring extra markets that may have been added via event_id matching."""

    @pytest.mark.asyncio
    async def test_extra_markets_excluded_from_sum(self):
        rel = _make_relation(3)
        strat = _build_strategy(rel, min_edge=0.01)

        # Register the 3 relation markets + 1 extra
        for i in range(3):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        extra_ticker = PolyMarketTicker(
            symbol='yes-tok-extra',
            name='Extra outcome',
            token_id='yes-tok-extra',
            market_id='market-extra',
            event_id='event-1',
            side='yes',
        )
        strat._tickers['market-extra'] = extra_ticker

        # Prices: relation markets at 0.30 each, extra at 0.50
        # If extra were included: sum_yes=1.40 (overpriced -> BUY_NO)
        # Correctly filtered: sum_yes=0.90, edge_buy_yes=1-0.90-0.015=0.085 -> BUY_YES
        prices = {f'market-{i}': Decimal('0.30') for i in range(3)}
        prices['market-extra'] = Decimal('0.50')
        trader = _mock_trader(prices)

        await strat._check_arb(trader)

        # Should trigger BUY_YES (not BUY_NO), proving the extra market was ignored
        assert strat._held_direction == 'BUY_YES'
        # Should place exactly 3 orders (not 4)
        assert trader.place_order.call_count == 3


# ── Test 4: Fee calculation ──────────────────────────────────────────


class TestFeeCalculation:
    """Verify that _FEE_PER_SIDE * n is correctly deducted from edge calculations."""

    def test_fee_constant_value(self):
        assert _FEE_PER_SIDE == Decimal('0.005')

    @pytest.mark.asyncio
    async def test_fee_prevents_marginal_arb(self):
        """An arb that is profitable pre-fees but not post-fees should not trigger."""
        rel = _make_relation(4)
        strat = _build_strategy(rel, min_edge=0.01)

        for i in range(4):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        # sum_yes = 4 * 0.245 = 0.98
        # Pre-fee edge_buy_yes = 1 - 0.98 = 0.02 (looks profitable)
        # Post-fee edge_buy_yes = 1 - 0.98 - 0.005*4 = 0.02 - 0.02 = 0.00
        # 0.00 < min_edge(0.01) -> no trade
        prices = {f'market-{i}': Decimal('0.245') for i in range(4)}
        trader = _mock_trader(prices)

        await strat._check_arb(trader)

        trader.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_fee_deduction_arithmetic(self):
        """Directly verify the edge formulas with known values."""
        rel = _make_relation(3)
        strat = _build_strategy(rel, min_edge=0.01)

        for i in range(3):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        # sum_yes = 0.30 + 0.25 + 0.35 = 0.90
        prices = {
            'market-0': Decimal('0.30'),
            'market-1': Decimal('0.25'),
            'market-2': Decimal('0.35'),
        }
        trader = _mock_trader(prices)

        # Manually compute expected edges:
        sum_yes = Decimal('0.90')
        n = 3
        expected_edge_buy_yes = Decimal('1') - sum_yes - _FEE_PER_SIDE * n
        # = 1.0 - 0.90 - 0.015 = 0.085
        expected_edge_buy_no = sum_yes - Decimal('1') - _FEE_PER_SIDE * n
        # = 0.90 - 1.0 - 0.015 = -0.115

        assert expected_edge_buy_yes == Decimal('0.085')
        assert expected_edge_buy_no == Decimal('-0.115')

        # Run the strategy; BUY_YES should win with edge=0.085
        await strat._check_arb(trader)

        assert strat._held_direction == 'BUY_YES'

        # Verify per-leg decisions recorded (one per market)
        decisions = strat.get_decisions()
        assert len(decisions) == 3  # one decision per leg
        for d in decisions:
            assert d.action == 'BUY_YES'
            assert d.executed is True
            sig = d.signal_values
            assert abs(sig['edge_buy_yes'] - 0.085) < 1e-9
            assert abs(sig['edge_buy_no'] - (-0.115)) < 1e-9


# ── Test 5: Cooldown enforcement ─────────────────────────────────────


class TestCooldown:
    """cooldown_seconds must prevent rapid-fire trades."""

    @pytest.mark.asyncio
    async def test_cooldown_blocks_second_trade(self):
        rel = _make_relation(2)
        strat = _build_strategy(rel, min_edge=0.01, cooldown_seconds=60)

        for i in range(2):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        # Big edge: sum_yes=0.40, edge_buy_yes=1-0.40-0.01=0.59
        prices = {f'market-{i}': Decimal('0.20') for i in range(2)}
        trader = _mock_trader(prices)

        # First arb should fire
        await strat._check_arb(trader)
        assert strat._held_direction == 'BUY_YES'
        first_count = trader.place_order.call_count
        assert first_count == 2

        # Reset held_direction to simulate position closed externally
        strat._held_direction = None

        # Second arb immediately after should be blocked by cooldown
        await strat._check_arb(trader)

        # No additional orders placed
        assert trader.place_order.call_count == first_count

    @pytest.mark.asyncio
    async def test_cooldown_allows_after_expiry(self):
        rel = _make_relation(2)
        # Use a very short cooldown for testability
        strat = _build_strategy(rel, min_edge=0.01, cooldown_seconds=1)

        for i in range(2):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        prices = {f'market-{i}': Decimal('0.20') for i in range(2)}
        trader = _mock_trader(prices)

        # First trade
        await strat._check_arb(trader)
        assert strat._held_direction == 'BUY_YES'
        strat._held_direction = None

        # Artificially expire cooldown by rewinding _last_arb_time
        strat._last_arb_time = time.monotonic() - 2

        # Second trade should now fire
        await strat._check_arb(trader)
        assert strat._held_direction == 'BUY_YES'
        assert trader.place_order.call_count == 4  # 2 legs x 2 arbs


# ── Test 6: NO ticker resolution ────────────────────────────────────


class TestNoTickerResolution:
    """_market_no_ticker dict must be constructed correctly from relation data,
    mapping market_id -> PolyMarketTicker(side='no')."""

    def test_market_no_ticker_populated(self):
        rel = _make_relation(3)
        strat = _build_strategy(rel)

        # Should have 3 entries
        assert len(strat._market_no_ticker) == 3

        for i in range(3):
            mid = f'market-{i}'
            assert mid in strat._market_no_ticker, f'Missing NO ticker for {mid}'

            no_ticker = strat._market_no_ticker[mid]
            assert isinstance(no_ticker, PolyMarketTicker)
            assert no_ticker.side == 'no'
            assert no_ticker.token_id == f'no-tok-{i}'
            assert no_ticker.market_id == mid

    @pytest.mark.asyncio
    async def test_no_ticker_used_in_buy_no(self):
        """When BUY_NO is chosen, the pre-built NO tickers should be used for orders."""
        rel = _make_relation(2, spread_type='exclusivity')
        strat = _build_strategy(rel, min_edge=0.01)

        for i in range(2):
            strat._tickers[f'market-{i}'] = _make_ticker(i)

        # sum_yes = 2 * 0.60 = 1.20 -> overpriced -> BUY_NO
        # edge_buy_no = 1.20 - 1.0 - 0.01 = 0.19
        prices = {f'market-{i}': Decimal('0.60') for i in range(2)}
        trader = _mock_trader(prices)

        await strat._check_arb(trader)

        assert strat._held_direction == 'BUY_NO'
        # The tickers passed to place_order should be NO-side tickers
        for call in trader.place_order.call_args_list:
            ticker_arg = call.kwargs.get('ticker') or call.args[1]
            assert (
                ticker_arg.side == 'no'
            ), f'Expected NO-side ticker but got side={ticker_arg.side}'

    def test_no_tickers_dict_separate_from_yes(self):
        """_no_tickers and _yes_tickers should have same count but different token IDs."""
        rel = _make_relation(4)
        strat = _build_strategy(rel)

        assert len(strat._yes_tickers) == 4
        assert len(strat._no_tickers) == 4

        yes_tokens = set(strat._yes_tickers.keys())
        no_tokens = set(strat._no_tickers.keys())
        assert yes_tokens.isdisjoint(no_tokens), 'YES and NO token IDs must not overlap'

    def test_missing_no_token_skipped(self):
        """If a market has no NO token_id (only 1 token), _market_no_ticker should skip it."""
        markets = [
            {
                'id': 'market-0',
                'event_id': 'event-1',
                'question': 'Outcome 0?',
                'token_ids': ['yes-tok-0', 'no-tok-0'],
            },
            {
                'id': 'market-1',
                'event_id': 'event-1',
                'question': 'Outcome 1?',
                'token_ids': ['yes-tok-1'],  # Only YES token -- no NO
            },
        ]
        rel = MarketRelation(
            relation_id='test-partial-no',
            markets=markets,
            spread_type='complementary',
        )
        strat = _build_strategy(rel)

        # market-0 should have a NO ticker, market-1 should NOT
        assert 'market-0' in strat._market_no_ticker
        assert 'market-1' not in strat._market_no_ticker


# ── Run ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
