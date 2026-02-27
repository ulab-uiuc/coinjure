---
name: strategy-lab
description: Autonomous prediction-market strategy lab. Use when asked to design, test, iterate, and operationalize strategies from local history data to paper/live deployment.
---

# Strategy Lab

Run the full strategy lifecycle with reproducible artifacts and clear promotion gates.

## Default Workspace

- strategy code: `coinjure/strategy/`, `examples/strategies/`, `strategies/`
- primary history file: `data/backtest_5min.jsonl`
- research artifacts: `data/research/<run_id>/`

## End-to-End Loop

1. Discover candidate markets:

- `coinjure research markets --history-file data/backtest_5min.jsonl --sort-by points --limit 20 --output data/research/<run_id>/markets.jsonl --json`

2. Create or update strategy:

- `coinjure strategy create --output strategies/<strategy_name>.py --class-name <ClassName>`
- implement `process_event` and constructor kwargs.

3. Validate quickly:

- `coinjure strategy validate --strategy-ref strategies/<strategy_name>.py:<ClassName> --strategy-kwargs-json '<json>' --dry-run --events 10 --json`

4. Run main pipeline:

- `coinjure research alpha-pipeline --history-file data/backtest_5min.jsonl --strategy-ref strategies/<strategy_name>.py:<ClassName> --strategy-kwargs-json '<json>' --market-rank 1 --market-sort-by points --artifacts-dir data/research/<run_id> --json`

5. Expand generalization check:

- `coinjure research batch-markets --history-file data/backtest_5min.jsonl --strategy-ref strategies/<strategy_name>.py:<ClassName> --strategy-kwargs-json '<json>' --limit 20 --output data/research/<run_id>/batch.jsonl --json`

6. Promotion decision:

- if gate and paper criteria pass, move to live with explicit user approval.

## Paper and Live Operations

- paper run:
- `coinjure paper run --exchange polymarket --strategy-ref strategies/<strategy_name>.py:<ClassName> --strategy-kwargs-json '<json>' --duration 600 --json`
- live run:
- `coinjure live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY" --strategy-ref strategies/<strategy_name>.py:<ClassName> --strategy-kwargs-json '<json>'`

## Runtime Control

- `coinjure trade status --json`
- `coinjure trade state --json`
- `coinjure trade swap --strategy-ref strategies/<strategy_name>.py:<ClassName> --strategy-kwargs-json '<json>' --json`
- `coinjure trade pause`
- `coinjure trade resume`
- `coinjure trade stop`
- `coinjure trade killswitch --on`

## Hard Rules

- Use JSON output whenever supported for machine-readable logs.
- Do not promote based on one backtest.
- Store every run under `data/research/<run_id>/`.
- Treat live deployment as opt-in and approval-gated.
