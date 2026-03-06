---
name: pm-data-discovery
description: Use this skill to discover available data, search for target markets, and obtain data samples. Data discovery and retrieval only — no strategy assumptions.
---

# PM Data Discovery

Use this skill when the user wants to explore data, find markets, or discover arbitrage opportunities.

## Goal

Answer 3 questions:

- What markets and arbitrage opportunities are available
- How to search for target events / markets
- How to pass discovery results to subsequent strategy deployment steps

## Data Entry Points

### 1. Market Search (online, no authentication required)

```bash
# List open Polymarket markets
coinjure market list --exchange polymarket --limit 50 --json

# Search by keyword
coinjure market search --exchange polymarket --query "<keyword>" --limit 50 --json
coinjure market search --exchange kalshi --query "<keyword>" --limit 50 --json

# View single market details (includes bid/ask)
coinjure market info --market-id <market_id> --json

# Cross-platform fuzzy matching (find same event on Poly + Kalshi)
coinjure market match --query "<keyword>" --min-similarity 0.6 --json
```

### 2. Arbitrage Opportunity Scanning (online)

```bash
# Cross-platform arbitrage: Polymarket vs Kalshi same-event price spread
coinjure market scan --query "<keyword>" --min-edge 0.02 --json

# Single-platform multi-outcome arbitrage: sum(YES) != 1.0 within same event
coinjure market scan-events --query "<keyword>" --min-edge 0.01 --json
# Output includes: event_id, event_title, sum_yes, best_edge, action, markets[]
```

### 3. News and External Events (online)

```bash
# Google News / RSS scraping
coinjure market news --source google --query "<keyword>" --limit 30 --json
coinjure market news --source rss --query "<keyword>" --limit 30 --json
```

### 4. Backtest Data (local parquet)

```bash
# List available parquet files
ls data/*.parquet

# Use in backtesting
coinjure strategy backtest --parquet data/<file>.parquet --strategy-ref <ref> --json
```

## Recommended Workflow

1. `market search` to find candidate markets.
2. `market news` to gather related news events and inform market direction.
3. `market scan` / `market scan-events` to discover current arbitrage opportunities.
4. `market match` to confirm cross-platform pairing quality (similarity score).
5. `market info` to confirm liquidity (bid/ask data available).
6. Pass `event_id` / `poly_id` / `kalshi_ticker` to the deployment step.

## Output Requirements

- Provide a clear market list: event_id, market_id, current edge.
- Attach a reproducible command to every conclusion.
- Prefer `--json` output for downstream agent consumption.

## Hard Rules

- Do not propose strategy logic assumptions in this skill.
- Only perform data discovery, filtering, and sampling.
- If edge < 0, explicitly state the opportunity does not meet criteria — do not force deployment.
