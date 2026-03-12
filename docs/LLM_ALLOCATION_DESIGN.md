# LLM Allocation Design

This document describes the implemented hybrid quant + LLM allocation design for builtin arbitrage strategies.

## Goal

Keep the existing quant arbitrage detectors and fast execution path, while allowing operators to opt into LLM review for:

- portfolio capital allocation across relations
- launch-time sizing parameters per strategy

The LLM does not decide which arbitrage opportunities exist. Builtin strategies still own edge detection and trade execution.

## Design Summary

The system has two optional LLM control points:

1. **Portfolio review**
   - Module: `coinjure/trading/llm_allocator.py`
   - Triggered from batch engine startup
   - Takes the baseline quant allocation from `allocate_capital()` and optionally adjusts per-strategy budgets

2. **Launch-time sizing review**
   - Module: `coinjure/trading/llm_sizing.py`
   - Triggered from batch engine startup
   - Produces conservative sizing overrides such as `kelly_fraction`, `min_size`, and `max_size`

Both features are off by default.

## Runtime Flow

```text
relations
   |
   v
_run_batch() in engine_commands.py
   |
   +--> allocate_capital() -------------------------------+
   |                                                      |
   +--> optional allocate_capital_llm() review            |
   |                                                      v
   |                                                 budgets per relation
   |
   +--> optional compute_llm_sizing() review
   |         |
   |         v
   |    sizing overrides per relation
   |
   v
build strategy kwargs and launch engine instances
   |
   v
builtin strategy process_event()
   |
   v
compute_trade_size() + RiskManager backstops
```

## Strategy Behavior

All 7 builtin strategies support the `llm_trade_sizing` toggle in their constructors.

Implemented strategy behavior:

- `DirectArbStrategy` and `GroupArbStrategy`
  - already used `compute_trade_size()`
  - now accept the LLM sizing toggle like the other strategies

- `ImplicationArbStrategy`
- `ConditionalArbStrategy`
- `StructuralArbStrategy`
- `CointSpreadStrategy`
- `LeadLagStrategy`
  - now use `compute_trade_size()` instead of fixed `self.trade_size`
  - preserve quant sizing by default
  - can accept launch-time LLM sizing overrides through kwargs

This keeps the hot path quant-only even when LLM sizing is enabled.

## Safety Model

### Quant remains the baseline

- `allocate_capital()` runs first for portfolio allocation
- `compute_trade_size()` remains the execution-time sizing function
- LLM review is optional and additive

### Fallback behavior

- portfolio review failure -> use baseline quant budgets
- sizing review failure -> use default quant sizing params
- malformed or invalid LLM output -> ignore and fall back silently

### Validation rules

`llm_allocator.py` validates that:

- all returned strategy IDs match the candidate set
- each budget is positive
- total allocation does not exceed deployable capital
- each strategy stays within the per-strategy cap

`llm_sizing.py` validates that:

- all values are numeric and positive
- `kelly_fraction` is clamped to `0.5` maximum
- `min_size <= max_size`
- unknown strategy IDs are ignored

### Existing risk controls stay in place

The LLM cannot bypass runtime safety controls. Orders still flow through the existing `RiskManager` and trader checks.

## CLI Toggles

Batch engine commands support two flags:

- `--llm-portfolio-review`
- `--llm-trade-sizing`

Examples:

```bash
coinjure engine paper-run --all-relations --llm-portfolio-review
coinjure engine paper-run --all-relations --llm-trade-sizing
coinjure engine paper-run --all-relations --llm-portfolio-review --llm-trade-sizing
coinjure engine live-run --all-relations --llm-portfolio-review --llm-trade-sizing
```

## Implemented Modules

- `coinjure/trading/llm_allocator.py`
  - wraps quant allocation with optional LLM budget review
  - returns baseline budgets on any failure

- `coinjure/trading/llm_sizing.py`
  - computes launch-time sizing overrides for one or more strategies
  - returns an empty override map on any failure

- `coinjure/cli/engine_commands.py`
  - wires the two LLM toggles into `_run_batch()`

## Tests

Coverage for the LLM modules lives in:

- `tests/test_llm_allocator.py`
- `tests/test_llm_sizing.py`

These tests verify:

- valid LLM responses are accepted
- invalid JSON and validation failures fall back safely
- API exceptions fall back safely
- sizing overrides are validated and filtered correctly

## Non-Goals

This design does not currently do the following:

- per-trade LLM calls inside the event loop
- LLM-driven trade entry or exit decisions
- LLM-driven replacement of builtin arbitrage detection logic
- centralized portfolio rebalancing outside batch startup

## Rationale

This design keeps the system fast and reproducible:

- quant code still handles the hot path
- LLM usage is explicit and opt-in
- strategy refs and kwargs remain serializable in the registry
- failures degrade to deterministic quant behavior instead of blocking trading
