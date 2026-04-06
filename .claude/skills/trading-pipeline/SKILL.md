---
name: trading-pipeline
description: Use when the user asks to run the full trading workflow end-to-end, from finding markets to paper trading — covers discover, backtest, and paper-run in sequence.
---

# Trading Pipeline

**discover → backtest → paper-run**. Each phase gates the next. Be terse — do the work, report ONE summary table at the end.

## Execution Style

- **Batch everything.** Run discovers in parallel, add relations in parallel, check statuses in parallel.
- **Don't narrate.** Skip analysis monologues. The user can read the table output.
- **One summary at the end.** Don't report after each sub-step. Deliver a single final summary with: relations found, backtest results, deploy status.
- **Skip redundant checks.** Don't `relations list` right after adding — you already know what you added.

## Phase 1: Discover & Add Relations

### Step 1: News → keywords (optional, skip if user provides topic)

```bash
coinjure market news --limit 10
```

Extract 2-4 keywords. Move on immediately.

### Step 2: Parallel discovers

Run 2-4 `discover` commands **in a single parallel batch**:

```bash
# Run ALL of these in parallel — never sequentially
coinjure market discover -q "keyword1" --exchange polymarket --limit 40 --auto-discover
coinjure market discover -q "keyword2" --exchange polymarket --limit 40 --auto-discover
coinjure market discover -q "keyword3" --exchange polymarket --limit 40
```

Use `--auto-discover` to auto-create intra-event relations (implication, exclusivity, complementary). **Do NOT use `--json`** — read the table to spot cross-event relations.

### Step 3: Add relations in parallel

After scanning all discover results, add all identified relations **in a single parallel batch**:

```bash
# Run ALL adds in parallel
coinjure market relations add -m <id1> -m <id2> --spread-type <type> --hypothesis "<constraint>" --reasoning "why"
coinjure market relations add -m <id3> -m <id4> --spread-type <type> --hypothesis "<constraint>" --reasoning "why"
# ... etc
```

**Relation types** (each auto-selects a builtin strategy):

| Type            | Strategy               | Hypothesis                                 |
| --------------- | ---------------------- | ------------------------------------------ |
| `implication`   | ImplicationArbStrategy | A <= B (e.g. "by March" implies "by June") |
| `exclusivity`   | GroupArbStrategy       | sum <= 1 (mutually exclusive outcomes)     |
| `complementary` | GroupArbStrategy       | sum == 1 (all outcomes in one event)       |
| `same_event`    | DirectArbStrategy      | A ~= B (same question, two platforms)      |
| `correlated`    | CointSpreadStrategy    | spread mean-reverts                        |
| `structural`    | StructuralArbStrategy  | f(A) = g(B)                                |
| `conditional`   | ConditionalArbStrategy | A\|B bounded                               |
| `temporal`      | LeadLagStrategy        | lead-lag                                   |

**Quick pattern recognition** — look for these in discover output:

- Same question, nested dates → `implication` (earlier ≤ later)
- Same underlying, nested thresholds → `implication` (harder ≤ easier)
- Same topic, different angles, same timeframe → `correlated`
- Same question across exchanges → `same_event`

**Skip markets** with bid/ask at 0.99/1.00 or 0.00/0.01 — already resolved.

## Phase 2: Backtest (one command)

```bash
coinjure engine backtest --all-relations --json
```

Results auto-update relation status. Don't stop to discuss failures — they're filtered out by `--all-relations` in the next phase.

## Phase 3: Paper-Run

```bash
# Stop any old hub, start fresh, disable killswitch, deploy — chain it
coinjure hub stop 2>&1; coinjure hub start --detach --json       # or --demo for synthetic data
coinjure engine killswitch --off
coinjure engine paper-run --all-relations --detach --json
```

Use `--demo` on hub start when user wants synthetic/demo data.

After deploy, wait ~8s then check all statuses **in parallel**:

```bash
coinjure engine status --id <id1> --json
coinjure engine status --id <id2> --json
# ... etc
```

## Final Summary Format

Deliver ONE table at the end:

```
| Relation | Type | Backtest PnL | Trades | Paper Status | Portfolio |
|----------|------|-------------|--------|--------------|-----------|
| ...      | ...  | ...         | ...    | ...          | ...       |
```

Plus monitoring commands for the user.

## Gate Rules

| Gate                | Requirement                  |
| ------------------- | ---------------------------- |
| Discover → Backtest | At least 1 relation exists   |
| Backtest → Paper    | At least 1 `backtest_passed` |
| Paper → Live        | Explicit user approval       |

## Hard Rules

- Never use `--json` for `market discover` — table output is needed for cross-event reasoning.
- Never skip backtest before paper-run.
- Always start hub before paper-run (unless `--no-hub`).
- `--all-relations` on paper-run only deploys `backtest_passed` relations.

## Reset (if needed)

```bash
# Remove all relations
coinjure market relations list 2>&1 \
  | grep '│' \
  | awk -F'│' '{gsub(/^[ \t]+|[ \t]+$/, "", $2); if ($2 != "" && $2 != "Relation ID") print $2}' \
  | while read id; do coinjure market relations remove "$id"; done
```
