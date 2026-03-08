---
name: paper-trade
description: Run paper trading after backtest passes, with monitoring and intervention.
---

# Paper Trade

Use this skill when the user asks to run paper trading.

## Prerequisites

- At least one backtest result is acceptable

## Start

```bash
coinjure engine paper-run \
  --exchange <polymarket|kalshi|rss> \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --duration <seconds> --json
```

Add `--monitor` to open the TUI monitor.

## Runtime Controls

```bash
coinjure engine status --id <strategy_id> --json
coinjure engine status --id <strategy_id> --full --json
coinjure engine pause  --id <strategy_id> --json
coinjure engine resume --id <strategy_id> --json
coinjure engine swap   --id <strategy_id> --strategy-ref <new_ref> --json
coinjure engine stop   --id <strategy_id> --json
```

## Hard Rules

- On anomaly, `pause` first; then `resume` or `stop` after assessment.
- Paper phase must not use live credentials.
