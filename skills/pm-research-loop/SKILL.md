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

1. Check past experiments first:

- `coinjure research memory best --metric total_pnl --top 10 --json`
- `coinjure research memory list --tag <relevant_tag> --json`

2. Discover candidate markets:

- `coinjure research markets --history-file <history.jsonl> --sort-by points --limit 20 --output <markets.jsonl> --json`

3. Run one-shot pipeline:

- `coinjure research alpha-pipeline --history-file <history.jsonl> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --market-id <M> --event-id <E> --artifacts-dir <dir> --json`
- if market IDs are unknown, omit them and use `--market-rank <n>` with `--market-sort-by <key>`.
- Results are auto-recorded to the experiment ledger.

4. Expand to cross-market validation:

- `coinjure research batch-markets --history-file <history.jsonl> --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --limit 20 --output <batch.jsonl> --json`

5. Manually record extra results to memory (if not using alpha-pipeline):

- `coinjure research memory add --run-id <id> --strategy-ref <ref> --market-id <M> --metrics-json '{"total_pnl": ..., "sharpe_ratio": ...}' --tag <tag> --json`

6. Handoff to paper run:

- if gate checks pass, proceed to `pm-paper-trade-ops`.

## Hard Rules

- Produce reproducible artifacts under `data/research/<run_id>/`.
- Treat failed gate checks as a loop-back signal, not a promotion signal.
- Keep all commands in JSON mode where available.
- Check memory before starting to avoid repeating failed approaches.
