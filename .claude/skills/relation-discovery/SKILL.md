---
name: relation-discovery
description: Discover market relations (8 types) that map 1:1 to builtin arbitrage strategies.
---

# Relation Discovery

Use this skill when the user asks to find spread, arbitrage, or related market opportunities.

The core idea: every tradable opportunity is a **relation** between two markets. There are 8 relation types, each with a dedicated builtin strategy. Discovering relations = finding trades.

## 8 Relation Types → 8 Strategies (1:1 mapping)

### Auto-pair relations (rule-based, intra-event)

These are detected automatically by `market discover --auto-pair`. No semantic judgment needed.

**1. `implication` → ImplicationArbStrategy**
- **Constraint**: P(A) <= P(B) — A implies B (e.g., "Trump wins nomination" implies "Trump wins election")
- **Detection**: Same event, deadline A < deadline B (date nesting)
- **Trading**: When price_A > price_B (violation), sell A + buy B. Exit when constraint restored.
- **Example**: If P(nomination) = 0.65 but P(election) = 0.60, sell nomination NO, buy election YES.

**2. `exclusivity` → ExclusivityArbStrategy**
- **Constraint**: P(A) + P(B) <= 1 — A and B are mutually exclusive (can't both happen)
- **Detection**: Same event, <=20 markets, 80%+ "will X win" pattern (winner-take-all)
- **Trading**: When price_A + price_B > 1.0 (violation), sell both A and B (buy both NOs). Exit when sum <= 1.
- **Example**: Two candidates in same race sum to 1.05 → buy both NOs, guaranteed 0.05 profit.

**3. `complementary` → EventSumArbStrategy**
- **Constraint**: Sum of all outcome prices ~= 1.0 — exactly one outcome wins
- **Detection**: Same event, mid-prices sum deviates from 1.0
- **Trading**: If sum < 1.0, buy all YES sides (underpriced). If sum > 1.0, buy all NO sides (overpriced).
- **Example**: 4 candidates sum to 0.93 → buy all 4 YES sides, guaranteed 0.07 profit on settlement.

### Agent-judged relations (semantic, cross-event or cross-platform)

These require the agent to search, read context, compare resolution rules, and decide.

**4. `same_event` → DirectArbStrategy**
- **Constraint**: P_poly(A) ~= P_kalshi(A) — same question on two platforms should have same price
- **How to find**: Search same keywords on both exchanges, compare resolution rules with `--with-rules`
- **Trading**: When prices diverge beyond min_edge, buy cheap side + sell expensive side simultaneously.
- **Example**: "Will X happen?" at 0.55 on Polymarket vs 0.62 on Kalshi → buy Poly, sell Kalshi.

**5. `correlated` → CointSpreadStrategy**
- **Constraint**: Spread is stationary (cointegrated) — related markets with shared fundamental drivers
- **How to find**: Cross-reference markets on related topics (e.g., "ceasefire" + "peace deal")
- **Trading**: Mean-reversion on the spread. Self-calibrates during warmup to compute spread mean/std. Enter when spread > entry_mult * std from mean, exit on reversion.
- **Example**: Ceasefire market and peace deal market are cointegrated; spread spikes → trade toward mean.

**6. `structural` → StructuralArbStrategy**
- **Constraint**: p(A) = slope * p(B) + intercept — known mathematical relationship
- **How to find**: Markets with different payout structures on same underlying
- **Trading**: Monitor residual (actual price - expected price), trade when residual > min_edge toward equilibrium.

**7. `conditional` → ConditionalArbStrategy**
- **Constraint**: p(A|B) bounded by [lower, upper] — conditional probability bounds
- **How to find**: Markets where one outcome logically constrains another
- **Trading**: When prices violate conditional bounds, sell overpriced leg + buy underpriced leg.

**8. `temporal` → LeadLagStrategy**
- **Constraint**: A leads B by N steps — one market moves first, the other follows
- **How to find**: Markets with known information flow direction (e.g., primary → general election)
- **Trading**: When leader A makes a significant move, trade follower B in the same direction. Exit on catch-up or timeout.

## Auto-pair Filtering

Auto-pair filters by **snapshot arb**: only candidates with current pricing violations (arb > 0) are persisted. This reduces noise (e.g., 78 structural pairs → 3 with actual mispricing).

## Full Workflow

### Step 1: Discover candidates

```bash
coinjure market discover -q "keyword1" -q "keyword2" --exchange both --limit 40
```

Auto-pair candidates (implication/exclusivity/complementary with current arb > 0) are shown and persisted automatically. For agent-judged types, the agent identifies candidates from the market list.

### Step 2: Determine relation type

- Auto-pair found implication/exclusivity/complementary? → verify pricing
- Same question on two platforms? → `same_event`
- Related topics with shared drivers? → `correlated`
- Known mathematical relationship? → `structural`
- Conditional dependency? → `conditional`
- Price lead-lag? → `temporal`

### Step 3: Get market info if needed

```bash
coinjure market info --market-id <id> --json
```

### Step 4: Add pairs with actual opportunities

```bash
coinjure market relations add \
  --market-id-a <a> --market-id-b <b> \
  --spread-type <type> \
  --hypothesis "price relationship" \
  --reasoning "why these are related"
```

### Step 5: Review all relations

```bash
coinjure market relations list --json
```

## Strategy Code Reference

All builtin strategies: `coinjure/strategy/builtin/`
Mapping dict: `coinjure.strategy.builtin.STRATEGY_BY_RELATION` (relation type string → strategy class)

## Validation Criteria by Type

| Type | Method | Valid when |
|------|--------|-----------|
| implication | structural constraint | price_A <= price_B holds |
| exclusivity | structural constraint | price_A + price_B <= 1 holds |
| complementary | structural constraint | sum of prices ~= 1.0 |
| same_event | cross-platform comparison | prices converge (< min_edge) |
| correlated | cointegration + ADF | `is_cointegrated == true` |
| structural | residual analysis | spread stationary around model |
| conditional | conditional bounds | bounds hold with low violation rate |
| temporal | cross-correlation | `lead_lag_significant == true` |

Note: pairs with low violation rates (< 3%) are still interesting — the violations themselves are the arb opportunities.

## Hard Rules

- This phase is discovery only — no strategy implementation or trading.
- Determine the relation type BEFORE adding — the type determines which strategy runs.
- For implication pairs, always put the narrower/earlier market as A and broader/later as B.
- For same_event discovery, always use `--with-rules` to include resolution criteria for cross-platform comparison.
- Only add relations that have actual or potential trading opportunities.
