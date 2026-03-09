"""Built-in strategies — one per relation type."""

from __future__ import annotations

from typing import Any

from coinjure.market.relations import MarketRelation
from coinjure.strategy.builtin.coint_spread_strategy import CointSpreadStrategy
from coinjure.strategy.builtin.conditional_arb_strategy import ConditionalArbStrategy
from coinjure.strategy.builtin.direct_arb_strategy import DirectArbStrategy
from coinjure.strategy.builtin.group_arb_strategy import GroupArbStrategy
from coinjure.strategy.builtin.implication_arb_strategy import ImplicationArbStrategy
from coinjure.strategy.builtin.lead_lag_strategy import LeadLagStrategy
from coinjure.strategy.builtin.structural_arb_strategy import StructuralArbStrategy

# Relation type → default strategy class mapping
STRATEGY_BY_RELATION = {
    'same_event': DirectArbStrategy,
    'complementary': GroupArbStrategy,
    'implication': ImplicationArbStrategy,
    'exclusivity': GroupArbStrategy,
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
        m0 = relation.markets[0] if relation.markets else {}
        m1 = relation.markets[1] if len(relation.markets) > 1 else {}
        plat_0 = str(m0.get('platform', 'polymarket')).lower()
        if plat_0 == 'kalshi':
            poly_m, kalshi_m, poly_idx = m1, m0, 1
        else:
            poly_m, kalshi_m, poly_idx = m0, m1, 0
        kwargs.setdefault('poly_market_id', str(poly_m.get('id', '')))
        kwargs.setdefault('poly_token_id', relation.get_token_id(poly_idx))
        kwargs.setdefault(
            'kalshi_ticker',
            str(kalshi_m.get('ticker', kalshi_m.get('id', ''))),
        )
    elif spread_type in ('complementary', 'exclusivity'):
        kwargs.setdefault('relation_id', relation.relation_id)
        for m in relation.markets:
            eid = m.get('event_id', '')
            if eid:
                kwargs.setdefault('event_id', str(eid))
                break
    else:
        kwargs.setdefault('relation_id', relation.relation_id)

    module = strategy_cls.__module__
    name = strategy_cls.__name__
    ref = f'{module}:{name}'
    return ref, kwargs


__all__ = [
    'DirectArbStrategy',
    'GroupArbStrategy',
    'ImplicationArbStrategy',
    'LeadLagStrategy',
    'CointSpreadStrategy',
    'ConditionalArbStrategy',
    'StructuralArbStrategy',
    'STRATEGY_BY_RELATION',
    'build_strategy_ref_for_relation',
]
