---
name: pm-quant-strategy-authoring
description: Use this skill to implement quantitative ideas as Strategy code with tunable parameter interfaces and verifiable behavior.
---

# PM Quant Strategy Authoring

Use this skill when the user asks to have the agent design and code a strategy.

## Goal

- Produce a runnable strategy class (`Strategy` subclass)
- Expose JSON-serializable constructor parameters (for `engine deploy` / `engine add`)
- Ensure `strategy validate` and `strategy backtest` can execute

## Code Entry Points

- Arbitrage strategy base class: `coinjure/strategy/strategy.py` (`Strategy`)
- Existing arbitrage strategy references:
  - `examples/strategies/direct_arb_strategy.py` — cross-platform two-leg arbitrage
  - `examples/strategies/event_sum_arb_strategy.py` — single-platform event-sum arbitrage
  - `examples/strategies/multi_leg_arb_strategy.py` — multi-leg arbitrage
- Quant strategy base class: `coinjure/strategy/quant_strategy.py` (`QuantStrategy`)
- New strategy directory: `strategies/` or `examples/strategies/`

## Arbitrage Strategy Key Constraints

Constructor must only accept JSON-serializable primitive types (str / float / int / bool) so it can be deployed via `engine deploy --strategy-kwargs-json` or `engine add --kwargs-json`:

```python
class MyArbStrategy(Strategy):
    def __init__(
        self,
        event_id: str,          # from market scan-events output
        min_edge: float = 0.02,
        trade_size: float = 10.0,
        cooldown_seconds: int = 60,
    ) -> None: ...
```

## Implementation Workflow

1. Implement `process_event` following existing arbitrage strategy patterns

2. Quick validation

```bash
coinjure strategy validate \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --dry-run --events 10 --json
```

3. Single-market backtest (parquet data)

```bash
coinjure strategy backtest \
  --parquet data/<file>.parquet \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --json
```

4. Batch backtest

```bash
coinjure strategy batch \
  --history-file <history.jsonl> \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --limit 20 \
  --output data/research/batch_result.json \
  --json
```

5. Full pipeline (validate + backtest + gate)

```bash
coinjure strategy alpha-pipeline \
  --history-file <history.jsonl> \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --json
```

## Hard Rules

- Do not embed strategy logic in CLI scripts; it must live in a standalone strategy file.
- Constructor parameters must only be JSON-serializable primitives (no dict / object / callable).
- Do not use future information (no look-ahead).
- Must be reproducible: strategy file path, class name, kwargs, and commands must all be explicit.
- Arbitrage strategy `process_event` must actually call `trader.place_order(...)`, not just `record_decision`.
