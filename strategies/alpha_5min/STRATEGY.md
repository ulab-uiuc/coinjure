# Alpha 5-Minute Hybrid Strategy

## Strategy Ref
`./strategies/alpha_5min/alpha_5min_hybrid_strategy.py:AlphaFiveMinHybridStrategy`

## Idea
Hybrid long-YES signal on short-horizon markets:
- Momentum entry: buy after a positive jump above threshold.
- Mean-reversion entry: buy after oversold z-score with early reversal.
- Exit on risk/profit or reversal.

## Best Candidate Found
- Market: `517311`
- Event: `16282`
- Best run id: `run-05`
- Backtest PnL: `+0.24750`
- Trades: `4`
- Win rate: `50%`
- Max drawdown: `0.0013875`
- Sharpe: `-0.744`

## Recommended Params (run-05)
```json
{"trade_size":"15","lookback":3,"momentum_entry":"0.012","mean_revert_z":"0.90","stop_loss":"0.05","take_profit":"0.08"}
```

## Commands

Validate:
```bash
poetry run coinjure strategy validate \
  --strategy-ref ./strategies/alpha_5min/alpha_5min_hybrid_strategy.py:AlphaFiveMinHybridStrategy \
  --strategy-kwargs-json '{"trade_size":"15","lookback":3,"momentum_entry":"0.012","mean_revert_z":"0.90","stop_loss":"0.05","take_profit":"0.08"}' \
  --json
```

Backtest:
```bash
poetry run coinjure backtest run \
  --history-file artifacts/alpha_5min/backtest_5min_normalized.jsonl \
  --market-id 517311 \
  --event-id 16282 \
  --strategy-ref ./strategies/alpha_5min/alpha_5min_hybrid_strategy.py:AlphaFiveMinHybridStrategy \
  --strategy-kwargs-json '{"trade_size":"15","lookback":3,"momentum_entry":"0.012","mean_revert_z":"0.90","stop_loss":"0.05","take_profit":"0.08"}' \
  --json
```

Paper test:
```bash
poetry run coinjure paper run \
  --exchange rss \
  --duration 35 \
  --strategy-ref ./strategies/alpha_5min/alpha_5min_hybrid_strategy.py:AlphaFiveMinHybridStrategy \
  --strategy-kwargs-json '{"trade_size":"15","lookback":3,"momentum_entry":"0.012","mean_revert_z":"0.90","stop_loss":"0.05","take_profit":"0.08"}' \
  --json
```

## Artifacts
All research outputs are in:
`artifacts/alpha_5min/`

Paper test logs:
- `artifacts/alpha_5min/paper_run_517311.log` (rss, 15s)
- `artifacts/alpha_5min/paper_run_polymarket.log` (polymarket, 20s)

Short-duration paper tests completed cleanly with no fills in the sampled window.
