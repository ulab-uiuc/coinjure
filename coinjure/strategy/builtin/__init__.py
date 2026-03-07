"""Built-in strategies — one per relation type."""

from __future__ import annotations

from coinjure.strategy.builtin.coint_spread_strategy import CointSpreadStrategy
from coinjure.strategy.builtin.conditional_arb_strategy import ConditionalArbStrategy
from coinjure.strategy.builtin.direct_arb_strategy import DirectArbStrategy
from coinjure.strategy.builtin.event_sum_arb_strategy import EventSumArbStrategy
from coinjure.strategy.builtin.exclusivity_arb_strategy import ExclusivityArbStrategy
from coinjure.strategy.builtin.implication_arb_strategy import ImplicationArbStrategy
from coinjure.strategy.builtin.lead_lag_strategy import LeadLagStrategy
from coinjure.strategy.builtin.structural_arb_strategy import StructuralArbStrategy

# Relation type → strategy class mapping
STRATEGY_BY_RELATION = {
    'same_event': DirectArbStrategy,
    'complementary': EventSumArbStrategy,
    'implication': ImplicationArbStrategy,
    'exclusivity': ExclusivityArbStrategy,
    'temporal': LeadLagStrategy,
    'semantic': CointSpreadStrategy,
    'conditional': ConditionalArbStrategy,
    'structural': StructuralArbStrategy,
}

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
]
