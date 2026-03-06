---
name: pm-agent-strategy-authoring
description: 用于把 LLM/工具驱动的想法实现为 AgentStrategy 代码，并通过 paper trading 评估效果。
---

# PM Agent Strategy Authoring

当用户要求"写一个用 LLM / MCP 工具 / 外部 API 做决策的策略"时，使用这个技能。

## 目标

- 产出可运行的策略类（`AgentStrategy` 子类）
- 保证 `strategy validate` 与 `paper run` 可执行
- **不**做参数网格搜索（agent 策略不可 batch-tune）

## 代码入口

- 基类契约：`coinjure/strategy/agent_strategy.py` (`AgentStrategy`)
- LLM 策略参考：`coinjure/strategy/simple_strategy.py`
- 新策略目录：`strategies/`

## 实施流程

1. 实现策略（直接新建文件，无需骨架生成命令）

```python
from coinjure.strategy.agent_strategy import AgentStrategy

class MyAgentStrategy(AgentStrategy):
    def __init__(self, trade_size: float = 10.0) -> None:
        super().__init__()
        self.trade_size = Decimal(str(trade_size))

    async def process_event(self, event: Event, trader: Trader) -> None:
        if self.is_paused():
            return
        # 调用 LLM / 外部 API ...
        self.record_decision(reasoning=llm_output)
```

2. 快速验证

```bash
coinjure strategy validate \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --dry-run --events 10 --json
```

3. Paper trading 评估（agent 策略的正确评估方式）

```bash
coinjure paper run \
  --exchange polymarket \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --monitor
```

4. 观察运行状态

```bash
coinjure trade status --json       # 基本状态
coinjure trade state --json        # 完整快照（持仓、决策、订单）
```

5. 干预与调整

```bash
coinjure trade pause --json
coinjure trade resume --json
coinjure trade swap \
  --strategy-ref strategies/<new>.py:<NewClass> \
  --strategy-kwargs-json '<json>' \
  --json
```

## Hard Rules

- `AgentStrategy` 子类不可用于 `research batch-markets`（非确定性，结果无意义）。
- 外部 API 调用必须做好超时与错误处理，避免阻塞 asyncio 事件循环。
- 必须通过 `self.is_paused()` 检查再决策，尊重控制平面暂停信号。
- 不使用未来信息（禁止 look-ahead）。
- 必须可复现路径：策略文件路径、类名、命令都要明确记录。
