---
name: pm-strategy-synthesis-gate
description: Use this skill when the user asks to convert research findings into a strategy file, verify runtime behavior, and gate promotion to paper/live trading.
---

# PM Strategy Synthesis And Gate

Use this skill after hypotheses are ranked.

## Inputs

- target strategy file path
- strategy class name
- selected `strategy_kwargs`
- `history_file`, `market_id`, `event_id`

## Workflow

1. Create/update strategy file:

- `pm-cli strategy create --output ./strategies/<name>.py --class-name <ClassName>`
- implement logic by adapting `examples/strategies/*`.

2. Validate constructor and imports:

- `pm-cli strategy validate --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --json`

3. Runtime sanity:

- `pm-cli strategy dry-run --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --events 10 --json`

4. Robustness checks:

- `pm-cli research walk-forward --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --output <wf.jsonl> --json`
- `pm-cli research stress-test --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --output <stress.jsonl> --json`
- `pm-cli research strategy-gate --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --json`

5. Promotion path:

- if gate passes: run `paper run`
- if stable in paper and explicitly approved: run `live run`

## Hard Rules

- No paper/live run before `validate` + `dry-run` + `strategy-gate`.
- Always keep rollback path (`trade pause` / `trade stop`).
- Do not use removed commands (`trade estop`, `news search`, `analytics`, `token cancel`).
