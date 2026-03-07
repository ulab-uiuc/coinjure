"""Tests for market relations, validation, unified market, and spread executor."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest


def _has_statsmodels():
    try:
        import statsmodels  # noqa: F401

        return True
    except ImportError:
        return False


# ── Unified Market ───────────────────────────────────────────────────────


class TestUnifiedMarket:
    def test_from_polymarket(self):
        from coinjure.market.market_model import Market

        data = {
            'id': '123',
            'question': 'Will X happen?',
            'description': 'Some description',
            'bestBid': '0.45',
            'bestAsk': '0.50',
            'volume': '1000',
            'endDate': '2026-12-31',
            'active': True,
            'clobTokenIds': '["token1", "token2"]',
        }
        m = Market.from_polymarket(data)
        assert m.platform == 'polymarket'
        assert m.question == 'Will X happen?'
        assert m.best_bid == Decimal('0.45')
        assert m.best_ask == Decimal('0.50')
        assert m.mid_price == Decimal('0.475')
        assert m.token_id == 'token1'
        assert m.no_token_id == 'token2'

    def test_from_kalshi(self):
        from coinjure.market.market_model import Market

        data = {
            'ticker': 'KALSHI-123',
            'title': 'Will Y happen?',
            'yes_bid': 45,
            'yes_ask': 50,
            'close_time': '2026-12-31',
            'event_ticker': 'EVT-1',
        }
        m = Market.from_kalshi(data)
        assert m.platform == 'kalshi'
        assert m.question == 'Will Y happen?'
        assert m.best_bid == Decimal('0.45')
        assert m.best_ask == Decimal('0.50')
        assert m.event_id == 'EVT-1'

    def test_summary(self):
        from coinjure.market.market_model import Market

        m = Market(
            market_id='test',
            platform='polymarket',
            question='Will something happen by end of year?',
            best_bid=Decimal('0.6'),
            best_ask=Decimal('0.7'),
        )
        s = m.summary()
        assert 'polymarket' in s
        assert 'something' in s


# ── Relations ────────────────────────────────────────────────────────────


class TestRelationStore:
    def test_crud(self, tmp_path):
        from coinjure.market.relations import MarketRelation, RelationStore

        store = RelationStore(path=tmp_path / 'relations.json')

        r = MarketRelation(
            relation_id='test-1',
            market_a={'market_id': 'A'},
            market_b={'market_id': 'B'},
            spread_type='same_event',
            confidence=0.9,
        )
        store.add(r)

        loaded = store.get('test-1')
        assert loaded is not None
        assert loaded.confidence == 0.9

        loaded.confidence = 0.95
        store.update(loaded)
        assert store.get('test-1').confidence == 0.95

        assert store.remove('test-1')
        assert store.get('test-1') is None

    def test_graph_queries(self, tmp_path):
        from coinjure.market.relations import MarketRelation, RelationStore

        store = RelationStore(path=tmp_path / 'relations.json')

        store.add(
            MarketRelation(
                relation_id='r1',
                market_a={'market_id': 'M1'},
                market_b={'market_id': 'M2'},
                confidence=0.8,
            )
        )
        store.add(
            MarketRelation(
                relation_id='r2',
                market_a={'market_id': 'M2'},
                market_b={'market_id': 'M3'},
                confidence=0.9,
            )
        )
        store.add(
            MarketRelation(
                relation_id='r3',
                market_a={'market_id': 'M4'},
                market_b={'market_id': 'M5'},
                confidence=0.5,
            )
        )

        # find_by_market
        m2_relations = store.find_by_market('M2')
        assert len(m2_relations) == 2

        # strongest
        top2 = store.strongest(n=2)
        assert len(top2) == 2
        assert top2[0].confidence == 0.9

    def test_validation_lifecycle(self, tmp_path):
        from coinjure.market.relations import (
            MarketRelation,
            RelationStore,
            ValidationResult,
        )

        store = RelationStore(path=tmp_path / 'relations.json')
        r = MarketRelation(relation_id='v1', confidence=0.8)
        store.add(r)

        result = ValidationResult(
            adf_statistic=-3.5,
            adf_pvalue=0.01,
            is_stationary=True,
            is_cointegrated=True,
            coint_pvalue=0.02,
        )
        r.set_validation(result)
        store.update(r)

        loaded = store.get('v1')
        assert loaded.status == 'validated'
        vr = loaded.get_validation()
        assert vr.is_valid
        assert vr.adf_pvalue == 0.01

    def test_invalidate_retire(self, tmp_path):
        from coinjure.market.relations import MarketRelation, RelationStore

        store = RelationStore(path=tmp_path / 'relations.json')
        store.add(MarketRelation(relation_id='x1', confidence=0.5))

        assert store.invalidate('x1', reason='test')
        assert store.get('x1').status == 'invalidated'

        assert store.retire('x1')
        assert store.get('x1').status == 'retired'


# ── Validation ───────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not _has_statsmodels(),
    reason='statsmodels not installed',
)
class TestValidation:
    def test_validate_cointegrated_pair(self):
        import numpy as np

        from coinjure.market.validation import validate_relation

        np.random.seed(42)
        n = 200
        noise = np.cumsum(np.random.randn(n) * 0.01)
        prices_a = [0.5 + noise[i] + np.random.randn() * 0.003 for i in range(n)]
        prices_b = [0.5 + noise[i] + np.random.randn() * 0.003 for i in range(n)]

        result = validate_relation(prices_a, prices_b)
        assert result.is_valid
        assert result.is_cointegrated
        assert result.hedge_ratio is not None
        assert abs(result.hedge_ratio - 1.0) < 0.5

    def test_validate_unrelated_pair(self):
        import numpy as np

        from coinjure.market.validation import validate_relation

        np.random.seed(42)
        n = 200
        prices_a = [0.5 + np.cumsum(np.random.randn(1) * 0.01)[0] for _ in range(n)]
        prices_b = [0.5 + np.cumsum(np.random.randn(1) * 0.01)[0] for _ in range(n)]

        result = validate_relation(prices_a, prices_b)
        # Not guaranteed to be invalid but correlation should be low
        assert result.correlation is not None

    def test_insufficient_data(self):
        from coinjure.market.validation import validate_relation

        result = validate_relation([0.5] * 10, [0.5] * 10)
        assert not result.is_valid


# ── Monitoring ───────────────────────────────────────────────────────────


class TestMonitoring:
    def test_pnl_alerts(self):
        from coinjure.engine.monitoring import MonitorConfig, StrategyMonitor

        config = MonitorConfig(max_drawdown_pct=0.10, max_consecutive_losses=3)
        monitor = StrategyMonitor(config)

        # 15% drawdown should trigger
        alerts = monitor.check_pnl([100, 95, 90, 85], 100)
        assert any(a.alert_type == 'drawdown' for a in alerts)

    def test_consecutive_losses(self):
        from coinjure.engine.monitoring import MonitorConfig, StrategyMonitor

        config = MonitorConfig(max_consecutive_losses=3)
        monitor = StrategyMonitor(config)

        # 4 consecutive losses
        alerts = monitor.check_pnl([100, 99, 98, 97, 96], 100)
        assert any(a.alert_type == 'consecutive_losses' for a in alerts)

    def test_correlation_break(self):
        from coinjure.engine.monitoring import MonitorConfig, StrategyMonitor

        config = MonitorConfig(min_correlation=0.5)
        monitor = StrategyMonitor(config)

        alerts = monitor.check_relation_validity(0.2, 0.9)
        assert any(a.alert_type == 'correlation_break' for a in alerts)

    def test_should_retire(self):
        from coinjure.engine.monitoring import MonitorConfig, StrategyMonitor

        config = MonitorConfig(max_drawdown_pct=0.05)
        monitor = StrategyMonitor(config)
        monitor.check_pnl([100, 90], 100)

        should, reason = monitor.should_retire()
        assert should
        assert 'drawdown' in reason


# ── Spread Executor ──────────────────────────────────────────────────────


class TestSpreadExecutor:
    def test_spread_leg_creation(self):
        from coinjure.engine.trader.spread_executor import SpreadLeg
        from coinjure.engine.trader.types import TradeSide
        from coinjure.ticker import PolyMarketTicker

        ticker = PolyMarketTicker(symbol='TEST', name='Test')
        leg = SpreadLeg(
            side=TradeSide.BUY,
            ticker=ticker,
            limit_price=Decimal('0.50'),
            quantity=Decimal('10'),
        )
        assert leg.side == TradeSide.BUY
        assert leg.quantity == Decimal('10')

    def test_spread_order_result(self):
        from coinjure.engine.trader.spread_executor import SpreadOrderResult

        result = SpreadOrderResult(success=True, leg_results=[])
        assert result.success
        assert not result.hedged


# ── Correlation-Aware Risk ───────────────────────────────────────────────


class TestCorrelationAwareRisk:
    def test_init(self):
        from coinjure.data.data_manager import DataManager
        from coinjure.engine.trader.position_manager import PositionManager
        from coinjure.engine.trader.risk_manager import CorrelationAwareRiskManager

        pm = PositionManager()
        md = DataManager()
        rm = CorrelationAwareRiskManager(
            position_manager=pm,
            market_data=md,
            max_correlated_exposure=Decimal('5000'),
        )
        assert rm.max_correlated_exposure == Decimal('5000')

    def test_set_correlation(self):
        from coinjure.data.data_manager import DataManager
        from coinjure.engine.trader.position_manager import PositionManager
        from coinjure.engine.trader.risk_manager import CorrelationAwareRiskManager

        pm = PositionManager()
        md = DataManager()
        rm = CorrelationAwareRiskManager(position_manager=pm, market_data=md)
        rm.set_correlation('A', 'B', 0.9)
        assert rm._correlations['A']['B'] == 0.9
        assert rm._correlations['B']['A'] == 0.9
