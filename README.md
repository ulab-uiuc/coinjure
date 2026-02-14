# SWM Agent: Social World Model Trading Agent

[![Python 3.10](https://img.shields.io/badge/python-3.10-blue.svg)](https://www.python.org/downloads/release/python-3109/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://pre-commit.com/)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![bear-ified](https://raw.githubusercontent.com/beartype/beartype-assets/main/badge/bear-ified.svg)](https://beartype.readthedocs.io)

> [!NOTE]
> This repository is continuously updating with more features and improvements. Any contribution is welcome.

## 🎯 Project Overview

**SWM Agent** is an intelligent trading agent designed for Polymarket prediction markets, built on the concept of Social World Models. The agent combines real-time market data, news sentiment analysis, and machine learning to make automated trading decisions.

### Key Features

- **Real-time Market Integration**: Connects to Polymarket's CLOB (Central Limit Order Book) API for live trading
- **News Sentiment Analysis**: Integrates with news APIs to analyze market-moving events
- **LLM-Powered Decision Making**: Uses Large Language Models to analyze news and make trading decisions
- **Risk Management**: Built-in risk management and position tracking
- **Backtesting Framework**: Historical data testing capabilities
- **Paper Trading**: Safe simulation mode for strategy testing
- **CLI Monitoring**: Real-time terminal dashboard for monitoring trades, positions, and portfolio P&L

## 🏗️ Architecture

The project follows a modular, event-driven architecture with clear separation of concerns:

```
SWM Agent
├── Core Trading Engine
├── Data Sources (Live & Historical)
├── Strategy Layer (LLM-based decisions)
├── Risk & Position Management
├── Market Data Processing
└── Analytics & Performance Tracking
```

## 📁 Project Structure

### Core Components

#### `swm_agent/core/`

- **`trading_engine.py`**: Main orchestration engine that coordinates data flow, strategy execution, and trading operations

#### `swm_agent/strategy/`

- **`strategy.py`**: Abstract base class for all trading strategies
- **`simple_strategy.py`**: LLM-powered strategy that analyzes news events and makes trading decisions
- **`test_strategy.py`**: Testing strategy implementation

#### `swm_agent/trader/`

- **`trader.py`**: Abstract base class for trading interfaces
- **`polymarket_trader.py`**: Concrete implementation for Polymarket CLOB trading
- **`paper_trader.py`**: Simulation trader for testing strategies without real money
- **`types.py`**: Trading-related data types and enums

#### `swm_agent/data/`

- **`data_source.py`**: Abstract base class for data sources
- **`market_data_manager.py`**: Manages market data processing and storage
- **`backtest/`**: Historical data sources for backtesting
- **`live/`**: Real-time data sources for live trading

#### `swm_agent/events/`

- **`events.py`**: Event system for market data and news events
  - `OrderBookEvent`: Market order book updates
  - `NewsEvent`: News articles and sentiment data
  - `PriceChangeEvent`: Price movement events

#### `swm_agent/analytics/`

- **`performance_analyzer.py`**: Trading performance analysis and metrics

#### `swm_agent/backtest/`

- **`backtester.py`**: Historical strategy testing framework

#### `swm_agent/risk/`

- **`risk_manager.py`**: Risk management and position sizing

#### `swm_agent/position/`

- **`position_manager.py`**: Position tracking and management

#### `swm_agent/order/`

- **`order_book.py`**: Order book management and analysis

#### `swm_agent/ticker/`

- **`ticker.py`**: Market ticker and symbol management

#### `swm_agent/live/`

- **`live_trader.py`**: Live trading execution engine

### Scripts

#### `scripts/`

- **`get_live_news_data.py`**: Script to test and collect live news data
- **`get_live_polymarket_data.py`**: Script to test and collect live Polymarket data

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- Polymarket API credentials
- News API key (optional, for news sentiment analysis)

### Installation

```bash
# Clone the repository
git clone https://github.com/ulab-uiuc/swm-agent.git
cd swm-agent

# Install dependencies using Poetry
pip install poetry
poetry install

# Or using pip
pip install -e .
```

### Environment Setup

```bash
# Set up environment variables
export POLYMARKET_PRIVATE_KEY="your_private_key"
export NEWS_API_KEY="your_news_api_key"  # Optional
```

### Basic Usage

```python
from swm_agent.core.trading_engine import TradingEngine
from swm_agent.strategy.simple_strategy import SimpleStrategy
from swm_agent.trader.polymarket_trader import PolymarketTrader
from swm_agent.data.live.live_data_source import LivePolyMarketDataSource

# Create components
data_source = LivePolyMarketDataSource()
strategy = SimpleStrategy()
trader = PolymarketTrader(wallet_private_key="your_key")

# Initialize trading engine
engine = TradingEngine(data_source, strategy, trader)

# Start trading
await engine.start()
```

### Testing Data Sources

```bash
# Test news data collection
python scripts/get_live_news_data.py --api-token YOUR_TOKEN --duration 300

# Test Polymarket data collection
python scripts/get_live_polymarket_data.py --duration 300
```

### CLI Monitoring

Monitor your trading activities in real-time with the built-in CLI dashboard:

```bash
# Display current portfolio snapshot
swm-agent monitor

# Live monitoring with auto-refresh
swm-agent monitor --watch

# Custom refresh rate (1 second)
swm-agent monitor --watch --refresh 1.0

# Run the demo
python examples/demo_monitor.py --watch
```

The monitor displays:

- **Portfolio Summary**: Total value, cash positions, realized/unrealized P&L
- **Active Positions**: Open positions with current prices and P&L breakdown
- **Recent Orders**: Order history with status and fill details
- **Market Snapshot**: Real-time bid/ask spreads
- **Statistics**: Success rate, order counts, session runtime

For detailed usage and integration instructions, see [CLI Monitoring Documentation](docs/CLI_MONITORING.md).

## 🔧 Configuration

The agent supports various configuration options:

- **Trading Parameters**: Position sizes, confidence thresholds
- **Risk Management**: Stop losses, position limits
- **Data Sources**: API endpoints, polling intervals
- **LLM Configuration**: Model selection, API keys

## 📊 Strategy Overview

### Simple Strategy (LLM-Powered)

The default strategy uses Large Language Models to:

1. **Analyze News Events**: Process incoming news articles for market sentiment
2. **Generate Trading Signals**: Determine buy/sell/hold decisions with confidence scores
3. **Execute Trades**: Place orders based on LLM analysis when confidence exceeds threshold

### Custom Strategies

Implement custom strategies by extending the `Strategy` base class:

```python
from swm_agent.strategy.strategy import Strategy
from swm_agent.events.events import Event
from swm_agent.trader.trader import Trader

class MyCustomStrategy(Strategy):
    async def process_event(self, event: Event, trader: Trader) -> None:
        # Your custom logic here
        pass
```

## 🛡️ Risk Management

The agent includes comprehensive risk management:

- **Position Limits**: Maximum position sizes per market
- **Stop Losses**: Automatic position closure on adverse moves
- **Portfolio Limits**: Overall exposure controls
- **Order Validation**: Pre-trade risk checks

## 📈 Performance Analytics

Track trading performance with built-in analytics:

- **P&L Tracking**: Real-time profit/loss monitoring
- **Sharpe Ratio**: Risk-adjusted returns
- **Drawdown Analysis**: Maximum drawdown tracking
- **Trade Statistics**: Win rate, average trade size

## 🧪 Testing

### Unit Tests

```bash
# Run all tests
pytest

# Run specific test modules
pytest tests/strategy/
pytest tests/trader/
```

### Backtesting

```bash
# Run backtest with historical data
python -m swm_agent.backtest.backtester
```

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guidelines](CONTRIBUTING.md) for details.

### Development Setup

```bash
# Install development dependencies
poetry install --with dev

# Set up pre-commit hooks
pre-commit install

# Run linting
ruff check .
ruff format .
```

## 📄 License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This software is for educational and research purposes. Trading involves substantial risk of loss and is not suitable for all investors. Past performance does not guarantee future results.
