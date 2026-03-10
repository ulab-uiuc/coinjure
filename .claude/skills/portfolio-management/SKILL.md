---
name: portfolio-management
description: Manage multi-strategy portfolios and automate lifecycle.
---

# Portfolio Management

Use this skill when the user asks to manage multiple strategies or automate strategy lifecycle.

## View & Monitor

```bash
coinjure engine list --json
coinjure engine status --json              # All engines
coinjure engine status --id <id> --json    # Single engine
```

## Lifecycle Management

```bash
coinjure engine retire --id <id> --reason "market_closed" --json
coinjure engine retire --all --reason "end_of_season" --json
```

## Intervention

```bash
coinjure engine pause --id <strategy_id> --json
coinjure engine pause --all --json
coinjure engine stop --all --json
```

## Hard Rules

- Live strategy count must not exceed `--max-live` limit.
- Monitor engine status regularly; retire stale strategies promptly.
