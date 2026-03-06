---
name: pm-backtest-ops
description: Use this skill when asked to run repeatable backtests, robustness checks, and promotion gates for prediction-market strategies.
---

# PM Backtest Ops

Use this skill for deterministic backtest execution and evidence generation.

## Inputs

- `history_file`
- `market_id`
- `event_id`
- `strategy_ref`
- optional `strategy_kwargs_json`
- optional parameter set (`params_jsonl` or `param_grid_json`)

## Fast Path (recommended)

Run one command to execute preflight + backtest + stress + gate (+ optional batch):

- `coinjure research alpha-pipeline --history-file <history.jsonl> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --market-id <M> --event-id <E> --artifacts-dir <dir> --json`

Results are auto-recorded to the experiment ledger (`~/.coinjure/experiment_ledger.jsonl`).

## Manual Workflow

1. Preflight checks:

- `coinjure strategy validate --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --dry-run --events 10 --json`

2. Single backtest:

- `coinjure backtest run --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

3. Parameter sweep:

- `coinjure research backtest-batch --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --params-jsonl <params.jsonl> --output <runs.jsonl> --json`
- `coinjure research grid --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --param-grid-json '<grid_json>' --output <grid_runs.jsonl> --json`
- `coinjure research compare-runs --input-file <runs_or_grid.jsonl> --sort-key sharpe_ratio --top 20 --output <top_runs.jsonl> --json`

4. Robustness:

- `coinjure research walk-forward-auto --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --output <wf.jsonl> --json`
- `coinjure research stress-test --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --output <stress.jsonl> --json`

5. Gate before paper/live:

- `coinjure research strategy-gate --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

## Hard Rules

- Always use JSON outputs where supported.
- Never promote based on one run.
- Keep artifacts (`runs.jsonl`, `top_runs.jsonl`, `wf.jsonl`, `stress.jsonl`, `gate.json`).
