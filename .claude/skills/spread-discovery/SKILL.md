---
name: spread-discovery
description: Discover related or identical market pairs with spread/arb opportunities.
---

# Spread Discovery

Use this skill when the user asks to find spread or arbitrage opportunities.

## 8 Relation Types & Their Strategies

Each relation type has a dedicated arbitrage strategy. Knowing the type determines the strategy.

| Type | Strategy | Constraint | Auto-pair? |
|------|----------|-----------|------------|
| `implication` | ImplicationArbStrategy | A <= B | Yes (date nesting) |
| `exclusivity` | ExclusivityArbStrategy | A + B <= 1 | Yes (winner-take-all) |
| `complementary` | EventSumArbStrategy | Sum ~= 1 | Yes (outcome sum) |
| `same_event` | DirectArbStrategy | A ~= B | No — agent judges |
| `correlated` | CointSpreadStrategy | Cointegrated | No — agent judges |
| `structural` | StructuralArbStrategy | p(A) = slope*p(B) + intercept | No — agent judges |
| `conditional` | ConditionalArbStrategy | Conditional probability bounds | No — agent judges |
| `temporal` | LeadLagStrategy | A leads/lags B | No — agent judges |

## What Auto-pair Can Do (rules only)

Auto-pair only detects **intra-event structural relations** — pure math, no semantic understanding needed:

1. **Date nesting** -> `implication`: Same event, deadline A < deadline B -> P(A) <= P(B)
2. **Winner-take-all** -> `exclusivity`: Same event, <=20 markets, 80%+ "will X win" pattern
3. **Outcome sum** -> `complementary`: Same event, mid-prices sum ~= 1.0

Auto-pair also filters by **snapshot arb**: only candidates with current pricing violations (arb > 0) are returned. This reduces noise (e.g., 78 candidates -> 3 with actual mispricing).

## What the Agent Must Do (semantic judgment)

Everything else requires the agent to search, read context, and decide:

### same_event (cross-platform)
Search the same keywords on both exchanges, compare resolution rules.

```bash
coinjure market discover -q "Trump resign" --exchange both --with-rules --limit 20
# Compare resolution rules between platforms to confirm same event
```

### correlated (cross-event)
Find markets on related topics with shared fundamental drivers.

```bash
coinjure market discover -q "ceasefire" --exchange polymarket --limit 20
coinjure market discover -q "Ukraine election" --exchange polymarket --limit 20
# Cross-reference: do any pairs have shared fundamental drivers?
```

### structural / conditional / temporal
These require deeper analysis — look for markets with known mathematical relationships, conditional dependencies, or price lead-lag patterns.

## Full Workflow

### Step 1: Discover candidates

```bash
coinjure market discover -q "keyword1" -q "keyword2" --exchange both --limit 40
```

Auto-pair candidates (implication/exclusivity/complementary with current arb > 0) are shown automatically. For other relation types, the agent must identify candidates from the market list.

### Step 2: Determine relation type

Before adding, decide the type:
- Auto-pair found implication/exclusivity/complementary? -> verify pricing
- Same question on two platforms? -> `same_event`
- Related topics with shared drivers? -> `correlated`
- Known mathematical relationship? -> `structural`
- Conditional dependency? -> `conditional`
- Price lead-lag? -> `temporal`

### Step 3: Get market info if needed

```bash
coinjure market info --market-id <id> --json
```

### Step 4: Add pairs with actual opportunities

```bash
coinjure market relations add --market-id-a <a> --market-id-b <b> --spread-type <type>
```

### Step 5: Review all relations

```bash
coinjure market relations list
```

## Validation Criteria by Type

| Type | Method | Valid when |
|------|--------|-----------|
| implication | structural constraint | `violation_count == 0` |
| same_event | structural constraint | `violation_count == 0` |
| complementary | structural constraint (A+B<=1) | `violation_count == 0` |
| exclusivity | structural constraint (A+B<=1) | `violation_count == 0` |
| correlated | cointegration + ADF | `is_cointegrated == true` |
| structural | residual analysis | spread stationary around model |
| conditional | conditional bounds | bounds hold with low violation rate |
| temporal | cross-correlation | `lead_lag_significant == true` |

Note: structural pairs with low violation rates (< 3%) are still interesting — the violations themselves are the arb opportunities.

## Hard Rules

- This phase is discovery only — no strategy implementation or trading.
- Default table output is readable by both humans and agents. Use `--json` only when programmatic parsing is needed.
- Determine the relation type BEFORE running analysis — using the wrong type gives meaningless results.
- For implication pairs, always put the narrower/earlier market as A and broader/later as B.
- For same_event discovery, always use `--with-rules` to include resolution criteria for cross-platform comparison.
- Only add relations that have actual or potential trading opportunities (current_arb > 0, high violation_rate, or cointegrated with significant zscore).
