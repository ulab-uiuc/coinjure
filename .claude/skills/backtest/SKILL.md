---
name: backtest
description: Backtest strategies against historical data.
---

# Backtest

Use this skill when the user asks to backtest a strategy.

## Core Command

```bash
# Default: fetches price history from the CLOB API (no --parquet needed)
coinjure engine backtest \
  --relation <relation_id> \
  --json

# Optional: use parquet orderbook files instead of API
coinjure engine backtest \
  --relation <relation_id> \
  --parquet <orderbook.parquet> \
  --json
```

Use `--all-relations` to backtest all active relations.

## Data Sources

- **API history (default)**: Fetches candlestick price history from Polymarket CLOB / Kalshi REST API. No extra flags needed — just omit `--parquet`.
- **Parquet replay (optional)**: Use `--parquet <file>` to replay recorded orderbook snapshots instead.

Always prefer API history unless the user explicitly asks for parquet replay.

## Hard Rules

- Backtest must pass before entering paper trade.
- Focus on key metrics: sharpe, max_drawdown, win_rate.
- Always use `--json` output.
- For batch/grid-search/pipeline workflows, the agent orchestrates multiple backtest calls.
