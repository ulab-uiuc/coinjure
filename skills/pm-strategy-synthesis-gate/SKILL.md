---
name: pm-strategy-synthesis-gate
description: Use this skill when asked to turn research findings into a strategy file and gate it for paper/live promotion.
---

# PM Strategy Synthesis And Gate

Use this skill after hypotheses or parameter studies identify a candidate approach.

## Inputs

- target strategy file path
- strategy class name
- selected `strategy_kwargs`
- `history_file`, `market_id`, `event_id`

## Workflow

1. Create/update strategy file:

- `coinjure strategy create --output ./strategies/<name>.py --class-name <ClassName>`
- implement logic by adapting `coinjure/strategy/*` and `examples/strategies/*`.

2. Validate constructor and runtime:

- `coinjure strategy validate --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --dry-run --events 10 --json`

3. Gate robustness:

- `coinjure research walk-forward-auto --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --output <wf.jsonl> --json`
- `coinjure research stress-test --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --output <stress.jsonl> --json`
- `coinjure research strategy-gate --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref ./strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --json`

4. Promotion path:

- if gate passes: run `pm-paper-trade-ops`.
- if paper run is stable and user explicitly approves: run `pm-live-trade-ops`.

## Hard Rules

- No paper/live before validate + dry-run + gate.
- Keep rollback commands ready (`trade pause`, `trade stop`, `trade killswitch --on`).
