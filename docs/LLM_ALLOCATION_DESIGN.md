# LLM Allocation Design

This document describes the hybrid quant + LLM allocation system for builtin arbitrage strategies.

## Goal

Keep the existing quant arbitrage detectors and fast execution path, while allowing operators to opt into LLM review at two layers:

1. **Portfolio allocation** — LLM reviews capital distribution across relations at launch time.
2. **Per-opportunity trade sizing** — LLM reviews portfolio state and decides trade size when a strategy detects an arb opportunity at runtime.

The LLM does not decide which arbitrage opportunities exist. Builtin strategies still own edge detection and trade execution.

## Architecture

```text
relations
   |
   v
_run_batch() in engine_commands.py
   |
   +--> allocate_capital()  [quant baseline]
   |
   +--> optional allocate_capital_llm() review  [launch-time]
   |         |
   |         v
   |    adjusted budgets per relation
   |
   v
build strategy kwargs (incl. llm_trade_sizing, llm_model) and launch engines
   |
   v
builtin strategy process_event()
   |
   +--> detect opportunity (edge > threshold)
   |
   +--> await compute_trade_size_with_llm(pm, edge, ...)
            |
            +--> compute_trade_size()  [always runs first — quant baseline]
            |
            +--> if llm_trade_sizing=False: return quant_size  [fast path]
            |
            +--> if llm_trade_sizing=True:
            |       +--> rate limiter check (5s min gap)
            |       +--> build OpportunitySizingRequest with real portfolio state
            |       +--> await compute_opportunity_sizing_llm(request)
            |       +--> validate, clamp, quantize response
            |       +--> return llm_size (or quant_size on failure/None)
            |
            v
         trader.place_order(side, ticker, price, size)
```

## Two LLM Control Points

### 1. Portfolio Review (launch-time)

- Module: `coinjure/trading/llm_allocator.py`
- Function: `allocate_capital_llm()`
- Triggered once at batch engine startup
- Takes the baseline quant allocation and optionally adjusts per-strategy budgets
- Fallback: uses quant budgets on any failure

### 2. Per-Opportunity Trade Sizing (runtime)

- Module: `coinjure/trading/llm_sizing.py`
- Function: `compute_opportunity_sizing_llm()`
- Router: `coinjure/trading/sizing.py` → `compute_trade_size_with_llm()`
- Triggered inside the event loop when a strategy detects an arb opportunity
- Receives real context: edge, available capital, current exposure, portfolio utilization, quant baseline size
- Rate-limited (5s minimum gap between LLM calls, configurable)
- Fallback: returns quant size on rate-limit skip, API failure, invalid response, or None

Both features are **off by default**.

## Strategy Integration

All 7 builtin strategies call `await compute_trade_size_with_llm()` at every trade decision point (12 call sites total):

| Strategy | Call Sites | Relation Type |
|---|---|---|
| `DirectArbStrategy` | 1 | `same_event` |
| `GroupArbStrategy` | 1 | `same_event` |
| `ImplicationArbStrategy` | 1 | `implication` |
| `ConditionalArbStrategy` | 2 | `conditional` |
| `StructuralArbStrategy` | 2 | `structural` |
| `CointSpreadStrategy` | 2 | `cointegrated` |
| `LeadLagStrategy` | 2 | `lead_lag` |

Each strategy accepts `llm_trade_sizing: bool` and `llm_model: str | None` as constructor kwargs. When `llm_trade_sizing=False` (default), the async router immediately returns the quant size with zero overhead.

## Safety Model

### Quant remains the baseline

- `allocate_capital()` runs first for portfolio allocation
- `compute_trade_size()` always runs first for trade sizing
- LLM review is optional and additive — never blocks the quant path

### Fallback behavior

| Failure Mode | Behavior |
|---|---|
| Portfolio review API error | Use baseline quant budgets |
| Portfolio review invalid output | Use baseline quant budgets |
| Per-opportunity API error | Return quant size |
| Per-opportunity invalid size | Return quant size |
| Per-opportunity rate-limited | Return quant size (no API call) |
| LLM returns size > max_size | Clamp to max_size |
| LLM returns size < 1 after rounding | Clamp to 1 |

### Validation rules

`llm_allocator.py`:
- All returned strategy IDs must match the candidate set
- Each budget must be positive
- Total allocation cannot exceed deployable capital
- Each strategy stays within the per-strategy cap

`llm_sizing.py` (per-opportunity):
- Size must be numeric, finite, and positive
- Size is clamped to `max_size` ceiling
- Size is quantized to integer contracts (Kalshi requirement)
- Minimum size is 1
- Rate limiter enforces minimum 5s gap between LLM calls (free-tier safe)

### Existing risk controls stay in place

The LLM cannot bypass runtime safety controls. Orders still flow through the existing `RiskManager` and trader checks.

## CLI Toggles

```bash
# Portfolio review only
coinjure engine paper-run --all-relations --llm-portfolio-review

# Per-opportunity trade sizing only
coinjure engine paper-run --all-relations --llm-trade-sizing

# Both layers
coinjure engine paper-run --all-relations --llm-portfolio-review --llm-trade-sizing

# With custom model
coinjure engine paper-run --all-relations --llm-trade-sizing --llm-model gemini-3.1-flash-lite-preview

# Backtest with LLM sizing
coinjure strategy backtest --all-relations --llm-trade-sizing --llm-model gpt-4.1-mini
```

All three commands (`paper-run`, `live-run`, `backtest`) support `--llm-portfolio-review`, `--llm-trade-sizing`, and `--llm-model`.

## Modules

| Module | Purpose |
|---|---|
| `coinjure/trading/llm_allocator.py` | Launch-time LLM portfolio budget review |
| `coinjure/trading/llm_sizing.py` | Per-opportunity LLM sizing + rate limiter |
| `coinjure/trading/sizing.py` | Trade sizing router (`compute_trade_size_with_llm`) |
| `coinjure/cli/engine_commands.py` | CLI wiring for LLM toggles |

## Tests

| Test File | Tests | Coverage |
|---|---|---|
| `tests/test_llm_allocator.py` | 7 | Portfolio allocation validation and fallbacks |
| `tests/test_llm_sizing.py` | 18 | Launch-time sizing (7) + per-opportunity sizing (8) + trade size router (3) |

## Non-Goals

- LLM-driven trade entry or exit decisions (LLM only sizes, never triggers)
- LLM-driven replacement of builtin arbitrage detection logic
- Periodic portfolio rebalancing during trading (currently launch-time only)

## Rationale

- Quant code handles the hot path — LLM is consulted only when opted in
- Rate limiting protects against API cost and latency in the event loop
- Failures degrade to deterministic quant behavior instead of blocking trading
- Strategy refs and kwargs remain serializable in the registry
- Both layers are independently toggleable for different use cases (fast simple arb = quant only, complex arb = LLM sizing)
