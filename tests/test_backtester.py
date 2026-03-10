"""Tests for the relation-based backtester."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from coinjure.engine.backtester import (
    BacktestResult,
    Leg,
    PriceHistoryDataSource,
    _build_same_event_kwargs,
    _make_ticker,
    run_backtest_relation,
)
from coinjure.events import OrderBookEvent, PriceChangeEvent
from coinjure.market.relations import MarketRelation
from coinjure.ticker import KalshiTicker, PolyMarketTicker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_relation(
    spread_type: str = 'implication',
    *,
    market_ids: tuple[str, str] = ('MA', 'MB'),
    token_ids: tuple[str, str] = ('TA', 'TB'),
    event_id: str = 'E1',
) -> MarketRelation:
    return MarketRelation(
        relation_id=f'{market_ids[0]}-{market_ids[1]}',
        markets=[
            {
                'id': market_ids[0],
                'question': 'Market 0 question?',
                'event_id': event_id,
                'token_ids': [token_ids[0]],
            },
            {
                'id': market_ids[1],
                'question': 'Market 1 question?',
                'event_id': event_id,
                'token_ids': [token_ids[1]],
            },
        ],
        spread_type=spread_type,
        confidence=0.95,
    )


def _price_series(values: list[float], start_ts: int = 1000000) -> list[dict]:
    """Create a price series from a list of floats."""
    return [{'t': start_ts + i * 60, 'p': v} for i, v in enumerate(values)]


# ---------------------------------------------------------------------------
# PriceHistoryDataSource
# ---------------------------------------------------------------------------


class TestPriceHistoryDataSource:
    def test_builds_events_from_two_series(self):
        ticker_a = PolyMarketTicker(
            symbol='TA',
            name='A',
            token_id='TA',
            market_id='MA',
            event_id='E1',
        )
        ticker_b = PolyMarketTicker(
            symbol='TB',
            name='B',
            token_id='TB',
            market_id='MB',
            event_id='E1',
        )
        prices_a = _price_series([0.5, 0.6])
        prices_b = _price_series([0.3, 0.4])

        ds = PriceHistoryDataSource(
            [(ticker_a, prices_a, None), (ticker_b, prices_b, None)]
        )

        # Each price point produces: PriceChange + bid OrderBook + ask OrderBook = 3 events
        # 2 series × 2 points = 4 price points × 3 = 12 events
        events = asyncio.run(self._drain(ds))
        assert len(events) == 12

        # Check event types alternate: price, bid, ask
        for i in range(0, len(events), 3):
            assert isinstance(events[i], PriceChangeEvent)
            assert isinstance(events[i + 1], OrderBookEvent)
            assert events[i + 1].side == 'bid'
            assert isinstance(events[i + 2], OrderBookEvent)
            assert events[i + 2].side == 'ask'

    def test_events_sorted_by_timestamp(self):
        ticker_a = PolyMarketTicker(
            symbol='TA',
            name='A',
            token_id='TA',
            market_id='MA',
            event_id='E1',
        )
        ticker_b = PolyMarketTicker(
            symbol='TB',
            name='B',
            token_id='TB',
            market_id='MB',
            event_id='E1',
        )
        # B series starts before A
        prices_a = _price_series([0.5], start_ts=2000)
        prices_b = _price_series([0.3], start_ts=1000)

        ds = PriceHistoryDataSource(
            [(ticker_a, prices_a, None), (ticker_b, prices_b, None)]
        )
        events = asyncio.run(self._drain(ds))

        # First event should be from B (earlier timestamp)
        assert events[0].ticker.symbol == 'TB'

    def test_returns_none_when_exhausted(self):
        ticker_a = PolyMarketTicker(
            symbol='TA',
            name='A',
            token_id='TA',
            market_id='MA',
            event_id='E1',
        )
        ds = PriceHistoryDataSource([(ticker_a, _price_series([0.5]), None)])
        events = asyncio.run(self._drain(ds))
        # Should get exactly 3 events (1 price point × 3)
        assert len(events) == 3

    def test_skips_invalid_price_points(self):
        ticker_a = PolyMarketTicker(
            symbol='TA',
            name='A',
            token_id='TA',
            market_id='MA',
            event_id='E1',
        )
        prices_a = [
            {'t': 1000, 'p': 0.5},
            {'t': 'bad', 'p': 0.6},  # bad timestamp
            {'p': 0.7},  # missing timestamp
        ]
        ds = PriceHistoryDataSource([(ticker_a, prices_a, None)])
        events = asyncio.run(self._drain(ds))
        assert len(events) == 3  # only the valid point

    def test_mixed_poly_and_kalshi_tickers(self):
        poly = PolyMarketTicker(
            symbol='TA',
            name='A',
            token_id='TA',
            market_id='MA',
            event_id='E1',
        )
        kalshi = KalshiTicker(
            symbol='KXBTC',
            name='B',
            market_ticker='KXBTC-25MAR14',
        )
        prices_a = _price_series([0.5])
        prices_b = _price_series([0.6])

        ds = PriceHistoryDataSource([(poly, prices_a, None), (kalshi, prices_b, None)])
        events = asyncio.run(self._drain(ds))
        assert len(events) == 6  # 2 points × 3 events each

        # Verify both ticker types appear
        ticker_types = {type(e.ticker) for e in events}
        assert PolyMarketTicker in ticker_types
        assert KalshiTicker in ticker_types

    @staticmethod
    async def _drain(ds: PriceHistoryDataSource) -> list:
        events = []
        while True:
            e = await ds.get_next_event()
            if e is None:
                break
            events.append(e)
        return events


# ---------------------------------------------------------------------------
# _make_ticker
# ---------------------------------------------------------------------------


class TestMakeTicker:
    def test_creates_polymarket_ticker_by_default(self):
        rel = _make_relation()
        ticker = _make_ticker(rel, 0)
        assert isinstance(ticker, PolyMarketTicker)
        assert ticker.symbol == 'TA'
        assert ticker.market_id == 'MA'

    def test_creates_kalshi_ticker_for_kalshi_platform(self):
        rel = _make_relation()
        rel.markets[1]['platform'] = 'kalshi'
        rel.markets[1]['ticker'] = 'KXBTC-25MAR14'
        rel.markets[1]['event_ticker'] = 'KXBTC'
        ticker = _make_ticker(rel, 1)
        assert isinstance(ticker, KalshiTicker)
        assert ticker.market_ticker == 'KXBTC-25MAR14'
        assert ticker.event_ticker == 'KXBTC'

    def test_creates_polymarket_ticker_for_leg_b(self):
        rel = _make_relation()
        ticker = _make_ticker(rel, 1)
        assert isinstance(ticker, PolyMarketTicker)
        assert ticker.symbol == 'TB'
        assert ticker.market_id == 'MB'


# ---------------------------------------------------------------------------
# run_backtest_relation
# ---------------------------------------------------------------------------


class TestBuildSameEventKwargs:
    def test_poly_a_kalshi_b(self):
        rel = _make_relation(spread_type='same_event')
        rel.markets[0]['platform'] = 'polymarket'
        rel.markets[1]['platform'] = 'kalshi'
        rel.markets[1]['ticker'] = 'KXBTC-25MAR14'
        kwargs: dict = {}
        _build_same_event_kwargs(kwargs, rel)
        assert kwargs['poly_market_id'] == 'MA'
        assert kwargs['poly_token_id'] == 'TA'
        assert kwargs['kalshi_ticker'] == 'KXBTC-25MAR14'

    def test_kalshi_a_poly_b(self):
        rel = _make_relation(spread_type='same_event')
        rel.markets[0]['platform'] = 'kalshi'
        rel.markets[0]['ticker'] = 'K-MKT'
        rel.markets[1]['platform'] = 'polymarket'
        kwargs: dict = {}
        _build_same_event_kwargs(kwargs, rel)
        assert kwargs['poly_market_id'] == 'MB'
        assert kwargs['poly_token_id'] == 'TB'
        assert kwargs['kalshi_ticker'] == 'K-MKT'


class TestRunBacktestRelation:
    def test_unknown_type_returns_error(self):
        rel = _make_relation(spread_type='nonexistent_type')
        result = asyncio.run(run_backtest_relation(rel))
        assert result.error is not None
        assert 'No strategy' in result.error

    def test_insufficient_price_data_returns_error(self, monkeypatch):
        """When API returns too few price points, should error."""
        rel = _make_relation(spread_type='implication')

        async def mock_fetch_leg(market, token_id):
            return [{'t': 1000, 'p': 0.5}]

        monkeypatch.setattr(
            'coinjure.engine.backtester._fetch_leg_prices',
            mock_fetch_leg,
        )
        result = asyncio.run(run_backtest_relation(rel))
        assert result.error is not None
        assert 'Insufficient' in result.error

    def test_backtest_result_fields(self):
        result = BacktestResult(
            relation_id='test-id',
            spread_type='implication',
            strategy_name='test',
            total_pnl=Decimal('100'),
            trade_count=5,
            passed=True,
        )
        assert result.relation_id == 'test-id'
        assert result.passed is True
        assert result.total_pnl == Decimal('100')
        assert result.error is None

    def test_fetch_failure_returns_error(self, monkeypatch):
        """When price fetch raises, should capture the error."""
        rel = _make_relation(spread_type='implication')

        async def mock_fetch_leg(market, token_id):
            raise RuntimeError('API down')

        monkeypatch.setattr(
            'coinjure.engine.backtester._fetch_leg_prices',
            mock_fetch_leg,
        )
        result = asyncio.run(run_backtest_relation(rel))
        assert result.error is not None
        assert 'API down' in result.error
        assert not result.passed

    def test_structural_type_runs_full_data(self, monkeypatch, tmp_path):
        """Structural types should run on full data (not walk-forward split)."""
        rel = _make_relation(spread_type='implication')
        # Save relation to store so strategy can load it
        from coinjure.market.relations import RelationStore

        store = RelationStore(tmp_path / 'rel.json')
        store.add(rel)
        monkeypatch.setattr(
            'coinjure.market.relations.RELATIONS_PATH',
            tmp_path / 'rel.json',
        )

        # Generate enough price data with a violation pattern
        # A > B means implication violation → strategy should trade
        prices_a = _price_series([0.6 + i * 0.001 for i in range(50)])
        prices_b = _price_series([0.4 + i * 0.001 for i in range(50)])
        leg_prices = [prices_a, prices_b]
        call_idx = 0

        async def mock_fetch_leg(market, token_id):
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            return leg_prices[idx % len(leg_prices)]

        monkeypatch.setattr(
            'coinjure.engine.backtester._fetch_leg_prices',
            mock_fetch_leg,
        )
        result = asyncio.run(run_backtest_relation(rel))
        assert result.error is None
        assert result.spread_type == 'implication'
        assert result.strategy_name == 'implication_arb'

    def test_statistical_type_uses_walk_forward(self, monkeypatch, tmp_path):
        """Statistical types should use 60/40 walk-forward split."""
        rel = _make_relation(spread_type='correlated')
        from coinjure.market.relations import RelationStore

        store = RelationStore(tmp_path / 'rel.json')
        store.add(rel)
        monkeypatch.setattr(
            'coinjure.market.relations.RELATIONS_PATH',
            tmp_path / 'rel.json',
        )

        # Need enough data for 60/40 split (at least ~13 points so test has ≥5)
        prices_a = _price_series([0.5 + i * 0.002 for i in range(50)])
        prices_b = _price_series([0.5 - i * 0.001 for i in range(50)])
        leg_prices = [prices_a, prices_b]
        call_idx = 0

        async def mock_fetch_leg(market, token_id):
            nonlocal call_idx
            idx = call_idx
            call_idx += 1
            return leg_prices[idx % len(leg_prices)]

        monkeypatch.setattr(
            'coinjure.engine.backtester._fetch_leg_prices',
            mock_fetch_leg,
        )
        result = asyncio.run(run_backtest_relation(rel))
        assert result.error is None
        assert result.spread_type == 'correlated'
        assert result.strategy_name == 'coint_spread'
