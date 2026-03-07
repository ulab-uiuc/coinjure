---
name: pm-agent-strategy-authoring
description: Use this skill to implement LLM/tool-driven ideas as AgentStrategy code and evaluate effectiveness through paper trading.
---

# PM Agent Strategy Authoring

Use this skill when the user asks to write a strategy that uses LLM / MCP tools / external APIs for decision-making.

## Goal

- Produce a runnable strategy class (`AgentStrategy` subclass)
- Ensure `strategy validate` and `engine run --mode paper` can execute
- **Do not** perform parameter grid search (agent strategies cannot be batch-tuned)

## Code Entry Points

- Base class contract: `coinjure/strategy/agent_strategy.py` (`AgentStrategy`)
- Example strategies: `examples/strategies/` (spread/arb patterns)
- New strategy directory: `strategies/`

## Implementation Workflow

1. Implement strategy (create file directly, no scaffold command needed)

```python
from coinjure.strategy.agent_strategy import AgentStrategy

class MyAgentStrategy(AgentStrategy):
    def __init__(self, trade_size: float = 10.0) -> None:
        super().__init__()
        self.trade_size = Decimal(str(trade_size))

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        # Call LLM / external API ...
        self.record_decision(reasoning=llm_output)
```

2. Quick validation

```bash
coinjure strategy validate \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --dry-run --events 10 --json
```

3. Paper trading evaluation (the correct way to evaluate agent strategies)

```bash
coinjure engine run --mode paper \
  --exchange polymarket \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --monitor
```

4. Observe running status

```bash
coinjure engine status --json        # basic status
coinjure engine status --full --json # full snapshot (positions, decisions, orders)
```

5. Intervention and adjustment

```bash
coinjure engine pause --json
coinjure engine resume --json
coinjure engine swap \
  --strategy-ref strategies/<new>.py:<NewClass> \
  --strategy-kwargs-json '<json>' \
  --json
```

## Hard Rules

- External API calls must handle timeouts and errors properly to avoid blocking the asyncio event loop.
- Must check `self.is_paused()` before making decisions to respect control plane pause signals.
- Do not use future information (no look-ahead).
- Must be reproducible: strategy file path, class name, and commands must all be explicitly recorded.
