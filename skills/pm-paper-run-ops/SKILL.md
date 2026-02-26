---
name: pm-paper-run-ops
description: Use this skill when the user asks to launch, monitor, and safely operate paper trading runs with pm-cli.
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

- `pm-cli strategy validate --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`
- `pm-cli strategy dry-run --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --events 10 --json`

2. Launch paper run:

- `pm-cli paper run --exchange <exchange> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --monitor`

3. Operational control (separate terminal):

- `pm-cli trade status --json`
- `pm-cli trade pause`
- `pm-cli trade resume`
- `pm-cli trade stop`

4. Optional token-level checks:

- `pm-cli token orderbook <token_id>`
- `pm-cli token positions`
- `pm-cli token place --token <token_id> --side buy --price <p> --size <q> --json`

## Hard Rules

- If behavior is abnormal, `trade pause` first, then inspect.
- Use `trade stop` for clean shutdown.
- Do not use removed commands (`trade estop`, `token cancel`).
