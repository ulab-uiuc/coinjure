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

## Speed Rules (read before starting)

**Do not use `alpha-pipeline` for iteration.** It runs validate + backtest + stress + gate + 20-market batch — 25+ backtests per call. Only run it once at the final gate step.

Correct inner loop:

```
scan-markets  →  param-sweep  →  alpha-pipeline --no-run-batch-markets  →  paper
```

Always add `--spread 0.003` to every backtest/pipeline call. The default `--spread 0.01` is 2% round-trip friction and will make nearly every strategy appear unprofitable.

## Step-by-Step

### 1. Find market candidates

```bash
coinjure research markets \
  --history-file data/backtest_5min.jsonl \
  --sort-by volatility --limit 20 \
  --min-std 0.01 \
  --output data/research/<run_id>/markets.jsonl --json
```

Use `--trend-direction up|down|neutral` to filter for markets that suit the strategy direction.

### 2. Fast market scan with the strategy

Run the strategy across all candidate markets to find where it actually has edge. **Do this before any parameter tuning.**

```bash
coinjure research scan-markets \
  --history-file data/backtest_5min.jsonl \
  --strategy-ref strategies/<strategy_name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --max-markets 20 --sort-key price_range \
  --output data/research/<run_id>/scan.jsonl --json
```

Pick the top 2-3 markets by PnL / Sharpe from the scan output. Note their `market_id` and `event_id`.

### 3. Create or update strategy

```bash
coinjure strategy create --output strategies/<strategy_name>.py --class-name <ClassName>
```

**Required pattern** (copy from `strategies/alpha_momentum_v2.py`):

- Use `self.record_decision()` OR override `get_decisions()` / `get_decision_stats()`.
- Only call `trader.place_order(side=TradeSide.BUY, ...)` to enter; only SELL after confirming `position.quantity > 0`.
- Check `result.order is not None and result.order.filled_quantity > 0` to confirm fills.
- Use `ticker.symbol` as dict key, `ticker.name` for display.
- Set entry limit = `min(Decimal('0.99'), price + Decimal('0.003'))`.
- Set exit limit = `max(Decimal('0.01'), price - Decimal('0.003'))`.

### 4. Validate (quick smoke test)

```bash
coinjure strategy validate \
  --strategy-ref strategies/<strategy_name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --dry-run --events 20 --json
```

Must see `"ok": true` and `decisions >= 1` before proceeding.

### 5. Parameter sweep on best market

Run grid search to find optimal kwargs in one shot. **Do not manually iterate alpha-pipeline for this.**

```bash
coinjure research param-sweep \
  --history-file data/backtest_5min.jsonl \
  --market-id <market_id> --event-id <event_id> \
  --strategy-ref strategies/<strategy_name>.py:<ClassName> \
  --param-grid '{"min_move":[0.002,0.003,0.005],"window":[2,3],"max_hold":[12,20,30],"cooldown":[2,4]}' \
  --spread 0.003 --sort-by total_pnl --top 5 \
  --output data/research/<run_id>/sweep.json --json
```

Pick the kwargs with highest `total_pnl` and positive `sharpe_ratio`.

### 6. Gate with alpha-pipeline (single market, no batch)

Run the full gate **once** with the winning params. Skip the slow batch step during iteration.

```bash
coinjure research alpha-pipeline \
  --history-file data/backtest_5min.jsonl \
  --strategy-ref strategies/<strategy_name>.py:<ClassName> \
  --strategy-kwargs-json '<winning_json>' \
  --market-id <market_id> --event-id <event_id> \
  --spread 0.003 \
  --no-run-batch-markets \
  --artifacts-dir data/research/<run_id> --json
```

If `"passed": true` → proceed to step 7. If not, go back to step 5.

### 7. Generalization check (batch, run once)

Only run this after the single-market gate passes.

```bash
coinjure research batch-markets \
  --history-file data/backtest_5min.jsonl \
  --strategy-ref strategies/<strategy_name>.py:<ClassName> \
  --strategy-kwargs-json '<winning_json>' \
  --limit 20 \
  --output data/research/<run_id>/batch.jsonl --json
```

Parse results: `python3 -c "import json; data=[json.loads(l) for l in open('data/research/<run_id>/batch.jsonl')]; [print(r['market_id'], r['metrics'].get('total_pnl','?'), r['metrics'].get('sharpe_ratio','?')) for r in sorted(data, key=lambda x: float(x['metrics'].get('total_pnl',0)), reverse=True)]"`

Require: at least 30% of markets show positive PnL.

### 8. Promotion decision

If steps 6 and 7 pass → proceed to paper run. Otherwise iterate.

## Paper and Live Operations

Paper run:

```bash
coinjure paper run \
  --exchange polymarket \
  --strategy-ref strategies/<strategy_name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --duration 600 --json
```

Live run (requires explicit user approval):

```bash
coinjure live run \
  --exchange polymarket \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --strategy-ref strategies/<strategy_name>.py:<ClassName> \
  --strategy-kwargs-json '<json>'
```

## Runtime Control

```bash
coinjure trade status --json
coinjure trade state --json
coinjure trade swap --strategy-ref strategies/<strategy_name>.py:<ClassName> --strategy-kwargs-json '<json>' --json
coinjure trade pause
coinjure trade resume
coinjure trade stop
coinjure trade killswitch --on
```

## Hard Rules

- **Never use `alpha-pipeline` for parameter search** — use `param-sweep` or `grid`.
- **Always pass `--spread 0.003`** — default 0.01 is unrealistically high and masks edge.
- Use JSON output whenever supported for machine-readable logs.
- Do not promote based on one backtest.
- Store every run under `data/research/<run_id>/`.
- Treat live deployment as opt-in and approval-gated.
