---
name: live-trade
description: Execute live trading with explicit authorization, strict risk and emergency controls.
---

# Live Trade

Use only when the user explicitly requests live trading and paper validation is complete.

## Prerequisites

- Backtest results acceptable
- Paper run behavior stable
- User explicitly approved

## Start

```bash
# Polymarket
coinjure engine live-run --exchange polymarket \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --strategy-ref <strategy_ref> --json

# Kalshi
coinjure engine live-run --exchange kalshi \
  --kalshi-api-key-id "$KALSHI_API_KEY_ID" \
  --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" \
  --strategy-ref <strategy_ref> --json
```

## Runtime Controls

```bash
coinjure engine status --id <strategy_id> --json
coinjure engine pause  --id <strategy_id> --json
coinjure engine resume --id <strategy_id> --json
coinjure engine stop   --id <strategy_id> --json
```

## Emergency Sequence

1. `engine pause --id <id>`
2. `engine status --id <id> --full` — assess positions
3. `engine killswitch --on` — global emergency halt
4. `engine stop --id <id>`

## Hard Rules

- Never start live without explicit user approval.
- Never skip the paper phase.
- All live runs must maintain an auditable record.
