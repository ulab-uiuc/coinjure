# Batch Paper/Live Trading Design

**Date:** 2026-03-09
**Branch:** TBD
**Status:** Approved

## Problem

`engine paper-run` and `engine live-run` only support a single strategy per invocation. To run all backtest-passed relations in parallel, users must manually spawn N processes + start the hub. This should be automated.

## Design

### Architecture

Each relation runs in its own detached OS process. A shared Hub fans out market data to all subscribers.

```
paper-run --all-relations
  ├─ Auto hub start --detach (if not running)
  ├─ spawn: engine paper-run --detach (relation A, DirectArbStrategy)
  ├─ spawn: engine paper-run --detach (relation B, EventSumArbStrategy)
  └─ Register all to StrategyRegistry, output summary
```

### Command Interface

#### `engine paper-run` extensions

New options:

- `--all-relations` — batch run all `backtest_passed` relations
- `--detach/--no-detach` — run as background process

```bash
# Batch
coinjure engine paper-run --all-relations [--initial-capital 1000]

# Single detached
coinjure engine paper-run --strategy-ref builtin:DirectArb --detach
```

`--all-relations` and `--strategy-ref` are mutually exclusive.
`--initial-capital` is per-relation (not total).

#### `engine live-run` extensions

Same pattern:

- `--all-relations` — batch run all `deployed` relations
- `--detach/--no-detach`

```bash
coinjure engine live-run --all-relations [--initial-capital 1000]
```

#### `engine promote` (new command)

```bash
coinjure engine promote <relation-id>
coinjure engine promote --all   # all paper_trading entries with pnl > 0
```

Updates relation status to `deployed` and registry lifecycle accordingly.

### Batch Orchestration Flow

1. Query `RelationStore.list(status='backtest_passed')` (paper) or `status='deployed'` (live)
2. If empty, error exit
3. Check Hub running (`HUB_SOCKET_PATH.exists()`)
   - Not running -> auto `hub start --detach`, wait for socket ready
4. For each relation:
   a. `STRATEGY_BY_RELATION[relation.spread_type]` -> strategy class
   b. Build strategy_kwargs (relation_id, market pair, etc.)
   c. Spawn detached subprocess: `coinjure engine paper-run --strategy-ref ... --strategy-kwargs-json ... --detach`
   d. Record in StrategyRegistry (strategy_id=relation.relation_id, pid, socket_path, lifecycle)
5. Output summary table

### Capital Allocation

`--initial-capital` is the per-relation capital amount. Each subprocess gets the same value. Fine-grained allocation deferred to `engine allocate` or custom allocation agents.

### Relation Lifecycle

```
active -> backtest_passed -> (paper-run) -> deployed -> (live-run) -> retired
```

Promotion from paper to deployed is manual via `engine promote`.

### Detach Pattern

Reuses hub's detach pattern:

```python
cmd = [coinjure_bin, 'engine', 'paper-run', '--strategy-ref', ..., '--no-detach']
proc = subprocess.Popen(cmd, start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)
```

## File Changes

| File                           | Change                                                                                 |
| ------------------------------ | -------------------------------------------------------------------------------------- |
| `engine_commands.py`           | `paper-run` add `--all-relations` + `--detach`; `live-run` same; new `promote` command |
| `engine/registry.py`           | `StrategyEntry` add `relation_id` field; `register_from_relation()` helper             |
| `strategy/builtin/__init__.py` | `build_strategy_for_relation(relation)` helper                                         |
| `hub_commands.py`              | No changes                                                                             |
| `market/relations.py`          | No changes                                                                             |

No new files. All extensions to existing modules.

## Test Plan

- Unit: `build_strategy_for_relation()` returns correct strategy for each spread_type
- Unit: `--all-relations` and `--strategy-ref` mutual exclusion
- Integration: mock RelationStore + mock subprocess, verify spawn count = backtest_passed count
- Integration: hub auto-start logic (socket absent -> start hub)
- Manual: `promote` command status transitions
