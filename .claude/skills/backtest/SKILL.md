---
name: backtest
description: Backtest strategies against historical data.
---

# Backtest

Use this skill when the user asks to backtest a strategy.

## Core Commands

1. Validate strategy loads correctly

```bash
coinjure strategy validate \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' --json
```

2. Run backtest

```bash
coinjure strategy backtest \
  --parquet <orderbook.parquet> \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' --json
```

## Hard Rules

- Backtest must pass before entering paper trade.
- Focus on key metrics: sharpe, max_drawdown, win_rate.
- Always use `--json` output.
- For batch/grid-search/pipeline workflows, the agent orchestrates multiple `backtest` calls.
