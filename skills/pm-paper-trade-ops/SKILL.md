---
name: pm-paper-trade-ops
description: Use this skill to execute paper trading, monitoring, intervention, and archival after a strategy passes backtesting.
---

# PM Paper Trade Ops

Use this skill when the user asks to run paper trading or verify live behavior.

## Prerequisites

- Strategy has passed: `coinjure strategy validate ... --json`
- Clear strategy_ref + kwargs available (from market scan / scan-events output)

## Launch Methods

### Method A: Single strategy manual launch

```bash
# Cross-platform arbitrage (Poly + Kalshi)
coinjure engine run --mode paper \
  --exchange cross_platform \
  --strategy-ref examples/strategies/direct_arb_strategy.py:DirectArbStrategy \
  --strategy-kwargs-json '{"poly_market_id":"...","poly_token_id":"...","kalshi_ticker":"...","min_edge":0.02}' \
  --json

# Single-platform event-sum arbitrage
coinjure engine run --mode paper \
  --exchange polymarket \
  --strategy-ref examples/strategies/event_sum_arb_strategy.py:EventSumArbStrategy \
  --strategy-kwargs-json '{"event_id":"...","min_edge":0.01}' \
  --json

# Connect to shared data source (required for multi-strategy)
coinjure engine run --mode paper ... --hub-socket ~/.coinjure/hub.sock --json

# Visual monitoring
coinjure engine run --mode paper ... --monitor
```

### Method B: Batch deployment via portfolio (recommended)

```bash
# Cross-platform batch (auto scan + register + launch)
coinjure engine deploy --query "NBA" --min-edge 0.02 --max-deploy 5 --json

# Single-platform event-sum batch
coinjure engine deploy-events --query "NBA" --min-edge 0.01 --max-deploy 5 --json

# Dry-run validation first
coinjure engine deploy-events --query "NBA" --dry-run --json
```

## Runtime Control (single engine)

```bash
coinjure engine status --json          # running status
coinjure engine state --json           # full snapshot (positions, decisions, order books)
coinjure engine pause --json           # pause
coinjure engine resume --json          # resume
coinjure engine swap --strategy-ref <ref> --strategy-kwargs-json '<json>' --json
coinjure engine stop --json            # stop
```

## Batch Monitoring (portfolio)

```bash
coinjure engine list --json
coinjure engine health --json   # check which are alive, which crashed, PnL
coinjure engine retire --strategy-id <id> --reason <reason> --json
```

## Hub Shared Data Source

```bash
# Must start hub first for multi-strategy (avoids API rate limiting)
coinjure hub start --detach --json
coinjure hub status --json
coinjure hub stop --json
```

## Result Archival

- Save key outputs to `data/research/<strategy_id>/`
- Must include at minimum: config (strategy_ref + kwargs), state snapshot, final PnL

## Hard Rules

- On anomaly, `pause` first; then `resume` or `stop` after confirmation.
- Never use live credentials during paper trading.
- Must `--dry-run` before batch deployment to verify strategy instantiation.
- Must start hub when running multiple strategies in parallel to avoid exchange rate limiting.
