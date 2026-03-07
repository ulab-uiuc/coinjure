---
name: spread-discovery
description: Discover related or identical market pairs with spread/arb opportunities.
---

# Spread Discovery

Use this skill when the user asks to find spread or arbitrage opportunities.

## Core Workflow

1. Multi-keyword search + structural pair detection

```bash
coinjure market discover -q "election" -q "Trump" --exchange both --limit 50 --json
coinjure market discover -q "crypto" --exchange polymarket --limit 30 --json
```

Supports `--exchange polymarket|kalshi|both`. Kalshi searches via events API (bypasses SDK bug).
Discovered pairs are persisted to `~/.coinjure/relations.json`.
Detects three structural types: temporal/implication (same event, different deadlines), complementary (same event, outcomes sum to 1), same_event (cross-platform fuzzy match).

2. Quantitative analysis of a single market or pair

```bash
# Single market stats
coinjure market analyze --exchange polymarket --market-id <id> --json

# Pair analysis (correlation, cointegration, hedge ratio, half-life)
coinjure market analyze --exchange polymarket --market-id <id_a> --compare <id_b> --json
```

3. Manage discovered relations

```bash
coinjure market relations list --type same_event --json
coinjure market relations show <relation_id> --json
coinjure market relations strongest -n 10 --json
coinjure market relations find <market_id> --json
coinjure market relations validate <relation_id> --history-a a.jsonl --history-b b.jsonl --json
coinjure market relations remove <relation_id>
```

## Output

- Each pair includes: market_a, market_b, spread_type, confidence, hypothesis, expected_spread, entry/exit_threshold
- All results are persisted to `~/.coinjure/relations.json` for downstream backtesting and deployment

## Hard Rules

- This phase is discovery only — no strategy implementation or trading.
- Always use `--json` output.
