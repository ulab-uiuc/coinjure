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
        assert loaded.status == 'active'  # set_validation no longer changes status
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
# ── Correlation-Aware Risk ───────────────────────────────────────────────


class TestCorrelationAwareRisk:
    def test_init(self):
        from coinjure.data.manager import DataManager
        from coinjure.trading.position import PositionManager
        from coinjure.trading.risk import CorrelationAwareRiskManager

        pm = PositionManager()
        md = DataManager()
        rm = CorrelationAwareRiskManager(
            position_manager=pm,
            market_data=md,
            max_correlated_exposure=Decimal('5000'),
        )
        assert rm.max_correlated_exposure == Decimal('5000')

    def test_set_correlation(self):
        from coinjure.data.manager import DataManager
        from coinjure.trading.position import PositionManager
        from coinjure.trading.risk import CorrelationAwareRiskManager

        pm = PositionManager()
        md = DataManager()
        rm = CorrelationAwareRiskManager(position_manager=pm, market_data=md)
        rm.set_correlation('A', 'B', 0.9)
        assert rm._correlations['A']['B'] == 0.9
        assert rm._correlations['B']['A'] == 0.9
