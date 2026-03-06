---
name: pm-paper-trade-ops
description: 用于在策略通过回测后执行 paper trading、监控、干预和归档。
---

# PM Paper Trade Ops

当用户要求跑 paper trading 或验证线上行为时，使用这个技能。

## 前置条件

- 策略已通过：`coinjure strategy validate ... --json`
- 有明确的 strategy_ref + kwargs（从 arb scan / scan-events 输出获取）

## 启动流程

### 方式 A：单策略手动启动

```bash
# 跨平台套利（Poly + Kalshi）
coinjure paper run \
  --exchange cross_platform \
  --strategy-ref examples/strategies/direct_arb_strategy.py:DirectArbStrategy \
  --strategy-kwargs-json '{"poly_market_id":"...","poly_token_id":"...","kalshi_ticker":"...","min_edge":0.02}' \
  --json

# 单平台 event-sum 套利
coinjure paper run \
  --exchange polymarket \
  --strategy-ref examples/strategies/event_sum_arb_strategy.py:EventSumArbStrategy \
  --strategy-kwargs-json '{"event_id":"...","min_edge":0.01}' \
  --json

# 接入共享数据源（多策略时必用）
coinjure paper run ... --hub-socket ~/.coinjure/hub.sock --json

# 可视化监控
coinjure paper run ... --monitor
```

### 方式 B：通过 portfolio 批量部署（推荐）

```bash
# 跨平台批量（自动扫描 + 注册 + 启动）
coinjure arb deploy --query "NBA" --min-edge 0.02 --max-deploy 5 --json

# 单平台 event-sum 批量
coinjure arb deploy-events --query "NBA" --min-edge 0.01 --max-deploy 5 --json

# 先 dry-run 验证
coinjure arb deploy-events --query "NBA" --dry-run --json
```

## 运行中控制（单引擎）

```bash
coinjure trade status --json          # 运行状态
coinjure trade state --json           # 完整快照（持仓、决策、订单簿）
coinjure trade pause --json           # 暂停
coinjure trade resume --json          # 恢复
coinjure trade swap --strategy-ref <ref> --strategy-kwargs-json '<json>' --json
coinjure trade stop --json            # 停止
```

## 批量监控（portfolio）

```bash
coinjure portfolio list --json
coinjure portfolio health-check --json   # 检查哪些活着、哪些挂了、PnL
coinjure portfolio retire --strategy-id <id> --reason <reason> --json
```

## Hub 共享数据源

```bash
# 多策略必须先启动 hub（避免 API 限流）
coinjure hub start --detach --json
coinjure hub status --json
coinjure hub stop --json
```

## 结果归档

- 保存关键输出到 `data/research/<strategy_id>/`
- 至少包含：配置（strategy_ref + kwargs）、状态快照、最终 PnL

## Hard Rules

- 发现异常先 `pause`，确认后再 `resume` 或 `stop`。
- paper 阶段不使用 live 凭证。
- 批量部署前必须先 `--dry-run` 验证策略可实例化。
- 多策略并行时必须启动 hub，避免交易所限流。
