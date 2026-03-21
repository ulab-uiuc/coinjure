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
build strategy kwargs (incl. llm_trade_sizing, llm_portfolio_review, llm_model) and launch engines
   |
   v
TradingEngine event loop (engine.py)
   |
   +--> data_source.get_next_event()
   |
   +--> strategy.process_event(event, trader)
   |      |
   |      +--> detect opportunity (edge > threshold)
   |      |
   |      +--> await compute_trade_size_with_llm(pm, edge, leg_count, leg_prices, ...)
   |               |
   |               +--> compute_trade_size()  [always runs first — quant baseline]
   |               |
   |               +--> if llm_trade_sizing=False: return quant_size  [fast path]
   |               |
   |               +--> if llm_trade_sizing=True:
   |               |       +--> rate limiter check (configurable, disabled by default)
   |               |       +--> build OpportunitySizingRequest with real portfolio state
   |               |       |       includes leg_count and leg_prices for multi-leg context
   |               |       +--> await compute_opportunity_sizing_llm(request)
   |               |       +--> validate, clamp, quantize response
   |               |       +--> return llm_size (or quant_size on failure/None)
   |               |
   |               v
   |            trader.place_order(side, ticker, price, size)
   |
   +--> every 500 events: _check_llm_portfolio_review()
            |
            +--> if llm_portfolio_review=False: skip
            |
            +--> if llm_portfolio_review=True:
                    +--> await review_portfolio_llm(strategy state snapshot)
                    +--> validate response (kelly_fraction in [0.01, 0.5], max_trade_size >= 1)
                    +--> apply adjustments to strategy.kelly_fraction / strategy.max_trade_size
                    +--> fallback: no changes on error
```

## Three LLM Control Points

### 1. Portfolio Allocation (launch-time)

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
- Receives real context: edge, available capital, current exposure, portfolio utilization, quant baseline size, leg_count, leg_prices
- Rate-limited (configurable interval, disabled by default; set `_OPPORTUNITY_MIN_INTERVAL_SECONDS` or use API to adjust)
- Fallback: returns quant size on rate-limit skip, API failure, invalid response, or None

### 3. Periodic Portfolio Review (runtime)

- Module: `coinjure/trading/llm_allocator.py`
- Function: `review_portfolio_llm()`
- Dataclass: `PortfolioAdjustment` (kelly_fraction, max_trade_size, reasoning)
- Triggered every 500 events in the engine event loop via `_check_llm_portfolio_review()`
- Receives runtime snapshot: available capital, exposure, realized/unrealized PnL, position/trade counts, current sizing parameters
- Can adjust `kelly_fraction` (range [0.01, 0.5]) and `max_trade_size` (>= 1) on the live strategy instance
- Null fields mean "keep current value" — conservative by default
- Fallback: no adjustments on any error

All three features are **off by default**.

## Strategy Integration

All 7 builtin strategies call `await compute_trade_size_with_llm()` at every trade decision point (11 call sites total):

| Strategy | Call Sites | Relation Type |
|---|---|---|
| `DirectArbStrategy` | 1 | `same_event` |
| `GroupArbStrategy` | 1 | `same_event` |
| `ImplicationArbStrategy` | 1 | `implication` |
| `ConditionalArbStrategy` | 2 | `conditional` |
| `StructuralArbStrategy` | 2 | `structural` |
| `CointSpreadStrategy` | 2 | `cointegrated` |
| `LeadLagStrategy` | 2 | `lead_lag` |

Each strategy accepts `llm_trade_sizing: bool`, `llm_portfolio_review: bool`, and `llm_model: str | None` as constructor kwargs. When `llm_trade_sizing=False` (default), the async router immediately returns the quant size with zero overhead. When `llm_portfolio_review=False` (default), the engine skips periodic review entirely.

## Safety Model

### Quant remains the baseline

- `allocate_capital()` runs first for portfolio allocation
- `compute_trade_size()` always runs first for trade sizing
- LLM review is optional and additive — never blocks the quant path

### Fallback behavior

| Failure Mode | Behavior |
|---|---|
| Portfolio allocation API error | Use baseline quant budgets |
| Portfolio allocation invalid output | Use baseline quant budgets |
| Per-opportunity API error | Return quant size |
| Per-opportunity invalid size | Return quant size |
| Per-opportunity rate-limited | Return quant size (no API call) |
| LLM returns size > max_size | Clamp to max_size |
| LLM returns size < 1 after rounding | Clamp to 1 |
| Portfolio review API error | No adjustments applied |
| Portfolio review invalid kelly/max_size | No adjustments applied |
| Portfolio review null fields | Keep current values (no change) |

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
- Rate limiter interface is preserved but disabled by default (interval = 0.0); operators can re-enable via `set_interval()`

### Existing risk controls stay in place

The LLM cannot bypass runtime safety controls. Orders still flow through the existing `RiskManager` and trader checks.

## CLI Toggles

```bash
# Launch-time portfolio allocation review only
coinjure engine paper-run --all-relations --llm-portfolio-review

# Per-opportunity trade sizing only
coinjure engine paper-run --all-relations --llm-trade-sizing

# Runtime periodic portfolio review (adjusts kelly_fraction / max_trade_size during trading)
coinjure engine paper-run --all-relations --llm-portfolio-review --llm-trade-sizing

# All three layers with custom model
coinjure engine paper-run --all-relations --llm-portfolio-review --llm-trade-sizing --llm-model gemini-3.1-flash-lite-preview

# Backtest with LLM sizing
coinjure engine backtest --all-relations --llm-trade-sizing --llm-model gpt-4.1-mini
```

All three commands (`paper-run`, `live-run`, `backtest`) support `--llm-portfolio-review`, `--llm-trade-sizing`, and `--llm-model`.

## Modules

| Module | Purpose |
|---|---|
| `coinjure/trading/llm_allocator.py` | Launch-time portfolio allocation + runtime periodic portfolio review |
| `coinjure/trading/llm_sizing.py` | Per-opportunity LLM sizing + rate limiter |
| `coinjure/trading/sizing.py` | Trade sizing router (`compute_trade_size_with_llm`) |
| `coinjure/engine/engine.py` | Periodic `_check_llm_portfolio_review()` hook |
| `coinjure/cli/engine_commands.py` | CLI wiring for LLM toggles |

## Tests

| Test File | Tests | Coverage |
|---|---|---|
| `tests/test_llm_allocator.py` | 11 | Portfolio allocation validation/fallbacks (7) + runtime portfolio review (4) |
| `tests/test_llm_sizing.py` | 13 | Per-opportunity sizing (8) + trade size router (3) + leg context serialization (2) |

## Known Limitations

- **Live budget enforcement**: `--llm-portfolio-review` computes adjusted budgets but live mode does not enforce them (each live runner loads full exchange balance independently). Portfolio review is effective in paper/backtest only until live budget plumbing is added.
- **Per-process throttling**: Rate limiting is per-strategy-process, not portfolio-wide.

## Non-Goals

- LLM-driven trade entry or exit decisions (LLM only sizes, never triggers)
- LLM-driven replacement of builtin arbitrage detection logic

## Rationale

- Quant code handles the hot path — LLM is consulted only when opted in
- Rate limiting protects against API cost and latency in the event loop
- Failures degrade to deterministic quant behavior instead of blocking trading
- Strategy refs and kwargs remain serializable in the registry
- Both layers are independently toggleable for different use cases (fast simple arb = quant only, complex arb = LLM sizing)
