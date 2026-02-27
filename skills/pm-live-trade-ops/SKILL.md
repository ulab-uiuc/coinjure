---
name: pm-live-trade-ops
description: 用于在明确授权下执行 live trading，并严格执行风险与应急控制。
---

# PM Live Trade Ops

仅在用户明确要求 live 且已完成 paper 验证后使用。

## 前置门槛

- `strategy validate` 通过
- 最新 backtest/auto-tune 结果可接受
- 最近 paper run 行为稳定
- 用户明确批准 live 启动

## 启动命令

1. Polymarket

- `coinjure live run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

2. Kalshi

- `coinjure live run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH" --strategy-ref <strategy_ref> --strategy-kwargs-json '<json>' --json`

## 运行控制

- `coinjure trade status --json`
- `coinjure trade state --json`
- `coinjure trade pause --json`
- `coinjure trade resume --json`
- `coinjure trade killswitch --on --json`
- `coinjure trade stop --json`

## 应急顺序

1. 先 `pause`
2. 评估持仓和订单状态
3. 必要时 `killswitch --on`
4. 最后 `stop`

## Hard Rules

- 无明确用户批准，不启动 live。
- 不跳过 paper 阶段直接上 live。
- 所有 live 运行必须保留可审计记录（时间、参数、状态快照、处置动作）。
