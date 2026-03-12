# LLM Allocation Strategy — Analysis & Implementation Plan

> **Status**: Plan (no code changes made)
> **Goal**: Replace portfolio-level capital allocation, per-trade order sizing, and adjustment timing with LLM-driven decisions while preserving all 7 builtin arbitrage strategies' opportunity detection logic.

---

## Table of Contents

1. [Architecture Analysis](#1-architecture-analysis)
2. [Hidden Ambiguities](#2-hidden-ambiguities)
3. [Failure Modes & Mitigations](#3-failure-modes--mitigations)
4. [Implementation Shape](#4-implementation-shape)
5. [TDD Test Plan](#5-tdd-test-plan)
6. [Atomic Commit Strategy](#6-atomic-commit-strategy)

---

## 1. Architecture Analysis

### 1.1 Current Replacement Targets

The three capabilities to replace live in distinct, well-isolated locations:

#### A. Portfolio-Level Capital Allocation

| Aspect | Current State |
|---|---|
| **File** | `coinjure/trading/allocator.py` |
| **Function** | `allocate_capital(total_capital, candidates, *, min_budget, max_budget_pct, reserve_pct)` |
| **Algorithm** | Linear PnL-weighted: profitable strategies get shares proportional to `backtest_pnl`; unprofitable get `min_budget`; clamped by `max_budget_pct` |
| **Callers** | (1) `_run_batch()` in `engine_commands.py` (batch deploy), (2) CLI `engine allocate` command |
| **Data model** | `AllocationCandidate(strategy_id, backtest_pnl)` — minimal, only PnL |

**Key observation**: The current allocator uses a single signal (backtest PnL). An LLM allocator can consume richer context: live PnL, drawdown, correlation, market conditions, news sentiment.

#### B. Per-Trade Order Sizing

| Aspect | Current State |
|---|---|
| **File** | `coinjure/trading/sizing.py` |
| **Function** | `compute_trade_size(position_manager, edge, *, kelly_fraction, edge_cap, min_size, max_size)` |
| **Algorithm** | Conservative Kelly: `available_cash × kelly_fraction × min(edge/edge_cap, 1)`, clamped to `[min_size, max_size]` |
| **Callers** | `DirectArbStrategy.process_event()`, `GroupArbStrategy.process_event()` (2 of 7 strategies) |

**Critical discovery — TWO sizing patterns exist**:

| Pattern | Strategies | Mechanism |
|---|---|---|
| `compute_trade_size()` (Kelly) | `DirectArbStrategy`, `GroupArbStrategy` | Dynamic, edge-weighted |
| Fixed `self.trade_size` | `ImplicationArbStrategy`, `StructuralArbStrategy`, `ConditionalArbStrategy`, `CointSpreadStrategy`, `LeadLagStrategy` | Static constructor param (default `Decimal('25')`) |

The 5 fixed-size strategies completely bypass `compute_trade_size()`. Any LLM sizing replacement must handle **both** patterns.

#### C. Adjustment/Rebalancing Timing

| Aspect | Current State |
|---|---|
| **Location** | Spread across individual strategies' `process_event()` methods |
| **Mechanisms** | `_cooldown_until` (per-strategy), warmup periods, event-driven (every price change triggers evaluation) |
| **Control surface** | CLI `pause`/`resume` per strategy; `TradingEngine` auto-degrade on health failure |

**Key observation**: There is no centralized timing controller. Each strategy owns its own cadence. The LLM timing layer must operate **above** individual strategies, deciding when to allow/suppress portfolio-wide rebalancing.

### 1.2 What Must NOT Change

- **7 builtin strategies** — `process_event()` logic that detects arbitrage edges (spread deviations, constraint violations, cross-platform gaps). These are the alpha generators.
- **RiskManager hierarchy** — Pre-trade checks (`StandardRiskManager` has 6 checks: max position, max drawdown, max order, cooldown, exposure, market hours). LLM decisions must pass through risk checks, not bypass them.
- **PositionManager** — Cash/position tracking is source of truth.
- **TradingEngine event loop** — `_process_one_event()` → `strategy.process_event()` → trade flow.
- **StrategyRegistry** — Lifecycle management, PnL tracking, socket mapping.
- **Trader ABC** — `place_order()` interface, kill-switch, read-only mode.

### 1.3 Existing LLM Infrastructure

The repo already has a mature `AgentStrategy` pattern:

| Component | Location | Details |
|---|---|---|
| `AgentStrategy` base class | `coinjure/strategy/agent.py` | Extends `Strategy`, adds OpenAI Agents SDK integration |
| LLM provider | `openai-agents` package | Conditional import, already in deps |
| Default model | `gpt-4.1-mini` | Via `get_agent_model()` |
| Tool-building pattern | `build_openai_tools()` | 7 `@function_tool` functions with `StrategyContext` closure |
| Prompt engineering | `build_prompt_context()` | Market state, positions, news → structured text |
| Agent execution | `run_openai_agent()` | `Runner.run(agent, input, max_turns=8)` |
| Test mocking | `tests/test_agent_strategy_sdk.py` | Clean SDK mock pattern with `unittest.mock` |

**This is the pattern to extend.** The LLM allocator should follow the same `AgentStrategy` / `@function_tool` conventions.

---

## 2. Hidden Ambiguities

### 2.1 Scope Ambiguity: Where Does the LLM Live?

**Question**: Is the LLM allocator a **single meta-strategy** that wraps all 7 builtins, or a **sidecar service** called from `_run_batch()` / CLI `allocate`?

**Recommendation**: **Sidecar module** (`coinjure/trading/llm_allocator.py`) that provides drop-in replacements for `allocate_capital()` and `compute_trade_size()`. Reasons:
- Preserves the existing call graph (callers don't change shape)
- Avoids coupling LLM lifecycle to strategy lifecycle
- Testable independently
- Can be toggled via a `--method llm` flag alongside existing `equal`/`edge`/`kelly`

### 2.2 Sizing Ambiguity: Unify or Fork?

The codebase has two sizing patterns. Options:

| Option | Change scope | Risk |
|---|---|---|
| A. Only replace `compute_trade_size()` | 2 strategies affected | 5 strategies still use fixed sizes — LLM has no influence over them |
| B. Also inject sizing into the 5 fixed-size strategies | 7 strategies affected | Requires modifying each strategy's `process_event()` to call a sizing function instead of using `self.trade_size` |
| C. Add a sizing hook to the base `Strategy` class | 1 class + 5 strategies | Cleanest long-term, but larger refactor |

**Recommendation**: **Option B** — add `compute_trade_size()` calls to the 5 fixed-size strategies. This is a small, mechanical change (replace `self.trade_size` with a function call) and brings all 7 strategies under unified LLM sizing control. The existing Kelly implementation becomes the default fallback.

### 2.3 Timing Ambiguity: What "Timing" Means

"Adjustment timing" could mean:
1. **Trade-level**: Should this specific trade execute now or wait? (Per-event decision)
2. **Rebalance-level**: Should the portfolio rebalance allocations now? (Periodic decision)
3. **Strategy-level**: Should this strategy be active/paused right now? (Lifecycle decision)

**Recommendation**: Implement **level 2 (rebalance timing)** first. The LLM decides *when* to re-run `allocate_capital()` and *whether* to adjust budgets, using portfolio state + market conditions. Level 1 is too latency-sensitive for an LLM call. Level 3 already exists via CLI `pause`/`resume`.

### 2.4 Latency Ambiguity

LLM calls take 1-5 seconds. The engine event loop processes events synchronously. If sizing calls the LLM per-trade, it blocks the event loop.

**Recommendation**: 
- **Allocation + timing**: Async, off the hot path. LLM runs periodically or on-demand, produces a budget table that strategies read.
- **Sizing**: **Pre-computed budget envelope**, not per-trade LLM call. The LLM sets `(min_size, max_size, kelly_fraction)` per strategy during allocation. `compute_trade_size()` uses these params at trade time (no LLM call in the hot path).

### 2.5 Fallback Ambiguity

What happens when the LLM is unavailable (API down, rate limit, timeout)?

**Recommendation**: Explicit fallback chain:
1. Try LLM allocation
2. On failure → fall back to current PnL-weighted allocation
3. Log warning + emit metric
4. Never block trading on LLM availability

### 2.6 Data Staleness

The allocator currently receives `backtest_pnl` only. The LLM needs richer data, but some of it (live PnL, positions) changes between allocation calls.

**Recommendation**: Build an `AllocationSnapshot` dataclass that freezes all LLM-relevant data at decision time. This becomes the LLM's input and the audit trail.

---

## 3. Failure Modes & Mitigations

### 3.1 LLM Hallucination → Invalid Allocations

| Risk | The LLM returns allocations that don't sum correctly, reference non-existent strategies, or violate constraints |
|---|---|
| **Likelihood** | Medium (tool-use models are reliable but not infallible) |
| **Mitigation** | Post-LLM validation layer: (1) verify all strategy IDs exist in registry, (2) verify sum ≤ deployable capital, (3) verify per-strategy ≤ max_budget_pct, (4) reject and fall back to heuristic on any violation |

### 3.2 LLM Latency Spikes → Stale Allocations

| Risk | LLM takes 10+ seconds, market moves, allocation is stale |
|---|---|
| **Likelihood** | Medium |
| **Mitigation** | (1) Timeout at 30s, (2) allocation has a `computed_at` timestamp, (3) strategies check freshness, (4) stale allocations use previous-valid allocation |

### 3.3 Adversarial Prompt Injection via Market Data

| Risk | Market event text (news headlines, market descriptions) contains prompt injection |
|---|---|
| **Likelihood** | Low but non-zero (prediction market titles are user-generated) |
| **Mitigation** | (1) Structured tool output only (no raw text in system prompt), (2) LLM receives sanitized numerical data, (3) news text truncated and escaped |

### 3.4 Concentration Risk from LLM Overconfidence

| Risk | LLM consistently over-allocates to one strategy that appears profitable |
|---|---|
| **Likelihood** | High (recency bias is a known LLM weakness) |
| **Mitigation** | Hard constraints enforced *after* LLM decision: `max_budget_pct` cap (existing), minimum diversification (new: no strategy > 40%, at least 3 strategies funded), correlation penalty |

### 3.5 Runaway Sizing

| Risk | LLM-set sizing parameters produce unexpectedly large trades |
|---|---|
| **Likelihood** | Medium |
| **Mitigation** | `RiskManager` already enforces `max_order_size` and `max_position_size`. These are hard caps independent of sizing. Additionally, the `max_size` param in `compute_trade_size()` provides a per-trade ceiling. |

### 3.6 Cost Explosion

| Risk | LLM called too frequently → high API costs |
|---|---|
| **Likelihood** | Medium (if sizing calls LLM per-trade) |
| **Mitigation** | Architecture ensures LLM is only called at allocation/rebalance time (minutes/hours cadence), never per-trade. Per-trade sizing uses pre-computed parameters. Estimated cost: ~$0.01-0.05 per allocation call with `gpt-4.1-mini`. |

---

## 4. Implementation Shape

### 4.1 New Files

```
coinjure/trading/llm_allocator.py    # Core LLM allocation logic
tests/test_llm_allocator.py          # Unit tests
```

### 4.2 Modified Files

```
coinjure/trading/allocator.py        # Add method dispatch (keep existing as default)
coinjure/trading/sizing.py           # Accept LLM-provided params override
coinjure/cli/engine_commands.py      # Add --method llm to allocate command
coinjure/strategy/builtin/implication_arb_strategy.py  # Replace fixed trade_size
coinjure/strategy/builtin/structural_arb_strategy.py   # Replace fixed trade_size
coinjure/strategy/builtin/conditional_arb_strategy.py  # Replace fixed trade_size
coinjure/strategy/builtin/coint_spread_strategy.py     # Replace fixed trade_size
coinjure/strategy/builtin/lead_lag_strategy.py         # Replace fixed trade_size
```

### 4.3 Architecture Diagram

```
                    ┌───────────────────────────────────┐
                    │       LLM Allocation Agent         │
                    │   (coinjure/trading/llm_allocator) │
                    │                                    │
                    │  ┌──────────┐  ┌───────────────┐  │
                    │  │  OpenAI   │  │  @function_tool│  │
                    │  │  Agent    │──│  get_strategies│  │
                    │  │  (gpt-4.1 │  │  get_pnl      │  │
                    │  │   -mini)  │  │  get_positions │  │
                    │  └──────────┘  │  get_drawdown  │  │
                    │                │  get_correlation│  │
                    │                └───────────────┘  │
                    └──────────┬───────────────────────┘
                               │
                    Returns: AllocationResult
                    {budgets: {sid→$}, sizing_params: {sid→{kelly, min, max}}}
                               │
              ┌────────────────┼────────────────────┐
              ▼                ▼                     ▼
   allocate_capital()   compute_trade_size()   rebalance_check()
   (dispatch: llm|edge) (uses LLM params       (timing: should we
                         if available,          re-allocate now?)
                         else Kelly default)
              │                │
              ▼                ▼
   _run_batch() / CLI    Strategy.process_event()
                         (all 7 strategies)
                               │
                               ▼
                         RiskManager.check()
                         (unchanged — hard caps)
                               │
                               ▼
                         Trader.place_order()
```

### 4.4 Core Data Model

```python
@dataclass
class AllocationSnapshot:
    """Frozen state at decision time — LLM input + audit trail."""
    timestamp: datetime
    total_capital: Decimal
    strategies: list[StrategySnapshot]  # id, backtest_pnl, live_pnl, positions, drawdown
    market_conditions: dict[str, Any]   # volatility, correlation matrix, news summary
    
@dataclass
class StrategySnapshot:
    strategy_id: str
    backtest_pnl: float
    live_pnl: float
    current_positions: list[dict]
    max_drawdown: float
    win_rate: float
    recent_trades: int
    
@dataclass  
class AllocationResult:
    """LLM output — validated before use."""
    budgets: dict[str, Decimal]           # strategy_id → capital budget
    sizing_params: dict[str, SizingParams] # strategy_id → per-trade params
    reasoning: str                         # LLM's explanation (audit log)
    computed_at: datetime
    model: str                             # which model produced this
    fallback_used: bool                    # True if LLM failed → heuristic

@dataclass
class SizingParams:
    kelly_fraction: Decimal   # 0.05 - 0.25 range
    min_size: Decimal         # floor
    max_size: Decimal         # ceiling per trade
```

### 4.5 Key Functions

#### `llm_allocate_capital()` — Drop-in replacement

```python
async def llm_allocate_capital(
    total_capital: Decimal,
    candidates: list[AllocationCandidate],
    *,
    registry: StrategyRegistry | None = None,
    position_manager: PositionManager | None = None,
    # Same defaults as allocate_capital() for fallback
    min_budget: Decimal = Decimal('10'),
    max_budget_pct: Decimal = Decimal('0.4'),
    reserve_pct: Decimal = Decimal('0.1'),
) -> AllocationResult:
    """LLM-driven allocation with automatic fallback."""
```

Flow:
1. Build `AllocationSnapshot` from registry + position manager
2. Create OpenAI Agent with `@function_tool` tools for querying portfolio state
3. Run agent with structured prompt: "Given this portfolio state, allocate capital"
4. Parse structured output → `AllocationResult`
5. **Validate**: IDs exist, sums correct, constraints met
6. On any failure → fall back to `allocate_capital()` (existing heuristic)

#### `get_sizing_params()` — Parameter lookup

```python
def get_sizing_params(
    strategy_id: str,
    allocation_result: AllocationResult | None = None,
) -> SizingParams | None:
    """Look up LLM-provided sizing params for a strategy."""
```

Not an LLM call — just reads the pre-computed `AllocationResult`.

### 4.6 Integration Points

#### CLI `engine allocate --method llm`

Add `llm` as a third method alongside existing `equal`/`edge`/`kelly`:

```python
@click.option('--method', type=click.Choice(['equal', 'edge', 'kelly', 'llm']), default='edge')
```

When `--method llm`:
1. Call `llm_allocate_capital()` instead of `allocate_capital()`
2. Store `AllocationResult` in registry metadata for strategies to read
3. Display LLM reasoning in output

#### `_run_batch()` Integration

In `_run_batch()`, replace:
```python
budgets = allocate_capital(Decimal(initial_capital), candidates)
```
With method dispatch:
```python
if method == 'llm':
    result = await llm_allocate_capital(Decimal(initial_capital), candidates, registry=reg)
    budgets = result.budgets
else:
    budgets = allocate_capital(Decimal(initial_capital), candidates)
```

#### Strategy Sizing Unification

For the 5 fixed-size strategies, replace:
```python
self.trade_size = Decimal(str(trade_size))
# ... later in process_event():
size = self.trade_size
```
With:
```python
self.default_trade_size = Decimal(str(trade_size))
# ... later in process_event():
size = compute_trade_size(
    self.require_context().position_manager, 
    edge,
    min_size=self.default_trade_size,  # use fixed size as floor
    max_size=self.default_trade_size * 4,  # reasonable ceiling
)
```

This is a mechanical change per strategy. The existing `compute_trade_size()` function already handles all the Kelly math. The LLM influence comes from `AllocationResult.sizing_params` overriding the defaults.

### 4.7 Prompt Design (Sketch)

```
You are a portfolio allocation agent for Coinjure, a prediction market trading system.

You manage capital allocation across {n} arbitrage strategies. Each strategy detects 
specific market inefficiencies — you do NOT decide what to trade, only HOW MUCH capital 
each strategy receives and what sizing parameters to use.

## Decision Inputs
Use the provided tools to inspect:
- Strategy performance (backtest + live PnL, win rate, drawdown)
- Current positions and exposure
- Market conditions (volatility, correlation)

## Decision Outputs  
Return a JSON object with:
- budgets: {strategy_id: dollar_amount} — must sum to ≤ {deployable_capital}
- sizing_params: {strategy_id: {kelly_fraction, min_size, max_size}}
- reasoning: brief explanation of your allocation logic

## Hard Constraints (ENFORCED — violations will be rejected)
- No strategy > {max_budget_pct}% of deployable capital
- At least 3 strategies must receive funding (if 3+ available)  
- Total allocated ≤ deployable capital (= total - {reserve_pct}% reserve)
- kelly_fraction must be in [0.05, 0.25]
- min_size ≥ 1, max_size ≤ 200

## Guidelines
- Favor strategies with consistent positive PnL over volatile high-PnL strategies
- Reduce allocation to strategies in drawdown
- Consider correlation: don't over-allocate to correlated strategies
- When uncertain, allocate more evenly (closer to equal-weight)
```

---

## 5. TDD Test Plan

### 5.1 Test File: `tests/test_llm_allocator.py`

Tests use the mocking pattern from `tests/test_agent_strategy_sdk.py` — mock the OpenAI Agents SDK, control LLM output, verify behavior.

#### Unit Tests — Validation Layer

| # | Test Name | Description | Input | Expected |
|---|---|---|---|---|
| 1 | `test_valid_allocation_accepted` | Well-formed LLM output passes validation | Valid budgets + sizing | `AllocationResult` with `fallback_used=False` |
| 2 | `test_unknown_strategy_id_rejected` | LLM references non-existent strategy | Budget for `"fake-strategy"` | Falls back to heuristic, `fallback_used=True` |
| 3 | `test_over_budget_rejected` | Budgets sum exceeds deployable capital | Sum > total × (1 - reserve) | Falls back to heuristic |
| 4 | `test_single_strategy_cap_enforced` | One strategy > max_budget_pct | Strategy at 60% | Falls back to heuristic |
| 5 | `test_insufficient_diversification_rejected` | Fewer than 3 strategies funded (when 3+ available) | 2 of 5 funded | Falls back to heuristic |
| 6 | `test_kelly_fraction_out_of_range` | kelly_fraction outside [0.05, 0.25] | `kelly_fraction=0.5` | Clamped or rejected |
| 7 | `test_negative_budget_rejected` | Negative allocation | `-100` for a strategy | Falls back to heuristic |

#### Unit Tests — Fallback Behavior

| # | Test Name | Description | Input | Expected |
|---|---|---|---|---|
| 8 | `test_llm_timeout_falls_back` | LLM call exceeds timeout | Mock timeout | Returns heuristic allocation, `fallback_used=True` |
| 9 | `test_llm_api_error_falls_back` | LLM API returns error | Mock 500 error | Returns heuristic allocation |
| 10 | `test_llm_malformed_output_falls_back` | LLM returns unparseable output | Mock garbage text | Returns heuristic allocation |
| 11 | `test_fallback_matches_existing_allocator` | Fallback produces same result as direct `allocate_capital()` call | Same candidates | Identical budgets |

#### Unit Tests — Snapshot Building

| # | Test Name | Description | Input | Expected |
|---|---|---|---|---|
| 12 | `test_snapshot_captures_all_strategies` | AllocationSnapshot includes all registry entries | 5 strategies in registry | Snapshot has 5 `StrategySnapshot`s |
| 13 | `test_snapshot_handles_missing_pnl` | Strategy with no PnL data | New strategy, no trades | `live_pnl=0, drawdown=0, win_rate=0` |
| 14 | `test_snapshot_freezes_positions` | Positions captured at snapshot time | Positions change after snapshot | Snapshot has original values |

#### Unit Tests — Sizing Param Integration

| # | Test Name | Description | Input | Expected |
|---|---|---|---|---|
| 15 | `test_compute_trade_size_uses_llm_params` | `compute_trade_size()` respects LLM-provided overrides | `SizingParams(kelly=0.15, min=5, max=80)` | Size computed with those params |
| 16 | `test_compute_trade_size_default_without_llm` | No LLM params → existing Kelly behavior | No `AllocationResult` | Identical to current behavior |

#### Integration Tests

| # | Test Name | Description | Scope |
|---|---|---|---|
| 17 | `test_cli_allocate_method_llm` | `engine allocate --method llm` invokes LLM allocator | CLI → allocator → mock LLM |
| 18 | `test_run_batch_with_llm_allocation` | `_run_batch()` with LLM method uses LLM budgets | Batch → allocator → spawn |
| 19 | `test_fixed_size_strategies_use_compute` | The 5 converted strategies call `compute_trade_size()` | Strategy → sizing |

#### Existing Test Preservation

| # | Test Name | Description |
|---|---|---|
| 20 | `test_existing_allocator_tests_pass` | All 6 tests in `tests/test_allocator.py` still pass unchanged |
| 21 | `test_existing_agent_strategy_tests_pass` | All tests in `tests/test_agent_strategy_sdk.py` still pass unchanged |

### 5.2 Test Mocking Strategy

Follow the established pattern from `test_agent_strategy_sdk.py`:

```python
# Mock the OpenAI Agents SDK
with patch('coinjure.trading.llm_allocator._import_agents_sdk') as mock_sdk:
    mock_agent = MagicMock()
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value=MockRunResult(
        final_output=json.dumps({
            "budgets": {"strat-1": "500", "strat-2": "300", "strat-3": "200"},
            "sizing_params": {...},
            "reasoning": "Equal weight due to similar performance"
        })
    ))
    mock_sdk.return_value = (mock_agent_cls, mock_runner, mock_function_tool)
```

---

## 6. Atomic Commit Strategy

Each commit is independently testable and deployable. The system works correctly at every commit boundary.

### Commit 1: Data models and validation (Green from start)

```
feat(trading): add LLM allocation data models and validation

Files:
  + coinjure/trading/llm_allocator.py  (AllocationSnapshot, StrategySnapshot, 
                                         AllocationResult, SizingParams, 
                                         validate_allocation_result())
  + tests/test_llm_allocator.py        (tests 1-7: validation layer)
```

**What works after**: Data models importable, validation logic tested. No integration yet.

### Commit 2: Fallback and snapshot building

```
feat(trading): add LLM allocator fallback chain and snapshot builder

Files:
  ~ coinjure/trading/llm_allocator.py  (build_allocation_snapshot(), 
                                         _fallback_allocation())
  ~ tests/test_llm_allocator.py        (tests 8-14: fallback + snapshot)
```

**What works after**: Snapshot can be built from registry. Fallback produces correct heuristic allocations. Still no LLM calls.

### Commit 3: LLM agent implementation

```
feat(trading): implement LLM allocation agent with OpenAI Agents SDK

Files:
  ~ coinjure/trading/llm_allocator.py  (llm_allocate_capital(), 
                                         build_allocation_tools(), 
                                         create_allocation_agent())
  ~ tests/test_llm_allocator.py        (tests 15-16: full mock LLM flow)
```

**What works after**: `llm_allocate_capital()` callable with mocked SDK. Produces validated allocations with fallback on failure. Not yet wired to CLI.

### Commit 4: Sizing unification for 5 fixed-size strategies

```
refactor(strategy): unify sizing via compute_trade_size for all builtin strategies

Files:
  ~ coinjure/strategy/builtin/implication_arb_strategy.py
  ~ coinjure/strategy/builtin/structural_arb_strategy.py
  ~ coinjure/strategy/builtin/conditional_arb_strategy.py
  ~ coinjure/strategy/builtin/coint_spread_strategy.py
  ~ coinjure/strategy/builtin/lead_lag_strategy.py
  ~ tests/test_llm_allocator.py        (test 19: strategies use compute_trade_size)
```

**What works after**: All 7 strategies use `compute_trade_size()`. Existing behavior preserved (fixed size becomes `min_size` default). All existing tests pass.

### Commit 5: CLI and batch integration

```
feat(cli): add --method llm to engine allocate and batch commands

Files:
  ~ coinjure/cli/engine_commands.py    (--method llm option, dispatch logic)
  ~ coinjure/trading/allocator.py      (add method dispatch wrapper)
  ~ tests/test_llm_allocator.py        (tests 17-18: CLI + batch integration)
```

**What works after**: `coinjure engine allocate --method llm` works end-to-end. `_run_batch()` supports LLM allocation. Existing `--method edge`/`kelly` unchanged.

### Commit 6: Sizing param override plumbing

```
feat(trading): wire LLM sizing params into compute_trade_size

Files:
  ~ coinjure/trading/sizing.py         (accept optional SizingParams override)
  ~ tests/test_llm_allocator.py        (tests 15-16: param override verification)
  ~ tests/test_allocator.py            (verify existing tests still pass - test 20)
```

**What works after**: Full pipeline operational. LLM allocates capital AND sets sizing params. Strategies use LLM-provided params at trade time. All existing tests pass.

### Commit 7: Documentation and test finalization

```
docs: add LLM allocation strategy usage guide

Files:
  ~ docs/LLM_ALLOCATION_STRATEGY_PLAN.md  (update status to implemented)
  ~ tests/test_llm_allocator.py            (test 20-21: existing test preservation verification)
```

---

## Appendix: Risk Budget

| Risk | Probability | Impact | Mitigation Cost | Priority |
|---|---|---|---|---|
| LLM hallucination | Medium | High (wrong allocations) | Low (validation layer) | **P0** |
| Latency blocking event loop | High (if per-trade) | High | Low (pre-compute architecture) | **P0** |
| API cost explosion | Medium | Medium | Low (cadence control) | **P1** |
| Concentration from overconfidence | High | Medium | Low (hard caps) | **P1** |
| Prompt injection via market data | Low | High | Medium (sanitization) | **P2** |
| SDK breaking changes | Low | Medium | Low (conditional import) | **P3** |

## Appendix: Estimated Effort

| Commit | Effort | Complexity |
|---|---|---|
| 1. Data models + validation | 1-2 hours | Low |
| 2. Fallback + snapshot | 1-2 hours | Low |
| 3. LLM agent | 2-3 hours | Medium |
| 4. Sizing unification | 1-2 hours | Low (mechanical) |
| 5. CLI integration | 1-2 hours | Low |
| 6. Sizing param plumbing | 1 hour | Low |
| 7. Docs + test finalization | 30 min | Low |
| **Total** | **7-12 hours** | **Medium** |
