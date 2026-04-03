---
name: paper-trade
description: Run paper trading after backtest passes, with monitoring and intervention.
---

# Paper Trade

Use this skill when the user asks to run paper trading.

## Prerequisites

- At least one backtest result is acceptable

## Step 1: Start the Market Data Hub

The hub **must** be running before launching paper-trade engines. Without it, engines either fail to get order book data (stuck retrying hub connection) or each engine polls the API independently (wasteful and rate-limited).

```bash
# Check if hub is already running
coinjure hub status --json

# If not running, start it (detached)
coinjure hub start --detach --json
```

Wait for hub to be ready before proceeding.

## Step 2: Launch paper-trade engines

```bash
coinjure engine paper-run \
  --exchange <polymarket|kalshi|rss> \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --duration <seconds> --json
```

Add `--monitor` to open the TUI monitor.

To batch-deploy all backtest-passed relations:

```bash
coinjure engine paper-run --all-relations --detach --json
```

Use `--no-hub` **only** for quick single-engine debugging when hub is unavailable.

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

- **Always start hub before paper-trade engines** (unless explicitly using `--no-hub` for debugging).
- On anomaly, `pause` first; then `resume` or `stop` after assessment.
- Paper phase must not use live credentials.
