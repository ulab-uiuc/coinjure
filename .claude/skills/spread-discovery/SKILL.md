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

2. Quantitative analysis of a single market or pair

```bash
# Single market stats
coinjure market analyze --exchange polymarket --market-id <id> --json

# Pair analysis (correlation, cointegration, hedge ratio, half-life, lead-lag)
coinjure market analyze --exchange polymarket --market-id <id_a> --compare <id_b> --json
```

3. Manage relations (agent adds after analysis)

```bash
coinjure market relations list --type same_event --json
coinjure market relations add --market-id-a <id_a> --market-id-b <id_b> --spread-type implication --json
coinjure market relations remove <relation_id>
```

## Output

- `market discover` returns raw market data for agent analysis (no auto-persist)
- `market analyze --compare` returns quantitative stats (correlation, cointegration, hedge ratio, lead-lag, half-life)
- Agent decides relation type and adds via `market relations add`; relations are persisted to `~/.coinjure/relations.json`

## Hard Rules

- This phase is discovery only — no strategy implementation or trading.
- Always use `--json` output.
