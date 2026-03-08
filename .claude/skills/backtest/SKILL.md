---
name: backtest
description: Backtest strategies against historical data.
---

# Backtest

Use this skill when the user asks to backtest a strategy.

## Core Command

```bash
coinjure engine backtest \
  --relation <relation_id> \
  --parquet <orderbook.parquet> \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' --json
```

Use `--all-relations` to backtest all active relations.

## Hard Rules

- Backtest must pass before entering paper trade.
- Focus on key metrics: sharpe, max_drawdown, win_rate.
- Always use `--json` output.
- For batch/grid-search/pipeline workflows, the agent orchestrates multiple backtest calls.
