---
name: pm-data-discovery
description: 用于先搞清楚"有哪些数据可看、怎么搜索目标市场、怎么落地数据样本"的技能。只做数据发现与取数，不做策略假设。
---

# PM Data Discovery

当用户要先看数据、找市场、发现套利机会时，使用这个技能。

## 目标

回答 3 个问题：

- 有哪些可用市场和套利机会
- 如何搜索到目标 event / market
- 如何把发现结果传给后续策略部署步骤

## 数据入口

### 1. 市场搜索（在线，无需认证）

```bash
# 列出 Polymarket 开放市场
coinjure market list --exchange polymarket --limit 50 --json

# 按关键词搜索
coinjure market search --exchange polymarket --query "<关键词>" --limit 50 --json
coinjure market search --exchange kalshi --query "<关键词>" --limit 50 --json

# 查看单个市场详情（含 bid/ask）
coinjure market info --market-id <market_id> --json

# 跨平台模糊匹配（找 Poly + Kalshi 相同事件）
coinjure market match --query "<关键词>" --min-similarity 0.6 --json
```

### 2. 套利机会扫描（在线）

```bash
# 跨平台套利：Polymarket vs Kalshi 同一事件价差
coinjure arb scan --query "<关键词>" --min-edge 0.02 --json

# 单平台多结果套利：同一 event 下 sum(YES) != 1.0
coinjure arb scan-events --query "<关键词>" --min-edge 0.01 --json
# 输出包含: event_id, event_title, sum_yes, best_edge, action, markets[]
```

### 3. 新闻与外部事件（在线）

```bash
# Google News / RSS 抓取
coinjure news fetch --source google --query "<关键词>" --limit 30 --json
coinjure news fetch --source rss --query "<关键词>" --limit 30 --json
```

### 4. 回测数据（本地 parquet）

```bash
# 查看可用的 parquet 文件
ls data/*.parquet

# 回测时使用
coinjure backtest run --parquet data/<file>.parquet --strategy-ref <ref> --json
```

## 推荐流程

1. `market search` 找候选市场。
2. `news fetch` 获取相关新闻事件，辅助判断市场方向。
3. `arb scan` / `arb scan-events` 发现当前套利机会。
4. `market match` 确认跨平台配对质量（similarity 分数）。
5. `market info` 确认流动性（有 bid/ask 数据）。
6. 把 `event_id` / `poly_id` / `kalshi_ticker` 传给部署步骤。

## 输出要求

- 给出明确的市场清单：event_id、market_id、当前 edge。
- 每个结论都附可复现命令。
- 命令优先 `--json` 输出，便于后续 agent 消费。

## Hard Rules

- 不在此技能里提出策略逻辑假设。
- 只做数据发现、过滤、取样。
- 如发现 edge < 0，明确说明该机会不满足条件，不要强行部署。
