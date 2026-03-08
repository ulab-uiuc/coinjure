---
name: strategy-writing
description: Write spread strategy code as a Strategy subclass and register it.
---

# Strategy Writing

Use this skill when the user asks to write spread/arb strategy code.

## Code Entry Points

- Base class: `coinjure/strategy/strategy.py` (`Strategy` subclass, implement `process_event`)
- Shared trading types: `coinjure/trading/types.py` (`TradeSide`), `coinjure/trading/trader.py` (`Trader`)
- Built-in strategies (one per relation type): `coinjure/strategy/builtin/*.py`
- Custom strategies go in: `strategies/`

## Workflow

1. Write the strategy file

- Handle events in `process_event` (typically `PriceChangeEvent`)
- Put parameters in the constructor, keep them JSON-serializable
- Place trades via `trader.place_order(...)`
- Log signals via `self.record_decision(...)`

2. Register in the strategy registry

```bash
coinjure engine add \
  --strategy-id <id> \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --kwargs-json '<json>' --json
```

## Hard Rules

- Strategy logic must live in a standalone `.py` file, not inline scripts.
- No look-ahead — never use future information.
- Must be reproducible: strategy path, class name, and kwargs must be explicit.
