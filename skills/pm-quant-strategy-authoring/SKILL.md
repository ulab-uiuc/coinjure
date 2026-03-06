---
name: pm-quant-strategy-authoring
description: 用于把 agent 的量化想法实现为 Strategy 代码，并形成可调参数接口与可验证行为。
---

# PM Quant Strategy Authoring

当用户要求"让 agent 自己想策略并写代码"时，使用这个技能。

## 目标

- 产出可运行的策略类（`Strategy` 子类）
- 暴露可 JSON 序列化的构造参数（用于 `arb deploy` / `portfolio add`）
- 保证 `strategy validate` 与 `backtest` 可执行

## 代码入口

- 套利策略基类：`coinjure/strategy/strategy.py` (`Strategy`)
- 现有套利策略参考：
  - `examples/strategies/direct_arb_strategy.py` — 跨平台两腿套利
  - `examples/strategies/event_sum_arb_strategy.py` — 单平台 event-sum 套利
  - `examples/strategies/multi_leg_arb_strategy.py` — 多腿套利
- 量化策略基类：`coinjure/strategy/quant_strategy.py` (`QuantStrategy`)
- 新策略目录：`strategies/` 或 `examples/strategies/`

## 套利策略关键约束

构造函数必须只接受 JSON 序列化的基本类型（str / float / int / bool），这样才能通过
`arb deploy --strategy-kwargs-json` 或 `portfolio add --kwargs-json` 部署：

```python
class MyArbStrategy(Strategy):
    def __init__(
        self,
        event_id: str,          # 来自 arb scan-events 输出
        min_edge: float = 0.02,
        trade_size: float = 10.0,
        cooldown_seconds: int = 60,
    ) -> None: ...
```

## 实施流程

1. 参考现有套利策略实现 `process_event`

2. 快速验证

```bash
coinjure strategy validate \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --strategy-kwargs-json '<json>' \
  --dry-run --events 10 --json
```

3. 单点回测（parquet 数据）

```bash
coinjure backtest run \
  --parquet data/<file>.parquet \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --json
```

4. 批量回测

```bash
coinjure research batch-markets \
  --history-file <history.jsonl> \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --limit 20 \
  --output data/research/batch_result.json \
  --json
```

5. 完整流水线（validate + backtest + gate）

```bash
coinjure research alpha-pipeline \
  --history-file <history.jsonl> \
  --strategy-ref strategies/<name>.py:<ClassName> \
  --json
```

## Hard Rules

- 不把策略逻辑写死在命令行脚本里，必须落到独立策略文件。
- 构造函数参数只允许 JSON 序列化的基本类型（不能是 dict / object / callable）。
- 不使用未来信息（禁止 look-ahead）。
- 必须可复现：策略文件路径、类名、kwargs、命令都要明确。
- 套利策略的 `process_event` 必须真正调用 `trader.place_order(...)`，不能只 `record_decision`。
