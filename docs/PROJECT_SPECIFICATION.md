# Coinjure Project Specification

## 1. Core Features and Problems Solved

### 1.1 Core Features

**Coinjure** (Social World Model Trading Agent) is an intelligent trading agent system designed for the **Polymarket prediction market**, built on the Social World Model concept. Its core features include:

| Module                           | Description                                                                          |
| -------------------------------- | ------------------------------------------------------------------------------------ |
| **Real-time Market Integration** | Connects to Polymarket's CLOB (Central Limit Order Book) API for live trading        |
| **News Sentiment Analysis**      | Integrates news APIs and RSS feeds to analyze market-relevant news events            |
| **LLM-driven Decision Making**   | Uses large language models to analyze news content and generate trading signals      |
| **Risk Management**              | Multi-level risk managers with configurable position, drawdown, and per-trade limits |
| **Backtesting Framework**        | Strategy validation using historical data                                            |
| **Paper Trading**                | Simulated trading mode for strategy testing without real capital risk                |
| **Performance Analytics**        | Provides Sharpe ratio, maximum drawdown, win rate, and other metrics                 |

### 1.2 Problems Solved

1. **Automated prediction market trading**: Automates the connection between news, order book events, and trading decisions
2. **Risk-controlled live and simulated trading**: Reduces live-trading trial-and-error cost through risk managers and paper trading
3. **Reusable and extensible strategies**: Unified strategy interface for easy customization and backtesting
4. **Unified multi-source data ingestion**: Abstract data source interface supporting historical, news, RSS, and Polymarket live data

---

## 2. Technology Stack

### 2.1 Language and Runtime

- **Python**: >= 3.10, < 3.12

### 2.2 Core Frameworks and Libraries

| Category             | Technology     | Purpose                                |
| -------------------- | -------------- | -------------------------------------- |
| **CLI**              | Click          | Command-line interface                 |
| **Terminal Display** | Rich           | Monitoring dashboards, tables, layouts |
| **Data Validation**  | Pydantic       | Data model validation                  |
| **Type Checking**    | Beartype       | Runtime type checking                  |
| **Polymarket**       | py-clob-client | Polymarket CLOB API interaction        |
| **HTTP Client**      | httpx          | Asynchronous HTTP requests             |
| **RSS Parsing**      | feedparser     | RSS feed parsing                       |

### 2.3 Databases and Middleware

- **No dedicated database**: Uses local JSONL files to cache events and news (e.g., `events_cache.jsonl`, `news_cache.jsonl`)
- **No message queue**: Uses Python `asyncio.Queue` for event streaming

### 2.4 Development and Testing Tools

- **Package management**: Poetry
- **Code style**: Ruff (replaces Black / isort)
- **Type checking**: mypy (strict mode)
- **Testing**: pytest, pytest-asyncio, pytest-cov, pytest-mock, hypothesis
- **Pre-commit**: pre-commit

---

## 3. Project Directory Structure

```
prediction-market-cli/
├── coinjure/                        # Main package
│   ├── ticker.py                    # Ticker, PolyMarketTicker, KalshiTicker, CashTicker
│   ├── events.py                    # Event, PriceChangeEvent, OrderBookEvent, NewsEvent
│   ├── trading/                     # Shared trading abstractions
│   │   ├── types.py                 # TradeSide, Order, Trade, OrderStatus, PlaceOrderResult
│   │   ├── trader.py                # Trader ABC
│   │   ├── position.py              # PositionManager, Position
│   │   └── risk.py                  # RiskManager ABC, Standard/Conservative/Aggressive
│   ├── data/                        # Data infrastructure
│   │   ├── source.py                # DataSource ABC, CompositeDataSource
│   │   ├── order_book.py            # Level, OrderBook
│   │   ├── manager.py               # DataManager, DataPoint
│   │   ├── fetcher.py               # Polymarket Gamma API + Kalshi REST fetching
│   │   ├── news.py                  # fetch_google_news, fetch_rss, fetch_thenewsapi
│   │   ├── live/
│   │   │   ├── polymarket.py        # LivePolyMarketDataSource (CLOB)
│   │   │   └── kalshi.py            # LiveKalshiDataSource (REST)
│   │   └── backtest/
│   │       └── parquet.py           # ParquetDataSource (replay)
│   ├── market/                      # Market analysis
│   │   ├── relations.py             # MarketRelation + RelationStore
│   │   └── auto_pair.py             # Auto-pair detection
│   ├── strategy/                    # Strategy framework
│   │   ├── strategy.py              # Strategy ABC + IdleStrategy
│   │   ├── demo.py                  # DemoStrategy (example momentum)
│   │   ├── agent.py                 # AgentStrategy (LLM-powered)
│   │   ├── loader.py                # load_strategy() — dynamic class loading
│   │   ├── relation_mixin.py        # RelationArbMixin
│   │   └── builtin/                 # 8 built-in strategies (DirectArb, EventSumArb, etc.)
│   ├── engine/                      # Trading engine + concrete traders
│   │   ├── engine.py                # TradingEngine (main event loop)
│   │   ├── runner.py                # run_live_paper_trading, run_live_polymarket_trading
│   │   ├── backtester.py            # run_backtest_parquet
│   │   ├── performance.py           # PerformanceAnalyzer
│   │   ├── registry.py              # StrategyRegistry (portfolio state)
│   │   ├── control.py               # ControlServer (Unix socket RPC)
│   │   └── trader/                  # Concrete trader implementations
│   │       ├── paper.py             # PaperTrader
│   │       ├── polymarket.py        # PolymarketTrader
│   │       ├── kalshi.py            # KalshiTrader
│   │       └── alerter.py           # Alerter ABC + LogAlerter
│   ├── hub/                         # Optional: fan-out Unix socket server
│   │   ├── hub.py                   # MarketDataHub + send_hub_command()
│   │   └── subscriber.py            # HubDataSource
│   ├── storage/                     # Persistence
│   │   ├── serializers.py           # JSON serialization for trading types
│   │   └── state_store.py           # StateStore (JSON file persistence)
│   └── cli/                         # Click CLI (thin wrappers)
│       ├── cli.py                   # Main entry point
│       ├── market_commands.py       # market info, discover, news, relations
│       ├── engine_commands.py       # engine paper-run, live-run, hub-*, list, add, etc.
│       ├── monitor.py               # TradingMonitor
│       ├── textual_monitor.py       # TUI dashboard
│       └── utils.py                 # CLI utilities
├── tests/                           # Unit tests
├── docs/                            # Documentation
├── .github/                         # CI/CD and issue templates
├── pyproject.toml                   # Poetry configuration
├── README.md
└── .pre-commit-config.yaml
```

### 3.1 Key Directory Descriptions

| Directory                 | Purpose                                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------- |
| `coinjure/trading/`       | Shared trading abstractions: Trader ABC, types, position management, risk managers           |
| `coinjure/data/`          | Data source abstraction: live (Polymarket CLOB, Kalshi REST), backtest (Parquet), news, RSS  |
| `coinjure/market/`        | Market analysis: relation graph, auto-pair detection                                         |
| `coinjure/strategy/`      | Strategy definitions: Strategy ABC, demo, agent, loader, and 8 built-in arbitrage strategies |
| `coinjure/engine/`        | Trading engine: event loop, runner, backtester, performance analyzer, control server         |
| `coinjure/engine/trader/` | Concrete trader implementations: PaperTrader, PolymarketTrader, KalshiTrader                 |
| `coinjure/hub/`           | Optional fan-out Unix socket server for shared market data                                   |
| `coinjure/storage/`       | Persistence: JSON serialization, state store                                                 |
| `coinjure/cli/`           | Click CLI: market commands, engine commands, monitor, TUI dashboard                          |

---

## 4. Program Entry Points

### 4.1 CLI Entry Point (Main)

Defined in `pyproject.toml`:

```toml
[tool.poetry.scripts]
coinjure = "coinjure.cli.cli:cli"
```

The main entry point is: **`coinjure.cli.cli:cli`**.

After installation, the CLI can be invoked directly:

```bash
coinjure engine monitor    # Attach live TUI monitor
coinjure engine paper-run  # Paper trading
coinjure engine live-run   # Live trading
```

### 4.2 Entry Point Overview

| Entry Point       | File                            | Description                                                       |
| ----------------- | ------------------------------- | ----------------------------------------------------------------- |
| **CLI**           | `coinjure/cli/cli.py`           | `cli()` — registers `market`, `engine`, and `hub` command groups  |
| **Backtesting**   | `coinjure/engine/backtester.py` | `run_backtest_parquet()` or via `coinjure engine backtest`        |
| **Paper Trading** | `coinjure/engine/runner.py`     | `run_live_paper_trading()` or via `coinjure engine paper-run`     |
| **Live Trading**  | `coinjure/engine/runner.py`     | `run_live_polymarket_trading()` or via `coinjure engine live-run` |

### 4.3 Execution Flow Overview

```
User command (coinjure engine paper-run / live-run / backtest)
    ↓
CLI (cli.py → engine_commands.py)
    ↓
runner.py → TradingEngine(data_source, strategy, trader)
    ↓
engine.start(): loops calling data_source.get_next_event()
    ↓
OrderBookEvent → market_data.process_orderbook_event()
PriceChangeEvent → market_data.process_price_change_event()
    ↓
strategy.process_event(event, trader)
    ↓
Strategy internally calls trader.place_order()
    ↓
PaperTrader / PolymarketTrader / KalshiTrader executes order and updates position_manager
```

---

## 5. Appendix: Typical Run Commands

### Backtesting

```bash
coinjure engine backtest --relation-id <REL_ID>
```

### Paper Trading

```bash
coinjure engine paper-run --exchange polymarket --strategy-ref ./strategies/my_strategy.py:MyStrategy
```

### Live Trading

```bash
coinjure engine live-run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY"
```

### Monitoring

```bash
coinjure engine monitor
```

---

_Document version: Based on current project code structure_
