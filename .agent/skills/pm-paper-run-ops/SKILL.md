---
name: pm-paper-run-ops
description: Use this skill when asked to launch, monitor, and safely operate paper-trading sessions after backtest gating.
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

- `coinjure strategy validate --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --dry-run --events 10 --json`

2. Launch paper run:

- `coinjure paper run --exchange <exchange> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --duration <seconds> --json`
- optional monitor mode: add `--monitor`

3. Runtime control (separate terminal):

- `coinjure trade status --json`
- `coinjure trade state --json`
- `coinjure trade pause`
- `coinjure trade resume`
- `coinjure trade stop`

4. Post-run artifact capture:

- save status/state snapshots and decisions in `data/research/<run_id>/paper/`.

## Hard Rules

- If behavior is abnormal, run `trade pause` first.
- Use `trade stop` for clean shutdown.
- Do not run live commands from this skill.
