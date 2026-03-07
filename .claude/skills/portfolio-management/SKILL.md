---
name: portfolio-management
description: Allocate capital, manage multi-strategy portfolios, automate lifecycle.
---

# Portfolio Management

Use this skill when the user asks to manage multiple strategies, allocate capital, or automate strategy lifecycle.

## Strategy Registration and Deployment

```bash
# Register
coinjure engine add --strategy-id <id> --strategy-ref <ref> --kwargs-json '<json>' --json

# Deploy (start paper/live process)
coinjure engine add --strategy-id <id> --strategy-ref <ref> --kwargs-json '<json>' --json

# View all strategies
coinjure engine list --json
```

## Health Checks and Retirement

```bash
coinjure engine report --check-health --json   # PnL report + detect dead/stale/degraded
coinjure engine retire --id <id> --reason "market_closed" --json
coinjure engine retire --all --reason "end_of_season" --json
```

## Capital Allocation

```bash
# Three methods: equal (even split), edge (PnL-weighted), kelly (half-Kelly)
coinjure engine allocate --method kelly --max-exposure 10000 --max-per-strategy 2000 --json
```

## Monitoring & Intervention

```bash
# Agent checks health and decides actions
coinjure engine report --check-health --json
coinjure engine pause --id <strategy_id> --json
coinjure engine retire --id <strategy_id> --reason "spread collapsed" --json
```

## Bulk Operations

```bash
coinjure engine pause --all --json
coinjure engine stop --all --json
coinjure engine report --json
```

## Hard Rules

- Live strategy count must not exceed `--max-live` limit.
- Run `report --check-health` regularly; retire stale strategies promptly.
