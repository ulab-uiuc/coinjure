"""Built-in strategies — one per relation type."""

from __future__ import annotations

from typing import Any

from coinjure.market.relations import MarketRelation
from coinjure.strategy.builtin.coint_spread_strategy import CointSpreadStrategy
from coinjure.strategy.builtin.conditional_arb_strategy import ConditionalArbStrategy
from coinjure.strategy.builtin.direct_arb_strategy import DirectArbStrategy
from coinjure.strategy.builtin.event_sum_arb_strategy import EventSumArbStrategy
from coinjure.strategy.builtin.exclusivity_arb_strategy import ExclusivityArbStrategy
from coinjure.strategy.builtin.implication_arb_strategy import ImplicationArbStrategy
from coinjure.strategy.builtin.lead_lag_strategy import LeadLagStrategy
from coinjure.strategy.builtin.structural_arb_strategy import StructuralArbStrategy

# Relation type → default strategy class mapping
STRATEGY_BY_RELATION = {
    'same_event': DirectArbStrategy,
    'complementary': EventSumArbStrategy,
    'implication': ImplicationArbStrategy,
    'exclusivity': ExclusivityArbStrategy,
    'correlated': CointSpreadStrategy,
    'structural': StructuralArbStrategy,
    'conditional': ConditionalArbStrategy,
    'temporal': LeadLagStrategy,
}


def build_strategy_ref_for_relation(
    relation: MarketRelation,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Build (strategy_ref, strategy_kwargs) for a relation.

    Returns (None, {}) if no strategy maps to the relation's spread_type.
    """
    strategy_cls = STRATEGY_BY_RELATION.get(relation.spread_type)
    if strategy_cls is None:
        return None, {}

    kwargs: dict[str, Any] = dict(extra_kwargs or {})
    spread_type = relation.spread_type

    if spread_type == 'same_event':
        plat_a = str(relation.market_a.get('platform', 'polymarket')).lower()
        if plat_a == 'kalshi':
            poly_m, kalshi_m, poly_leg = relation.market_b, relation.market_a, 'b'
        else:
            poly_m, kalshi_m, poly_leg = relation.market_a, relation.market_b, 'a'
        kwargs.setdefault('poly_market_id', str(poly_m.get('id', '')))
        kwargs.setdefault('poly_token_id', relation.get_token_id(poly_leg))
        kwargs.setdefault(
            'kalshi_ticker',
            str(kalshi_m.get('ticker', kalshi_m.get('id', ''))),
        )
    elif spread_type == 'complementary':
        kwargs.setdefault('event_id', str(relation.market_a.get('event_id', '')))
    else:
        kwargs.setdefault('relation_id', relation.relation_id)

    module = strategy_cls.__module__
    name = strategy_cls.__name__
    ref = f'{module}:{name}'
    return ref, kwargs


__all__ = [
    'DirectArbStrategy',
    'EventSumArbStrategy',
    'ImplicationArbStrategy',
    'ExclusivityArbStrategy',
    'LeadLagStrategy',
    'CointSpreadStrategy',
    'ConditionalArbStrategy',
    'StructuralArbStrategy',
    'STRATEGY_BY_RELATION',
    'build_strategy_ref_for_relation',
]
