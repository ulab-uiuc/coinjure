# Coinjure

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![PyPI version](https://img.shields.io/pypi/v/coinjure.svg)](https://pypi.org/project/coinjure/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)

**Coinjure** is a trading agent harness for [Polymarket](https://polymarket.com/) and [Kalshi](https://kalshi.com/) prediction markets. It empowers LLM agents to drive the entire strategy lifecycle purely through CLI commands — autonomously discovering cross-market relations, building executable strategies, running large-scale backtests, and deploying to live execution.

## Key Features

- **Real-time Market Integration** — Connects to Polymarket's CLOB API and Kalshi for live order book data and trading
- **LLM-Powered Strategies** — Analyzes news events with Large Language Models to generate trading signals
- **Multi-Source Data Ingestion** — Polymarket live data, News API, and RSS feeds (WSJ, etc.)
- **Risk Management** — Configurable limits on trade size, position size, drawdown, daily loss, and more
- **Backtesting** — Test strategies against historical data before going live
- **Paper Trading** — Simulate live trading without real capital
- **Performance Analytics** — Sharpe ratio, max drawdown, win rate, profit factor, equity curve

## Architecture

```
TradingEngine
  ├── DataSource          (Live / Historical / RSS / News API)
  ├── Strategy            (LLM-based / Custom)
  └── Trader              (PaperTrader / PolymarketTrader / KalshiTrader)
       ├── RiskManager    (Standard / Conservative / Aggressive)
       └── PositionManager
```

## Installation

```bash
pip install coinjure
```

Or install from source with [Poetry](https://python-poetry.org/):

```bash
git clone https://github.com/ulab-uiuc/prediction-market-cli.git
cd coinjure
pip install poetry
poetry install
```

!!! note "Requirements"
Python >= 3.10, < 3.12

## Quick Start

### Run a Backtest

```python
import asyncio
from decimal import Decimal

from coinjure.engine.engine import TradingEngine
from coinjure.data.backtest.parquet import ParquetDataSource
from coinjure.data.manager import DataManager
from coinjure.trading.position import Position, PositionManager
from coinjure.trading.risk import NoRiskManager
from coinjure.strategy.demo import DemoStrategy
from coinjure.ticker import CashTicker, PolyMarketTicker
from coinjure.engine.trader.paper import PaperTrader

async def run():
    ticker = PolyMarketTicker(symbol="my_market", name="My Market",
                               market_id="123", event_id="456")
    data_source = ParquetDataSource("data.parquet", ticker)
    market_data = DataManager()
    position_manager = PositionManager()
    position_manager.update_position(
        Position(ticker=CashTicker.POLYMARKET_USDC, quantity=Decimal("10000"),
                 average_cost=Decimal("0"), realized_pnl=Decimal("0"))
    )
    trader = PaperTrader(market_data=market_data, risk_manager=NoRiskManager(),
                         position_manager=position_manager,
                         min_fill_rate=Decimal("0.5"), max_fill_rate=Decimal("1.0"),
                         commission_rate=Decimal("0.0"))
    engine = TradingEngine(data_source=data_source, strategy=DemoStrategy(), trader=trader)
    await engine.start()
    print(f"Final PnL: {position_manager.get_total_realized_pnl()}")

asyncio.run(run())
```

### Custom Strategy

```python
from coinjure.strategy.strategy import Strategy
from coinjure.events import Event
from coinjure.trading.trader import Trader

class MyStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Your logic here
        pass
```

## CLI Commands

```bash
# Market discovery & relations
coinjure market discover --exchange polymarket
coinjure market info --exchange polymarket --market-id <ID>
coinjure market news --source google --query "prediction markets"
coinjure market relations list
coinjure market relations add <RELATION_JSON>

# Backtest mode
coinjure engine backtest --relation-id <REL_ID>

# Paper trading (simulation with live data)
coinjure engine paper-run --exchange polymarket --strategy-ref ./strategies/my_strategy.py:MyStrategy
coinjure engine paper-run --exchange kalshi --strategy-ref ./strategies/my_strategy.py:MyStrategy

# Real trading
coinjure engine live-run --exchange polymarket --wallet-private-key "$POLYMARKET_PRIVATE_KEY"
coinjure engine live-run --exchange kalshi --kalshi-api-key-id "$KALSHI_API_KEY_ID" --kalshi-private-key-path "$KALSHI_PRIVATE_KEY_PATH"

# Operator monitor + emergency control
coinjure engine monitor
coinjure engine status
coinjure engine pause
coinjure engine resume
coinjure engine stop
coinjure engine killswitch
```

## Risk Management

Three built-in tiers:

| Setting        | Conservative | Aggressive |
| -------------- | ------------ | ---------- |
| Max Trade Size | $500         | $5,000     |
| Max Position   | $2,000       | $20,000    |
| Max Exposure   | $10,000      | $100,000   |
| Max Drawdown   | 10%          | 30%        |

## Environment Variables

```bash
export POLYMARKET_PRIVATE_KEY="your_private_key"   # Required for live trading
export KALSHI_API_KEY_ID="your_kalshi_key_id"      # Required for Kalshi live trading
export KALSHI_PRIVATE_KEY_PATH="/path/key.pem"     # Required for Kalshi live trading
export NEWS_API_KEY="your_news_api_key"             # Optional, for News API source
```

## License

[MIT](https://github.com/ulab-uiuc/prediction-market-cli/blob/main/LICENSE)

!!! warning "Disclaimer"
This software is for **educational and research purposes only**. Trading involves substantial risk of loss. Always test strategies with paper trading before using real funds.
