---
name: pm-research-loop
description: Use this skill when asked to run the full prediction-market research loop from market selection to gated promotion artifacts.
---

# PM Research Loop

Use this skill to execute the full process: discovery -> validation -> backtest -> robustness -> gate -> paper-ready package.

## Inputs

- `history_file`
- `strategy_ref`
- optional `strategy_kwargs_json`
- optional `market_id` and `event_id`
- optional gate thresholds

## Preferred Workflow

1. Discover candidate markets:

- `coinjure research markets --history-file <history.jsonl> --sort-by points --limit 20 --output <markets.jsonl> --json`

2. Run one-shot pipeline:

- `coinjure research alpha-pipeline --history-file <history.jsonl> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --market-id <M> --event-id <E> --artifacts-dir <dir> --json`
- if market IDs are unknown, omit them and use `--market-rank <n>` with `--market-sort-by <key>`.

3. Expand to cross-market validation:

- `coinjure research batch-markets --history-file <history.jsonl> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --limit 20 --output <batch.jsonl> --json`

4. Persist experiment memory:

- `coinjure research memory add --input-file <batch_or_top_runs.jsonl> --tag <tag> --json`
- `coinjure research memory list --tag <tag> --json`

5. Handoff to paper run:

- if gate checks pass, proceed to `pm-paper-run-ops`.

## Hard Rules

- Produce reproducible artifacts under `data/research/<run_id>/`.
- Treat failed gate checks as a loop-back signal, not a promotion signal.
- Keep all commands in JSON mode where available.
