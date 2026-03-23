# Group Relation Design: MarketRelation Pair → Group

## Problem

`MarketRelation` only supports pairs (`market_a`, `market_b`), but exclusivity
and complementary relations are inherently about N markets (e.g. all candidates
in a presidential election). The pair-only model causes:

- O(n²) pair explosion for N-market events, forcing `max_event_size` caps
- Weaker pairwise constraint `A + B ≤ 1` misses group violations (3 markets at
  0.4 each: every pair sums to 0.8 but group sums to 1.2)
- `arb > 0` filter discards most pairs since pairwise violations are rare
- `EventSumArbStrategy` already handles groups correctly but isn't wired to
  relation discovery

## Design

### 1. Data Structure — `MarketRelation`

Replace `market_a: dict` + `market_b: dict` with `markets: list[dict]`.

```python
@dataclass
class MarketRelation:
    relation_id: str
    markets: list[dict[str, Any]] = field(default_factory=list)
    spread_type: str = 'unknown'
    ...

    def get_token_id(self, index: int = 0) -> str: ...
    def get_no_token_id(self, index: int = 0) -> str: ...
```

`from_dict()` auto-migrates old format:

```python
if 'market_a' in d and 'markets' not in d:
    d['markets'] = [d.pop('market_a'), d.pop('market_b')]
```

### 2. File Renames

| Old                   | New                       |
| --------------------- | ------------------------- |
| `market/auto_pair.py` | `market/auto_discover.py` |
| `AutoPairResult`      | `DiscoveryResult`         |
| `auto_pair_markets()` | `discover_relations()`    |
| CLI `--auto-pair`     | `--auto-discover`         |

### 3. Detection Logic — `auto_discover.py`

- **Implication (date nesting)**: stays pairwise (`len(markets) == 2`)
- **Exclusivity / Complementary**: one group relation per event, `markets`
  contains all active markets in the event
- Remove `arb > 0` filter — keep all structural relations
- Remove `max_event_size` caps — groups don't explode
- Add Kalshi intra-event detection (currently unused)

### 4. Strategy Merge

Merge `ExclusivityArbStrategy` + `EventSumArbStrategy` → `GroupArbStrategy`.

- Receives `relation_id`, loads `markets` list from relation
- Checks `Σ(prices)` vs 1.0
- Direction based on `spread_type` (exclusivity: ≤1, complementary: ≈1)
- Replaces two separate strategies with one unified implementation

### 5. Downstream Adaptation

- **`RelationArbMixin`**: `_id_a/_id_b/_token_a/_token_b` → `_ids: list` /
  `_tokens: list`. Pair strategies use `[0]`/`[1]`.
- **`backtester.py`**: `_make_ticker(relation, leg='a')` →
  `_make_ticker(relation, index=0)`
- **`market_commands.py`**: display adapts to N markets, `relations add` gets
  `--market-ids` option
- **`builtin/__init__.py`**: route exclusivity/complementary → `GroupArbStrategy`
- **Tests**: update all fixtures

### 6. Storage Compatibility

`MarketRelation.from_dict()` handles old `{market_a, market_b}` format
transparently. No migration script needed.

## Affected Files

### Core (high impact)

- `coinjure/market/relations.py` — MarketRelation dataclass
- `coinjure/market/auto_pair.py` → `auto_discover.py` — detection logic
- `coinjure/strategy/relation_mixin.py` — RelationArbMixin
- `coinjure/strategy/builtin/exclusivity_arb_strategy.py` → removed
- `coinjure/strategy/builtin/event_sum_arb_strategy.py` → `group_arb_strategy.py`
- `coinjure/strategy/builtin/__init__.py` — routing

### Medium impact

- `coinjure/cli/market_commands.py` — display + CLI flags
- `coinjure/engine/backtester.py` — ticker creation from legs

### Tests

- `tests/test_auto_pair.py` → `tests/test_auto_discover.py`
- `tests/test_relations.py`
- `tests/test_backtester.py`

### Unaffected

- `DirectArbStrategy` — doesn't use relations
- `RelationStore` persistence — JSON flexible, auto-migration handles it
- `ImplicationArbStrategy`, `CointSpreadStrategy`, etc. — pair strategies
  just use `markets[0]`/`markets[1]`
