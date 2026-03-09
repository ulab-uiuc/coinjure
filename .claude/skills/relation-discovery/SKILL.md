---
name: relation-discovery
description: Discover market relations (8 types) that map 1:1 to builtin arbitrage strategies.
---

# Relation Discovery

Use this skill when the user asks to find spread, arbitrage, or related market opportunities.

The core idea: every tradable opportunity is a **relation** between markets. There are 8 relation types mapping to 7 strategy classes. Discovering relations = finding trades.

## 8 Relation Types → 7 Strategies

### Auto-discover relations (rule-based, intra-event)

Detected automatically by `market discover --auto-discover`. No semantic judgment needed.

**1. `implication` → ImplicationArbStrategy**

- **Constraint**: P(A) <= P(B) — A implies B (e.g., "ceasefire by March" implies "ceasefire by June")
- **Detection**: Same event, deadline A < deadline B (date nesting)
- **Caveat**: Only checks date ordering, not semantic consistency. Same event may contain different questions with different dates (e.g., "called by June" vs "held by June") — these get mis-detected. Validation happens at backtest, not discovery.
- **Trading**: When price_A > price_B (violation), sell A + buy B. Exit when constraint restored.

**2. `exclusivity` → GroupArbStrategy**

- **Constraint**: P(A) + P(B) + ... <= 1 — mutually exclusive outcomes (can't have multiple winners)
- **Detection**: Same event, <=50 markets, 80%+ match "will X win" pattern (winner-take-all)
- **Trading**: When sum(prices) > 1.0 (violation), buy NO on overpriced outcomes. Exit when sum <= 1.

**3. `complementary` → GroupArbStrategy** (same strategy as exclusivity)

- **Constraint**: Sum of all outcome prices = 1.0 — exactly one outcome wins
- **Detection**: Same event, mid-prices sum within tolerance (0.30) of 1.0
- **Trading**: If sum < 1.0, buy all YES (underpriced). If sum > 1.0, buy all NO (overpriced).

> **Note:** `exclusivity` and `complementary` share the same `GroupArbStrategy`. The distinction is semantic (sum <= 1 vs sum = 1) but trading logic is identical.

### Agent-judged relations (semantic, cross-event or cross-platform)

These require the agent to search, read context, compare resolution rules, and decide. After running `market discover`, the agent MUST review the output and identify candidates for these types.

**4. `same_event` → DirectArbStrategy**

- **Constraint**: P_poly(A) ~= P_kalshi(A) — same question on two platforms
- **How to find**: Search same keywords on both exchanges, compare resolution rules
- **Trading**: When prices diverge beyond min_edge, buy cheap + sell expensive simultaneously.

**5. `correlated` → CointSpreadStrategy**

- **Constraint**: Spread is stationary (cointegrated) — related markets with shared drivers
- **How to find**: Cross-reference markets on related topics (e.g., "ceasefire" + "election")
- **Trading**: Mean-reversion on the spread. Enter when spread > entry_mult \* std from mean.

**6. `structural` → StructuralArbStrategy**

- **Constraint**: p(A) = slope \* p(B) + intercept — known mathematical relationship
- **How to find**: Markets with nested thresholds on same underlying (e.g., BTC reach $120K implies reach $100K — price nesting that auto-discover misses because it only detects date nesting)
- **Trading**: Monitor residual, trade when residual > min_edge toward equilibrium.

**7. `conditional` → ConditionalArbStrategy**

- **Constraint**: p(A|B) bounded by [lower, upper] — conditional probability bounds
- **How to find**: Markets where one outcome logically constrains another (e.g., P(election) <= P(ceasefire) when ceasefire is precondition for election)
- **Trading**: When prices violate conditional bounds, sell overpriced + buy underpriced.

**8. `temporal` → LeadLagStrategy**

- **Constraint**: A leads B by N steps — one market moves first, the other follows
- **How to find**: Markets with known information flow direction (e.g., primary → general election)
- **Trading**: When leader A makes a significant move, trade follower B in the same direction.

## Full Workflow

### Step 1: News scan — identify current hot topics

**Start here.** Before searching for markets, understand what's happening in the world. Hot topics = active markets = trading opportunities.

```bash
coinjure market news --limit 15
```

Read the headlines and extract keywords for the current news cycle. Group them by theme:

- What geopolitical conflicts are active? (wars, sanctions, diplomacy)
- What economic events are upcoming? (Fed meetings, earnings, tariffs)
- What political events are in play? (elections, nominations, legislation)
- What's trending in tech, sports, culture?

These keywords drive Step 2.

### Step 2: Iterative search — find market groups through repeated queries

**This is the most important step.** Use the keywords from Step 1 to search for markets. Finding relations means finding **groups of related markets**. This requires iterative, exploratory searching — you won't find all the pieces in one query.

**The core loop:**

1. Pick a hot topic from the news scan → search with its keywords
2. Look at the results — do any markets look like they could form a group?
3. Search again with refined/related keywords to find more related markets
4. Repeat until you've assembled candidate groups

**Example workflow for discovering a group:**

```
News headlines: "Iran warship sunk", "US gasoline up 17%", "Schumer oil reserves"
  → Keywords: "Iran", "oil", "gasoline", "conflict"
Search "Iran ceasefire" → find 4 ceasefire markets with different dates
  → implication chain (auto-discovered)
Search "Iran" on Kalshi → find matching Kalshi Iran markets
  → Cross-reference → same_event candidates
Search "oil price" → find oil/gasoline markets
  → Cross-reference with Iran conflict markets → correlated candidates
```

**After exhausting news-driven topics, sweep remaining categories:**

1. **Geopolitics**: "Ukraine", "Russia", "China", "Taiwan", "NATO", "ceasefire", "war", "sanctions"
2. **US Politics**: "Trump", "election", "president", "congress", "senate", "governor"
3. **Economics/Finance**: "Bitcoin", "crypto", "Fed", "interest rate", "recession", "inflation"
4. **Technology**: "AI", "GPT", "Apple", "Tesla", "SpaceX"
5. **Sports**: "World Cup", "Super Bowl", "NBA", "NFL", "Olympics"
6. **Culture/Entertainment**: "Oscar", "Grammy", "GTA"
7. **Science/Health**: "FDA", "vaccine", "climate", "NASA"

Use BOTH keyword queries (-q) AND tag filters (-t):

```bash
# Keyword search — driven by news topics
coinjure market discover -q "keyword1" -q "keyword2" --exchange polymarket --limit 40
# Tag search — broad category sweep
coinjure market discover -t "Politics" --exchange polymarket --limit 100
# Cross-platform (for same_event discovery)
coinjure market discover -q "keyword1" --exchange kalshi --limit 40
```

**Keep going until you have explored at least 10+ topic areas.** Prioritize news-driven topics first — they have the most active markets and price movement. Auto-discover detects intra-event structural relations (implication, exclusivity, complementary) automatically from the results.

### Step 2: Agent judges cross-event / cross-platform relations

After each `discover` call, **review the market output carefully** and cross-reference results across queries. The agent must actively connect the dots:

- **Same question on two platforms?** → `same_event` — requires searching the same keywords on both Polymarket and Kalshi, then comparing resolution rules
- **Related topics with shared drivers?** → `correlated` — requires searching across related topics (e.g., "ceasefire" results vs "election" results) and judging causal links
- **Nested thresholds on same underlying?** → `structural` — requires finding markets with numerical thresholds on the same metric (e.g., BTC $100K and BTC $120K across different events)
- **One outcome constrains another?** → `conditional` — requires reasoning about logical preconditions across events
- **Known information flow direction?** → `temporal` — requires identifying leader/follower dynamics across events

**Key principle:** Auto-discover only finds relations within a single event. All cross-event and cross-platform relations require the agent to **search multiple times, compare results, and make semantic judgments**.

### Step 3: Get market info if needed

```bash
coinjure market info --market-id <id> --json
```

### Step 4: Add relation groups

```bash
coinjure market relations add \
  -m <id1> -m <id2> -m <id3> \
  --spread-type <type> \
  --hypothesis "price relationship" \
  --reasoning "why these are related"
```

Exchange is auto-detected per market ID (numeric → polymarket, else → kalshi). Override with `--exchange polymarket|kalshi`.

### Step 5: Review all relations

```bash
coinjure market relations list --json
```

### Step 6: Backtest all relations

Only after accumulating a substantial pool of relations (20+):

```bash
coinjure engine backtest --all-relations --json
```

This uses API price history. Focus on which relations show positive PnL and trades.

## Strategy Code Reference

All builtin strategies: `coinjure/strategy/builtin/`
Mapping dict: `coinjure.strategy.builtin.STRATEGY_BY_RELATION` (relation type → strategy class)

## Validation Criteria by Type

| Type          | Method                    | Valid when                          |
| ------------- | ------------------------- | ----------------------------------- |
| implication   | structural constraint     | price_A <= price_B holds            |
| exclusivity   | structural constraint     | sum(prices) <= 1 holds              |
| complementary | structural constraint     | sum(prices) ~= 1.0                  |
| same_event    | cross-platform comparison | prices converge (< min_edge)        |
| correlated    | cointegration + ADF       | `is_cointegrated == true`           |
| structural    | residual analysis         | spread stationary around model      |
| conditional   | conditional bounds        | bounds hold with low violation rate |
| temporal      | cross-correlation         | `lead_lag_significant == true`      |

Note: constraint violations are the arb opportunities — low violation rates are fine.

## Hard Rules

- This phase is discovery only — no strategy implementation or trading.
- Determine the relation type BEFORE adding — the type determines which strategy runs.
- For implication, always put the narrower/earlier market first.
- For same_event, always search both exchanges and compare resolution rules.
- **Do not rely solely on auto-discover.** After each `discover` call, the agent MUST review output and cross-reference with previous search results to identify agent-judged relation candidates.
- **Search iteratively.** One query is never enough. Refine keywords, try related topics, search the other exchange. The best relations come from connecting markets found in different searches.
- A relation is a group of 2+ markets. Use `-m` to specify each market in the group.
