---
name: pm-research-loop
description: Use this skill when the user asks to evaluate prediction-market hypotheses on yes/no time-series data using repeatable research and backtest loops.
---

# PM Research Loop

Use this skill to turn hypotheses into ranked parameterized evidence.

## Inputs

- `history_file`
- `market_id`
- `event_id`
- `strategy_ref`
- optional params grid JSONL

## Workflow

1. Slice target series:

- `pm-cli research slice --history-file <history.jsonl> --market-id <M> --event-id <E> --output <slice.jsonl> --json`

2. Build features/labels:

- `pm-cli research features --history-file <history.jsonl> --market-id <M> --event-id <E> --output <features.jsonl> --json`
- `pm-cli research labels --history-file <history.jsonl> --market-id <M> --event-id <E> --output <labels.jsonl> --json`

3. Batch backtest params:

- `pm-cli research backtest-batch --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --params-jsonl <params.jsonl> --output <runs.jsonl> --json`

4. Rank and keep top runs:

- `pm-cli research compare-runs --input-file <runs.jsonl> --sort-key sharpe_ratio --top 20 --output <top_runs.jsonl> --json`

5. Persist experiment memory:

- `pm-cli research memory add --input-file <top_runs.jsonl> --tag <tag> --json`
- `pm-cli research memory list --tag <tag> --json`

## Params JSONL Row Format

Use one JSON object per line:

```json
{
  "id": "run-1",
  "strategy_kwargs": { "entry_z": 1.2, "exit_z": 0.3, "trade_size": "25" }
}
```

## Hard Rules

- Always run JSON mode where supported.
- Never promote from a single run; require ranked comparison output.
- Store results with tags so future loops can avoid rediscovering failed regions.
