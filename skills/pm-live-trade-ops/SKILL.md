---
name: pm-live-trade-ops
description: Use this skill to execute live trading under explicit authorization with strict risk and emergency controls.
---

# PM Live Trade Ops

Use only when the user explicitly requests live trading and paper validation is complete.

## Prerequisites

- `strategy validate` passed
- Recent paper run behavior is stable (confirmed via `engine health`)
- User has explicitly approved live launch

## Launch Commands

### Polymarket

```bash
coinjure engine run --mode live \
  --exchange polymarket \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --json
```

### Kalshi

```bash
coinjure engine run --mode live \
  --exchange kalshi \
  --kalshi-api-key-id "$KALSHI_API_KEY_ID" \
  --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --json
```

### Cross-platform (via hub shared data)

```bash
coinjure hub start --detach --json
coinjure engine run --mode live \
  --exchange cross_platform \
  --hub-socket ~/.coinjure/hub.sock \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --kalshi-api-key-id "$KALSHI_API_KEY_ID" \
  --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --json
```

### Via portfolio promotion (recommended)

```bash
coinjure engine promote --strategy-id <id> --to live --json
```

## Runtime Control

```bash
coinjure engine status --json
coinjure engine state --json
coinjure engine pause --json
coinjure engine resume --json
coinjure engine swap --strategy-ref <ref> --strategy-kwargs-json '<json>' --json
coinjure engine stop --json
```

## Batch Monitoring

```bash
coinjure engine list --json
coinjure engine health --json
coinjure engine retire --strategy-id <id> --reason <reason> --json
```

## Emergency Procedure

1. `pause` first
2. `engine state --json` to assess positions and order status
3. If necessary, `engine stop --json`
4. Batch stop: `engine retire --strategy-id <id> --reason "emergency"`

## Hard Rules

- Never launch live without explicit user approval.
- Never skip the paper stage and go directly to live.
- All live runs must maintain an auditable record (timestamp, parameters, state snapshot, actions taken).
