---
name: pm-paper-run-ops
description: Use this skill when the user asks to launch, monitor, and safely operate paper trading runs with coinjure.
---

# PM Paper Run Ops

Use this skill for paper-trading execution loops.

## Inputs

- `exchange` (`polymarket`, `kalshi`, `rss`)
- `strategy_ref`
- optional `strategy_kwargs_json`
- optional run duration and monitor preference

## Workflow

1. Preflight:

- `coinjure strategy validate --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`
- `coinjure strategy dry-run --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --events 10 --json`

2. Launch paper run:

- `coinjure paper run --exchange <exchange> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --monitor`

3. Operational control (separate terminal):

- `coinjure trade status --json`
- `coinjure trade pause`
- `coinjure trade resume`
- `coinjure trade stop`

4. Optional token-level checks:

- `coinjure token orderbook <token_id>`
- `coinjure token positions`
- `coinjure token place --token <token_id> --side buy --price <p> --size <q> --json`

## Hard Rules

- If behavior is abnormal, `trade pause` first, then inspect.
- Use `trade stop` for clean shutdown.
- Do not use removed commands (`trade estop`, `token cancel`).
