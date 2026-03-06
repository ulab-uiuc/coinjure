---
name: pm-autonomous-loop
description: Use this skill to run the full autonomous strategy discovery loop — orient, remember, hypothesize, code, test, gate, promote, harvest, iterate.
---

# PM Autonomous Strategy Discovery Loop

This skill orchestrates the complete strategy discovery workflow. Each iteration builds on the experiment ledger so knowledge accumulates across sessions.

## Loop Steps

### 1. Orient — understand current situation

```bash
coinjure market snapshot --exchange polymarket --json
```

This returns active portfolio, memory summary, and top past experiments in one call.

### 2. Remember — review past experiments

```bash
coinjure memory best --metric total_pnl --top 5 --json
coinjure memory summary --json
```

Avoid repeating strategies/markets that already failed. Focus on unexplored approaches or markets with edge.

### 3. Hypothesize — generate trade ideas

Use the `pm-hypothesis-discovery` skill to produce 3-10 hypotheses. Write them to `data/research/<run_id>/hypotheses.jsonl`.

### 4. Code — implement strategy

Use `pm-quant-strategy-authoring` for quantitative strategies or `pm-agent-strategy-authoring` for LLM-driven ones.

```bash
coinjure strategy create --output strategies/<name>.py --class-name <ClassName>
coinjure strategy validate --strategy-ref strategies/<name>.py:<ClassName> --strategy-kwargs-json '<json>' --dry-run --events 10 --json
```

### 5. Test — backtest with gating

Use the `pm-backtest-ops` fast path. Results auto-record to the experiment ledger.

```bash
coinjure strategy alpha-pipeline \
  --history-file data/backtest_data.jsonl \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --market-rank 1 --market-sort-by points \
  --artifacts-dir data/research/<run_id> \
  --json
```

### 6. Decide — gate check

- **Gate PASSED**: proceed to step 7 (promote).
- **Gate FAILED**: record learnings, loop back to step 3 with adjusted hypothesis. Use `--tag failed` when adding notes.

```bash
coinjure memory add --run-id <id> --strategy-ref <ref> --notes "Failed: <reason>" --tag failed --json
```

### 7. Promote — paper trade

```bash
coinjure engine add --strategy-id <id> --strategy-ref <ref> --kwargs-json '<json>' --json
coinjure engine promote --strategy-id <id> --to paper_trading --json
```

### 8. Monitor & Harvest — collect paper performance

```bash
coinjure engine status -s ~/.coinjure/<id>.sock --json
coinjure engine feedback --strategy-id <id> --json
```

### 9. Compare — backtest vs. paper reality

```bash
coinjure engine feedback --strategy-id <id> --json
```

If paper significantly underperforms backtest, investigate slippage, market conditions, or overfitting.

### 10. Iterate — next cycle

Return to step 1. The ledger now has more data points. Use this to:

- Avoid strategies that don't generalize from backtest to paper
- Double down on market/strategy combinations that show real edge
- Refine parameters based on feedback gap analysis

## Hard Rules

- Never skip the gate check.
- Never promote to live without paper evidence AND explicit user approval.
- Always use `--json` for machine-readable output.
- Record every experiment — successes and failures — to the ledger.
- Check memory at the start of every session to build on prior work.
