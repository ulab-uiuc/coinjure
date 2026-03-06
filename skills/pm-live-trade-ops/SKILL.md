---
name: pm-live-trade-ops
description: 用于在明确授权下执行 live trading，并严格执行风险与应急控制。
---

# PM Live Trade Ops

仅在用户明确要求 live 且已完成 paper 验证后使用。

## 前置门槛

- `strategy validate` 通过
- 最近 paper run 行为稳定（通过 `portfolio health-check` 确认）
- 用户明确批准 live 启动

## 启动命令

### Polymarket

```bash
coinjure live run \
  --exchange polymarket \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --json
```

### Kalshi

```bash
coinjure live run \
  --exchange kalshi \
  --kalshi-api-key-id "$KALSHI_API_KEY_ID" \
  --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --json
```

### 跨平台（通过 hub 共享数据）

```bash
coinjure hub start --detach --json
coinjure live run \
  --exchange cross_platform \
  --hub-socket ~/.coinjure/hub.sock \
  --wallet-private-key "$POLYMARKET_PRIVATE_KEY" \
  --kalshi-api-key-id "$KALSHI_API_KEY_ID" \
  --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" \
  --strategy-ref <strategy_ref> \
  --strategy-kwargs-json '<json>' \
  --json
```

### 通过 portfolio 升级（推荐）

```bash
coinjure portfolio promote --strategy-id <id> --to live --json
```

## 运行控制

```bash
coinjure trade status --json
coinjure trade state --json
coinjure trade pause --json
coinjure trade resume --json
coinjure trade swap --strategy-ref <ref> --strategy-kwargs-json '<json>' --json
coinjure trade stop --json
```

## 批量监控

```bash
coinjure portfolio list --json
coinjure portfolio health-check --json
coinjure portfolio retire --strategy-id <id> --reason <reason> --json
```

## 应急顺序

1. 先 `pause`
2. `trade state --json` 评估持仓和订单状态
3. 必要时 `trade stop --json`
4. 批量停止：`portfolio retire --strategy-id <id> --reason "emergency"`

## Hard Rules

- 无明确用户批准，不启动 live。
- 不跳过 paper 阶段直接上 live。
- 所有 live 运行必须保留可审计记录（时间、参数、状态快照、处置动作）。
