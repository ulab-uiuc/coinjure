# Group Relation Refactor — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace pair-only `MarketRelation` (`market_a`/`market_b`) with group-based `markets: list[dict]`, merge exclusivity+event-sum strategies into `GroupArbStrategy`, and rename `auto_pair` → `auto_discover`.

**Architecture:** `MarketRelation.markets` is a list of N dicts. Pair relations have `len(markets)==2`. Group relations (exclusivity, complementary) have `len(markets)>=2`. All downstream code uses index-based access (`markets[0]`, `markets[1]`) or iteration.

**Tech Stack:** Python 3.10+, Click CLI, asyncio, Decimal arithmetic, JSON persistence.

---

### Task 1: Update `MarketRelation` data structure

**Files:**

- Modify: `coinjure/market/relations.py`
- Test: `tests/test_relations.py`

**Step 1: Update test fixtures to use `markets` list**

In `tests/test_relations.py`, update all `MarketRelation(...)` calls:

```python
# Old:
MarketRelation(relation_id='test-1', market_a={'market_id': 'A'}, market_b={'market_id': 'B'}, ...)

# New:
MarketRelation(relation_id='test-1', markets=[{'market_id': 'A'}, {'market_id': 'B'}], ...)
```

Update `test_graph_queries` similarly. Update `test_validation_lifecycle` and `test_invalidate_retire` (these don't set markets, so just ensure they still work with default empty list).

**Step 2: Run tests to verify they fail**

Run: `poetry run python -m pytest tests/test_relations.py -v -p no:nbmake`
Expected: FAIL — `markets` is not a valid field

**Step 3: Modify `MarketRelation` dataclass**

In `coinjure/market/relations.py`:

Replace:

```python
market_a: dict[str, Any] = field(default_factory=dict)
market_b: dict[str, Any] = field(default_factory=dict)
```

With:

```python
markets: list[dict[str, Any]] = field(default_factory=list)
```

Update `get_token_id`:

```python
def get_token_id(self, index: int = 0) -> str:
    """Get the first YES-side CLOB token_id for market at index.

    Checks token_ids (list), then token_id (singular), then falls back
    to the market dict's 'id' field.
    """
    if index >= len(self.markets):
        return ''
    m = self.markets[index]
    token_ids = m.get('token_ids', [])
    if token_ids:
        return str(token_ids[0])
    token_id = m.get('token_id', '')
    if token_id:
        return str(token_id)
    return str(m.get('id', ''))
```

Update `get_no_token_id`:

```python
def get_no_token_id(self, index: int = 0) -> str:
    """Get the NO-side CLOB token_id for market at index.

    Checks token_ids[1] (list), then no_token_id (singular).
    Returns empty string if not available.
    """
    if index >= len(self.markets):
        return ''
    m = self.markets[index]
    token_ids = m.get('token_ids', [])
    if len(token_ids) >= 2:
        return str(token_ids[1])
    no_token_id = m.get('no_token_id', '')
    if no_token_id:
        return str(no_token_id)
    return ''
```

Update `from_dict` to auto-migrate old format:

```python
@classmethod
def from_dict(cls, d: dict[str, Any]) -> MarketRelation:
    known = {f.name for f in cls.__dataclass_fields__.values()}
    # Auto-migrate old pair format
    if 'market_a' in d and 'markets' not in d:
        d['markets'] = [d.pop('market_a'), d.pop('market_b', {})]
    filtered = {k: v for k, v in d.items() if k in known}
    return cls(**filtered)
```

Update `RelationStore.find_by_market` to iterate `markets` list:

```python
def find_by_market(self, market_id: str) -> list[MarketRelation]:
    """Return all relations involving a given market (by any id field)."""
    results = []
    for r in self.list():
        for m in r.markets:
            ids = {
                m.get('market_id', ''),
                m.get('id', ''),
                m.get('ticker', ''),
                *m.get('token_ids', []),
            }
            if market_id in ids:
                results.append(r)
                break
    return results
```

**Step 4: Run tests to verify they pass**

Run: `poetry run python -m pytest tests/test_relations.py -v -p no:nbmake`
Expected: PASS

**Step 5: Commit**

```
feat(relations): replace market_a/market_b with markets list
```

---

### Task 2: Update `RelationArbMixin` and pair strategies

**Files:**

- Modify: `coinjure/strategy/relation_mixin.py`
- Modify: `coinjure/strategy/builtin/implication_arb_strategy.py`
- Modify: `coinjure/strategy/builtin/coint_spread_strategy.py`
- Modify: `coinjure/strategy/builtin/structural_arb_strategy.py`
- Modify: `coinjure/strategy/builtin/conditional_arb_strategy.py`
- Modify: `coinjure/strategy/builtin/lead_lag_strategy.py`

**Step 1: Update `RelationArbMixin`**

In `coinjure/strategy/relation_mixin.py`, replace `_id_a/_id_b/_token_a/_token_b` with lists:

```python
class RelationArbMixin:
    """Provides common relation loading, token watching, and ticker matching."""

    _relation: object | None
    _ids: list[str]
    _tokens: list[str]

    def _init_from_relation(self, relation_id: str) -> None:
        """Load relation from store and extract market/token IDs."""
        self._relation = None
        self._ids = []
        self._tokens = []
        if relation_id:
            store = RelationStore()
            self._relation = store.get(relation_id)
            if self._relation and self._relation.status == 'invalidated':
                raise ValueError(
                    f'Relation {relation_id} is invalidated — validate it first'
                )

        if self._relation:
            for m in self._relation.markets:
                self._ids.append(
                    m.get('condition_id', '') or m.get('id', '')
                )
                self._tokens.append(m.get('token_id', ''))

    def watch_tokens(self) -> list[str]:
        """Return CLOB token IDs so the data source prioritizes these markets."""
        return [t for t in self._tokens if t]

    def _matches(self, ticker_id: str, market_id: str, token_id: str = '') -> bool:
        if market_id and (market_id in ticker_id or ticker_id in market_id):
            return True
        if token_id and ticker_id == token_id:
            return True
        return False
```

**Step 2: Update all pair strategies** that use `self._id_a` → `self._ids[0]`, `self._id_b` → `self._ids[1]`, `self._token_a` → `self._tokens[0]`, `self._token_b` → `self._tokens[1]`.

Helper to get token safely:

```python
# In each strategy that accesses tokens by index, use:
# self._ids[0] if len(self._ids) > 0 else ''
# self._ids[1] if len(self._ids) > 1 else ''
# etc.
```

In each pair strategy file (`implication_arb_strategy.py`, `coint_spread_strategy.py`, `structural_arb_strategy.py`, `conditional_arb_strategy.py`, `lead_lag_strategy.py`):

Replace all occurrences of:

- `self._id_a` → `self._ids[0]`
- `self._id_b` → `self._ids[1]`
- `self._token_a` → `self._tokens[0] if self._tokens else ''`
- `self._token_b` → `self._tokens[1] if len(self._tokens) > 1 else ''`

In `_matches` calls like:

```python
# Old:
self._matches(tid, self._id_a, self._token_a)
# New:
self._matches(tid, self._ids[0], self._tokens[0] if self._tokens else '')
```

**Step 3: Run tests**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x`
Expected: Some failures in test_auto_pair and test_backtester (they still use market_a/market_b). Pair strategy tests should pass.

**Step 4: Commit**

```
refactor(strategies): update RelationArbMixin to use markets list
```

---

### Task 3: Rename `auto_pair.py` → `auto_discover.py` and update detection logic

**Files:**

- Rename: `coinjure/market/auto_pair.py` → `coinjure/market/auto_discover.py`
- Rename: `tests/test_auto_pair.py` → `tests/test_auto_discover.py`
- Modify: `coinjure/cli/market_commands.py` (import path)

**Step 1: Rename files**

```bash
git mv coinjure/market/auto_pair.py coinjure/market/auto_discover.py
git mv tests/test_auto_pair.py tests/test_auto_discover.py
```

**Step 2: Update `auto_discover.py` internals**

Rename classes/functions:

- `AutoPairResult` → `DiscoveryResult`
- `auto_pair_markets()` → `discover_relations()`

Update `detect_date_nesting` to use `markets` list (still pairwise):

```python
# In each MarketRelation creation, change:
#   market_a=_enrich(m_a, platform),
#   market_b=_enrich(m_b, platform),
# To:
#   markets=[_enrich(m_a, platform), _enrich(m_b, platform)],
```

Update `detect_exclusivity` to return ONE group relation per event:

```python
def detect_exclusivity(
    markets: list[dict],
    event_title: str,
    platform: str,
    max_event_size: int = 50,
) -> list[MarketRelation]:
    """Create a single group relation for winner-take-all events."""
    if len(markets) > max_event_size:
        return []

    active = []
    for m in markets:
        bid = m.get('best_bid') or m.get('yes_bid')
        try:
            if bid and float(bid) > 0:
                active.append(m)
        except (ValueError, TypeError):
            continue
    if len(active) < 2:
        return []

    winner_count = sum(1 for m in active if _RE_WINNER.search(m.get('question', '')))
    if winner_count < len(active) * 0.8:
        return []

    # One group relation for the entire event
    market_ids = sorted(_mid(m) for m in active if _mid(m))
    relation_id = '-'.join(market_ids[:3]) + (f'-+{len(market_ids)-3}' if len(market_ids) > 3 else '')
    return [
        MarketRelation(
            relation_id=relation_id,
            markets=[_enrich(m, platform) for m in active],
            spread_type='exclusivity',
            confidence=0.99,
            reasoning=f'Mutually exclusive outcomes within "{event_title}" ({len(active)} markets)',
            hypothesis='sum(prices) <= 1',
        )
    ]
```

Update `detect_complementary` to return ONE group relation per event:

```python
def detect_complementary(
    markets: list[dict],
    event_title: str,
    platform: str,
    max_event_size: int = 50,
    sum_tolerance: float = 0.30,
) -> list[MarketRelation]:
    """Detect complementary group whose probabilities sum to ~1."""
    if len(markets) < 2 or len(markets) > max_event_size:
        return []

    priced: list[tuple[float, dict]] = []
    for m in markets:
        bid = m.get('best_bid') or m.get('yes_bid')
        ask = m.get('best_ask') or m.get('yes_ask')
        try:
            b = float(bid) if bid else 0.0
            a = float(ask) if ask else 0.0
        except (ValueError, TypeError):
            continue
        mid = (b + a) / 2 if (b or a) else 0.0
        if mid > 0:
            priced.append((mid, m))

    if len(priced) < 2:
        return []

    total = sum(p for p, _ in priced)
    if abs(total - 1.0) > sum_tolerance:
        return []

    enriched = [_enrich(m, platform) for _, m in priced]
    market_ids = sorted(_mid(m) for _, m in priced if _mid(m))
    relation_id = '-'.join(market_ids[:3]) + (f'-+{len(market_ids)-3}' if len(market_ids) > 3 else '')
    return [
        MarketRelation(
            relation_id=relation_id,
            markets=enriched,
            spread_type='complementary',
            confidence=0.95,
            reasoning=(
                f'Complementary outcomes (sum={total:.2f}) '
                f'within "{event_title}" ({len(priced)} markets)'
            ),
            hypothesis='sum(prices) = 1',
        )
    ]
```

Update `_compute_current_arb` to handle groups:

```python
def _compute_current_arb(rel: MarketRelation) -> float:
    """Compute current constraint violation from snapshot bid/ask prices.

    For groups (exclusivity/complementary): checks sum(mids) vs 1.0.
    For pairs (implication): checks mid_a > mid_b.
    """
    if rel.spread_type == 'implication':
        if len(rel.markets) < 2:
            return 0.0
        if not _has_liquidity(rel.markets[0]) or not _has_liquidity(rel.markets[1]):
            return 0.0
        mid_a = _compute_mid_price(rel.markets[0])
        mid_b = _compute_mid_price(rel.markets[1])
        if mid_a is None or mid_b is None:
            return 0.0
        return max(mid_a - mid_b, 0.0)

    if rel.spread_type in ('exclusivity', 'complementary'):
        mids = []
        for m in rel.markets:
            if not _has_liquidity(m):
                return 0.0
            mid = _compute_mid_price(m)
            if mid is None:
                return 0.0
            mids.append(mid)
        return max(sum(mids) - 1.0, 0.0)

    return 0.0
```

Update deduplication in `discover_relations` to use sorted tuple of all market IDs:

```python
# Old dedup:
pair_key = frozenset([a_id, b_id])

# New dedup:
market_ids = tuple(sorted(m.get('id', '') for m in rel.markets))
if market_ids in seen:
    continue
seen.add(market_ids)
```

Update filtering: remove `arb > 0` gate for exclusivity/complementary (keep all structural relations):

```python
candidates: list[MarketRelation] = []
for rel in deduped:
    arb = _compute_current_arb(rel)
    # Annotate each market with current_mid
    for m in rel.markets:
        m['current_mid'] = _compute_mid_price(m)
    rel.markets[0]['current_arb'] = round(arb, 4) if rel.markets else None
    # Keep all structural relations (not just those with current arb)
    candidates.append(rel)
```

Add Kalshi intra-event detection:

```python
# In discover_relations(), after Polymarket processing:
kalshi_by_event: dict[str, list[dict]] = {}
for m in kalshi_markets:
    eid = str(m.get('event_ticker', ''))
    if eid:
        kalshi_by_event.setdefault(eid, []).append(m)

for eid, mkts in kalshi_by_event.items():
    if len(mkts) < 2:
        continue
    rels = detect_date_nesting(mkts, mkts[0].get('title', ''), 'kalshi')
    by_layer['date_nesting'] = by_layer.get('date_nesting', 0) + len(rels)
    all_rels.extend(rels)

if not skip_exclusivity:
    for eid, mkts in kalshi_by_event.items():
        rels = detect_exclusivity(mkts, mkts[0].get('title', ''), 'kalshi')
        by_layer['exclusivity'] = by_layer.get('exclusivity', 0) + len(rels)
        all_rels.extend(rels)

for eid, mkts in kalshi_by_event.items():
    if len(mkts) < 2:
        continue
    rels = detect_complementary(mkts, mkts[0].get('title', ''), 'kalshi')
    by_layer['complementary'] = by_layer.get('complementary', 0) + len(rels)
    all_rels.extend(rels)
```

**Step 3: Update tests in `tests/test_auto_discover.py`**

- Update imports from `coinjure.market.auto_discover`
- Rename `AutoPairResult` → `DiscoveryResult`, `auto_pair_markets` → `discover_relations`
- Update assertions: exclusivity/complementary now return 1 group relation (not C(n,2) pairs)
- Update `_compute_current_arb` tests to create `MarketRelation` with `markets=[...]`
- Remove pairwise assertions, add group assertions

Key test changes:

```python
# Old: detect_exclusivity returns 3 pairs for 3 markets
# New: detect_exclusivity returns 1 group with 3 markets
def test_winner_take_all(self):
    markets = [...]
    rels = detect_exclusivity(markets, 'Election Winner', 'polymarket')
    assert len(rels) == 1  # one group, not C(3,2)=3 pairs
    assert len(rels[0].markets) == 3
    assert rels[0].spread_type == 'exclusivity'

# Old: detect_complementary returns C(n,2) pairs
# New: detect_complementary returns 1 group
def test_three_outcomes_sum_to_one(self):
    markets = [...]
    rels = detect_complementary(markets, 'E', 'polymarket')
    assert len(rels) == 1  # one group
    assert len(rels[0].markets) == 3

# _compute_current_arb tests use markets=[]
def test_compute_current_arb_implication(self):
    rel = MarketRelation(
        relation_id='t',
        markets=[
            {'best_bid': '0.60', 'best_ask': '0.70'},
            {'best_bid': '0.30', 'best_ask': '0.40'},
        ],
        spread_type='implication',
    )
    assert _compute_current_arb(rel) == pytest.approx(0.30)
```

**Step 4: Run tests**

Run: `poetry run python -m pytest tests/test_auto_discover.py -v -p no:nbmake`
Expected: PASS

**Step 5: Commit**

```
refactor(market): rename auto_pair → auto_discover, group relations
```

---

### Task 4: Merge strategies → `GroupArbStrategy`

**Files:**

- Create: `coinjure/strategy/builtin/group_arb_strategy.py`
- Delete: `coinjure/strategy/builtin/exclusivity_arb_strategy.py`
- Delete: `coinjure/strategy/builtin/event_sum_arb_strategy.py`
- Modify: `coinjure/strategy/builtin/__init__.py`

**Step 1: Create `GroupArbStrategy`**

Create `coinjure/strategy/builtin/group_arb_strategy.py`:

```python
"""GroupArbStrategy — unified group constraint arbitrage.

For group relations (exclusivity, complementary) where N markets in an event
should satisfy sum(prices) ≈ 1.0 (or ≤ 1.0), this strategy:

    Case A — underpriced (sum < 1.0):
        Buy YES on every outcome.
        Cost   = sum(ask_YES_i)            < 1.0
        Payout = 1.0 (exactly one wins)
        Profit = 1.0 - sum(ask_YES_i)      > 0

    Case B — overpriced (sum > 1.0):
        Buy NO on every outcome.
        Cost   = sum(1 - ask_YES_i) = N - sum(ask_YES_i)
        Payout = N - 1 (all N-1 losing YES settle NO)
        Profit = sum(ask_YES_i) - 1.0      > 0

Usage:
    coinjure engine run \\
      --exchange polymarket --mode paper \\
      --strategy-ref coinjure/strategy/builtin/group_arb_strategy.py:GroupArbStrategy \\
      --strategy-kwargs-json '{"relation_id": "m1-m2-m3"}'
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from coinjure.market.relations import RelationStore
from coinjure.trading.trader import Trader
from coinjure.trading.types import TradeSide
from coinjure.events import Event, OrderBookEvent, PriceChangeEvent
from coinjure.strategy.strategy import Strategy
from coinjure.ticker import PolyMarketTicker

logger = logging.getLogger(__name__)

_FEE_PER_SIDE = Decimal('0.005')


class GroupArbStrategy(Strategy):
    """Unified group constraint arbitrage for exclusivity/complementary relations.

    Parameters
    ----------
    relation_id:
        Relation ID from RelationStore. Must be exclusivity or complementary.
    event_id:
        Polymarket event ID (alternative to relation_id for ad-hoc usage).
    min_edge:
        Minimum net profit per share after fees to trigger (default 0.02).
    trade_size:
        Dollar amount per leg.
    cooldown_seconds:
        Minimum seconds between arb executions (default 120).
    min_markets:
        Require at least this many markets before attempting arb (default 2).
    """

    name = 'group_arb'
    version = '1.0.0'
    author = 'coinjure'

    def __init__(
        self,
        relation_id: str = '',
        event_id: str = '',
        min_edge: float = 0.02,
        trade_size: float = 10.0,
        cooldown_seconds: int = 120,
        min_markets: int = 2,
    ) -> None:
        super().__init__()
        self.relation_id = relation_id
        self.min_edge = Decimal(str(min_edge))
        self.trade_size = Decimal(str(trade_size))
        self.cooldown_seconds = cooldown_seconds
        self.min_markets = min_markets

        # Resolve event_id from relation if not provided
        self._event_id = event_id
        self._relation_market_ids: set[str] = set()
        if relation_id and not event_id:
            store = RelationStore()
            rel = store.get(relation_id)
            if rel:
                for m in rel.markets:
                    eid = m.get('event_id', '')
                    if eid:
                        self._event_id = eid
                    mid = m.get('id', '')
                    if mid:
                        self._relation_market_ids.add(mid)

        # market_id → (ticker, latest_ask_price)
        self._asks: dict[str, Decimal] = {}
        self._tickers: dict[str, PolyMarketTicker] = {}
        self._last_arb_time: float = 0.0

    def watch_tokens(self) -> list[str]:
        """Return CLOB token IDs so the data source prioritizes these markets."""
        tokens = []
        for ticker in self._tickers.values():
            tid = getattr(ticker, 'token_id', '')
            if tid:
                tokens.append(tid)
        return tokens

    def _should_track(self, ticker: PolyMarketTicker) -> bool:
        """Check if this ticker belongs to our group."""
        if self._event_id and ticker.event_id == self._event_id:
            return True
        if self._relation_market_ids and ticker.market_id in self._relation_market_ids:
            return True
        return False

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return

        ticker = getattr(event, 'ticker', None)
        if not isinstance(ticker, PolyMarketTicker):
            return
        if not self._should_track(ticker):
            return

        mid = ticker.market_id
        if not mid:
            return

        if mid not in self._tickers:
            self._tickers[mid] = ticker
            logger.debug(
                'GroupArb: registered market %s (%s)',
                mid[:16],
                ticker.name[:40] if ticker.name else '?',
            )

        if isinstance(event, OrderBookEvent):
            if event.side == 'ask' and event.price > 0:
                self._asks[mid] = event.price
            elif event.side == 'bid' and mid not in self._asks:
                self._asks[mid] = event.price
        elif isinstance(event, PriceChangeEvent):
            if mid not in self._asks:
                self._asks[mid] = event.price

        await self._check_arb(trader)

    async def _check_arb(self, trader: Trader) -> None:
        if len(self._asks) < self.min_markets:
            return

        market_ids = [mid for mid in self._asks if mid in self._tickers]
        if len(market_ids) < self.min_markets:
            return

        prices = {mid: self._asks[mid] for mid in market_ids}
        sum_yes = sum(prices.values())
        n = len(prices)

        edge_buy_yes = Decimal('1') - sum_yes - _FEE_PER_SIDE * n
        edge_buy_no = sum_yes - Decimal('1') - _FEE_PER_SIDE * n
        best_edge = max(edge_buy_yes, edge_buy_no)
        action = 'BUY_YES' if edge_buy_yes >= edge_buy_no else 'BUY_NO'

        signal = {
            'sum_yes': float(sum_yes),
            'n_markets': n,
            'edge_buy_yes': float(edge_buy_yes),
            'edge_buy_no': float(edge_buy_no),
            'best_edge': float(best_edge),
        }

        label = f'group({self.relation_id[:16] or self._event_id[:16]})'

        if best_edge < self.min_edge:
            self.record_decision(
                ticker_name=label,
                action='HOLD',
                executed=False,
                reasoning=(
                    f'sum_yes={float(sum_yes):.4f} n={n} '
                    f'best_edge={float(best_edge):.4f} < min={float(self.min_edge):.4f}'
                ),
                signal_values=signal,
            )
            return

        now = time.monotonic()
        if now - self._last_arb_time < self.cooldown_seconds:
            return
        self._last_arb_time = now

        logger.info(
            'GroupArb: %s %s sum_yes=%.4f n=%d edge=%.4f',
            action, label, float(sum_yes), n, float(best_edge),
        )

        executed_legs = 0
        failed_legs = 0

        for mid, ask_price in prices.items():
            ticker = self._tickers[mid]

            if action == 'BUY_YES':
                trade_ticker = ticker
                leg_price = ask_price
            else:
                no_ticker = trader.market_data.find_complement(ticker)
                if no_ticker is None:
                    logger.warning(
                        'GroupArb: no NO ticker for market %s, skipping leg', mid[:16],
                    )
                    continue
                trade_ticker = no_ticker
                leg_price = Decimal('1') - ask_price

            try:
                result = await trader.place_order(
                    side=TradeSide.BUY,
                    ticker=trade_ticker,
                    limit_price=leg_price,
                    quantity=self.trade_size,
                )
                if result.failure_reason:
                    logger.warning('GroupArb leg failed: %s', result.failure_reason)
                    failed_legs += 1
                else:
                    executed_legs += 1
            except Exception:
                logger.exception(
                    'GroupArb: exception placing leg for market %s', mid[:16]
                )
                failed_legs += 1

        self.record_decision(
            ticker_name=label,
            action=action,
            executed=executed_legs > 0,
            reasoning=(
                f'sum_yes={float(sum_yes):.4f} n={n} edge={float(best_edge):.4f} '
                f'legs={executed_legs}/{n} failed={failed_legs}'
            ),
            signal_values={
                **signal,
                'executed_legs': executed_legs,
                'failed_legs': failed_legs,
            },
        )
```

**Step 2: Delete old strategy files**

```bash
rm coinjure/strategy/builtin/exclusivity_arb_strategy.py
rm coinjure/strategy/builtin/event_sum_arb_strategy.py
```

**Step 3: Update `builtin/__init__.py`**

```python
from coinjure.strategy.builtin.group_arb_strategy import GroupArbStrategy

# Remove ExclusivityArbStrategy and EventSumArbStrategy imports

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
```

Update `build_strategy_ref_for_relation`:

```python
def build_strategy_ref_for_relation(
    relation: MarketRelation,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    strategy_cls = STRATEGY_BY_RELATION.get(relation.spread_type)
    if strategy_cls is None:
        return None, {}

    kwargs: dict[str, Any] = dict(extra_kwargs or {})
    spread_type = relation.spread_type

    if spread_type == 'same_event':
        # Same_event is still a pair — markets[0] and markets[1]
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
        # Also pass event_id if available
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
```

Update `__all__`:

```python
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
```

**Step 4: Run tests**

Run: `poetry run python -m pytest tests/ -p no:nbmake -x`
Expected: Failures in test_backtester (still uses old format) but strategy routing should work.

**Step 5: Commit**

```
feat(strategies): merge ExclusivityArb + EventSumArb → GroupArbStrategy
```

---

### Task 5: Update backtester

**Files:**

- Modify: `coinjure/engine/backtester.py`
- Modify: `tests/test_backtester.py`

**Step 1: Update test fixtures**

In `tests/test_backtester.py`, change `_make_relation`:

```python
def _make_relation(
    spread_type: str = 'implication',
    *,
    market_a_id: str = 'MA',
    market_b_id: str = 'MB',
    token_a: str = 'TA',
    token_b: str = 'TB',
    event_id: str = 'E1',
) -> MarketRelation:
    return MarketRelation(
        relation_id=f'{market_a_id}-{market_b_id}',
        markets=[
            {
                'id': market_a_id,
                'question': 'Market A question?',
                'event_id': event_id,
                'token_ids': [token_a],
            },
            {
                'id': market_b_id,
                'question': 'Market B question?',
                'event_id': event_id,
                'token_ids': [token_b],
            },
        ],
        spread_type=spread_type,
        confidence=0.95,
    )
```

Update `TestMakeTicker` to use `_make_ticker(rel, 0)` instead of `_make_ticker(rel, 'a')`:

```python
class TestMakeTicker:
    def test_creates_polymarket_ticker_by_default(self):
        rel = _make_relation()
        ticker = _make_ticker(rel, 0)
        assert isinstance(ticker, PolyMarketTicker)
        ...

    def test_creates_kalshi_ticker_for_kalshi_platform(self):
        rel = _make_relation()
        rel.markets[1]['platform'] = 'kalshi'
        rel.markets[1]['ticker'] = 'KXBTC-25MAR14'
        rel.markets[1]['event_ticker'] = 'KXBTC'
        ticker = _make_ticker(rel, 1)
        ...

    def test_creates_polymarket_ticker_for_leg_b(self):
        rel = _make_relation()
        ticker = _make_ticker(rel, 1)
        ...
```

Update `TestBuildSameEventKwargs`:

```python
class TestBuildSameEventKwargs:
    def test_poly_a_kalshi_b(self):
        rel = _make_relation(spread_type='same_event')
        rel.markets[0]['platform'] = 'polymarket'
        rel.markets[1]['platform'] = 'kalshi'
        rel.markets[1]['ticker'] = 'KXBTC-25MAR14'
        ...

    def test_kalshi_a_poly_b(self):
        rel = _make_relation(spread_type='same_event')
        rel.markets[0]['platform'] = 'kalshi'
        rel.markets[0]['ticker'] = 'K-MKT'
        rel.markets[1]['platform'] = 'polymarket'
        ...
```

**Step 2: Update `backtester.py`**

Change `_make_ticker(relation, leg='a')` → `_make_ticker(relation, index=0)`:

```python
def _make_ticker(relation: MarketRelation, index: int, side: str = 'yes') -> Ticker:
    """Create a PolyMarketTicker or KalshiTicker from a relation market."""
    m = relation.markets[index]
    platform = str(m.get('platform', 'polymarket')).lower()
    question = str(m.get('question', m.get('title', '')))[:40]

    if platform == 'kalshi':
        market_ticker = str(m.get('ticker', m.get('id', '')))
        return KalshiTicker(
            symbol=market_ticker,
            name=question,
            market_ticker=market_ticker,
            event_ticker=str(m.get('event_ticker', '')),
            series_ticker=str(m.get('series_ticker', '')),
            side=side,
        )

    if side == 'no':
        token_id = relation.get_no_token_id(index)
    else:
        token_id = relation.get_token_id(index)
    market_id = str(m.get('id', ''))
    return PolyMarketTicker(
        symbol=token_id,
        name=question,
        token_id=token_id,
        market_id=market_id,
        event_id=str(m.get('event_id', '')),
        side=side,
    )
```

Update `_build_same_event_kwargs`:

```python
def _build_same_event_kwargs(kwargs: dict[str, Any], relation: MarketRelation) -> None:
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
```

Update `_fetch_relation_prices`:

```python
async def _fetch_relation_prices(
    relation: MarketRelation,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch price history for first two legs (dispatches by platform)."""
    token_a = relation.get_token_id(0)
    token_b = relation.get_token_id(1)
    prices_a, prices_b = await asyncio.gather(
        _fetch_leg_prices(relation.markets[0], token_a),
        _fetch_leg_prices(relation.markets[1], token_b),
    )
    return prices_a, prices_b
```

Update `run_backtest_relation`:

```python
# Line where it builds market_ids for parquet:
market_ids = [m.get('id', '') for m in relation.markets]

# Line where complementary kwargs are set:
elif spread_type in ('complementary', 'exclusivity'):
    kwargs.setdefault('relation_id', relation.relation_id)
    for m in relation.markets:
        eid = m.get('event_id', '')
        if eid:
            kwargs.setdefault('event_id', str(eid))
            break

# Lines where tickers are built:
ticker_a = _make_ticker(relation, 0)
ticker_b = _make_ticker(relation, 1)

# NO-side tickers:
if relation.get_no_token_id(0):
    no_ticker_a = _make_ticker(relation, 0, side='no')
if relation.get_no_token_id(1):
    no_ticker_b = _make_ticker(relation, 1, side='no')
```

**Step 3: Run tests**

Run: `poetry run python -m pytest tests/test_backtester.py -v -p no:nbmake`
Expected: PASS

**Step 4: Commit**

```
refactor(backtester): use index-based market access
```

---

### Task 6: Update CLI

**Files:**

- Modify: `coinjure/cli/market_commands.py`

**Step 1: Update imports and flag names**

Change `--auto-pair/--no-auto-pair` → `--auto-discover/--no-auto-discover`:

```python
@click.option(
    '--auto-discover/--no-auto-discover',
    'auto_discover',
    default=True,
    show_default=True,
    help='Auto-detect and persist intra-event structural relations '
    '(implication, exclusivity, complementary). Use --no-auto-discover to disable.',
)
```

Update function signature: `auto_pair: bool` → `auto_discover: bool`

Update import:

```python
# Old:
from coinjure.market.auto_pair import auto_pair_markets
# New:
from coinjure.market.auto_discover import discover_relations
```

**Step 2: Update `relations_add` to use `markets`**

```python
rel = MarketRelation(
    relation_id=rid,
    markets=[info_a, info_b],
    spread_type=spread_type,
    reasoning=reasoning,
    hypothesis=hypothesis,
)
```

Update display:

```python
click.echo(f'  A: [{info_a.get("platform")}] {info_a.get("question", "")[:60]}')
click.echo(f'  B: [{info_b.get("platform")}] {info_b.get("question", "")[:60]}')
```

**Step 3: Update `relations_list` display**

```python
for r in relations:
    click.echo(
        f'  [{r.relation_id}] {r.spread_type}  conf={r.confidence:.2f}  '
        f'status={r.status}  markets={len(r.markets)}'
    )
    for i, m in enumerate(r.markets):
        click.echo(f'    [{i}] [{m.get("platform", "?")}] {m.get("question", "?")[:60]}')
    if r.hypothesis:
        click.echo(f'    Hypothesis: {r.hypothesis}')
    click.echo()
```

**Step 4: Update relation annotation section**

```python
# Build a set of market IDs already in relations
related_ids: dict[str, list[str]] = {}
for r in all_relations:
    for m in r.markets:
        for key in ('id', 'market_id', 'ticker'):
            mid = m.get(key, '')
            if mid:
                related_ids.setdefault(mid, []).append(r.relation_id)

# Update relation_summary
relation_summary = []
for r in all_relations:
    market_ids = [m.get('id', m.get('ticker', '')) for m in r.markets]
    market_questions = [m.get('question', '')[:60] for m in r.markets]
    relation_summary.append({
        'relation_id': r.relation_id,
        'type': r.spread_type,
        'status': r.status,
        'market_count': len(r.markets),
        'market_ids': market_ids,
        'market_questions': market_questions,
    })
```

**Step 5: Update auto-discover section**

```python
if auto_discover:
    from coinjure.market.auto_discover import discover_relations

    result = discover_relations(poly_markets, kalshi_markets)

    stored_new = 0
    if result.candidates:
        from coinjure.market.relations import RelationStore
        store = RelationStore()
        stored_new = store.add_batch(result.candidates)

    auto_discover_summary = {
        'total_detected': result.total_detected,
        'candidate_count': len(result.candidates),
        'stored_new': stored_new,
        'by_type': result.by_type,
        'by_layer': result.by_layer,
        'candidates': [
            {
                'relation_id': r.relation_id,
                'type': r.spread_type,
                'confidence': r.confidence,
                'reasoning': r.reasoning,
                'market_count': len(r.markets),
                'market_ids': [m.get('id', '') for m in r.markets],
                'market_questions': [m.get('question', '')[:60] for m in r.markets],
                'current_arb': r.markets[0].get('current_arb', 0) if r.markets else 0,
                'market_mids': [m.get('current_mid') for m in r.markets],
            }
            for r in result.candidates
        ],
    }
```

Update display for auto-discover results (human-readable output):

```python
if auto_discover_summary:
    s = auto_discover_summary
    td = s['total_detected']
    cc = s['candidate_count']
    sn = s.get('stored_new', 0)
    click.echo(f'  Auto-discover: {td} relations detected, {cc} candidates')
    if cc > 0:
        click.echo(f'    Stored {sn} new relation(s) ({cc - sn} already in store)')
    if s['by_type']:
        click.echo(f'    By type: {s["by_type"]}')
    click.echo()
    for r in s['candidates'][:20]:
        arb = r.get('current_arb', 0)
        arb_s = f'   arb={arb:.3f}' if arb else ''
        n = r.get('market_count', 0)
        ids = r.get('market_ids', [])
        ids_s = ', '.join(str(i) for i in ids[:3])
        if len(ids) > 3:
            ids_s += f' +{len(ids)-3} more'
        click.echo(f'    [{r["type"]}] {n} markets: {ids_s}{arb_s}')
        for q in r.get('market_questions', [])[:5]:
            click.echo(f'      - {q}')
        mids = r.get('market_mids', [])
        if mids:
            mid_sum = sum(m for m in mids if m is not None)
            click.echo(f'      sum(mids)={mid_sum:.3f}')
        click.echo(f'      {r["reasoning"]}')
        click.echo()
    if len(s['candidates']) > 20:
        click.echo(f'    ... and {len(s["candidates"]) - 20} more')
        click.echo()
```

Also update the JSON payload key from `auto_pair` to `auto_discover`:

```python
if auto_discover_summary is not None:
    payload['auto_discover'] = auto_discover_summary
```

**Step 6: Run full test suite**

Run: `poetry run python -m pytest tests/ -p no:nbmake`
Expected: PASS (all tests should pass now)

**Step 7: Commit**

```
refactor(cli): update discover command for group relations
```

---

### Task 7: Final verification and cleanup

**Step 1: Run full test suite**

Run: `poetry run python -m pytest tests/ -p no:nbmake -v`
Expected: All tests pass. Pre-existing failures (`test_strategy_create_and_validate`, `test_research_alpha_pipeline`) are unrelated.

**Step 2: Verify CLI works end-to-end**

Run: `poetry run coinjure market discover -q "election" --exchange polymarket --limit 20`
Expected: Output shows group relations instead of O(n²) pairs.

**Step 3: Verify no stale imports**

Run: `poetry run python -c "from coinjure.market.auto_discover import discover_relations, DiscoveryResult; print('OK')"`
Run: `poetry run python -c "from coinjure.strategy.builtin import GroupArbStrategy, STRATEGY_BY_RELATION; print(STRATEGY_BY_RELATION)"`

**Step 4: Commit final cleanup**

```
chore: verify group relation refactor complete
```
