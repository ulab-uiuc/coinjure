---
name: pm-backtest-ops
description: Use this skill when the user asks to run backtests, compare strategy parameters, and produce promotion-ready evidence from historical yes/no time-series data.
---

# PM Backtest Ops

Use this skill for deterministic backtest execution and analysis.

## Inputs

- `history_file`
- `market_id`
- `event_id`
- `strategy_ref`
- optional `strategy_kwargs_json`
- optional params grid `params_jsonl`

## Workflow

1. Preflight checks:

- `pm-cli strategy validate --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`
- `pm-cli strategy dry-run --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --events 10 --json`

2. Single backtest:

- `pm-cli backtest run --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

3. Parameter sweep:

- `pm-cli research backtest-batch --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --params-jsonl <params.jsonl> --output <runs.jsonl> --json`
- `pm-cli research compare-runs --input-file <runs.jsonl> --sort-key sharpe_ratio --top 20 --output <top_runs.jsonl> --json`

4. Robustness:

- `pm-cli research walk-forward --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --output <wf.jsonl> --json`
- `pm-cli research stress-test --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --output <stress.jsonl> --json`

5. Gate before paper/live:

- `pm-cli research strategy-gate --history-file <history.jsonl> --market-id <M> --event-id <E> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

## Hard Rules

- Always use JSON outputs where supported.
- Never promote based on one run.
- Keep artifacts (`runs.jsonl`, `top_runs.jsonl`, `wf.jsonl`, `stress.jsonl`) for reproducibility.
