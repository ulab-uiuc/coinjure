---
name: pm-hypothesis-discovery
description: Use this skill when asked to discover prediction-market trade ideas and select backtest targets from local history files and optional live exchange/news context.
---

# PM Hypothesis Discovery

Use this skill to produce candidate hypotheses before coding or promotion.

## Inputs

- `history_file` (default: `data/backtest_5min.jsonl`)
- optional `theme`/sector
- optional exchange preference (`polymarket` or `kalshi`)
- optional liquidity and spread constraints

## Workflow

1. Build local market universe first (fast, deterministic):

- `coinjure research markets --history-file <history.jsonl> --sort-by points --limit 50 --json`

2. Optionally enrich with live context when network is available:

- `coinjure market search --exchange <exchange> --query "<theme>" --limit 50 --json`
- `coinjure market list --exchange <exchange> --limit 50 --json`
- `coinjure news fetch --source google --query "<theme>" --limit 20 --json`
- `coinjure news fetch --source rss --query "<theme>" --limit 20 --json`

3. Inspect top candidates:

- `coinjure market info --exchange <exchange> --market-id <id> --json` (optional live check)
- if offline: use top rows from `research markets` output.

4. Produce 3-10 hypotheses with fields:

- `hypothesis_id`
- `market_id`
- `event_id`
- `direction` (`long_yes` / `long_no` / `mean_revert` / `momentum`)
- `trigger`
- `invalidation`
- `holding_horizon`
- `risk_note`
- `why_now`

5. Write hypothesis artifact for downstream steps:

- `data/research/<run_id>/hypotheses.jsonl`

## Hard Rules

- Do not use look-ahead information.
- Reject hypotheses without explicit invalidation criteria.
- If live APIs fail, continue with dataset-driven discovery instead of blocking.
