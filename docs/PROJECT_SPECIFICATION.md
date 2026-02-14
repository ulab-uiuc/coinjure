# SWM Agent 项目说明书

## 一、项目核心功能与解决的问题

### 1.1 核心功能

**SWM Agent**（Social World Model Trading Agent）是一个面向 **Polymarket 预测市场** 的智能交易代理系统，基于社会世界模型（Social World Model）概念构建。项目的核心功能包括：

| 功能模块 | 描述 |
|---------|------|
| **实时市场集成** | 对接 Polymarket 的 CLOB（中央限价订单簿）API 进行实盘交易 |
| **新闻情感分析** | 集成新闻 API 与 RSS 源，分析市场相关新闻事件 |
| **LLM 驱动决策** | 使用大语言模型分析新闻内容并生成交易信号 |
| **风险管控** | 支持多级风险经理，可配置持仓、回撤、单笔等限制 |
| **回测框架** | 基于历史数据进行策略验证 |
| **模拟交易** | 纸质交易模式，可在无真实资金风险下测试策略 |
| **绩效分析** | 提供 Sharpe 比率、最大回撤、胜率等指标 |

### 1.2 解决的问题

1. **自动化预测市场交易**：将新闻、订单簿等事件与交易决策自动化衔接  
2. **风险可控的实盘与模拟**：通过风险经理与模拟交易降低实盘试错成本  
3. **策略可复用与可扩展**：统一策略接口，方便定制和回测  
4. **多数据源统一接入**：抽象数据源接口，支持历史、新闻、RSS、Polymarket 实时数据  

---

## 二、技术栈

### 2.1 编程语言与运行时

- **Python**：>= 3.10，< 3.12

### 2.2 核心框架与库

| 类别 | 技术 | 用途 |
|------|------|------|
| **CLI** | Click | 命令行界面 |
| **终端展示** | Rich | 监控面板、表格、布局 |
| **数据验证** | Pydantic | 数据模型校验 |
| **类型检查** | Beartype | 运行时类型检查 |
| **Polymarket** | py-clob-client | Polymarket CLOB API 交互 |
| **HTTP 客户端** | httpx | 异步 HTTP 请求 |
| **RSS 解析** | feedparser | RSS 订阅解析 |

### 2.3 数据库与中间件

- **无专用数据库**：使用本地 JSONL 文件缓存事件与新闻（如 `events_cache.jsonl`、`news_cache.jsonl` 等）
- **无消息队列**：使用 Python `asyncio.Queue` 实现事件流

### 2.4 开发与测试工具

- **包管理**：Poetry  
- **代码风格**：Ruff（替代 Black / isort）  
- **类型检查**：mypy（strict 模式）  
- **测试**：pytest、pytest-asyncio、pytest-cov、pytest-mock、hypothesis  
- **预提交**：pre-commit  

---

## 三、项目目录结构

```
qfj/
├── swm_agent/                    # 主包目录
│   ├── cli/                     # 命令行
│   │   ├── cli.py               # CLI 入口
│   │   ├── monitor.py           # 交易监控命令
│   │   └── utils.py
│   ├── core/                    # 核心引擎
│   │   └── trading_engine.py    # 交易引擎（事件循环与驱动）
│   ├── strategy/                # 策略层
│   │   ├── strategy.py         # 策略抽象基类
│   │   ├── simple_strategy.py  # LLM 策略
│   │   └── test_strategy.py    # 测试用策略
│   ├── trader/                  # 交易执行层
│   │   ├── trader.py           # 交易者抽象基类
│   │   ├── paper_trader.py     # 纸质交易（模拟）
│   │   ├── polymarket_trader.py # Polymarket 实盘交易
│   │   └── types.py            # 交易类型定义
│   ├── data/                    # 数据层
│   │   ├── data_source.py      # 数据源抽象基类
│   │   ├── market_data_manager.py # 行情管理
│   │   ├── backtest/
│   │   │   └── historical_data_source.py # 历史回测数据源
│   │   └── live/
│   │       └── live_data_source.py # 实时数据源（Polymarket/新闻/RSS）
│   ├── events/                  # 事件系统
│   │   └── events.py           # OrderBookEvent、NewsEvent、PriceChangeEvent
│   ├── ticker/                  # 标的标识
│   │   └── ticker.py           # Ticker、PolyMarketTicker、CashTicker
│   ├── order/                   # 订单
│   │   └── order_book.py       # 订单簿管理
│   ├── position/                # 持仓
│   │   └── position_manager.py # 持仓与 PnL 管理
│   ├── risk/                    # 风控
│   │   └── risk_manager.py     # NoRisk/Standard/Conservative/Aggressive
│   ├── analytics/                # 分析
│   │   └── performance_analyzer.py # 绩效分析
│   ├── backtest/                # 回测
│   │   └── backtester.py       # 回测编排
│   └── live/                    # 实盘
│       └── live_trader.py      # 实盘/模拟运行入口
├── examples/                     # 示例
│   ├── backtest_example.py
│   ├── live_paper_trading_example.py
│   ├── custom_strategy_example.py
│   ├── performance_analysis_example.py
│   ├── monitor_example.py
│   └── demo_monitor.py
├── scripts/                      # 工具脚本
│   ├── get_live_polymarket_data.py
│   └── get_live_news_data.py
├── tests/                        # 单元测试
├── docs/                         # 文档（本项目说明书所在目录）
├── .github/                      # CI/CD 和 Issue 模板
├── pyproject.toml               # Poetry 配置
├── README.md
└── .pre-commit-config.yaml
```

### 3.1 关键目录说明

| 目录 | 作用 |
|------|------|
| `swm_agent/core/` | 交易引擎，负责事件循环、策略调用、交易执行调度 |
| `swm_agent/strategy/` | 策略定义，实现 `process_event` 并调用 `trader.place_order` |
| `swm_agent/trader/` | 交易执行，包含模拟（PaperTrader）与实盘（PolymarketTrader） |
| `swm_agent/data/` | 数据源抽象，历史、Polymarket、新闻、RSS 的实现 |
| `swm_agent/events/` | 事件类型：OrderBookEvent、NewsEvent、PriceChangeEvent |
| `swm_agent/risk/` | 风控层，限制单笔、单标、总敞口、回撤、日损失等 |
| `swm_agent/position/` | 持仓与 PnL 计算 |
| `swm_agent/analytics/` | Sharpe、胜率、最大回撤、盈亏比等绩效指标 |
| `swm_agent/live/` | 实盘/模拟运行入口（`run_live_paper_trading`、`run_live_polymarket_trading` 等） |

---

## 四、程序入口

### 4.1 CLI 入口（主入口）

在 `pyproject.toml` 中定义：

```toml
[tool.poetry.scripts]
swm-agent = "swm_agent.cli.cli:cli"
```

即主入口为：**`swm_agent.cli.cli:cli`**。

安装后可通过命令：

```bash
swm-agent monitor           # 监控
swm-agent monitor --watch   # 实时刷新
```

直接调用该 CLI。

### 4.2 代码入口点一览

| 入口 | 文件 | 说明 |
|------|------|------|
| **CLI** | `swm_agent/cli/cli.py` | `cli()` → 注册 `monitor` 子命令 |
| **回测** | `examples/backtest_example.py` | 直接运行该脚本进行回测 |
| **模拟实盘** | `examples/live_paper_trading_example.py` 或 `swm_agent/live/live_trader.py` | 通过 `run_live_paper_trading()` 运行 |
| **实盘交易** | `swm_agent/live/live_trader.py` | 通过 `run_live_polymarket_trading()` 运行 |

### 4.3 执行流程概览

```
用户命令 / 脚本
    ↓
CLI (cli.py) 或 examples / live_trader
    ↓
TradingEngine(data_source, strategy, trader)
    ↓
engine.start()：循环调用 data_source.get_next_event()
    ↓
对 OrderBookEvent → market_data.process_orderbook_event()
对 PriceChangeEvent → market_data.process_price_change_event()
    ↓
strategy.process_event(event, trader)
    ↓
strategy 内部调用 trader.place_order()
    ↓
PaperTrader 或 PolymarketTrader 执行订单并更新 position_manager
```

---

## 五、附录：典型运行方式

### 回测

```bash
python examples/backtest_example.py
```

### 模拟实盘（RSS 新闻）

```bash
python examples/live_paper_trading_example.py
# 或
python -c "
import asyncio
from decimal import Decimal
from swm_agent.data.live.live_data_source import LiveRSSNewsDataSource
from swm_agent.live.live_trader import run_live_paper_trading
from swm_agent.strategy.test_strategy import TestStrategy

asyncio.run(run_live_paper_trading(
    data_source=LiveRSSNewsDataSource(polling_interval=60.0),
    strategy=TestStrategy(),
    initial_capital=Decimal('10000'),
    duration=300,
))
"
```

### 监控

```bash
swm-agent monitor
swm-agent monitor --watch --refresh 1.0
```

---

*文档版本：基于项目当前代码结构整理*
