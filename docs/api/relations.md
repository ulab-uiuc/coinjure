# Market Relations

Module: `coinjure.market.relations`

The relation system models structural and statistical relationships between prediction markets. Each relation type maps to a dedicated arbitrage strategy.

## Relation Types

| Type            | Constraint                                    | Example                                       |
| --------------- | --------------------------------------------- | --------------------------------------------- |
| `same_event`    | Identical market across platforms (p_A ≈ p_B) | Same question on Polymarket + Kalshi          |
| `complementary` | Outcomes sum to 1 (Σp ≈ 1)                    | "Will X win?" + "Will Y win?" + "Will Z win?" |
| `implication`   | A implies B (p_A ≤ p_B)                       | "By March" implies "By June"                  |
| `exclusivity`   | Mutually exclusive (Σp ≤ 1)                   | Winner-take-all candidates                    |
| `correlated`    | Cointegrated price series                     | Semantically related markets                  |
| `structural`    | Known linear relationship (p_A = α + β·p_B)   | Price nesting                                 |
| `conditional`   | Conditional probability bounds                | p(A\|B) ∈ [lower, upper]                      |
| `temporal`      | Lead-lag information flow                     | Market A leads Market B by N steps            |

## MarketRelation

```python
from coinjure.market.relations import MarketRelation

relation = MarketRelation(
    relation_id="12345-67890",
    markets=[
        {"id": "12345", "question": "Will X happen by March?", "token_ids": ["abc"]},
        {"id": "67890", "question": "Will X happen by June?", "token_ids": ["def"]},
    ],
    spread_type="implication",
    confidence=0.95,
    reasoning="March deadline implies June deadline",
    hypothesis="p_A <= p_B",
)
```

### Fields

| Field             | Type            | Default     | Description                                                                      |
| ----------------- | --------------- | ----------- | -------------------------------------------------------------------------------- |
| `relation_id`     | `str`           | —           | Unique identifier                                                                |
| `markets`         | `list[dict]`    | `[]`        | Market metadata (id, question, token_ids, platform)                              |
| `spread_type`     | `str`           | `"unknown"` | One of the 8 relation types                                                      |
| `confidence`      | `float`         | `0.0`       | Discovery confidence (0.0–1.0)                                                   |
| `reasoning`       | `str`           | `""`        | Why the markets are related                                                      |
| `hypothesis`      | `str`           | `""`        | Mathematical relationship (e.g., `"p_A - p_B ≈ 0"`)                              |
| `hedge_ratio`     | `float`         | `1.0`       | β from OLS regression                                                            |
| `lead_lag`        | `int`           | `0`         | Lead-lag steps (positive = A leads B)                                            |
| `status`          | `str`           | `"active"`  | Lifecycle: `active`, `backtest_passed`, `backtest_failed`, `deployed`, `retired` |
| `backtest_pnl`    | `float \| None` | `None`      | PnL from backtest run                                                            |
| `backtest_trades` | `int \| None`   | `None`      | Trade count from backtest                                                        |

### Methods

| Method                                     | Description                                    |
| ------------------------------------------ | ---------------------------------------------- |
| `set_backtest_result(passed, pnl, trades)` | Update lifecycle based on backtest outcome     |
| `get_token_id(index)`                      | Get YES-side CLOB token_id for market at index |
| `get_no_token_id(index)`                   | Get NO-side CLOB token_id for market at index  |
| `to_dict()`                                | Serialize to dictionary                        |
| `from_dict(d)`                             | Classmethod. Deserialize from dictionary       |

## RelationStore

JSON-backed persistent store at `~/.coinjure/relations.json`.

```python
from coinjure.market.relations import RelationStore

store = RelationStore()

# CRUD
store.add(relation)
store.update(relation)
store.remove("12345-67890")
rel = store.get("12345-67890")

# Query
all_relations = store.list()
implication_rels = store.list(spread_type="implication")
passed = store.list(status="backtest_passed")
related = store.find_by_market("12345")

# Batch
count = store.add_batch([rel1, rel2, rel3])
```

### Methods

| Method                      | Returns                  | Description                                 |
| --------------------------- | ------------------------ | ------------------------------------------- |
| `list(spread_type, status)` | `list[MarketRelation]`   | Filter relations by type and/or status      |
| `get(relation_id)`          | `MarketRelation \| None` | Fetch by ID                                 |
| `add(relation)`             | `None`                   | Upsert a relation                           |
| `update(relation)`          | `None`                   | Update existing or add if not found         |
| `remove(relation_id)`       | `bool`                   | Delete by ID, returns success               |
| `add_batch(relations)`      | `int`                    | Bulk upsert, returns count of new relations |
| `find_by_market(market_id)` | `list[MarketRelation]`   | Find all relations involving a market       |

## Lifecycle

```
active → backtest_passed → deployed → retired
   ↓
backtest_failed
```

Relations progress through lifecycle stages as they are backtested and deployed. The `status` field gates which strategies can advance to paper and live trading.
