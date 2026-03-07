---
name: spread-discovery
description: Discover related or identical market pairs with spread/arb opportunities.
---

# Spread Discovery

Use this skill when the user asks to find spread or arbitrage opportunities.

## Relation Types & How to Find Them

Different relation types require fundamentally different search strategies and analysis methods. When discovering pairs, think about **which type** each candidate pair is, then use the correct analysis.

### 1. Implication (structural)

**What**: A implies B — if A is true, B must also be true. Pricing constraint: `p(A) <= p(B)`.

**How to find**: Look for same-topic markets with **nested timeframes** or **prerequisite relationships** within the same event series.

- "X by March" vs "X by June" — earlier deadline implies later deadline
- "election called" vs "election held" — held implies called
- "in 2025" vs "before 2027" — subset implies superset

**Search strategy**: Discover with a topic keyword, then scan results for the same `event_id` or similar question text with different dates.

```bash
coinjure market discover -q "Ukraine election" --exchange polymarket --limit 30 --json (optional)
# Look for: "called by March" vs "called by June" vs "held by June" vs "held by Dec"
```

**Analysis**: Use `--relation-type implication`. Validates `A <= B` (auto-detects direction).

```bash
coinjure market analyze --market-id <earlier> --compare <later> --relation-type implication --json (optional)
```

**Key metrics**: `constraint_holds` (boolean), `violation_count`, `violation_rate`. Valid if 0 violations.

### 2. Same Event (structural)

**What**: Identical market on two different platforms. Pricing constraint: `p_A ~= p_B`.

**How to find**: Search the **same keywords on both exchanges** and look for matching questions.

```bash
coinjure market discover -q "Trump resign" --exchange both --with-rules --limit 20
# Compare resolution rules between platforms to confirm same event
```

Use `--with-rules` to include resolution criteria in output. Even if titles differ, the resolution rules may be substantially the same. Compare the rules text to decide if two markets are truly the same event.

**Analysis**: Use `--relation-type same_event`. Validates `A <= B` (prices should be near-equal).

**Reality**: Polymarket (geopolitics, pop culture) and Kalshi (economic data) have very little overlap. Cross-platform same_event pairs are rare — use `--with-rules` to find near-matches.

### 3. Complementary / Exclusivity (structural)

**What**: Mutually exclusive outcomes within the same event. Constraint: `p(A) + p(B) <= 1`.

**How to find**: Look for markets within the **same event** that represent different outcomes (e.g., different winners of the same election).

```bash
coinjure market discover -q "2028 presidential" --exchange polymarket --limit 40 --json (optional)
# Look for: multiple candidates in the same event_id where sum of prices should <= 1
```

**Analysis**: Use `--relation-type complementary` or `--relation-type exclusivity`.

```bash
coinjure market analyze --market-id <outcome_a> --compare <outcome_b> --relation-type complementary --json (optional)
```

**Key metrics**: `constraint_holds`, `violation_count`. Violations = arb opportunities (sum > 1).

### 4. Temporal / Semantic / Conditional (cointegration)

**What**: Markets that move together due to shared fundamental drivers, but have NO structural pricing constraint.

**How to find**: Search for markets on **related but distinct topics** that share causal drivers.

- "ceasefire" vs "election called" — peace process might enable elections
- "inflation" vs "Fed rate hike" — macro causation
- "tariffs" vs "recession" — economic chain reaction

```bash
coinjure market discover -q "ceasefire" --exchange polymarket --limit 20 --json (optional)
coinjure market discover -q "Ukraine election" --exchange polymarket --limit 20 --json (optional)
# Cross-reference: do any pairs have shared fundamental drivers?
```

**Analysis**: Use `--relation-type temporal` (or `semantic` / `conditional`). Runs cointegration + ADF + half-life.

```bash
coinjure market analyze --market-id <id_a> --compare <id_b> --relation-type temporal --json (optional)
```

**Key metrics**: `is_cointegrated` (Engle-Granger p < 0.05), `is_stationary` (ADF), `half_life` (mean-reversion speed), `hedge_ratio` (OLS beta). Valid only if cointegrated AND spread is stationary.

**Reality**: Most semantic pairs fail cointegration on prediction markets because mid-prices rarely move (thick $0.01 tick walls). These are the hardest to find but most valuable if validated.

### 5. Lead-Lag (supplementary)

**What**: Market A's price changes predict Market B's price changes with a time delay.

**Not a standalone type** — lead-lag is checked automatically for ALL relation types. Look for `lead_lag_significant: true` in the output of any analysis.

**Key metrics**: `lead_lag` (positive = A leads B by N steps), `lead_lag_corr` (significant if |corr| > 0.3).

## Full Workflow

### Step 1: Discover candidates across categories

```bash
coinjure market discover -q "keyword1" -q "keyword2" --exchange both --limit 40 --json (optional)
```

Search multiple topic areas: elections, crypto, geopolitics, macro, sports, etc.

### Step 2: Identify candidate pairs and determine relation type FIRST

Before analyzing, decide the type:
- Same event series with nested deadlines? -> `implication`
- Same question on two platforms? -> `same_event`
- Different outcomes in same event? -> `complementary`
- Related topics with shared drivers? -> `temporal` or `semantic`

### Step 3: Analyze with the correct relation type

```bash
coinjure market analyze --market-id <a> --compare <b> --relation-type <type> --json (optional)
```

### Step 4: Add validated pairs as relations

```bash
coinjure market relations add --market-id-a <a> --market-id-b <b> --spread-type <type> --json (optional)
```

### Step 5: Run quantitative validation on stored relations

```bash
coinjure market relations validate <relation_id> --json (optional)
```

### Step 6: Review all relations

```bash
coinjure market relations list --json (optional)
coinjure market relations list --status validated --json (optional)
```

## Validation Criteria by Type

| Type | Method | Valid when |
|------|--------|-----------|
| implication | structural constraint | `violation_count == 0` |
| same_event | structural constraint | `violation_count == 0` |
| complementary | structural constraint (A+B<=1) | `violation_count == 0` |
| exclusivity | structural constraint (A+B<=1) | `violation_count == 0` |
| temporal | cointegration + ADF | `is_cointegrated == true` |
| semantic | cointegration + ADF | `is_cointegrated == true` |
| conditional | cointegration + ADF | `is_cointegrated == true` |

Note: structural pairs with low violation rates (< 3%) are still interesting — the violations themselves are the arb opportunities.

## Hard Rules

- This phase is discovery only — no strategy implementation or trading.
- Default table output is readable by both humans and agents. Use `--json` only when programmatic parsing is needed.
- Determine the relation type BEFORE running analysis — using the wrong type gives meaningless results.
- For implication pairs, always put the narrower/earlier market as A and broader/later as B.
- For same_event discovery, always use `--with-rules` to include resolution criteria for cross-platform comparison.
- Only add relations that have actual or potential trading opportunities (current_arb > 0, high violation_rate, or cointegrated with significant zscore).
