---
name: pm-hypothesis-discovery
description: Use this skill when the user asks to discover prediction-market trading ideas from market structure and news catalysts before coding a strategy.
---

# PM Hypothesis Discovery

Use this skill to generate candidate trading hypotheses (not code yet).

## Inputs

- Theme or sector (e.g. crypto, elections, macro)
- Exchange preference (`polymarket` or `kalshi`)
- Liquidity/spread constraints

## Workflow

1. Build market universe:

- `pm-cli market list --exchange <exchange> --limit 100`
- `pm-cli market search --exchange <exchange> --query "<theme>" --limit 100`
- `pm-cli research universe --exchange <exchange> --min-volume <v> --max-spread <s> --json`

2. Pull catalyst context:

- `pm-cli news fetch --source google --query "<theme>" --limit 20 --json`
- `pm-cli news fetch --source rss --query "<theme>" --limit 20 --json`

3. Inspect top candidates:

- `pm-cli market info --exchange <exchange> --market-id <id> --json`

4. Produce hypothesis set (3-10 ideas), each with:

- `hypothesis_id`
- `market_id`
- `event_id`
- `direction` (`long_yes` / `long_no` / `mean_revert` / `momentum`)
- `trigger`
- `invalidation`
- `holding_horizon`
- `risk_note`
- `why_now`

## Hard Rules

- Do not use look-ahead information.
- Prefer high-liquidity, bounded-spread markets first.
- Reject hypotheses without explicit invalidation criteria.
